"""Behavioral contracts for persistent delegation trees and child recovery (#287/#288)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent.factory import AgentFactory
from agent_harness.agent.profiles import AgentSpec
from agent_harness.agent.runtime import AgentRuntime
from agent_harness.agent.types import STATUS_IDENTICAL_TOOL_FAILURE_LOOP
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.recovery import RecoveryCoordinator
from agent_harness.recovery.scan import scan_interrupted_sessions
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import Session
from agent_harness.session.event import (
    RUN_INTERRUPTED,
    TOOL_FAILURE_GUARD,
    TOOL_RESULT,
)
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import SqliteOperationLedger
from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tools.read import ReadTool
from tests.scripted_model import ScriptedModel


def _spec(name: str) -> AgentSpec:
    return AgentSpec(
        name=name,
        description=name,
        system_prompt=f"You are {name}.",
        tool_scope=frozenset({"delegate"}),
        max_agent_turns=8,
        max_depth=4,
    )


def _delegate(call_id: str, target: str, task: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "id": call_id,
        "name": "delegate",
        "args": {"target": target, "task": task},
    }])


def _delegates(*call_ids: str, target: str, task: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "id": call_id,
        "name": "delegate",
        "args": {"target": target, "task": task},
    } for call_id in call_ids])


class _FailingModel:
    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        raise RuntimeError("scripted child failure")


@pytest.mark.asyncio
async def test_descendants_consume_one_cumulative_tree_budget(tmp_path):
    """Root, child, and grandchild share the root's finite delegation budget."""
    session_store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    session = Session.start(session_store, session_id="root-session")
    workspace_registry.create(session.session_id, workspace_root=tmp_path / "workspace")

    profiles = {name: _spec(name) for name in ("supervisor", "leaf", "nested")}
    child_model = ScriptedModel([
        _delegate("child-call-1", "leaf", "work leaf"),
        _delegate("leaf-call-1", "nested", "work nested"),
        AIMessage(content="nested complete"),
        AIMessage(content="leaf complete"),
        AIMessage(content="supervisor complete"),
    ])
    provider = InProcessSubagentProvider(profiles=profiles)
    delegate = DelegateTool(provider, max_delegations=2)
    tree_ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await tree_ledger.initialize()
    registry = ToolRegistry()
    registry.register(delegate)
    provider.activate(
        factory=AgentFactory(model=child_model),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=session.session_id,
        max_depth=4,
        delegation_ledger=tree_ledger,
    )

    root = AgentRuntime(
        model=ScriptedModel([
            _delegate("root-call-1", "supervisor", "start tree"),
            AIMessage(content="root complete"),
        ]),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=8,
    )

    result = await root.run(session, "run the delegation tree")

    assert result.status == "completed"
    assert len(provider.last_child_sessions) == 2, (
        "the third spawn must be refused before creating a nested child session"
    )
    leaf_session = provider.last_child_sessions[-1]
    leaf_results = [
        json.loads(event.data["content"])
        for event in leaf_session.events
        if event.type == TOOL_RESULT
    ]
    assert len(leaf_results) == 1
    assert leaf_results[0]["ok"] is False
    assert "预算耗尽" in leaf_results[0]["message"]


@pytest.mark.asyncio
async def test_delegation_tree_metadata_persists_on_child_session(tmp_path):
    session_store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    session = Session.start(session_store, session_id="metadata-root")
    workspace_registry.create(session.session_id, workspace_root=tmp_path / "workspace")
    profiles = {"supervisor": _spec("supervisor")}
    model = ScriptedModel([AIMessage(content="done")])
    provider = InProcessSubagentProvider(profiles=profiles)
    registry = ToolRegistry()
    registry.register(DelegateTool(provider))
    tree_ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await tree_ledger.initialize()
    provider.activate(
        factory=AgentFactory(model=model),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=session.session_id,
        max_depth=3,
        delegation_ledger=tree_ledger,
    )

    root = AgentRuntime(
        model=ScriptedModel([
            _delegate("metadata-call", "supervisor", "task"),
            AIMessage(content="root complete"),
        ]),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=4,
    )
    await root.run(session, "start")

    child = provider.last_child_sessions[0]
    started = next(event for event in child.events if event.type == "session/started")
    root_run = next(event for event in session.events if event.type == "run/started")
    assert started.data["delegation_tree_id"] == root_run.run_id
    assert started.data["delegation_root_session_id"] == session.session_id
    assert started.data["delegation_remaining_depth"] == 2

    # Rebuild a provider around the persisted child session with a deliberately
    # larger profile cap; the session's remaining allowance stays authoritative.
    assert child.sandbox is workspace_registry.get(session.session_id)
    assert workspace_registry.get(child.session_id) is child.sandbox
    recovered_provider = InProcessSubagentProvider(profiles=profiles)
    recovered_registry = ToolRegistry()
    recovered_registry.register(DelegateTool(recovered_provider))
    recovered_provider.activate(
        factory=AgentFactory(model=ScriptedModel([AIMessage(content="grandchild done")])),
        source_registry=recovered_registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=child.session_id,
        max_depth=99,
        delegation_ledger=tree_ledger,
    )
    recovered_runtime = AgentRuntime(
        model=ScriptedModel([
            _delegate("recovered-child-call", "supervisor", "continue nested work"),
            AIMessage(content="recovered child complete"),
        ]),
        registry=recovered_registry,
        executor=ToolExecutor(recovered_registry),
        max_agent_turns=4,
    )
    await recovered_runtime.run(child, "resume child")

    nested_started = next(
        event for event in recovered_provider.last_child_sessions[0].events
        if event.type == "session/started"
    )
    assert nested_started.data["delegation_tree_id"] == root_run.run_id
    assert nested_started.data["delegation_remaining_depth"] == 1


@pytest.mark.asyncio
async def test_interrupted_root_reuses_the_same_tree_budget_on_resume(tmp_path):
    session_store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    session = Session.start(session_store, session_id="resume-root")
    session.append(RUN_INTERRUPTED, {"reason": "process_restart"}, run_id="prior-tree")
    workspace_registry.create(session.session_id, workspace_root=tmp_path / "workspace")
    profiles = {"supervisor": _spec("supervisor")}
    provider = InProcessSubagentProvider(profiles=profiles)
    registry = ToolRegistry()
    registry.register(DelegateTool(provider))
    tree_ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await tree_ledger.initialize()
    provider.activate(
        factory=AgentFactory(model=ScriptedModel([AIMessage(content="child done")])),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=session.session_id,
        max_depth=3,
        delegation_ledger=tree_ledger,
    )
    root = AgentRuntime(
        model=ScriptedModel([
            _delegate("resume-call", "supervisor", "continue interrupted work"),
            AIMessage(content="root complete"),
        ]),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=4,
    )

    await root.run(session, "resume")

    state = await tree_ledger.get_state("prior-tree")
    assert state.used_delegations == 1
    started = next(
        event for event in provider.last_child_sessions[0].events
        if event.type == "session/started"
    )
    assert started.data["delegation_tree_id"] == "prior-tree"


@pytest.mark.asyncio
async def test_interrupted_root_cannot_expand_persisted_depth_on_resume(tmp_path):
    session_store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    session = Session.start(session_store, session_id="depth-resume-root")
    session.append(RUN_INTERRUPTED, {"reason": "process_restart"}, run_id="bounded-tree")
    workspace_registry.create(session.session_id, workspace_root=tmp_path / "workspace")
    tree_ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await tree_ledger.initialize()
    await tree_ledger.reserve(
        "bounded-tree", root_session_id=session.session_id,
        max_delegations=8, max_depth=2,
    )

    profiles = {"supervisor": _spec("supervisor")}
    provider = InProcessSubagentProvider(profiles=profiles)
    registry = ToolRegistry()
    registry.register(DelegateTool(provider, max_delegations=8))
    provider.activate(
        factory=AgentFactory(model=ScriptedModel([
            _delegate("nested-after-resume", "supervisor", "nested task"),
            AIMessage(content="child complete"),
        ])),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=session.session_id,
        max_depth=3,
        max_delegations=8,
        delegation_ledger=tree_ledger,
    )
    root = AgentRuntime(
        model=ScriptedModel([
            _delegate("root-after-resume", "supervisor", "resume work"),
            AIMessage(content="root complete"),
        ]),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=4,
    )

    result = await root.run(session, "resume interrupted root")

    assert result.status == "completed"
    assert len(provider.last_child_sessions) == 1
    first_child = provider.last_child_sessions[0]
    started = next(event for event in first_child.events if event.type == "session/started")
    assert started.data["delegation_remaining_depth"] == 1
    assert (await tree_ledger.get_state("bounded-tree")).max_depth == 2


@pytest.mark.asyncio
async def test_runtime_provider_clones_share_process_child_limit(tmp_path):
    class _ConcurrentChildModel:
        def __init__(self):
            self.active = 0
            self.peak = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.03)
                return AIMessage(content="child complete")
            finally:
                self.active -= 1

    session_store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    model = _ConcurrentChildModel()
    prototype = InProcessSubagentProvider(profiles={"supervisor": _spec("supervisor")})
    runtimes = []
    for index in range(2):
        session = Session.start(session_store, session_id=f"parallel-root-{index}")
        workspace_registry.create(session.session_id)
        provider = prototype.new_runtime_instance()
        registry = ToolRegistry()
        registry.register(DelegateTool(provider))
        provider.activate(
            factory=AgentFactory(model=model),
            source_registry=registry,
            session_store=session_store,
            workspace_registry=workspace_registry,
            parent_session_id=session.session_id,
            max_depth=2,
            max_active_children=1,
        )
        runtimes.append((
            AgentRuntime(
                model=ScriptedModel([
                    _delegate(f"parallel-call-{index}", "supervisor", "parallel task"),
                    AIMessage(content="root complete"),
                ]),
                registry=registry,
                executor=ToolExecutor(registry),
                max_agent_turns=4,
            ),
            session,
        ))

    results = await asyncio.gather(*(
        runtime.run(session, "delegate concurrently")
        for runtime, session in runtimes
    ))

    assert [result.status for result in results] == ["completed", "completed"]
    assert model.peak == 1


@pytest.mark.asyncio
async def test_same_failed_delegation_across_descendants_triggers_persistent_guard(tmp_path):
    session_store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    session = Session.start(session_store, session_id="guard-root")
    workspace_registry.create(session.session_id, workspace_root=tmp_path / "workspace")
    profiles = {"supervisor": _spec("supervisor")}
    child_model = ScriptedModel([
        _delegate("child-fails", "supervisor", "identical failing task"),
    ])
    provider = InProcessSubagentProvider(profiles=profiles)
    registry = ToolRegistry()
    registry.register(DelegateTool(provider, max_delegations=10))
    tree_ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await tree_ledger.initialize()
    provider.activate(
        factory=AgentFactory(model=child_model),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=session.session_id,
        max_depth=4,
        delegation_ledger=tree_ledger,
    )
    root = AgentRuntime(
        model=ScriptedModel([
            _delegate(f"root-{index}", "supervisor", "identical failing task")
            for index in range(5)
        ]),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=10,
    )

    result = await root.run(session, "repeat the delegated task")

    assert result.status == STATUS_IDENTICAL_TOOL_FAILURE_LOOP
    guard_events = [event.data for event in session.events if event.type == TOOL_FAILURE_GUARD]
    assert [event["level"] for event in guard_events] == ["soft", "hard"]
    assert guard_events[0]["consecutive_failures"] == 3
    assert guard_events[1]["consecutive_failures"] == 6
    persisted_results = [
        json.loads(event.data["content"])
        for event in session.events if event.type == TOOL_RESULT
    ]
    assert all("runtime_signal" not in result for result in persisted_results)


@pytest.mark.asyncio
async def test_identical_failed_sibling_delegations_share_guard_state(tmp_path):
    session_store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    session = Session.start(session_store, session_id="sibling-guard-root")
    workspace_registry.create(session.session_id, workspace_root=tmp_path / "workspace")
    profiles = {"supervisor": _spec("supervisor")}
    provider = InProcessSubagentProvider(profiles=profiles)
    registry = ToolRegistry()
    registry.register(DelegateTool(provider, max_delegations=3))
    tree_ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await tree_ledger.initialize()
    provider.activate(
        factory=AgentFactory(model=_FailingModel()),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=session.session_id,
        max_depth=4,
        max_delegations=3,
        delegation_ledger=tree_ledger,
    )
    root = AgentRuntime(
        model=ScriptedModel([
            _delegate("sibling-a", "supervisor", "same sibling failure"),
            _delegate("sibling-b", "supervisor", "same sibling failure"),
            _delegate("sibling-c", "supervisor", "same sibling failure"),
            AIMessage(content="siblings reconciled"),
        ]),
        registry=registry,
        executor=ToolExecutor(registry),
        # 本剧本需要 4 个产出轮（3 次委派 + 1 次收口回答）。`#312` 起 local fuse
        # 预留最后 1 轮给 closeout（`02 §5.1/5.2`：fuse=4 ⇒ 只接纳 3 个产出轮，
        # 第 4 轮归收口调用）——所以这里给 5，否则 run 会在第 4 轮之前 `run/paused`。
        max_agent_turns=5,
    )

    result = await root.run(session, "run sibling failures concurrently")

    assert result.status == "completed"
    guard_events = [event.data for event in session.events if event.type == TOOL_FAILURE_GUARD]
    assert [event["level"] for event in guard_events] == ["soft"]
    assert guard_events[0]["consecutive_failures"] == 3


@pytest.mark.asyncio
async def test_constraints_participate_in_repeated_delegation_fingerprint(tmp_path):
    session_store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    session = Session.start(session_store, session_id="constraint-guard-root")
    workspace_registry.create(session.session_id, workspace_root=tmp_path / "workspace")
    profiles = {"supervisor": _spec("supervisor")}
    provider = InProcessSubagentProvider(profiles=profiles)
    registry = ToolRegistry()
    registry.register(DelegateTool(provider, max_delegations=3))
    tree_ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await tree_ledger.initialize()
    provider.activate(
        factory=AgentFactory(model=_FailingModel()),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=session.session_id,
        max_depth=4,
        max_delegations=3,
        delegation_ledger=tree_ledger,
    )
    root = AgentRuntime(
        model=ScriptedModel([
            AIMessage(content="", tool_calls=[
                {"id": "constraint-a1", "name": "delegate", "args": {
                    "target": "supervisor", "task": "same task",
                    "constraints": ["constraint A"],
                }},
                {"id": "constraint-b", "name": "delegate", "args": {
                    "target": "supervisor", "task": "same task",
                    "constraints": ["constraint B"],
                }},
                {"id": "constraint-a2", "name": "delegate", "args": {
                    "target": "supervisor", "task": "same task",
                    "constraints": ["constraint A"],
                }},
            ]),
            AIMessage(content="different constrained work was attempted"),
        ]),
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=4,
    )

    result = await root.run(session, "compare distinct constrained delegations")

    assert result.status == "completed"
    assert not [event for event in session.events if event.type == TOOL_FAILURE_GUARD]
    root_run = next(event for event in session.events if event.type == "run/started")
    tree_state = await tree_ledger.get_state(root_run.run_id)
    assert tree_state.consecutive_failures == 1


@pytest.mark.asyncio
async def test_sibling_delegations_race_for_one_durable_tree_slot(tmp_path):
    session_store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    session = Session.start(session_store, session_id="budget-race-root")
    workspace_registry.create(session.session_id, workspace_root=tmp_path / "workspace")
    profiles = {"supervisor": _spec("supervisor")}
    provider = InProcessSubagentProvider(profiles=profiles)
    tool = DelegateTool(provider, max_delegations=1)
    registry = ToolRegistry()
    registry.register(tool)
    tree_ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await tree_ledger.initialize()
    provider.activate(
        factory=AgentFactory(model=ScriptedModel([AIMessage(content="child done")])),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=session.session_id,
        max_depth=3,
        max_delegations=1,
        delegation_ledger=tree_ledger,
    )
    args_type = tool.args_schema

    results = await asyncio.gather(
        tool.execute(args_type(target="supervisor", task="sibling A")),
        tool.execute(args_type(target="supervisor", task="sibling B")),
    )

    assert sum(result.ok for result in results) == 1
    assert sum("预算耗尽" in result.message for result in results) == 1
    assert len(provider.last_child_sessions) == 1
    state = await tree_ledger.get_state(session.session_id)
    assert state.used_delegations == 1
    assert state.max_delegations == 1


@pytest.mark.asyncio
async def test_process_kill_and_recovery_preserve_budget_and_failure_fingerprint(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    child_process = Path(__file__).with_name("_delegation_tree_kill_child.py")
    root = tmp_path / "process-kill"
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    crashed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(child_process), json.dumps({"root": str(root)})],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert crashed.returncode == 91
    assert "KILL_AFTER_TWO_DURABLE_FAILURES" in crashed.stdout

    database = root / "harness.db"
    session_store = JsonlSessionStore(root / "sessions")
    workspace_registry = WorkspaceRegistry(root=root / "workspaces")
    operation_ledger = SqliteOperationLedger(database)
    tree_ledger = SqliteDelegationTreeLedger(database)
    await operation_ledger.initialize()
    await tree_ledger.initialize()
    scan_results = await scan_interrupted_sessions(
        session_store=session_store,
        workspace_registry=workspace_registry,
        operation_ledger=operation_ledger,
        database_path=database,
    )
    assert [result.session_id for result in scan_results] == ["delegation-tree-crash"]
    recovered = Session.resume(session_store, "delegation-tree-crash")
    prior_run = next(event for event in recovered.events if event.type == "run/started")
    assert any(event.type == RUN_INTERRUPTED for event in recovered.events), [
        (event.type, event.run_id) for event in recovered.events
    ]
    prior_tree = await tree_ledger.get_state(prior_run.run_id)
    assert prior_tree.used_delegations == 2
    assert prior_tree.fingerprint is not None
    assert prior_tree.consecutive_failures == 2

    profiles = {"supervisor": _spec("supervisor")}
    provider = InProcessSubagentProvider(profiles=profiles)
    registry = ToolRegistry()
    registry.register(DelegateTool(provider, max_delegations=5))
    provider.activate(
        factory=AgentFactory(model=_FailingModel()),
        source_registry=registry,
        session_store=session_store,
        workspace_registry=workspace_registry,
        parent_session_id=recovered.session_id,
        max_depth=4,
        max_delegations=5,
        delegation_ledger=tree_ledger,
    )
    resumed_runtime = AgentRuntime(
        model=ScriptedModel([
            _delegate("after-restart", "supervisor", "same failing task across restart"),
            AIMessage(content="resume complete"),
        ]),
        registry=registry,
        executor=ToolExecutor(registry, operation_ledger=operation_ledger),
        max_agent_turns=4,
    )

    result = await resumed_runtime.run(recovered, "resume interrupted tree")

    assert result.status == "completed"
    resumed_child_started = next(
        event for event in provider.last_child_sessions[0].events
        if event.type == "session/started"
    )
    assert resumed_child_started.data["delegation_tree_id"] == prior_run.run_id
    guard_events = [event.data for event in recovered.events if event.type == TOOL_FAILURE_GUARD]
    assert [event["level"] for event in guard_events] == ["soft"], (
        await tree_ledger.get_state(prior_run.run_id)
    )
    assert guard_events[0]["consecutive_failures"] == 3
    resumed_tree = await tree_ledger.get_state(prior_run.run_id)
    assert resumed_tree.used_delegations == 3
    assert resumed_tree.consecutive_failures == 3


@pytest.mark.asyncio
async def test_child_process_recovery_restores_delegated_coding_workspace(tmp_path):
    """A restarted RecoveryCoordinator resolves the persisted child workspace alias."""
    project_root = Path(__file__).resolve().parents[2]
    child_process = Path(__file__).with_name("_workspace_recovery_kill_child.py")
    root = tmp_path / "workspace-recovery"
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    crashed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(child_process), json.dumps({"root": str(root)})],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert crashed.returncode == 91
    assert "KILL_AFTER_CHILD_WORKSPACE_WRITE" in crashed.stdout

    session_store = JsonlSessionStore(root / "sessions")
    child_sessions = [
        session_id
        for session_id in session_store.list_session_ids()
        if session_id != "workspace-recovery-parent"
    ]
    assert len(child_sessions) == 1
    child_id = child_sessions[0]
    child_events = session_store.read_events(child_id)
    child_started = next(event for event in child_events if event.type == "session/started")
    assert child_started.agent_id == "coding"
    assert child_started.data["delegation_root_session_id"] == "workspace-recovery-parent"
    database = root / "harness.db"
    operation_ledger = SqliteOperationLedger(database)
    await operation_ledger.initialize()
    registry_after_restart = WorkspaceRegistry(root=root / "workspaces")
    coordinator = RecoveryCoordinator(
        session_store=session_store,
        workspace_registry=registry_after_restart,
        operation_ledger=operation_ledger,
        database_path=database,
    )
    recovered_child = await coordinator.recover(child_id)

    assert recovered_child.sandbox is not None
    assert Path(recovered_child.sandbox.workspace_root) == root / "workspace"
    assert recovered_child.sandbox.read_text("recovery-marker.txt") == "written-before-crash"
    child_operations = await operation_ledger.list_for_session(child_id)
    assert len(child_operations) == 1
    assert child_operations[0].session_id == child_id
    assert child_operations[0].agent_id == "coding"
    assert child_operations[0].tool_name == "write"

    recovered_registry = ToolRegistry()
    recovered_registry.register(ReadTool(recovered_child.sandbox))
    recovered_runtime = AgentRuntime(
        model=ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "id": "read-marker-after-recovery",
                "name": "read",
                "args": {"path": "recovery-marker.txt"},
            }]),
            AIMessage(content="recovered child complete"),
        ]),
        registry=recovered_registry,
        executor=ToolExecutor(
            recovered_registry, operation_ledger=operation_ledger,
        ),
        max_agent_turns=4,
        agent_id="coding",
    )
    result = await recovered_runtime.run(recovered_child, "continue after recovery")

    assert result.status == "completed"
    assert recovered_child.sandbox.read_text("recovery-marker.txt") == "written-before-crash"
    resumed_operations = await operation_ledger.list_for_session(child_id)
    assert [operation.tool_name for operation in resumed_operations] == ["write", "read"]
    assert [operation.agent_id for operation in resumed_operations] == [
        "coding", "coding",
    ]
    read_result = next(
        json.loads(event.data["content"])
        for event in recovered_child.events
        if event.type == TOOL_RESULT
        and event.data["tool_call_id"] == "read-marker-after-recovery"
    )
    assert read_result["data"]["content"] == "written-before-crash"
    assert sum(event.type == TOOL_RESULT for event in recovered_child.events) == 2
