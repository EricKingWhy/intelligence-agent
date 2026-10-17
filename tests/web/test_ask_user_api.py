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


def _web_run(tmp_path, **settings_overrides):
    app, client = _web(tmp_path, **settings_overrides)
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
        # #1224：超时档随请求落盘（默认 300s），前端据此渲染与后端注入消息一致的数字。
        assert request["data"]["timeout_seconds"] == 300
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


def test_new_message_while_ask_user_pending_reports_neutral_conflict(tmp_path):
    """#1223：`ask_user` 待回答时发新消息 → 409，文案**中性**（不得写死"约束澄清"）。

    约束澄清（`protected_fact_conflict`）与通用提问（`ask_user`）共用同一道
    `_latest_pending_user_input_request` 闸门，但 kind 不同：文案必须按 kind 分流，
    否则通用提问会被误报成"约束澄清"，把用户引导到不存在的约束冲突上。
    """
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
        assert any(e["type"] == USER_INPUT_REQUESTED for e in before)

        blocked = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "Another task before I answer."},
        )

    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert "约束澄清" not in detail, detail
    assert "提问" in detail, detail
    # 零副作用：拒绝不落任何新事件。
    assert client.get(f"/api/sessions/{session_id}/events").json() == before


# ── #1284 P2-1：装配接线（settings → payload）必须被非默认档位钉住 ──────


def test_non_default_timeout_setting_reaches_the_persisted_payload(tmp_path):
    """600 秒档走真实装配链：删掉 `assembly.py` 的注入会回落工具缺省 300 而红。"""
    _app, client, session_id = _web_run(
        tmp_path, ask_user_question_timeout=600,
    )
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

    events = client.get(f"/api/sessions/{session_id}/events").json()
    request = next(e for e in events if e["type"] == USER_INPUT_REQUESTED)
    assert request["data"]["timeout_seconds"] == 600
    assert request["data"]["timeout_minutes"] == 10


# ── #1284 P2-2：改配置 + 重启后，旧请求仍按落盘快照到期 ────────────────


def test_timeout_message_uses_request_snapshot_after_config_change(tmp_path):
    """旧请求在 60s 档下创建；重启成 600s 后到期，注入消息仍是 1 分钟。

    快照是唯一事实源：卡片（`timeout_minutes`）与注入消息必须用同一个数。
    """
    _app1, client1, session_id = _web_run(
        tmp_path, ask_user_question_timeout=60,
    )
    with _start_probe():
        started = client1.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": "Please set up the storage layer.",
                "budget": {"run": {"max_agent_turns_total": 8, "max_model_requests": 8}},
            },
        )
        assert started.status_code == 200, started.text

    before = client1.get(f"/api/sessions/{session_id}/events").json()
    request = next(e for e in before if e["type"] == USER_INPUT_REQUESTED)
    pause = next(e for e in before if e["type"] == RUN_PAUSED)
    request_id = request["data"]["request_id"]
    assert request["data"]["timeout_seconds"] == 60
    assert request["data"]["timeout_minutes"] == 1

    # 运维改配置并重启：同一 workspace 起一个新 app（600s），旧请求不受影响。
    _app2, client2 = _web(tmp_path, ask_user_question_timeout=600)
    probe = _ModelProbe([[AIMessage(content="Continued after timeout.")]])
    with probe:
        resumed = client2.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": pause["run_id"],
                "resume_basis": "user_input",
                "budget": {"expected_version": pause["data"]["budget_version"], "run": {}},
                "input_request": {"request_id": request_id, "timeout": True},
            },
        )
    assert resumed.status_code == 200, resumed.text

    events = client2.get(f"/api/sessions/{session_id}/events").json()
    timeout_message = next(
        e for e in events
        if e["type"] == USER_MESSAGE
        and e["data"].get("input_request_id") == request_id
    )
    assert timeout_message["data"]["content"] == (
        "用户在 1 分钟内未回答，可能不在，请按你的判断继续"
    )
