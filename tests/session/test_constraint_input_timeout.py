from __future__ import annotations

from types import SimpleNamespace

from agent_harness.session import USER_INPUT_REQUESTED, USER_MESSAGE, SessionEvent
from agent_harness.session.service import _user_input_answer_data


def test_constraint_timeout_resumes_without_selecting_or_authorizing_an_option():
    request = SessionEvent(
        seq=2,
        type=USER_INPUT_REQUESTED,
        session_id="session-1",
        run_id="run-1",
        data={"request_id": "request-1", "kind": "protected_fact_conflict"},
    )
    paused = SimpleNamespace(reason="user_input", input_request_id="request-1")
    answer = {"request_id": "request-1", "timeout": True}

    data, replayed = _user_input_answer_data(
        [request], run_id="run-1", paused=paused, answer=answer,
        ask_user_timeout_seconds=300,
    )

    assert not replayed
    assert data is not None
    assert data["input_request_answer"] == answer
    assert "未解决" in data["content"]
    assert "未选择或授权任何选项" in data["content"]
    assert "停止受冲突影响的工作" in data["content"]
    assert "choice" not in data["input_request_answer"]

    answer_event = SessionEvent(
        seq=3, type=USER_MESSAGE, session_id="session-1", run_id="run-1",
        data=data,
    )
    replayed_data, replayed = _user_input_answer_data(
        [request, answer_event], run_id="run-1", paused=paused, answer=answer,
        ask_user_timeout_seconds=300,
    )
    assert replayed
    assert replayed_data is None
