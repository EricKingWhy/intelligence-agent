"""SQLite 权威记录。记录与 outbox 在同一事务中提交。"""

import json
from pathlib import Path
from uuid import uuid4

import aiosqlite

from agent_harness.identity import IdentityContext
from agent_harness.memory.record_store import PendingMemory
from agent_harness.memory.types import MemoryEntry, MemoryNamespace, MemoryScope


class SqliteMemoryRecordStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_records (
                    memory_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, scope TEXT NOT NULL, namespace TEXT NOT NULL,
                    content TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL,
                    indexed BOOLEAN NOT NULL DEFAULT FALSE
                );
                CREATE INDEX IF NOT EXISTS memory_owner
                    ON memory_records(tenant_id, user_id, scope);
                CREATE TABLE IF NOT EXISTS memory_outbox (
                    memory_id TEXT PRIMARY KEY, revision TEXT NOT NULL
                );
            """)
            await db.commit()

    async def store(self, entry: MemoryEntry, identity: IdentityContext) -> str:
        namespace = MemoryNamespace.of(entry.scope, identity).as_json()
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT namespace FROM memory_records WHERE memory_id=?", (entry.id,)) as cursor:
                previous = await cursor.fetchone()
            if previous and previous[0] != namespace:
                raise PermissionError("Memory belongs to a different namespace")
            await db.execute("""
                INSERT INTO memory_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, FALSE)
                ON CONFLICT(memory_id) DO UPDATE SET content=excluded.content,
                    metadata=excluded.metadata, indexed=FALSE
            """, (entry.id, identity.tenant_id, identity.user_id, entry.scope.value, namespace,
                  entry.content, json.dumps(entry.metadata, ensure_ascii=False), entry.created_at))
            await db.execute("""
                INSERT INTO memory_outbox(memory_id, revision) VALUES (?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision
            """, (entry.id, str(uuid4())))
            await db.commit()
        return entry.id

    async def get(self, memory_id: str, identity: IdentityContext) -> MemoryEntry:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM memory_records WHERE memory_id=? AND tenant_id=? AND user_id=?
            """, (memory_id, identity.tenant_id, identity.user_id)) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise KeyError(memory_id)
        if json.loads(row["namespace"]) != list(MemoryNamespace.of(MemoryScope(row["scope"]), identity).as_tuple()):
            raise KeyError(memory_id)
        return self._entry(row)

    async def list_by_scope(
        self, scope: MemoryScope, identity: IdentityContext, limit: int,
    ) -> list[MemoryEntry]:
        namespace = MemoryNamespace.of(scope, identity).as_json()
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM memory_records WHERE tenant_id=? AND user_id=? AND namespace=?
                ORDER BY created_at DESC, memory_id DESC LIMIT ?
            """, (identity.tenant_id, identity.user_id, namespace, max(0, limit))) as cursor:
                return [self._entry(row) for row in await cursor.fetchall()]

    @staticmethod
    def _entry(row: aiosqlite.Row) -> MemoryEntry:
        return MemoryEntry(id=row["memory_id"], content=row["content"], metadata=json.loads(row["metadata"]),
                           created_at=row["created_at"], scope=row["scope"], indexed=bool(row["indexed"]))

    async def pending(self, limit: int = 100, after_id: str = "") -> list[PendingMemory]:
        """仅 relay 调用的系统级 outbox 读取，不暴露给模型/请求。"""
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT r.*, o.revision FROM memory_records r JOIN memory_outbox o
                    ON r.memory_id=o.memory_id WHERE r.memory_id > ? ORDER BY r.memory_id LIMIT ?
            """, (after_id, max(0, limit))) as cursor:
                rows = await cursor.fetchall()
        return [PendingMemory(self._entry(row),
                              IdentityContext(row["tenant_id"], row["user_id"], [row["scope"]]),
                              MemoryNamespace(tuple(json.loads(row["namespace"]))).session_id,
                              str(row["revision"])) for row in rows]

    async def acknowledge(self, change: PendingMemory) -> bool:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("DELETE FROM memory_outbox WHERE memory_id=? AND revision=?",
                                      (change.entry.id, change.revision))
            matched = cursor.rowcount == 1
            if matched:
                await db.execute("UPDATE memory_records SET indexed=TRUE WHERE memory_id=?",
                                 (change.entry.id,))
            await db.commit()
        return matched
