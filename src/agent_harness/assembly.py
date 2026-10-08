"""Runtime 装配的单一入口（批次 A / 架构候选 1+4）。

"一个 Run 被装配了什么"此前没有单一答案：web/_build_runtime 内联 70 行、
cli 第二套削弱装配（无 Ledger/Checkpoint/工具）——耐久性语义分叉，且测试
只能按名 patch 私有符号。本 module 是 web 与 CLI 共享的深 factory：
- initialize_stores：恢复三 Store（Ledger / Checkpoint / SessionMeta）幂等初始化
- assemble_wiring：CAPABILITIES env → CapabilityWiring（工具/provider/生命周期）
- build_runtime：model + coding 工具 + capability 工具 + artifact 溢出 +
  Executor(Ledger/审批) + Checkpoint 策略 + ContextBuilder —— 全栈接线一处可读

web 与 CLI 是它的两个 adapter（两个 adapter = 真实 seam）；capability 的
发现与降级仍归 wire_capabilities（ADR-0010 不动），本层只消费其产物。
"""

from __future__ import annotations

import asyncio
import logging
import platform
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_harness.agent import AgentRuntime
from agent_harness.agent.budget import SOURCE_DEPLOYMENT
from agent_harness.agent.completion import CompletionPolicy
from agent_harness.agent.resume_evidence import StuckEvidencePort
from agent_harness.agent.run_budget import (
    LaunchRunBudget,
    SessionBudgetPort,
    SessionLimits,
    validate_tool_call_limits_registered,
)

logger = logging.getLogger(__name__)
from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import CapabilityWiring, wire_capabilities
from agent_harness.config import Settings
from agent_harness.context.builder import ContextBuilder
from agent_harness.context.project_instructions import (
    ProjectInstructionStore,
    project_instruction_store,
)
from agent_harness.model.concurrency import ModelCallGate
from agent_harness.model.config import ConfigError, ModelConfig, model_supports_vision
from agent_harness.model.provider import create_chat_model
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.observability import get_observability_sink

if TYPE_CHECKING:
    # profiles 模块在函数体内延迟导入（与 BUILTIN_PROFILES 取用点同款）；这里
    # 只为 `_root_profile_spec` 的返回值注解服务。
    from agent_harness.agent.profiles import AgentSpec
from agent_harness.prompt import (
    DEFAULT_REGISTRY,
    build_registry,
    compose_agent_prompt,
    join_guidance,
    parse_persona_config,
    tool_guidance_sections,
)
from agent_harness.sandbox import Sandbox, WorkspaceRegistry
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import (
    OnStableBoundary,
    SqliteCheckpointStore,
    SqliteOperationLedger,
    SqliteSessionMetaStore,
)
from agent_harness.storage.artifact_select import select_artifact_store
from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry
from agent_harness.tooling.approval import ApprovalCallback, ApprovalResponse
from agent_harness.tooling.contract import (
    PermissionPolicy,
    ToolExposure,
    ToolReconcileInfo,
    exposure_of,
)
from agent_harness.tooling.exposure import ToolExposureController, ToolSearchTool
from agent_harness.tooling.overflow import ArtifactOverflowHandler
from agent_harness.tooling.permission_rules import default_rule_set
from agent_harness.tooling.resource_locks import ResourceLockRegistry
from agent_harness.tools import (
    ApplyPatchTool,
    BashTool,
    EditTool,
    GitDiffTool,
    GitStatusTool,
    GlobTool,
    GrepTool,
    ReadTool,
    WriteTool,
)
from agent_harness.tools.register_constraint import RegisterConstraintTool
from agent_harness.tools.request_constraint_resolution import (
    REGISTER_CONSTRAINT_HANDOFF,
    RequestConstraintResolutionTool,
)
from agent_harness.tools.update_plan import UpdatePlanTool
from agent_harness.workspace import SqliteWorkspaceStore, WorkspaceIndex
from agent_harness.workspace.index import SessionHeaders

#: `build_runtime` **无条件**注册的本地工具类（顺序 = 注册顺序）。
#: 提成模块常量是为了让 profile 对账有一个**机械输入**：未归属任何档位、也不在带理由
#: 白名单里的工具会让 `tests/agent/test_tool_scope_reconciliation.py` 变红——加工具
#: 时改这里，别在函数里再写一份。
BUILTIN_LOCAL_TOOLS: tuple[type[Tool], ...] = (
    ReadTool, WriteTool, BashTool, EditTool, ApplyPatchTool,
    GlobTool, GrepTool, GitStatusTool, GitDiffTool,
)


@dataclass
class RecoveryStores:
    """恢复子系统三 Store（同一 SQLite 文件，ADR-0004 布局）。

    AppState 与 CLI 各自构造实例、共享生命周期所有权；factory 只消费。

    `workspace_index` 是 ADR-0025 的 Workspace 实体 + 有序会话账本（WS-2 / #152），
    同库不同表。它是**可选**成员：它需要会话 header 来源，而大量单测只调
    `recovery_stores(path)` 不接会话存储——那些场景不需要项目索引，保持 `None`。"""

    operation_ledger: SqliteOperationLedger
    checkpoint_store: SqliteCheckpointStore
    session_meta_store: SqliteSessionMetaStore
    delegation_tree_ledger: SqliteDelegationTreeLedger
    workspace_index: WorkspaceIndex | None = None


def recovery_stores(
    database_path: str | Path, *, workspace_headers: SessionHeaders | None = None
) -> RecoveryStores:
    """构造恢复 Store 束（同一 harness.db；未初始化——initialize_stores 幂等初始化）。

    `workspace_headers` 给定时才装配 `workspace_index`（见 `RecoveryStores` 说明）。
    """
    path = Path(database_path)
    return RecoveryStores(
        operation_ledger=SqliteOperationLedger(path),
        checkpoint_store=SqliteCheckpointStore(path),
        session_meta_store=SqliteSessionMetaStore(path),
        delegation_tree_ledger=SqliteDelegationTreeLedger(path),
        workspace_index=(
            WorkspaceIndex(SqliteWorkspaceStore(path), workspace_headers)
            if workspace_headers is not None
            else None
        ),
    )


async def initialize_stores(stores: RecoveryStores) -> None:
    """Store 束幂等初始化（并发首请求由调用方的 once 语义守护）。

    `workspace_index.initialize()` 顺带完成首次 bootstrap（AC14–16）——那一步需要
    读会话 header，所以只在装了 index 的进程里发生。
    """
    await stores.operation_ledger.initialize()
    await stores.checkpoint_store.initialize()
    await stores.session_meta_store.initialize()
    await stores.delegation_tree_ledger.initialize()
    if stores.workspace_index is not None:
        await stores.workspace_index.initialize()


async def assemble_wiring(
    settings: Settings,
    *,
    sessions: JsonlSessionStore | None = None,
    workspace_index: WorkspaceIndex | None = None,
) -> tuple[CapabilityRegistry, CapabilityWiring]:
    """CAPABILITIES env → 显式装配（capability 发现/降级归 wire_capabilities）。

    `sessions` 透传给 `wire_capabilities`，是**同一个**会话日志存储（#298 T7b）：
    V2 记忆形成在执行 job 时要按 `(session_id, run_id)` 从它里面切这一轮的事件，
    而它必须与运行时空正在写的那一份是同一处——所以由调用方注入，不在这里按约定现建。
    `workspace_index` 给 V2 自动召回与形成作业提供可信的 session → project 绑定。
    不传 `sessions` = 不装配 V2 记忆（其余能力零改动）。
    """
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry, parse_capabilities_config(settings.capabilities),
        settings=settings, sessions=sessions, workspace_index=workspace_index,
    )
    return registry, wiring


def _select_context_providers(
    wired: list[Any], requested: list[str] | None,
) -> list[Any]:
    """会话级 context_providers 筛选（ADR-0020b）。

    - ``requested is None`` → 返回全量 wired（默认行为，向后兼容）；
    - ``requested == []`` → 返回空（用户显式选零 provider，区别于 None 的默认全量）；
    - ``requested`` 非空 → 仅保留 ``name ∈ requested`` 的 provider；
      未知名字 fail-open 跳过（与 OPTIONAL_RUNTIME 降级原则一致——会话请求不能
      因为一个未装配的 provider 名字而拖垮 Core，不变量 #21）。

    未声明 ``name`` 属性的 provider（未来情况）经 ``getattr`` 容错为 None，
    不会被任何请求名字命中——fail-open 不报错。
    """
    if requested is None:
        return list(wired)
    wanted = set(requested)
    return [p for p in wired if getattr(p, "name", None) in wanted]


@dataclass(frozen=True)
class _RootTooling:
    """`_build_tooling` 的产物束：registry 与与它配对的旁路依赖。"""

    registry: ToolRegistry
    overflow_handler: ArtifactOverflowHandler | None
    context_artifact_store: Any | None
    context_read_tool_name: str | None
    runtime_multiagent_provider: Any | None


def _root_profile_spec(agent_profile: str | None) -> AgentSpec:
    """根档位声明的唯一取用点（#615②）：根配额 depth / delegations 同源。

    `_build_tooling`（DelegateTool 的树配额）与 `build_runtime`（multiagent
    provider.activate 的树账）此前**各写一遍**取用式（`agent_profile or "main"`
    vs `is not None` 分支）——漂移 = 工具面文案里的"整棵委派树最多 N 次"与树账
    的 max_delegations 各说各话；"" 边界还一处静默落 main、一处 KeyError
    （Web 边界已 422 掉空串，属死输入潜伏分叉，统一后两侧同形）。
    注意："无档位"语义（根 system_prompt 走文本包裹、registry 不收窄）仍以
    `profile_spec is not None` 判定，**不**经过本函数——那两处的 None 是
    有意义的状态，不是配额缺省。
    """
    from agent_harness.agent.profiles import BUILTIN_PROFILES

    return (
        BUILTIN_PROFILES[agent_profile]
        if agent_profile is not None
        else BUILTIN_PROFILES["main"]
    )


def _build_tooling(
    settings: Settings,
    wiring: CapabilityWiring,
    *,
    session_id: str,
    workspace: Path,
    workspace_registry: WorkspaceRegistry,
    session_store: JsonlSessionStore | None,
    agent_profile: str | None,
    include_constraint_tools: bool = False,
    include_constraint_resolution_tool: bool | None = None,
    project_instructions: ProjectInstructionStore | None = None,
    # #363 / W-19：显式选择的 sandbox 后端（None ⇒ registry 部署默认）。
    sandbox_backend: str | None = None,
) -> _RootTooling:
    """sandbox 绑定 + **根 registry**（收窄前）构造——唯一的工具面事实源。

    为什么独立成函数（#564 裁决 (a)）：service 层的 session 注册名校验必须发生在
    eager CAS **之前**，彼时 build_runtime 还没有跑，"哪些工具已注册"只能从同一份
    构造逻辑取。build_runtime 调本函数；pre-CAS 校验用的是本函数的**零副作用**
    名字集投影 `root_registry_tool_names`（不实例化 sandbox，审查 P2-1）——新增
    工具来源必须同时落在两处（漏落 = `tests/test_assembly_root_registry_names.py`
    的三方对账红灯）。
    """
    # #286：根委派配额来自档位声明（与 build_runtime 的树账同一取用点）。
    root_max_delegations = _root_profile_spec(agent_profile).max_delegations
    include_resolution_tool = (
        include_constraint_tools
        if include_constraint_resolution_tool is None
        else include_constraint_resolution_tool
    )
    # #372（ADR-0048 残余 16）：已有持久映射的会话（fork 副本 / 委派子会话的
    # 属主 alias）走"取回既有绑定"的 get 语义——alias 映射记录的是属主授权，
    # create() 对它响亮拒绝（防改写属主绑定，tests/sandbox/test_workspace_registry.py
    # 钉住），恢复路径不该撞它。无映射 = 新会话，照旧 create。注意：工具面收窄
    # （agent_profile → registry.filtered）在 build_runtime 下游与 sandbox 解析无关，
    # 恢复入口的授权重建由调用方（service.resume_and_launch）按子会话 AgentSpec 传
    # agent_profile 兑现——只放开这一半会放大子会话工具面，两条必须一起成立。
    if workspace_registry.exists(session_id):
        sandbox = workspace_registry.get(session_id)
    else:
        sandbox = workspace_registry.create(
            session_id, workspace_root=workspace, backend=sandbox_backend
        )
    if project_instructions is None:
        project_instructions = project_instruction_store(settings)
    registry = ToolRegistry()
    for tool_cls in BUILTIN_LOCAL_TOOLS:
        # #244 AC5：Bash 预算来自 Settings，其余工具类无参构造——工具自己不认识
        # Settings，装配层是唯一的接线点；接不上就成死键（tests/test_assembly_bash_budget.py）。
        kwargs = {}
        if tool_cls is BashTool:
            kwargs["timeout_seconds"] = settings.bash_timeout_seconds
        elif tool_cls is ReadTool:
            kwargs["project_instructions_loader"] = (
                lambda relative_path: project_instructions.load_for_path(
                    session_id, workspace, workspace / relative_path,
                )
            )
        registry.register(tool_cls(sandbox, **kwargs))

    # W-26（#380）：`update_plan` 是会话域工具（事件写入，不碰 sandbox / 文件系统），
    # 无构造依赖——会话从 `current_session_var` 在执行期拿（context.py）。与
    # BUILTIN_LOCAL_TOOLS 同样无条件注册；profile 归属见 `profiles._CODING_TOOLS`。
    registry.register(UpdatePlanTool())
    if include_constraint_tools:
        # 冲突指引**跟着 resolver 的在册状态走**：resolver 缺席的入口（CLI）拿到那段话
        # 只会去调一个不存在的工具（#663 P2）。注入的是**增量**转接句，不是 resolver
        # guidance 的副本——那份全文经 `tool:request_constraint_resolution` 独立进
        # system prompt，照抄一遍等于每次请求下发两份（Call 3 P2-1）。
        #
        # 设计来源: pi 28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9
        #   packages/agent/src/agent.ts:85 —— 系统消息里的工具声明由**当前那份活的
        #   tools 列表**派生（`tools.map(toToolDeclaration)`），不是另抄一份静态清单：
        #   工具面变了、说明不同步变，就是让模型对着不存在的工具下指令。这里同理——
        #   转接句跟着 registry 的在册状态走。
        registry.register(RegisterConstraintTool(
            resolution_guidance=(
                REGISTER_CONSTRAINT_HANDOFF
                if include_resolution_tool
                else None
            ),
        ))
    if include_resolution_tool:
        registry.register(RequestConstraintResolutionTool())

    # 外置写入与模型侧读取**必须成对**：溢出处理器（唯一写入者）与读回工具指向
    # **同一个** store，否则会出现"东西写进了 A、模型从 B 读"的静默错配。
    # 选择口径（优先级 + 半配置判定）收敛在 `storage/artifact_select.py`，读路径
    # （`web/artifacts.py`）用同一个函数——两处各写一遍 if 级联就等于给漂移留门
    # （#192 批 1 审查发现）。
    overflow_handler = None
    context_artifact_store = None
    context_read_tool_name: str | None = None
    selection = select_artifact_store(settings, session_id)
    if selection is not None:
        read_tool = selection.read_tool(selection.store)
        registry.register(read_tool)
        # 摘要里的读回提示点名**这个**工具（#186 AC4）：S3 配 `inspect_artifact`，
        # MinIO / Local 配 `read_artifact`。名字从选择器**实例化出来的那个工具**上取，
        # 不在这里再填一个字面量——那样等于把"配对关系"这份知识写了第二遍。
        overflow_handler = ArtifactOverflowHandler(
            selection.store,
            settings.artifact_overflow_chars,
            read_tool_name=read_tool.name,
        )
        # W-03 (#347)：ContextBuilder 的旧 Tool Result 裁剪与 overflow 共用同一个
        # store 与同一个真实读回工具名——骨架行的回读提示必须指向**确实配对**的
        # 工具（名字不得写死）。store 未选中（None）→ builder 裁剪整体关闭。
        context_artifact_store = selection.store
        context_read_tool_name = read_tool.name

    runtime_multiagent_provider = None
    if wiring.multiagent_provider is not None and session_store is not None:
        # CapabilityWiring is cached across requests; activation state is not.
        # Give each root Runtime its own provider and DelegateTool while descendants
        # inherit that same registry/provider through AgentFactory.
        runtime_multiagent_provider = wiring.multiagent_provider.new_runtime_instance()

    for capability_tool in wiring.tools:
        # multiagent 依赖 session_store 建独立 child session——缺席时降级缺席
        # （不注册 delegate，单代理照常），与 optional capability 语义一致。
        if isinstance(capability_tool, DelegateTool) and session_store is None:
            logger.warning(
                "multiagent 已启用但未提供 session_store，delegate 工具降级缺席"
            )
            continue
        if isinstance(capability_tool, DelegateTool) and runtime_multiagent_provider is not None:
            # 结论 ref 化（W-31.3 / #415）：注入**同一个** context_artifact_store
            #（上面选出，ContextBuilder 共用）——不得新建第二个 store，否则
            # 出现"写进 A、从 B 读"的静默错配。未选中 store（None）= fail-open
            # 不外置，与 W-03 整体关闭语义同口径。
            capability_tool = DelegateTool(
                runtime_multiagent_provider, max_delegations=root_max_delegations,
                artifact_store=context_artifact_store,
                summary_overflow_tokens=settings.subagent_summary_overflow_tokens,
            )
        registry.register(capability_tool)

    return _RootTooling(
        registry=registry,
        overflow_handler=overflow_handler,
        context_artifact_store=context_artifact_store,
        context_read_tool_name=context_read_tool_name,
        runtime_multiagent_provider=runtime_multiagent_provider,
    )


def _project_instruction_cwd(
    workspace: Path | Sandbox,
    workspace_registry: WorkspaceRegistry,
    session_id: str,
) -> Path:
    """Resolve the host workspace path used to discover repository instructions."""
    if isinstance(workspace, Path):
        return workspace

    recorded_roots = workspace_registry.recorded_workspace_roots(session_id)
    sandbox_root = workspace.workspace_root
    if isinstance(sandbox_root, Path):
        resolved_root = sandbox_root.resolve()
        if not recorded_roots or str(resolved_root) in recorded_roots:
            return resolved_root
        raise ValueError(
            "sandbox workspace root does not match its recorded workspace roots"
        )
    if len(recorded_roots) == 1:
        return Path(recorded_roots[0])
    raise ValueError(
        "cannot resolve a host workspace path for project instructions"
    )


def root_registry_tool_names(
    settings: Settings,
    wiring: CapabilityWiring,
    *,
    session_id: str,
    session_store: JsonlSessionStore | None,
    include_constraint_tools: bool = False,
    include_constraint_resolution_tool: bool | None = None,
) -> frozenset[str]:
    """根 registry 名字集的**零副作用**计算——pre-CAS session 名字校验专用。

    #564 审查 P2-1：validator 若走完整 `_build_tooling`，`LocalSubprocessSandbox
    .__init__` 会对 workspace `mkdir`，发生在 service 的归属对账（#266
    `SessionCwdUnavailable` 守卫，`WorkspaceNotFound` 子型）**之前** ⇒ 坏名 422
    会把已删 cwd 凭空重建、合法名
    resume 掩蔽守卫。名字集不依赖 sandbox 实例：本地工具"构造器只存依赖"（既有
    判定，`tests/agent/test_tool_scope_reconciliation.py` 传 None 读 `.name`）；
    读回工具名只由 Provider 选择决定（store 构造无副作用，mkdir 在写路径）；
    capability 工具在 wiring 里已是实例。`_build_tooling` 与本函数必须同源一致
    ——`tests/test_assembly_root_registry_names.py` 三方对账（真实装配 /
    `_build_tooling` / 本函数），漂移即红灯。
    """
    names: set[str] = {tool_cls(None).name for tool_cls in BUILTIN_LOCAL_TOOLS}
    # W-26（#380）：`update_plan` 无条件注册（与 `_build_tooling` 同一句判定）。
    names.add(UpdatePlanTool().name)
    include_resolution_tool = (
        include_constraint_tools
        if include_constraint_resolution_tool is None
        else include_constraint_resolution_tool
    )
    if include_constraint_tools:
        names.add(RegisterConstraintTool().name)
    if include_resolution_tool:
        names.add(RequestConstraintResolutionTool().name)
    selection = select_artifact_store(settings, session_id)
    if selection is not None:
        # 读回工具名从**配对的那个工具类**上取，不写第二遍字面量（#186 AC4 同源）。
        names.add(selection.read_tool(None).name)
    for capability_tool in wiring.tools:
        # multiagent 依赖 session_store 建独立 child session——缺席时降级缺席，
        # 与 `_build_tooling` 的同名分支逐字同判（名字在不在，两边必须一致）。
        if isinstance(capability_tool, DelegateTool) and session_store is None:
            continue
        names.add(capability_tool.name)
    return frozenset(names)


def _tool_reconcile_info(tool: Tool) -> ToolReconcileInfo:
    """Tool 实例/裸构造 → 恢复裁决呈现元数据（属性逐字快照，不执行任何方法）。"""
    hint = tool.reconcile_hint
    return ToolReconcileInfo(
        replay_safe=tool.replay_safe,
        verifiable=hint.verifiable,
        suggested_action=hint.suggested_action,
    )


def root_registry_reconcile_info(
    settings: Settings,
    wiring: CapabilityWiring,
    *,
    session_id: str,
    session_store: JsonlSessionStore | None,
) -> dict[str, ToolReconcileInfo]:
    """根 registry 恢复裁决呈现元数据的**零副作用**计算（#357 W-13 §2.4）。

    与 ``root_registry_tool_names`` 逐分支同构（名字集的对账测试同型钉住）：
    本地工具"构造器只存依赖"（传 ``None`` 读类属性），capability 工具在
    wiring 里已是实例——**不实例化 sandbox、不 mkdir**（审查 P2-1 同款取舍）。
    消费点：``_reconcile_pending`` 产出 ``default_action/risk_level/probe``
    只读展示字段；判据与真实工具面漂移即红灯
    （``tests/test_assembly_root_registry_names.py``）。
    """
    info: dict[str, ToolReconcileInfo] = {}
    for tool_cls in BUILTIN_LOCAL_TOOLS:
        tool = tool_cls(None)
        info[tool.name] = _tool_reconcile_info(tool)
    update_plan = UpdatePlanTool()
    info[update_plan.name] = _tool_reconcile_info(update_plan)
    selection = select_artifact_store(settings, session_id)
    if selection is not None:
        read_tool = selection.read_tool(None)
        info[read_tool.name] = _tool_reconcile_info(read_tool)
    for capability_tool in wiring.tools:
        # multiagent 依赖 session_store 建独立 child session——缺席时降级缺席，
        # 与 `root_registry_tool_names` 的同名分支逐字同判（两边必须一致）。
        if isinstance(capability_tool, DelegateTool) and session_store is None:
            continue
        info[capability_tool.name] = _tool_reconcile_info(capability_tool)
    return info


async def build_runtime(
    *,
    settings: Settings,
    wiring: CapabilityWiring,
    stores: RecoveryStores,
    workspace_registry: WorkspaceRegistry,
    session_id: str,
    workspace: Path | Sandbox,
    max_agent_turns: int,
    permission_mode: PermissionPolicy = PermissionPolicy.WORKSPACE_WRITE,
    auto_approve: bool | None = None,
    approval_callback: ApprovalCallback | None = None,
    session_store: JsonlSessionStore | None = None,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
    agent_profile: str | None = None,
    include_constraint_tools: bool = False,
    include_constraint_resolution_tool: bool | None = None,
    context_providers: list[str] | None = None,
    steer_source: Any | None = None,
    run_budget: LaunchRunBudget | None = None,
    local_fuse_source: str = SOURCE_DEPLOYMENT,
    stuck_evidence: StuckEvidencePort | None = None,
    session_budget: SessionBudgetPort | None = None,
    session_declared_limits: SessionLimits | None = None,
    # `#524`：完成门可选域策略（默认 None = DefaultCompletionPolicy，行为零变化；
    # 显式传入 EvidenceCompletionPolicy 即开启证据检查与纠正循环——装配开关就是
    # 策略实例本身，无布尔旗标）。
    completion_policy: CompletionPolicy | None = None,
    # #363 / W-19：显式选择的 sandbox 后端（None ⇒ registry 部署默认）。
    sandbox_backend: str | None = None,
) -> AgentRuntime:
    """装配全栈 Runtime：调用方保证 stores 已 initialize、workspace 已就绪。

    模型经 create_chat_model(settings) 构造（测试替身注入点）；sandbox 由
    WorkspaceRegistry 统一创建并持久化映射（恢复时按映射还原）。
    model_name（ADR-0016 §5）：None = 默认链；catalog 名 = 会话级选择
    （未知名字在 web 层已 422，这里 resolve 再响亮失败一次）。

    permission_mode（Phase 5）：会话级 PermissionPolicy 上限（审批阈值，不是
    硬墙——policy 决定哪些工具 needs_approval，审批结果仍由 callback 决定）。
    approval_callback：None → 安全默认（auto-approve 全批），调用方也可注入交互
    式审批 callback（见 web 层 PendingApprovalQueue）。
    steer_source（ADR-0030 §4.3）：待注入 steer 的读取端口（Web 层传
    MessageQueueManager 的内存镜像）；None = 不注入，CLI 与既有单测逐字不变。

    `max_agent_turns`（#308）：**已解析的** local fuse 生效值，本函数只消费结果。刻意
    **不**在这里解析：装配点只有一个输入，而不是"再判一次策略"（解析点见
    `agent.budget.resolve_local_fuse`）。

    `run_budget` / `local_fuse_source`（`#312`）：run 作用域账本上下文与 local fuse 的
    **来源标识**，两者都只是**透传**给 AgentRuntime（判定与解析都在服务层与
    `agent/run_budget.py`）。默认 None / deployment ⇒ 既有调用方（CLI、单测、
    delegate 子 runtime）逐字不变：新 run、无 run ceiling、fuse 来源记 deployment。

    `stuck_evidence`（`#317`）：stuck 暂停的三类恢复依据里"环境 / 策略"两条的
    **观测端口**，同样只是透传（`AgentRuntime` 只在暂停那一刻读一次）。构造方是
    服务层——它才掌握"本次生效策略"的全部输入，且恢复侧要用**同一份函数**现算再
    比较（ADR-0048 D8）。默认 None ⇒ 不观测（那两条依据届时按"无快照可比"409，
    "相关 steer"那条不受影响），CLI / 单测的既有路径逐字不变。

    `session_declared_limits`（`#564`）：本次请求**点名**的 session 声明（区别于
    `session_budget.limits` 的账行现值——恢复通道两者可以不同）。非 None 时按
    **根 registry** 判注册名 422（树级语义）；账行现值里的陈旧名降告警不拒绝，
    见校验块内注释。
    """
    # agent_profile 运行时消费（ADR-0020a，RUNTIME 子批次）：查 BUILTIN_PROFILES
    # 拿 AgentSpec——main/None 走原路径（registry 全量、无 system_prompt 注入），
    # coding/research_review 收窄 registry 到 spec.tool_scope + 注入 spec.system_prompt。
    # 未知名字 web 层已 422，这里 KeyError 再响亮失败一次（防御性，不应发生）。
    # #286：main 也是**根委派配额**的来源——即便 agent_profile=None（= 出厂
    # main），根 max_depth 也要从这里取，所以 import 提到分支外。
    from agent_harness.agent.profiles import BUILTIN_PROFILES

    profile_spec = None
    if agent_profile is not None:
        profile_spec = BUILTIN_PROFILES[agent_profile]
    instruction_workspace = (
        workspace_registry.get(session_id)
        if workspace_registry.exists(session_id)
        else workspace
    )
    instruction_cwd = _project_instruction_cwd(
        instruction_workspace, workspace_registry, session_id,
    )
    project_instructions = project_instruction_store(settings)
    await asyncio.to_thread(
        project_instructions.load_for_session, session_id, instruction_cwd,
    )
    # 根配额（#286 冻结语义 1）：root depth=0 ⇒ max_depth 就是"还能往下几层"。
    # 与 _build_tooling 的 DelegateTool 树配额同一取用点（#615②，双算已并一）。
    root_profile = _root_profile_spec(agent_profile)
    root_max_depth = root_profile.max_depth
    root_max_delegations = root_profile.max_delegations

    # T5 persona（ADR-0023 D10）：env JSON → 前后缀 section。形制与
    # parse_capabilities_config 一致——坏配置装配期响亮失败，不静默降级。
    # DEFAULT_REGISTRY 不读环境（§4.4），所以这里显式按 settings 构建一次。
    persona = parse_persona_config(settings.agent_persona)

    # #203 / ADR-0032 D8：模型解析收敛——统一解析点 resolve_selection（catalog
    # 名优先 + 自定义供应商 `<provider>:<model_id>` 命名空间 fallback）；两者都
    # 未命中才响亮失败。被删 provider **不静默 fallback**（D9：明确错误含
    # provider id）。终审 P2 修复：不再裸 model_id 跨 provider 匹配——两个自定义
    # provider 注册同一 model_id 时此前的循环取文件序第一个（顺序依赖的静默选择）；
    # 现在 catalog 名精确命中 + composite id（provider:model）精确解析，无歧义。
    if model_name is None:
        config = ModelConfig.from_settings(settings)
    else:
        try:
            from agent_harness.model.provider_store import ProviderStore

            store = ProviderStore.for_settings(settings)
            config = ModelConfig.resolve_selection(settings, model_name, store)
        except ConfigError as error:
            raise error from None
    model = create_chat_model(config, reasoning_effort=reasoning_effort)
    summary_model = None
    summary_model_name = (settings.summary_model or "").strip()
    if summary_model_name:
        try:
            from agent_harness.model.provider_store import ProviderStore

            summary_store = ProviderStore.for_settings(settings)
            summary_config = ModelConfig.resolve_selection(
                settings, summary_model_name, summary_store,
            )
        except ConfigError as error:
            raise error from None
        # Summary generation uses the selected model without inheriting the
        # primary model's reasoning-effort setting.
        summary_model = create_chat_model(summary_config)
    # Model Fallback 两级链（ADR-0014 决策 14/16）：FALLBACK_MODEL_PROVIDER
    # 已配 → 构造 fallback 模型；切换决策在 FallbackPolicy，编排由 Runtime
    # 的 per-run coordinator 负责（见 agent/fallback 接线）。
    fallback_model = None
    if config.fallback is not None:
        fallback_effort = (
            config.fallback.reasoning_effort.default
            if config.fallback.reasoning_effort is not None
            else None
        )
        fallback_model = create_chat_model(
            config.fallback, reasoning_effort=fallback_effort,
        )
    # 进程级模型并发闸（#89 / #559 修复）：闸实例归 wiring（装配生命周期）所有，
    # 同一进程内所有 build_runtime 共享同一实例（全局在飞模型调用数的语义——
    # 跨 Session / child / fallback / 摘要一致）。手搓 wiring（直接
    # CapabilityWiring()，无装配生命周期）保持 None ⇒ 退回每次新建，行为同历史。
    model_call_gate = wiring.model_call_gate
    if model_call_gate is None:
        model_call_gate = ModelCallGate(settings.model_max_concurrency)

    # #372（ADR-0048 残余 16）：已有持久映射的会话（fork 副本 / 委派子会话的
    # 属主 alias）走"取回既有绑定"的 get 语义——alias 映射记录的是属主授权，
    # create() 对它响亮拒绝（防改写属主绑定，tests/sandbox/test_workspace_registry.py
    # 钉住），恢复路径不该撞它。无映射 = 新会话，照旧 create。注意：工具面收窄
    # （agent_profile → registry.filtered）在本函数下游与 sandbox 解析无关，恢复
    # 入口的授权重建由调用方（service.resume_and_launch）按子会话 AgentSpec 传
    # agent_profile 兑现——只放开这一半会放大子会话工具面，两条必须一起成立。
    # （sandbox 规则与 registry 构造在 `_build_tooling`——与 #564 的 pre-CAS 校验
    # 同一事实源，见其 docstring。）
    include_resolution_tool = (
        include_constraint_tools
        if include_constraint_resolution_tool is None
        else include_constraint_resolution_tool
    )
    tooling = _build_tooling(
        settings, wiring,
        session_id=session_id, workspace=instruction_cwd,
        workspace_registry=workspace_registry, session_store=session_store,
        agent_profile=agent_profile,
        include_constraint_tools=include_constraint_tools,
        include_constraint_resolution_tool=include_resolution_tool,
        project_instructions=project_instructions,
        # #363 / W-19：显式 sandbox 后端选择。
        sandbox_backend=sandbox_backend,
    )
    registry = tooling.registry
    overflow_handler = tooling.overflow_handler
    context_artifact_store = tooling.context_artifact_store
    context_read_tool_name = tooling.context_read_tool_name
    runtime_multiagent_provider = tooling.runtime_multiagent_provider
    # 根 registry（收窄前）的名字集：session 作用域判据的依据（#564 裁决的树级
    # 语义——session 预算横跨会话树，"整棵树调得到"以根为准，不看本 runtime 的
    # 收窄面）。在收窄**前**取，一旦错过就无从对比。
    root_registry_names = frozenset(tool.name for tool in registry.list())

    # Phase 5：permission_mode 是会话级 PermissionPolicy 上限（审批阈值）。
    # approval_callback 由调用方决定：None → 安全默认（全批），注入 → 交互审批。
    # 切片 B：ApprovalCallback 已 async 化（外部 /approve 交互式审批需要 run 暂停）。
    policy = permission_mode
    if auto_approve is False and approval_callback is None:
        async def approval_callback(_req):  # type: ignore[no-redef]
            # #423：用户向措辞（原 "manual approval not yet wired" 已退役，见 CHANGELOG）。
            return ApprovalResponse(
                approved=False,
                reason="自动批准未开启，且当前会话没有可用的审批通道；已按 fail-closed 拒绝本次工具执行",
            )
    elif approval_callback is None:
        async def approval_callback(_req):  # type: ignore[no-redef]
            return ApprovalResponse(approved=True, reason="auto-approve")

    # agent_profile tool_scope 收窄（ADR-0020a）：仅在非 main profile 时过滤——
    # main 的 _MAIN_TOOLS 是全量的超集，filter 等价不过滤，但若未来新增了一个
    # tool_scope 未声明的工具，filter 会隐性收窄它。所以 main/None 走原路径不 filter，
    # 只有 coding/research_review 才收窄。收窄后 registry 流向所有下游：
    # ToolExecutor / AgentRuntime / multiagent activate 的 source_registry。
    # #198：被剔除的工具名必须在收窄**前**记录（收窄后已无从对比）——这正是
    # "模型说没有 write/edit/apply_patch"的答案本身，经 run_config 日志可回溯。
    dropped_tools: tuple[str, ...] = ()
    if profile_spec is not None and agent_profile != "main":
        pre_filter_names = {tool.name for tool in registry.list()}
        registry = registry.filtered(profile_spec.tool_scope)
        dropped_tools = tuple(sorted(pre_filter_names - {tool.name for tool in registry.list()}))
    if agent_profile == "coding":
        # These tools belong to a direct user-facing root coding session. They stay out
        # of the shared coding tool_scope so AgentFactory cannot grant them to children.
        registered_names = {tool.name for tool in registry.list()}
        if include_constraint_tools and RegisterConstraintTool().name not in registered_names:
            registry.register(RegisterConstraintTool(
                resolution_guidance=(
                    REGISTER_CONSTRAINT_HANDOFF
                    if include_resolution_tool
                    else None
                ),
            ))
        if include_resolution_tool and RequestConstraintResolutionTool().name not in registered_names:
            registry.register(RequestConstraintResolutionTool())

    # 曝光级别接线（#528 / IMP-11）：Registry 定型（含上面的收窄）后，存在
    # deferred 工具才注册内置 tool_search 并建立定义集控制器——全 direct
    # （现状默认）时零新增工具、零行为变化。收窄后注册保证被权限剔除的工具
    # 物理不在 tool_search 的搜索面上（发现不授予执行权）。注意：tool_search
    # 不进 root_registry_names（上面已取）⇒ session 配额声明点名它会 422
    # （fail-closed，V1 不支持对它配额）。
    tool_exposure: ToolExposureController | None = None
    if any(
        exposure_of(tool) is ToolExposure.DEFERRED for tool in registry.list()
    ):
        tool_exposure = ToolExposureController(registry)
        registry.register(ToolSearchTool(tool_exposure))

    # per-tool 配额的**注册名**校验（`#314` / `04 §9.1`）：判定点是这里，因为注册表
    # 到上一行为止才定型（内置 + artifact 读回 + capability，并按 profile 收窄）。
    # 位置仍然满足 `11 §6.1` 的"无副作用"：在任何 model / tool / child 工作之前，
    # 也不落任何消耗预算的事件。子 runtime（delegate）不带 run_budget ⇒ 自然跳过。
    if run_budget is not None:
        validate_tool_call_limits_registered(
            run_budget.limits, registered=[tool.name for tool in registry.list()],
        )
    # session 作用域（#564 裁决 (a)+(b)）——两个不同的对象、两条不同的处置：
    # - **请求声明**（session_declared_limits，本次请求点名的那份）：422，判据 =
    #   **根 registry**（树级语义）。session 预算横跨会话树，本 runtime 收窄掉的
    #   工具树根仍调得到（child registry ⊆ 根 registry），按收窄面判会误杀合法
    #   委派配额。service 层的 resume 通道已在 eager CAS **之前**核过同一份声明
    #   （坏名永不触碰账行），这里是 create 等其余通道的无副作用拒绝点。
    # - **账行现值**（session_budget.limits，可能含历史合法名）：陈旧名（能力下线、
    #   工件 store 切换）**降告警不拒绝**——修复前按收窄面重核 422，会让一次打错的
    #   请求或一次能力下线把会话毒成不可恢复态（K8s 对事后悬空引用、AWS IAM 对
    #   悬空 ARN 都是"响亮暴露但不 brick"同型）。配额对陈旧名不再生效。
    if session_declared_limits is not None:
        validate_tool_call_limits_registered(
            session_declared_limits, registered=sorted(root_registry_names),
            scope="session",
        )
    if session_budget is not None:
        stale_names = sorted(
            name for name in session_budget.limits.tool_call_limits
            if name not in root_registry_names
        )
        if stale_names:
            logger.warning(
                "session 账行含当前 registry 不存在的配额名 %s（能力下线 / 工件 "
                "store 切换等历史原因）：配额对该工具不再生效；按 #564 裁决 (b) "
                "告警不拒绝，422 只属于请求声明",
                stale_names,
            )

    # T6 工具 guidance（ADR-0023 D11）：把**收窄后** registry 里各工具自带的
    # `prompt_guidance` 注册成 `tool:<name>` section（order 2000，scope `{"*"}`）。
    # 内容边界由这个 registry 决定——coding 收窄后没有 delegate，所以它的 prompt
    # 拿不到委派说明（工具缺席 → 说明缺席，这是本票的核心性质）。
    prompt_registry = build_registry(
        persona, tool_sections=tool_guidance_sections(registry.list()),
    )

    # #525 一期（IMP-14）：父与并发 SubAgent 的 executor 共享同一 resource
    # 锁注册表——同一 resource key（如 workspace-file 键）的执行跨批次/跨
    # runtime 串行。锁是进程内同步原语，生命周期与本次 build 的 wiring 相同。
    resource_locks = ResourceLockRegistry()

    # multiagent 激活（ADR-0015）：模型链与 registry 已就绪，注入 child 的
    # 全部依赖。executor_factory 闭包捕获父级审批/策略/记账——child 与 parent
    # 同一审批面（决策 11 权限传递）；resource 锁同源（#525 一期）。
    if runtime_multiagent_provider is not None:
        from agent_harness.agent.factory import AgentFactory

        def _child_executor_factory(child_registry: ToolRegistry) -> ToolExecutor:
            return ToolExecutor(
                child_registry, policy=policy, approval_callback=approval_callback,
                overflow_handler=overflow_handler,
                operation_ledger=stores.operation_ledger,
                resource_locks=resource_locks,
                # #358 / W-14：子执行器与父同一审批面（决策 11 权限传递）——
                # 显式注入产品级默认权限矩阵（规则引擎本体默认 None = 不启用）。
                permission_rules=default_rule_set(),
                workspace_root=instruction_cwd,
            )

        runtime_multiagent_provider.activate(
            factory=AgentFactory(
                model=model, fallback_model=fallback_model,
                executor_factory=_child_executor_factory,
                primary_model_name=config.model_name,
                fallback_model_name=(config.fallback.model_name
                                     if config.fallback is not None else "fallback"),
                stream_idle_timeout=settings.model_stream_idle_timeout,
                stream_total_timeout=settings.model_stream_total_timeout,
                model_call_gate=model_call_gate,
                observability_sink=get_observability_sink(settings),
                persona=persona,
                # child 的 guidance 来自它自己收窄后的 registry——这里显式开开关。
                # Factory 默认 False：B2 契约（child.system_prompt == spec.system_prompt）
                # 的成立必须与"工具恰好没有 guidance"无关。
                include_tool_guidance=True,
                # child 的 local fuse 上限（#308）：档位声明（内置三档位是 None=继承）
                # 只能收窄到 Deployment ceiling 之下，越界在 Factory.create 里被拒。
                local_max_agent_turns=settings.local_max_agent_turns,
                # `#524`：子与父同一完成门策略面（Factory 透传给 child runtime）。
                completion_policy=completion_policy,
            ),
            source_registry=registry,
            session_store=session_store,
            workspace_registry=workspace_registry,
            parent_session_id=session_id,
            max_depth=root_max_depth,
            max_delegations=root_max_delegations,
            delegation_ledger=stores.delegation_tree_ledger,
        )

    def _render_runtime_context() -> str:
        """渲染运行时上下文快照（T7 / ADR-0023 D8；BUG-013 瘦身）——每次 build 一次。

        事实来源全部取**当前**值，不缓存：
        - `cwd`：当前进程工作目录；
        - `os`：`platform.system()` + `release()`；
        - `date`：本地日期，只到日（用 `datetime.now()` 会让每次 build 文本都变，
          既毁 prefix cache 又难断言）。

        BUG-013 瘦身：**不再渲染 `model` / `tools`**——工具清单已在 system prompt
        的 tool guidance 区（静态能力），模型名运行时可查；快照紧贴最新用户消息，
        列工具会诱导模型把对话任务误判为工具任务。

        产物落 `meta_user`：快照是 user-role 消息，不是 system-role。
        """
        runtime_context = DEFAULT_REGISTRY.assemble("runtime:context_snapshot", {
            "cwd": str(Path.cwd()),
            "os": f"{platform.system()} {platform.release()}",
            # 本地日期（用户看到的"今天"），**不**用 UTC：跨时区时 UTC 日期会与
            # 用户的一天错位。DTZ011 要的是 tz-aware，而这里刻意要本地日历日。
            "date": date.today().isoformat(),  # noqa: DTZ011
        }).meta_user_text
        return project_instructions.load_for_session(
            session_id, instruction_cwd,
        ).with_runtime_context(runtime_context)

    # #823 / MM-02（A2）：视觉能力按**当前请求模型**判定（PRD D6——"fallback 自动
    # 降级"）。主/备两级各自解析声明；运行时在每次 build 前按 coordinator 的当前角色
    # 同步进 context_builder，切 fallback 后投影自动改用 fallback 的口径。
    primary_supports_vision = model_supports_vision(settings, config)
    fallback_supports_vision = (
        model_supports_vision(settings, config.fallback)
        if config.fallback is not None else False
    )
    return AgentRuntime(
        model=model,
        registry=registry,
        executor=ToolExecutor(registry, policy=policy, approval_callback=approval_callback,
                              overflow_handler=overflow_handler,
                              operation_ledger=stores.operation_ledger,
                              resource_locks=resource_locks,
                              # #358 / W-14：主执行器注入产品级默认权限矩阵
                              # （规则引擎本体默认 None = 不启用；矩阵归 composition root）。
                              permission_rules=default_rule_set(),
                              workspace_root=instruction_cwd),
        max_agent_turns=max_agent_turns,
        checkpoint_policy=OnStableBoundary(stores.checkpoint_store),
        session_meta_store=stores.session_meta_store,
        context_builder=ContextBuilder(
            model, max_context_tokens=settings.max_context_tokens,
            auto_compact_threshold=settings.auto_compact_threshold,
            hard_guard_threshold=settings.hard_guard_threshold,
            summary_model=summary_model,
            # #559：摘要调用与主循环同闸（进程级在飞 ≤N 的语义，见上）。
            model_call_gate=model_call_gate,
            # W-29 (#383)：清单兜底重注入周期（PRD §4.6 Cline 默认值，可配置）。
            plan_reinject_every_messages=settings.plan_reinject_every_messages,
            # context_providers 运行时消费（ADR-0020b）：会话请求字段按 name 筛选
            # wiring 自动装配的 provider 子集；None=默认全量，[]=显式零，未知名字 fail-open。
            context_providers=_select_context_providers(
                wiring.context_providers, context_providers,
            ),
            # 有 profile → 走注册表组装：persona（0 / 10200）与工具 guidance（2000）
            # 的 order 由容器排序，persona 为空且无 guidance 时该 scope 只有一条
            # section，T2 保证产物逐字节等于原文（C5/C7 因此保持绿）。
            # 无 profile → 没有可组装的 scope，直接文本包裹（guidance 仍拼进去：
            # 它是"怎么用工具"的操作信息，与身份无关；两者皆空时返回 None，C4 保持）。
            system_prompt=(
                prompt_registry.assemble(f"profile:{agent_profile}").system_text
                if profile_spec is not None
                else compose_agent_prompt(None, persona, join_guidance(registry.list()))
            ),
            # T7：快照**不**拼进 system_prompt（那会破坏 T5/T6 与 C4/C5/C7 的逐字节
            # 契约），而是走独立通道，由 builder 按 meta_user 语义插到当前用户消息前。
            runtime_context_provider=_render_runtime_context,
            # W-03 (#347)：store 未装配时传 None（裁剪整体关闭，行为不变）。
            artifact_store=context_artifact_store,
            artifact_read_tool_name=context_read_tool_name,
            # W-31.2 (#414)：裁剪的两个确定性护栏，原样透传（校验在 pruner 构造处）。
            keep_recent_tool_results=settings.keep_recent_tool_results,
            clear_at_least_tokens=settings.clear_at_least_tokens,
            # #823 / MM-02：本次 run 的请求模型是否支持视觉——决定 `user/message`
            # 的附件引用被物化成图片内容块还是降级为占位符。构造期按**主模型**，
            # run 内切 fallback 时由 Runtime 经 `vision_by_model_role` 更新（A2）。
            model_supports_vision=primary_supports_vision,
            # #823 / MM-02（B2）：图片块 `detail` 档位（PRD D4/D11 可配置）。
            image_detail=settings.image_detail,
            # #823 / MM-02（B4）：发送前归一化目标（PRD D11 可配置）。
            image_normalize_max_dimension=settings.image_normalize_max_dimension,
            image_normalize_max_bytes=settings.image_normalize_max_bytes,
        ),
        # #298 T7b：V2 记忆形成的宿主。它是**进程级单例**（装配期建一次，见
        # `memory/v2/assembly.py` 决定三），本函数每轮调用只是把它接上终结臂——
        # 因此这里传引用，绝不在这里新建（每轮新建 = 每轮起一条服务循环）。
        # 未装配时是 None：终结臂只跳过记忆形成。
        memory_formation=wiring.memory_formation,
        fallback_model=fallback_model,
        # #823 / MM-02（A2）：角色 → 视觉能力，供 Runtime 每次 build 前让投影跟随
        # 当前请求模型（PRD D6）。
        vision_by_model_role={
            "primary": primary_supports_vision,
            "fallback": fallback_supports_vision,
        },
        stream_idle_timeout=settings.model_stream_idle_timeout,
        stream_total_timeout=settings.model_stream_total_timeout,
        model_call_gate=model_call_gate,
        primary_model_name=config.model_name,
        fallback_model_name=(config.fallback.model_name if config.fallback is not None
                             else "fallback"),
        observability_sink=get_observability_sink(settings),
        steer_source=steer_source,
        # `#312`：run 作用域账本（暂停/恢复）+ 生效 fuse 的来源标识。装配点只透传。
        run_budget=run_budget,
        local_fuse_source=local_fuse_source,
        # #198：生效档位（未指定 = "main"）与被 tool_scope 剔除的工具名——
        # run_config 结构化日志与 run/started 事件的数据源。
        agent_profile=(agent_profile if agent_profile is not None else "main"),
        dropped_tools=dropped_tools,
        # #528：曝光级别定义集控制器（registry 无 deferred 工具时为 None，
        # 绑定路径与之前逐字相同）。
        tool_exposure=tool_exposure,
        # `#317`：stuck 暂停的证据端口（环境 revision + 策略版本）。装配点只透传——
        # 构造方是服务层（它才有一份"本次生效策略"的完整输入，恢复侧也用同一份函数
        # 现算再比较；ADR-0048 D8）。None = 不观测（CLI / 单测的既有路径逐字不变）。
        stuck_evidence=stuck_evidence,
        # `#318`：session 树账端口（跨 run / 跨会话共享）。装配点只透传；构造方是
        # 服务层（它才知道预算 key 与请求声明）。None = 不接 session 账（旧行为）。
        session_budget=session_budget,
        # `#524`：完成门可选域策略，装配点只透传（默认 None = 既有默认策略，
        # CLI / 既有单测路径行为零变化）。
        completion_policy=completion_policy,
    )
