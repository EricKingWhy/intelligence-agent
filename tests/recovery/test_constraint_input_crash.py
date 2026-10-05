from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import partial
from pathlib import Path
from unittest.mock import patch

import anyio.to_thread
import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent.run_budget import (
    REASON_USER_INPUT,
    SessionLimits,
)
from agent_harness.recovery.scan import ScanRecovery, scan_interrupted_sessions
from agent_harness.session.event import (
    MODEL_REQUEST,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
)
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import Operation, SqliteOperationLedger
from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger
from agent_harness.storage.operation import OperationState
from agent_harness.tooling import ToolResult
from tests.scripted_model import ScriptedModel
from tests.web.test_budget_local_fuse_api import _web

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD = Path(__file__).with_name("_constraint_input_crash_child.py")


@pytest.mark.asyncio
async def test_startup_replays_constraint_tool_budget_for_terminal_sessions(tmp_path):
    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store, session_id="terminal-budget-replay")
    run_id, _ = session.begin_run()
    session.append(USER_MESSAGE, {"content": "Continue."}, run_id=run_id)
    session.append(
        TOOL_CALL,
        {
            "tool_call_id": "constraint-call",
            "tool_name": "request_constraint_resolution",
            "args": {},
        },
        run_id=run_id,
        step_id=1,
    )
    session.append(
        TOOL_RESULT,
        {
            "tool_call_id": "constraint-call",
            "content": "{}",
            "budget_delta": {
                "tool_name": "request_constraint_resolution",
                "tool_calls": 1,
                "tool_attempts": 1,
            },
        },
        run_id=run_id,
        step_id=1,
    )
    session.append(RUN_FAILED, {"reason": "accounting failed"}, run_id=run_id)

    operation_ledger = SqliteOperationLedger(tmp_path / "harness.db")
    await operation_ledger.initialize()
    budget_ledger = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    await budget_ledger.initialize()
    await budget_ledger.ensure_session_budget(
        session.session_id,
        root_session_id=session.session_id,
        limits=SessionLimits(max_model_requests=10),
    )

    results = await scan_interrupted_sessions(
        session_store=store,
        operation_ledger=operation_ledger,
        workspace_registry=None,
        database_path=tmp_path / "harness.db",
        session_budget_reader=budget_ledger.get_session_budget,
        session_tool_result_recorder=budget_ledger.record_session_tool_call,
    )

    assert results == []
    budget = await budget_ledger.get_session_budget(session.session_id)
    assert budget is not None
    assert budget.consumed.tool_calls_by_tool == {"request_constraint_resolution": 1}
    assert budget.consumed.tool_attempts_by_tool == {"request_constraint_resolution": 1}


@pytest.mark.parametrize(
    "result_data",
    [
        {"content": "{}", "budget_delta": {
            "tool_name": "request_constraint_resolution",
            "tool_calls": 1,
            "tool_attempts": 1,
        }},
        {"content": "{}"},
        {"tool_call_id": "malformed-result", "content": "{}"},
        {"tool_call_id": "unpaired-result", "content": "{}"},
        {"tool_call_id": "malformed-result", "content": "{}", "budget_delta": {
            "tool_name": "request_constraint_resolution",
            "tool_calls": 1,
            "tool_attempts": True,
        }},
        {"tool_call_id": "different-valid-id", "content": "{}", "budget_delta": {
            "tool_name": "request_constraint_resolution",
            "tool_calls": 1,
            "tool_attempts": 1,
        }},
    ],
)
@pytest.mark.asyncio
async def test_malformed_constraint_tool_result_fails_budget_recovery_closed(
    tmp_path, result_data,
):
    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store, session_id="malformed-constraint-budget")
    run_id, _ = session.begin_run()
    session.append(
        TOOL_CALL,
        {
            "tool_call_id": "malformed-result",
            "tool_name": "request_constraint_resolution",
            "args": {},
        },
        run_id=run_id,
    )
    session.append(TOOL_RESULT, result_data, run_id=run_id)

    operation_ledger = SqliteOperationLedger(tmp_path / "harness.db")
    await operation_ledger.initialize()
    budget_ledger = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    await budget_ledger.initialize()
    await budget_ledger.ensure_session_budget(
        session.session_id,
        root_session_id=session.session_id,
        limits=SessionLimits(max_model_requests=10),
    )

    results = await scan_interrupted_sessions(
        session_store=store,
        operation_ledger=operation_ledger,
        workspace_registry=None,
        database_path=tmp_path / "harness.db",
        session_budget_reader=budget_ledger.get_session_budget,
        session_tool_result_recorder=budget_ledger.record_session_tool_call,
    )

    assert len(results) == 1
    assert results[0].session_id == session.session_id
    assert results[0].recovery is ScanRecovery.FAILED
    assert results[0].budget_recovery_failed is True
    budget = await budget_ledger.get_session_budget(session.session_id)
    assert budget is not None
    assert budget.consumed.tool_calls_by_tool == {}
    assert budget.consumed.tool_attempts_by_tool == {}


@pytest.mark.parametrize("fail_budget_replay", [False, True])
@pytest.mark.asyncio
async def test_recovery_replays_budget_for_synthesized_constraint_result(
    tmp_path, fail_budget_replay: bool,
):
    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store, session_id="recovered-constraint-result")
    run_id, _ = session.begin_run()
    call_id = "rejected-constraint-call"
    session.append(
        TOOL_CALL,
        {
            "tool_call_id": call_id,
            "tool_name": "request_constraint_resolution",
            "args": {"fact_id": "missing-fact", "candidate": "Use TypeScript."},
        },
        run_id=run_id,
        step_id=1,
    )

    operation_ledger = SqliteOperationLedger(tmp_path / "harness.db")
    await operation_ledger.initialize()
    await operation_ledger.create(Operation(
        tool_call_id=call_id,
        session_id=session.session_id,
        run_id=run_id,
        agent_id="default",
        tool_name="request_constraint_resolution",
        args_identity='{"fact_id":"missing-fact","candidate":"Use TypeScript."}',
        state=OperationState.PENDING,
        started_at="2026-10-05T00:00:00+00:00",
    ))
    await operation_ledger.update_state(
        session.session_id, call_id, OperationState.RUNNING,
    )
    rejected = ToolResult.success(
        message="No active fact.",
        data={"status": "rejected", "reason_code": "FACT_NOT_ACTIVE"},
        metadata={"attempt": 1},
    )
    await operation_ledger.update_state(
        session.session_id,
        call_id,
        OperationState.SUCCEEDED,
        result_json=rejected.model_dump_json(),
    )

    budget_ledger = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    await budget_ledger.initialize()
    await budget_ledger.ensure_session_budget(
        session.session_id,
        root_session_id=session.session_id,
        limits=SessionLimits(tool_call_limits={"request_constraint_resolution": 2}),
    )

    async def record_constraint_result(*args, **kwargs):
        if fail_budget_replay:
            raise RuntimeError("budget ledger unavailable during replay")
        return await budget_ledger.record_session_tool_call(*args, **kwargs)

    results = await scan_interrupted_sessions(
        session_store=store,
        operation_ledger=operation_ledger,
        workspace_registry=None,
        database_path=tmp_path / "harness.db",
        session_budget_reader=budget_ledger.get_session_budget,
        session_tool_result_recorder=record_constraint_result,
    )

    assert len(results) == 1
    assert results[0].session_id == session.session_id
    events = store.read_events(session.session_id)
    recovered_result = next(
        event for event in events
        if event.type == TOOL_RESULT and event.data.get("tool_call_id") == call_id
    )
    assert recovered_result.data["budget_delta"] == {
        "tool_name": "request_constraint_resolution",
        "tool_calls": 1,
        "tool_attempts": 1,
    }
    budget = await budget_ledger.get_session_budget(session.session_id)
    assert budget is not None
    if fail_budget_replay:
        assert results[0].recovery is ScanRecovery.FAILED
        assert results[0].budget_recovery_failed is True
        assert budget.consumed.tool_calls_by_tool == {}
    else:
        assert results[0].recovery is ScanRecovery.RECOVERED
        assert results[0].budget_recovery_failed is False
        assert budget.consumed.tool_calls_by_tool == {"request_constraint_resolution": 1}
        assert budget.consumed.tool_attempts_by_tool == {
            "request_constraint_resolution": 1,
        }


@pytest.mark.asyncio
async def test_restart_materializes_user_input_pause_without_repeating_work(tmp_path):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT / "src"), str(_REPO_ROOT), env.get("PYTHONPATH", "")]
    )
    completed = await anyio.to_thread.run_sync(
        partial(
            subprocess.run,
            [sys.executable, str(_CHILD), json.dumps({"root": str(tmp_path)})],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    )
    assert completed.returncode == 9, completed.stderr
    assert completed.stdout.strip().endswith("AFTER_INPUT_REQUEST"), completed.stdout

    store = JsonlSessionStore(root=tmp_path / "sessions")
    session_id = "constraint-input-crash"
    before = store.read_events(session_id)
    requests = [event for event in before if event.type == USER_INPUT_REQUESTED]
    assert len(requests) == 1
    assert requests[0].data["tool_call_id"] == "ask-constraint-crash"
    assert not any(event.type == RUN_PAUSED for event in before)

    ledger = SqliteOperationLedger(tmp_path / "harness.db")
    await ledger.initialize()
    budget_ledger = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    await budget_ledger.initialize()
    await budget_ledger.ensure_session_budget(
        session_id,
        root_session_id=session_id,
        limits=SessionLimits(
            max_model_requests=12,
            tool_call_limits={"request_constraint_resolution": 2},
        ),
    )
    interrupted_operation = await ledger.get(session_id, "ask-constraint-crash")
    assert interrupted_operation is not None
    assert interrupted_operation.state.value == "RUNNING"

    seen_budget_keys = []

    async def read_session_budget(budget_key: str):
        seen_budget_keys.append(budget_key)
        return await budget_ledger.get_session_budget(budget_key)

    results = await scan_interrupted_sessions(
        session_store=store,
        operation_ledger=ledger,
        workspace_registry=None,
        database_path=tmp_path / "harness.db",
        session_budget_reader=read_session_budget,
        session_tool_result_recorder=budget_ledger.record_session_tool_call,
    )

    after = store.read_events(session_id)
    budget_snapshot = await budget_ledger.get_session_budget(session_id)
    assert budget_snapshot is not None
    pauses = [event for event in after if event.type == RUN_PAUSED]
    assert [item.recovery for item in results] == [ScanRecovery.RECOVERED]
    assert len(seen_budget_keys) == 1 and seen_budget_keys[0]
    assert len(pauses) == 1
    assert pauses[0].data["reason"] == REASON_USER_INPUT
    assert pauses[0].data["input_request_id"] == requests[0].data["request_id"]
    assert pauses[0].data["limits"]["session"] == budget_snapshot.limits.as_projection()
    assert pauses[0].data["session"] == {
        "version": budget_snapshot.version,
        "consumed": budget_snapshot.consumed.as_projection(),
    }
    assert budget_snapshot.consumed.tool_calls_by_tool == {
        "request_constraint_resolution": 1,
    }
    assert budget_snapshot.consumed.tool_attempts_by_tool == {
        "request_constraint_resolution": 1,
    }
    assert not any(event.type == RUN_INTERRUPTED for event in after)
    assert sum(event.type == USER_INPUT_REQUESTED for event in after) == 1
    assert sum(event.type == MODEL_REQUEST for event in after) == 1
    assert sum(event.type == TOOL_CALL for event in after) == 1
    assert sum(event.type == TOOL_RESULT for event in after) == 1
    result_event = next(event for event in after if event.type == TOOL_RESULT)
    assert result_event.data["budget_delta"] == {
        "tool_name": "request_constraint_resolution",
        "tool_calls": 1,
        "tool_attempts": 1,
    }
    recovered_result = await ledger.get(session_id, "ask-constraint-crash")
    assert recovered_result is not None
    assert recovered_result.state.value == "SUCCEEDED"
    assert recovered_result.result_json is not None

    second_scan = await scan_interrupted_sessions(
        session_store=store,
        operation_ledger=ledger,
        workspace_registry=None,
        database_path=tmp_path / "harness.db",
        session_budget_reader=read_session_budget,
        session_tool_result_recorder=budget_ledger.record_session_tool_call,
    )
    assert second_scan == []
    after_second_scan = store.read_events(session_id)
    assert sum(event.type == USER_INPUT_REQUESTED for event in after_second_scan) == 1
    assert sum(event.type == RUN_PAUSED for event in after_second_scan) == 1
    assert sum(event.type == TOOL_RESULT for event in after_second_scan) == 1
    assert (await budget_ledger.get_session_budget(session_id)).consumed.tool_calls_by_tool == {
        "request_constraint_resolution": 1,
    }

    run_id = pauses[0].run_id
    request_id = requests[0].data["request_id"]
    app, client = _web(tmp_path)
    app.state.agent.workspace_registry.create(session_id)
    model = ScriptedModel([AIMessage(content="Continued after recovery.")])
    answer = {"request_id": request_id, "choice": "keep_existing"}
    with patch("agent_harness.assembly.create_chat_model", return_value=model):
        response = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": run_id,
                "resume_basis": "user_input",
                "budget": {"expected_version": pauses[0].data["budget_version"], "run": {}},
                "input_request": answer,
            },
        )
    assert response.status_code == 200, response.text
    assert "run/completed" in response.text

    final_events = app.state.agent.store.read_events(session_id)
    assert sum(event.type == USER_INPUT_REQUESTED for event in final_events) == 1
    assert sum(
        event.type == USER_MESSAGE
        and event.data.get("input_request_id") == request_id
        for event in final_events
    ) == 1
    assert sum(event.type == TOOL_CALL for event in final_events) == 1
    assert sum(event.type == TOOL_RESULT for event in final_events) == 1
    assert sum(event.type == RUN_STARTED for event in final_events) == 1
    assert sum(event.type == RUN_PAUSED for event in final_events) == 1
    assert sum(event.type == RUN_RESUMED for event in final_events) == 1
    assert sum(event.type == RUN_COMPLETED for event in final_events) == 1
    resumed = next(event for event in final_events if event.type == RUN_RESUMED)
    assert resumed.run_id == run_id
    assert final_events[-1].run_id == run_id
