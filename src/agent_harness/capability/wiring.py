"""wire_capabilities：按显式配置把 capability 接进 Harness（ADR-0010 Q6）。

装配层约定：capability 的 Tool 一律进 ToolRegistry（统一 ToolExecutor 路径，
插件不能绕过 Permission / Operation Ledger，spec 08 §9）；ContextProvider 贡献
进调用方列表。Agent Loop（AgentRuntime）零改动——Gate 1 的结构保证。
"""

from __future__ import annotations

import logging
import re
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
from agent_harness.model.concurrency import ModelCallGate
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
    #: 工具贡献的第二来源（#159）：由 wiring 挂载的工具仍走统一收集循环。
    tool_contributors: list[Any] = field(default_factory=list)
    memory_provider: str | None = None
    memory_vectors: Any | None = None
    #: V2 记忆形成管线（`memory/v2/assembly.build_memory_formation` 的产物）。它是**终结臂的
    #: 接收端**：`build_runtime` 把它注入 `AgentRuntime(memory_formation=...)`，每轮 run 收尾
    #: 时由它决定该不该入队、并在可见答复交付**之前**把 job 落盘（#298 T7b）。
    #: 它同时是 `lifecycle` 成员（`aclose` 先停泵再排空在飞 job），但它单独留一个字段，
    #: 因为调用方要**按名字**取它注入 runtime，而不是去生命周期列表里按类型翻。
    #: 未装配（模型角色没配 / 调用方没提供会话日志）时恒 `None` ⇒ 终结臂走"没有宿主"的旧路径。
    memory_formation: Any | None = None
    #: Shared V2 authority/index service for recall, commands, and governance routes.
    memory_v2: Any | None = None
    #: 进程级模型并发闸（#89 / #559 修复）：闸的语义是「全局在飞模型调用数」，
    #: 归属**装配生命周期**（wiring 是一次装配的产物与 lifecycle owner）——同一
    #: 进程内 web / CLI 各自装配一次，所有 build_runtime（跨 Session / child /
    #: fallback / 摘要）经这里共享同一实例。由 `wire_capabilities` 建一次；
    #: 手搓 `CapabilityWiring()`（无装配生命周期）时保持 None，build_runtime
    #: 退回每次新建（历史行为）。
    model_call_gate: ModelCallGate | None = None
    # 通用生命周期对象（提供 aclose()）：如 MCP 连接管理（Phase 8）；由 aclose 关闭。
    lifecycle: list[Any] = field(default_factory=list)
    # Multi-Agent（Phase 13，ADR-0015）：delegate 工具已进 tools，但其依赖
    # （模型链/registry/session store）要等 build_runtime 装配完才能注入——
    # 此处是缓存 wiring 上的 provider prototype；build_runtime 为每个 root Runtime
    # 创建独立实例再激活，descendant runtimes 继承该实例。
    multiagent_provider: Any | None = None
    #: Skill capability（#529 T-529-5）：Web 只读 catalog 展示面经 `get_wiring()`
    #: 取用；未装配 skills 时为 None（路由层 503 如实降级）。
    skills: Any | None = None
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
        # Formation jobs share the vector client. Stop and drain them before it closes.
        if self.memory_formation is not None:
            try:
                await self.memory_formation.aclose()
            except Exception:
                logger.warning("memory formation 关闭失败（继续其余清理）", exc_info=True)
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
    """Prepare the shared vector client; V2 owns all active memory behavior."""
    if not cfg.enabled:
        return
    if not (
        settings.milvus_uri
        and settings.milvus_token.get_secret_value()
        and settings.milvus_collection
        and settings.embedding_model
        and settings.embedding_base_url
        and settings.embedding_api_key.get_secret_value()
    ):
        wiring.degradations["memory"] = DegradeReason.MISSING_SETTINGS.value
        return
    target = settings.milvus_collection
    if (
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target)
        or (settings.knowledge_collection and
            target.casefold() == settings.knowledge_collection.casefold())
    ):
        raise CapabilityError(
            "memory collection target is invalid or conflicts with Knowledge",
            code="init_failed",
        )

    vectors = factories.build_memory_vector_client(settings)
    try:
        await vectors.initialize()
    except Exception:
        try:
            await vectors.close()
        except Exception:
            logger.warning("memory vector client cleanup failed", exc_info=True)
        raise
    wiring.memory_provider = cfg.provider
    wiring.memory_vectors = vectors


async def _wire_memory_v2(
    registry: CapabilityRegistry, settings: Settings, wiring: CapabilityWiring,
    *, sessions: JsonlSessionStore | None,
    workspace_index: Any | None = None,
) -> None:
    """Wire V2 as the exclusive memory service, index client, and optional formation."""
    vectors = wiring.memory_vectors
    if wiring.memory_provider is None or vectors is None:
        return
    from agent_harness.model.config import ConfigError

    try:
        # Keep optional imports inside the degradation boundary.
        from agent_harness.memory.v2.assembly import (
            build_memory_formation,
            build_memory_v2_service,
        )
        from agent_harness.memory.v2.recall import MemoryV2ContextProvider
        from agent_harness.memory.v2.roles import resolve_memory_roles
        from agent_harness.memory.v2.search_tool import RetrieveMemoryV2Tool

        service = await build_memory_v2_service(settings, vector_store=vectors)
        if sessions is None:
            runner = None
        else:
            roles = resolve_memory_roles(settings)
            runner = await build_memory_formation(
                settings, sessions=sessions, memory_v2=service,
                vector_store=vectors, workspace_index=workspace_index, roles=roles,
            )
    except ConfigError as error:
        await vectors.close()
        wiring.memory_vectors = None
        raise CapabilityError(
            f"capability 'memory' 的 V2 形成管线配置错误：{error}", code="init_failed"
        ) from error
    except Exception:
        try:
            await vectors.close()
        except Exception:
            logger.warning("memory vector client cleanup failed", exc_info=True)
        wiring.memory_vectors = None
        logger.warning(
            "V2 记忆装配失败，按 %s 降级跳过",
            Degradation.OPTIONAL_RUNTIME.value, exc_info=True,
        )
        wiring.degradations["memory"] = DegradeReason.INIT_FAILED.value
        wiring.degradations["memory_v2"] = DegradeReason.INIT_FAILED.value
        return

    registry.register(
        CapabilityDescriptor(
            name="memory", version="2.0.0", provider_name="builtin-v2",
            capabilities=["store", "search", "recall"], risk="low",
            supports_concurrency=True, supports_recovery=True,
            degradation=Degradation.OPTIONAL_RUNTIME,
        ),
        service,
    )
    wiring.memory_v2 = service
    if sessions is not None:
        v2_context = MemoryV2ContextProvider(
            service, workspace_index=workspace_index,
            timeout_seconds=settings.memory_search_timeout_seconds,
        )
        wiring.context_providers.append(_MemorySettingsContextProvider(v2_context, service))
    if sessions is not None:
        from agent_harness.memory.v2.tools import (
            ForgetMemoryV2Tool,
            RememberMemoryV2Tool,
        )

        wiring.tool_contributors.append(_ToolsProvider([
            RememberMemoryV2Tool(service, sessions, workspace_index=workspace_index),
            ForgetMemoryV2Tool(service, sessions, workspace_index=workspace_index),
            RetrieveMemoryV2Tool(
                service, workspace_index=workspace_index,
                timeout_seconds=settings.memory_search_timeout_seconds,
            ),
        ]))
    service.start_tombstone_purger()
    wiring.lifecycle.append(service)
    wiring.lifecycle.append(vectors)
    if runner is not None:
        wiring.memory_formation = runner
        # Stop/drain the runner before the shared vector provider is closed.
        wiring.lifecycle.append(runner)

def coerce_skill_path_list(cfg: ProviderConfig, key: str) -> list[Path]:
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
        return [Path(item).expanduser() for item in value]
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
    from agent_harness.skills.package_manager import (
        SkillManifestError,
        SkillPackageError,
        SkillPackageManager,
    )

    # 全局目录（spec 09 §2）+ 项目目录（workspace 级）+ options 扩展目录/手动路径。
    global_dir = Path(settings.skill_global_dir).expanduser() if settings.skill_global_dir \
        else Path.home() / ".intelligence-agent" / "skills"
    project_dir = Path(settings.workspace_dir).expanduser() / "skills"
    additional_directories = coerce_skill_path_list(cfg, "directories")
    manual_paths = coerce_skill_path_list(cfg, "paths")
    package_manager = SkillPackageManager(
        settings.workspace_dir,
        global_skills_dir=global_dir,
        additional_skill_directories=additional_directories,
        additional_skill_paths=manual_paths,
    )
    global_package_manager = SkillPackageManager(
        settings.workspace_dir,
        global_skills_dir=global_dir,
        scope="global",
    )
    managed_dir = package_manager.managed_skills_dir
    try:
        global_package_manager.apply_pending_versions()
    except SkillPackageError as error:
        logger.warning(
            "pending global Skill package versions were not applied: %s",
            error,
        )
    try:
        package_manager.apply_pending_versions()
    except SkillPackageError as error:
        logger.warning(
            "pending managed Skill versions were not applied; installed versions remain selected: %s",
            error,
        )
    selection_errors: list[str] = []
    try:
        enabled_managed_skill_digests = package_manager.enabled_skill_digests()
    except SkillPackageError as error:
        # 与全局侧同一条降级契约：整个受管根缺席 + 留痕，但**不**静默。
        # 混装（有的项目包坏了）不是可选项——受管根是单一命名空间，部分装配
        # 只会让「谁生效」变得不可解释；项目版失效时也**不**回落到全局版。
        # 标签分两类（与全局侧同判据）：清单读不出来是盘上状态坏了，不是用户
        # 的选择失效——两者要用户做的事完全不同。
        logger.warning("managed Skills are unavailable; imported Skills stay disabled: %s", error)
        enabled_managed_skill_digests = {}
        label = (
            "Skill package storage is unreadable"
            if isinstance(error, SkillManifestError)
            else "managed Skill package selection cannot be honoured"
        )
        # 受管根是全有全无：一条坏掉，**其余受管包一起缺席**。不说这句，用户会以为
        # 坏的只有异常消息里那一个、其余照常运行。
        selection_errors.append(
            f"{label}: {error} (every managed Skill stays disabled until this is fixed)"
        )
    try:
        # #874 T5：全局安装根与自动发现根已分开（T4），但「装了什么」不等于「谁能装配」。
        # 只有本项目显式选择 global 的包才进 catalog；被选版本失效时**不回落**项目版，
        # 该全局贡献降级缺席并在此留痕（spec 08 §6.3：导入包不得改变 Agent Core 的
        # 启动条件，所以是响亮告警 + 缺席，不是装配失败）。
        enabled_global_skill_digests = package_manager.enabled_global_skill_digests()
    except SkillPackageError as error:
        logger.warning(
            "this project's global Skill package selection cannot be honoured; "
            "those Skills stay disabled (no fallback to project scope): %s",
            error,
        )
        enabled_global_skill_digests = {}
        # 清单损坏也会走到这里。标签分开写：把「清单读不出来」说成「选择失效」
        # 会指控一个用户没做过的动作，而这两者要用户做的事完全不同。
        prefix = (
            "Skill package storage is unreadable"
            if isinstance(error, SkillManifestError)
            else "global Skill package selection cannot be honoured"
        )
        selection_errors.append(f"{prefix}: {error}")
    global_managed_dir = global_package_manager.managed_skills_dir
    directories = [global_dir, project_dir, managed_dir]
    if enabled_global_skill_digests:
        directories.append(global_managed_dir)
    directories.extend(additional_directories)
    # #529：discovery 引用传给 capability（不再是装配期静态 catalog）——
    # project_dir 是闭环写入面，沉淀 register/update/remove 写它并内嵌刷新。
    discovery = SkillDiscovery(
        directories=directories,
        manual_paths=manual_paths,
        project_dir=project_dir,
        managed_directories={
            "project": managed_dir,
            "global": global_managed_dir,
        },
        enabled_managed_digests={
            "project": enabled_managed_skill_digests,
            "global": enabled_global_skill_digests,
        },
        selection_errors=selection_errors,
    )
    catalog = discovery.discover()
    # 解析失败可观察（ADR-0011 Q1：不静默跳过）——坏 SKILL.md 在装配日志里留痕，
    # SkillCapability.errors() 仍可编程读取。
    if catalog.errors:
        logger.warning("skill 发现阶段有 %d 个错误：%s", len(catalog.errors), catalog.errors)
    # #529 T-529-5：沉淀状态机装配（staging 在 project skill 目录第二层，单层
    # 扫描不可见——未确认草稿结构上进不了 catalog）。
    from agent_harness.skills.promote import SkillPromoter

    promoter = SkillPromoter(discovery, staging_root=project_dir / ".staging")
    capability = SkillCapability(discovery, promoter=promoter)
    registry.register(
        CapabilityDescriptor(
            name="skills", version="1.1.0", provider_name=cfg.provider,
            capabilities=["catalog", "load", "promote"], risk="medium",
            supports_concurrency=True, supports_recovery=False, supports_streaming=False,
            degradation=Degradation.OPTIONAL_RUNTIME,
        ),
        capability,
    )
    wiring.context_providers.append(SkillCatalogContextProvider(capability))
    # #529 T-529-5：capability 挂到 wiring（Web 只读 catalog 展示面的取用口）。
    wiring.skills = capability
    # load_skill（及装配了 promoter 时的 promote_skill/register_skill）不在这里
    # append：SkillCapability 实现 ContributesTools，与其他工具贡献统一走
    # wire_capabilities 末尾的收集循环。


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


class _ToolsProvider:
    """Contributes already constructed tools through the common registry collection path."""

    def __init__(self, tools: list[Any]) -> None:
        self._tools = tools

    def contributes_tools(self) -> list[Any]:
        return list(self._tools)

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
        from agent_harness.session import run_context_var
        from agent_harness.session.event import MEMORY_DEGRADED

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
            try:
                reason_code = (
                    "retrieval_timeout" if isinstance(error, TimeoutError)
                    else "authorization_denied" if isinstance(error, PermissionError)
                    else "retrieval_unavailable"
                )
                session.append(MEMORY_DEGRADED, {
                    "operation": "recall", "stage": "retrieval",
                    "reason_code": reason_code, "job_id": None,
                    "attempts": 1, "fallback_used": False,
                }, run_id=run_context_var.get())
            except Exception:  # noqa: BLE001 — a logging failure must not fail this optional provider.
                logging.getLogger(__name__).warning(
                    "Could not persist Memory V2 settings degradation",
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

    `sessions`（#298 T7b）：运行时空在写的那个会话日志存储。治理 service 独立装配；V2
    自动召回、显式命令与形成管线需要会话身份或事件来源，缺少 store 时保持关闭。形成 job
    按 `(session_id, run_id)` 从日志读取本轮事件。生产的两处调用点（`web/app.py` 的
    `get_wiring` / `assemble_wiring`）各自把手上的 store 传进来。
    """
    wiring = CapabilityWiring()
    # 进程级模型并发闸（#89 / #559）：与 capability 配置无关、恒创建——闸是
    # 装配生命周期的成员，不是某个 capability 的产物；limit 来自同一份 settings。
    wiring.model_call_gate = ModelCallGate(settings.model_max_concurrency)
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

    # Wire V2 as the only active memory service before the common tool collection loop.
    await _wire_memory_v2(
        registry, settings, wiring, sessions=sessions, workspace_index=workspace_index,
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
