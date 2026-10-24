from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_harness.session.event import RUN_COMPLETED, RUN_STARTED, USER_MESSAGE
from agent_harness.session.store import JsonlSessionStore
from agent_harness.watchtower.service import CONSENT_VERSION, WatchtowerService
from agent_harness.watchtower.store import SqliteWatchtowerStore

CRASH_EXIT_CODE = 91
PRE_SESSION_CRASH_EXIT_CODE = 92


async def _run_child(command: list[str], *, cwd: Path, env: dict[str, str]):
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crash_point", "expected_exit_code"),
    [
        ("before_session", PRE_SESSION_CRASH_EXIT_CODE),
        ("after_message", CRASH_EXIT_CODE),
    ],
)
async def test_process_kill_recovers_the_same_fire_once(
    tmp_path, crash_point, expected_exit_code,
):
    root = tmp_path / "serve"
    root.mkdir()
    store = SqliteWatchtowerStore(root / "harness.db")
    await store.initialize()
    service = WatchtowerService(
        store=store,
        session_service=SimpleNamespace(),
        settings=SimpleNamespace(ask_user_question_timeout=300),
    )
    rule = await service.create_rule(
        prompt="检查构建失败并修复",
        trigger_kind="relative",
        timezone="Asia/Shanghai",
        delay_seconds=60,
        local_time=None,
        model=None,
        sandbox_backend="local",
        cwd=None,
        budget_turns=4,
        budget_tokens=32_000,
        budget_duration_seconds=1800,
        consent_acknowledged=True,
        consent_version=CONSENT_VERSION,
    )
    armed = await store.transition(
        rule.rule_id, expected_version=rule.version,
        state="armed", event_kind="armed",
    )
    execution = await store.claim_fire(
        rule.rule_id, expected_version=armed.version,
        fire_id="fire-process-kill", session_id="session-process-kill",
        fire_at=rule.trigger_at,
    )
    assert execution is not None

    child = Path(__file__).with_name("_watchtower_kill_child.py")
    repo_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root / "src"), str(repo_root), environment.get("PYTHONPATH", "")],
    )
    crashed = await _run_child(
        [sys.executable, str(child), str(root), f"crash-{crash_point}", execution.session_id],
        cwd=repo_root,
        env=environment,
    )
    crash_output = (crashed.stdout + crashed.stderr).decode("utf-8", errors="replace")
    assert crashed.returncode == expected_exit_code, crash_output
    # Which of the two bookkeeping states the fire is left in depends on whether
    # `_dispatch` reached `finish_fire(fired)` before the child died; both are
    # legitimate crash windows and both must recover (#1546). The durable
    # contract is asserted below: no `run/started` yet, and recovery launches
    # that same fire exactly once.
    assert (await store.get_execution(execution.fire_id)).state in {
        "dispatching", "fired",
    }

    session_store = JsonlSessionStore(root / "sessions")
    crashed_events = session_store.read_events(execution.session_id)
    crashed_messages = [event for event in crashed_events if event.type == USER_MESSAGE]
    crashed_execution = await store.get_execution(execution.fire_id)
    if crash_point == "before_session":
        assert crashed_events == []
    else:
        assert len(crashed_messages) == 1
        assert crashed_messages[0].data["idempotency_key"] == execution.fire_id
    # The first deadline remains authoritative even if the process crashes
    # before creating the planned session.
    assert crashed_execution.deadline_at is not None
    assert not [event for event in crashed_events if event.type == RUN_STARTED]

    recovered = await _run_child(
        [sys.executable, str(child), str(root), "recover", execution.session_id],
        cwd=repo_root,
        env=environment,
    )
    recovery_output = (recovered.stdout + recovered.stderr).decode(
        "utf-8", errors="replace",
    )
    assert recovered.returncode == 0, recovery_output
    assert recovered.stdout.decode("utf-8", errors="replace").strip() == "1 1 1"

    final_events = session_store.read_events(execution.session_id)
    assert session_store.list_session_ids() == [execution.session_id]
    assert sum(event.type == USER_MESSAGE for event in final_events) == 1
    assert sum(event.type == RUN_STARTED for event in final_events) == 1
    assert sum(event.type == RUN_COMPLETED for event in final_events) == 1
    updated_store = SqliteWatchtowerStore(root / "harness.db")
    await updated_store.initialize()
    executions = await updated_store.list_executions(rule.rule_id)
    assert len(executions) == 1
    assert executions[0].fire_id == execution.fire_id
    assert executions[0].state == "fired"
    assert executions[0].deadline_at > datetime.now(UTC)
    assert executions[0].deadline_at == crashed_execution.deadline_at
    assert (await updated_store.get_rule(rule.rule_id)).fired_session_id == execution.session_id
    await service.stop()
