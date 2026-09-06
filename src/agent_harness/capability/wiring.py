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
from agent_harness.sandbox import WorkspaceRegistry

logger = logging.getLogger(__name__)


@runtime_checkable
class ContributesTools(Protocol):
    """可选贡献 Protocol：provider 提供一组进统一 ToolRegistry 的工具。"""

    def contributes_tools(self) -> list[Any]: ...


@dataclass
class CapabilityWiring:
    """一次装配的产出：调用方把这些接到 ToolRegistry / ContextBuilder / AgentRuntime。

    本对象同时是装配产物的 lifecycle owner：aclose() 关闭 memory 组件与
    lifecycle 通道（关闭知识收拢在创建者，web 层只管 get / shutdown）。
    """

    context_providers: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    memory_writer: Any | None = None
    memory: Any | None = None  # MemoryComponents 生命周期包（relay/writeback），由 aclose 关闭
    # 通用生命周期对象（提供 aclose()）：如 MCP 连接管理（Phase 8）；由 aclose 关闭。
    lifecycle: list[Any] = field(default_factory=list)

    async def aclose(self) -> None:
        """关闭本次装配持有的全部生命周期资源；逐项故障隔离——进程退出路径，
        一项失败不阻断其余清理。"""
        if self.memory is not None:
            try:
                await self.memory.close()
            except Exception:
                logger.warning("memory 组件关闭失败（继续其余清理）", exc_info=True)
        for obj in self.lifecycle:
            aclose = getattr(obj, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception:
                logger.warning("lifecycle 关闭失败（%s），继续其余清理",
                               type(obj).__name__, exc_info=True)


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
    # 连接生命周期先挂通道（AppState.shutdown 统一关闭；capability.aclose
    # 关闭其全部连接——公共接口，不伸私有属性）。必须在任何早退之前：连上了
    # 但 tools/list 为空的 server（mis-scoped token 常见症状）走"无工具降级
    # 跳过"分支时，连接也必须能被 shutdown 关闭——否则 owner task 与 stdio
    # 子进程泄漏到进程退出。
    wiring.lifecycle.append(capability)
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
    # 工具贡献走 wire_capabilities 末尾的 ContributesTools 收集循环（零旁路）。


async def _wire_knowledge(
    registry: CapabilityRegistry, cfg: ProviderConfig, settings: Settings, wiring: CapabilityWiring,
) -> None:
    """Knowledge Capability 接线（Phase 11，ADR-0013）。

    失败语义（Q10）：KNOWLEDGE_COLLECTION 未配置 = OPTIONAL_RUNTIME 缺席
    降级（警告可观察）；store 连接/schema 故障 = 环境错误同样降级缺席；
    其余配置错误（provider 名等）在 wire_capabilities 上游响亮失败。
    """
    from agent_harness.knowledge.milvus_store import MilvusKnowledgeVectorStore
    from agent_harness.knowledge.registry import SqliteKnowledgeSourceRegistry
    from agent_harness.knowledge.service import KnowledgeService
    from agent_harness.knowledge.tools import (
        IngestDocumentTool,
        ReadKnowledgeSourceTool,
        RetrieveKnowledgeTool,
    )

    if not settings.knowledge_collection:
        logger.warning(
            "capability 'knowledge' 未配置 KNOWLEDGE_COLLECTION，按 %s 降级缺席",
            Degradation.OPTIONAL_RUNTIME.value,
        )
        return

    try:
        from agent_harness.memory.embeddings import create_embeddings

        store = MilvusKnowledgeVectorStore(
            settings,
            create_embeddings(settings) if settings.embedding_model else None,
        )
        await store.initialize()
        source_registry = SqliteKnowledgeSourceRegistry(
            Path(settings.workspace_dir) / "harness.db"
        )
        await source_registry.initialize()
    except Exception as error:  # noqa: BLE001 — 环境故障（连接/维度/schema）按档降级
        logger.warning(
            "capability 'knowledge' 初始化失败（%s: %s），按 %s 降级缺席",
            type(error).__name__, error, Degradation.OPTIONAL_RUNTIME.value,
        )
        return

    service = KnowledgeService(
        store=store, registry=source_registry,
        min_score=settings.knowledge_min_score,
    )
    workspace_registry = WorkspaceRegistry(root=Path(settings.workspace_dir))
    tools = [
        RetrieveKnowledgeTool(service),
        ReadKnowledgeSourceTool(service),
        IngestDocumentTool(service, workspace_registry),
    ]

    registry.register(
        CapabilityDescriptor(
            name="knowledge", version="1.0.0", provider_name=cfg.provider,
            capabilities=["tools"], risk="medium",
            supports_concurrency=True, supports_recovery=False, supports_streaming=False,
            degradation=Degradation.OPTIONAL_RUNTIME,
        ),
        _KnowledgeCapabilityProvider(tools, store),
    )
    # 向量 client 的关闭走 lifecycle 通道（批次 A 候选 4 的通用出口）。
    wiring.lifecycle.append(store)
    # 工具贡献走 wire_capabilities 末尾的 ContributesTools 收集循环（零旁路）。


class _KnowledgeCapabilityProvider:
    """ContributesTools 适配：工具列表经统一 ToolRegistry 进 Executor（不变量 #7）。"""

    def __init__(self, tools: list[Any], store: Any) -> None:
        self._tools = tools
        self._store = store

    def contributes_tools(self) -> list[Any]:
        return list(self._tools)


class _WebSearchCapabilityProvider:
    """ContributesTools 适配（websearch）：无状态 provider，仅贡献工具。"""

    def __init__(self, tools: list[Any]) -> None:
        self._tools = tools

    def contributes_tools(self) -> list[Any]:
        return list(self._tools)


async def _wire_websearch(
    registry: CapabilityRegistry, cfg: ProviderConfig, settings: Settings, wiring: CapabilityWiring,
) -> None:
    """Web Search Capability 接线（Phase 12，ADR-0014 决策 8/10）。

    失败语义：TAVILY_API_KEY 未配置 = OPTIONAL_RUNTIME 缺席降级（警告可
    观察）——不配 = 不联网，绝不静默触网。provider 无状态（per-request
    httpx client），无生命周期资源。知识库证据不足时的降级引导由
    retrieve_knowledge 的 hint 承担（tool 侧 affordance，决策 13）。
    """
    from agent_harness.websearch.tavily import TavilyWebSearchProvider
    from agent_harness.websearch.tools import WebSearchTool

    api_key = settings.tavily_api_key.get_secret_value()
    if not api_key.strip():
        logger.warning(
            "capability 'websearch' 未配置 TAVILY_API_KEY，按 %s 降级缺席",
            Degradation.OPTIONAL_RUNTIME.value,
        )
        return

    provider = TavilyWebSearchProvider(api_key)
    tools = [WebSearchTool(provider)]
    registry.register(
        CapabilityDescriptor(
            name="websearch", version="1.0.0", provider_name=cfg.provider,
            capabilities=["tools"], risk="low",
            supports_concurrency=True, supports_recovery=False, supports_streaming=False,
            degradation=Degradation.OPTIONAL_RUNTIME,
        ),
        _WebSearchCapabilityProvider(tools),
    )
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
    "knowledge": (_wire_knowledge, Degradation.OPTIONAL_RUNTIME),
    "websearch": (_wire_websearch, Degradation.OPTIONAL_RUNTIME),
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
    "knowledge": {"builtin"},
    "websearch": {"builtin"},
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
