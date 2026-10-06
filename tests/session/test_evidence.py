"""#352 [W-08]：验收项 ↔ 真实证据的服务端投影。

写侧：`evidence/recorded` append-only typed 事件（payload = 票面 14 字段 DTO）；
读侧：`derive_evidence_state(events)` 纯投影（按 criterion_id 聚合，幂等、可 replay）；
陈旧层：`compute_evidence_manifest` + `evaluate_evidence_freshness`（读取时求值，
产出明确过期原因；绝不沿用旧"通过"）。

`VerificationEntry.evidence`（`str|None` 人类摘要/指针）维持不变；`#524`
completion_evidence（Runtime 完成门）与本票（服务端投影）严格区分。
"""

from __future__ import annotations

import hashlib

import pytest

from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.evidence import (
    apply_evidence_recorded,
    compute_evidence_manifest,
    derive_evidence_state,
    evaluate_evidence_freshness,
)
from agent_harness.session.task import (
    apply_acceptance,
    apply_task_definition,
    derive_task_state,
)


def test_evidence_recorded_is_durable_event_type() -> None:
    """新事件 `evidence/recorded` 必须进 durable 词汇表（append-only，#3）。"""
    from agent_harness.session import event as event_mod

    assert event_mod.EVIDENCE_RECORDED == "evidence/recorded"
    assert event_mod.EVIDENCE_RECORDED in event_mod.EVENT_TYPES
    assert event_mod.EVIDENCE_RECORDED not in event_mod.STREAM_ONLY_TYPES


def _session(tmp_path) -> Session:
    return Session.start(JsonlSessionStore(root=tmp_path), cwd=str(tmp_path))


def _two_criteria(session) -> list[str]:
    outcome = apply_task_definition(
        session,
        task_text="修登录",
        criteria=[{"text": "登录成功跳转"}, {"text": "登出清会话"}],
    )
    assert outcome.ok, outcome.reason
    return [c.item_id for c in derive_task_state(session.events).criteria]


def _dto(session_id: str, criterion_id: str, **overrides) -> dict:
    """票面 14 字段 DTO 的合法基线（逐项覆写做反例）。"""
    dto = {
        "evidence_id": "ev-001",
        "task_session_id": session_id,
        "run_id": "run-1",
        "criterion_id": criterion_id,
        "kind": "test",
        "source_event_seq": 7,
        "tool_call_id": None,
        "captured_at": "2026-10-06T15:00:00.000+00:00",
        "result": "pass",
        "command_or_action": "pytest -q tests/auth",
        "exit_code_or_observation": 0,
        "artifact_ref": None,
        "base_head": "a" * 40,
        "workspace_manifest": {
            "files": [{"path": "src/auth.py", "sha256": "b" * 64}],
            "manifest_hash": "c" * 64,
            "progress_md_sha256": "d" * 64,
        },
    }
    dto.update(overrides)
    return dto


def _record(session, **overrides):
    cids = _two_criteria(session)
    dto = _dto(session.session_id, cids[0], **overrides)
    outcome = apply_evidence_recorded(session, dto)
    assert outcome.ok, outcome.reason
    return derive_evidence_state(session.events).by_criterion[cids[0]][0], cids


# ── 写侧：合法记录 ─────────────────────────────────────────────────────────


def test_record_evidence_appends_typed_event(tmp_path) -> None:
    """票面验收：成功 pytest → kind=test/result=pass，14 字段原样落盘。"""
    from agent_harness.session import event as event_mod

    session = _session(tmp_path)
    cids = _two_criteria(session)
    dto = _dto(session.session_id, cids[0])
    outcome = apply_evidence_recorded(session, dto)
    assert outcome.ok, outcome.reason
    event = session.events[-1]
    assert event.type == event_mod.EVIDENCE_RECORDED
    assert event.run_id == "run-1"
    payload = dict(event.data)
    for key in (
        "evidence_id", "task_session_id", "run_id", "criterion_id", "kind",
        "source_event_seq", "tool_call_id", "captured_at", "result",
        "command_or_action", "exit_code_or_observation", "artifact_ref",
        "base_head", "workspace_manifest",
    ):
        assert payload[key] == dto[key], f"字段 {key} 必须原样持久化"


def test_record_failed_pytest_and_ui_blocked(tmp_path) -> None:
    """票面验收：失败 pytest（exit_code=1）/ 浏览器缺插件（ui + blocked，不是 pass）。"""
    session = _session(tmp_path)
    cids = _two_criteria(session)
    # 失败的 pytest
    fail = apply_evidence_recorded(
        session,
        _dto(session.session_id, cids[0], evidence_id="ev-fail",
             result="fail", exit_code_or_observation=1,
             command_or_action="pytest -q tests/auth"),
    )
    assert fail.ok, fail.reason
    # 浏览器缺插件：无 Chrome/MCP 时是 blocked/未完成，绝不落 pass
    blocked = apply_evidence_recorded(
        session,
        _dto(session.session_id, cids[1], evidence_id="ev-ui",
             kind="ui", result="blocked",
             command_or_action="browser: 打开登录页截图",
             exit_code_or_observation="缺 Chrome MCP：未执行"),
    )
    assert blocked.ok, blocked.reason
    state = derive_evidence_state(session.events)
    assert state.by_criterion[cids[0]][0].result == "fail"
    assert state.by_criterion[cids[1]][0].kind == "ui"
    assert state.by_criterion[cids[1]][0].result == "blocked"


def test_rerun_appends_without_overwriting(tmp_path) -> None:
    """票面工作指令 1：重跑追加新证据，不覆盖旧记录（1 criterion ↔ N 证据）。"""
    session = _session(tmp_path)
    cids = _two_criteria(session)
    assert apply_evidence_recorded(
        session, _dto(session.session_id, cids[0], evidence_id="ev-1", result="fail",
                      exit_code_or_observation=1)
    ).ok
    assert apply_evidence_recorded(
        session, _dto(session.session_id, cids[0], evidence_id="ev-2", result="pass",
                      exit_code_or_observation=0)
    ).ok
    records = derive_evidence_state(session.events).by_criterion[cids[0]]
    assert [r.evidence_id for r in records] == ["ev-1", "ev-2"]
    assert [r.result for r in records] == ["fail", "pass"]


def test_duplicate_evidence_id_is_conflict(tmp_path) -> None:
    """同一 evidence_id 重复记录 → conflict（409），零新事件。"""
    session = _session(tmp_path)
    cids = _two_criteria(session)
    assert apply_evidence_recorded(session, _dto(session.session_id, cids[0])).ok
    before = len(session.events)
    outcome = apply_evidence_recorded(session, _dto(session.session_id, cids[0]))
    assert not outcome.ok
    assert outcome.error_kind == "conflict"
    assert len(session.events) == before


def test_sensitive_values_not_in_plaintext_by_recorder(tmp_path) -> None:
    """票面验收：敏感输出值不进正文——记录侧先脱敏，存储层原样往返脱敏后的值。"""
    session = _session(tmp_path)
    cids = _two_criteria(session)
    dto = _dto(
        session.session_id, cids[0],
        command_or_action="curl -H 'Authorization: <已脱敏>' https://api.example.com/me",
        exit_code_or_observation="<响应体已脱敏>",
    )
    assert apply_evidence_recorded(session, dto).ok
    record = derive_evidence_state(session.events).by_criterion[cids[0]][0]
    assert record.command_or_action == dto["command_or_action"]
    assert record.exit_code_or_observation == dto["exit_code_or_observation"]


# ── 写侧：形状校验（坏形状 → 拒绝、零事件；plan.py/task.py 同判据）───────────


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("evidence_id"),
        lambda d: d.update(evidence_id=""),
        lambda d: d.update(kind="video"),
        lambda d: d.update(result="unknown"),
        lambda d: d.update(source_event_seq="7"),
        lambda d: d.update(source_event_seq=-1),
        lambda d: d.update(captured_at=""),
        lambda d: d.update(command_or_action="x" * 2001),
        lambda d: d.update(exit_code_or_observation=""),
        lambda d: d.update(artifact_ref=""),
        lambda d: d.update(base_head=""),
        lambda d: d.update(workspace_manifest=None),
        lambda d: d.update(workspace_manifest={"files": "nope"}),
        lambda d: d.update(workspace_manifest={
            "files": [{"path": "/abs/path.py", "sha256": "b" * 64}],
            "manifest_hash": "c" * 64, "progress_md_sha256": None}),
        lambda d: d.update(workspace_manifest={
            "files": [{"path": "../escape.py", "sha256": "b" * 64}],
            "manifest_hash": "c" * 64, "progress_md_sha256": None}),
        lambda d: d.update(workspace_manifest={
            "files": [{"path": "progress.md", "sha256": "b" * 64}],
            "manifest_hash": "c" * 64, "progress_md_sha256": None}),
        lambda d: d.update(workspace_manifest={
            "files": [{"path": "a.py", "sha256": "xyz"}],
            "manifest_hash": "c" * 64, "progress_md_sha256": None}),
        lambda d: d.update(workspace_manifest={
            "files": [], "manifest_hash": "not-hex",
            "progress_md_sha256": None}),
        lambda d: d.update(extra_key="未来字段"),
    ],
    ids=[
        "缺 evidence_id", "空 evidence_id", "kind 非法", "result 非法",
        "source_event_seq 非 int", "source_event_seq 负数", "captured_at 空",
        "command 超长", "observation 空串", "artifact_ref 空串", "base_head 空串",
        "manifest 缺失", "manifest.files 非数组", "manifest 路径绝对",
        "manifest 路径穿越", "progress.md 进覆盖集", "sha256 非法",
        "manifest_hash 非法", "未知顶层键",
    ],
)
def test_record_rejects_bad_shape_with_zero_events(tmp_path, mutate) -> None:
    """坏形状 → 拒绝、零事件（不产生半条证据）。"""
    session = _session(tmp_path)
    cids = _two_criteria(session)
    dto = _dto(session.session_id, cids[0])
    mutate(dto)
    before = len(session.events)
    outcome = apply_evidence_recorded(session, dto)
    assert not outcome.ok, f"{dto} 应该被拒绝"
    assert outcome.error_kind == "shape"
    assert len(session.events) == before, "拒绝 = 零事件"


def test_record_rejects_non_dict(tmp_path) -> None:
    session = _session(tmp_path)
    _two_criteria(session)
    before = len(session.events)
    outcome = apply_evidence_recorded(session, ["not", "a", "dict"])
    assert not outcome.ok and outcome.error_kind == "shape"
    assert len(session.events) == before


def test_record_rejects_session_mismatch_and_unknown_criterion(tmp_path) -> None:
    """task_session_id 与会话对不上 / criterion 不在当前清单 → shape 拒绝。"""
    session = _session(tmp_path)
    cids = _two_criteria(session)
    before = len(session.events)
    assert not apply_evidence_recorded(
        session, _dto("other-session", cids[0])
    ).ok, "跨会话证据必须拒绝"
    assert not apply_evidence_recorded(
        session, _dto(session.session_id, "ac-not-exist")
    ).ok, "未知验收项必须拒绝"
    assert len(session.events) == before


def test_record_rejects_without_task_definition(tmp_path) -> None:
    """无任务定义时记录证据 → shape 拒绝（criterion 无处对齐）。"""
    session = _session(tmp_path)
    before = len(session.events)
    outcome = apply_evidence_recorded(session, _dto(session.session_id, "ac-x"))
    assert not outcome.ok and outcome.error_kind == "shape"
    assert len(session.events) == before


# ── 读侧：derive_evidence_state 纯投影 ───────────────────────────────────────


def test_derive_groups_by_criterion_and_is_idempotent(tmp_path) -> None:
    """按 criterion_id 聚合；幂等（两次 derive 结果一致）；可 replay。"""
    session = _session(tmp_path)
    cids = _two_criteria(session)
    assert apply_evidence_recorded(
        session, _dto(session.session_id, cids[0], evidence_id="ev-a")).ok
    assert apply_evidence_recorded(
        session, _dto(session.session_id, cids[1], evidence_id="ev-b",
                      kind="diff", result="fail")).ok
    events = list(session.events)
    first = derive_evidence_state(events)
    second = derive_evidence_state(events)
    assert first.to_payload() == second.to_payload(), "幂等：重放 N 次结果一致"
    assert set(first.by_criterion) == set(cids)
    assert first.by_criterion[cids[0]][0].evidence_id == "ev-a"
    assert first.by_criterion[cids[1]][0].kind == "diff"


def test_derive_skips_malformed_payloads(tmp_path) -> None:
    """手写/污染的畸形 evidence/recorded 事件 → 投影跳过，不崩。"""
    from agent_harness.session import event as event_mod

    session = _session(tmp_path)
    cids = _two_criteria(session)
    assert apply_evidence_recorded(
        session, _dto(session.session_id, cids[0], evidence_id="ev-good")).ok
    session.append(event_mod.EVIDENCE_RECORDED, {"bogus": True})
    session.append(event_mod.EVIDENCE_RECORDED, {"evidence_id": "ev-bad-shape"})
    state = derive_evidence_state(session.events)
    assert list(state.by_criterion) == [cids[0]]
    assert state.by_criterion[cids[0]][0].evidence_id == "ev-good"


def test_derive_unaffected_by_acceptance_axis(tmp_path) -> None:
    """票面验收：用户带缺项接受（接受轴）不改变证据投影。"""
    session = _session(tmp_path)
    cids = _two_criteria(session)
    assert apply_evidence_recorded(
        session, _dto(session.session_id, cids[0], evidence_id="ev-1")).ok
    before = derive_evidence_state(session.events).to_payload()
    state = derive_task_state(session.events)
    assert apply_acceptance(
        session, "accepted_with_gaps", reason="登录项缺浏览器证据，先接受",
        expected_version=state.version,
    ).ok
    after = derive_evidence_state(session.events).to_payload()
    assert before == after, "接受轴裁决不得改写证据投影"


# ── manifest：显式文件清单 + 逐文件 sha256 ──────────────────────────────────


def _write(tmp_path, rel: str, content: bytes) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_manifest_computes_per_file_sha256(tmp_path) -> None:
    _write(tmp_path, "src/auth.py", b"print('hi')\n")
    _write(tmp_path, "data.json", b'{"a": 1}')
    manifest = compute_evidence_manifest(tmp_path, ["src/auth.py", "data.json"])
    payload = manifest.to_payload()
    assert payload["files"] == [
        {"path": "src/auth.py",
         "sha256": hashlib.sha256(b"print('hi')\n").hexdigest()},
        {"path": "data.json",
         "sha256": hashlib.sha256(b'{"a": 1}').hexdigest()},
    ]
    # manifest_hash 确定：与顺序无关、与内容绑定
    again = compute_evidence_manifest(tmp_path, ["data.json", "src/auth.py"])
    assert again.manifest_hash == manifest.manifest_hash
    assert len(manifest.manifest_hash) == 64


def test_manifest_tracks_progress_md_separately(tmp_path) -> None:
    """审计红线：progress.md 不进覆盖集，其 hash 独立单列。"""
    _write(tmp_path, "src/a.py", b"x = 1\n")
    _write(tmp_path, "progress.md", "# 进度\n".encode())
    manifest = compute_evidence_manifest(tmp_path, ["src/a.py"])
    assert manifest.progress_md_sha256 == hashlib.sha256("# 进度\n".encode()).hexdigest()
    assert all(f["path"] != "progress.md" for f in manifest.to_payload()["files"])
    # progress.md 缺席时如实记 None，不伪造
    (tmp_path / "progress.md").unlink()
    assert compute_evidence_manifest(tmp_path, ["src/a.py"]).progress_md_sha256 is None


@pytest.mark.parametrize("bad", ["progress.md", "sub/progress.md"])
def test_manifest_rejects_progress_md_in_covered_set(tmp_path, bad) -> None:
    """覆盖集里出现 progress.md → 拒绝（否则更新进度即自我过期）。"""
    _write(tmp_path, bad, "# 进度\n".encode())
    with pytest.raises(ValueError):
        compute_evidence_manifest(tmp_path, [bad])


@pytest.mark.parametrize("bad", ["/abs/path.py", "../escape.py", "a/../../b.py"])
def test_manifest_rejects_unsafe_paths(tmp_path, bad) -> None:
    with pytest.raises(ValueError):
        compute_evidence_manifest(tmp_path, [bad])


def test_manifest_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        compute_evidence_manifest(tmp_path, ["gone.py"])


# ── 陈旧层：读取时求值 ─────────────────────────────────────────────────────


def _recorded_with_real_manifest(tmp_path, **overrides):
    """在真实 tmp 工作区上记录一条证据，返回 (record, criterion_ids)。"""
    session = _session(tmp_path)
    cids = _two_criteria(session)
    _write(tmp_path, "src/auth.py", b"v1\n")
    _write(tmp_path, "progress.md", b"# p1\n")
    manifest = compute_evidence_manifest(tmp_path, ["src/auth.py"])
    dto = _dto(
        session.session_id, cids[0],
        workspace_manifest=manifest.to_payload(),
        **overrides,
    )
    outcome = apply_evidence_recorded(session, dto)
    assert outcome.ok, outcome.reason
    record = derive_evidence_state(session.events).by_criterion[cids[0]][0]
    return record, manifest, session


def test_fresh_when_nothing_changed(tmp_path) -> None:
    record, manifest, _ = _recorded_with_real_manifest(tmp_path)
    freshness = evaluate_evidence_freshness(
        record, current_manifest=manifest, current_head=record.base_head,
    )
    assert freshness.status == "fresh"
    assert freshness.reasons == ()


def test_stale_when_covered_file_changed(tmp_path) -> None:
    """票面验收：测试后改一行源码 → stale，明确列出变动文件。"""
    record, _, _ = _recorded_with_real_manifest(tmp_path)
    _write(tmp_path, "src/auth.py", "v2 // 改了一行\n".encode())
    current = compute_evidence_manifest(tmp_path, ["src/auth.py"])
    freshness = evaluate_evidence_freshness(
        record, current_manifest=current, current_head=record.base_head,
    )
    assert freshness.status == "stale"
    assert any("src/auth.py" in reason for reason in freshness.reasons)
    # 陈旧只改状态，不改写当初记录的 result（绝不沿用旧"通过"，也不篡改旧事实）
    assert record.result == "pass"


def test_stale_when_covered_file_removed(tmp_path) -> None:
    record, _, _ = _recorded_with_real_manifest(tmp_path)
    (tmp_path / "src" / "auth.py").unlink()
    current = compute_evidence_manifest(tmp_path, [])
    freshness = evaluate_evidence_freshness(
        record, current_manifest=current, current_head=record.base_head,
    )
    assert freshness.status == "stale"
    assert any("src/auth.py" in reason for reason in freshness.reasons)


def test_stale_when_base_head_changed(tmp_path) -> None:
    record, manifest, _ = _recorded_with_real_manifest(tmp_path)
    freshness = evaluate_evidence_freshness(
        record, current_manifest=manifest, current_head="f" * 40,
    )
    assert freshness.status == "stale"
    assert any("base_head" in reason for reason in freshness.reasons)


def test_progress_md_update_does_not_expire_evidence(tmp_path) -> None:
    """票面验收：进度文件元数据更新 → 证据不自我过期（hash 独立单列）。"""
    record, _, _ = _recorded_with_real_manifest(tmp_path)
    _write(tmp_path, "progress.md", "# p2：更新了进度\n".encode())
    current = compute_evidence_manifest(tmp_path, ["src/auth.py"])
    assert current.progress_md_sha256 != record.workspace_manifest.progress_md_sha256
    freshness = evaluate_evidence_freshness(
        record, current_manifest=current, current_head=record.base_head,
    )
    assert freshness.status == "fresh"


def test_unreadable_workspace_is_stale_fail_closed(tmp_path) -> None:
    """工作区不可读 → fail-closed 判 stale，不谎称 fresh。"""
    record, _, _ = _recorded_with_real_manifest(tmp_path)
    freshness = evaluate_evidence_freshness(
        record, current_manifest=None, current_head=record.base_head,
    )
    assert freshness.status == "stale"


def test_missing_artifact_is_stale(tmp_path) -> None:
    """票面验收：Artifact 丢失 → stale，明确指出不可读回。"""
    record, _, _ = _recorded_with_real_manifest(
        tmp_path, evidence_id="ev-art", artifact_ref="ab" * 8,
        tool_call_id="tc-1",
    )
    manifest = compute_evidence_manifest(tmp_path, ["src/auth.py"])
    freshness = evaluate_evidence_freshness(
        record, current_manifest=manifest, current_head=record.base_head,
        artifact_readable=False, artifact_attribution_ok=True,
    )
    assert freshness.status == "stale"
    assert any("ab" * 8 in reason for reason in freshness.reasons)


def test_artifact_attribution_mismatch_is_stale(tmp_path) -> None:
    """artifact 归属/来源校验：tool_call_id 对不上 → stale。"""
    record, _, _ = _recorded_with_real_manifest(
        tmp_path, evidence_id="ev-art2", artifact_ref="cd" * 8,
        tool_call_id="tc-1",
    )
    manifest = compute_evidence_manifest(tmp_path, ["src/auth.py"])
    freshness = evaluate_evidence_freshness(
        record, current_manifest=manifest, current_head=record.base_head,
        artifact_readable=True, artifact_attribution_ok=False,
    )
    assert freshness.status == "stale"
    assert any("归属" in reason for reason in freshness.reasons)


def test_artifact_ok_stays_fresh(tmp_path) -> None:
    record, _, _ = _recorded_with_real_manifest(
        tmp_path, evidence_id="ev-art3", artifact_ref="ef" * 8,
        tool_call_id="tc-1",
    )
    manifest = compute_evidence_manifest(tmp_path, ["src/auth.py"])
    freshness = evaluate_evidence_freshness(
        record, current_manifest=manifest, current_head=record.base_head,
        artifact_readable=True, artifact_attribution_ok=True,
    )
    assert freshness.status == "fresh"
