"""FastAPI 应用工厂 + 路由（Phase 9/10 精简版）。

create_app() 是单一入口——传入 Settings，返回装配好的 FastAPI。
测试用 test settings 注入；生产用 Settings() 从 .env 读。

路由契约见模块 docstring（web/__init__.py）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import jwt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
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
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model
from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import USER_MESSAGE
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

    task: str
    workspace: str | None = None  # None → 用默认 workspace
    max_steps: int = 10
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
        # Capability 装配：只在首次使用时执行（含 Memory / Skills / demo 等）。
        # asyncio.Lock 守住 once 语义：并发首请求都看到 _wiring is None 时，
        # 只有一个真正执行 wire_capabilities，另一个等锁后复用结果
        # （否则第二个会在重复注册上炸掉 / 被降级吞掉）。
        self._wiring_lock = asyncio.Lock()
        self._registry: CapabilityRegistry | None = None
        self._wiring: CapabilityWiring | None = None

    async def get_wiring(self) -> tuple[CapabilityRegistry, CapabilityWiring]:
        """惰性装配 Capability 子系统并缓存；返回 (registry, wiring)。"""
        if self._wiring is not None and self._registry is not None:
            return self._registry, self._wiring
        async with self._wiring_lock:
            if self._wiring is None or self._registry is None:
                config = parse_capabilities_config(self.settings.capabilities)
                registry = CapabilityRegistry()
                wiring = await wire_capabilities(registry, config, settings=self.settings)
                self._registry, self._wiring = registry, wiring
        return self._registry, self._wiring

    @property
    def wiring(self) -> CapabilityWiring | None:
        """已装配的 wiring（未装配时 None；测试与 shutdown 用）。"""
        return self._wiring

    async def shutdown(self) -> None:
        """进程退出时关闭后台 relay 与外部连接。调用方保证只执行一次。"""
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

    sandbox = LocalSubprocessSandbox(workspace_root=workspace)
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

    return AgentRuntime(
        model=model,
        registry=registry,
        executor=ToolExecutor(registry, policy=policy, approval_callback=approval_callback,
                              overflow_handler=overflow_handler),
        max_steps=max_steps,
        context_builder=ContextBuilder(
            model, max_context_tokens=settings.max_context_tokens,
            auto_compact_threshold=settings.auto_compact_threshold,
            hard_guard_threshold=settings.hard_guard_threshold,
            context_providers=list(wiring.context_providers),
        ),
        memory_writer=wiring.memory_writer,
    )


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

    @app.middleware("http")
    async def auth_seam(request: Any, call_next: Any):
        identity = IdentityContext("local", "local", ["user", "session"])
        authorization = request.headers.get("authorization")
        if settings.jwt_secret and authorization:
            try:
                scheme, encoded = authorization.split(" ", 1)
                if scheme.lower() != "bearer":
                    raise ValueError("Expected Bearer token")
                claims = jwt.decode(encoded, settings.jwt_secret, algorithms=["HS256"],
                                    options={"require": ["tenant_id", "user_id"]})
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
        """列历史 session（按最近活动倒序）。"""
        store = app.state.agent.store
        ids = store.list_session_ids()
        summaries: list[SessionSummary] = []
        for sid in ids:
            events = store.read_events(sid)
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
        """读历史 SessionEvent——前端刷新后从此重建视图（不变量 #22）。"""
        store = app.state.agent.store
        events = store.read_events(session_id)
        if not events:
            raise HTTPException(status_code=404, detail=f"session '{session_id}' not found")
        return [e.to_dict() for e in events]

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequest):
        """起新 session + 跑任务，流式返回 AgentEvent（SSE）。

        这是 Phase 9 的核心 endpoint：前端 POST 任务，后端流式回每个事件。
        """
        state = app.state.agent

        # 为这次 session 准备 workspace（每个 session 独立目录）
        session = Session.start(state.store)
        workspace = Path(req.workspace) if req.workspace else state.workspaces_root / session.session_id
        workspace.mkdir(parents=True, exist_ok=True)

        runtime = await _build_runtime(state, workspace, req.max_steps, req.auto_approve,
                                       session_id=session.session_id)

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
