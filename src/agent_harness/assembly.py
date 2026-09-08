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

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_harness.agent import AgentRuntime

logger = logging.getLogger(__name__)
from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import CapabilityWiring, wire_capabilities
from agent_harness.config import Settings
from agent_harness.context.builder import ContextBuilder
from agent_harness.model.concurrency import ModelCallGate
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.observability import get_observability_sink
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import (
    OnStableBoundary,
    SqliteCheckpointStore,
    SqliteOperationLedger,
    SqliteSessionMetaStore,
)
from agent_harness.storage.s3_artifact import S3ArtifactStore
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tooling.approval import ApprovalCallback, ApprovalResponse
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.tooling.overflow import ArtifactOverflowHandler
from agent_harness.tools import (
    ApplyPatchTool,
    BashTool,
    EditTool,
    GitDiffTool,
    GitStatusTool,
    GlobTool,
    GrepTool,
    InspectArtifactTool,
    ReadTool,
    WriteTool,
)


@dataclass
class RecoveryStores:
    """恢复子系统三 Store（同一 SQLite 文件，ADR-0004 布局）。

    AppState 与 CLI 各自构造实例、共享生命周期所有权；factory 只消费。"""

    operation_ledger: SqliteOperationLedger
    checkpoint_store: SqliteCheckpointStore
    session_meta_store: SqliteSessionMetaStore


def recovery_stores(database_path: str | Path) -> RecoveryStores:
    """构造恢复三 Store（同一 harness.db；未初始化——initialize_stores 幂等初始化）。"""
    path = Path(database_path)
    return RecoveryStores(
        operation_ledger=SqliteOperationLedger(path),
        checkpoint_store=SqliteCheckpointStore(path),
        session_meta_store=SqliteSessionMetaStore(path),
    )


async def initialize_stores(stores: RecoveryStores) -> None:
    """恢复三 Store 幂等初始化（并发首请求由调用方的 once 语义守护）。"""
    await stores.operation_ledger.initialize()
    await stores.checkpoint_store.initialize()
    await stores.session_meta_store.initialize()


async def assemble_wiring(
    settings: Settings,
) -> tuple[CapabilityRegistry, CapabilityWiring]:
    """CAPABILITIES env → 显式装配（capability 发现/降级归 wire_capabilities）。"""
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry, parse_capabilities_config(settings.capabilities), settings=settings
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


async def build_runtime(
    *,
    settings: Settings,
    wiring: CapabilityWiring,
    stores: RecoveryStores,
    workspace_registry: WorkspaceRegistry,
    session_id: str,
    workspace: Path,
    max_steps: int,
    permission_mode: PermissionPolicy = PermissionPolicy.WORKSPACE_WRITE,
    auto_approve: bool | None = None,
    approval_callback: ApprovalCallback | None = None,
    session_store: JsonlSessionStore | None = None,
    model_name: str | None = None,
    reasoning_effort: str | None = None,
    agent_profile: str | None = None,
    context_providers: list[str] | None = None,
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
    """
    # agent_profile 运行时消费（ADR-0020a，RUNTIME 子批次）：查 BUILTIN_PROFILES
    # 拿 AgentSpec——main/None 走原路径（registry 全量、无 system_prompt 注入），
    # coding/research_review 收窄 registry 到 spec.tool_scope + 注入 spec.system_prompt。
    # 未知名字 web 层已 422，这里 KeyError 再响亮失败一次（防御性，不应发生）。
    profile_spec = None
    if agent_profile is not None:
        from agent_harness.agent.profiles import BUILTIN_PROFILES
        profile_spec = BUILTIN_PROFILES[agent_profile]

    config = (ModelConfig.from_settings(settings) if model_name is None
              else ModelConfig.from_catalog(settings, model_name))
    model = create_chat_model(config, reasoning_effort=reasoning_effort)
    # Model Fallback 两级链（ADR-0014 决策 14/16）：FALLBACK_MODEL_PROVIDER
    # 已配 → 构造 fallback 模型；切换决策在 FallbackPolicy，编排由 Runtime
    # 的 per-run coordinator 负责（见 agent/fallback 接线）。
    fallback_model = None
    if config.fallback is not None:
        fallback_model = create_chat_model(
            config.fallback, reasoning_effort=reasoning_effort,
        )
    # 进程级模型并发闸（#89）：本次 build_runtime 与其派生的所有 child 共享
    # 同一实例（全局在飞模型调用数的语义）。
    model_call_gate = ModelCallGate(settings.model_max_concurrency)

    sandbox = workspace_registry.create(session_id, workspace_root=workspace)
    registry = ToolRegistry()
    for tool_cls in (
        ReadTool, WriteTool, BashTool, EditTool, ApplyPatchTool,
        GlobTool, GrepTool, GitStatusTool, GitDiffTool,
    ):
        registry.register(tool_cls(sandbox))

    overflow_handler = None
    if any((settings.artifact_store_endpoint, settings.artifact_store_bucket,
            settings.artifact_store_access_key, settings.artifact_store_secret_key,
            settings.artifact_store_region)):
        artifact_store = S3ArtifactStore(settings, session_id=session_id)
        registry.register(InspectArtifactTool(artifact_store))
        overflow_handler = ArtifactOverflowHandler(artifact_store, settings.artifact_overflow_chars)

    # Phase 5：permission_mode 是会话级 PermissionPolicy 上限（审批阈值）。
    # approval_callback 由调用方决定：None → 安全默认（全批），注入 → 交互审批。
    # 切片 B：ApprovalCallback 已 async 化（外部 /approve 交互式审批需要 run 暂停）。
    policy = permission_mode
    if auto_approve is False and approval_callback is None:
        async def approval_callback(_req):  # type: ignore[no-redef]
            return ApprovalResponse(approved=False, reason="manual approval not yet wired")
    elif approval_callback is None:
        async def approval_callback(_req):  # type: ignore[no-redef]
            return ApprovalResponse(approved=True, reason="auto-approve")

    for capability_tool in wiring.tools:
        # multiagent 依赖 session_store 建独立 child session——缺席时降级缺席
        # （不注册 delegate，单代理照常），与 optional capability 语义一致。
        if isinstance(capability_tool, DelegateTool) and session_store is None:
            logger.warning(
                "multiagent 已启用但未提供 session_store，delegate 工具降级缺席"
            )
            continue
        registry.register(capability_tool)

    # agent_profile tool_scope 收窄（ADR-0020a）：仅在非 main profile 时过滤——
    # main 的 _MAIN_TOOLS 是全量的超集，filter 等价不过滤，但若未来新增了一个
    # tool_scope 未声明的工具，filter 会隐性收窄它。所以 main/None 走原路径不 filter，
    # 只有 coding/research_review 才收窄。收窄后 registry 流向所有下游：
    # ToolExecutor / AgentRuntime / multiagent activate 的 source_registry。
    if profile_spec is not None and agent_profile != "main":
        registry = registry.filtered(profile_spec.tool_scope)

    # multiagent 激活（ADR-0015）：模型链与 registry 已就绪，注入 child 的
    # 全部依赖。executor_factory 闭包捕获父级审批/策略/记账——child 与 parent
    # 同一审批面（决策 11 权限传递）。
    if wiring.multiagent_provider is not None and session_store is not None:
        from agent_harness.agent.factory import AgentFactory

        def _child_executor_factory(child_registry: ToolRegistry) -> ToolExecutor:
            return ToolExecutor(
                child_registry, policy=policy, approval_callback=approval_callback,
                overflow_handler=overflow_handler,
                operation_ledger=stores.operation_ledger,
            )

        wiring.multiagent_provider.activate(
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
            ),
            source_registry=registry,
            session_store=session_store,
            workspace_registry=workspace_registry,
            parent_session_id=session_id,
        )

    return AgentRuntime(
        model=model,
        registry=registry,
        executor=ToolExecutor(registry, policy=policy, approval_callback=approval_callback,
                              overflow_handler=overflow_handler,
                              operation_ledger=stores.operation_ledger),
        max_steps=max_steps,
        checkpoint_policy=OnStableBoundary(stores.checkpoint_store),
        session_meta_store=stores.session_meta_store,
        context_builder=ContextBuilder(
            model, max_context_tokens=settings.max_context_tokens,
            auto_compact_threshold=settings.auto_compact_threshold,
            hard_guard_threshold=settings.hard_guard_threshold,
            # context_providers 运行时消费（ADR-0020b）：会话请求字段按 name 筛选
            # wiring 自动装配的 provider 子集；None=默认全量，[]=显式零，未知名字 fail-open。
            context_providers=_select_context_providers(
                wiring.context_providers, context_providers,
            ),
            system_prompt=(profile_spec.system_prompt if profile_spec is not None else None),
        ),
        memory_writer=wiring.memory_writer,
        fallback_model=fallback_model,
        stream_idle_timeout=settings.model_stream_idle_timeout,
        stream_total_timeout=settings.model_stream_total_timeout,
        model_call_gate=model_call_gate,
        primary_model_name=config.model_name,
        fallback_model_name=(config.fallback.model_name if config.fallback is not None
                             else "fallback"),
        observability_sink=get_observability_sink(settings),
    )
