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
    EVIDENCE_RECORDED,
    TOOL_RESULT,
)
from agent_harness.session.service import (
    ActiveRunConflict,
    SnapshotTokenMismatch,
)
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
    reachable = await service._compute_reachable_artifacts(sid)

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
    reachable = await service._compute_reachable_artifacts(sid)
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
    assert json.loads(cleaned_path.read_text(encoding="utf-8")) == sorted([ref, orphan])


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
    assert await service._check_evidence_artifact(_KeyErrorStore(), record) == (
        False, None, True,
    )

    other = SimpleNamespace(
        artifact_ref="b" * 16, tool_call_id=None, task_session_id=sid
    )
    assert await service._check_evidence_artifact(_KeyErrorStore(), other) == (
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


# ── preview mode 校验 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_rejects_unknown_mode(make_session_service, env):
    settings, store = env
    session = Session.start(store)
    service = _make_service(make_session_service, settings, store)
    with pytest.raises(ValueError):
        await service.preview_artifact_cleanup(session.session_id, mode="all")
