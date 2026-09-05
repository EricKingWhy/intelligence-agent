"""wire_capabilities：按显式配置把 capability 接进 Harness（ADR-0010 Q6）。

装配层约定：capability 的 Tool 一律进 ToolRegistry（统一 ToolExecutor 路径，
插件不能绕过 Permission / Operation Ledger，spec 08 §9）；ContextProvider 贡献
进调用方列表。Agent Loop（AgentRuntime）零改动——Gate 1 的结构保证。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agent_harness.capability.base import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityRegistry,
    Degradation,
)
from agent_harness.capability.config import ProviderConfig
from agent_harness.config import Settings

logger = logging.getLogger(__name__)


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
    # 通用生命周期对象（提供 aclose()）：如 MCP 连接管理（Phase 8）；由 AppState 关闭。
    lifecycle: list[Any] = field(default_factory=list)


async def _wire_memory(
    registry: CapabilityRegistry, cfg: ProviderConfig, settings: Settings, wiring: CapabilityWiring,
) -> None:
    from agent_harness.capability.factories import build_memory_components
    from agent_harness.memory.context_provider import MemoryContextProvider

    components = build_memory_components(settings)
    if components is None:
        # 配置不齐 → OPTIONAL_RUNTIME 降级：不注册、不注入（与 Phase 6 行为一致）。
        return
    try:
        await components.initialize()
        components.relay.start()
    except Exception:
        # 半初始化失败（Milvus 连上后 schema/探测挂）时，已构造的 gRPC channel /
        # httpx client 必须显式关闭——外层 wire_capabilities 只会降级跳过，不会
        # 关闭 components；wiring.memory 未设置意味着 AppState.shutdown 也够不到，
        # 不关就是永久泄漏（对故障 Milvus 的后台重连永不停止）。
        try:
            await components.close()
        except Exception as close_error:  # noqa: BLE001 — 清理失败不掩盖原始故障
            logger.warning("memory components 清理失败：%r", close_error)
        raise
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


def _coerce_path_list(cfg: ProviderConfig, key: str) -> list[Path]:
    """规整 options 里的目录/路径选项为 list[Path]，容忍 str/Path 单值写法。

    options 是 dict[str, Any]，strict 校验不查值：`"directories": "D:/skills"`
    这种常见手误若直接迭代会按字符产出 Path("D")、Path(":")……而不存在的路径
    又被当作 OPTIONAL 语义静默跳过 → 技能悄悄消失。字符串/Path 包一层成单元素
    列表；不可迭代的垃圾值（int 等）显式 init_failed——配置写错必须响亮失败，
    与本文件里 unknown capability / provider 的显式校验一致，不走静默降级。
    """
    value = cfg.options.get(key, [])
    if isinstance(value, (str, Path)):
        value = [value]
    # dict 会按键迭代（{"a": 1} → Path("a")）——与按字符迭代同属静默错误，显式拒绝。
    if not isinstance(value, (list, tuple)):
        raise CapabilityError(
            f"capability 'skills' option '{key}' must be a path or a list of paths, got {value!r}",
            code="init_failed",
        )
    try:
        return [Path(item) for item in value]
    except TypeError:
        raise CapabilityError(
            f"capability 'skills' option '{key}' must be a path or a list of paths, got {value!r}",
            code="init_failed",
        ) from None


async def _wire_skills(
    registry: CapabilityRegistry, cfg: ProviderConfig, settings: Settings, wiring: CapabilityWiring,
) -> None:
    from agent_harness.skills.capability import SkillCapability
    from agent_harness.skills.context_provider import SkillCatalogContextProvider
    from agent_harness.skills.discovery import SkillDiscovery

    # 全局目录（spec 09 §2）+ 项目目录（workspace 级）+ options 扩展目录/手动路径。
    global_dir = Path(settings.skill_global_dir) if settings.skill_global_dir \
        else Path.home() / ".intelligence-agent" / "skills"
    directories = [global_dir, Path(settings.workspace_dir) / "skills"]
    directories.extend(_coerce_path_list(cfg, "directories"))
    manual_paths = _coerce_path_list(cfg, "paths")
    catalog = SkillDiscovery(directories=directories, manual_paths=manual_paths).discover()
    # 解析失败可观察（ADR-0011 Q1：不静默跳过）——坏 SKILL.md 在装配日志里留痕，
    # SkillCapability.errors() 仍可编程读取。
    if catalog.errors:
        logger.warning("skill 发现阶段有 %d 个解析错误：%s", len(catalog.errors), catalog.errors)
    capability = SkillCapability(catalog)
    registry.register(
        CapabilityDescriptor(
            name="skills", version="1.0.0", provider_name=cfg.provider,
            capabilities=["catalog", "load"], risk="low",
            supports_concurrency=True, supports_recovery=False, supports_streaming=False,
            degradation=Degradation.OPTIONAL_RUNTIME,
        ),
        capability,
    )
    wiring.context_providers.append(SkillCatalogContextProvider(capability))
    # load_skill 不在这里 append：SkillCapability 实现 ContributesTools，
    # 与其他工具贡献统一走 wire_capabilities 末尾的收集循环。


async def _wire_mcp(
    registry: CapabilityRegistry, cfg: ProviderConfig, settings: Settings, wiring: CapabilityWiring,
) -> None:
    """MCP Client 接线（Phase 8，ADR-0012）：配置解析 → 逐 server 连接 → discovery。

    失败语义（Q10）：schema 非法 = 配置错误 → CapabilityError(init_failed) 响亮
    失败（不做 ZCode 式静默丢弃）；连接失败 = 环境错误 → 该 server 降级缺席
    （errors 可观察），其余 server 不受影响；全部不可达 → 整个 capability 跳过。
    """
    from agent_harness.mcp.capability import build_mcp_capability
    from agent_harness.mcp.config import ConfigError, parse_mcp_servers

    try:
        servers = parse_mcp_servers(cfg.options)
    except ConfigError as error:
        raise CapabilityError(
            f"capability 'mcp' 配置错误：{error}", code="init_failed"
        ) from error
    servers = [server for server in servers if server.enabled]

    capability = await build_mcp_capability(servers)
    if not capability.contributes_tools():
        detail = "; ".join(capability.errors) if capability.errors else "无可用 server"
        logger.warning("capability 'mcp' 无任何可用工具，按 %s 降级跳过：%s",
                       Degradation.OPTIONAL_RUNTIME.value, detail)
        return
    if capability.errors:
        logger.warning("capability 'mcp' 部分降级：%s", capability.errors)

    registry.register(
        CapabilityDescriptor(
            name="mcp", version="1.0.0", provider_name=cfg.provider,
            capabilities=["tools"], risk="high",
            supports_concurrency=True, supports_recovery=False, supports_streaming=False,
            degradation=Degradation.OPTIONAL_RUNTIME,
        ),
        capability,
    )
    # 连接生命周期：AppState.shutdown 统一关闭（通用 lifecycle 通道）。
    wiring.lifecycle.extend(capability._connections)
    # 工具贡献走 wire_capabilities 末尾的 ContributesTools 收集循环（零旁路）。


async def _wire_ticker(
    registry: CapabilityRegistry, cfg: ProviderConfig, settings: Settings, wiring: CapabilityWiring,
) -> None:
    from agent_harness.capability.demo import TickerCapability

    registry.register(
        CapabilityDescriptor(
            name="ticker", version="1.0.0", provider_name=cfg.provider,
            capabilities=["tick"], risk="low",
            supports_concurrency=True, supports_recovery=False, supports_streaming=False,
            degradation=Degradation.OPTIONAL_RUNTIME,
        ),
        TickerCapability(),
    )


#: 已知 capability 的显式接线表（ADR-0010 Q6：显式装配优于反射式发现）。
#: 值带该能力声明的降级档位：装配期 factory 失败时，OPTIONAL 按档降级跳过，
#: REQUIRED_CORE 显式失败——08 §7 的三分类在装配边界落地。
_BUILTIN_WIRING: dict[str, tuple[Any, Degradation]] = {
    "memory": (_wire_memory, Degradation.OPTIONAL_RUNTIME),
    "skills": (_wire_skills, Degradation.OPTIONAL_RUNTIME),
    "ticker": (_wire_ticker, Degradation.OPTIONAL_RUNTIME),
    "mcp": (_wire_mcp, Degradation.OPTIONAL_RUNTIME),
}


#: 每个 capability 接受的 provider 名。`"builtin"` 恒合法（= 该能力的内置 factory）；
# 其余是 factory 认的显式别名（memory 的内置 factory 即 LangMem 实现）。config 写了
# 既非 builtin 也非已知别名的 provider 时显式失败（08 §5：不允许"接受但静默忽略"）
# ——注意这是装配期直接抛错，不走降级：配置写错属于用户必须修的错误。
_KNOWN_PROVIDERS: dict[str, set[str]] = {
    "memory": {"builtin", "langmem"},
    "skills": {"builtin"},
    "ticker": {"builtin"},
    "mcp": {"builtin"},
}


async def wire_capabilities(
    registry: CapabilityRegistry,
    config: dict[str, ProviderConfig],
    *,
    settings: Settings,
) -> CapabilityWiring:
    """按 config 驱动 builtin 接线；未知 capability / 未知 provider 显式报错（不静默忽略）。

    config 为空 = 零行为变化。返回的 CapabilityWiring 由调用方接到
    ToolRegistry / ContextBuilder / AgentRuntime。
    OPTIONAL capability 的 factory 失败（外部依赖故障等）降级为跳过并记 warning——
    失败的能力不会出现在 Registry 里，Consumer 走 optional() 的 None 降级路径
    （08 §7 验收：Optional Provider 故障可以降级）；REQUIRED_CORE 则向上抛。
    """
    wiring = CapabilityWiring()
    for name, cfg in config.items():
        entry = _BUILTIN_WIRING.get(name)
        if entry is None:
            raise CapabilityError(
                f"unknown capability '{name}' in CAPABILITIES config "
                f"(known: {sorted(_BUILTIN_WIRING)})",
                code="init_failed",
            )
        if cfg.provider not in _KNOWN_PROVIDERS[name]:
            raise CapabilityError(
                f"unknown provider '{cfg.provider}' for capability '{name}' "
                f"(known: {sorted(_KNOWN_PROVIDERS[name])})",
                code="init_failed",
            )
        factory, degradation = entry
        if not cfg.enabled:
            continue
        try:
            await factory(registry, cfg, settings, wiring)
        except CapabilityError:
            # CapabilityError 是配置/契约错误（init_failed 等，见 config.py 的同类校验），
            # 不是"外部依赖故障"——响亮失败，不做 OPTIONAL 降级（降级只留给外部故障）。
            raise
        except Exception as error:
            if degradation is Degradation.REQUIRED_CORE:
                raise
            logger.warning(
                "capability '%s' 初始化失败，按 %s 降级跳过：%r",
                name, degradation.value, error,
            )
            continue

    # 收集所有已注册 provider 的工具贡献（demo capability 走这条路）。
    for descriptor in registry.available():
        if not descriptor.enabled:
            continue
        provider = registry.optional(descriptor.name)
        if isinstance(provider, ContributesTools):
            wiring.tools.extend(provider.contributes_tools())
    return wiring
