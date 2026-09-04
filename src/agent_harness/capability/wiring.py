"""wire_capabilities：按显式配置把 capability 接进 Harness（ADR-0010 Q6）。

装配层约定：capability 的 Tool 一律进 ToolRegistry（统一 ToolExecutor 路径，
插件不能绕过 Permission / Operation Ledger，spec 08 §9）；ContextProvider 贡献
进调用方列表。Agent Loop（AgentRuntime）零改动——Gate 1 的结构保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent_harness.capability.base import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityRegistry,
    Degradation,
)
from agent_harness.capability.config import ProviderConfig
from agent_harness.config import Settings


@runtime_checkable
class ContributesTools(Protocol):
    """可选贡献 Protocol：provider 提供一组进统一 ToolRegistry 的工具。"""

    def contributes_tools(self) -> list[Any]: ...


@dataclass
class CapabilityWiring:
    """一次装配的产出：调用方把这些接到 ToolRegistry / ContextBuilder / AgentRuntime。"""

    context_providers: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    memory_writer: Any | None = None
    memory: Any | None = None  # MemoryComponents 生命周期包（relay/writeback），由 AppState 关闭


async def _wire_memory(
    registry: CapabilityRegistry, cfg: ProviderConfig, settings: Settings, wiring: CapabilityWiring,
) -> None:
    from agent_harness.capability.factories import build_memory_components
    from agent_harness.memory.context_provider import MemoryContextProvider

    components = build_memory_components(settings)
    if components is None:
        # 配置不齐 → OPTIONAL_RUNTIME 降级：不注册、不注入（与 Phase 6 行为一致）。
        return
    await components.initialize()
    components.relay.start()
    registry.register(
        CapabilityDescriptor(
            name="memory", version="1.0.0", provider_name=cfg.provider,
            capabilities=["store", "search", "recall"], risk="low",
            supports_concurrency=True, supports_recovery=True,
            degradation=Degradation.OPTIONAL_RUNTIME,
        ),
        components.capability,
    )
    wiring.context_providers.append(MemoryContextProvider(components.capability))
    wiring.memory_writer = components.writeback
    wiring.memory = components


#: 已知 capability 的显式接线表（ADR-0010 Q6：显式装配优于反射式发现）。
_BUILTIN_WIRING: dict[str, Any] = {
    "memory": _wire_memory,
}


async def wire_capabilities(
    registry: CapabilityRegistry,
    config: dict[str, ProviderConfig],
    *,
    settings: Settings,
) -> CapabilityWiring:
    """按 config 驱动 builtin 接线；未知 capability 显式报错（不静默忽略）。

    config 为空 = 零行为变化。返回的 CapabilityWiring 由调用方接到
    ToolRegistry / ContextBuilder / AgentRuntime。
    """
    wiring = CapabilityWiring()
    for name, cfg in config.items():
        factory = _BUILTIN_WIRING.get(name)
        if factory is None:
            raise CapabilityError(
                f"unknown capability '{name}' in CAPABILITIES config "
                f"(known: {sorted(_BUILTIN_WIRING)})",
                code="init_failed",
            )
        if not cfg.enabled:
            continue
        await factory(registry, cfg, settings, wiring)

    # 收集所有已注册 provider 的工具贡献（T5 的 demo capability 走这条路）。
    for descriptor in registry.available():
        if not descriptor.enabled:
            continue
        provider = registry.optional(descriptor.name)
        if isinstance(provider, ContributesTools):
            wiring.tools.extend(provider.contributes_tools())
    return wiring
