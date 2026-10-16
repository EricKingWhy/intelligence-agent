"""#1204：通用版运行中提问 `ask_user` 的参数校验（TDD 首红）。

形状抄 Claude Code `AskUserQuestion`（一手
https://code.claude.com/docs/en/agent-sdk/user-input）：`questions[]` 1–4 题、
每题 options 2–4 个、`header` ≤12 字符；`recommended` 为本仓自创（源自 dsh
效果图"推荐"标，越界参数校验直接拒绝）。超限一律 pydantic 参数错误。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_harness.tools.ask_user import AskQuestion, AskUserArgs


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
