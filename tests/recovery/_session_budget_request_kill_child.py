"""Child process for the #619 ledger-commit crash test."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent.run_budget import SessionLimits
from agent_harness.agent.runtime import AgentRuntime
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.storage.delegation_tree import (
    SessionBudgetHandle,
    SqliteDelegationTreeLedger,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult


class _EmptyArgs(BaseModel):
    pass


class _NoopTool(Tool):
    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "A no-op tool for the recovery test."

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EmptyArgs

    async def execute(self, args: _EmptyArgs) -> ToolResult:
        return ToolResult.success(message="done", data={})


class _CloseoutModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "id": "call-1",
                    "name": "noop",
                    "args": {},
                    "type": "tool_call",
                }],
                usage_metadata={
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
                response_metadata={"cost": "0.01"},
            )
        return AIMessage(
            content=json.dumps({
                "completed": ["The first step is complete."],
                "remaining": ["The next step is pending."],
                "blockers": [],
                "next_safe_action": "Resume after review.",
            }),
            usage_metadata={
                "input_tokens": 3,
                "output_tokens": 4,
                "total_tokens": 7,
            },
            response_metadata={"cost": "0.02"},
        )

    async def astream(self, messages, **kwargs):
        raise AssertionError("this test uses AgentRuntime.run")
        yield


async def main() -> None:
    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    session_id = config["session_id"]
    database = root / "harness.db"
    ledger = SqliteDelegationTreeLedger(database)
    await ledger.initialize()
    budget = SessionBudgetHandle(
        ledger,
        budget_key=session_id,
        root_session_id=session_id,
        limits=SessionLimits(max_model_requests=2),
    )
    store = JsonlSessionStore(root / "sessions")
    session = Session.start(store, session_id=session_id)
    registry = ToolRegistry()
    registry.register(_NoopTool())

    original_append = SqliteDelegationTreeLedger._append_session_event
    settlement_writes = 0

    async def barrier_append(
        connection, budget_key, kind, *, version, detail=None, event_id=None,
    ):
        nonlocal settlement_writes
        persisted_id = await original_append(
            connection,
            budget_key,
            kind,
            version=version,
            detail=detail,
            event_id=event_id,
        )
        if kind == "requests_recorded":
            settlement_writes += 1
            if settlement_writes == 2:
                print("LEDGER_COMMIT_BARRIER", flush=True)
                await asyncio.Future()
        return persisted_id

    SqliteDelegationTreeLedger._append_session_event = staticmethod(barrier_append)
    runtime = AgentRuntime(
        model=_CloseoutModel(),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=1,
        session_budget=budget,
    )
    await runtime.run(session, "Exercise durable SessionBudget recovery.")


if __name__ == "__main__":
    asyncio.run(main())
