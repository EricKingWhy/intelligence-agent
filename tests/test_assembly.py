"""Runtime 装配单一入口（批次 A / 候选 1+4）。

build_runtime 是 web 与 CLI 共享的深 factory：一个 interface 回答
"一个 Run 被装配了什么"。此前 web/_build_runtime 内联 70 行、cli 第二套
削弱装配（无 Ledger/Checkpoint/工具）——耐久性语义分叉且测试只能按名
patch 私有符号。CapabilityWiring.aclose 把关闭知识从 web 层收拢回创建者。
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk
from pydantic import BaseModel

from agent_harness.assembly import (
    RecoveryStores,
    assemble_wiring,
    build_runtime,
    initialize_stores,
    recovery_stores,
)
from agent_harness.capability.wiring import CapabilityWiring
from agent_harness.config import Settings
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.storage import OnStableBoundary
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, workspace_dir=str(tmp_path),
                    model_api_key="sk-test")


class ScriptedModelFactory:
    """astream 可用的替身模型（factory 只需 runtime 可构造，不真正调用）。"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(content="ok")


class _ProbeArgs(BaseModel):
    text: str = ""


class ProbeCapabilityTool(Tool):
    """capability 贡献的工具替身（进统一 ToolRegistry 验证零旁路）。"""

    @property
    def name(self) -> str:
        return "capability_probe"

    @property
    def description(self) -> str:
        return "probe"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _ProbeArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.READ_ONLY

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def timeout_seconds(self) -> float:
        return 5.0

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(verifiable=False)

    async def execute(self, args: BaseModel) -> ToolResult:
        return ToolResult.success("probe")


def _stores(tmp_path) -> RecoveryStores:
    return recovery_stores(tmp_path / "harness.db")


@pytest.mark.asyncio
async def test_build_runtime_wires_full_stack(tmp_path):
    """factory 产出与旧 web._build_runtime 等价的全栈接线：coding 工具 +
    Ledger + Checkpoint + SessionMeta + workspace 映射 + 审批直通。"""
    settings = _settings(tmp_path)
    _, wiring = await assemble_wiring(settings)
    stores = _stores(tmp_path)
    await initialize_stores(stores)
    workspace_registry = WorkspaceRegistry(root=tmp_path, backend="local")

    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModelFactory()):
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspace_registry,
            session_id="sess-assembly",
            workspace=tmp_path / "workspaces" / "sess-assembly",
            max_steps=10, auto_approve=True,
        )

    # coding 工具在册（CLI 不再是削弱装配——与 web 同一 stack）
    assert "bash" in [tool.name for tool in runtime.registry.list()]
    # Ledger / Meta 是传入实例（不变量 #13：统一记账）；Checkpoint 策略挂 store
    assert runtime.executor._operation_ledger is stores.operation_ledger
    assert runtime._session_meta_store is stores.session_meta_store
    assert isinstance(runtime._checkpoint_policy, OnStableBoundary)
    assert runtime._checkpoint_policy._store is stores.checkpoint_store
    # workspace 映射持久化：恢复时可还原 sandbox
    assert workspace_registry.exists("sess-assembly")
    # auto_approve=True → 审批直通
    assert runtime.executor._approval_callback is not None


@pytest.mark.asyncio
async def test_build_runtime_wires_capability_tools_and_manual_approval(tmp_path):
    """wiring.tools 进 ToolRegistry（统一 Executor 路径，不变量 #7）、
    wiring.context_providers 进 ContextBuilder；manual 模式危险操作默认拒绝。"""
    settings = _settings(tmp_path)
    provider_sentinel = object()
    wiring = CapabilityWiring(context_providers=[provider_sentinel],
                              tools=[ProbeCapabilityTool()])
    stores = _stores(tmp_path)
    await initialize_stores(stores)

    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModelFactory()):
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=WorkspaceRegistry(root=tmp_path, backend="local"),
            session_id="s", workspace=tmp_path / "w",
            max_steps=5, auto_approve=False,
        )

    assert "capability_probe" in [tool.name for tool in runtime.registry.list()]
    assert runtime._context_builder.context_providers == [provider_sentinel]
    response = runtime.executor._approval_callback(object())
    assert response.approved is False


@pytest.mark.asyncio
async def test_build_runtime_wires_model_fallback(tmp_path):
    """Model Fallback 装配（T5, #80, ADR-0014 决策 14/16）：config.fallback
    存在 → 两级模型入 Runtime；未配 → fallback 保持 None（单级）。"""
    _, wiring = await assemble_wiring(_settings(tmp_path))
    stores = _stores(tmp_path)
    await initialize_stores(stores)
    created = []

    def fake_create(config):
        model = ScriptedModelFactory()
        created.append((config, model))
        return model

    # 未配 fallback：单级
    with patch("agent_harness.assembly.create_chat_model", side_effect=fake_create):
        runtime = await build_runtime(
            settings=_settings(tmp_path), wiring=wiring, stores=stores,
            workspace_registry=WorkspaceRegistry(root=tmp_path, backend="local"),
            session_id="s1", workspace=tmp_path / "w1",
            max_steps=5, auto_approve=True,
        )
    assert runtime._fallback_model is None
    assert runtime._primary_model_name == "deepseek-chat"

    created.clear()
    fb_settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        fallback_model_provider="senseaudio",
        fallback_model_name="deepseek-v4-flash-0731",
        fallback_model_api_key="sk-fb",
    )
    with patch("agent_harness.assembly.create_chat_model", side_effect=fake_create):
        runtime = await build_runtime(
            settings=fb_settings, wiring=wiring, stores=stores,
            workspace_registry=WorkspaceRegistry(root=tmp_path, backend="local"),
            session_id="s2", workspace=tmp_path / "w2",
            max_steps=5, auto_approve=True,
        )
    # 两次构造：第 1 次 primary、第 2 次 fallback；配置链一致
    assert len(created) == 2
    primary_config, _ = created[0]
    fallback_config, _ = created[1]
    assert primary_config.provider == "deepseek"
    assert fallback_config is primary_config.fallback
    assert fallback_config.provider == "senseaudio"
    assert runtime._fallback_model is not None
    assert runtime._fallback_model is not runtime.model
    assert runtime._fallback_model_name == "deepseek-v4-flash-0731"
    assert runtime._primary_model_name == "deepseek-chat"


@pytest.mark.asyncio
async def test_assemble_wiring_empty_config_is_inert(tmp_path):
    """CAPABILITIES 为空 → 零工具、零 provider、零生命周期对象（默认 opt-in）。"""
    _, wiring = await assemble_wiring(_settings(tmp_path))
    assert wiring.tools == []
    assert wiring.context_providers == []
    assert wiring.lifecycle == []
    assert wiring.memory is None
    assert wiring.memory_writer is None


@pytest.mark.asyncio
async def test_initialize_stores_is_idempotent(tmp_path):
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    await initialize_stores(stores)  # 二次调用不得抛错（幂等）


def test_recovery_stores_bundle_holds_three_stores(tmp_path: Path):
    stores = recovery_stores(tmp_path / "harness.db")
    assert stores.operation_ledger is not None
    assert stores.checkpoint_store is not None
    assert stores.session_meta_store is not None
