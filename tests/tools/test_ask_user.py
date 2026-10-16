"""#1204：通用版运行中提问 `ask_user` 的参数校验（TDD 首红）。

形状抄 Claude Code `AskUserQuestion`（一手
https://code.claude.com/docs/en/agent-sdk/user-input）：`questions[]` 1–4 题、
每题 options 2–4 个、`header` ≤12 字符；`recommended` 为本仓自创（源自 dsh
效果图"推荐"标，越界参数校验直接拒绝）。超限一律 pydantic 参数错误。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_harness.session.context import (
    ConstraintToolContext,
    current_constraint_tool_context_var,
    current_session_var,
    run_context_var,
)
from agent_harness.session.event import USER_INPUT_REQUESTED
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tools.ask_user import (
    AskOption,
    AskQuestion,
    AskUserArgs,
    AskUserTool,
)


def _option(label: str = "A") -> dict:
    return {"label": label}


def _question(
    *,
    question: str = "Which one?",
    header: str = "pick",
    options: list[dict] | None = None,
    recommended: int | None = None,
) -> dict:
    payload = {
        "question": question,
        "header": header,
        "options": options if options is not None else [_option("A"), _option("B")],
    }
    if recommended is not None:
        payload["recommended"] = recommended
    return payload


def test_one_question_is_accepted():
    args = AskUserArgs(questions=[_question()])
    assert len(args.questions) == 1


def test_four_questions_are_accepted():
    args = AskUserArgs(questions=[_question() for _ in range(4)])
    assert len(args.questions) == 4


def test_zero_questions_are_rejected():
    with pytest.raises(ValidationError):
        AskUserArgs(questions=[])


def test_five_questions_are_rejected():
    with pytest.raises(ValidationError):
        AskUserArgs(questions=[_question() for _ in range(5)])


# ── 审查 P1：纯空白 label 必须参数拒绝 ──
# 前端投影对空白 label 整条丢弃（projection.ts `!label.trim()`），若工具端放行
# 空白 label，run 已暂停却没有可渲染的卡片 ⇒ 无 UI 出口卡死。
@pytest.mark.parametrize("blank", [" ", "\n", "\t", "   "])
def test_whitespace_only_option_label_is_rejected(blank):
    with pytest.raises(ValidationError):
        AskOption(label=blank)


def test_normal_option_label_is_accepted_verbatim():
    option = AskOption(label="今晚低峰期")
    assert option.label == "今晚低峰期"


def test_two_options_are_accepted():
    question = AskQuestion(**_question(options=[_option("A"), _option("B")]))
    assert len(question.options) == 2


def test_four_options_are_accepted():
    question = AskQuestion(
        **_question(options=[_option("A"), _option("B"), _option("C"), _option("D")])
    )
    assert len(question.options) == 4


def test_one_option_is_rejected():
    with pytest.raises(ValidationError):
        AskQuestion(**_question(options=[_option("A")]))


def test_five_options_are_rejected():
    with pytest.raises(ValidationError):
        AskQuestion(
            **_question(
                options=[
                    _option("A"),
                    _option("B"),
                    _option("C"),
                    _option("D"),
                    _option("E"),
                ]
            )
        )


def test_header_of_twelve_characters_is_accepted():
    question = AskQuestion(**_question(header="x" * 12))
    assert len(question.header) == 12


def test_header_of_thirteen_characters_is_rejected():
    with pytest.raises(ValidationError):
        AskQuestion(**_question(header="x" * 13))


def test_recommended_none_is_accepted():
    question = AskQuestion(**_question(recommended=None))
    assert question.recommended is None


def test_recommended_within_range_is_accepted():
    question = AskQuestion(
        **_question(options=[_option("A"), _option("B")], recommended=1)
    )
    assert question.recommended == 1


def test_recommended_out_of_range_is_rejected():
    with pytest.raises(ValidationError):
        AskQuestion(**_question(options=[_option("A"), _option("B")], recommended=2))


def test_recommended_negative_is_rejected():
    with pytest.raises(ValidationError):
        AskQuestion(
            **_question(options=[_option("A"), _option("B")], recommended=-1)
        )


# ── 准入 / 去重（与 request_constraint_resolution 逐字一致，唯不校验来源原文）──


def _session(tmp_path, session_id: str = "ask-user-test") -> Session:
    return Session.start(JsonlSessionStore(tmp_path), session_id=session_id)


def _bind(
    session: Session,
    *,
    run_id: str = "run-1",
    is_delegated_child: bool = False,
    rejection_code: str | None = None,
):
    context = ConstraintToolContext(
        session_id=session.session_id,
        run_id=run_id,
        agent_id="default",
        agent_profile="main",
        source_event_id="src",
        source_seq=0,
        source_content="",
        protected_fact_token_budget=8192,
        user_input_request_rejection_code=rejection_code,
        is_delegated_child=is_delegated_child,
    )
    return (
        current_session_var.set(session),
        run_context_var.set(run_id),
        current_constraint_tool_context_var.set(context),
    )


def _reset(tokens) -> None:
    current_constraint_tool_context_var.reset(tokens[2])
    run_context_var.reset(tokens[1])
    current_session_var.reset(tokens[0])


@pytest.mark.asyncio
async def test_deadline_configured_rejects_without_creating_request(tmp_path):
    session = _session(tmp_path)
    tokens = _bind(session, rejection_code="DEADLINE_CONFIGURED")
    try:
        result = await AskUserTool().execute(
            AskUserArgs(questions=[AskQuestion(**_question())])
        )
    finally:
        _reset(tokens)

    assert result.ok
    assert result.data["status"] == "rejected"
    assert result.data["reason_code"] == "DEADLINE_CONFIGURED"
    assert not result.pending_events
    assert not any(event.type == USER_INPUT_REQUESTED for event in session.events)


@pytest.mark.asyncio
async def test_insufficient_resume_budget_rejects_without_creating_request(tmp_path):
    session = _session(tmp_path)
    tokens = _bind(session, rejection_code="RESUME_UNAVAILABLE")
    try:
        result = await AskUserTool().execute(
            AskUserArgs(questions=[AskQuestion(**_question())])
        )
    finally:
        _reset(tokens)

    assert result.ok
    assert result.data["status"] == "rejected"
    assert result.data["reason_code"] == "RESUME_UNAVAILABLE"
    assert not result.pending_events


@pytest.mark.asyncio
async def test_delegated_child_session_is_disabled(tmp_path):
    session = _session(tmp_path)
    tokens = _bind(session, is_delegated_child=True)
    try:
        result = await AskUserTool().execute(
            AskUserArgs(questions=[AskQuestion(**_question())])
        )
    finally:
        _reset(tokens)

    assert result.ok
    assert result.data["status"] == "rejected"
    assert result.data["reason_code"] == "SCOPE_NOT_ALLOWED"
    assert not result.pending_events


@pytest.mark.asyncio
async def test_duplicate_call_reuses_the_single_pending_request(tmp_path):
    session = _session(tmp_path)
    tokens = _bind(session)
    tool = AskUserTool()
    args = AskUserArgs(questions=[AskQuestion(**_question())])
    try:
        first = await tool.execute(args)
        assert first.ok
        assert first.data["status"] == "requested"
        assert first.pending_events is not None
        event_type, payload = first.pending_events[0]
        assert event_type == USER_INPUT_REQUESTED
        assert payload["kind"] == "ask_user"
        session.append(event_type, payload, run_id="run-1")

        duplicate = await tool.execute(args)
    finally:
        _reset(tokens)

    requests = [event for event in session.events if event.type == USER_INPUT_REQUESTED]
    assert len(requests) == 1
    assert duplicate.data == {
        "status": "pending",
        "request_id": requests[0].data["request_id"],
    }
    assert tool.batch_exclusive is True


@pytest.mark.asyncio
async def test_request_does_not_require_direct_user_source(tmp_path):
    """#1204 票面 §5 准入清单只有 deadline/预算闸、委派禁用、去重——

    与 request_constraint_resolution 不同，ask_user **不**校验"来源须是可核实
    直接用户原文"（context 的 source_* 字段是空的，仍应放行）。
    """
    session = _session(tmp_path)
    tokens = _bind(session)
    try:
        result = await AskUserTool().execute(
            AskUserArgs(questions=[AskQuestion(**_question())])
        )
    finally:
        _reset(tokens)

    assert result.ok
    assert result.data["status"] == "requested"
