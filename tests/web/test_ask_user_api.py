"""#1204：`ask_user` 的暂停 / 恢复集成（web 全链路）。

覆盖：工具 → `USER_INPUT_REQUESTED(kind=ask_user)` → run 暂停 → resume 带 answers →
答案以 `USER_MESSAGE` 落盘 → run 继续；以及整卡跳过（各题 `answer=null`）注入
"用户跳过了提问…"消息后 run 仍继续。
"""

from __future__ import annotations

from uuid import uuid4

from langchain_core.messages import AIMessage

from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_PAUSED,
    RUN_RESUMED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
)
from agent_harness.session.session import Session
from tests.web.test_budget_local_fuse_api import _web
from tests.web.test_constraint_resolution_api import _ModelProbe

_QUESTIONS = [
    {
        "question": "Which database should we use?",
        "header": "Storage",
        "options": [
            {"label": "SQLite", "description": "local file"},
            {"label": "Postgres", "description": "server"},
        ],
        "recommended": 0,
    }
]


def _web_run(tmp_path):
    app, client = _web(tmp_path)
    session_id = str(uuid4())
    Session.start(app.state.agent.store, session_id=session_id)
    return app, client, session_id


def _start_probe() -> _ModelProbe:
    return _ModelProbe([
        [AIMessage(content="", tool_calls=[{
            "id": "ask-1",
            "name": "ask_user",
            "args": {"questions": _QUESTIONS},
        }])],
        [AIMessage(content="Continued after the user answered.")],
    ])


def test_ask_user_pauses_and_resume_continues_the_same_run(tmp_path):
    _app, client, session_id = _web_run(tmp_path)
    probe = _start_probe()

    with probe:
        started = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": "Please set up the storage layer.",
                "budget": {
                    "run": {"max_agent_turns_total": 8, "max_model_requests": 8},
                    "session": {"max_agent_turns_total": 12, "max_model_requests": 12},
                },
            },
        )
        assert started.status_code == 200, started.text
        before = client.get(f"/api/sessions/{session_id}/events").json()
        request = next(e for e in before if e["type"] == USER_INPUT_REQUESTED)
        pause = next(e for e in before if e["type"] == RUN_PAUSED)
        assert request["data"]["kind"] == "ask_user"
        assert request["data"]["questions"][0]["question"] == "Which database should we use?"
        assert pause["data"]["reason"] == "user_input"
        assert pause["data"]["input_request_id"] == request["data"]["request_id"]
        assert len(probe.models[0].snapshots) == 1

        resumed = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": pause["run_id"],
                "resume_basis": "user_input",
                "budget": {"expected_version": pause["data"]["budget_version"], "run": {}},
                "input_request": {
                    "request_id": request["data"]["request_id"],
                    "questions": [
                        {
                            "question": "Which database should we use?",
                            "answer": {"label": "SQLite"},
                        }
                    ],
                },
            },
        )
        assert resumed.status_code == 200, resumed.text
        assert "run/completed" in resumed.text

    events = client.get(f"/api/sessions/{session_id}/events").json()
    answer_event = next(
        e for e in events
        if e["type"] == USER_MESSAGE
        and e["data"].get("input_request_id") == request["data"]["request_id"]
    )
    assert answer_event["data"]["input_request_answer"] == {
        "request_id": request["data"]["request_id"],
        "questions": [
            {"question": "Which database should we use?", "answer": {"label": "SQLite"}}
        ],
    }
    assert len([e for e in events if e["type"] == RUN_RESUMED]) == 1
    assert len([e for e in events if e["type"] == RUN_COMPLETED]) == 1
    resumed_messages = "\n".join(
        str(message.content) for message in probe.models[1].snapshots[0].messages
    )
    assert "Which database should we use?" in resumed_messages
    assert "SQLite" in resumed_messages


def test_ask_user_full_card_skip_continues_with_skip_message(tmp_path):
    app, client, session_id = _web_run(tmp_path)
    probe = _start_probe()

    with probe:
        started = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": "Please set up the storage layer.",
                "budget": {
                    "run": {"max_agent_turns_total": 8, "max_model_requests": 8},
                },
            },
        )
        assert started.status_code == 200, started.text
        before = client.get(f"/api/sessions/{session_id}/events").json()
        request = next(e for e in before if e["type"] == USER_INPUT_REQUESTED)
        pause = next(e for e in before if e["type"] == RUN_PAUSED)

        resumed = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": pause["run_id"],
                "resume_basis": "user_input",
                "budget": {"expected_version": pause["data"]["budget_version"], "run": {}},
                "input_request": {
                    "request_id": request["data"]["request_id"],
                    "questions": [
                        {"question": "Which database should we use?", "answer": None}
                    ],
                },
            },
        )
        assert resumed.status_code == 200, resumed.text
        assert "run/completed" in resumed.text

    events = app.state.agent.store.read_events(session_id)
    assert sum(
        1 for e in events
        if e.type == USER_MESSAGE and e.data.get("input_request_id")
    ) == 1
    answer_event = next(
        e for e in events
        if e.type == USER_MESSAGE and e.data.get("input_request_id")
    )
    assert answer_event.data["input_request_answer"]["questions"] == [
        {"question": "Which database should we use?", "answer": None}
    ]
    resumed_messages = "\n".join(
        str(message.content) for message in probe.models[1].snapshots[0].messages
    )
    assert "用户跳过了提问（共 1 题：Which database should we use?）" in resumed_messages


def test_ask_user_timeout_resume_injects_message_without_any_answer(tmp_path):
    """超时到点：注入"用户可能不在"系统消息，**不替用户选**（不写任何 answer）。"""
    app, client, session_id = _web_run(tmp_path)
    probe = _start_probe()

    with probe:
        started = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": "Please set up the storage layer.",
                "budget": {"run": {"max_agent_turns_total": 8, "max_model_requests": 8}},
            },
        )
        assert started.status_code == 200, started.text
        before = client.get(f"/api/sessions/{session_id}/events").json()
        request = next(e for e in before if e["type"] == USER_INPUT_REQUESTED)
        pause = next(e for e in before if e["type"] == RUN_PAUSED)

        resumed = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": pause["run_id"],
                "resume_basis": "user_input",
                "budget": {"expected_version": pause["data"]["budget_version"], "run": {}},
                "input_request": {
                    "request_id": request["data"]["request_id"],
                    "timeout": True,
                },
            },
        )
        assert resumed.status_code == 200, resumed.text
        assert "run/completed" in resumed.text

    events = app.state.agent.store.read_events(session_id)
    answer_event = next(
        e for e in events
        if e.type == USER_MESSAGE and e.data.get("input_request_id")
    )
    assert answer_event.data["input_request_answer"] == {
        "request_id": request["data"]["request_id"],
        "timeout": True,
    }
    assert "questions" not in answer_event.data["input_request_answer"]
    resumed_messages = "\n".join(
        str(message.content) for message in probe.models[1].snapshots[0].messages
    )
    assert "用户在 5 分钟内未回答，可能不在，请按你的判断继续" in resumed_messages


def test_ask_user_pause_rejects_constraint_shaped_answer(tmp_path):
    """kind 分流：ask_user 暂停必须用 questions 形态回答，收到 choice 形态=422（不误当约束）。"""
    _app, client, session_id = _web_run(tmp_path)
    probe = _start_probe()

    with probe:
        started = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": "Please set up the storage layer.",
                "budget": {"run": {"max_agent_turns_total": 8, "max_model_requests": 8}},
            },
        )
        assert started.status_code == 200, started.text
        before = client.get(f"/api/sessions/{session_id}/events").json()
        request = next(e for e in before if e["type"] == USER_INPUT_REQUESTED)
        pause = next(e for e in before if e["type"] == RUN_PAUSED)

        rejected = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": pause["run_id"],
                "resume_basis": "user_input",
                "budget": {"expected_version": pause["data"]["budget_version"], "run": {}},
                "input_request": {
                    "request_id": request["data"]["request_id"],
                    "choice": "keep_existing",
                },
            },
        )

    assert rejected.status_code == 422, rejected.text
    after = client.get(f"/api/sessions/{session_id}/events").json()
    assert after == before
