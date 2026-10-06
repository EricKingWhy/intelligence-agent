"""W-10（#354）目录租约与等待队列的 SQLite 持久层（`harness.db`，ADR-0004 布局）。

两张表：

- `workspace_leases`   每个目录键至多一行（PK = 规范化 `dir_key`）。
  `state='held'`（`session_id` 为持有者 Task）/ `'awaiting_presence'`
  （已释放、队首等用户回来，`session_id` 为 NULL）。
- `workspace_lease_queue`  持久 FIFO：`state='waiting'` 行按 `queue_id` 升序即
  排队顺序；grant/cancel 只改 `state` 不删行（重启后顺序与历史都可见）。

连接纪律照 `workspace/store.py`（ADR-0025 D4）：每操作新连接 + WAL（initialize
一次）+ `busy_timeout` + 写走 `BEGIN IMMEDIATE` + `retry_on_busy`（#515 扩面）。

**本层只管持久化，不含语义**（谁排谁、presence 判定在 `lease.py`）；一件事例外
且刻意：`release_lease` 把"释放持有 + 原子选出队首"放进**同一个事务**——票面
"释放时原子选出队首"，中间崩溃不允许出现"无主租约 + 队首未决"的撕裂态。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from agent_harness.storage.sqlite import retry_on_busy

_BUSY_TIMEOUT_MS = 10_000

LEASE_HELD = "held"
LEASE_AWAITING_PRESENCE = "awaiting_presence"
QUEUE_WAITING = "waiting"
QUEUE_GRANTED = "granted"
QUEUE_CANCELLED = "cancelled"

_DDL = """
CREATE TABLE IF NOT EXISTS workspace_leases (
    dir_key TEXT PRIMARY KEY,
    dir_path TEXT NOT NULL,
    session_id TEXT,
    state TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS workspace_leases_holder
    ON workspace_leases(session_id) WHERE session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS workspace_lease_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dir_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting'
);

CREATE UNIQUE INDEX IF NOT EXISTS workspace_lease_queue_waiting
    ON workspace_lease_queue(session_id) WHERE state = 'waiting';
"""


@dataclass(frozen=True)
class LeaseRow:
    """`workspace_leases` 一行的只读快照。"""

    dir_key: str
    dir_path: str
    session_id: str | None
    state: str
    acquired_at: str
    updated_at: str


@dataclass(frozen=True)
class QueueRow:
    """`workspace_lease_queue` 一行的只读快照。"""

    queue_id: int
    dir_key: str
    session_id: str
    enqueued_at: str
    state: str


class SqliteLeaseStore:
    """`harness.db` 上的租约/队列持久层（无缓存、无语义）。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @asynccontextmanager
    async def _connect(self):
        connection = await aiosqlite.connect(self.database_path)
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            yield connection
        finally:
            await connection.close()

    @retry_on_busy
    async def initialize(self) -> None:
        """幂等建表（WAL 只在这里设一次）。"""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.executescript(_DDL)
            await connection.commit()

    # —— 读 ——

    @staticmethod
    async def _load_leases(connection: aiosqlite.Connection) -> list[LeaseRow]:
        async with connection.execute(
            "SELECT dir_key, dir_path, session_id, state, acquired_at, updated_at "
            "FROM workspace_leases"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_lease_row(row) for row in rows]

    async def load_leases(self) -> list[LeaseRow]:
        async with self._connect() as connection:
            return await self._load_leases(connection)

    async def load_queue(self) -> list[QueueRow]:
        """全部队列行按 queue_id 升序（含 granted/cancelled 历史）。"""
        async with self._connect() as connection, connection.execute(
            "SELECT queue_id, dir_key, session_id, enqueued_at, state "
            "FROM workspace_lease_queue ORDER BY queue_id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            QueueRow(
                queue_id=int(row["queue_id"]),
                dir_key=row["dir_key"],
                session_id=row["session_id"],
                enqueued_at=row["enqueued_at"],
                state=row["state"],
            )
            for row in rows
        ]

    # —— 写 ——

    @retry_on_busy
    async def insert_lease(
        self, dir_key: str, dir_path: str, session_id: str, now: str
    ) -> None:
        """新持有（纯 INSERT：PK 即 CAS，撞上说明有另一份写者，响亮失败）。"""
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "INSERT INTO workspace_leases "
                "(dir_key, dir_path, session_id, state, acquired_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (dir_key, dir_path, session_id, LEASE_HELD, now, now),
            )
            await connection.commit()

    @retry_on_busy
    async def insert_waiting(self, dir_key: str, session_id: str, now: str) -> int:
        """入队（纯 INSERT；部分唯一索引挡同 session 重复 waiting）。"""
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "INSERT INTO workspace_lease_queue "
                "(dir_key, session_id, enqueued_at, state) VALUES (?, ?, ?, ?)",
                (dir_key, session_id, now, QUEUE_WAITING),
            )
            await connection.commit()
            return int(cursor.lastrowid or 0)

    @retry_on_busy
    async def set_queue_state(self, queue_id: int, state: str) -> None:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "UPDATE workspace_lease_queue SET state=? WHERE queue_id=?",
                (state, queue_id),
            )
            await connection.commit()

    @retry_on_busy
    async def release_lease(
        self, session_id: str, promote_session_id: str | None, now: str
    ) -> str:
        """释放 + 原子选出队首（**单事务**，模块 docstring）。

        - 调用方不是持有者 → `'not-holder'`（重复释放安全，幂等 no-op）；
        - `promote_session_id` 非空 → 队首行转 `granted`、租约同事务转授给它，
          返回 `'promoted'`；
        - 否则该键**有** waiting 队首 → 租约转 `awaiting_presence`（无主等
          用户回来），返回 `'awaiting'`；
        - 该键无任何 waiting → 租约行删除，目录回到完全空闲，返回 `'freed'`。

        `promote_session_id` 由语义层先按 presence 只读合同判定后传入；本方法
        只负责把它与释放合成一个原子步骤。
        """
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                async with connection.execute(
                    "SELECT dir_key FROM workspace_leases "
                    "WHERE session_id=? AND state=?",
                    (session_id, LEASE_HELD),
                ) as cursor:
                    holder = await cursor.fetchone()
                if holder is None:
                    await connection.rollback()
                    return "not-holder"
                dir_key = holder["dir_key"]
                promote_queue_id: int | None = None
                async with connection.execute(
                    "SELECT queue_id FROM workspace_lease_queue "
                    "WHERE dir_key=? AND state=? ORDER BY queue_id LIMIT 1",
                    (dir_key, QUEUE_WAITING),
                ) as cursor:
                    head = await cursor.fetchone()
                if promote_session_id is not None and head is not None:
                    async with connection.execute(
                        "SELECT session_id FROM workspace_lease_queue "
                        "WHERE queue_id=?",
                        (int(head["queue_id"]),),
                    ) as cursor:
                        head_row = await cursor.fetchone()
                    if head_row is not None and (
                        head_row["session_id"] == promote_session_id
                    ):
                        promote_queue_id = int(head["queue_id"])
                    else:
                        # 语义层判的在场对象不是队首（调用方失配）：按无队首
                        # 处理，不静默越位转授。
                        promote_session_id = None
                if promote_queue_id is not None and promote_session_id is not None:
                    await connection.execute(
                        "UPDATE workspace_lease_queue SET state=? WHERE queue_id=?",
                        (QUEUE_GRANTED, promote_queue_id),
                    )
                    await connection.execute(
                        "UPDATE workspace_leases SET session_id=?, state=?, "
                        "updated_at=? WHERE dir_key=?",
                        (promote_session_id, LEASE_HELD, now, dir_key),
                    )
                    result = "promoted"
                elif head is not None:
                    await connection.execute(
                        "UPDATE workspace_leases SET session_id=NULL, state=?, "
                        "updated_at=? WHERE dir_key=?",
                        (LEASE_AWAITING_PRESENCE, now, dir_key),
                    )
                    result = "awaiting"
                else:
                    await connection.execute(
                        "DELETE FROM workspace_leases WHERE dir_key=?", (dir_key,)
                    )
                    result = "freed"
                await connection.commit()
                return result
            except BaseException:
                await connection.rollback()
                raise

    @retry_on_busy
    async def grant_awaiting(self, dir_key: str, session_id: str, now: str) -> None:
        """`awaiting_presence` → `held` 的显式转授（单事务：队首行转 granted）。

        仅当租约确实处于 awaiting 态才生效（WHERE state 守卫 = 条件 CAS）；
        竞争下已被他人转授/摘除时静默 no-op，由语义层重读事实。
        """
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            async with connection.execute(
                "SELECT queue_id FROM workspace_lease_queue "
                "WHERE dir_key=? AND session_id=? AND state=? "
                "ORDER BY queue_id LIMIT 1",
                (dir_key, session_id, QUEUE_WAITING),
            ) as cursor:
                head = await cursor.fetchone()
            if head is not None:
                await connection.execute(
                    "UPDATE workspace_lease_queue SET state=? WHERE queue_id=?",
                    (QUEUE_GRANTED, int(head["queue_id"])),
                )
                await connection.execute(
                    "UPDATE workspace_leases SET session_id=?, state=?, updated_at=? "
                    "WHERE dir_key=? AND state=?",
                    (session_id, LEASE_HELD, now, dir_key, LEASE_AWAITING_PRESENCE),
                )
            await connection.commit()

    @retry_on_busy
    async def drop_awaiting(self, dir_key: str) -> None:
        """摘除已无等待对象的 `awaiting_presence` 租约（目录回到空闲）。"""
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "DELETE FROM workspace_leases WHERE dir_key=? AND state=?",
                (dir_key, LEASE_AWAITING_PRESENCE),
            )
            await connection.commit()

    @retry_on_busy
    async def drop_session_rows(self, session_id: str) -> int:
        """重启对账：删掉该 session 的持有行与 waiting 行（已失效会话）。"""
        dropped = 0
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "DELETE FROM workspace_leases WHERE session_id=?", (session_id,)
            )
            dropped += max(cursor.rowcount, 0)
            cursor = await connection.execute(
                "DELETE FROM workspace_lease_queue "
                "WHERE session_id=? AND state=?",
                (session_id, QUEUE_WAITING),
            )
            dropped += max(cursor.rowcount, 0)
            await connection.commit()
        return dropped


def _lease_row(row: aiosqlite.Row) -> LeaseRow:
    return LeaseRow(
        dir_key=row["dir_key"],
        dir_path=row["dir_path"],
        session_id=row["session_id"],
        state=row["state"],
        acquired_at=row["acquired_at"],
        updated_at=row["updated_at"],
    )
