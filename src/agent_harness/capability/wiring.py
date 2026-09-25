"""wire_capabilities：按显式配置把 capability 接进 Harness（ADR-0010 Q6）。

装配层约定：capability 的 Tool 一律进 ToolRegistry（统一 ToolExecutor 路径，
插件不能绕过 Permission / Operation Ledger，spec 08 §9）；ContextProvider 贡献
进调用方列表。Agent Loop（AgentRuntime）零改动——Gate 1 的结构保证。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agent_harness.capability import factories
from agent_harness.capability.base import (
    CapabilityDescriptor,
    CapabilityError,
    CapabilityRegistry,
    Degradation,
    DegradeReason,
)
from agent_harness.capability.config import ProviderConfig
from agent_harness.config import Settings
from agent_harness.memory.capability import MemoryCapability
from agent_harness.memory.tools import (
    ForgetMemoryTool,
    RememberThisTool,
    RetrieveMemoryTool,
)
from agent_harness.sandbox import WorkspaceRegistry

if TYPE_CHECKING:
    # 只出现在注解里（`wire_capabilities` 的 `sessions` 参数），运行期由调用方传实例。
    from agent_harness.session.store import JsonlSessionStore

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

    # Ticket B2（ADR-0020b）：已装配 context provider 裸 list。
    # web 层 GET 端点投影 wiring.context_providers 的 name 属性；
    # handler 层 422 校验也走裸 list + getattr(p, "name")。
    context_providers: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    #: 工具贡献的第二来源（#159）：当"注册的 provider 必须是契约对象本身"时（memory 的
    #: 描述符注册的就是 `MemoryCapability`，由 seam 测试钉住），不能把注册项换成 wrapper，
    #: 于是在这里挂 wrapper，仍由 `wire_capabilities` 末尾的同一个收集循环收进 `tools`。
    tool_contributors: list[Any] = field(default_factory=list)
    memory_writer: Any | None = None
    memory: Any | None = None  # MemoryComponents 生命周期包（relay/writeback），由 aclose 关闭
    #: V2 记忆形成管线（`memory/v2/assembly.build_memory_formation` 的产物）。它是**终结臂的
    #: 接收端**：`build_runtime` 把它注入 `AgentRuntime(memory_formation=...)`，每轮 run 收尾
    #: 时由它决定该不该入队、并在可见答复交付**之前**把 job 落盘（#298 T7b）。
    #: 它同时是 `lifecycle` 成员（`aclose` 先停泵再排空在飞 job），但它单独留一个字段，
    #: 因为调用方要**按名字**取它注入 runtime，而不是去生命周期列表里按类型翻。
    #: 未装配（模型角色没配 / 调用方没提供会话日志）时恒 `None` ⇒ 终结臂走"没有宿主"的旧路径。
    memory_formation: Any | None = None
    #: V2 SQLite authority / derived index service used by governance routes and commands.
    memory_v2: Any | None = None
    # 通用生命周期对象（提供 aclose()）：如 MCP 连接管理（Phase 8）；由 aclose 关闭。
    lifecycle: list[Any] = field(default_factory=list)
    # Multi-Agent（Phase 13，ADR-0015）：delegate 工具已进 tools，但其依赖
    # （模型链/registry/session store）要等 build_runtime 装配完才能注入——
    # 此处是缓存 wiring 上的 provider prototype；build_runtime 为每个 root Runtime
    # 创建独立实例再激活，descendant runtimes 继承该实例。
    multiagent_provider: Any | None = None
    #: capability 名 → 降级原因（`DegradeReason` 的**值**；写点一律取 `.value`——
    #: 存枚举成员的话，将来任何 `f"{reason}"` 会写出 `DegradeReason.X` 而不是码）。
    #: **只登记"非缺省"的原因**：
    #: "CAPABILITIES 里没有它"（= NOT_CONFIGURED）是缺省状态，不进本表——查表者据此
    #: 把 `None`（或键不存在）读作 NOT_CONFIGURED。一个 capability 因**自身内容为空**
    #: 而缺席（如 mcp 连上了但没有任何工具）也不进本表：那是该能力自己的领域判据，
    #: 它自己的 errors 才是落点。
    #: 存在的意义：装配期只把原因写进 logger.warning，路由层无从区分"没配"与"配了
    #: 但坏了"，只能给一句话（#225）。
    degradations: dict[str, str] = field(default_factory=dict)

    async def aclose(self) -> None:
        """关闭本次装配持有的全部生命周期资源；逐项故障隔离——进程退出路径，
        一项失败不阻断其余清理。"""
        # Formation jobs share the provider's vector store. Stop and drain them before
        # MemoryComponents.close() tears down that store.
        if self.memory_formation is not None:
            try:
                await self.memory_formation.aclose()
            except Exception:
                logger.warning("memory formation 关闭失败（继续其余清理）", exc_info=True)
        if self.memory is not None:
            try:
                await self.memory.close()
            except Exception:
                logger.warning("memory 组件关闭失败（继续其余清理）", exc_info=True)
        for obj in self.lifecycle:
            if obj is self.memory_formation:
                continue
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
    from agent_harness.memory.context_provider import MemoryContextProvider

    # cfg.provider 必须**真的**决定构造哪个 provider（ADR-0024 / ticket #149）：
    # 否则下面的 provider_name=cfg.provider 会让描述符对外声称一个并未生效的实现。
    # 走 `factories.<name>` 属性查找（而非 from-import 绑名），既保持装配期惰性
    # 构造，也让单测能 patch 模块属性换掉 provider。
    components = factories.build_memory_components(settings, provider=cfg.provider)
    if components is None:
        # 配置不齐 → OPTIONAL_RUNTIME 降级：不注册、不注入（与 Phase 6 行为一致）。
        # 原因记进 wiring（#225）：路由层要能对用户说清"缺的是哪些配置"，而不是
        # 一律"请在 CAPABILITIES 中配置 memory"——后者指向的开关本来就是开的。
        wiring.degradations["memory"] = DegradeReason.MISSING_SETTINGS.value
        return
    try:
        # 只调 provider 自己的生命周期入口（ADR-0024 D6）：包内有几个组件、什么顺序，
        # 都是 provider 的实现细节——装配方伸手去 components.relay.start() 的话，
        # 一个不带 `.relay` 的 provider 会被 AttributeError 降级成"没有记忆"。
        await components.initialize()
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
    # BUG-014：检索外层超时从写死 5s 改为 Settings 注入（默认 10s）——代理转发
    # 场景下 5s 比 embedding SDK 的 15s 还紧，必然超时（真机 TimeoutError 实证）。
    wiring.context_providers.append(MemoryContextProvider(
        components.capability, timeout_seconds=settings.memory_search_timeout_seconds,
    ))
    wiring.memory_writer = components.writeback
    wiring.memory = components
    # #159：遗忘工具经契约（MemoryCapability）贡献，收集走末尾的统一循环。
    # #202 / ADR-0031：retrieve_memory 的检索超时与 provider 同一配置口径
    # （settings.memory_search_timeout_seconds）——两处口径漂移就是"两套检索"。
    wiring.tool_contributors.append(_MemoryCapabilityProvider(
        components.capability, timeout_seconds=settings.memory_search_timeout_seconds,
    ))


async def _wire_memory_formation(
    settings: Settings, wiring: CapabilityWiring, *, sessions: JsonlSessionStore | None,
    workspace_index: Any | None = None,
) -> None:
    """在**已活的** V1 记忆之上接出 V2 形成管线（#298 T7b）。

    闸门是**装配结果**而不是配置项：`wiring.memory is not None` 才是"这个进程真的有记忆"
    ——它一次覆盖三种形态（CAPABILITIES 里没有 memory / `enabled: false` 被关掉 / 组件
    没配齐而降级），正是 PRD §5.6.4「关闭记忆 = 同时关掉自动抽取与自动召回」的落点。
    所以这里**不该**再有第二个开关。

    为什么不塞进 `_wire_memory`：那条路的入参形状是"一个 capability 的 cfg + 它的 provider
    名"，而 V2 管线既不吃 cfg 也不认 provider（它只认 `memory.primary` 这个模型角色）；
    硬塞进去就得给 `_BUILTIN_WIRING` 里其余六个 factory 各加一个用不上的参数。

    失败按 OPTIONAL 降级（与 `wire_capabilities` 循环里的同一条纪律）：V2 管线起不来不该
    让整个应用起不来，更不该影响 V1 记忆——那才是此刻在服务用户的路径。
    """
    if wiring.memory is None:
        return
    from agent_harness.memory.v2.assembly import (
        build_memory_formation,
        build_memory_v2_service,
    )
    from agent_harness.model.config import ConfigError

    try:
        vector_store = getattr(wiring.memory, "vectors", None)
        service = await build_memory_v2_service(settings, vector_store=vector_store)
        runner = None
        if sessions is not None:
            runner = await build_memory_formation(
                settings, sessions=sessions, memory_v2=service,
                vector_store=vector_store, workspace_index=workspace_index,
            )
    except ConfigError as error:
        # 配置类故障**响亮上抛**，不降级（T8 两轴审查 P2 修）：`roles.resolve_memory_roles`
        # 自己的契约就是"配错了要响亮"（缺 `MODEL_API_KEY` / provider 名非法都抛
        # `ConfigError`），而 `ConfigError` 不是 `CapabilityError` 的子类 —— 下面那条宽
        # `except Exception` 原本会把它一起吞掉。后果是把"你配错了"降级成"你没配"：
        # 运维看到 `degradations["memory_v2"] = init_failed`，而真正的原因（`AGENT_MODELS`
        # 坏）只留在 traceback 里。与主循环对 `CapabilityError` 的分流（见本文件 `wire_capabilities`
        # 的 `except CapabilityError: raise`）及 `_wire_mcp` 的 `ConfigError → CapabilityError`
        # 是同一条纪律：**降级只留给外部/环境故障**。
        raise CapabilityError(
            f"capability 'memory' 的 V2 形成管线配置错误：{error}", code="init_failed"
        ) from error
    except Exception:
        logger.warning(
            "V2 记忆形成装配失败，按 %s 降级跳过（V1 记忆不受影响）",
            Degradation.OPTIONAL_RUNTIME.value, exc_info=True,
        )
        wiring.degradations["memory_v2"] = DegradeReason.INIT_FAILED.value
        return
    wiring.memory_v2 = service
    for index, provider in enumerate(wiring.context_providers):
        if getattr(provider, "name", None) == "memory":
            wiring.context_providers[index] = _MemorySettingsContextProvider(provider, service)
            break
    if sessions is not None:
        for contributor in wiring.tool_contributors:
            if isinstance(contributor, _MemoryCapabilityProvider):
                contributor.use_v2_commands(
                    service, sessions, workspace_index=workspace_index,
                )
    if runner is None:
        # 没有 formation model 时，持久治理 API 与显式 V2 工具仍可工作。
        service.start_tombstone_purger()
        wiring.lifecycle.append(service)
        return
    wiring.memory_formation = runner
    # 生命周期挂通道：`aclose` 先停泵（此后拒绝新 run）再有界地排空在飞 job。
    wiring.lifecycle.append(runner)
    service.start_tombstone_purger()
    wiring.lifecycle.append(service)


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
        wiring.degradations["knowledge"] = DegradeReason.MISSING_SETTINGS.value
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
        wiring.degradations["knowledge"] = DegradeReason.INIT_FAILED.value
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


class _MemoryCapabilityProvider:
    """ContributesTools 适配（memory，#159 AC4）。

    为什么需要一个**额外的** wrapper 而不是把 `contributes_tools` 塞进
    `LangMemMemoryCapability`：工具的落点必须是"契约 + 唯一执行路径（不变量 #7）"，
    而具体 provider 是可替换的（seam A / ARCH-6）——工具塞进 LangMem 实现就把"遗忘入口"
    绑死在一个 provider 上了。websearch 的做法是把 wrapper **当成**注册的 provider；
    memory 不能照抄：`registry.optional("memory")` 必须**是** capability 本身
    （`test_memory_provider_seam` 钉住这条），所以这个 wrapper 走 `CapabilityWiring.tool_contributors`
    这条**同一个收集循环**的第二个来源（见 `wire_capabilities` 末尾），而不是偷塞进 `wiring.tools`。
    """

    def __init__(self, capability: MemoryCapability, timeout_seconds: float = 10.0) -> None:
        self._capability = capability
        self._timeout_seconds = timeout_seconds
        self._v2_commands: list[Any] | None = None

    def use_v2_commands(
        self, service: Any, sessions: JsonlSessionStore, *, workspace_index: Any | None = None,
    ) -> None:
        from agent_harness.memory.v2.tools import (
            ForgetMemoryV2Tool,
            RememberMemoryV2Tool,
        )

        self._v2_commands = [
            RememberMemoryV2Tool(service, sessions, workspace_index=workspace_index),
            ForgetMemoryV2Tool(service, sessions, workspace_index=workspace_index),
        ]

    def contributes_tools(self) -> list[Any]:
        if self._v2_commands is not None:
            # V1 retrieval remains available until the separate V2 recall adapter is wired.
            return [
                RetrieveMemoryTool(self._capability, timeout_seconds=self._timeout_seconds),
                *self._v2_commands,
            ]
        # #202 / ADR-0031：三个记忆工具（读 retrieve_memory / 写 remember_this /
        # 删 forget_memory）都随 capability 存在而存在（D5）——不写 runtime 特判。
        return [
            RetrieveMemoryTool(self._capability, timeout_seconds=self._timeout_seconds),
            RememberThisTool(self._capability),
            ForgetMemoryTool(self._capability),
        ]


class _MemorySettingsContextProvider:
    """Apply the durable per-user recall switch around the existing provider."""

    name = "memory"

    def __init__(self, provider: Any, service: Any) -> None:
        self._provider = provider
        self._service = service

    async def select(self, session: Any, token_budget: int) -> list[Any]:
        import logging

        from agent_harness.identity import get_identity_context
        from agent_harness.memory.v2.types import TrustedMemoryIdentity

        identity = get_identity_context()
        if "user" not in identity.scopes:
            return []
        try:
            settings = await self._service.get_settings(
                TrustedMemoryIdentity(identity.tenant_id, identity.user_id),
            )
        except Exception as error:  # noqa: BLE001 — optional memory failure must not block a run.
            logging.getLogger(__name__).warning(
                "V2 memory settings unavailable; skipping automatic recall (%s)",
                type(error).__name__,
            )
            return []
        if not settings.recall_enabled:
            return []
        return await self._provider.select(session, token_budget)


class _MultiagentCapabilityProvider:
    """ContributesTools 适配（multiagent）：贡献 delegate 工具。"""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def contributes_tools(self) -> list[Any]:
        return [self._delegate]


async def _wire_multiagent(
    registry: CapabilityRegistry, cfg: ProviderConfig, settings: Settings, wiring: CapabilityWiring,
) -> None:
    """Multi-Agent Capability 接线（Phase 13，ADR-0015 决策 2/4）。

    「一切皆可插件」：multiagent 是 CAPABILITIES 显式 opt-in 的 capability，
    贡献 delegate 工具（经统一 ToolExecutor，不变量 #7）。依赖分两段注入：
    wire 期只造 provider+工具；factory/registry/session store 的激活在
    build_runtime 装配完成后进行（激活前 delegate 调用明确失败）。未在
    CAPABILITIES 配置 = 单代理零感知。
    """
    from agent_harness.multiagent.provider import InProcessSubagentProvider
    from agent_harness.multiagent.tools import DelegateTool

    provider = InProcessSubagentProvider()
    delegate = DelegateTool(provider)
    registry.register(
        CapabilityDescriptor(
            name="multiagent", version="1.0.0", provider_name=cfg.provider,
            capabilities=["tools"], risk="medium",
            supports_concurrency=True, supports_recovery=False, supports_streaming=False,
            degradation=Degradation.OPTIONAL_RUNTIME,
        ),
        _MultiagentCapabilityProvider(delegate),
    )
    # delegate 进工具贡献循环（build_runtime 注册进 ToolRegistry）；
    # provider 引用经 wiring 交给 build_runtime 激活。
    wiring.multiagent_provider = provider


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
        wiring.degradations["websearch"] = DegradeReason.MISSING_SETTINGS.value
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
    "multiagent": (_wire_multiagent, Degradation.OPTIONAL_RUNTIME),
}


#: 每个 capability 接受的 provider 名。`"builtin"` 恒合法（= 该能力的内置 factory）；
# 其余是 factory 认的显式别名。config 写了既非 builtin 也非已知别名的 provider 时
# 显式失败（08 §5：不允许"接受但静默忽略"）——注意这是装配期直接抛错，不走降级：
# 配置写错属于用户必须修的错误。
#: **memory 不在这张表里**：它的白名单直接问 factory 分派表（`_known_providers()`，
#: ADR-0024），使「接受的 provider 名」与「真有实现的 provider 名」结构上是同一集合。
_STATIC_KNOWN_PROVIDERS: dict[str, set[str]] = {
    "skills": {"builtin"},
    "ticker": {"builtin"},
    "mcp": {"builtin"},
    "knowledge": {"builtin"},
    "websearch": {"builtin"},
    "multiagent": {"builtin"},
}


def _known_providers(capability: str) -> set[str]:
    """该 capability 接受的 provider 名集合（装配期白名单的唯一查询入口）。"""
    if capability == "memory":
        return factories.memory_provider_names()
    known = _STATIC_KNOWN_PROVIDERS.get(capability)
    if known is None:
        # 两张按 capability 名索引的表（_BUILTIN_WIRING / _STATIC_KNOWN_PROVIDERS）
        # 必须同键。漏登记时返回空集会让报错变成 "known: []"（把装配表漏项说成
        # "这个 provider 不存在"）——直接指认装配表不一致。
        raise CapabilityError(
            f"capability '{capability}' 有 wiring 但没有 provider 白名单（装配表不一致）",
            code="init_failed",
        )
    return known


async def wire_capabilities(
    registry: CapabilityRegistry,
    config: dict[str, ProviderConfig],
    *,
    settings: Settings,
    sessions: JsonlSessionStore | None = None,
    workspace_index: Any | None = None,
) -> CapabilityWiring:
    """按 config 驱动 builtin 接线；未知 capability / 未知 provider 显式报错（不静默忽略）。

    config 为空 = 零行为变化。返回的 CapabilityWiring 由调用方接到
    ToolRegistry / ContextBuilder / AgentRuntime。
    OPTIONAL capability 的 factory 失败（外部依赖故障等）降级为跳过并记 warning——
    失败的能力不会出现在 Registry 里，Consumer 走 optional() 的 None 降级路径
    （08 §7 验收：Optional Provider 故障可以降级）；REQUIRED_CORE 则向上抛。

    `sessions`（#298 T7b）：运行时空在写的那个会话日志存储。装配 V2 记忆形成管线要它
    （执行 job 时按 `(session_id, run_id)` 从日志切这一轮的事件）。**不提供 = 不装配 V2
    形成**（`memory_formation` 恒 None）：测试与不跑 V2 的调用方因此零改动，而生产的两处
    调用点（`web/app.py` 的 `get_wiring` / `assemble_wiring`）各自把自己手上的 store 传进来。
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
        if cfg.provider not in _known_providers(name):
            raise CapabilityError(
                f"unknown provider '{cfg.provider}' for capability '{name}' "
                f"(known: {sorted(_known_providers(name))})",
                code="init_failed",
            )
        factory, degradation = entry
        if not cfg.enabled:
            wiring.degradations[name] = DegradeReason.DISABLED.value
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
            wiring.degradations[name] = DegradeReason.INIT_FAILED.value
            continue

    # V2 service and governance commands depend on the initialized memory provider;
    # wire them before the common contributor collection loop.
    await _wire_memory_formation(
        settings, wiring, sessions=sessions, workspace_index=workspace_index,
    )

    # 收集工具贡献：已启用的 provider（demo capability 走这条）**加上**第二来源
    # `tool_contributors`（#159）。一个循环、一条 `isinstance` 规则、零旁路——没有任何
    # 地方直接往 `wiring.tools` 里 append。
    contributors: list[Any] = [
        registry.optional(descriptor.name)
        for descriptor in registry.available()
        if descriptor.enabled
    ]
    contributors.extend(wiring.tool_contributors)
    for contributor in contributors:
        if isinstance(contributor, ContributesTools):
            wiring.tools.extend(contributor.contributes_tools())

    return wiring
