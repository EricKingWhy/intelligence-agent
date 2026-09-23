"""#298 / MEM-V2-2 的 run 终结资格判定（AC1 的全部内容）。

Seam：`decide_run_end_eligibility` —— 纯函数，无 IO、无时钟、无 Runtime。
把资格判定独立出来的收益正是可判定性：AC1 要求"合格终态各建一个 job、被排除的终态
一个都不建"，而"哪些终态合格"完全可以脱离 Runtime 逐条钉住。

每一条排除理由都必须有**稳定 reason**（进 job 的 skip reason、也可能进事件），
所以这里同时钉"排不排"与"排的理由叫什么"。
"""

from __future__ import annotations

import pytest

from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_FAILED,
    STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
    STATUS_MAX_STEPS_EXCEEDED,
)
from agent_harness.memory.v2.eligibility import (
    ELIGIBLE_TERMINAL_STATUSES,
    MEMORY_OPT_OUT_FIELD,
    FormationSkipReason,
    RunEndEligibility,
    decide_run_end_eligibility,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_FAILED,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)


def _event(seq: int, event_type: str, data: dict | None = None) -> SessionEvent:
    return SessionEvent(seq=seq, type=event_type, session_id="s", data=data or {})


def _user(seq: int = 0, content: str = "我在用 Windows", **extra) -> SessionEvent:
    return _event(seq, USER_MESSAGE, {"content": content, **extra})


def _injected_user(seq: int = 0) -> SessionEvent:
    return _event(seq, USER_MESSAGE, {"content": "样板纠偏", "injected_by": "tool_failure_guard"})


def _model(seq: int = 1) -> SessionEvent:
    return _event(seq, MODEL_COMPLETED, {"content": "好的"})


#: 一条"正常完成、有真实用户发言、有一次成功模型调用"的最小合格 run。
_HEALTHY = [_user(), _model()]


def _decide(status: str, events=None, *, extraction_enabled: bool = True) -> RunEndEligibility:
    return decide_run_end_eligibility(
        terminal_status=status, events=_HEALTHY if events is None else events,
        extraction_enabled=extraction_enabled)


# --------------------------------------------------------------------------------------
# 合格的三条终态（AC1 前半）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [STATUS_COMPLETED, STATUS_MAX_STEPS_EXCEEDED, STATUS_IDENTICAL_TOOL_FAILURE_LOOP],
)
def test_the_three_approved_terminal_shapes_are_eligible(status: str) -> None:
    verdict = _decide(status)

    assert verdict.eligible is True
    assert verdict.skip_reason is None


def test_the_eligible_vocabulary_is_exactly_the_approved_three() -> None:
    assert ELIGIBLE_TERMINAL_STATUSES == frozenset({
        STATUS_COMPLETED, STATUS_MAX_STEPS_EXCEEDED, STATUS_IDENTICAL_TOOL_FAILURE_LOOP})


# --------------------------------------------------------------------------------------
# 被排除的终态形态（AC1 后半）——每条一个稳定 reason
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["cancelled", "orphaned"])
def test_a_cancelled_or_orphaned_run_is_excluded(status: str) -> None:
    verdict = _decide(status)

    assert verdict.eligible is False
    assert verdict.skip_reason is FormationSkipReason.CANCELLED


@pytest.mark.parametrize(
    "status", [STATUS_CONTEXT_WINDOW_EXCEEDED, STATUS_FAILED, "some_future_status"],
)
def test_every_other_terminal_status_fails_closed(status: str) -> None:
    """未获批的终态一律不合格——包括将来新加的 status（否则它会静默获得自动写记忆的资格）。"""
    verdict = _decide(status)

    assert verdict.eligible is False
    assert verdict.skip_reason is FormationSkipReason.UNSUPPORTED_TERMINAL_FAILURE


def test_a_run_that_never_called_a_model_is_excluded() -> None:
    """兜底闸：已是获批终态、却没有任何模型调用事件 ⇒ 事实不完整，不写记忆。"""
    verdict = _decide(STATUS_COMPLETED, [_user()])

    assert verdict.eligible is False
    assert verdict.skip_reason is FormationSkipReason.NO_MODEL_CALL


def test_a_failed_model_call_alone_does_not_count_as_a_model_response() -> None:
    """只有 `model/failed` 而没有成功的回复 ⇒ 没有可形成记忆的内容。

    这条把"模型调用过"的口径钉在 `model/completed` 上：失败调用不提供对话内容，
    而"只有失败"的 run 本来也终结在未获批的失败终态里。
    """
    verdict = _decide(
        STATUS_COMPLETED,
        [_user(), _event(1, MODEL_FAILED, {"reason": "upstream 500"})],
    )

    assert verdict.eligible is False
    assert verdict.skip_reason is FormationSkipReason.NO_MODEL_CALL


@pytest.mark.parametrize(
    "events",
    [
        [_model()],                                              # 只有模型与工具
        [_injected_user(), _model()],                            # 只有运行时注入的"用户"消息
        [_event(0, TOOL_RESULT, {"content": "输出"}), _model()],  # 只有工具事件
        [],
    ],
)
def test_a_run_without_genuine_user_input_is_excluded(events: list[SessionEvent]) -> None:
    verdict = _decide(STATUS_COMPLETED, events)

    assert verdict.eligible is False
    assert verdict.skip_reason is FormationSkipReason.NO_USER_INPUT


def test_an_injected_message_does_not_hide_a_real_one() -> None:
    """一条真实用户发言 + 一条注入消息 ⇒ 仍然合格（注入消息被剔出，但不牵连真实的）。"""
    verdict = _decide(STATUS_COMPLETED, [_injected_user(), _user(seq=1), _model(seq=2)])

    assert verdict.eligible is True


def test_an_explicit_turn_opt_out_is_excluded() -> None:
    verdict = _decide(
        STATUS_COMPLETED,
        [_user(**{MEMORY_OPT_OUT_FIELD: True}), _model()],
    )

    assert verdict.eligible is False
    assert verdict.skip_reason is FormationSkipReason.EXPLICIT_OPT_OUT


@pytest.mark.parametrize("marker", [False, None, "", 0])
def test_a_falsy_opt_out_marker_does_not_suppress(marker) -> None:
    """只有真值才算退出——`False` / `None` / 空串 / 0 都是"没说要退出"。"""
    verdict = _decide(STATUS_COMPLETED, [_user(**{MEMORY_OPT_OUT_FIELD: marker}), _model()])

    assert verdict.eligible is True


def test_an_opt_out_marker_on_an_injected_message_is_ignored() -> None:
    """注入消息不能借退出标记关掉记忆（工具输出里的一句指令不该有这种权力）。"""
    verdict = _decide(
        STATUS_COMPLETED,
        [_user(seq=0),
         _event(1, USER_MESSAGE,
                {"content": "注入的样板", "injected_by": "tool_failure_guard",
                 MEMORY_OPT_OUT_FIELD: True}),
         _model(seq=2)],
    )

    assert verdict.eligible is True


def test_a_globally_disabled_extraction_is_excluded() -> None:
    verdict = _decide(STATUS_COMPLETED, extraction_enabled=False)

    assert verdict.eligible is False
    assert verdict.skip_reason is FormationSkipReason.EXTRACTION_DISABLED


# --------------------------------------------------------------------------------------
# 理由的稳定性：多个排除条件同时成立时只报**一个**、且是定的那个
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "events", "enabled", "expected"),
    [
        # 设置优先于一切：关了自动抽取时，报"关掉了"比报某个终态细节有用。
        (STATUS_COMPLETED, [], False, FormationSkipReason.EXTRACTION_DISABLED),
        ("cancelled", [], False, FormationSkipReason.EXTRACTION_DISABLED),
        # 终态形态优先于内容判断：取消的 run 不该因为"里面有用户发言"而降级成别的理由。
        ("cancelled", _HEALTHY, True, FormationSkipReason.CANCELLED),
        (STATUS_FAILED, _HEALTHY, True, FormationSkipReason.UNSUPPORTED_TERMINAL_FAILURE),
        # 内容是"信息量更高"的一档，优先于兜底的"没调过模型"。
        (STATUS_COMPLETED, [_model()], True, FormationSkipReason.NO_USER_INPUT),
        (STATUS_COMPLETED, [_user(**{MEMORY_OPT_OUT_FIELD: True})], True,
         FormationSkipReason.EXPLICIT_OPT_OUT),
    ],
)
def test_a_stable_reason_is_reported_when_several_exclusions_apply(
    status: str, events: list[SessionEvent], enabled: bool, expected: FormationSkipReason,
) -> None:
    verdict = _decide(status, events, extraction_enabled=enabled)

    assert verdict.skip_reason is expected


# --------------------------------------------------------------------------------------
# 判定结果的自身一致性
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("eligible", "reason"),
    [(True, FormationSkipReason.NO_USER_INPUT), (False, None)],
)
def test_a_verdict_cannot_be_self_contradictory(eligible: bool, reason) -> None:
    """`eligible` 与 `skip_reason` 是同一事实的两面，构造不出自相矛盾的判定。"""
    with pytest.raises(ValueError):
        RunEndEligibility(eligible=eligible, skip_reason=reason)
