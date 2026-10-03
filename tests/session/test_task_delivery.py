"""W-07（#351）：Task / Run / 验证 / 接受四种事实分开的分轴契约。

票面「状态契约」：Task 身份 = Session ID，一 Task 多 Run；三轴事实**分别追加、
分别投影**——定义轴 `task/defined` + `task/acceptance-revised`、验证轴
`verification/updated`、接受轴 `task/accepted` + `task/acceptance-released`。
`run/completed` 只说明 Runtime 收口（#305 完成闸门零写入契约原样），MUST NOT
自动写验证值或接受状态；产品四态（执行中/待验证/可交付/已接受）是纯投影
（`derive_task_state`），刷新/重启从事件流重建同一结果，TUI/Web 不各算一套。
"""

from __future__ import annotations

from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_STARTED,
    TASK_ACCEPTANCE_REVISED,
    TASK_ACCEPTED,
    TASK_DEFINED,
)
from agent_harness.session.task import (
    apply_acceptance,
    apply_acceptance_release,
    apply_acceptance_revision,
    apply_task_definition,
    apply_verification,
    derive_task_state,
)


def _session(tmp_path) -> Session:
    return Session.start(JsonlSessionStore(root=tmp_path), cwd=str(tmp_path))


def _criteria(state):
    return [c.item_id for c in state.criteria]


# ── 定义轴：task/defined + task/acceptance-revised ──────────────────────────


def test_task_definition_roundtrip(tmp_path) -> None:
    session = _session(tmp_path)
    outcome = apply_task_definition(
        session,
        task_text="把登录页修好",
        read_write_intent="只读 src/auth，可写 tests/",
        criteria=[{"text": "登录成功跳转", "origin": "user"}],
    )
    assert outcome.ok, outcome.reason
    assert session.events[-1].type == TASK_DEFINED
    state = derive_task_state(session.events)
    assert state.defined and state.task_text == "把登录页修好"
    assert state.read_write_intent == "只读 src/auth，可写 tests/"
    # 工作目录从 session/started 的既有 cwd 锚单源读取（AC5），task/defined 不复制第二份
    assert state.cwd == str(tmp_path)
    assert len(state.criteria) == 1
    assert state.criteria[0].text == "登录成功跳转"
    assert state.criteria[0].origin == "user"
    assert state.criteria[0].confirmed is True
    assert state.criteria[0].item_id.startswith("ac-")
    # 无 open run、无验证值、未接受 ⇒ 待验证；版本从 0 起
    assert state.product_state == "pending_verification"
    assert state.version == 0
    assert state.acceptance is None


def test_task_definition_rejects_bad_payload(tmp_path) -> None:
    session = _session(tmp_path)
    before = len(session.events)
    assert not apply_task_definition(session, task_text="   ").ok
    assert not apply_task_definition(
        session, task_text="T", criteria=[{"text": "A", "origin": "model"}]
    ).ok, "origin 枚举外必须拒绝"
    assert not apply_task_definition(
        session, task_text="T", criteria=[{"text": "A", "confirmed": "yes"}]
    ).ok, "confirmed 必须是布尔"
    assert not apply_task_definition(
        session,
        task_text="T",
        criteria=[{"text": "A", "item_id": "ac-x"}, {"text": "B", "item_id": "ac-x"}],
    ).ok, "item_id 重复必须拒绝"
    assert len(session.events) == before, "拒绝 = 零事件（不产生半条定义）"


def test_acceptance_revision_appends_and_keeps_history(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "旧判据"}])
    first_event = session.events[-1]
    outcome = apply_acceptance_revision(
        session,
        criteria=[
            {"text": "新判据一", "origin": "agent"},  # 缺 confirmed：Agent 提出的生来未确认
            {"text": "新判据二"},
        ],
    )
    assert outcome.ok, outcome.reason
    revision = session.events[-1]
    assert revision.type == TASK_ACCEPTANCE_REVISED
    # 变更 AC 只追加事件，且保留旧版来源（source_event_ids 指向上一版定义/修订）
    assert revision.source_event_ids == [first_event.event_id]
    state = derive_task_state(session.events)
    assert [c.text for c in state.criteria] == ["新判据一", "新判据二"]
    assert state.criteria[0].confirmed is False, "agent 提出的清单默认未确认"
    assert state.criteria[1].confirmed is True, "user 清单默认已确认"
    # 旧版事实原样留在事件流里（append-only，不删除不改写）
    assert any(
        e.type == TASK_DEFINED and e.event_id == first_event.event_id
        for e in session.events
    )


# ── 验证轴：verification/updated 逐项 last-wins ─────────────────────────────


def test_verification_last_wins(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}, {"text": "B"}])
    state0 = derive_task_state(session.events)
    item_a, item_b = _criteria(state0)
    assert apply_verification(session, item_a, "in_progress").ok
    assert apply_verification(
        session, item_a, "passed", evidence="pytest -q tests/auth：12 passed"
    ).ok
    assert apply_verification(session, item_b, "failed", evidence="3 failed").ok
    state = derive_task_state(session.events)
    assert state.verification[item_a].value == "passed"
    assert state.verification[item_a].evidence == "pytest -q tests/auth：12 passed"
    assert state.verification[item_b].value == "failed"
    # 观察事实可被后续 run 重估覆盖（回归是真实语义），旧值留痕于更早事件
    assert apply_verification(session, item_a, "failed").ok
    assert derive_task_state(session.events).verification[item_a].value == "failed"


def test_verification_rejects_unknown_item_and_bad_value(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    item_a = _criteria(derive_task_state(session.events))[0]
    before = len(session.events)
    unknown = apply_verification(session, "ac-does-not-exist", "passed")
    assert not unknown.ok and unknown.error_kind == "shape"
    bad_value = apply_verification(session, item_a, "skipped")
    assert not bad_value.ok and bad_value.error_kind == "shape"
    assert len(session.events) == before, "拒绝 = 零事件"


# ── 接受轴：task/accepted + task/acceptance-released，CAS 保护 ───────────────


def test_acceptance_cas_and_no_double_write(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    stale = apply_acceptance(session, "accepted", expected_version=1)
    assert not stale.ok and stale.error_kind == "conflict", "版本过期必须 409 语义"
    assert apply_acceptance(session, "accepted", expected_version=0).ok
    state = derive_task_state(session.events)
    assert state.product_state == "accepted"
    assert state.version == 1
    # 已接受再接受 = 明确冲突（不能双写）：接受事件全流恰一条
    again = apply_acceptance(session, "accepted", expected_version=1)
    assert not again.ok and again.error_kind == "conflict"
    assert len([e for e in session.events if e.type == TASK_ACCEPTED]) == 1
    # 释放：过期版本拒绝；正确版本回未接受，version 再 +1
    assert apply_acceptance_release(session, expected_version=0).error_kind == "conflict"
    assert apply_acceptance_release(session, expected_version=1, reason="还要补测试").ok
    state = derive_task_state(session.events)
    assert state.acceptance is None and state.version == 2
    # 释放后可再接受（带缺项接受也要走 CAS）
    assert apply_acceptance(
        session, "accepted_with_gaps", reason="失败项留到下批", expected_version=2
    ).ok
    state = derive_task_state(session.events)
    assert state.acceptance is not None and state.acceptance.decision == "accepted_with_gaps"
    assert state.product_state == "accepted"


def test_acceptance_shape_rules(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    before = len(session.events)
    gaps_without_reason = apply_acceptance(
        session, "accepted_with_gaps", reason=None, expected_version=0
    )
    assert not gaps_without_reason.ok and gaps_without_reason.error_kind == "shape"
    bad_decision = apply_acceptance(session, "auto_accepted", expected_version=0)
    assert not bad_decision.ok and bad_decision.error_kind == "shape"
    release_when_never_accepted = apply_acceptance_release(
        session, expected_version=0
    )
    assert (
        not release_when_never_accepted.ok
        and release_when_never_accepted.error_kind == "conflict"
    ), "从未接受过就没有可释放的接受事实"
    assert len(session.events) == before, "全部拒绝 = 零事件"


# ── 产品四态投影：执行中/待验证/可交付/已接受 + run/completed 边界 ────────────


def test_product_state_precedence_and_run_completed_boundary(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}, {"text": "B"}])
    item_a, item_b = _criteria(derive_task_state(session.events))
    session.append(RUN_STARTED, {}, run_id="r1")
    assert derive_task_state(session.events).product_state == "executing"
    # run/completed 只说明 Runtime 收口：验证轴 / 接受轴零自动写入
    session.append(RUN_COMPLETED, {"final_text": "做完了"}, run_id="r1")
    state = derive_task_state(session.events)
    assert state.product_state == "pending_verification"
    assert state.acceptance is None
    assert item_a not in state.verification and item_b not in state.verification
    apply_verification(session, item_a, "passed")
    assert derive_task_state(session.events).product_state == "pending_verification"
    apply_verification(session, item_b, "passed")
    assert derive_task_state(session.events).product_state == "deliverable"
    assert apply_acceptance(session, "accepted", expected_version=0).ok
    assert derive_task_state(session.events).product_state == "accepted"


def test_product_state_requires_at_least_one_passed_item(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T")  # 缺项：清单为空
    session.append(RUN_COMPLETED, {}, run_id="r1")
    state = derive_task_state(session.events)
    assert state.product_state == "pending_verification", (
        "零验收项时『证据齐全』不成立——run/completed 不能把空清单任务变成可交付"
    )


# ── 持久化：重启后从事件流重建同一投影 ────────────────────────────────────────


def test_restart_rebuilds_same_state(tmp_path) -> None:
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store, cwd=str(tmp_path))
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    item_a = _criteria(derive_task_state(session.events))[0]
    apply_verification(session, item_a, "passed", evidence="12 passed")
    apply_acceptance(session, "accepted", expected_version=0)
    before = derive_task_state(session.events).to_payload()
    reloaded = Session.load(store, session.session_id)
    assert derive_task_state(reloaded.events).to_payload() == before


def test_session_without_task_definition_has_no_task_state(tmp_path) -> None:
    session = _session(tmp_path)
    state = derive_task_state(session.events)
    assert not state.defined
    assert state.product_state == ""  # 未定义任务：无交付状态可言（REST 404 语义）
