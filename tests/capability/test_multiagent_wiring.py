"""multiagent capability 接线测试（Phase 13 T2, #83, ADR-0015 决策 2）。

- CAPABILITIES 配 multiagent → wiring 携带 delegate 工具 + provider（激活后可用）
- 未配 / enabled=false → 零 delegate（单代理零感知）
- build_runtime 激活链：session_store 缺席 → delegate 降级缺席（不注册不炸）
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from agent_harness.assembly import (
    build_runtime,
    initialize_stores,
    recovery_stores,
)
from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import wire_capabilities
from agent_harness.config import Settings
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import Session
from agent_harness.session.store import JsonlSessionStore
from tests.scripted_model import ScriptedModel


def _settings(tmp_path, *, multiagent: bool = True) -> Settings:
    caps = {"multiagent": {"provider": "builtin", "enabled": multiagent, "options": {}}}
    return Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        capabilities=json.dumps(caps),
    )


@pytest.mark.asyncio
async def test_multiagent_wiring_carries_delegate(tmp_path):
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry, parse_capabilities_config(_settings(tmp_path).capabilities),
        settings=_settings(tmp_path),
    )
    assert any(isinstance(t, DelegateTool) for t in wiring.tools)
    assert isinstance(wiring.multiagent_provider, InProcessSubagentProvider)


@pytest.mark.asyncio
async def test_multiagent_disabled_no_delegate(tmp_path):
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry,
        parse_capabilities_config(_settings(tmp_path, multiagent=False).capabilities),
        settings=_settings(tmp_path, multiagent=False),
    )
    assert not any(isinstance(t, DelegateTool) for t in wiring.tools)
    assert wiring.multiagent_provider is None


@pytest.mark.asyncio
async def test_build_runtime_activates_provider(tmp_path):
    """端到端：capability 配置 → build_runtime → delegate 进 registry 且
    provider 已激活（可真实派 child，ScriptedModel 替身）。"""
    from unittest.mock import patch

    from tests.test_assembly import ScriptedModelFactory

    settings = _settings(tmp_path)
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config(settings.capabilities),
        settings=settings,
    )
    store = JsonlSessionStore(tmp_path / "sessions")

    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModelFactory()):
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=WorkspaceRegistry(root=tmp_path),
            session_id="sess-ma", workspace=tmp_path / "w",
            max_agent_turns=5, auto_approve=True, session_store=store,
        )

    assert "delegate" in [t.name for t in runtime.registry.list()]
    provider = runtime.registry.get("delegate")._provider
    assert isinstance(provider, InProcessSubagentProvider)
    assert provider._activated


@pytest.mark.asyncio
async def test_build_runtime_isolates_provider_state_between_sessions(tmp_path):
    """One cached capability wiring must not cross-bind concurrent root sessions."""
    from unittest.mock import patch

    settings = _settings(tmp_path)
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config(settings.capabilities),
        settings=settings,
    )
    store = JsonlSessionStore(tmp_path / "sessions")
    workspaces = WorkspaceRegistry(root=tmp_path / "workspaces")
    session_a = Session.start(store, session_id="runtime-a")
    session_b = Session.start(store, session_id="runtime-b")

    def delegate(call_id: str, task: str) -> AIMessage:
        return AIMessage(content="", tool_calls=[{
            "id": call_id,
            "name": "delegate",
            "args": {"target": "coding", "task": task},
        }])

    models = [
        ScriptedModel([
            delegate("root-a-call", "task from A"),
            AIMessage(content="child A complete"),
            AIMessage(content="root A complete"),
        ]),
        ScriptedModel([
            delegate("root-b-call", "task from B"),
            AIMessage(content="child B complete"),
            AIMessage(content="root B complete"),
        ]),
    ]
    with patch("agent_harness.assembly.create_chat_model", side_effect=models):
        runtime_a = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspaces, session_id=session_a.session_id,
            workspace=tmp_path / "project-a", max_agent_turns=5, auto_approve=True,
            session_store=store,
        )
        runtime_b = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspaces, session_id=session_b.session_id,
            workspace=tmp_path / "project-b", max_agent_turns=5, auto_approve=True,
            session_store=store,
        )

    provider_a = runtime_a.registry.get("delegate")._provider
    provider_b = runtime_b.registry.get("delegate")._provider
    assert provider_a is not provider_b

    results = await asyncio.gather(
        runtime_a.run(session_a, "root A"),
        runtime_b.run(session_b, "root B"),
    )

    assert [result.status for result in results] == ["completed", "completed"]
    assert provider_a.last_child_sessions[0].sandbox is workspaces.get("runtime-a")
    assert provider_b.last_child_sessions[0].sandbox is workspaces.get("runtime-b")
    root_run_a = next(event for event in session_a.events if event.type == "run/started")
    root_run_b = next(event for event in session_b.events if event.type == "run/started")
    tree_a = await stores.delegation_tree_ledger.get_state(root_run_a.run_id)
    tree_b = await stores.delegation_tree_ledger.get_state(root_run_b.run_id)
    assert tree_a.root_session_id == "runtime-a"
    assert tree_b.root_session_id == "runtime-b"
    assert tree_a.used_delegations == tree_b.used_delegations == 1


@pytest.mark.asyncio
async def test_build_runtime_without_session_store_degrades_delegate(tmp_path, caplog):
    """multiagent 启用但 session_store 缺席 → delegate 降级缺席（不注册不炸）。"""
    from unittest.mock import patch

    from tests.test_assembly import ScriptedModelFactory

    settings = _settings(tmp_path)
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    wiring = await wire_capabilities(
        CapabilityRegistry(), parse_capabilities_config(settings.capabilities),
        settings=settings,
    )

    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModelFactory()):
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=WorkspaceRegistry(root=tmp_path),
            session_id="s", workspace=tmp_path / "w",
            max_agent_turns=5, auto_approve=True, session_store=None,
        )

    assert "delegate" not in [t.name for t in runtime.registry.list()]
    assert any("session_store" in rec.message for rec in caplog.records)
