"""V2 存储层共用的 SQLite 连接策略与时间戳格式。

`SqliteMemoryV2Store`（记录 / outbox）与 `SqliteMemoryV2JobStore`（formation job）
落在**同一个** `memory-v2.db`（PRD §7.2 第 3 条要求复用既有基底），所以"怎么连、
怎么设超时、怎么写时间戳"必须是同一份实现——两份拷贝迟早漂移成两种并发行为。

# 为什么每操作新连接 + busy_timeout

与 V1（`memory/sqlite_record_store.py`）同款：写路径用 `BEGIN IMMEDIATE`，而
writeback、索引 relay、formation job 会并发写同一个库；没有 busy_timeout 时
第二次写会立刻抛 "database is locked" 而不是排队。

# 为什么时间戳必须归一到 UTC

`lease_expires_at <= now`、`ORDER BY created_at` 这类判据是**在 SQL 里做字符串比较**
的（lease 的 CAS 不能先读回 Python 再比，否则不是原子的）。字符串序要等于时间序，
前提是所有值都在同一个偏移量上——`+08:00` 与 `+00:00` 混在一起时字典序会排错。
`stamp` 因此一律 `astimezone(UTC)`。宽度不必固定：`isoformat()` 省略微秒的那一档
恰好等价于 `.000000`，字典序与时间序仍然一致（`'+' < '.'`）。

`stamp` 存在的另一个理由是**可注入的时钟**：AC6 的 kill/重启与 lease 到期用例要把
"三分钟后"喂进来，而不是真的等三分钟。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

BUSY_TIMEOUT_MS = 10_000


@asynccontextmanager
async def connect(database_path: Path):
    connection = await aiosqlite.connect(database_path)
    try:
        connection.row_factory = aiosqlite.Row
        await connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        yield connection
    finally:
        await connection.close()


def stamp(moment: datetime | None = None) -> str:
    """UTC 归一化后的 ISO 时间戳（见模块 docstring）。"""
    return (moment if moment is not None else datetime.now(UTC)).astimezone(UTC).isoformat()
