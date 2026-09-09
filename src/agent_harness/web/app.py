"""FastAPI 应用工厂 + 路由（Phase 9/10 精简版）。

create_app() 是单一入口——传入 Settings，返回装配好的 FastAPI。
测试用 test settings 注入；生产用 Settings() 从 .env 读。

路由契约见模块 docstring（web/__init__.py）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path, PureWindowsPath
from typing import Any

import jwt
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response

from agent_harness.agent import AgentEvent
from agent_harness.assembly import RecoveryStores, initialize_stores
from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import CapabilityWiring, wire_capabilities
from agent_harness.config import Settings
from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.logging import setup_logging
from agent_harness.model.config import (
    PROVIDER_PRESETS,
    ModelConfig,
    _pick_capabilities,
    parse_model_catalog,
)
from agent_harness.observability import flush_process_sink
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import JsonlSessionStore, SessionEvent
from agent_harness.session.event import RUNTIME_EVENT_SCHEMA_VERSION
from agent_harness.session.queue import MessageQueueManager
from agent_harness.session.service import (
    ActiveRunConflict,
    AmendOptions,
    ApprovalAlreadyResolved,
    ApprovalQueueMissing,
    ApprovalRequestMissing,
    InvalidDecision,
    InvalidSessionId,
    QueueItemNotFound,
    RecoveryConflict,
    SessionNotFound,
    SessionService,
    SteerTargetNotFound,
    WorkspaceNameInvalid,
    validate_session_id,
)
from agent_harness.storage import (
    SqliteCheckpointStore,
    SqliteOperationLedger,
    SqliteSessionMetaStore,
)
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.contract import (
    PERMISSION_MODE_DESCRIPTIONS,
    PermissionPolicy,
)
from agent_harness.web.runmanager import RunManager
from agent_harness.web.serialization import (
    build_event_payload,
    build_session_event_payload,
)

# ── Staged amend 字段的人类可读描述（Phase 5 + Ticket B1）──────────────
# 这些 dict 是 POST /api/sessions validator 与 GET 清单端点的**单一事实源**：
# validator 用它们的 key 集合判断合法值；GET 端点用它们的 value 投影 display_name +
# description。两边引用同一份常量 → 加新档位只改一处，validator 与清单永不漂移。
# 与 PERMISSION_MODE_DESCRIPTIONS（tooling/contract.py）同模式（Reuse First §6）。

#: reasoning_effort 三档（已运行时消费——经 create_chat_model 注入 model_kwargs）。
REASONING_EFFORT_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "minimal": {
        "display_name": "轻量",
        "description": "最少推理开销，最快但不够深入。",
    },
    "standard": {
        "display_name": "标准",
        "description": "平衡的推理深度，适用于常规任务（默认）。",
    },
    "deep": {
        "display_name": "深度",
        "description": "最多推理开销，较慢但最深入。",
    },
}

#: agent_profile 三档（已运行时消费——system_prompt 经 ContextBuilder 注入 + tool_scope 经 registry.filtered 收窄，ADR-0020a）。
AGENT_PROFILE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "main": {
        "display_name": "通用",
        "description": "通用编排 Agent（默认）。",
    },
    "coding": {
        "display_name": "编程",
        "description": "专精代码编辑、调试和构建任务。",
    },
    "research_review": {
        "display_name": "研究审查",
        "description": "专精研究、检索和审查任务。",
    },
}

#: context_providers 已装配清单投影（ADR-0020b，已运行时消费——按 name 筛选
#: wiring 自动装配的 ContextProvider 子集注入 ContextBuilder）。id 必须与
#: MemoryContextProvider.name / SkillCatalogContextProvider.name 严格对齐——
#: 前端据本清单渲染选项，用户选中的 id 经 POST /api/sessions 回传触发筛选。
CONTEXT_PROVIDER_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "memory": {
        "display_name": "记忆",
        "description": "注入与用户相关的召回记忆。",
    },
    "skills": {
        "display_name": "技能",
        "description": "注入可用技能目录（名称 + 描述）。",
    },
}

# ── Request / Response schemas ──


class CreateSessionRequest(BaseModel):
    """POST /api/sessions 的请求体。"""

    # 空 task 直接 422（FastAPI 自动校验）；纯空白 task 容忍（runtime 侧无意义但不危险）。
    # max_length 封顶：task 会逐字持久化进 JSONL（user/message）并整体进模型上下文，
    # 无上限时一个多 MB 请求体就能写爆日志 + 撑爆 context。
    task: str = Field(min_length=1, max_length=100_000)
    workspace: str | None = None  # None → 用默认 workspace；只接受单段目录名（见 _validate_workspace_name）
    max_steps: int = Field(default=10, ge=1, le=200)  # 非正数 / 过大 → 422（防客端刷爆循环预算）
    # Phase 5：permission_mode 是会话级「审批阈值」声明（不是硬墙）。三档真实
    # PermissionPolicy；未知值 → 422。permission_mode 决定 ToolExecutor 的 policy
    # 上限，审批本身仍走 ApprovalCallback（默认 auto-approve）。
    permission_mode: str = Field(default="workspace-write")
    # auto_approve 保留为 deprecated alias（向后兼容）：true ≡ workspace-write
    # + auto-approve callback；false ≡ workspace-write + deny callback。两者同传
    # 时 permission_mode 优先。两个字段都缺省 → workspace-write + auto-approve
    # （现行为不变）。
    auto_approve: bool = True
    # amend contract fields（Phase 5 staged → RUNTIME 子批次全部消费：reasoning_effort
    # / agent_profile / context_providers）
    reasoning_effort: str | None = None
    agent_profile: str | None = None
    context_providers: list[str] | None = None

    @field_validator("reasoning_effort")
    @classmethod
    def _validate_reasoning_effort(cls, v: str | None) -> str | None:
        if v is not None and v not in REASONING_EFFORT_DESCRIPTIONS:
            valid = ", ".join(REASONING_EFFORT_DESCRIPTIONS)
            raise ValueError(f"reasoning_effort must be one of: {valid}")
        return v

    @field_validator("agent_profile")
    @classmethod
    def _validate_agent_profile(cls, v: str | None) -> str | None:
        if v is not None and v not in AGENT_PROFILE_DESCRIPTIONS:
            valid = ", ".join(AGENT_PROFILE_DESCRIPTIONS)
            raise ValueError(f"agent_profile must be one of: {valid}")
        return v

    @field_validator("context_providers")
    @classmethod
    def _validate_context_providers(cls, v: list[str] | None) -> list[str] | None:
        # 运行时消费（ADR-0020b）：会话请求按已装配 provider 的 name 子集筛选。
        # 不对未知名字 422——provider 是否装配取决于 CAPABILITIES 运行时状态，
        # 静态校验没法判定；未知名字在 assembly 层 fail-open 跳过（不变量 #21）。
        # 这里只校验形状：每项是非空字符串（Pydantic 已保证 list[str]，空 list 合法）。
        if v is None:
            return v
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    "context_providers entries must be non-empty strings"
                )
        return v

    # 会话级模型选择（ADR-0016 §5，C6）：None = 默认链（现行为不变）；
    # 命名 = AGENT_MODELS catalog 条目，未知名字 422。fallback 链不受影响。
    model: str | None = None

    @field_validator("permission_mode")
    @classmethod
    def _validate_permission_mode(cls, v: str) -> str:
        valid = {p.value for p in PermissionPolicy}
        if v not in valid:
            raise ValueError(
                f"permission_mode must be one of {sorted(valid)}; got {v!r}"
            )
        return v


class ApproveRequest(BaseModel):
    """POST /api/sessions/{id}/approve 的请求体（Phase 5 + Batch 5.1）。

    decision 是 spec 契约（03 §9 PermissionDecision）；approved 是兼容字段。
    两者都传时 decision 优先；只传 approved 时从它推导（True→approve_once，
    False→deny）。decision 必须命中 requested 事件里 allowed_decisions。
    """

    approval_id: str | None = None
    approved: bool = True
    decision: str | None = None
    reason: str = ""


class ResumeRequest(BaseModel):
    """POST /api/sessions/{id}/resume 的请求体。"""

    task: str = Field(min_length=1, max_length=100_000)
    # staged amend 字段（可选，None = 默认行为）
    reasoning_effort: str | None = None
    agent_profile: str | None = None
    context_providers: list[str] | None = None
    model: str | None = None


class SendMessageRequest(BaseModel):
    """POST /api/sessions/{id}/messages 的请求体（PRD §5.3 续聊入口）。

    ``mode`` 取自 PRD 锁定决策 D-7：

      * ``queue``（默认）——空闲 → 直接拉起新 run；在途 → 入队等待
        （不抢断不丢消息，下个 run 自然消费）。
      * ``steer``——仅在途 run 时合法：注入引导请求，被当前 step 边界
        的 run 读取（不重启 run、不改写历史事件）。
    """

    content: str = Field(min_length=1, max_length=100_000)
    mode: str = Field(default="queue", pattern="^(queue|steer)$")
    max_steps: int = Field(default=10, ge=1, le=200)
    # staged amend 字段（可选，None = 默认行为）
    reasoning_effort: str | None = None
    agent_profile: str | None = None
    context_providers: list[str] | None = None
    model: str | None = None


class SessionSummary(BaseModel):
    """GET /api/sessions 返回的单条摘要。"""

    session_id: str
    event_count: int
    first_event_time: str | None = None
    last_event_time: str | None = None
    # Gap 3 (P0)：首条 user/message content 截断 128 字符——前端 SessionList
    # 零额外请求渲染标题（保留 events 扫描作为后端未返回时的降级路径）。
    first_user_message: str | None = None
    # Gap 2 (P2)：Langfuse trace 关联。真实 trace 由 Phase 15 可观测层创建；
    # 未接入前恒为 null（绝不伪造，前端显示「未追踪」）。
    trace_id: str | None = None


class AppState:
    """app 内部共享状态的薄容器——避免全局变量。

    V1：单进程内存里的 runtime 工厂 + session store 根目录。
    Capability 子系统按 CAPABILITIES 配置惰性装配一次（wire_capabilities），
    进程退出时统一关闭；配置为空 = 零行为变化。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # sessions 和 workspace 默认放在 .agent/ 下（跟现有诊断日志一致）
        self.sessions_root = Path(settings.workspace_dir) / "sessions"
        self.workspaces_root = Path(settings.workspace_dir) / "workspaces"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.store = JsonlSessionStore(root=self.sessions_root)
        # detached-run 托管（ADR-0016 §2.1，D-A）：run 生命周期与 HTTP 请求
        # 解耦——SSE 订阅者离开只 unsubscribe，取消只经 POST /cancel 或孤儿
        # 回收（宽限期 Settings.run_disconnect_grace_seconds）。
        self.run_manager = RunManager(
            disconnect_grace_seconds=settings.run_disconnect_grace_seconds,
        )
        # Phase 5：会话级待审批队列——当 permission_mode 非 danger-full-access
        # 且 ToolExecutor 触发 needs_approval 时，callback 经此 queue 与前端 /approve
        # 对接。key 是 session_id；安全默认下（auto-approve）callback 不挂 queue。
        self.approval_queues: dict[str, PendingApprovalQueue] = {}
        # Phase Multiturn T2：续聊排队 + steer 请求注册表（PRD §5.3 / §6）。
        self.message_queues = MessageQueueManager()
        # 恢复基础设施（R8-1，用户拍板接线）：三 Store 共享同一 SQLite 文件
        # （ADR-0004 布局），WorkspaceRegistry 持久化 session↔sandbox 映射。
        # initialize 是异步的 → 惰性执行（ensure_stores），兼容不走 lifespan
        # 的测试路径。
        self.harness_db = Path(settings.workspace_dir) / "harness.db"
        self.operation_ledger = SqliteOperationLedger(self.harness_db)
        self.checkpoint_store = SqliteCheckpointStore(self.harness_db)
        self.session_meta_store = SqliteSessionMetaStore(self.harness_db)
        self.workspace_registry = WorkspaceRegistry(
            root=Path(settings.workspace_dir), backend="local"
        )
        self._stores_lock = asyncio.Lock()
        self._stores_ready = False
        # Capability 装配：只在首次使用时执行（含 Memory / Skills / demo 等）。
        # asyncio.Lock 守住 once 语义：并发首请求都看到 _wiring is None 时，
        # 只有一个真正执行 wire_capabilities，另一个等锁后复用结果
        # （否则第二个会在重复注册上炸掉 / 被降级吞掉）。
        self._wiring_lock = asyncio.Lock()
        self._registry: CapabilityRegistry | None = None
        self._wiring: CapabilityWiring | None = None
        self._closed = False  # shutdown 后置位：get_wiring 拒绝在关停后新装配

    async def ensure_stores(self) -> None:
        """惰性初始化恢复三 Store（幂等；并发首请求由锁守 once 语义）。"""
        if self._stores_ready:
            return
        async with self._stores_lock:
            if not self._stores_ready:
                await initialize_stores(self.stores)
                self._stores_ready = True

    @property
    def stores(self) -> RecoveryStores:
        """恢复三 Store 束（factory 消费；实例归 AppState 所有）。"""
        return RecoveryStores(
            operation_ledger=self.operation_ledger,
            checkpoint_store=self.checkpoint_store,
            session_meta_store=self.session_meta_store,
        )

    async def get_wiring(self) -> tuple[CapabilityRegistry, CapabilityWiring]:
        """惰性装配 Capability 子系统并缓存；返回 (registry, wiring)。

        shutdown 可能在 wire await 期间发生：入口和拿锁后都检查 _closed，
        wire 完成后再查一次——在途调用以 RuntimeError 失败，但刚装配好的
        wiring 仍留在字段上，由随后拿到锁的 shutdown 关闭（连接不泄露）。
        """
        if self._closed:
            raise RuntimeError("AppState is shut down")
        if self._wiring is not None and self._registry is not None:
            return self._registry, self._wiring
        async with self._wiring_lock:
            if self._closed:
                raise RuntimeError("AppState is shut down")
            if self._wiring is None or self._registry is None:
                config = parse_capabilities_config(self.settings.capabilities)
                registry = CapabilityRegistry()
                wiring = await wire_capabilities(registry, config, settings=self.settings)
                # 先落字段再查 _closed：锁在手上，shutdown 必然排在本次释放之后，
                # 它会从字段上取走这份 wiring 并关闭——绝不静默丢弃。
                self._registry, self._wiring = registry, wiring
                if self._closed:
                    raise RuntimeError("AppState is shut down")
            return self._registry, self._wiring

    @property
    def wiring(self) -> CapabilityWiring | None:
        """已装配的 wiring（未装配时 None；测试与 shutdown 用）。"""
        return self._wiring

    async def shutdown(self) -> None:
        """进程退出时关闭后台 run task、relay 与外部连接。幂等：重复调用只关闭一次。

        必须拿 _wiring_lock：否则在途 get_wiring 可能在 swap 之后才完成装配，
        装配出的 wiring 永远没人关（泄露 Milvus / embedding 连接）。
        先置位 _closed 再拿锁——让在途装配在 wire 完成后立刻失败，而不是
        把 wiring 交给一个已关停的 app。
        """
        self._closed = True
        # 先停 detached run（它们引用 wiring 的工具/模型），再关 wiring。
        await self.run_manager.aclose()
        async with self._wiring_lock:
            wiring, self._wiring, self._registry = self._wiring, None, None
        # 关闭知识在 CapabilityWiring.aclose（批次 A 候选 4）：memory 组件 +
        # lifecycle 通道逐项隔离关闭，web 层不再懂每种 capability 的关闭姿势。
        if wiring is not None:
            await wiring.aclose()


def _validate_workspace_name(state: AppState, workspace: str | None) -> str | None:
    """校验请求里的 workspace 字段——V1 安全边界：它是名字，不是路径。

    客户端若能直接传路径（"C:\\Users\\me"、"../../.."），Bash / Write 等工具
    就会以任意宿主目录为 sandbox 根执行（路径逃逸漏洞）。V1 采用最简单的
    安全规则：只接受单个路径段的目录名——
    - None → 返回 None（调用方用默认 session_id 目录，向后兼容）；
    - 单段相对名（"my-task"）→ 返回该名字，目录建在 workspaces_root 下；
    - 绝对路径 / 盘符 / 含 / 或 \\ 的多段名 / "." ".." → 422 拒绝。

    必须在任何 mkdir / Session 落盘之前调用：被拒请求不能留下任何痕迹。
    """
    if workspace is None:
        return None
    # PureWindowsPath 让盘符检查在非 Windows 平台上也生效（"C:foo" 在 POSIX
    # 是合法单段名，但语义上是 Windows 盘符相对路径——一律拒绝）。
    candidate = PureWindowsPath(workspace)
    if (workspace.strip() in ("", ".", "..")
            or candidate.drive or candidate.root or candidate.is_absolute()
            or "/" in workspace or "\\" in workspace):
        raise HTTPException(
            status_code=422,
            detail=f"workspace 只接受单个目录名（不接受路径）：{workspace!r}",
        )
    # 双保险：解析后的候选目录必须仍落在 workspaces_root 内（防符号链接逃逸）。
    resolved_root = state.workspaces_root.resolve()
    if not (resolved_root / workspace).resolve().is_relative_to(resolved_root):
        raise HTTPException(
            status_code=422,
            detail=f"workspace 越出 workspaces_root：{workspace!r}",
        )
    return workspace


#: 重放 backlog 阈值（durable 事件数，ADR-0016 §2.3）：after_seq 落后超过
#: 该值 → 单帧 stream/truncated 控制事件后收流，客户端走 GET /events 全量
#: 重建后带 after_seq=latest_seq 重连（02 §10.4 简化版；snapshot 层 DEFER）。
STREAM_REPLAY_MAX_EVENTS = 1000


def _render_model_option(
    *, id: str, provider: str, model_name: str, is_default: bool,
    capabilities: dict[str, Any], metadata_source: str,
) -> dict[str, Any]:
    """渲染一条 ModelOption（SDD 03 §16）。

    新契约字段：id / display_name / provider / is_default / is_available /
    context_window? / speed_tier? / supports_*? / metadata_source。
    旧字段 alias（向后兼容）：name / model / default。
    未知能力位不在 capabilities dict 里即不出现在响应（契约：「not guessed」）。
    """
    option: dict[str, Any] = {
        # 新契约字段
        "id": id,
        "provider": provider,
        "is_default": is_default,
        "is_available": True,  # catalog 无 disabled 概念，恒可用
        "metadata_source": metadata_source,
        # 旧字段 alias（前端切换期间保留，避免破坏现有客户端）
        "name": id,
        "model": model_name,
        "default": is_default,
    }
    # display_name 缺省回落到 model_name（更可读）。
    option["display_name"] = capabilities.get("display_name", model_name)
    # 已知能力位透传（未声明的键不在 capabilities 里 → 省略，不猜测）。
    for cap_key in ("context_window", "speed_tier", "supports_tools",
                    "supports_vision", "supports_reasoning_summary"):
        if cap_key in capabilities:
            option[cap_key] = capabilities[cap_key]
    return option


def _event_to_sse_dict(event: AgentEvent, session_id: str) -> dict[str, str]:
    """AgentEvent → SSE 帧（信封构建在 web/serialization.py，SSE/WS 共用）。"""
    return {"data": json.dumps(
        build_event_payload(event, session_id), ensure_ascii=False
    )}


def _session_event_to_sse_dict(event: SessionEvent, session_id: str) -> dict[str, str]:
    """SessionEvent → SSE 帧（重放通道，信封构建在 web/serialization.py）。"""
    return {"data": json.dumps(
        build_session_event_payload(event, session_id), ensure_ascii=False
    )}


#: CSP（集成 AI 移交，INTEGRATION_NOTES §4.1）：静态 HTML 的纵深防御——
#: 脚本/样式只认同源构建产物，img 放行 data:。所有响应统一携带（浏览器
#: 仅对 HTML 文档执行，JSON 响应带此头无害），避免漏掉任何静态入口。
_CSP_POLICY = "default-src 'self'; img-src 'self' data:"


class CSPHeaderMiddleware:
    """纯 ASGI 中间件：所有响应统一携带 CSP 头（行为契约同旧实现）。

    为什么不用 @app.middleware（BaseHTTPMiddleware）：SSE 流过其 anyio 内存
    流 machinery 时，客户端断连的取消会在嵌套中间件间传播污染请求栈（全量
    回归下曾放大为逐请求 500 "No response returned"）。纯 ASGI 直通流式
    帧，无缓冲无任务组（ADR-0016 §2.1 生产级加固）。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Content-Security-Policy"] = _CSP_POLICY
            await send(message)

        await self.app(scope, receive, send_wrapper)


class AuthSeamMiddleware:
    """纯 ASGI 中间件：身份认证 + IdentityContext 绑定（行为契约同旧实现）。

    fail-open/fail-closed 语义、claims 校验、401 形状逐字节不变
    （tests/test_identity.py / test_web_api.py 钉住）；差异仅在传输层：
    不经 BaseHTTPMiddleware 的任务组，SSE 断连取消不再跨请求传染。
    """

    def __init__(self, app: Any, settings: Settings) -> None:
        self.app = app
        self._settings = settings

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        identity = IdentityContext("local", "local", ["user", "session"])
        headers = Headers(scope=scope)
        authorization = headers.get("authorization")
        if self._settings.jwt_secret:
            # R6-4/R8-3（用户拍板 fail-closed）：配置了密钥 = 需要认证。
            # 匿名请求不再静默降级为 trusted local（此前配合 CORS * 等于把
            # agent API 开放给任意网页）；无 exp 的 token 一并拒绝（强制
            # 过期语义，永不过期的签名 token 等于永久凭证）。
            if not authorization:
                response: Response = JSONResponse(
                    {"detail": "Missing identity token"}, status_code=401)
                await response(scope, receive, send)
                return
            try:
                scheme, encoded = authorization.split(" ", 1)
                if scheme.lower() != "bearer":
                    raise ValueError("Expected Bearer token")
                # SecretStr 取明文给 jwt.decode；truthiness 判断仍基于密钥值
                # （SecretStr("") 为 falsy，未配置语义不变）。
                claims = jwt.decode(
                    encoded, self._settings.jwt_secret.get_secret_value(),
                    algorithms=["HS256"],
                    options={"require": ["tenant_id", "user_id", "exp"]})
                tenant, user = claims["tenant_id"], claims["user_id"]
                scopes = claims.get("scopes", ["user", "session"])
                if (not isinstance(tenant, str) or not tenant.strip()
                        or not isinstance(user, str) or not user.strip()
                        or not isinstance(scopes, list)
                        or any(not isinstance(s, str) for s in scopes)):
                    raise ValueError("Invalid identity claims")
                identity = IdentityContext(tenant, user, scopes)
            except (jwt.InvalidTokenError, ValueError):
                response = JSONResponse(
                    {"detail": "Invalid identity token"}, status_code=401)
                await response(scope, receive, send)
                return
        token = set_identity_context(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            identity_context_var.reset(token)


def create_app(settings: Settings | None = None, *, enable_cors: bool = True) -> FastAPI:
    """装配 FastAPI 应用。测试可注入 test settings；生产默认从 .env 读。"""
    if settings is None:
        settings = Settings()

    state = AppState(settings)

    # lifespan 替代 on_event：进程退出时关闭 Memory 子系统，避免泄露外部连接。
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # web 部署必须自己接诊断日志：uvicorn 默认只配 uvicorn.* logger，root
        # 无 handler 时 runtime/executor 的 log_event 全部 no-op——llm_call /
        # tool_operation / task_failed 审计链路整条消失（cli.py 有 setup_logging，
        # web 之前漏接）。幂等（重复调用先清 handlers）。
        setup_logging(settings.log_level, settings.workspace_dir)
        try:
            yield
        finally:
            await state.shutdown()
            # 旁路收尾（ADR-0018 D3）：服务停机前尽力发送剩余 Langfuse span
            # （有超时上限，不阻塞退出）。
            flush_process_sink()

    app = FastAPI(title="Agent Harness Inspector", version="0.1.0", lifespan=lifespan)
    app.state.agent = state  # 挂在 app.state 上，路由通过 request.app.state 取

    # Phase 14 lineage 路由（独立 router 文件——流式改造重刀 app.py 时的最小接入面）
    from agent_harness.web.lineage import register_lineage_routes

    register_lineage_routes(app, validate_session_id=validate_session_id)

    if not settings.jwt_secret:
        # R6-4：未配置密钥 = 本地信任模式（fail-open）。保留开发便利，但必须
        # 响亮告知——静默降级是原审计的核心危害。
        logging.getLogger("agent_harness.web").warning(
            "JWT_SECRET 未配置：API 以本地信任模式运行（所有请求视为 local 身份，"
            "不做身份校验）。生产部署必须配置 JWT_SECRET。"
        )

    # CSP（集成 AI 移交，INTEGRATION_NOTES §4.1）：静态 HTML 的纵深防御——
    # 脚本/样式只认同源构建产物，img 放行 data:。所有响应统一携带（浏览器
    # 仅对 HTML 文档执行，JSON 响应带此头无害），避免漏掉任何静态入口。
    _CSP_POLICY = "default-src 'self'; img-src 'self' data:"

    # 纯 ASGI 中间件（见类 docstring）：先 csp（内层）后 auth（外层），与
    # 旧 BaseHTTPMiddleware 版注册顺序逐层一致；CORS 仍最后添加 = 最外层。
    app.add_middleware(CSPHeaderMiddleware)
    app.add_middleware(AuthSeamMiddleware, settings=settings)

    if enable_cors:
        # V1 本地单用户：宽松 CORS 让 Vite dev server (5173) 能直连。
        # 多用户时收紧到已知 origin（接缝点）。
        # 必须最后添加 = 中间件栈最外层：浏览器预检（OPTIONS）天然不携带
        # Bearer，认证层在内层时预检直接 401，配置 JWT_SECRET 后跨域 dev
        # 模式整体失效。预检放行不削弱认证——数据请求仍逐个过认证层。
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ── 路由 ──

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/sessions")
    async def list_sessions() -> list[SessionSummary]:
        """列历史 session（按最近活动倒序）。

        列表页只需摘要字段——store.read_session_summary 单趟流式扫描
        （头部早退 + 末行），不再全量解析每个 JSONL（30 会话 × 2000 事件
        曾需秒级串行解析，现约几十 ms）。损坏行走 store 内全量回退，摘要
        语义与旧实现严格一致。同步磁盘 I/O 仍走 to_thread 卸载。
        """
        service = SessionService(app.state.agent)
        summaries = await service.list_sessions()
        return [
            SessionSummary(
                session_id=s["session_id"],
                event_count=s["event_count"],
                first_event_time=s["first_event_time"],
                last_event_time=s["last_event_time"],
                first_user_message=s["first_user_message"],
            )
            for s in summaries
        ]

    @app.get("/api/sessions/{session_id}/events")
    async def get_session_events(session_id: str) -> list[dict]:
        """读历史 SessionEvent——前端刷新后从此重建视图（不变量 #22）。

        session_id 先过安全校验（名字段，不是路径）；store 读是同步磁盘 I/O，
        走 to_thread 卸载（同 list_sessions）。
        """
        service = SessionService(app.state.agent)
        try:
            events = await service.get_events(session_id)
        except InvalidSessionId as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except SessionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return [e.to_dict() for e in events]

    @app.get("/api/models")
    async def list_models() -> dict[str, Any]:
        """列出可选模型（ADR-0016 §5，C6 + SDD 03 §16 ModelOption）。

        绝不携带任何密钥字段；is_default=true 的条目 = 不传 model 参数时的链。
        思考能力不进元数据（D-B③ 事件驱动：模型真吐思考才有 reasoning 事件）。

        Phase 2 加法（SDD 03 §16）：每条返回能力位元数据，来源标注 metadata_source。
        - 默认链：能力位来自 PROVIDER_PRESETS → metadata_source="provider_preset"；
        - catalog 条目：条目显式声明的能力位优先，回落 preset；条目自身声明时
          metadata_source="agent_models"，否则（全靠 preset 回落）="provider_preset"。
        未知能力位省略（契约：「not guessed」）。旧字段 name/model/default 作为
        alias 保留（前端切换期间不破）。
        """
        state = app.state.agent
        default_config = ModelConfig.from_settings(state.settings)
        default_provider = state.settings.model_provider
        default_caps = _pick_capabilities(PROVIDER_PRESETS.get(default_provider, {}))
        models: list[dict[str, Any]] = [_render_model_option(
            id=default_config.model_name,
            provider=default_provider,
            model_name=default_config.model_name,
            is_default=True,
            capabilities=default_caps,
            metadata_source="provider_preset",
        )]
        for entry in parse_model_catalog(state.settings):
            declared = entry.declared_capabilities()
            preset_caps = _pick_capabilities(PROVIDER_PRESETS.get(entry.provider, {}))
            # catalog 声明优先，preset 回落。metadata_source：条目声明了任何能力位
            # → agent_models；否则（全靠 preset）→ provider_preset。
            merged = {**preset_caps, **declared}
            source = "agent_models" if declared else "provider_preset"
            models.append(_render_model_option(
                id=entry.name,
                provider=entry.provider,
                model_name=entry.model_name,
                is_default=False,
                capabilities=merged,
                metadata_source=source,
            ))
        return {"models": models}

    @app.get("/api/permission-modes")
    async def list_permission_modes() -> dict[str, Any]:
        """列出后端能真实执行的权限模式（SDD 03 §10，Phase 2 加法）。

        返回 PermissionPolicy 全集 + 人类可读描述。诚实标注：当前 Web 层
        auto_approve 默认开（同步 callback），交互式审批是 Phase 5 的工作——
        这里只暴露「后端认识哪些 mode」，不假装审批已就绪。
        """
        modes = [
            {
                "id": policy.value,
                "display_name": desc["display_name"],
                "description": desc["description"],
            }
            for policy, desc in PERMISSION_MODE_DESCRIPTIONS.items()
        ]
        return {"modes": modes}

    @app.get("/api/capabilities")
    async def list_capabilities() -> dict[str, Any]:
        """列出已装配的 capability manifest（SDD 03 §17，Phase 2 加法）。

        默认 CAPABILITIES="" → registry.available() 为空 → 返回 {"capabilities": []}。
        前端据空列表自行 fallback 显示 chat/timeline（空就是空，不假装有基础能力）。

        投影规则：descriptor 无 surfaces 声明 → 保守默认（chat/timeline=true）；
        descriptor 显式声明 surfaces → 以声明为准。本轮没有 capability 填 surfaces，
        只搭骨架——具体 surfaces 声明是 Phase 6 的工作。
        """
        state = app.state.agent
        registry, _wiring = await state.get_wiring()
        available = registry.available()
        capabilities: list[dict[str, Any]] = []
        for descriptor in available:
            declared_surfaces = descriptor.surfaces or {}
            # 保守默认：未声明 surfaces 的 capability 只保证 chat + timeline 可用
            # （其余 surface 按 capability 显式声明）。
            surfaces = {
                "chat": declared_surfaces.get("chat", True),
                "timeline": declared_surfaces.get("timeline", True),
                "changes": declared_surfaces.get("changes", False),
                "terminal": declared_surfaces.get("terminal", False),
                "artifacts": declared_surfaces.get("artifacts", False),
            }
            actions = descriptor.actions or {
                # 保守默认：未声明 actions 的 capability 不主张任何交互动作可用。
                "permissions": False,
                "stop": False,
                "retry": False,
                "resume": False,
            }
            capabilities.append({
                "id": descriptor.name,
                "display_name": descriptor.display_name or descriptor.name,
                "version": descriptor.version,
                "provider_name": descriptor.provider_name,
                "surfaces": surfaces,
                "actions": actions,
            })
        return {"capabilities": capabilities}

    @app.get("/api/reasoning-efforts")
    async def list_reasoning_efforts() -> dict[str, Any]:
        """列出 reasoning_effort 可选档位（Ticket B1，SDD 03 §16 对齐 Phase 5）。

        reasoning_effort 已被运行时真实消费（经 create_chat_model 注入
        model_kwargs → extra_body 传给 API）；本端点暴露「后端认识哪些档位」。
        字段与 /api/permission-modes 同模式（{id, display_name, description}），
        单一事实源是模块级 REASONING_EFFORT_DESCRIPTIONS（validator 与清单
        引用同一份 → 永不漂移）。
        """
        efforts = [
            {
                "id": effort_id,
                "display_name": desc["display_name"],
                "description": desc["description"],
            }
            for effort_id, desc in REASONING_EFFORT_DESCRIPTIONS.items()
        ]
        return {"efforts": efforts}

    @app.get("/api/agent-profiles")
    async def list_agent_profiles() -> dict[str, Any]:
        """列出 agent_profile 可选档位（Ticket B1，SDD 03 §16 对齐 Phase 5）。

        同 reasoning-efforts：Phase 5 staged 契约的清单投影，运行时 no-op 不变。
        字段与 /api/permission-modes 同模式，单一事实源是 AGENT_PROFILE_DESCRIPTIONS。
        """
        profiles = [
            {
                "id": profile_id,
                "display_name": desc["display_name"],
                "description": desc["description"],
            }
            for profile_id, desc in AGENT_PROFILE_DESCRIPTIONS.items()
        ]
        return {"profiles": profiles}

    @app.get("/api/context-providers")
    async def list_context_providers() -> dict[str, Any]:
        """列出已装配的 context provider 清单（ADR-0020b 运行时消费）。

        动态投影 ``wiring.context_providers`` 的 ``name`` 属性（与
        MemoryContextProvider.name / SkillCatalogContextProvider.name 对齐）。
        display_name / description 从 CONTEXT_PROVIDER_DESCRIPTIONS 取——
        清单端点与 POST /api/sessions 共用同一 id 集合。

        未装配任何 capability（bare 配置）→ wiring.context_providers 为空 →
        返 ``{"providers": []}``（与 /api/capabilities 空目录降级同原则，
        不伪造基础项）。前端据空列表自行 fallback。
        """
        state = app.state.agent
        _, wiring = await state.get_wiring()
        providers: list[dict[str, Any]] = []
        for provider in wiring.context_providers:
            name = getattr(provider, "name", None)
            if not isinstance(name, str) or not name:
                # 未声明 name 的 provider（未来情况）不出现在清单——
                # 清单端点是稳定 id 契约，不暴露匿名项。
                continue
            desc = CONTEXT_PROVIDER_DESCRIPTIONS.get(name)
            providers.append({
                "id": name,
                "display_name": (desc["display_name"] if desc else name),
                "description": (desc["description"] if desc else ""),
            })
        return {"providers": providers}

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequest):
        """起新 session + 跑任务，流式返回 AgentEvent（SSE）。

        ADR-0016 §2.1（D-A）：run 由 RunManager 以 detached task 驱动，与
        本次 HTTP 请求生命周期解耦——断连（本 generator 被取消）只做
        unsubscribe，run 继续跑到终态；显式取消走 POST /cancel。
        """
        service = SessionService(app.state.agent)
        state = app.state.agent

        # context_providers handler-level 422（ADR-0021 模式，适配 ADR-0020b 的
        # name 属性机制）：validator 无法访问 AppState/wiring（Pydantic parse 早于
        # handler），故在 handler 内对 wiring 真实装配的 id 集合校验——与 model
        # 字段的 from_catalog 422 模式一致。未知 id → 422 + 可用清单。
        if req.context_providers is not None:
            _, wiring = await state.get_wiring()
            wired_ids = {getattr(p, "name", None) for p in wiring.context_providers}
            wired_ids.discard(None)
            unknown = [pid for pid in req.context_providers if pid not in wired_ids]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"context_providers contains unknown ids {unknown}; "
                        f"available: {sorted(wired_ids)}"
                    ),
                )

        permission_mode = PermissionPolicy(req.permission_mode)
        permission_mode_explicit = "permission_mode" in req.model_fields_set
        auto_approve_explicit = "auto_approve" in req.model_fields_set

        try:
            result = await service.create_and_launch(
                task=req.task,
                workspace_name=req.workspace,
                max_steps=req.max_steps,
                permission_mode=permission_mode,
                permission_mode_explicit=permission_mode_explicit,
                auto_approve_explicit=auto_approve_explicit,
                auto_approve=req.auto_approve,
                amend=AmendOptions.from_request(req),
            )
        except WorkspaceNameInvalid as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except InvalidDecision as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

        session, run, subscriber = result.session, result.run, result.subscriber

        async def event_generator():
            """SSE 事件源：消费订阅队列，转成 SSE 帧。

            断连时 EventSourceResponse 取消本 generator → finally unsubscribe
            （run 不受影响）；run 终结 → sentinel → 流干净收尾。
            """
            try:
                while True:
                    event = await subscriber.queue.get()
                    if event is state.run_manager.DONE:
                        break
                    yield _event_to_sse_dict(event, session.session_id)
            finally:
                run.unsubscribe(subscriber)

        return EventSourceResponse(event_generator())

    @app.get("/api/sessions/{session_id}/stream")
    async def stream_session(session_id: str, after_seq: int = -1):
        """重连续传（ADR-0016 §2.3，C4/C5）：重放 durable 事实 + 接上在途流。

        客户端维护 lastAppliedSeq，断线后带 after_seq 重连：
        1. session 无事件 → 404；
        2. 重放 durable 事件（after_seq < seq ≤ replay_upto，按 seq 序）；
        3. backlog 超过 STREAM_REPLAY_MAX_EVENTS → 单帧 stream/truncated
           控制事件（无 seq，非运行事实）后收流——客户端走 GET /events
           全量重建后带 after_seq=latest_seq 重连；
        4. run 在途 → 接上 live 流（先订阅后取游标，保证重放与 live 无缝
           无重复）；run 已终态/不在途 → 重放到 latest 后正常收尾
           （崩溃遗留的悬空 run 不伪造终态，修复走 POST /recover）。
        客户端对重放帧与 live 帧做同一 seq 幂等投影（C5）。
        """
        service = SessionService(app.state.agent)
        try:
            handle = await service.stream_reconnect(
                session_id=session_id,
                after_seq=after_seq,
                max_replay_events=STREAM_REPLAY_MAX_EVENTS,
            )
        except InvalidSessionId as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except SessionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

        events = handle.events
        latest_seq = handle.latest_seq
        run = handle.run
        subscriber = handle.subscriber
        replay_upto = handle.replay_upto
        state = app.state.agent

        if latest_seq - after_seq > STREAM_REPLAY_MAX_EVENTS:
            async def truncated_generator():
                control = {
                    "type": "stream/truncated",
                    "data": {"after_seq": after_seq, "latest_seq": latest_seq},
                    "seq": None, "run_id": None, "step_id": None,
                    "session_id": session_id,
                    "schema_version": RUNTIME_EVENT_SCHEMA_VERSION,
                    "durability": "transient",
                }
                yield {"data": json.dumps(control, ensure_ascii=False)}

            return EventSourceResponse(truncated_generator())

        async def event_generator():
            try:
                for event in events:
                    if after_seq < event.seq <= replay_upto:
                        yield _session_event_to_sse_dict(event, session_id)
                if subscriber is None:
                    return
                while True:
                    item = await subscriber.queue.get()
                    if item is state.run_manager.DONE:
                        break
                    if item.seq is not None and item.seq <= replay_upto:
                        continue  # 重放已覆盖（订阅与取游标窗口内的入队）
                    yield _event_to_sse_dict(item, session_id)
            finally:
                if subscriber is not None and run is not None:
                    run.unsubscribe(subscriber)

        return EventSourceResponse(event_generator())

    @app.post("/api/sessions/{session_id}/resume")
    async def resume_session(session_id: str, req: ResumeRequest):
        """最小 Resume（Phase 5）：重建 Session 后追加一轮新 user input。

        这是「续跑」而非精确恢复中断 run：Session.resume() 重建 append-only
        历史与 dangling 修复，RunManager.launch 驱动一轮新的 Agent Loop。
        在途 session 拒绝 409，避免同一 session 并发两轮。
        """
        service = SessionService(app.state.agent)
        try:
            result = await service.resume_and_launch(
                session_id=session_id,
                task=req.task,
                amend=AmendOptions.from_request(req),
            )
        except InvalidSessionId as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except SessionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ActiveRunConflict as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

        session_id = result.session.session_id
        run, subscriber = result.run, result.subscriber
        state = app.state.agent

        async def event_generator():
            try:
                while True:
                    event = await subscriber.queue.get()
                    if event is state.run_manager.DONE:
                        break
                    yield _event_to_sse_dict(event, session_id)
            finally:
                run.unsubscribe(subscriber)

        return EventSourceResponse(event_generator())

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str) -> dict[str, str]:
        """显式取消在途 run（ADR-0016 §2.2，D-A）：前端 Esc/停止的唯一取消通道。

        detached-run 下断连不再取消——本端点是仅有的两个外部终止路径之一
        （另一个是孤儿回收）。语义：在途 → 200 cancelling；无在途 run →
        200 no_active_run（幂等成功：用户按 Esc 与 run 恰好刚终结的竞态是
        常态不是错误）；session 不存在 → 404。取消与失败不混淆（02 §17）：
        run/failed data.reason=cancelled，与异常臂（无 reason）、孤儿回收
        （reason=orphaned）区分。
        """
        service = SessionService(app.state.agent)
        try:
            cancelled = await service.cancel(session_id)
        except InvalidSessionId as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except SessionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"status": "cancelling" if cancelled else "no_active_run"}

    @app.post("/api/sessions/{session_id}/approve")
    async def approve_tool_call(session_id: str, req: ApproveRequest) -> dict[str, str]:
        """交互式审批决策入口（Phase 5 切片 C + Batch 5.1）：前端拿到
        tool/approval-requested 事件后，调本端点注入批准/拒绝决策，唤醒 run
        内阻塞的 callback。

        语义：
          成功 resolve → 200 ok（run 在 callback 处继续；resolved 事件由 callback 写入）
          decision 不在 requested 事件的 allowed_decisions 内 → 422（违反契约）
          approval_id 已 resolved → 409（防重复决策；幂等性拒绝）
          approval_id 不存在 → 404（前端过期事件或非本 session 的 id）
          无 approval_id（旧 seam 调用）→ 200 received（向后兼容）
          session 不存在 → 404
        """
        if req.approval_id is None:
            # 旧 seam 入口（Phase 2 阶段性占位）：不解析任何决策，仅回 200 信号；
            # 不要求 session 存在——Phase 2 seam 测试用任意 id 探活。
            return {
                "status": "received",
                "note": "auto-approve is default; interactive approval via approval_id",
            }
        service = SessionService(app.state.agent)
        try:
            result = await service.resolve_approval(
                session_id=session_id,
                approval_id=req.approval_id,
                approved=req.approved,
                decision=req.decision,
                reason=req.reason,
            )
        except InvalidSessionId as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except SessionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ApprovalQueueMissing as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ApprovalRequestMissing as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except InvalidDecision as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except ApprovalAlreadyResolved as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return {
            "status": "resolved",
            "approval_id": req.approval_id,
            "decision": result.decision.value,
        }

    @app.post("/api/sessions/{session_id}/recover")
    async def recover_session(session_id: str) -> list[dict]:
        """恢复崩溃 session（R8-1 接线）：RecoveryCoordinator 唯一入口（07 §9）。

        修复 dangling tool_call（配对合成）、按 Ledger 终态精确回填结果、
        PENDING 默认 skip；RUNNING/UNKNOWN 需要人工裁决时返回 409（不伪造、
        不盲跑，不变量 #14）。幂等：重复调用靠事件配对自然跳过已修复项。
        """
        service = SessionService(app.state.agent)
        try:
            events = await service.recover(session_id)
        except InvalidSessionId as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except SessionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except RecoveryConflict as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return [e.to_dict() for e in events]

    # ── 续聊入口（PRD §5.3）───────────────────────────────────────────
    # 双模式：queue（默认）= 入队/直接拉起；steer = 注入在途 run。
    # launched 分支返回 SSE 流（PRD 锁定 D-10：续聊端点响应与创建端点一致）；
    # queued / steered 分支返回 JSON 确认（不打开流，前端订阅既有 SSE/WS）。
    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, req: SendMessageRequest):
        """续聊消息入口（Phase Multiturn T2 / PRD §5.3）。

        ``mode=queue``：空闲 → 直接 resume_and_launch 拉起新 run，返回
        SSE 流（同创建端点语义）；在途 → 入队并返回 JSON 确认。
        ``mode=steer``：仅在途 run 时合法——注册 SteerRequest 并返回
        JSON 确认；无在途 run → 409（steer 必须有目标）。
        """
        service = SessionService(app.state.agent)
        try:
            result = await service.send_message(
                session_id=session_id,
                content=req.content,
                mode=req.mode,
                max_steps=req.max_steps,
                amend=AmendOptions.from_request(req),
            )
        except InvalidSessionId as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except SessionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ActiveRunConflict as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except QueueItemNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except SteerTargetNotFound as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

        if result.status == "launched":
            # 与创建端点同形：SSE 直驱 run（ADR-0016 detached-run）。
            run = result.run
            subscriber = result.subscriber
            state = app.state.agent

            async def event_generator():
                try:
                    while True:
                        event = await subscriber.queue.get()
                        if event is state.run_manager.DONE:
                            break
                        yield _event_to_sse_dict(event, session_id)
                finally:
                    run.unsubscribe(subscriber)

            return EventSourceResponse(event_generator())
        # queued / steered：JSON 确认（不打开流——前端订阅既有 SSE/WS）。
        return result.to_response()

    @app.post("/api/sessions/{session_id}/queue/{queue_id}/cancel")
    async def cancel_queue_item(session_id: str, queue_id: str) -> dict[str, str]:
        """取消尚未消费的排队消息（PRD §5.3 / D-7）。

        语义：取消成功 → 200 cancelled；queue_id 已取消 / 已消费 /
        不存在 → 404；session 不存在 → 404。幂等失败（防覆盖式重置语义）。
        """
        service = SessionService(app.state.agent)
        try:
            cancelled = await service.cancel_queue(
                session_id=session_id, queue_id=queue_id
            )
        except InvalidSessionId as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except SessionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except QueueItemNotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"status": "cancelled" if cancelled else "already_consumed"}

    # ── WebSocket 多路复用通道（T2 / PRD §5.1）──────────────────────
    # 主 streaming 通道：单连接订阅多个 session、推增量事件；
    # 心跳 ping/pong；断线重连先推快照再增量。SSE 保留为灰度兼容路径。
    # 不维护第二套 session 真相（不变量 #22）——所有事件源于 RunManager 订阅。
    from agent_harness.web.websocket import handle_websocket

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """WS 主入口（PRD §5.1）：接受连接后交由 handle_websocket 多路复用。

        WS 只做传输——业务决策一律走 SessionService。WebSocketDisconnect
        是正常客户端断开，吞掉不打日志。
        """
        try:
            await handle_websocket(websocket, app.state.agent)
        except WebSocketDisconnect:
            # 正常断开：客户端关页 / 重连切换。
            return

    return app


def mount_static(app: FastAPI) -> None:
    """挂载前端构建产物为静态资源。

    独立于 ``create_app`` —— 测试在 ``create_app`` 返回后追加的自定义路由
    （如 ``/identity-probe``）不会被 StaticFiles Mount 遮蔽。生产部署由
    uvicorn ``--factory`` 调 ``create_prod_app``（= ``create_app`` +
    ``mount_static``)，或由反向代理直接服务静态资源、仅将 ``/api``
    转发到本服务。

    部署约束：静态挂载只适配本地信任模式（未配置 JWT_SECRET）。fail-closed
    生效时全量默认拒绝（test_auth_fail_closed 契约），而浏览器顶层导航无法
    携带 Bearer——index.html 都会 401。生产 + JWT 的支持形态是反向代理：
    静态资源在代理层直出，仅 /api 转发到本服务（前端带 Bearer 调用）。
    """
    web_dist = Path(__file__).resolve().parent.parent.parent.parent / "web" / "dist"
    if web_dist.exists():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="static")


def create_prod_app(settings: Settings | None = None) -> FastAPI:
    """生产工厂：``create_app`` + ``mount_static``。

    dev.sh / Dockerfile 用 ``uvicorn agent_harness.web.app:create_prod_app
    --factory``；测试仍直调 ``create_app``——不挂静态资源，避免 Mount
    遮蔽测试后加的 probe 路由。
    """
    app = create_app(settings, enable_cors=True)
    mount_static(app)
    return app
