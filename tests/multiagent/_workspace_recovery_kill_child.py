"""Real-process delegated Coding Tool crash fixture for #288."""

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
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tools.write import WriteTool


class _WriteThenKillModel:
    def __init__(self) -> None:
        self._calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        self._calls += 1
        if self._calls == 1:
            return AIMessage(content="", tool_calls=[{
                "id": "coding-write-marker",
                "name": "write",
                "args": {
                    "path": "recovery-marker.txt",
                    "content": "written-before-crash",
                },
            }])
        sys.stdout.write("KILL_AFTER_CHILD_WORKSPACE_WRITE\n")
        sys.stdout.flush()
        os._exit(91)


async def _run(root: Path) -> None:
    database = root / "harness.db"
    session_store = JsonlSessionStore(root / "sessions")
    workspace_registry = WorkspaceRegistry(root=root / "workspaces")
    parent = Session.start(session_store, session_id="workspace-recovery-parent")
    workspace_registry.create(
        parent.session_id, workspace_root=root / "workspace",
    )
    operation_ledger = SqliteOperationLedger(database)
    await operation_ledger.initialize()

    profiles = {
        "coding": AgentSpec(
            name="coding",
            description="coding child",
            system_prompt="write the requested marker",
            tool_scope=frozenset({"write"}),
            max_agent_turns=4,
        ),
    }
    provider = InProcessSubagentProvider(profiles=profiles)
    registry = ToolRegistry()
    sandbox = workspace_registry.get(parent.session_id)
    registry.register(WriteTool(sandbox))
    registry.register(DelegateTool(provider, max_delegations=1))
    provider.activate(
        factory=AgentFactory(
            model=_WriteThenKillModel(),
            executor_factory=lambda child_registry: ToolExecutor(
                child_registry, operation_ledger=operation_ledger,
            ),
        ),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=parent.session_id,
        max_depth=1,
        max_delegations=1,
    )
    runtime = AgentRuntime(
        model=_ParentDelegateModel(),
        registry=registry,
        executor=ToolExecutor(registry, operation_ledger=operation_ledger),
        max_agent_turns=4,
    )
    await runtime.run(parent, "delegate marker creation to coding")


class _ParentDelegateModel:
    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content="", tool_calls=[{
            "id": "root-delegation",
            "name": "delegate",
            "args": {"target": "coding", "task": "write recovery marker"},
        }])


if __name__ == "__main__":
    asyncio.run(_run(Path(json.loads(sys.argv[1])["root"])))
