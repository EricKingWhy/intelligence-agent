"""Hard-kill recovery for durable SessionBudget model request settlements."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from agent_harness.agent.run_budget import SessionLimits
from agent_harness.agent.runtime import AgentRuntime
from agent_harness.recovery.scan import ScanRecovery, scan_interrupted_sessions
from agent_harness.session import (
    MODEL_REQUEST,
    MODEL_REQUEST_STARTED,
    RUN_COMPLETED,
    RUN_INTERRUPTED,
    JsonlSessionStore,
    Session,
)
from agent_harness.storage import SqliteOperationLedger
from agent_harness.storage.delegation_tree import (
    SessionBudgetHandle,
    SessionBudgetRecoveryRequired,
    SqliteDelegationTreeLedger,
)
from agent_harness.tooling import ToolExecutor, ToolRegistry

_CHILD = Path(__file__).with_name("_session_budget_request_kill_child.py")


def test_session_budget_marker_owner_includes_current_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_harness.storage import delegation_tree

    current_pid = delegation_tree.os.getpid()
    current_owner = delegation_tree._session_budget_process_id()
    monkeypatch.setattr(
        delegation_tree.os, "getpid", lambda: current_pid + 1,
    )

    assert delegation_tree._session_budget_process_id() != current_owner


@pytest.mark.asyncio
async def test_hard_kill_recovers_closeout_settlement_once_and_enforces_ceiling(
    tmp_path: Path,
) -> None:
    session_id = f"session-budget-kill-{uuid4().hex}"
    config = json.dumps({"root": str(tmp_path), "session_id": session_id})
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_CHILD),
        config,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    store = JsonlSessionStore(tmp_path / "sessions")
    try:
        deadline = asyncio.get_running_loop().time() + 30
        output = []
        reached_barrier = False
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=remaining,
                )
            except TimeoutError:
                break
            if not line:
                break
            output.append(line)
            if line.rstrip(b"\r\n") == b"LEDGER_COMMIT_BARRIER":
                reached_barrier = True
                break
        if not reached_barrier:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)
            stdout = await process.stdout.read()
            stderr = await process.stderr.read()
            raise AssertionError(
                f"child exited before the ledger commit barrier: "
                f"rc={process.returncode}, output={output!r}, stdout={stdout!r}, "
                f"stderr={stderr!r}"
            )

        before_kill = store.read_events(session_id)
        requests = [event for event in before_kill if event.type == MODEL_REQUEST]
        starts = [
            event for event in before_kill if event.type == MODEL_REQUEST_STARTED
        ]
        assert [event.data["role"] for event in requests] == ["primary", "closeout"]
        assert [event.data["outcome"] for event in requests] == ["completed", "completed"]
        assert len({event.data["request_id"] for event in requests}) == 2
        assert [event.data["request_id"] for event in starts] == [
            event.data["request_id"] for event in requests
        ]
        assert [event.data["usage"]["total_tokens"] for event in requests] == [2, 7]
        assert [event.data["cost_usd"] for event in requests] == ["0.01", "0.02"]

        process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)
        await process.stdout.read()
        await process.stderr.read()
        assert process.returncode not in (None, 0)
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)
            await process.stdout.read()
            await process.stderr.read()

    database = tmp_path / "harness.db"
    operation_ledger = SqliteOperationLedger(database)
    budget_ledger = SqliteDelegationTreeLedger(database)
    await operation_ledger.initialize()
    await budget_ledger.initialize()

    recovered = await scan_interrupted_sessions(
        session_store=store,
        operation_ledger=operation_ledger,
        workspace_registry=None,
        database_path=database,
        session_budget_ledger=budget_ledger,
    )
    assert len(recovered) == 1
    assert recovered[0].session_id == session_id
    assert recovered[0].recovery is ScanRecovery.RECOVERED
    recovered_events = store.read_events(session_id)
    assert sum(event.type == RUN_INTERRUPTED for event in recovered_events) == 1
    assert [event for event in recovered_events if event.type == MODEL_REQUEST] == requests

    snapshot = await budget_ledger.get_session_budget(session_id)
    assert snapshot is not None
    assert snapshot.consumed.model_requests == 2
    assert snapshot.consumed.agent_turns == 1
    assert snapshot.consumed.total_tokens == 9
    assert snapshot.consumed.cost_usd == Decimal("0.03")

    repeated = await scan_interrupted_sessions(
        session_store=store,
        operation_ledger=operation_ledger,
        workspace_registry=None,
        database_path=database,
        session_budget_ledger=budget_ledger,
    )
    assert repeated == []
    assert await budget_ledger.get_session_budget(session_id) == snapshot

    class _NeverCall:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            raise AssertionError("provider must be rejected at admission")

        async def astream(self, messages, **kwargs):
            raise AssertionError("provider must be rejected at admission")
            yield

    provider = _NeverCall()
    resumed_session = Session.load(store, session_id)
    runtime = AgentRuntime(
        model=provider,
        registry=ToolRegistry(),
        executor=ToolExecutor(ToolRegistry()),
        max_agent_turns=10,
        session_budget=SessionBudgetHandle(
            budget_ledger,
            budget_key=session_id,
            root_session_id=session_id,
            limits=SessionLimits(max_model_requests=2),
        ),
    )
    result = await runtime.run(resumed_session, "This request must hit the ceiling.")
    assert result.status == "paused"
    assert provider.calls == 0
    assert sum(
        event.type == MODEL_REQUEST_STARTED for event in resumed_session.events
    ) == 2


@pytest.mark.asyncio
async def test_terminal_session_marker_is_recovered_without_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_harness.storage import delegation_tree

    session_id = f"session-budget-terminal-{uuid4().hex}"
    store = JsonlSessionStore(tmp_path / "sessions")
    session = Session.start(store, session_id=session_id)
    run_id, _ = session.begin_run()
    ledger = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    await ledger.initialize()
    await ledger.ensure_session_budget(
        session_id, root_session_id=session_id,
        limits=SessionLimits(max_model_requests=10),
    )

    admission = await ledger.admit_session_step(
        session_id,
        session_id=session_id,
        run_id=run_id,
        step_id=1,
        after_seq=session.next_seq,
    )
    assert admission.accepted
    request_id = str(uuid4())
    session.append(
        MODEL_REQUEST_STARTED,
        {"role": "primary", "request_id": request_id},
        run_id=run_id,
        step_id=1,
    )
    session.append(
        MODEL_REQUEST,
        {
            "role": "primary",
            "request_id": request_id,
            "outcome": "completed",
            "usage": {"total_tokens": 5},
            "cost_usd": "0.02",
        },
        run_id=run_id,
        step_id=1,
    )
    session.append(RUN_COMPLETED, {"final_text": "done"}, run_id=run_id)
    monkeypatch.setattr(
        delegation_tree,
        "_SESSION_BUDGET_PROCESS_ID",
        f"stale-{delegation_tree._SESSION_BUDGET_PROCESS_ID}",
    )

    operation_ledger = SqliteOperationLedger(tmp_path / "harness.db")
    await operation_ledger.initialize()
    recovered = await scan_interrupted_sessions(
        session_store=store,
        operation_ledger=operation_ledger,
        workspace_registry=None,
        database_path=tmp_path / "harness.db",
        session_budget_ledger=ledger,
    )

    assert recovered == []
    assert [event.type for event in store.read_events(session_id)].count(
        RUN_INTERRUPTED
    ) == 0
    snapshot = await ledger.get_session_budget(session_id)
    assert snapshot is not None
    assert snapshot.consumed.model_requests == 1
    assert snapshot.consumed.agent_turns == 1
    assert snapshot.consumed.total_tokens == 5
    assert snapshot.consumed.cost_usd == Decimal("0.02")


@pytest.mark.asyncio
async def test_malformed_pending_marker_blocks_new_admission(tmp_path: Path) -> None:
    ledger = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    await ledger.initialize()
    await ledger.ensure_session_budget(
        "session-budget-malformed", root_session_id="session-budget-malformed",
        limits=SessionLimits(),
    )
    async with ledger._connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        await connection.execute(
            """INSERT INTO session_budget_events
               (event_id, budget_key, kind, version, detail)
               VALUES (?, ?, ?, ?, ?)""",
            (
                str(uuid4()), "session-budget-malformed",
                "model_request_accounting_started", 1, "{",
            ),
        )
        await connection.commit()

    with pytest.raises(SessionBudgetRecoveryRequired, match="malformed"):
        await ledger.admit_session_step("session-budget-malformed")
