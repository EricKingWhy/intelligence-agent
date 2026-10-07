"""Issue #663 registration crash windows with real subprocess termination."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import partial
from pathlib import Path

import anyio.to_thread
import pytest

from agent_harness.recovery import (
    ReconcileCallback,
    ReconcileRequired,
    RecoveryCoordinator,
)
from agent_harness.recovery.reconcile import ReconcileVerdict
from agent_harness.session import JsonlSessionStore
from agent_harness.session.derive import is_direct_user_input_event
from agent_harness.session.event import (
    OPERATION_RECONCILE_REQUIRED,
    TASK_PROTECTED_FACT,
    TOOL_RESULT,
    USER_MESSAGE,
)
from agent_harness.storage import Operation, SqliteOperationLedger
from agent_harness.tooling import ToolResult

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD = Path(__file__).with_name("_register_constraint_crash_child.py")
_CALL_ID = "register-constraint-kill"
_SESSION_ID = "register-constraint-crash"
_CANDIDATE = "For this task, do not add third-party dependencies."


async def _kill_child(tmp_path: Path, kill_stage: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT / "src"), str(_REPO_ROOT), env.get("PYTHONPATH", "")]
    )
    return await anyio.to_thread.run_sync(
        partial(
            subprocess.run,
            [
                sys.executable,
                str(_CHILD),
                json.dumps({"root": str(tmp_path), "kill_stage": kill_stage}),
            ],
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


def _coordinator(
    store: JsonlSessionStore,
    ledger: SqliteOperationLedger,
    database_path: Path,
    *,
    reconcile_callback=None,
) -> RecoveryCoordinator:
    return RecoveryCoordinator(
        session_store=store,
        workspace_registry=None,
        operation_ledger=ledger,
        reconcile_callback=reconcile_callback,
        database_path=database_path,
    )


def _registered_facts(events):
    return [event for event in events if event.type == TASK_PROTECTED_FACT]


def _tool_results(events):
    return [
        event for event in events
        if event.type == TOOL_RESULT and event.data.get("tool_call_id") == _CALL_ID
    ]


@pytest.mark.asyncio
async def test_fact_durable_before_ledger_terminal_requires_manual_reconcile(
    tmp_path: Path,
) -> None:
    completed = await _kill_child(tmp_path, "fact")
    assert completed.returncode == 9, completed.stderr
    assert completed.stdout.strip().endswith("AFTER_PROTECTED_FACT")

    store = JsonlSessionStore(tmp_path / "sessions")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    events = store.read_events(_SESSION_ID)
    operation = await ledger.get(_SESSION_ID, _CALL_ID)
    facts = _registered_facts(events)
    assert len(facts) == 1
    assert operation is not None and operation.state.value == "RUNNING"
    assert not _tool_results(events)
    fact = facts[0]
    source_id = fact.data["source_event_id"]
    source = next(event for event in events if event.event_id == source_id)
    assert source.type == USER_MESSAGE
    assert source.data["content"] == _CANDIDATE
    assert fact.data["source_event_seq"] == source.seq
    assert is_direct_user_input_event(events, source_id)

    coordinator = _coordinator(store, ledger, tmp_path / "state.db")
    with pytest.raises(ReconcileRequired):
        await coordinator.recover(_SESSION_ID)
    required_events = [
        event for event in store.read_events(_SESSION_ID)
        if event.type == OPERATION_RECONCILE_REQUIRED
        and event.data.get("tool_call_id") == _CALL_ID
    ]
    assert not required_events, "缺少裁决 callback 时整体拒绝且不写事件"
    operation = await ledger.get(_SESSION_ID, _CALL_ID)
    assert operation is not None and operation.state.value == "RUNNING"

    class _ConfirmSuccess(ReconcileCallback):
        # 继承 ABC 而非鸭子类型：coordinator 对回调无条件调 `source_for`
        #（#357 W-13 契约 3），ABC 默认实现返回 None（诚实不伪造）；鸭子
        # 替身会在该调用点 AttributeError（#784）。resolve 的失败注入断言零改动。
        async def resolve(self, operation: Operation, hint) -> ReconcileVerdict:
            assert operation.tool_call_id == _CALL_ID
            current_events = store.read_events(_SESSION_ID)
            required = next(
                event for event in current_events
                if event.type == OPERATION_RECONCILE_REQUIRED
                and event.data.get("tool_call_id") == _CALL_ID
            )
            assert required.data["state"] == "NEED_RECONCILE"
            current_operation = await ledger.get(_SESSION_ID, _CALL_ID)
            assert current_operation is not None
            assert current_operation.state.value == "NEED_RECONCILE"
            return ReconcileVerdict.CONFIRM_SUCCESS

    coordinator = _coordinator(
        store,
        ledger,
        tmp_path / "state.db",
        reconcile_callback=_ConfirmSuccess(),
    )
    await coordinator.recover(_SESSION_ID)
    after_first_reconcile = store.read_events(_SESSION_ID)
    assert len(_registered_facts(after_first_reconcile)) == 1
    assert len(_tool_results(after_first_reconcile)) == 1
    first_fact = _registered_facts(after_first_reconcile)[0]
    first_source = next(
        event for event in after_first_reconcile
        if event.event_id == first_fact.data["source_event_id"]
    )
    assert first_fact.data["source_event_seq"] == first_source.seq
    assert is_direct_user_input_event(
        after_first_reconcile, first_source.event_id,
    )

    await coordinator.recover(_SESSION_ID)
    after_repeat = store.read_events(_SESSION_ID)
    assert len(_registered_facts(after_repeat)) == 1
    assert len(_tool_results(after_repeat)) == 1
    repeated_fact = _registered_facts(after_repeat)[0]
    repeated_source = next(
        event for event in after_repeat
        if event.event_id == repeated_fact.data["source_event_id"]
    )
    assert repeated_fact.data["source_event_seq"] == repeated_source.seq
    assert is_direct_user_input_event(after_repeat, repeated_source.event_id)
    terminal = await ledger.get(_SESSION_ID, _CALL_ID)
    assert terminal is not None and terminal.state.value == "SUCCEEDED"


@pytest.mark.asyncio
async def test_ledger_terminal_before_tool_result_restores_exact_result_once(
    tmp_path: Path,
) -> None:
    completed = await _kill_child(tmp_path, "terminal")
    assert completed.returncode == 9, completed.stderr
    assert completed.stdout.strip().endswith("AFTER_LEDGER_TERMINAL")

    store = JsonlSessionStore(tmp_path / "sessions")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    before = store.read_events(_SESSION_ID)
    operation = await ledger.get(_SESSION_ID, _CALL_ID)
    assert len(_registered_facts(before)) == 1
    assert not _tool_results(before)
    assert operation is not None and operation.state.value == "SUCCEEDED"
    assert operation.result_json is not None
    expected = ToolResult.model_validate_json(operation.result_json)
    assert expected.data is not None
    assert expected.data["status"] == "registered"
    assert expected.data["value"] == _CANDIDATE

    coordinator = _coordinator(store, ledger, tmp_path / "state.db")
    await coordinator.recover(_SESSION_ID)
    after_first_recovery = store.read_events(_SESSION_ID)
    restored = _tool_results(after_first_recovery)
    assert len(restored) == 1
    assert ToolResult.model_validate_json(restored[0].data["content"]) == expected

    await coordinator.recover(_SESSION_ID)
    after_repeat = store.read_events(_SESSION_ID)
    assert len(_registered_facts(after_repeat)) == 1
    assert len(_tool_results(after_repeat)) == 1
