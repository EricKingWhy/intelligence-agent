"""#1359 阶段 1：`goal/changed` 目标状态机 + 投影。

票面 AC3（revision CAS）、AC6（五段式可测判据）、AC11（默认 32 轮）与
`clear` 墓碑、坏 payload 容错都在这里钉住。Web 面见 `tests/web/test_goal_api.py`。
"""

from __future__ import annotations

from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import GOAL_CHANGED
from agent_harness.session.goal import (
    DEFAULT_MAX_GOAL_ROUNDS,
    GOAL_OBJECTIVE_MAX_LENGTH,
    GoalPhase,
    append_goal_change,
    effective_goal,
)

_GOOD = """## Objective
把登录页修好

## Success criteria
登录成功跳转

## Verification
pytest tests/auth
"""


def _session(tmp_path) -> Session:
    return Session.start(JsonlSessionStore(root=tmp_path), cwd=str(tmp_path))


def test_create_projects_full_snapshot(tmp_path) -> None:
    session = _session(tmp_path)
    outcome = append_goal_change(session, "create", objective=_GOOD)
    assert outcome.ok, outcome.reason
    assert session.events[-1].type == GOAL_CHANGED
    data = session.events[-1].data
    assert data["operation"] == "create"
    assert data["revision"] == 1
    assert data["phase"] == "active"
    assert data["max_goal_rounds"] == DEFAULT_MAX_GOAL_ROUNDS == 32
    assert data["rounds_started"] == 0
    assert data["objective"] == _GOOD.strip()

    state = effective_goal(session.events)
    assert state.defined and state.phase is GoalPhase.ACTIVE
    assert state.goal_id == data["goal_id"]
    assert state.source_event_id == session.events[-1].event_id
    assert state.source_seq == session.events[-1].seq
    assert not state.needs_interview


def test_needs_interview_is_measurable(tmp_path) -> None:
    """AC6：缺 `## Success criteria` 或 `## Verification` 段即算模糊。"""
    session = _session(tmp_path)
    append_goal_change(session, "create", objective="## Objective\n随便做点什么")
    assert effective_goal(session.events).needs_interview is True

    # 未定义目标不是"模糊"，访谈不该被触发。
    empty = _session(tmp_path / "empty")
    assert effective_goal(empty.events).needs_interview is False


def test_edit_advances_revision_and_keeps_phase(tmp_path) -> None:
    session = _session(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)
    append_goal_change(session, "pause")
    goal_id = effective_goal(session.events).goal_id

    outcome = append_goal_change(session, "edit", objective=_GOOD + "\n## Boundaries\n只改 src")
    assert outcome.ok, outcome.reason
    state = outcome.state
    assert state.revision == 3
    assert state.goal_id == goal_id
    assert state.phase is GoalPhase.PAUSED
    assert state.objective.endswith("只改 src")


def test_revision_cas_conflict_is_zero_events(tmp_path) -> None:
    """AC3：并发 edit 后者 409 + 当前 revision，且拒绝不落任何事件。"""
    session = _session(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)
    before = len(session.events)

    stale = append_goal_change(session, "edit", objective="新目标", expected_revision=7)
    assert not stale.ok
    assert stale.error_kind == "conflict"
    assert "r7" in stale.reason and "r1" in stale.reason
    assert stale.state.revision == 1
    assert len(session.events) == before

    ok = append_goal_change(session, "edit", objective="新目标", expected_revision=1)
    assert ok.ok and ok.state.revision == 2


def test_phase_transitions_and_rejections(tmp_path) -> None:
    session = _session(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)

    assert append_goal_change(session, "resume").ok  # active 幂等恢复
    assert append_goal_change(session, "pause").state.phase is GoalPhase.PAUSED
    assert not append_goal_change(session, "pause").ok
    assert append_goal_change(session, "resume").state.phase is GoalPhase.ACTIVE

    blocked = append_goal_change(
        session, "block", blocked_reason={"code": "approval", "message": "等审批"}
    )
    assert blocked.ok and blocked.state.phase is GoalPhase.BLOCKED
    assert blocked.state.blocked_reason == {"code": "approval", "message": "等审批"}
    # edit 保留 blocked reason（只是改文本）。
    edited = append_goal_change(session, "edit", objective=_GOOD + "x")
    assert edited.state.phase is GoalPhase.BLOCKED
    assert edited.state.blocked_reason is not None
    assert append_goal_change(session, "resume").state.phase is GoalPhase.ACTIVE

    completed = append_goal_change(session, "complete")
    assert completed.ok and completed.state.phase is GoalPhase.COMPLETE
    # 票面：complete 只能从 active —— paused/blocked/complete 都必须拒。
    assert not append_goal_change(session, "complete").ok
    assert not append_goal_change(session, "resume").ok
    # 完成后可开新目标（revision 归 1、新 goal_id）。
    old_id = completed.state.goal_id
    fresh = append_goal_change(session, "create", objective=_GOOD)
    assert fresh.ok and fresh.state.revision == 1 and fresh.state.goal_id != old_id


def test_create_rejected_while_live_goal_exists(tmp_path) -> None:
    session = _session(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)
    before = len(session.events)
    dup = append_goal_change(session, "create", objective="另一个")
    assert not dup.ok and dup.error_kind == "conflict"
    assert len(session.events) == before


def test_block_requires_reason(tmp_path) -> None:
    session = _session(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)
    before = len(session.events)
    for reason in (None, {}, {"code": "", "message": "x"}, {"code": "c", "message": " "}):
        out = append_goal_change(session, "block", blocked_reason=reason)
        assert not out.ok and out.error_kind == "shape", reason
    assert len(session.events) == before


def test_shape_rejections_are_zero_events(tmp_path) -> None:
    session = _session(tmp_path)
    before = len(session.events)
    for objective in (None, "", "   ", 42, "x" * (GOAL_OBJECTIVE_MAX_LENGTH + 1)):
        out = append_goal_change(session, "create", objective=objective)
        assert not out.ok and out.error_kind == "shape", repr(objective)[:40]
    for rounds in (0, -1, True, "3"):
        out = append_goal_change(session, "create", objective=_GOOD, max_goal_rounds=rounds)
        assert not out.ok and out.error_kind == "shape", rounds
    unknown = append_goal_change(session, "destroy")
    assert not unknown.ok and unknown.error_kind == "shape"
    assert len(session.events) == before


def test_clear_writes_tombstone_and_keeps_history(tmp_path) -> None:
    session = _session(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)
    before = len(session.events)
    cleared = append_goal_change(session, "clear")
    assert cleared.ok and not cleared.state.defined
    assert session.events[-1].data["operation"] == "clear"
    assert session.events[-1].data["cleared"]["revision"] == 2
    assert len(session.events) == before + 1
    assert effective_goal(session.events).defined is False

    # 墓碑之后可重新创建；二次 clear 没有目标可清 → 409。
    assert append_goal_change(session, "create", objective=_GOOD).ok
    append_goal_change(session, "clear")
    no_goal = append_goal_change(session, "clear")
    assert not no_goal.ok and no_goal.error_kind == "conflict"


def test_resume_rejected_when_rounds_exhausted(tmp_path) -> None:
    session = _session(tmp_path)
    append_goal_change(session, "create", objective=_GOOD, max_goal_rounds=2)
    append_goal_change(session, "pause")
    # 阶段 3 才会写 rounds_started；这里直接构造投影前置（事件是唯一事实源）。
    base = effective_goal(session.events)
    session.append(
        GOAL_CHANGED,
        {
            "operation": "resume",
            "goal_id": base.goal_id,
            "revision": base.revision + 1,
            "objective": base.objective,
            "phase": "active",
            "max_goal_rounds": 2,
            "rounds_started": 2,
        },
    )
    session.append(
        GOAL_CHANGED,
        {
            "operation": "pause",
            "goal_id": base.goal_id,
            "revision": base.revision + 2,
            "objective": base.objective,
            "phase": "paused",
            "max_goal_rounds": 2,
            "rounds_started": 2,
        },
    )
    out = append_goal_change(session, "resume")
    assert not out.ok and out.error_kind == "conflict"
    assert "2/2" in out.reason


def test_bad_payload_is_skipped_last_wins(tmp_path) -> None:
    """坏事件只损失它自己：继续往前找，不猜半截目标（与 workflow 同纪律）。"""
    session = _session(tmp_path)
    append_goal_change(session, "create", objective=_GOOD)
    good_id = effective_goal(session.events).goal_id
    session.append(GOAL_CHANGED, {"operation": "edit", "revision": "many"})
    session.append(GOAL_CHANGED, "not-a-dict")  # type: ignore[arg-type]
    state = effective_goal(session.events)
    assert state.defined and state.goal_id == good_id


def test_no_goal_projection_on_plain_session(tmp_path) -> None:
    session = _session(tmp_path)
    state = effective_goal(session.events)
    assert state.defined is False
    assert state.revision == 0
    assert state.to_payload()["defined"] is False
