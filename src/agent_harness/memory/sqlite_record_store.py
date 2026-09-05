"""SQLite 权威记录。记录与 outbox 在同一事务中提交。"""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import aiosqlite

from agent_harness.identity import IdentityContext
from agent_harness.memory.record_store import PendingMemory
from agent_harness.memory.types import MemoryEntry, MemoryNamespace, MemoryScope

#: 每操作新连接模式下的连接级 PRAGMA（与 storage/sqlite.py R4-5 同款）：
#: writeback 的 store 与 relay 的 pending/ack 并发写（BEGIN IMMEDIATE），
#: 默认 busy_timeout=0 会立刻抛 "database is locked" 而不是等锁。
_BUSY_TIMEOUT_MS = 10_000


@asynccontextmanager
async def _connect(database_path: Path):
    """打开一个带 busy_timeout 的连接（close 随 CM 退出）。"""
    connection = await aiosqlite.connect(database_path)
    try:
        await connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        yield connection
    finally:
        await connection.close()


class SqliteMemoryRecordStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with _connect(self.database_path) as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.executescript("""
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
            await connection.commit()

    async def store(self, entry: MemoryEntry, identity: IdentityContext) -> str:
        namespace = MemoryNamespace.of(entry.scope, identity).as_json()
        async with _connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            async with connection.execute("SELECT namespace FROM memory_records WHERE memory_id=?", (entry.id,)) as cursor:
                previous = await cursor.fetchone()
            if previous and previous[0] != namespace:
                raise PermissionError("Memory belongs to a different namespace")
            await connection.execute("""
                INSERT INTO memory_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, FALSE)
                ON CONFLICT(memory_id) DO UPDATE SET content=excluded.content,
                    metadata=excluded.metadata, indexed=FALSE
            """, (entry.id, identity.tenant_id, identity.user_id, entry.scope.value, namespace,
                  entry.content, json.dumps(entry.metadata, ensure_ascii=False), entry.created_at))
            await connection.execute("""
                INSERT INTO memory_outbox(memory_id, revision) VALUES (?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision
            """, (entry.id, str(uuid4())))
            await connection.commit()
        return entry.id

    async def get(self, memory_id: str, identity: IdentityContext) -> MemoryEntry:
        async with _connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute("""
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
        async with _connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute("""
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
        async with _connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            async with connection.execute("""
                SELECT r.*, o.revision FROM memory_records r JOIN memory_outbox o
                    ON r.memory_id=o.memory_id WHERE r.memory_id > ? ORDER BY r.memory_id LIMIT ?
            """, (after_id, max(0, limit))) as cursor:
                rows = await cursor.fetchall()
        return [PendingMemory(self._entry(row),
                              IdentityContext(row["tenant_id"], row["user_id"], [row["scope"]]),
                              MemoryNamespace(tuple(json.loads(row["namespace"]))).session_id,
                              str(row["revision"])) for row in rows]

    async def acknowledge(self, change: PendingMemory) -> bool:
        async with _connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute("DELETE FROM memory_outbox WHERE memory_id=? AND revision=?",
                                              (change.entry.id, change.revision))
            matched = cursor.rowcount == 1
            if matched:
                await connection.execute("UPDATE memory_records SET indexed=TRUE WHERE memory_id=?",
                                         (change.entry.id,))
            await connection.commit()
        return matched
