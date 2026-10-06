from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.session.derive import build_protected_fact_data
from agent_harness.session.event import TASK_PROTECTED_FACT, USER_MESSAGE
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import SqliteOperationLedger
from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry
from agent_harness.tools.request_constraint_resolution import (
    RequestConstraintResolutionTool,
)
from tests.scripted_model import ScriptedModel


async def main() -> None:
    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    session = Session.start(
        JsonlSessionStore(root / "sessions"),
        session_id="constraint-input-crash",
    )
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    fact = build_protected_fact_data(
        session.events,
        session_id=session.session_id,
        fact_type="constraint",
        value="Use Python.",
        source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, fact)
    candidate = "For this task, use TypeScript."
    model = ScriptedModel([AIMessage(
        content="",
        tool_calls=[{
            "id": "ask-constraint-crash",
            "name": "request_constraint_resolution",
            "args": {"fact_id": fact["fact_id"], "candidate": candidate},
        }],
    )])
    registry = ToolRegistry()
    registry.register(RequestConstraintResolutionTool())
    ledger = SqliteOperationLedger(root / "harness.db")
    await ledger.initialize()

    def crash_after_request(stage: str, _tool_call_id: str) -> None:
        if stage == "user_input_requested":
            sys.stdout.write("AFTER_INPUT_REQUEST\n")
            sys.stdout.flush()
            os._exit(9)

    runtime = AgentRuntime(
        model,
        registry,
        ToolExecutor(
            registry,
            policy=PermissionPolicy.DANGER_FULL_ACCESS,
            operation_ledger=ledger,
            kill_hook=crash_after_request,
        ),
    )
    await runtime.run(session, f"Please use the new approach: {candidate}")
    raise SystemExit(3)


asyncio.run(main())
