from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from agent_harness.session.derive import (
    build_protected_fact_data,
    derive_protected_facts,
)
from agent_harness.session.event import (
    MESSAGE_QUEUED,
    MODEL_REQUEST,
    QUEUE_CONSUMED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    TASK_PROTECTED_FACT,
    TOOL_RESULT,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
)
from agent_harness.session.session import Session
from agent_harness.web.app import session_service
from tests.scripted_model import ScriptedModel
from tests.web.test_budget_local_fuse_api import _web


class _ModelProbe:
    def __init__(self, scripts: list[list[AIMessage]]) -> None:
        self.scripts = list(scripts)
        self.models: list[ScriptedModel] = []
        self._patcher: Any = None

    def __enter__(self):
        def factory(config: Any, **kwargs: Any) -> ScriptedModel:
            model = ScriptedModel(responses=self.scripts.pop(0))
            self.models.append(model)
            return model

        self._patcher = patch("agent_harness.assembly.create_chat_model", side_effect=factory)
        self._patcher.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self._patcher.stop()
        return False


@pytest.mark.parametrize(
    ("choice", "custom_text", "expected_value"),
    [
        ("replace_persistently", None, "For this task, use TypeScript."),
        ("current_task_only", None, "Use Python."),
        ("keep_existing", None, "Use Python."),
        ("custom", "Keep the current rule for later tasks.", "Use Python."),
    ],
)
def test_constraint_answer_resumes_same_run_and_only_persistent_choice_replaces_fact(
    tmp_path, choice: str, custom_text: str | None, expected_value: str,
):
    _, client = _web(tmp_path)
    state = client.app.state.agent
    session_id = str(uuid4())
    session = Session.start(state.store, session_id=session_id)
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    old_fact = build_protected_fact_data(
        session.events,
        session_id=session_id,
        fact_type="constraint",
        value="Use Python.",
        source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, old_fact)
    candidate = "For this task, use TypeScript."
    probe = _ModelProbe([
        [AIMessage(content="", tool_calls=[{
            "id": "ask-constraint",
            "name": "request_constraint_resolution",
            "args": {"fact_id": old_fact["fact_id"], "candidate": candidate},
        }])],
        [AIMessage(content="Continued with the selected resolution.")],
    ])

    with probe:
        started = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": candidate,
                "budget": {
                    "run": {
                        "max_agent_turns_total": 8,
                        "max_model_requests": 8,
                    },
                    "session": {
                        "max_agent_turns_total": 12,
                        "max_model_requests": 12,
                    },
                },
            },
        )
        assert started.status_code == 200, started.text
        events_before_resume = client.get(f"/api/sessions/{session_id}/events").json()
        request = next(event for event in events_before_resume if event["type"] == USER_INPUT_REQUESTED)
        pause = next(event for event in events_before_resume if event["type"] == RUN_PAUSED)
        assert pause["data"]["reason"] == "user_input"
        assert pause["data"]["input_request_id"] == request["data"]["request_id"]
        assert len(probe.models) == 1
        assert len(probe.models[0].snapshots) == 1

        blocked_message = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "Start another task before I answer."},
        )
        assert blocked_message.status_code == 409, blocked_message.text
        assert client.get(f"/api/sessions/{session_id}/events").json() == events_before_resume

        Session.append_event(
            state.store, session_id, MESSAGE_QUEUED,
            {"queue_id": "queued-before-answer", "content": "Queued input"},
        )
        queued_events = client.get(f"/api/sessions/{session_id}/events").json()
        flushed = client.post(f"/api/sessions/{session_id}/queue/flush")
        assert flushed.status_code == 409, flushed.text
        assert client.get(f"/api/sessions/{session_id}/events").json() == queued_events
        assert not any(event["type"] == QUEUE_CONSUMED for event in queued_events)

        answer: dict[str, Any] = {"request_id": request["data"]["request_id"], "choice": choice}
        if custom_text is not None:
            answer["custom_text"] = custom_text
        stale_answer = {**answer, "request_id": "stale-request"}
        stale = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": pause["run_id"],
                "resume_basis": "user_input",
                "budget": {"expected_version": pause["data"]["budget_version"], "run": {}},
                "input_request": stale_answer,
            },
        )
        assert stale.status_code == 409, stale.text
        assert client.get(f"/api/sessions/{session_id}/events").json() == queued_events
        assert len(probe.models) == 1
        resumed = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": pause["run_id"],
                "resume_basis": "user_input",
                "budget": {"expected_version": pause["data"]["budget_version"], "run": {}},
                "input_request": answer,
            },
        )
        assert resumed.status_code == 200, resumed.text
        assert "run/completed" in resumed.text

    events = client.get(f"/api/sessions/{session_id}/events").json()
    budget = asyncio.run(
        state.stores.delegation_tree_ledger.get_session_budget(session_id)
    )
    assert budget is not None
    assert budget.consumed.tool_calls_by_tool == {
        "request_constraint_resolution": 1,
    }
    resumed_event = next(event for event in events if event["type"] == RUN_RESUMED)
    answer_event = next(
        event for event in events
        if event["type"] == USER_MESSAGE
        and event["data"].get("input_request_id") == request["data"]["request_id"]
    )
    assert resumed_event["run_id"] == pause["run_id"]
    assert resumed_event["data"]["resume_basis"] == "user_input"
    assert resumed_event["data"]["previous_budget_version"] == pause["data"]["budget_version"]
    assert resumed_event["data"]["budget_version"] == pause["data"]["budget_version"] + 1
    assert resumed_event["data"]["limits"]["run"] == pause["data"]["limits"]["run"]
    assert resumed_event["data"]["limits"]["local"] == pause["data"]["limits"]["local"]
    assert resumed_event["data"]["consumed"] == pause["data"]["consumed"]
    assert len([event for event in events if event["type"] == RUN_STARTED]) == 1
    assert len([event for event in events if event["type"] == RUN_PAUSED]) == 1
    assert len([event for event in events if event["type"] == RUN_COMPLETED]) == 1
    assert answer_event["data"]["input_request_answer"] == answer
    assert budget.limits.as_projection() == pause["data"]["limits"]["session"]
    assert budget.consumed.agent_turns == 2
    assert budget.consumed.model_requests == 2
    assert budget.consumed.tool_calls_by_tool == {
        "request_constraint_resolution": 1,
    }
    resumed_messages = "\n".join(
        str(message.content)
        for message in probe.models[1].snapshots[0].messages
    )
    expected_answer_text = {
        "replace_persistently": "用户确认永久替换旧约束。新约束原文：",
        "current_task_only": "用户选择仅在当前任务采用此约束：",
        "keep_existing": "用户选择保留旧约束并忽略本次冲突要求：",
        "custom": custom_text,
    }[choice]
    assert request["data"]["question"] in resumed_messages
    assert expected_answer_text in resumed_messages

    facts = derive_protected_facts(state.store.read_events(session_id))
    active = [fact.value for fact in facts if fact.status == "active" and fact.type == "constraint"]
    assert active == [expected_value]
    if choice == "replace_persistently":
        new_facts = [
            fact for fact in facts
            if fact.type == "constraint" and fact.fact_id != old_fact["fact_id"]
        ]
        assert len(new_facts) == 1
        assert new_facts[0].value == candidate
        assert new_facts[0].status == "active"
        old = next(fact for fact in facts if fact.fact_id == old_fact["fact_id"])
        assert old.superseded_by_fact_id == new_facts[0].fact_id
        assert answer_event["data"]["protected_facts"]
        assert answer_event["data"]["protected_facts"][0]["supersedes_fact_id"] == old_fact["fact_id"]
    else:
        assert not answer_event["data"].get("protected_facts")


def test_terminal_run_does_not_block_new_task_for_unanswered_request(tmp_path):
    app, client = _web(tmp_path)
    session_id = str(uuid4())
    session = Session.start(app.state.agent.store, session_id=session_id)
    run_id, _ = session.begin_run()
    session.append(
        USER_INPUT_REQUESTED,
        {"request_id": "terminal-request", "kind": "protected_fact_conflict"},
        run_id=run_id,
    )
    session.append(RUN_FAILED, {"reason": "cancelled"}, run_id=run_id)
    probe = _ModelProbe([[AIMessage(content="Started a new task.")]])

    with probe:
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "Start a new task after the previous run failed."},
        )

    assert response.status_code == 200, response.text
    events = app.state.agent.store.read_events(session_id)
    assert sum(event.type == USER_INPUT_REQUESTED for event in events) == 1
    assert sum(event.type == RUN_STARTED for event in events) == 2
    assert sum(event.type == RUN_COMPLETED for event in events) == 1


@pytest.mark.parametrize("deadline_scope", ["run", "session"])
def test_constraint_question_is_rejected_when_absolute_deadline_is_configured(
    tmp_path, deadline_scope: str,
):
    app, client = _web(tmp_path)
    session_id = str(uuid4())
    session = Session.start(app.state.agent.store, session_id=session_id)
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    old_fact = build_protected_fact_data(
        session.events,
        session_id=session_id,
        fact_type="constraint",
        value="Use Python.",
        source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, old_fact)
    candidate = "For this task, use TypeScript."
    scripts = [
        [AIMessage(content="", tool_calls=[{
            "id": "ask-with-deadline",
            "name": "request_constraint_resolution",
            "args": {"fact_id": old_fact["fact_id"], "candidate": candidate},
        }])],
        [AIMessage(content="Completed without waiting for user input.")],
    ]
    probe = _ModelProbe(scripts)

    budget = {
        "run": {"deadline_at": "2099-01-01T00:00:00Z"}
        if deadline_scope == "run" else {},
        "session": {"deadline_at": "2099-01-01T00:00:00Z"}
        if deadline_scope == "session" else {},
    }
    with probe:
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": candidate, "budget": budget},
        )
        assert response.status_code == 200, response.text

    events = client.get(f"/api/sessions/{session_id}/events").json()
    assert not any(event["type"] == USER_INPUT_REQUESTED for event in events)
    assert len(probe.models) == 1
    assert len(probe.models[0].snapshots) == 2
    tool_result = next(
        event for event in events
        if event["type"] == TOOL_RESULT
        and event["data"].get("tool_call_id") == "ask-with-deadline"
    )
    result = json.loads(tool_result["data"]["content"])
    assert result["data"]["status"] == "rejected"
    assert result["data"]["reason_code"] == "DEADLINE_CONFIGURED"


@pytest.mark.parametrize("budget_scope", ["run", "session"])
def test_constraint_question_is_rejected_without_same_run_budget_headroom(
    tmp_path, budget_scope: str,
):
    app, client = _web(tmp_path)
    session_id = str(uuid4())
    session = Session.start(app.state.agent.store, session_id=session_id)
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    old_fact = build_protected_fact_data(
        session.events,
        session_id=session_id,
        fact_type="constraint",
        value="Use Python.",
        source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, old_fact)
    candidate = "For this task, use TypeScript."
    probe = _ModelProbe([[
        AIMessage(content="", tool_calls=[{
            "id": "ask-with-tight-budget",
            "name": "request_constraint_resolution",
            "args": {"fact_id": old_fact["fact_id"], "candidate": candidate},
        }]),
    ]])

    budget = {
        "run": {"max_model_requests": 2} if budget_scope == "run" else {},
        "session": {"max_model_requests": 2} if budget_scope == "session" else {},
    }
    with probe:
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": candidate, "budget": budget},
        )
        assert response.status_code == 200, response.text

    events = client.get(f"/api/sessions/{session_id}/events").json()
    assert not any(event["type"] == USER_INPUT_REQUESTED for event in events)
    tool_result = next(
        event for event in events
        if event["type"] == TOOL_RESULT
        and event["data"].get("tool_call_id") == "ask-with-tight-budget"
    )
    result = json.loads(tool_result["data"]["content"])
    assert result["data"]["status"] == "rejected"
    assert result["data"]["reason_code"] == "RESUME_UNAVAILABLE"


def test_failed_constraint_budget_replay_blocks_session_run_admission(tmp_path, monkeypatch):
    app, client = _web(tmp_path)
    session_id = str(uuid4())
    session = Session.start(app.state.agent.store, session_id=session_id)
    run_id, _ = session.begin_run()
    session.append(USER_MESSAGE, {"content": "A prior task."}, run_id=run_id)
    session.append(
        TOOL_RESULT,
        {
            "tool_call_id": "unaccounted-constraint-call",
            "content": "{}",
            "budget_delta": {
                "tool_name": "request_constraint_resolution",
                "tool_calls": 1,
                "tool_attempts": 1,
            },
        },
        run_id=run_id,
    )
    session.append(RUN_FAILED, {"reason": "session budget write failed"}, run_id=run_id)

    async def fail_budget_replay(*_args, **_kwargs):
        raise RuntimeError("budget ledger unavailable")

    monkeypatch.setattr(
        app.state.agent.delegation_tree_ledger,
        "record_session_tool_call",
        fail_budget_replay,
    )
    service = session_service(app.state.agent)
    results = asyncio.run(service.scan_interrupted())
    assert len(results) == 1
    assert results[0].budget_recovery_failed is True

    probe = _ModelProbe([[AIMessage(content="This must not launch.")]])
    with probe:
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "Start another task."},
        )
    assert response.status_code == 409, response.text
    assert probe.models == []
    events = app.state.agent.store.read_events(session_id)
    assert sum(event.type == USER_MESSAGE for event in events) == 1
    assert sum(event.type == RUN_STARTED for event in events) == 1


def test_constraint_question_survives_session_budget_write_failure(tmp_path, monkeypatch):
    app, client = _web(tmp_path)
    state = app.state.agent
    session_id = str(uuid4())
    session = Session.start(state.store, session_id=session_id)
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    old_fact = build_protected_fact_data(
        session.events,
        session_id=session_id,
        fact_type="constraint",
        value="Use Python.",
        source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, old_fact)
    candidate = "For this task, use TypeScript."
    probe = _ModelProbe([
        [AIMessage(content="", tool_calls=[{
            "id": "ask-budget-write-failure",
            "name": "request_constraint_resolution",
            "args": {"fact_id": old_fact["fact_id"], "candidate": candidate},
        }])],
        [AIMessage(content="Continued after budget recovery.")],
    ])
    ledger = state.delegation_tree_ledger
    original_record = ledger.record_session_tool_call
    failed_once = False

    async def fail_once(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("temporary session budget write failure")
        return await original_record(*args, **kwargs)

    monkeypatch.setattr(ledger, "record_session_tool_call", fail_once)
    with probe:
        started = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": candidate},
        )
        assert started.status_code == 200, started.text

        events_before_answer = client.get(
            f"/api/sessions/{session_id}/events"
        ).json()
        request = next(
            event for event in events_before_answer
            if event["type"] == USER_INPUT_REQUESTED
        )
        pause = next(
            event for event in events_before_answer
            if event["type"] == RUN_PAUSED
        )
        assert not any(event["type"] == RUN_FAILED for event in events_before_answer)
        assert session_id in state.budget_recovery_failed_sessions

        resumed = client.post(
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
        assert resumed.status_code == 200, resumed.text
        assert "run/completed" in resumed.text

    assert failed_once
    assert session_id not in state.budget_recovery_failed_sessions
    final_events = state.store.read_events(session_id)
    assert sum(event.type == USER_INPUT_REQUESTED for event in final_events) == 1
    assert sum(
        event.type == USER_MESSAGE
        and event.data.get("input_request_id") == request["data"]["request_id"]
        for event in final_events
    ) == 1
    assert sum(event.type == RUN_PAUSED for event in final_events) == 1
    assert sum(event.type == RUN_RESUMED for event in final_events) == 1
    assert sum(event.type == RUN_COMPLETED for event in final_events) == 1
    budget = asyncio.run(ledger.get_session_budget(session_id))
    assert budget is not None
    assert budget.consumed.tool_calls_by_tool == {"request_constraint_resolution": 1}
    assert budget.consumed.tool_attempts_by_tool == {"request_constraint_resolution": 1}


def test_constraint_question_survives_operation_ledger_terminal_write_failure(
    tmp_path, monkeypatch,
):
    app, client = _web(tmp_path)
    state = app.state.agent
    session_id = str(uuid4())
    session = Session.start(state.store, session_id=session_id)
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    old_fact = build_protected_fact_data(
        session.events,
        session_id=session_id,
        fact_type="constraint",
        value="Use Python.",
        source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, old_fact)
    candidate = "For this task, use TypeScript."
    call_id = "ask-ledger-write-failure"
    probe = _ModelProbe([
        [AIMessage(content="", tool_calls=[{
            "id": call_id,
            "name": "request_constraint_resolution",
            "args": {"fact_id": old_fact["fact_id"], "candidate": candidate},
        }])],
        [AIMessage(content="Continued after operation recovery.")],
    ])
    ledger = state.operation_ledger
    original_update = ledger.update_state
    failed_once = False

    async def fail_terminal_once(session_id_arg, tool_call_id, new_state, **kwargs):
        nonlocal failed_once
        if (
            tool_call_id == call_id
            and new_state.value == "SUCCEEDED"
            and not failed_once
        ):
            failed_once = True
            raise RuntimeError("temporary operation ledger failure")
        return await original_update(
            session_id_arg, tool_call_id, new_state, **kwargs,
        )

    monkeypatch.setattr(ledger, "update_state", fail_terminal_once)
    with probe:
        started = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": candidate},
        )
        assert started.status_code == 200, started.text

        events_before_answer = client.get(
            f"/api/sessions/{session_id}/events"
        ).json()
        request = next(
            event for event in events_before_answer
            if event["type"] == USER_INPUT_REQUESTED
        )
        pause = next(
            event for event in events_before_answer
            if event["type"] == RUN_PAUSED
        )
        assert not any(event["type"] == RUN_FAILED for event in events_before_answer)
        operation = asyncio.run(ledger.get(session_id, call_id))
        assert operation is not None and operation.state.value == "RUNNING"

        resumed = client.post(
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
        assert resumed.status_code == 200, resumed.text
        assert "run/completed" in resumed.text

    assert failed_once
    final_events = state.store.read_events(session_id)
    assert sum(event.type == USER_INPUT_REQUESTED for event in final_events) == 1
    assert sum(
        event.type == USER_MESSAGE
        and event.data.get("input_request_id") == request["data"]["request_id"]
        for event in final_events
    ) == 1
    assert sum(
        event.type == TOOL_RESULT
        and event.data.get("tool_call_id") == call_id
        for event in final_events
    ) == 1
    assert sum(event.type == RUN_PAUSED for event in final_events) == 1
    assert sum(event.type == RUN_RESUMED for event in final_events) == 1
    assert sum(event.type == RUN_COMPLETED for event in final_events) == 1
    recovered_operation = asyncio.run(ledger.get(session_id, call_id))
    assert recovered_operation is not None
    assert recovered_operation.state.value == "SUCCEEDED"


def test_input_answer_rechecks_shared_session_budget_before_same_run_resume(tmp_path):
    app, client = _web(tmp_path)
    session_id = str(uuid4())
    session = Session.start(app.state.agent.store, session_id=session_id)
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    old_fact = build_protected_fact_data(
        session.events,
        session_id=session_id,
        fact_type="constraint",
        value="Use Python.",
        source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, old_fact)
    candidate = "For this task, use TypeScript."
    probe = _ModelProbe([[
        AIMessage(content="", tool_calls=[{
            "id": "ask-before-shared-budget-use",
            "name": "request_constraint_resolution",
            "args": {"fact_id": old_fact["fact_id"], "candidate": candidate},
        }]),
    ]])

    with probe:
        started = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": candidate,
                "budget": {"session": {"max_model_requests": 3}},
            },
        )
        assert started.status_code == 200, started.text
        before_answer = client.get(f"/api/sessions/{session_id}/events").json()
        request = next(
            event for event in before_answer if event["type"] == USER_INPUT_REQUESTED
        )
        pause = next(event for event in before_answer if event["type"] == RUN_PAUSED)

        async def spend_sibling_session_headroom():
            await app.state.agent.delegation_tree_ledger.record_session_model_requests(
                session_id,
                count=1,
                usage=None,
                cost=None,
            )

        asyncio.run(spend_sibling_session_headroom())
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

    assert rejected.status_code == 409, rejected.text
    assert len(probe.models) == 1
    after_answer = client.get(f"/api/sessions/{session_id}/events").json()
    assert after_answer == before_answer


@pytest.mark.parametrize("interleave", ["recovery", "runtime_builder"])
def test_input_answer_rechecks_budget_after_recovery_and_runtime_awaits(
    tmp_path, monkeypatch, interleave,
):
    from agent_harness.session.event import SESSION_RESUMED
    from agent_harness.session.service import SessionService

    app, client = _web(tmp_path)
    session_id = str(uuid4())
    session = Session.start(app.state.agent.store, session_id=session_id)
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    old_fact = build_protected_fact_data(
        session.events,
        session_id=session_id,
        fact_type="constraint",
        value="Use Python.",
        source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, old_fact)
    candidate = "For this task, use TypeScript."
    probe = _ModelProbe([
        [AIMessage(content="", tool_calls=[{
            "id": f"ask-before-{interleave}",
            "name": "request_constraint_resolution",
            "args": {"fact_id": old_fact["fact_id"], "candidate": candidate},
        }])],
        [AIMessage(content="This response must not run.")],
    ])

    with probe:
        started = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": candidate,
                "budget": {"session": {"max_model_requests": 3}},
            },
        )
        assert started.status_code == 200, started.text
        before_answer = client.get(f"/api/sessions/{session_id}/events").json()
        request = next(
            event for event in before_answer if event["type"] == USER_INPUT_REQUESTED
        )
        pause = next(event for event in before_answer if event["type"] == RUN_PAUSED)

        async def spend_sibling_session_headroom():
            await app.state.agent.delegation_tree_ledger.record_session_model_requests(
                session_id,
                count=1,
                usage=None,
                cost=None,
            )

        if interleave == "recovery":
            original_recover = SessionService.recover

            async def recover_then_spend(
                service, sid, decisions=None, *, defer_session_resumed=False,
            ):
                result = await original_recover(
                    service, sid, decisions,
                    defer_session_resumed=defer_session_resumed,
                )
                await spend_sibling_session_headroom()
                return result

            async def needs_recovery(_service, _sid):
                return True

            monkeypatch.setattr(SessionService, "recover", recover_then_spend)
            monkeypatch.setattr(
                SessionService, "_has_unreconciled_operations", needs_recovery,
            )
        else:
            import agent_harness.session.service as service_module

            original_build_runtime = service_module.build_runtime

            async def build_runtime_then_spend(**kwargs):
                runtime = await original_build_runtime(**kwargs)
                await spend_sibling_session_headroom()
                return runtime

            monkeypatch.setattr(
                service_module, "build_runtime", build_runtime_then_spend,
            )

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

    assert rejected.status_code == 409, rejected.text
    after_answer = client.get(f"/api/sessions/{session_id}/events").json()
    assert not any(
        event["type"] == USER_MESSAGE
        and event["data"].get("input_request_id") == request["data"]["request_id"]
        for event in after_answer
    )
    assert sum(event["type"] == RUN_RESUMED for event in after_answer) == sum(
        event["type"] == RUN_RESUMED for event in before_answer
    )
    assert sum(event["type"] == SESSION_RESUMED for event in after_answer) == sum(
        event["type"] == SESSION_RESUMED for event in before_answer
    )
    assert sum(event["type"] == MODEL_REQUEST for event in after_answer) == sum(
        event["type"] == MODEL_REQUEST for event in before_answer
    )


def test_new_task_rechecks_pending_request_inside_resume_lock(tmp_path, monkeypatch):
    app, client = _web(tmp_path)
    session_id = str(uuid4())
    Session.start(app.state.agent.store, session_id=session_id)
    run_id = "concurrent-question-run"
    manager = app.state.agent.run_manager
    underlying_lock = manager.session_lock(session_id)
    injected = False

    class InjectingLock:
        async def __aenter__(self):
            nonlocal injected
            if not injected:
                injected = True
                Session.append_event(
                    app.state.agent.store, session_id, RUN_STARTED,
                    {"turn_index": 1, "agent_profile": "main"}, run_id=run_id,
                )
                Session.append_event(
                    app.state.agent.store, session_id, USER_INPUT_REQUESTED,
                    {"request_id": "concurrent-request", "kind": "protected_fact_conflict"},
                    run_id=run_id,
                )
                Session.append_event(
                    app.state.agent.store, session_id, RUN_PAUSED,
                    {
                        "reason": "user_input",
                        "input_request_id": "concurrent-request",
                        "budget_version": 0,
                        "version": 0,
                    },
                    run_id=run_id,
                )
            await underlying_lock.acquire()
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            underlying_lock.release()

    monkeypatch.setattr(manager, "session_lock", lambda _session_id: InjectingLock())
    probe = _ModelProbe([[AIMessage(content="This run must not start.")]])
    with probe:
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "A new task racing with a pending question."},
        )

    assert response.status_code == 409, response.text
    assert probe.models == []
    events = app.state.agent.store.read_events(session_id)
    assert sum(event.type == USER_INPUT_REQUESTED for event in events) == 1
    assert sum(event.type == RUN_STARTED for event in events) == 1
    assert sum(event.type == USER_MESSAGE for event in events) == 0
