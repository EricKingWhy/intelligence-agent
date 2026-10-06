"""Subprocess helper for real register_constraint recovery crash windows."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import MethodType

from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import TASK_PROTECTED_FACT
from agent_harness.storage import SqliteOperationLedger
from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry
from agent_harness.tools.register_constraint import RegisterConstraintTool
from tests.scripted_model import ScriptedModel

CALL_ID = "register-constraint-kill"
CANDIDATE = "For this task, do not add third-party dependencies."


async def main() -> None:
    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    kill_stage = config["kill_stage"]
    store = JsonlSessionStore(root / "sessions")
    session = Session.start(store, session_id="register-constraint-crash")
    ledger = SqliteOperationLedger(root / "state.db")
    await ledger.initialize()

    if kill_stage == "fact":
        original = session.register_protected_fact

        def register_then_kill(self, *args, **kwargs):
            event = original(*args, **kwargs)
            if event.type != TASK_PROTECTED_FACT:
                raise AssertionError("expected a durable protected fact")
            sys.stdout.write("AFTER_PROTECTED_FACT\n")
            sys.stdout.flush()
            os._exit(9)

        session.register_protected_fact = MethodType(register_then_kill, session)

    registry = ToolRegistry()
    registry.register(RegisterConstraintTool())

    def kill_after_terminal(stage: str, _tool_call_id: str) -> None:
        if stage == "terminal":
            sys.stdout.write("AFTER_LEDGER_TERMINAL\n")
            sys.stdout.flush()
            os._exit(9)

    executor = ToolExecutor(
        registry,
        policy=PermissionPolicy.DANGER_FULL_ACCESS,
        operation_ledger=ledger,
        kill_hook=kill_after_terminal,
    )
    model = ScriptedModel([AIMessage(
        content="",
        tool_calls=[{
            "id": CALL_ID,
            "name": "register_constraint",
            "args": {"value": CANDIDATE},
        }],
    )])
    runtime = AgentRuntime(model, registry, executor)
    await runtime.run(session, CANDIDATE)
    raise SystemExit(3)


asyncio.run(main())
