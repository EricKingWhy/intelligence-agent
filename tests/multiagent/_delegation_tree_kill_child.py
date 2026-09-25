"""Real-process crash fixture for the delegation tree recovery test (#287)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from langchain_core.messages import AIMessage

from agent_harness.agent.factory import AgentFactory
from agent_harness.agent.profiles import AgentSpec
from agent_harness.agent.runtime import AgentRuntime
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import SqliteOperationLedger
from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger
from agent_harness.tooling import ToolExecutor, ToolRegistry


class _FailingModel:
    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        raise RuntimeError("scripted child failure")


class _KillOnThirdRootCall:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 3:
            sys.stdout.write("KILL_AFTER_TWO_DURABLE_FAILURES\n")
            sys.stdout.flush()
            os._exit(91)
        return AIMessage(content="", tool_calls=[{
            "id": f"crash-call-{self.calls}",
            "name": "delegate",
            "args": {
                "target": "supervisor",
                "task": "same failing task across restart",
                "constraints": [],
            },
        }])


async def _run(root: Path) -> None:
    database = root / "harness.db"
    sessions = JsonlSessionStore(root / "sessions")
    workspaces = WorkspaceRegistry(root=root / "workspaces")
    session = Session.start(sessions, session_id="delegation-tree-crash")
    workspaces.create(session.session_id, workspace_root=root / "workspace")
    operations = SqliteOperationLedger(database)
    trees = SqliteDelegationTreeLedger(database)
    await operations.initialize()
    await trees.initialize()
    profiles = {
        "supervisor": AgentSpec(
            name="supervisor", description="supervisor", system_prompt="supervisor",
            tool_scope=frozenset({"delegate"}), max_agent_turns=8, max_depth=4,
        ),
    }
    provider = InProcessSubagentProvider(profiles=profiles)
    registry = ToolRegistry()
    registry.register(DelegateTool(provider, max_delegations=5))
    provider.activate(
        factory=AgentFactory(
            model=_FailingModel(),
            executor_factory=lambda child_registry: ToolExecutor(
                child_registry, operation_ledger=operations,
            ),
        ),
        source_registry=registry,
        session_store=sessions,
        workspace_registry=workspaces,
        parent_session_id=session.session_id,
        max_depth=4,
        max_delegations=5,
        delegation_ledger=trees,
    )
    runtime = AgentRuntime(
        model=_KillOnThirdRootCall(),
        registry=registry,
        executor=ToolExecutor(registry, operation_ledger=operations),
        max_agent_turns=8,
    )
    await runtime.run(session, "start crash/resume verification")


if __name__ == "__main__":
    asyncio.run(_run(Path(json.loads(sys.argv[1])["root"])))
