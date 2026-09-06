"""FastAPI 应用工厂 + 路由（Phase 9/10 精简版）。

create_app() 是单一入口——传入 Settings，返回装配好的 FastAPI。
测试用 test settings 注入；生产用 Settings() 从 .env 读。

路由契约见模块 docstring（web/__init__.py）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

import anyio
import jwt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response

from agent_harness.agent import AgentEvent
from agent_harness.assembly import RecoveryStores, build_runtime, initialize_stores
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
from agent_harness.model.config import ConfigError, ModelConfig, parse_model_catalog
from agent_harness.observability import flush_process_sink
from agent_harness.recovery import RecoveryCoordinator, RecoveryError
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import JsonlSessionStore, Session, SessionEvent
from agent_harness.storage import (
    SqliteCheckpointStore,
    SqliteOperationLedger,
    SqliteSessionMetaStore,
)
from agent_harness.web.runmanager import RunManager

# ── Request / Response schemas ──


class CreateSessionRequest(BaseModel):
    """POST /api/sessions 的请求体。"""

    # 空 task 直接 422（FastAPI 自动校验）；纯空白 task 容忍（runtime 侧无意义但不危险）。
    # max_length 封顶：task 会逐字持久化进 JSONL（user/message）并整体进模型上下文，
    # 无上限时一个多 MB 请求体就能写爆日志 + 撑爆 context。
    task: str = Field(min_length=1, max_length=100_000)
    workspace: str | None = None  # None → 用默认 workspace；只接受单段目录名（见 _validate_workspace_name）
    max_steps: int = Field(default=10, ge=1, le=200)  # 非正数 / 过大 → 422（防客端刷爆循环预算）
    auto_approve: bool = True  # V1 默认自动批准（demo 同款）
    # 会话级模型选择（ADR-0016 §5，C6）：None = 默认链（现行为不变）；
    # 命名 = AGENT_MODELS catalog 条目，未知名字 422。fallback 链不受影响。
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


def _validate_session_id(session_id: str) -> str:
    """校验路径里的 session_id——它是单个名字段，不是路径。

    store.read_events 直接 ``self._root / session_id`` 拼路径：不校验时反斜杠段
    在 win32 上可越出 sessions 根目录（路径穿越读取 oracle），盘符段可整体替换
    基路径。与 _validate_workspace_name 同一安全边界；字符集与 S3ArtifactStore
    的 key 段规则一致（session_id 实际由 uuid4() 生成，天然满足）。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise HTTPException(
            status_code=422,
            detail=f"session_id 只接受单个安全名字段：{session_id!r}",
        )
    return session_id


#: 重放 backlog 阈值（durable 事件数，ADR-0016 §2.3）：after_seq 落后超过
#: 该值 → 单帧 stream/truncated 控制事件后收流，客户端走 GET /events 全量
#: 重建后带 after_seq=latest_seq 重连（02 §10.4 简化版；snapshot 层 DEFER）。
STREAM_REPLAY_MAX_EVENTS = 1000


def _event_to_sse_dict(event: AgentEvent, session_id: str) -> dict[str, str]:
    """把 AgentEvent 转成 SSE 的 data 字段（JSON 字符串）。

    session_id 由 endpoint 注入——runtime 内部的 AgentEvent 不知道自己属于哪个 session，
    但前端需要它在第一帧就能切换 selectedId（否则新 session 的对话无法渲染）。
    帧形状与重放路径（GET /stream 的 SessionEvent 帧）同形：seq 是幂等投影键，
    event_id 是事件身份，block_id 聚合同一段流式块（ADR-0016 §2.3）。
    """
    payload: dict[str, Any] = {
        "type": event.type,
        "data": event.data,
        "seq": event.seq,
        "run_id": event.run_id,
        "step_id": event.step_id,
        "session_id": session_id,
        "time": event.time,
    }
    if event.block_id is not None:
        payload["block_id"] = event.block_id
    return {"data": json.dumps(payload, ensure_ascii=False)}


def _session_event_to_sse_dict(event: SessionEvent, session_id: str) -> dict[str, str]:
    """把持久化 SessionEvent 转成 SSE 帧（重放通道，GET /stream 用）。

    帧形状与 live 通道（_event_to_sse_dict）严格同形——客户端对两条通道
    做同一 seq 幂等投影，无需区分帧来源（event_id 仅存于 JSONL/全量接口）。
    """
    payload: dict[str, Any] = {
        "type": event.type,
        "data": event.data,
        "seq": event.seq,
        "run_id": event.run_id,
        "step_id": event.step_id,
        "session_id": session_id,
        "time": event.time,
    }
    if event.block_id is not None:
        payload["block_id"] = event.block_id
    return {"data": json.dumps(payload, ensure_ascii=False)}


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

    register_lineage_routes(app, validate_session_id=_validate_session_id)

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
        store = app.state.agent.store
        ids = await anyio.to_thread.run_sync(store.list_session_ids)
        summaries: list[SessionSummary] = []
        for sid in ids:
            stats = await anyio.to_thread.run_sync(store.read_session_summary, sid)
            if stats is None or stats.event_count == 0:
                continue
            summaries.append(SessionSummary(
                session_id=sid,
                event_count=stats.event_count,
                first_event_time=stats.first_event_time,
                last_event_time=stats.last_event_time,
                first_user_message=stats.first_user_message,
            ))
        return summaries

    @app.get("/api/sessions/{session_id}/events")
    async def get_session_events(session_id: str) -> list[dict]:
        """读历史 SessionEvent——前端刷新后从此重建视图（不变量 #22）。

        session_id 先过安全校验（名字段，不是路径）；store 读是同步磁盘 I/O，
        走 to_thread 卸载（同 list_sessions）。
        """
        _validate_session_id(session_id)
        store = app.state.agent.store
        events = await anyio.to_thread.run_sync(store.read_events, session_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"session '{session_id}' not found")
        return [e.to_dict() for e in events]

    @app.get("/api/models")
    async def list_models() -> dict[str, Any]:
        """列出可选模型（ADR-0016 §5，C6）：默认链 + AGENT_MODELS catalog。

        绝不携带任何密钥字段；default=true 的条目 = 不传 model 参数时的链。
        思考能力不进元数据（D-B③ 事件驱动：模型真吐思考才有 reasoning 事件）。
        """
        state = app.state.agent
        default_config = ModelConfig.from_settings(state.settings)
        models: list[dict[str, Any]] = [{
            "name": default_config.model_name,
            "provider": state.settings.model_provider,
            "model": default_config.model_name,
            "default": True,
        }]
        for entry in parse_model_catalog(state.settings):
            models.append({
                "name": entry.name,
                "provider": entry.provider,
                "model": entry.model_name,
                "default": False,
            })
        return {"models": models}

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequest):
        """起新 session + 跑任务，流式返回 AgentEvent（SSE）。

        ADR-0016 §2.1（D-A）：run 由 RunManager 以 detached task 驱动，与
        本次 HTTP 请求生命周期解耦——断连（本 generator 被取消）只做
        unsubscribe，run 继续跑到终态；显式取消走 POST /cancel。
        """
        state = app.state.agent

        # 安全边界先行：workspace 名校验（422 拒绝）必须发生在任何 mkdir /
        # Session 落盘之前——被拒请求不能留下孤儿 session 或目录。
        workspace_name = _validate_workspace_name(state, req.workspace)

        # 组装顺序（R6-6）：先建 workspace + runtime，最后才 Session.start 落盘。
        # factory 需要 session_id 装配 S3 artifact 命名空间，因此预生成 id 传入
        # ——此前 Session.start 先落盘、runtime 组装失败时客户端拿 JSON 500
        # 且 store 里留下只含 session/started 的孤儿 session。
        session_id = str(uuid4())
        workspace = (state.workspaces_root / workspace_name if workspace_name is not None
                     else state.workspaces_root / session_id)
        workspace.mkdir(parents=True, exist_ok=True)

        if req.model is not None:
            try:
                ModelConfig.from_catalog(state.settings, req.model)
            except ConfigError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

        _, wiring = await state.get_wiring()
        await state.ensure_stores()
        runtime = await build_runtime(
            settings=state.settings, wiring=wiring, stores=state.stores,
            workspace_registry=state.workspace_registry,
            session_id=session_id, workspace=workspace,
            max_steps=req.max_steps, auto_approve=req.auto_approve,
            session_store=state.store,
            model_name=req.model,
        )
        session = Session.start(state.store, session_id=session_id)

        # launch 内无 await（create_task 只调度不执行）→ 订阅者挂载必然
        # 早于 run 的首批事件，不会丢帧。
        run, subscriber = state.run_manager.launch(session, runtime, req.task)

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
        _validate_session_id(session_id)
        state = app.state.agent
        events = await anyio.to_thread.run_sync(state.store.read_events, session_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"session '{session_id}' not found")
        latest_seq = events[-1].seq

        # 先订阅（注册进 fanout 集合）后取游标：订阅后到取游标之间的入队
        # 必然 ≤ 游标（被重放覆盖）或 > 游标（在队列里）——无缝无重复。
        run = state.run_manager.get_active(session_id)
        subscriber = run.subscribe() if run is not None else None
        replay_upto = run.last_enqueued_seq if run is not None else latest_seq

        if latest_seq - after_seq > STREAM_REPLAY_MAX_EVENTS:
            async def truncated_generator():
                control = {
                    "type": "stream/truncated",
                    "data": {"after_seq": after_seq, "latest_seq": latest_seq},
                    "seq": None, "run_id": None, "step_id": None,
                    "session_id": session_id,
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
        _validate_session_id(session_id)
        state = app.state.agent
        existing = await anyio.to_thread.run_sync(state.store.read_events, session_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"session '{session_id}' not found")
        cancelled = state.run_manager.cancel(session_id)
        return {"status": "cancelling" if cancelled else "no_active_run"}

    @app.post("/api/sessions/{session_id}/recover")
    async def recover_session(session_id: str) -> list[dict]:
        """恢复崩溃 session（R8-1 接线）：RecoveryCoordinator 唯一入口（07 §9）。

        修复 dangling tool_call（配对合成）、按 Ledger 终态精确回填结果、
        PENDING 默认 skip；RUNNING/UNKNOWN 需要人工裁决时返回 409（不伪造、
        不盲跑，不变量 #14）。幂等：重复调用靠事件配对自然跳过已修复项。
        """
        _validate_session_id(session_id)
        await state.ensure_stores()
        # 不存在的 session 显式 404（RecoveryError 统一留给"需要人工裁决"语义）。
        existing = await anyio.to_thread.run_sync(state.store.read_events, session_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"session '{session_id}' not found")
        coordinator = RecoveryCoordinator(
            session_store=state.store,
            workspace_registry=state.workspace_registry,
            operation_ledger=state.operation_ledger,
            database_path=state.harness_db,
        )
        try:
            recovered = await coordinator.recover(session_id)
        except RecoveryError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return [e.to_dict() for e in recovered.events]

    @app.post("/api/sessions/{session_id}/approve")
    async def approve(session_id: str, body: dict):
        """审批决策回传（V1 seam：runtime 用 auto-approve，此 endpoint 预留）。

        真正的交互式审批需要 pending approval queue + 通知机制（WebSocket seam）。
        V1 返回 202 表示「已接收但当前 runtime 用 auto-approve」。
        """
        return {
            "status": "received",
            "session_id": session_id,
            "note": "V1 uses auto-approve; interactive approval pending WebSocket seam",
        }

    # ── 静态资源（前端 build 产物）──
    # 生产模式：FastAPI serve web/dist；dev 模式 Vite 自己跑 5173。
    # 部署约束：静态挂载只适配本地信任模式（未配置 JWT_SECRET）。fail-closed
    # 生效时全量默认拒绝（test_auth_fail_closed 契约），而浏览器顶层导航无法
    # 携带 Bearer——index.html 都会 401。生产 + JWT 的支持形态是反向代理：
    # 静态资源在代理层直出，仅 /api 转发到本服务（前端带 Bearer 调用）。
    web_dist = Path(__file__).resolve().parent.parent.parent.parent / "web" / "dist"
    if web_dist.exists():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="static")

    return app
