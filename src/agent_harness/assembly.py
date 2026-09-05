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

from dataclasses import dataclass
from pathlib import Path

from agent_harness.agent import AgentRuntime
from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import CapabilityWiring, wire_capabilities
from agent_harness.config import Settings
from agent_harness.context.builder import ContextBuilder
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.storage import (
    OnStableBoundary,
    SqliteCheckpointStore,
    SqliteOperationLedger,
    SqliteSessionMetaStore,
)
from agent_harness.storage.s3_artifact import S3ArtifactStore
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tooling.approval import ApprovalResponse
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


async def build_runtime(
    *,
    settings: Settings,
    wiring: CapabilityWiring,
    stores: RecoveryStores,
    workspace_registry: WorkspaceRegistry,
    session_id: str,
    workspace: Path,
    max_steps: int,
    auto_approve: bool,
) -> AgentRuntime:
    """装配全栈 Runtime：调用方保证 stores 已 initialize、workspace 已就绪。

    模型经 create_chat_model(settings) 构造（测试替身注入点）；sandbox 由
    WorkspaceRegistry 统一创建并持久化映射（恢复时按映射还原）。
    """
    config = ModelConfig.from_settings(settings)
    model = create_chat_model(config)
    # Model Fallback 两级链（ADR-0014 决策 14/16）：FALLBACK_MODEL_PROVIDER
    # 已配 → 构造 fallback 模型；切换决策在 FallbackPolicy，编排由 Runtime
    # 的 per-run coordinator 负责（见 agent/fallback 接线）。
    fallback_model = None
    if config.fallback is not None:
        fallback_model = create_chat_model(config.fallback)

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

    if auto_approve:
        policy = PermissionPolicy.WORKSPACE_WRITE
        approval_callback = lambda _req: ApprovalResponse(approved=True, reason="auto-approve")
    else:
        # manual 模式 V1：拒绝所有危险操作（真正的交互式审批留到
        # WebSocket / pending queue，接缝点）。
        policy = PermissionPolicy.WORKSPACE_WRITE
        approval_callback = lambda _req: ApprovalResponse(approved=False, reason="manual approval not yet wired")

    for capability_tool in wiring.tools:
        registry.register(capability_tool)

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
            context_providers=list(wiring.context_providers),
        ),
        memory_writer=wiring.memory_writer,
        fallback_model=fallback_model,
        primary_model_name=config.model_name,
        fallback_model_name=(config.fallback.model_name if config.fallback is not None
                             else "fallback"),
    )
