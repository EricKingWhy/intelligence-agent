"""#1353 AC7：审批层（M6）——stage 后外部改文件，approve 必须拒绝并保留 record。

# 现状（B 路合并态实测，2026-10-19）

store 层 pinning 已落地（`tests/memory/v2/test_1353_ac7_pinning.py` 全绿：
`resolve_entry` / `matched_entry` / `resolve_batch_entries` 都会在条目变化后拒绝写入）。
**审批层缺口**：`src/agent_harness/memory/v2/` 下没有 staging 模块，
`SqliteMemoryV2Store` 只建 records / outbox / tombstones / settings 四张表，
issue §3 M6 要求的 `memory_v2_staged` 与 `/memory pending|approve|reject` 均不存在。
⇒ "stage → 外部改文件 → approve 拒绝且保留 record"这条路径**当前不可执行**。

# 这个文件的作用

把 M6 的**结构契约**钉成 fail-closed 用例：表不存在时 `xfail(strict=True)`
（不让 AC7 在无声中消失，也不伪装成 pass）；A 路补上表后 strict xfail 会变成
XPASS 并**转红**，强制把端到端行为用例补全后再摘掉标记。
"""

from __future__ import annotations

import sqlite3

import pytest
import pytest_asyncio

from agent_harness.memory.v2.store import SqliteMemoryV2Store

#: issue §3 M6 点名的最小列集。
STAGED_COLUMNS = {"id", "payload", "pinned_entry", "status", "created_at"}


@pytest_asyncio.fixture()
async def store(tmp_path):
    instance = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await instance.initialize()
    return instance


def _tables(database) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()


def _columns(database, table: str) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


@pytest.mark.xfail(
    strict=True,
    reason="M6 staged approval 未实现（实现侧 P0 缺口，已报协调员；表落地后本条转 XPASS）",
)
@pytest.mark.asyncio
async def test_staged_approval_schema_exists(store) -> None:
    """`memory_v2_staged` 必须存在且带 issue 点名的最小列集。"""
    database = store.database_path
    tables = _tables(database)
    assert "memory_v2_staged" in tables, (
        "AC7 审批层缺口：memory_v2_staged 不存在。"
        f"现有表：{sorted(tables)}"
    )
    assert STAGED_COLUMNS <= _columns(database, "memory_v2_staged")
