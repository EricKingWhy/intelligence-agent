"""build_runtime context_providers 运行时消费（Ticket B2，ADR-0021）。

覆盖验收标准的运行时侧（assembly）：
  - 用户传 ["memory"] → ContextBuilder 只含 MemoryContextProvider（子集）
  - 用户传 [] → ContextBuilder 空（用户显式选了不启用任何 provider）
  - 用户传 None → wiring 全集（向后兼容）
  - wiring 里未知 id（配置降级）→ 跳过 + 不崩溃

测试沿用 test_assembly_agent_profile.py 的装配模式：patch create_chat_model +
手工注册 provider 到 wiring.context_provider_entries。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk

from agent_harness.assembly import build_runtime, initialize_stores, recovery_stores
from agent_harness.capability.wiring import (
    CapabilityWiring,
    ContextProviderEntry,
)
from agent_harness.config import Settings
from agent_harness.sandbox import WorkspaceRegistry


class _FakeProvider:
    """最小 ContextProvider 占位——装配只关心引用相等，不需要真实 select()。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")


class ScriptedModelFactory:
    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(content="ok")


def _wire_two_providers(wiring: CapabilityWiring) -> tuple[_FakeProvider, _FakeProvider]:
    """模拟 wiring 装配了 memory + skills 两个 provider（带稳定 id）。"""
    mem = _FakeProvider("memory")
    skills = _FakeProvider("skills")
    wiring.context_provider_entries["memory"] = ContextProviderEntry(
        id="memory", provider=mem, display_name="Memory", description="recall",
    )
    wiring.context_provider_entries["skills"] = ContextProviderEntry(
        id="skills", provider=skills, display_name="Skills", description="catalog",
    )
    # 保持向后兼容视图同步（register_context_provider 在真实装配里做这事）
    wiring.context_providers.extend([mem, skills])
    return mem, skills


async def _build_runtime(
    tmp_path: Path,
    wiring: CapabilityWiring,
    context_providers: list[str] | None,
):
    settings = _settings(tmp_path)
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    workspace_registry = WorkspaceRegistry(root=tmp_path, backend="local")

    with patch("agent_harness.assembly.create_chat_model", return_value=ScriptedModelFactory()):
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspace_registry,
            session_id="sess-ctx",
            workspace=tmp_path / "workspaces" / "sess-ctx",
            max_steps=10,
            context_providers=context_providers,
        )
    return runtime


@pytest.mark.asyncio
async def test_context_providers_subset_selects_only_memory(tmp_path):
    """验收：context_providers=["memory"] → 只启用 MemoryContextProvider（不含 skills）。"""
    wiring = CapabilityWiring()
    mem, _skills = _wire_two_providers(wiring)
    runtime = await _build_runtime(tmp_path, wiring, context_providers=["memory"])
    providers = runtime._context_builder.context_providers
    assert providers == [mem], f"应只含 memory provider，实际：{providers}"


@pytest.mark.asyncio
async def test_context_providers_empty_list_selects_none(tmp_path):
    """验收：context_providers=[] → ContextBuilder 空（用户显式选了空）。"""
    wiring = CapabilityWiring()
    _wire_two_providers(wiring)
    runtime = await _build_runtime(tmp_path, wiring, context_providers=[])
    providers = runtime._context_builder.context_providers
    assert providers == [], f"空列表应产生空 provider 集合，实际：{providers}"


@pytest.mark.asyncio
async def test_context_providers_none_uses_full_set(tmp_path):
    """验收：context_providers=None → wiring 全集（向后兼容）。"""
    wiring = CapabilityWiring()
    mem, skills = _wire_two_providers(wiring)
    runtime = await _build_runtime(tmp_path, wiring, context_providers=None)
    providers = runtime._context_builder.context_providers
    assert providers == [mem, skills], f"None 应用 wiring 全集，实际：{providers}"


@pytest.mark.asyncio
async def test_context_providers_unknown_id_in_wiring_is_skipped(tmp_path):
    """运行时降级防御：wiring 未装配该 id（配置降级）→ 跳过不崩溃（web 层应先 422）。"""
    wiring = CapabilityWiring()
    mem, _skills = _wire_two_providers(wiring)
    # 用户传了 "memory" + 不存在的 "rag"——只有 memory 命中
    runtime = await _build_runtime(tmp_path, wiring, context_providers=["memory", "rag"])
    providers = runtime._context_builder.context_providers
    assert providers == [mem], f"应跳过未知 id 只留 memory，实际：{providers}"


@pytest.mark.asyncio
async def test_context_providers_order_preserved(tmp_path):
    """用户传 ["skills", "memory"] → 输出按用户指定顺序（不是 wiring 注册顺序）。"""
    wiring = CapabilityWiring()
    mem, skills = _wire_two_providers(wiring)
    runtime = await _build_runtime(tmp_path, wiring, context_providers=["skills", "memory"])
    providers = runtime._context_builder.context_providers
    assert providers == [skills, mem], f"应按用户顺序，实际：{providers}"


@pytest.mark.asyncio
async def test_context_providers_empty_wiring_with_none_yields_empty(tmp_path):
    """边界：wiring 没装配任何 provider + 用户传 None → 空（不伪造）。"""
    wiring = CapabilityWiring()
    runtime = await _build_runtime(tmp_path, wiring, context_providers=None)
    providers = runtime._context_builder.context_providers
    assert providers == [], f"空 wiring + None 应产生空集合，实际：{providers}"
