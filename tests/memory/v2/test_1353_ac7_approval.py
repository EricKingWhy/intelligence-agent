"""#1353 AC7：审批层（M6）——stage 后外部改文件，approve 必须拒绝并保留 record。

A 路已落地 `memory_v2_staged` + `StagedWriteStore`（`memory/v2/write_approval.py`）。
本文件从"结构契约 + 发现式探测"升级为**真实 API 的行为判据**：

1. 表与列集存在（M6 点名的最小列）；
2. 审批入口存在（`StagedWriteStore` 公开方法集白名单，见文末判据）；
3. stage → 文件未变 → approve 落盘（正控，证明重放链本身可用）；
4. stage → 外部改文件 → approve **拒绝**、状态保持 pending、**record 保留**、
   文件不被覆盖（AC7 核心）；
5. 没有 pin 的 legacy record 直接拒绝，不按 `old_text` 重搜（TOCTOU 本体）；
6. `evaluate_gate`：开关关 → allow，开关开 → stage（绝不静默拒绝）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agent_harness.memory.v2._sqlite import stamp
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.store import MemorySettingsV2, SqliteMemoryV2Store
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.memory.v2.write_approval import (
    APPROVED,
    APPROVING_LEASE,
    AWAITING,
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


def test_stage_approve_reject_surface_exists() -> None:
    """`StagedWriteStore` 公开方法集必须**恰好**是审批面七方法（白名单判据）。

    未来新增/删除公开方法都会显式红，逼着把新入口写进判据——不再是
    "任一模块名含 approval 就过"的恒真断言（Stds-P2）。
    ``initialize`` 是建表入口，与六个审批方法同属公开 API。
    """
    public_methods = {
        name
        for name, value in vars(StagedWriteStore).items()
        if not name.startswith("_") and callable(value)
    }
    assert public_methods == {
        "initialize", "stage", "resolve_pins", "pending", "get", "approve", "reject",
    }, (
        "AC7 审批面漂移：StagedWriteStore 公开方法集变化。"
        f"实际：{sorted(public_methods)}；预期：initialize + stage/resolve_pins/pending/get/approve/reject。"
        "若为有意变更，请同步更新本判据与 issue §3 M6。"
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
async def test_batch_approve_replays_pinned_ops(approval) -> None:
    """batch 的 stage→approve→`_replay` 全链路（评审 note：此前只在存储层验过 pin 通道）。

    整批是一条 pending record；重放时 pin 由 `_replay` 注入并走 `trusted_pins=True`。
    """
    staged, markdown = approval
    await _seed(markdown)
    await _seed(markdown, "gamma obsolete")
    payload = {
        "target": "memory",
        "action": "batch",
        "operations": [
            {"action": "replace", "old_text": "alpha preference", "content": "beta preference"},
            {"action": "remove", "old_text": "gamma obsolete"},
        ],
    }

    pins, error = await staged.resolve_pins(payload)
    assert error is None, error
    record = await staged.stage(payload, pins, LOCAL)
    decision, response = await staged.approve(record.id, LOCAL)

    assert decision.allow, decision
    assert response is not None and response["success"], response
    text = markdown.path_for("memory").read_text(encoding="utf-8")
    assert "beta preference" in text
    assert "alpha preference" not in text
    assert "gamma obsolete" not in text
    assert (await staged.get(record.id, LOCAL)).status == APPROVED


@pytest.mark.asyncio
async def test_batch_approve_rejects_after_external_edit_and_keeps_pending_record(approval) -> None:
    """同一全链路的负控：stage 后外部改条 ⇒ 整批拒绝、文件不被覆盖、record 保留。"""
    staged, markdown = approval
    await _seed(markdown)
    payload = {
        "target": "memory",
        "action": "batch",
        "operations": [
            {"action": "replace", "old_text": "alpha preference", "content": "beta preference"},
        ],
    }
    pins, error = await staged.resolve_pins(payload)
    assert error is None, error
    record = await staged.stage(payload, pins, LOCAL)

    path = markdown.path_for("memory")
    path.write_text("alpha preference, hand edited", encoding="utf-8")
    before = path.read_bytes()

    decision, response = await staged.approve(record.id, LOCAL)

    assert decision.blocked, decision
    assert response is None
    assert path.read_bytes() == before
    assert (await staged.get(record.id, LOCAL)).status == PENDING


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


@pytest.mark.asyncio
async def test_stale_approving_row_is_reclaimed_by_pending(approval) -> None:
    """进程在 `approving` 中间态被杀后，`pending()` 必须把超期行收回 PENDING。

    不回收的话这条待审写入既不在待审列表里，又会被 approve/reject 以
    "already being processed" 永久拒绝——静默消失。
    """
    staged, markdown = approval
    await _seed(markdown)
    record = await staged.stage(_replace_payload(), ["alpha preference"], LOCAL)

    # 模拟被 SIGKILL 的 approve：CAS 抢到 AWAITING 后再没动过。
    assert await staged._set_status(record.id, AWAITING, expected=PENDING)
    stale = stamp(datetime.now(UTC) - APPROVING_LEASE - timedelta(seconds=1))
    connection = sqlite3.connect(staged.database_path)
    try:
        connection.execute(
            "UPDATE memory_v2_staged SET claimed_at=? WHERE id=?", (stale, record.id))
        connection.commit()
    finally:
        connection.close()

    assert record.id in {item.id for item in await staged.pending(LOCAL)}, (
        "租约过期的 approving 行必须被 pending() 收回，否则这条待审写入静默消失")
    assert (await staged.get(record.id, LOCAL)).status == PENDING


@pytest.mark.asyncio
async def test_live_approving_row_is_not_reclaimed(approval) -> None:
    """租约内的 approving 行**不能**被回收——那会踩掉一个正在重放的 approve。"""
    staged, markdown = approval
    await _seed(markdown)
    record = await staged.stage(_replace_payload(), ["alpha preference"], LOCAL)
    assert await staged._set_status(record.id, AWAITING, expected=PENDING)

    assert record.id not in {item.id for item in await staged.pending(LOCAL)}
    assert (await staged.get(record.id, LOCAL)).status == AWAITING


def test_evaluate_gate_allows_when_off_and_stages_when_on() -> None:
    """M6 三态 gate：关 → allow；开 → stage（拿不到行内通道也绝不静默拒绝）。"""
    assert evaluate_gate(MemorySettingsV2()).allow is True

    staged = evaluate_gate(MemorySettingsV2(write_approval=True))
    assert staged.stage is True
    assert staged.allow is False and staged.blocked is False
    assert "Staged for approval" in staged.message
