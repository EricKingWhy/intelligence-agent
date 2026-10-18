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

行为面同理：`test_stage_approve_reject_surface_exists` 发现式探测审批入口
（不预设单一命名），入口不存在即红。**"stage → 外部改文件 → approve 拒绝且
保留 record"这条行为用例必须在 M6 落地后按真实 API 补写**——对着不存在的接口
编调用形状只会得到一条永假的测试，故此处只钉"面是否存在"。
"""

from __future__ import annotations

import pkgutil
import sqlite3

import pytest
import pytest_asyncio

import agent_harness.memory.v2 as v2_package
from agent_harness.memory.v2.store import SqliteMemoryV2Store

#: issue §3 M6 点名的最小列集（`id / identity / payload / pinned_entry / status / created_at`）。
STAGED_COLUMNS = {"id", "identity", "payload", "pinned_entry", "status", "created_at"}


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


def _approval_surface() -> set[str]:
    """实现侧审批面的落点：staging 模块名或 store 上的审批方法，发现式探测。

    不预设单一命名（A 路可用 `stage` / `approve` / `reject` 等任一命名），
    只要求"审批这一步在代码里存在"——否则 AC7 的行为用例无处落笔。
    """
    found = {
        f"memory.v2.{module.name}"
        for module in pkgutil.iter_modules(v2_package.__path__)
        if "approval" in module.name or "staged" in module.name
    }
    for name in ("stage", "stage_write", "approve", "approve_write", "reject"):
        if callable(getattr(SqliteMemoryV2Store, name, None)):
            found.add(f"SqliteMemoryV2Store.{name}")
    return found


@pytest.mark.xfail(
    strict=True,
    reason="M6 staged approval 未实现（实现侧 P0 缺口，已报协调员；入口落地后本条转 XPASS）",
)
def test_stage_approve_reject_surface_exists() -> None:
    """审批入口必须存在，否则 "stage → 外部改文件 → approve 拒绝且保留 record" 不可执行。"""
    surface = _approval_surface()
    assert surface, (
        "AC7 审批层缺口：找不到任何 staging/approve 入口——"
        "memory.v2 下无 approval/staged 模块，SqliteMemoryV2Store 上也无可调用审批方法。"
        "M6 要求的 memory_v2_staged + 审批回调（web/CLI）均未落地。"
    )
