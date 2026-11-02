"""#1353 AC7 + #1494：审批层的文件 owner——stage 后外部改文件，approve 必须拒绝并保留。

#1494 把 `memory_v2_staged` 的**运行时**读写迁到 `memory-state/staged/<id>.json`
（ADR-0058 D5）。本文件的判据随所有者一起搬：

1. 一条待审写入 = 一个 JSON 文件，键集固定（id / identity / payload / pinned_entry /
   status / created_at / claimed_at）；
2. 审批面公开方法集白名单（新增入口必须同步判据）；
3. stage → 文件未变 → approve 落盘（正控，证明重放链本身可用）；
4. stage → 外部改文件 → approve **拒绝**、状态保持 pending、**record 保留**、
   文件不被覆盖（AC7 核心）；
5. 没有 pin 的 legacy record 直接拒绝，不按 `old_text` 重搜（TOCTOU 本体）；
6. legacy `approving` 行（旧 SQLite 导入）按租约回收成 PENDING，租约内不回收；
7. `evaluate_gate`：开关关 → allow，开关开 → stage（绝不静默拒绝）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agent_harness.memory.v2._sqlite import stamp
from agent_harness.memory.v2.commit import build_markdown_commit
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.store import MemorySettingsV2
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

#: 文件 owner 的最小键集（旧表列集在 ADR-0058 D5 对读时逐列保留）。
STAGED_KEYS = {
    "id", "identity", "payload", "pinned_entry", "status", "created_at", "claimed_at",
}

#: 单用户部署的固定身份（issue §0 架构前提：tenant/user 固定透传）。
LOCAL = TrustedMemoryIdentity(tenant_id="local", user_id="local")


@pytest_asyncio.fixture()
async def approval(tmp_path):
    """真实 `StagedWriteStore`（文件）+ 真实 markdown 文件；不用 mock 替换审批链。"""
    markdown = MarkdownMemoryStore(tmp_path / "memories")
    commit = build_markdown_commit(markdown, tmp_path)
    staged = StagedWriteStore(commit.root, commit)
    await staged.initialize()
    return staged, markdown, commit


async def _seed(commit, content: str = "alpha preference") -> None:
    response = await commit.add("memory", content)
    assert response["success"], response


def _replace_payload(old_text: str = "alpha preference", content: str = "beta preference") -> dict:
    return {
        "target": "memory",
        "action": "replace",
        "old_text": old_text,
        "content": content,
    }


def _rewrite(staged: StagedWriteStore, staged_id: str, **changes) -> None:
    path = staged.path_for(staged_id)
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


@pytest.mark.asyncio
async def test_staged_record_is_a_state_file_with_fixed_keys(approval) -> None:
    """一条待审写入落成 `staged/<id>.json`，键集与旧表列集一一对应。"""
    staged, _markdown, commit = approval
    await _seed(commit)
    record = await staged.stage(_replace_payload(), ["alpha preference"], LOCAL)

    path = staged.root.staged_dir / f"{record.id}.json"
    assert path.exists()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert STAGED_KEYS <= set(document)
    assert document["status"] == PENDING
    assert document["pinned_entry"] == ["alpha preference"]
    assert document["project_id"] is None


def test_staged_surface_exists() -> None:
    """`StagedWriteStore` 公开方法集必须**恰好**是审批面（白名单判据）。

    `path_for` 是文件 owner 的只读诊断入口（CLI/测试据此报位置），不是第二个写口。
    """
    public_methods = {
        name
        for name, value in vars(StagedWriteStore).items()
        if not name.startswith("_") and callable(value)
    }
    assert public_methods == {
        "initialize", "path_for", "stage", "resolve_pins", "pending", "get",
        "approve", "reject",
    }, (
        "AC7 审批面漂移：StagedWriteStore 公开方法集变化。"
        f"实际：{sorted(public_methods)}。"
        "若为有意变更，请同步更新本判据与 issue §3 M6。"
    )


@pytest.mark.asyncio
async def test_approve_applies_pinned_replacement_when_file_is_unchanged(approval) -> None:
    """正控：文件没变时 approve 必须真的重放成功——否则"拒绝"用例可能只是永远红。"""
    staged, markdown, commit = approval
    await _seed(commit)
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
    """batch 的 stage→approve→重放全链路：整批一条 record，pin 按操作顺序存。"""
    staged, markdown, commit = approval
    await _seed(commit)
    await _seed(commit, "gamma obsolete")
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
    staged, markdown, commit = approval
    await _seed(commit)
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
    staged, markdown, commit = approval
    await _seed(commit)
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
    staged, markdown, commit = approval
    await _seed(commit)
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
    staged, _markdown, commit = approval
    await _seed(commit)
    record = await staged.stage(_replace_payload(), ["alpha preference"], LOCAL)
    stale = stamp(datetime.now(UTC) - APPROVING_LEASE - timedelta(seconds=1))
    _rewrite(staged, record.id, status=AWAITING, claimed_at=stale)

    assert record.id in {item.id for item in await staged.pending(LOCAL)}, (
        "租约过期的 approving 行必须被 pending() 收回，否则这条待审写入静默消失")
    assert (await staged.get(record.id, LOCAL)).status == PENDING


@pytest.mark.asyncio
async def test_live_approving_row_is_not_reclaimed(approval) -> None:
    """租约内的 approving 行**不能**被回收——那会踩掉一个正在重放的 approve。"""
    staged, _markdown, commit = approval
    await _seed(commit)
    record = await staged.stage(_replace_payload(), ["alpha preference"], LOCAL)
    _rewrite(staged, record.id, status=AWAITING, claimed_at=stamp())

    assert record.id not in {item.id for item in await staged.pending(LOCAL)}
    assert (await staged.get(record.id, LOCAL)).status == AWAITING


def test_evaluate_gate_allows_when_off_and_stages_when_on() -> None:
    """M6 三态 gate：关 → allow；开 → stage（拿不到行内通道也绝不静默拒绝）。"""
    assert evaluate_gate(MemorySettingsV2()).allow is True

    staged = evaluate_gate(MemorySettingsV2(write_approval=True))
    assert staged.stage is True
    assert staged.allow is False and staged.blocked is False
    assert "Staged for approval" in staged.message
