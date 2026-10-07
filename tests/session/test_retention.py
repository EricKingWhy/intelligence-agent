"""#368 [W-24]：证据保留、空间显示与显式清理预览（服务端 Contract）。

覆盖 Batch B 的三件套：

- ``get_session_usage``：会话占用近似/精确大小（Docker ``system df`` 列语义）；
- ``preview_artifact_cleanup``：可达集扫描 + 快照 token（CAS）+ blocked/affected 明细；
- ``execute_artifact_cleanup``：token 复验 + 逐 ref 重验 + cleaned 集合落盘。

可达集判据（本票校正后）：会话内多引用者 + fork 父子引用 + 未决 Operation。
``agent-progress/`` 与工作目录源码绝不进入清理面；只删自己拼出的 artifact 路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_harness.config import Settings
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import (
    ARTIFACT_CREATED,
    ARTIFACT_EXTERNALIZED,
    EVIDENCE_RECORDED,
    TOOL_CALL,
    TOOL_RESULT,
)
from agent_harness.session.service import (
    ActiveRunConflict,
    SnapshotTokenMismatch,
)
from agent_harness.session.task import apply_task_definition, derive_task_state
from agent_harness.storage.local_artifact import LocalArtifactStore
from agent_harness.storage.operation import Operation, OperationState
from agent_harness.storage.session_meta import SessionMeta
from tests.session.ledger_doubles import idle_operation_ledger


@pytest.fixture
def env(tmp_path):
    """隔离的 settings + 真实 JSONL store（sessions 根与 artifact 根都在 tmp 下）。"""
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
        enable_cors=False,
    )
    store = JsonlSessionStore(root=tmp_path / "sessions")
    return settings, store


def _make_service(make_session_service, settings, store, *, busy=False, ledger=None, metas=()):
    """按显式 collaborators 造 service；run_manager / meta / ledger 都是替身。

    形状按真实契约给：``is_busy`` 显式答 False（裸 MagicMock 恒真会把删除判成 409），
    ``list_all`` 答给定 metas。
    """
    run_manager = MagicMock()
    run_manager.is_busy = MagicMock(return_value=busy)
    run_manager.get_active = MagicMock(return_value=None)
    meta_store = AsyncMock()
    meta_store.list_all = AsyncMock(return_value=list(metas))
    return make_session_service(
        store=store,
        settings=settings,
        run_manager=run_manager,
        session_meta_store=meta_store,
        operation_ledger=ledger if ledger is not None else idle_operation_ledger(),
    )


async def _add_artifact(settings, session_id, content):
    """写一个真实本地 artifact，返回 (artifact_id, size)。"""
    store = LocalArtifactStore(settings, session_id=session_id)
    artifact = await store.save(
        session_id, content, mime_type="text/plain",
        source_tool="tool", tool_call_id="tc-1",
    )
    return artifact.artifact_id, artifact.size


def _evidence_payload(session_id, *, evidence_id, criterion_id, artifact_ref=None):
    """合法 14 字段证据 DTO（derive_evidence_state 能解析）。"""
    return {
        "evidence_id": evidence_id,
        "task_session_id": session_id,
        "run_id": "run-1",
        "criterion_id": criterion_id,
        "kind": "test",
        "source_event_seq": 1,
        "tool_call_id": None,
        "captured_at": "2026-10-06T15:00:00.000+00:00",
        "result": "pass",
        "command_or_action": "pytest -q",
        "exit_code_or_observation": 0,
        "artifact_ref": artifact_ref,
        "base_head": None,
        "workspace_manifest": {
            "files": [], "manifest_hash": "c" * 64, "progress_md_sha256": None,
        },
    }


async def _session_with_evidence_artifact(settings, store, *, content="ev-content"):
    """带 task 定义 + 出生证明 artifact + 引用它的证据的会话。

    返回 ``(session, sid, ref, criterion_id)``；artifact 走真实 LocalArtifactStore
    保存（tool_call_id 与后续事件一致 → 判出生证明，不阻断清理），证据引用因
    fixture 无工作区 → fail-closed stale（不阻断）。供投影端到端用例复用。
    """
    session = Session.start(store)
    sid = session.session_id
    outcome = apply_task_definition(
        session, task_text="清理证据原件", criteria=[{"text": "原件可被清理"}]
    )
    assert outcome.ok, outcome.reason
    criterion_id = derive_task_state(session.events).criteria[0].item_id
    artifact = await LocalArtifactStore(settings, session_id=sid).save(
        sid, content, mime_type="text/plain", source_tool="bash", tool_call_id="tc-1"
    )
    ref = artifact.artifact_id
    session.append(
        TOOL_CALL, {"tool_call_id": "tc-1", "tool_name": "bash", "arguments": {}}
    )
    session.append(
        ARTIFACT_EXTERNALIZED,
        {
            "artifact_id": ref, "session_id": sid, "source_tool": "bash",
            "tool_call_id": "tc-1", "size": artifact.size, "mime_type": "text/plain",
        },
    )
    session.append(
        TOOL_RESULT,
        {"tool_call_id": "tc-1", "content": json.dumps({"artifact_ref": ref})},
    )
    session.append(
        EVIDENCE_RECORDED,
        _evidence_payload(sid, evidence_id="ev-1", criterion_id=criterion_id,
                          artifact_ref=ref),
    )
    return session, sid, ref, criterion_id


def _freshness_reasons(state: dict, criterion_id: str) -> list[str]:
    return [
        reason
        for item in state["by_criterion"][criterion_id]
        for reason in item["freshness"]["reasons"]
    ]


# ── a) 可达集扫描 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reachable_scan_covers_all_reference_sources(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref_evidence, _ = await _add_artifact(settings, sid, "evidence-content")
    ref_tool, _ = await _add_artifact(settings, sid, "tool-content")
    ref_created, _ = await _add_artifact(settings, sid, "created-content")
    ref_orphan, _ = await _add_artifact(settings, sid, "orphan-content")

    session.append(
        EVIDENCE_RECORDED,
        _evidence_payload(sid, evidence_id="ev-1", criterion_id="ac-1",
                          artifact_ref=ref_evidence),
    )
    session.append(
        TOOL_RESULT,
        {"tool_call_id": "tc-9", "content": json.dumps({"artifact_ref": ref_tool})},
    )
    session.append(ARTIFACT_CREATED, {"artifact_id": ref_created, "tool_call_id": "tc-9"})

    service = _make_service(make_session_service, settings, store)
    reachable = await service._reachable_for_session(
        sid, await service._children_by_parent()
    )

    assert ref_evidence in reachable
    assert ref_tool in reachable
    assert ref_created in reachable
    assert ref_orphan not in reachable


# ── b) 多引用者：同一 ref 被两条证据引用，可达集只出现一次 ──────────────────


@pytest.mark.asyncio
async def test_multiple_referencers_single_entry(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref, size = await _add_artifact(settings, sid, "shared-content")

    session.append(
        EVIDENCE_RECORDED,
        _evidence_payload(sid, evidence_id="ev-1", criterion_id="ac-1", artifact_ref=ref),
    )
    session.append(
        EVIDENCE_RECORDED,
        _evidence_payload(sid, evidence_id="ev-2", criterion_id="ac-1", artifact_ref=ref),
    )

    service = _make_service(make_session_service, settings, store)
    reachable = await service._reachable_for_session(
        sid, await service._children_by_parent()
    )
    assert list(reachable) == [ref]
    assert len(reachable[ref]["referenced_by"]) == 2

    preview = await service.preview_artifact_cleanup(sid)
    affected = {item["artifact_ref"]: item for item in preview["affected"]}
    assert affected[ref]["size"] == size
    assert len(affected[ref]["referenced_by"]) == 2


# ── c) fork 子会话引用父 artifact → blocked fork_child_reference ───────────


@pytest.mark.asyncio
async def test_fork_child_reference_blocks(make_session_service, env):
    settings, store = env
    parent = Session.start(store)
    parent_id = parent.session_id
    ref, _ = await _add_artifact(settings, parent_id, "parent-content")

    child_id = "child-1"
    child = Session.start(store, session_id=child_id)
    child.append(
        EVIDENCE_RECORDED,
        _evidence_payload(child_id, evidence_id="ev-c", criterion_id="ac-c",
                          artifact_ref=ref),
    )
    metas = [
        SessionMeta(
            session_id=child_id, created_at="2026-10-06T00:00:00Z",
            parent_session_id=parent_id, origin="fork",
        )
    ]

    service = _make_service(make_session_service, settings, store, metas=metas)
    preview = await service.preview_artifact_cleanup(parent_id)
    blocked = {item["artifact_ref"]: item["reason"] for item in preview["blocked"]}
    assert blocked[ref] == "fork_child_reference"
    assert all(item["artifact_ref"] != ref for item in preview["affected"])


# ── d) preview 形状 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_shape(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref_evidence, size_evidence = await _add_artifact(settings, sid, "ev-content")
    ref_tool, _ = await _add_artifact(settings, sid, "tool-content")
    ref_orphan, size_orphan = await _add_artifact(settings, sid, "orphan-content")

    session.append(
        EVIDENCE_RECORDED,
        _evidence_payload(sid, evidence_id="ev-42", criterion_id="ac-1",
                          artifact_ref=ref_evidence),
    )
    session.append(
        TOOL_RESULT,
        {"tool_call_id": "tc-9", "content": json.dumps({"artifact_ref": ref_tool})},
    )

    service = _make_service(make_session_service, settings, store)
    preview = await service.preview_artifact_cleanup(sid)

    assert isinstance(preview["snapshot_token"], str) and preview["snapshot_token"]

    affected = {item["artifact_ref"]: item for item in preview["affected"]}
    assert set(affected) == {ref_evidence, ref_orphan}
    assert affected[ref_evidence]["size"] == size_evidence
    assert affected[ref_evidence]["referenced_by"]  # 证据引用者被列出
    assert affected[ref_orphan]["size"] == size_orphan
    assert affected[ref_orphan]["referenced_by"] == []

    assert preview["evidence_invalidated"] == ["ev-42"]
    assert preview["reclaimable_bytes"] == size_evidence + size_orphan

    blocked = {item["artifact_ref"]: item["reason"] for item in preview["blocked"]}
    assert blocked[ref_tool] == "referenced"


# ── d2) P0-1：真实事件顺序下，出生证明 artifact 可清理（不再恒 blocked）────────


@pytest.mark.asyncio
async def test_birth_certificate_artifact_is_reclaimable(make_session_service, env):
    """真实事件顺序 ``tool/call → artifact/externalized → tool/result`` + 证据引用。

    生产里每个本地 artifact 都由溢出外置产生（``tooling/overflow.py`` 的
    ``save(source_tool=..., tool_call_id=...)`` + deferred ``artifact/externalized``
    事件 + tool result 带 ``artifact_ref``）。旧语义把这些"出生证明"事件判成硬引用
    ⇒ 每个 artifact 永远 blocked、``affected`` 恒空。新语义：只剩出生证明（+ stale
    证据）的 artifact 可清理，``affected`` 非空、``reclaimable_bytes > 0``。
    """
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    # 走真实 LocalArtifactStore.save：tool_call_id 与后续事件一致
    artifact = await LocalArtifactStore(settings, session_id=sid).save(
        sid, "overflow-content", mime_type="text/plain",
        source_tool="bash", tool_call_id="tc-real",
    )
    ref = artifact.artifact_id

    # 真实事件顺序：tool/call → artifact/externalized → tool/result
    session.append(
        TOOL_CALL,
        {"tool_call_id": "tc-real", "tool_name": "bash", "arguments": {}},
    )
    session.append(
        ARTIFACT_EXTERNALIZED,
        {
            "artifact_id": ref, "session_id": sid, "source_tool": "bash",
            "tool_call_id": "tc-real", "size": artifact.size,
            "mime_type": "text/plain",
        },
    )
    session.append(
        TOOL_RESULT,
        {"tool_call_id": "tc-real", "content": json.dumps({"artifact_ref": ref})},
    )
    # 一条证据引用该 artifact（fixture manifest 对不上 → fail-closed stale → 不阻断）
    session.append(
        EVIDENCE_RECORDED,
        _evidence_payload(sid, evidence_id="ev-birth", criterion_id="ac-1",
                          artifact_ref=ref),
    )

    service = _make_service(make_session_service, settings, store)
    preview = await service.preview_artifact_cleanup(sid)

    affected = {item["artifact_ref"]: item for item in preview["affected"]}
    assert ref in affected, f"出生证明 artifact 应可清理，实际 blocked={preview['blocked']}"
    assert preview["reclaimable_bytes"] > 0
    assert affected[ref]["size"] == artifact.size
    # 出生证明事件的标签仍在 referenced_by 里（用户看得见"删掉会失去什么"）
    assert any("tc-real" in tag for tag in affected[ref]["referenced_by"])


# ── e) snapshot_token 稳定性 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_token_stable_and_changes(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref, _ = await _add_artifact(settings, sid, "content")
    orphan, _ = await _add_artifact(settings, sid, "orphan")

    service = _make_service(make_session_service, settings, store)
    first = await service.preview_artifact_cleanup(sid)
    second = await service.preview_artifact_cleanup(sid)
    assert first["snapshot_token"] == second["snapshot_token"]

    session.append(
        EVIDENCE_RECORDED,
        _evidence_payload(sid, evidence_id="ev-1", criterion_id="ac-1", artifact_ref=orphan),
    )
    third = await service.preview_artifact_cleanup(sid)
    assert third["snapshot_token"] != first["snapshot_token"]
    assert ref in {item["artifact_ref"] for item in third["affected"]}


# ── f) execute 成功 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_deletes_and_records(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref, _ = await _add_artifact(settings, sid, "to-delete")
    orphan, _ = await _add_artifact(settings, sid, "to-delete-2")

    service = _make_service(make_session_service, settings, store)
    preview = await service.preview_artifact_cleanup(sid)
    refs = [item["artifact_ref"] for item in preview["affected"]]
    assert set(refs) == {ref, orphan}

    result = await service.execute_artifact_cleanup(
        sid, preview["snapshot_token"], refs
    )
    assert set(result["deleted"]) == {ref, orphan}
    assert result["failed"] == []
    assert result["not_deleted"] == []

    assert not (Path(settings.artifact_dir) / sid / ref).exists()

    cleaned_path = store._root / sid / "cleaned_artifacts.json"
    cleaned = json.loads(cleaned_path.read_text(encoding="utf-8"))
    # #368 P3-1：cleaned 记录改 {ref: 清理代际（最大 seq）}
    assert set(cleaned) == {ref, orphan}
    assert all(isinstance(generation, int) for generation in cleaned.values())


# ── g) token 不符 → SnapshotTokenMismatch ────────────────────────────────


@pytest.mark.asyncio
async def test_execute_rejects_wrong_token(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref, _ = await _add_artifact(settings, sid, "content")

    service = _make_service(make_session_service, settings, store)
    with pytest.raises(SnapshotTokenMismatch):
        await service.execute_artifact_cleanup(sid, "0" * 64, [ref])


# ── h) execute 重验：新增引用后该 ref 进 not_deleted ──────────────────────


@pytest.mark.asyncio
async def test_execute_revalidates_referenced_ref(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref, _ = await _add_artifact(settings, sid, "content")

    service = _make_service(make_session_service, settings, store)
    await service.preview_artifact_cleanup(sid)  # 取得初始 token（未使用）

    # 新增一条（阻断性）tool/result 引用 → 该 ref 变为可达
    session.append(
        TOOL_RESULT,
        {"tool_call_id": "tc-9", "content": json.dumps({"artifact_ref": ref})},
    )
    refreshed = await service.preview_artifact_cleanup(sid)

    result = await service.execute_artifact_cleanup(
        sid, refreshed["snapshot_token"], [ref]
    )
    assert result["deleted"] == []
    assert result["not_deleted"] == [{"artifact_ref": ref, "reason": "referenced"}]
    assert (Path(settings.artifact_dir) / sid / ref).exists()


@pytest.mark.asyncio
async def test_execute_stale_token_after_new_reference(make_session_service, env):
    """CAS：预览后引用集变化 → 旧 token 执行必须 409 重算。"""
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref, _ = await _add_artifact(settings, sid, "content")

    service = _make_service(make_session_service, settings, store)
    preview = await service.preview_artifact_cleanup(sid)

    session.append(
        TOOL_RESULT,
        {"tool_call_id": "tc-9", "content": json.dumps({"artifact_ref": ref})},
    )
    with pytest.raises(SnapshotTokenMismatch):
        await service.execute_artifact_cleanup(sid, preview["snapshot_token"], [ref])


@pytest.mark.asyncio
async def test_child_new_reference_invalidates_token(make_session_service, env):
    """P1-1：fork 子会话新增引用 → 旧 token 执行必须 409。

    父 events.jsonl 不变、可达集**名集合**也不变（ref 已因父会话出生证明在可达集里），
    仅子会话引用变化——旧 token material 漏掉子会话事件与 ref reasons 时不会失效。
    """
    settings, store = env
    parent = Session.start(store)
    parent_id = parent.session_id
    ref, _ = await _add_artifact(settings, parent_id, "parent-content")
    # 父会话已有出生证明引用 → ref 已在可达集（名集合不因新增引用而变）
    parent.append(
        TOOL_RESULT,
        {"tool_call_id": "tc-1", "content": json.dumps({"artifact_ref": ref})},
    )

    child_id = "child-cas"
    child = Session.start(store, session_id=child_id)
    metas = [
        SessionMeta(
            session_id=child_id, created_at="2026-10-06T00:00:00Z",
            parent_session_id=parent_id, origin="fork",
        )
    ]
    service = _make_service(make_session_service, settings, store, metas=metas)
    preview = await service.preview_artifact_cleanup(parent_id)

    # 子会话新增一条引用该 ref 的 tool/result → 旧 token 必须失效
    child.append(
        TOOL_RESULT,
        {"tool_call_id": "tc-child", "content": json.dumps({"artifact_ref": ref})},
    )
    with pytest.raises(SnapshotTokenMismatch):
        await service.execute_artifact_cleanup(
            parent_id, preview["snapshot_token"], [ref]
        )


# ── e2) P3-3：token 绑定勾选 refs ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_rejects_changed_selection(make_session_service, env):
    """P3-3：preview 拿 token 后换勾选（传不同 ref 子集）→ 旧 token 执行 409。

    预览 token 绑定当时的 affected 全集；execute 用「请求 refs ∩ 当前可清理集」
    复算，勾选不一致即 SnapshotTokenMismatch。
    """
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    a, _ = await _add_artifact(settings, sid, "sel-a")
    b, _ = await _add_artifact(settings, sid, "sel-b")

    service = _make_service(make_session_service, settings, store)
    preview = await service.preview_artifact_cleanup(sid)
    assert {item["artifact_ref"] for item in preview["affected"]} == {a, b}

    with pytest.raises(SnapshotTokenMismatch):
        await service.execute_artifact_cleanup(sid, preview["snapshot_token"], [a])
    # 全选（与预览一致）仍可执行
    result = await service.execute_artifact_cleanup(
        sid, preview["snapshot_token"], [a, b]
    )
    assert set(result["deleted"]) == {a, b}


# ── e3) P3-6：preview 内 events 只读一遍 ─────────────────────────────────


@pytest.mark.asyncio
async def test_preview_reads_events_once(make_session_service, env):
    """P3-6：preview 只读一次 events.jsonl（_cleanup_state 复用给 evidence_invalidated）。"""
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    await _add_artifact(settings, sid, "once-content")

    calls = {"n": 0}
    real_read_events = store.read_events

    def _counting(session_id):
        calls["n"] += 1
        return real_read_events(session_id)

    store.read_events = _counting  # 实例属性遮蔽绑定方法，仅本用例计数
    service = _make_service(make_session_service, settings, store)
    await service.preview_artifact_cleanup(sid)
    assert calls["n"] == 1, f"preview 应只读一次 events，实际 {calls['n']} 次"


# ── e4) P3-7：execute → cleaned → 证据投影端到端 ─────────────────────────


@pytest.mark.asyncio
async def test_cleaned_artifact_projection_end_to_end(make_session_service, env):
    """P3-7：preview→execute 清理带证据引用的 artifact→cleaned 落盘→投影判"原件已清理"。"""
    settings, store = env
    _session, sid, ref, criterion_id = await _session_with_evidence_artifact(
        settings, store
    )

    service = _make_service(make_session_service, settings, store)
    preview = await service.preview_artifact_cleanup(sid)
    assert ref in {item["artifact_ref"] for item in preview["affected"]}

    result = await service.execute_artifact_cleanup(
        sid, preview["snapshot_token"], [ref]
    )
    assert result["deleted"] == [ref]

    cleaned = json.loads(
        (store._root / sid / "cleaned_artifacts.json").read_text(encoding="utf-8")
    )
    assert ref in cleaned

    state = await service.evidence_state(sid)
    reasons = _freshness_reasons(state, criterion_id)
    assert any(f"artifact 原件已清理：{ref}" in reason for reason in reasons)
    assert not any("artifact 不可读回" in reason for reason in reasons)


# ── e5) P3-1：清理后同 id 重建 → 不判"原件已清理" ────────────────────────


@pytest.mark.asyncio
async def test_recreated_artifact_not_projected_as_cleaned(make_session_service, env):
    """P3-1：清理 → 重建同内容（内容寻址同 id）→ 外部删除 → 投影判"不可读回"而非"已清理"。"""
    settings, store = env
    session, sid, ref, criterion_id = await _session_with_evidence_artifact(
        settings, store
    )

    service = _make_service(make_session_service, settings, store)
    preview = await service.preview_artifact_cleanup(sid)
    result = await service.execute_artifact_cleanup(
        sid, preview["snapshot_token"], [ref]
    )
    assert result["deleted"] == [ref]

    # 重建同内容：LocalArtifactStore 内容寻址 → 同 id；append 一条 seq 更大的 creation 事件
    rebuilt = await LocalArtifactStore(settings, session_id=sid).save(
        sid, "ev-content", mime_type="text/plain", source_tool="bash", tool_call_id="tc-1"
    )
    assert rebuilt.artifact_id == ref
    session.append(
        ARTIFACT_EXTERNALIZED,
        {
            "artifact_id": ref, "session_id": sid, "source_tool": "bash",
            "tool_call_id": "tc-1", "size": rebuilt.size, "mime_type": "text/plain",
        },
    )
    # 外部删除（非清理流程）：无事件，投影只能靠"清理代际 vs creation seq"区分
    (Path(settings.artifact_dir) / sid / ref).unlink()

    state = await service.evidence_state(sid)
    reasons = _freshness_reasons(state, criterion_id)
    assert any(f"artifact 不可读回：{ref}" in reason for reason in reasons)
    assert not any("原件已清理" in reason for reason in reasons)


# ── e6) P3-8：delegation 子会话引用不阻断（判定结论 b）────────────────────


@pytest.mark.asyncio
async def test_delegation_child_reference_does_not_block(make_session_service, env):
    """P3-8 判定结论 (b)：委派子会话引用父 artifact 不阻断父清理。

    委派 = spawn（fresh child，不继承父事件）；child 的 tool_scope 不含读回工具、
    工具溢出在根命名空间被拒（fail-open）——不构成父原件的活引用。
    """
    settings, store = env
    parent = Session.start(store)
    parent_id = parent.session_id
    ref, _ = await _add_artifact(settings, parent_id, "parent-content")

    child_id = "child-deleg"
    child = Session.start(store, session_id=child_id)
    child.append(
        TOOL_RESULT,
        {"tool_call_id": "tc-c", "content": json.dumps({"artifact_ref": ref})},
    )
    metas = [
        SessionMeta(
            session_id=child_id, created_at="2026-10-06T00:00:00Z",
            parent_session_id=parent_id, origin="delegation",
        )
    ]
    service = _make_service(make_session_service, settings, store, metas=metas)
    preview = await service.preview_artifact_cleanup(parent_id)

    assert ref in {item["artifact_ref"] for item in preview["affected"]}
    assert all(item["artifact_ref"] != ref for item in preview["blocked"])


# ── i) blocked-在途 run ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_busy_run_blocks_preview_and_execute(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref, _ = await _add_artifact(settings, sid, "content")
    orphan, _ = await _add_artifact(settings, sid, "orphan")

    service = _make_service(make_session_service, settings, store, busy=True)
    preview = await service.preview_artifact_cleanup(sid)
    assert preview["affected"] == []
    reasons = {item["reason"] for item in preview["blocked"]}
    assert reasons == {"active_task"}
    assert {item["artifact_ref"] for item in preview["blocked"]} == {ref, orphan}

    with pytest.raises(ActiveRunConflict):
        await service.execute_artifact_cleanup(sid, preview["snapshot_token"], [ref])


# ── j) blocked-未决 operation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreconciled_operation_blocks(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref, _ = await _add_artifact(settings, sid, "content")

    ledger = idle_operation_ledger()
    ledger.list_for_session = AsyncMock(return_value=[
        Operation(
            tool_call_id="tc-unresolved", session_id=sid, tool_name="run_cmd",
            args_identity="a", state=OperationState.RUNNING, artifact_ref=ref,
        )
    ])

    service = _make_service(make_session_service, settings, store, ledger=ledger)
    preview = await service.preview_artifact_cleanup(sid)
    blocked = {item["artifact_ref"]: item["reason"] for item in preview["blocked"]}
    assert blocked[ref] == "unreconciled_operation"


# ── k) _check_evidence_artifact 三元组 ───────────────────────────────────


class _KeyErrorStore:
    async def load(self, artifact_id):
        raise KeyError(artifact_id)


@pytest.mark.asyncio
async def test_check_evidence_artifact_reports_cleaned(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref = "a" * 16

    cleaned_dir = store._root / sid
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    (cleaned_dir / "cleaned_artifacts.json").write_text(
        json.dumps([ref]), encoding="utf-8"
    )

    service = _make_service(make_session_service, settings, store)
    record = SimpleNamespace(artifact_ref=ref, tool_call_id=None, task_session_id=sid)
    assert await service._check_evidence_artifact(_KeyErrorStore(), record, sid) == (
        False, None, True,
    )

    other = SimpleNamespace(
        artifact_ref="b" * 16, tool_call_id=None, task_session_id=sid
    )
    assert await service._check_evidence_artifact(_KeyErrorStore(), other, sid) == (
        False, None, False,
    )


@pytest.mark.asyncio
async def test_check_evidence_artifact_fallback_uses_request_session(
    make_session_service, env
):
    """P3-2：cleaned 兜底读**请求会话**集合，不再依赖 ``record.task_session_id``。

    请求会话（A）有 cleaned 记录；记录声明的 task_session_id（B）没有——兜底必须
    走 A（True）；把 session_id 换成 B 则 False，证明驱动量是 session_id 而非记录字段。
    """
    settings, store = env
    session_a = Session.start(store)
    sid_a = session_a.session_id
    ref = "a" * 16
    cleaned_dir = store._root / sid_a
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    (cleaned_dir / "cleaned_artifacts.json").write_text(
        json.dumps({ref: 0}), encoding="utf-8"
    )
    session_b = Session.start(store)
    sid_b = session_b.session_id
    assert sid_b != sid_a

    service = _make_service(make_session_service, settings, store)
    record = SimpleNamespace(artifact_ref=ref, tool_call_id=None, task_session_id=sid_b)
    # 兜底走请求会话 A → 已清理
    assert await service._check_evidence_artifact(_KeyErrorStore(), record, sid_a) == (
        False, None, True,
    )
    # 请求会话换成 B（无 cleaned 记录）→ 未清理（证明不看 record.task_session_id）
    assert await service._check_evidence_artifact(_KeyErrorStore(), record, sid_b) == (
        False, None, False,
    )


# ── get_session_usage ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_session_usage(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    ref, size = await _add_artifact(settings, sid, "referenced")
    _orphan, orphan_size = await _add_artifact(settings, sid, "orphan")
    session.append(
        EVIDENCE_RECORDED,
        _evidence_payload(sid, evidence_id="ev-1", criterion_id="ac-1", artifact_ref=ref),
    )

    service = _make_service(make_session_service, settings, store)
    usage = await service.get_session_usage(sid)

    assert usage["artifact_count"] == 2
    assert usage["events_bytes"] > 0
    assert usage["artifacts_bytes"] >= size + orphan_size
    assert usage["progress_bytes"] == 0
    # 证据引用不算阻断 → 两件都可在确认后清理
    assert usage["reclaimable_bytes"] == size + orphan_size
    assert usage["computed_at"]


@pytest.mark.asyncio
async def test_get_session_usage_busy_reclaimable_zero(make_session_service, env):
    """P2：在途 run 时 usage 的 reclaimable_bytes 与 preview 口径一致（记 0）。"""
    settings, store = env
    session = Session.start(store)
    sid = session.session_id
    await _add_artifact(settings, sid, "orphan")

    service = _make_service(make_session_service, settings, store, busy=True)
    usage = await service.get_session_usage(sid)
    assert usage["reclaimable_bytes"] == 0


# ── preview mode 校验 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_rejects_unknown_mode(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    service = _make_service(make_session_service, settings, store)
    with pytest.raises(ValueError):
        await service.preview_artifact_cleanup(session.session_id, mode="all")
