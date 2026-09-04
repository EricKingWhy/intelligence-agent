"""SQLite 权威记录。记录与 outbox 在同一事务中提交。"""

import json
from pathlib import Path

import aiosqlite

from agent_harness.identity import IdentityContext
from agent_harness.memory.types import MemoryEntry, MemoryScope, scope_to_namespace


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
                    memory_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1
                );
            """)
            await db.commit()

    async def store(self, entry: MemoryEntry, identity: IdentityContext) -> str:
        namespace = json.dumps(scope_to_namespace(entry.scope, identity))
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
                INSERT INTO memory_outbox(memory_id) VALUES (?)
                ON CONFLICT(memory_id) DO UPDATE SET revision=revision+1
            """, (entry.id,))
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
        if json.loads(row["namespace"]) != list(scope_to_namespace(MemoryScope(row["scope"]), identity)):
            raise KeyError(memory_id)
        return self._entry(row)

    async def list_by_scope(
        self, scope: MemoryScope, identity: IdentityContext, limit: int,
    ) -> list[MemoryEntry]:
        namespace = json.dumps(scope_to_namespace(scope, identity))
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
