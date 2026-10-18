"""#1356 方案 3：`plan_approval` 的恢复归一（领域层）。

三种决策共用 #1204 的 `resume_basis="user_input"` 通道，按事件 `kind` 分流。
本文件只钉**纯函数**的归一结果：注入消息的文案与 durable `input_request_answer`
的形状（幂等判据逐字比较它，形状不稳会让重放变成 409）。

切档（AC6 的顺序）**不**在这里——`_user_input_answer_data` 每次恢复被调用三次
（阶段一校验 / 恢复后复校 / 落盘前），带副作用就会重复写 mode 事件。切档落在
`_commit_paused_resume` 的落盘点，由 `tests/web/test_plan_approval_api.py` 钉。
"""

from __future__ import annotations

import pytest

from agent_harness.session import USER_INPUT_REQUESTED, USER_MESSAGE, SessionEvent
from agent_harness.session.service import (
    BudgetRejection,
    _plan_approval_input_answer_data,
)

_REQUEST_ID = "req-1356"


def _request(*, artifact_id: str = "abc123") -> SessionEvent:
    return SessionEvent(
        seq=0,
        type=USER_INPUT_REQUESTED,
        session_id="s1",
        run_id="run-1",
        data={
            "request_id": _REQUEST_ID,
            "kind": "plan_approval",
            "run_id": "run-1",
            "plan_markdown": "# 计划",
            "touched_files": ["src/a.py"],
            "artifact_id": artifact_id,
            "plan_sha256": "0" * 64,
        },
    )


def _normalize(answer: dict, *, events: list[SessionEvent] | None = None):
    return _plan_approval_input_answer_data(
        events or [], request_id=_REQUEST_ID, request=_request(), answer=answer,
    )


def test_approve_renders_approval_and_artifact_ref():
    data, replayed = _normalize({"request_id": _REQUEST_ID, "decision": "approve"})

    assert replayed is False
    assert data is not None
    assert data["input_request_id"] == _REQUEST_ID
    assert data["input_request_answer"] == {
        "request_id": _REQUEST_ID,
        "decision": "approve",
    }
    assert data["content"].startswith("计划已批准，现在开始执行。")
    # 计划正文已落 artifact：批准消息必须带上引用，模型据此回读而非复述。
    assert "abc123" in data["content"]


def test_revise_echoes_the_user_feedback_verbatim():
    feedback = "第 2 步改成先写测试；不要动 b.py。"
    data, _ = _normalize({
        "request_id": _REQUEST_ID, "decision": "revise", "feedback": feedback,
    })

    assert data is not None
    assert data["input_request_answer"] == {
        "request_id": _REQUEST_ID,
        "decision": "revise",
        "feedback": feedback,
    }
    assert feedback in data["content"]
    assert "submit_plan" in data["content"]


def test_reject_echoes_the_user_feedback_verbatim():
    feedback = "方向不对，先别改任何文件。"
    data, _ = _normalize({
        "request_id": _REQUEST_ID, "decision": "reject", "feedback": feedback,
    })

    assert data is not None
    assert data["content"].startswith("用户拒绝了该计划")
    assert feedback in data["content"]
    assert data["input_request_answer"]["decision"] == "reject"


@pytest.mark.parametrize("decision", ["revise", "reject"])
def test_feedback_is_optional_but_never_blank(decision):
    """`feedback` 可省（Claude Code 的 "No, keep planning" 就不带理由）；
    但显式给了空白串属于形状错误，不得静默当成"没给"。"""
    data, _ = _normalize({"request_id": _REQUEST_ID, "decision": decision})
    assert data is not None
    assert "feedback" not in data["input_request_answer"]

    with pytest.raises(BudgetRejection):
        _normalize({"request_id": _REQUEST_ID, "decision": decision, "feedback": "   "})


def test_unknown_decision_is_rejected():
    with pytest.raises(BudgetRejection):
        _normalize({"request_id": _REQUEST_ID, "decision": "approve_all"})


def test_foreign_fields_are_rejected():
    """fail-closed：与另外两条 kind 分支同款，未知字段一律拒绝。"""
    with pytest.raises(BudgetRejection):
        _normalize({"request_id": _REQUEST_ID, "decision": "approve", "choice": "custom"})


def test_feedback_over_the_limit_is_rejected():
    with pytest.raises(BudgetRejection):
        _normalize({
            "request_id": _REQUEST_ID, "decision": "revise", "feedback": "x" * 100_001,
        })


def test_replay_is_detected_and_returns_no_data():
    """同一 request_id 已落过等价回答 ⇒ `(None, True)`（幂等重放不重复注入）。"""
    first, _ = _normalize({"request_id": _REQUEST_ID, "decision": "approve"})
    assert first is not None
    recorded = SessionEvent(
        seq=1,
        type=USER_MESSAGE,
        session_id="s1",
        run_id="run-1",
        data={
            "content": first["content"],
            "input_request_id": _REQUEST_ID,
            "input_request_answer": first["input_request_answer"],
        },
    )

    data, replayed = _normalize(
        {"request_id": _REQUEST_ID, "decision": "approve"}, events=[recorded],
    )
    assert (data, replayed) == (None, True)


def test_conflicting_replay_raises():
    first, _ = _normalize({"request_id": _REQUEST_ID, "decision": "approve"})
    recorded = SessionEvent(
        seq=1,
        type=USER_MESSAGE,
        session_id="s1",
        run_id="run-1",
        data={"input_request_id": _REQUEST_ID,
              "input_request_answer": first["input_request_answer"]},
    )
    with pytest.raises(Exception) as excinfo:
        _normalize({"request_id": _REQUEST_ID, "decision": "reject"}, events=[recorded])
    assert "已用不同回答处理" in str(excinfo.value)


def test_normalizer_is_side_effect_free():
    """恢复流程一次会调它三遍——两遍之间事件流不得变化。"""
    events: list[SessionEvent] = []
    first, _ = _normalize({"request_id": _REQUEST_ID, "decision": "approve"}, events=events)
    second, _ = _normalize({"request_id": _REQUEST_ID, "decision": "approve"}, events=events)
    assert first == second
    assert events == []
