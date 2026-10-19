"""#1353 AC7：审批层（M6）——stage 后外部改文件，approve 必须拒绝并保留 record。

A 路已落地 `memory_v2_staged` + `StagedWriteStore`（`memory/v2/write_approval.py`）。
本文件从"结构契约 + 发现式探测"升级为**真实 API 的行为判据**：

1. 表与列集存在（M6 点名的最小列）；
2. 审批入口存在（发现式探测，不预设单一命名）；
3. stage → 文件未变 → approve 落盘（正控，证明重放链本身可用）；
4. stage → 外部改文件 → approve **拒绝**、状态保持 pending、**record 保留**、
   文件不被覆盖（AC7 核心）；
5. 没有 pin 的 legacy record 直接拒绝，不按 `old_text` 重搜（TOCTOU 本体）；
6. `evaluate_gate`：开关关 → allow，开关开 → stage（绝不静默拒绝）。
"""

from __future__ import annotations

import pkgutil
import sqlite3

import pytest
import pytest_asyncio

import agent_harness.memory.v2 as v2_package
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.store import MemorySettingsV2, SqliteMemoryV2Store
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.memory.v2.write_approval import (
    APPROVED,
    PENDING,
    REJECTED,
    StagedWriteStore,
    evaluate_gate,
)

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


def test_stage_approve_reject_surface_exists() -> None:
    """审批入口必须存在，否则 "stage → 外部改文件 → approve 拒绝且保留 record" 不可执行。"""
    surface = _approval_surface()
    assert surface, (
        "AC7 审批层缺口：找不到任何 staging/approve 入口——"
        "memory.v2 下无 approval/staged 模块，SqliteMemoryV2Store 上也无可调用审批方法。"
        "M6 要求的 memory_v2_staged + 审批回调（web/CLI）均未落地。"
    )


#: 单用户部署的固定身份（issue §0 架构前提：tenant/user 固定透传）。
LOCAL = TrustedMemoryIdentity(tenant_id="local", user_id="local")


@pytest_asyncio.fixture()
async def approval(tmp_path):
    """真实 `StagedWriteStore` + 真实 markdown 文件；不用 mock 替换审批链。"""
    markdown = MarkdownMemoryStore(tmp_path / "memories")
    staged = StagedWriteStore(tmp_path / "memory-v2.db", markdown)
    await staged.initialize()
    return staged, markdown


async def _seed(markdown: MarkdownMemoryStore, content: str = "alpha preference") -> None:
    response = await markdown.add("memory", content)
    assert response["success"], response


def _replace_payload(old_text: str = "alpha preference", content: str = "beta preference") -> dict:
    return {
        "target": "memory",
        "action": "replace",
        "old_text": old_text,
        "content": content,
    }


@pytest.mark.asyncio
async def test_approve_applies_pinned_replacement_when_file_is_unchanged(approval) -> None:
    """正控：文件没变时 approve 必须真的重放成功——否则"拒绝"用例可能只是永远红。"""
    staged, markdown = approval
    await _seed(markdown)
    payload = _replace_payload()

    pins, error = await staged.resolve_pins(payload)
    assert error is None, error
    assert pins == ["alpha preference"], pins

    record = await staged.stage(payload, pins, LOCAL)
    decision, response = await staged.approve(record.id, LOCAL)

    assert decision.allow, decision
    assert response is not None and response["success"], response
    text = markdown.path_for("memory").read_text(encoding="utf-8")
    assert "beta preference" in text
    assert "alpha preference" not in text
    assert (await staged.get(record.id, LOCAL)).status == APPROVED


@pytest.mark.asyncio
async def test_approve_rejects_after_external_edit_and_keeps_pending_record(approval) -> None:
    """AC7 核心：stage 后外部改文件 → approve 拒绝、文件不被覆盖、record 保留。"""
    staged, markdown = approval
    await _seed(markdown)
    payload = _replace_payload()

    pins, error = await staged.resolve_pins(payload)
    assert error is None, error
    record = await staged.stage(payload, pins, LOCAL)

    path = markdown.path_for("memory")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "alpha preference", "alpha preference (edited by user)"
        ),
        encoding="utf-8",
    )

    decision, response = await staged.approve(record.id, LOCAL)

    assert decision.blocked, decision
    assert response is None
    kept = await staged.get(record.id, LOCAL)
    assert kept is not None, "pin 失效时 record 必须保留，供用户 reject 或重建"
    assert kept.status == PENDING
    assert record.id in {item.id for item in await staged.pending(LOCAL)}
    text = path.read_text(encoding="utf-8")
    assert "alpha preference (edited by user)" in text
    assert "beta preference" not in text

    rejected = await staged.reject(record.id, LOCAL)
    assert rejected.allow, rejected
    assert (await staged.get(record.id, LOCAL)).status == REJECTED


@pytest.mark.asyncio
async def test_legacy_record_without_pin_is_refused_not_reresolved(approval) -> None:
    """没有 pin 的 legacy record 直接拒绝——按 old_text 重搜正是 TOCTOU 本体。"""
    staged, markdown = approval
    await _seed(markdown)
    payload = _replace_payload()

    record = await staged.stage(payload, None, LOCAL)
    decision, response = await staged.approve(record.id, LOCAL)

    assert decision.blocked, decision
    assert "pinned entry" in decision.message
    assert response is None
    assert (await staged.get(record.id, LOCAL)).status == PENDING
    text = markdown.path_for("memory").read_text(encoding="utf-8")
    assert "alpha preference" in text
    assert "beta preference" not in text


def test_evaluate_gate_allows_when_off_and_stages_when_on() -> None:
    """M6 三态 gate：关 → allow；开 → stage（拿不到行内通道也绝不静默拒绝）。"""
    assert evaluate_gate(MemorySettingsV2()).allow is True

    staged = evaluate_gate(MemorySettingsV2(write_approval=True))
    assert staged.stage is True
    assert staged.allow is False and staged.blocked is False
    assert "Staged for approval" in staged.message
