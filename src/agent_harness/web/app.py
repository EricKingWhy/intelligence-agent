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
from starlette.responses import JSONResponse

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import CapabilityWiring, wire_capabilities
from agent_harness.config import Settings
from agent_harness.context.builder import ContextBuilder
from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.logging import setup_logging
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model
from agent_harness.recovery import RecoveryCoordinator, RecoveryError
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import USER_MESSAGE
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
                await self.operation_ledger.initialize()
                await self.checkpoint_store.initialize()
                await self.session_meta_store.initialize()
                self._stores_ready = True

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
        """进程退出时关闭后台 relay 与外部连接。幂等：重复调用只关闭一次。

        必须拿 _wiring_lock：否则在途 get_wiring 可能在 swap 之后才完成装配，
        装配出的 wiring 永远没人关（泄露 Milvus / embedding 连接）。
        先置位 _closed 再拿锁——让在途装配在 wire 完成后立刻失败，而不是
        把 wiring 交给一个已关停的 app。
        """
        self._closed = True
        async with self._wiring_lock:
            wiring, self._wiring, self._registry = self._wiring, None, None
        if wiring is not None and wiring.memory is not None:
            await wiring.memory.close()


async def _build_runtime(
    state: AppState,
    workspace: Path,
    max_steps: int,
    auto_approve: bool,
    *,
    session_id: str,
) -> AgentRuntime:
    """装配工具与 Context；配置对象存储时按 Session 绑定 Artifact Provider。

    Memory 子系统按配置惰性装配：齐了注入 capability + writeback + context provider，
    不齐保持纯 Runtime（Memory 能力缺位，其他功能不受影响）。
    """
    config = ModelConfig.from_settings(state.settings)
    model = create_chat_model(config)

    # Sandbox 由 WorkspaceRegistry 统一创建并持久化映射（R8-1）：恢复时
    # RecoveryCoordinator 据映射还原 sandbox；workspace_root 允许命名 workspace。
    sandbox = state.workspace_registry.create(session_id, workspace_root=workspace)
    registry = ToolRegistry()
    for tool_cls in (
        ReadTool, WriteTool, BashTool, EditTool, ApplyPatchTool,
        GlobTool, GrepTool, GitStatusTool, GitDiffTool,
    ):
        registry.register(tool_cls(sandbox))

    settings = state.settings
    overflow_handler = None
    if any((settings.artifact_store_endpoint, settings.artifact_store_bucket,
            settings.artifact_store_access_key, settings.artifact_store_secret_key,
            settings.artifact_store_region)):
        artifact_store = S3ArtifactStore(settings, session_id=session_id)
        registry.register(InspectArtifactTool(artifact_store))
        overflow_handler = ArtifactOverflowHandler(artifact_store, settings.artifact_overflow_chars)

    if auto_approve:
        policy = PermissionPolicy.WORKSPACE_WRITE
        approval_callback: Any = lambda _req: ApprovalResponse(approved=True, reason="auto-approve")
    else:
        # manual 模式 V1：拒绝所有危险操作（不阻塞 demo 主流程）。
        # 真正的交互式审批留到 WebSocket / pending queue（接缝点）。
        policy = PermissionPolicy.WORKSPACE_WRITE
        approval_callback = lambda _req: ApprovalResponse(approved=False, reason="manual approval not yet wired")

    # Capability 装配（进程级缓存）：工具贡献进 ToolRegistry（统一 Executor 路径），
    # context providers / memory_writer 注入 AgentRuntime——Agent Loop 零改动。
    _, wiring = await state.get_wiring()
    for capability_tool in wiring.tools:
        registry.register(capability_tool)

    # 恢复基础设施接线（R8-1，用户拍板）：Ledger 记录副作用状态（不变量 #13）、
    # Checkpoint 在稳定边界落盘（不变量 #12）、SessionMeta 惰性登记。
    await state.ensure_stores()
    return AgentRuntime(
        model=model,
        registry=registry,
        executor=ToolExecutor(registry, policy=policy, approval_callback=approval_callback,
                              overflow_handler=overflow_handler,
                              operation_ledger=state.operation_ledger),
        max_steps=max_steps,
        checkpoint_policy=OnStableBoundary(state.checkpoint_store),
        session_meta_store=state.session_meta_store,
        context_builder=ContextBuilder(
            model, max_context_tokens=settings.max_context_tokens,
            auto_compact_threshold=settings.auto_compact_threshold,
            hard_guard_threshold=settings.hard_guard_threshold,
            context_providers=list(wiring.context_providers),
        ),
        memory_writer=wiring.memory_writer,
    )


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


def _event_to_sse_dict(event: AgentEvent, session_id: str) -> dict[str, str]:
    """把 AgentEvent 转成 SSE 的 data 字段（JSON 字符串）。

    session_id 由 endpoint 注入——runtime 内部的 AgentEvent 不知道自己属于哪个 session，
    但前端需要它在第一帧就能切换 selectedId（否则新 session 的对话无法渲染）。
    """
    payload = {
        "type": event.type,
        "data": event.data,
        "seq": event.seq,
        "run_id": event.run_id,
        "step_id": event.step_id,
        "session_id": session_id,
        "time": event.time,
    }
    return {"data": json.dumps(payload, ensure_ascii=False)}


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

    app = FastAPI(title="Agent Harness Inspector", version="0.1.0", lifespan=lifespan)
    app.state.agent = state  # 挂在 app.state 上，路由通过 request.app.state 取

    if enable_cors:
        # V1 本地单用户：宽松 CORS 让 Vite dev server (5173) 能直连。
        # 多用户时收紧到已知 origin（接缝点）。
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    if not settings.jwt_secret:
        # R6-4：未配置密钥 = 本地信任模式（fail-open）。保留开发便利，但必须
        # 响亮告知——静默降级是原审计的核心危害。
        logging.getLogger("agent_harness.web").warning(
            "JWT_SECRET 未配置：API 以本地信任模式运行（所有请求视为 local 身份，"
            "不做身份校验）。生产部署必须配置 JWT_SECRET。"
        )

    @app.middleware("http")
    async def auth_seam(request: Any, call_next: Any):
        identity = IdentityContext("local", "local", ["user", "session"])
        authorization = request.headers.get("authorization")
        if settings.jwt_secret:
            # R6-4/R8-3（用户拍板 fail-closed）：配置了密钥 = 需要认证。
            # 匿名请求不再静默降级为 trusted local（此前配合 CORS * 等于把
            # agent API 开放给任意网页）；无 exp 的 token 一并拒绝（强制
            # 过期语义，永不过期的签名 token 等于永久凭证）。
            if not authorization:
                return JSONResponse({"detail": "Missing identity token"}, status_code=401)
            try:
                scheme, encoded = authorization.split(" ", 1)
                if scheme.lower() != "bearer":
                    raise ValueError("Expected Bearer token")
                # SecretStr 取明文给 jwt.decode；truthiness 判断仍基于密钥值
                # （SecretStr("") 为 falsy，未配置语义不变）。
                claims = jwt.decode(encoded, settings.jwt_secret.get_secret_value(),
                                    algorithms=["HS256"],
                                    options={"require": ["tenant_id", "user_id", "exp"]})
                tenant, user = claims["tenant_id"], claims["user_id"]
                scopes = claims.get("scopes", ["user", "session"])
                if (not isinstance(tenant, str) or not tenant.strip()
                        or not isinstance(user, str) or not user.strip()
                        or not isinstance(scopes, list)
                        or any(not isinstance(scope, str) for scope in scopes)):
                    raise ValueError("Invalid identity claims")
                identity = IdentityContext(tenant, user, scopes)
            except (jwt.InvalidTokenError, ValueError):
                return JSONResponse({"detail": "Invalid identity token"}, status_code=401)
        token = set_identity_context(identity)
        try:
            return await call_next(request)
        finally:
            identity_context_var.reset(token)

    # ── 路由 ──

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/sessions")
    async def list_sessions() -> list[SessionSummary]:
        """列历史 session（按最近活动倒序）。

        store 读是同步磁盘 I/O，走 to_thread 卸载——一份大 JSONL 不能卡住事件循环。
        """
        store = app.state.agent.store
        ids = await anyio.to_thread.run_sync(store.list_session_ids)
        summaries: list[SessionSummary] = []
        for sid in ids:
            events = await anyio.to_thread.run_sync(store.read_events, sid)
            if not events:
                continue
            first_user_message = next(
                (e.data.get("content") for e in events if e.type == USER_MESSAGE
                 and isinstance(e.data.get("content"), str) and e.data["content"].strip()),
                None,
            )
            if first_user_message is not None:
                first_user_message = first_user_message.strip()[:128]
            summaries.append(SessionSummary(
                session_id=sid,
                event_count=len(events),
                first_event_time=events[0].time,
                last_event_time=events[-1].time,
                first_user_message=first_user_message,
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

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequest):
        """起新 session + 跑任务，流式返回 AgentEvent（SSE）。

        这是 Phase 9 的核心 endpoint：前端 POST 任务，后端流式回每个事件。
        """
        state = app.state.agent

        # 安全边界先行：workspace 名校验（422 拒绝）必须发生在任何 mkdir /
        # Session 落盘之前——被拒请求不能留下孤儿 session 或目录。
        workspace_name = _validate_workspace_name(state, req.workspace)

        # 组装顺序（R6-6）：先建 workspace + runtime，最后才 Session.start 落盘。
        # _build_runtime 需要 session_id 装配 S3 artifact 命名空间，因此预生成
        # id 传入——此前 Session.start 先落盘、runtime 组装失败时客户端拿
        # JSON 500 且 store 里留下只含 session/started 的孤儿 session。
        session_id = str(uuid4())
        workspace = (state.workspaces_root / workspace_name if workspace_name is not None
                     else state.workspaces_root / session_id)
        workspace.mkdir(parents=True, exist_ok=True)

        runtime = await _build_runtime(state, workspace, req.max_steps, req.auto_approve,
                                       session_id=session_id)
        session = Session.start(state.store, session_id=session_id)

        async def event_generator():
            """SSE 事件源：消费 run_stream，转成 SSE 帧。

            断连时 EventSourceResponse 自动取消这个 generator——不泄漏 producer。
            memory_session_var 在这一帧绑定：SESSION-scope 记忆操作需要可信 session id，
            不能让客户端任意指定（与 IdentityContext 同一信任边界）。
            """
            from agent_harness.memory.types import memory_session_var
            session_token = memory_session_var.set(session.session_id)
            try:
                async for event in runtime.run_stream(session, req.task):
                    yield _event_to_sse_dict(event, session.session_id)
            finally:
                memory_session_var.reset(session_token)

        return EventSourceResponse(event_generator())

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
    web_dist = Path(__file__).resolve().parent.parent.parent.parent / "web" / "dist"
    if web_dist.exists():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="static")

    return app
