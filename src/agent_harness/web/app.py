"""FastAPI 应用工厂 + 路由（Phase 9/10 精简版）。

create_app() 是单一入口——传入 Settings，返回装配好的 FastAPI。
测试用 test settings 注入；生产用 Settings() 从 .env 读。

路由契约见模块 docstring（web/__init__.py）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.config import Settings
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model
from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tooling.approval import ApprovalRequest, ApprovalResponse
from agent_harness.tooling.contract import PermissionPolicy
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


class AppState:
    """app 内部共享状态的薄容器——避免全局变量。

    V1：单进程内存里的 runtime 工厂 + session store 根目录。
    后续多用户时在这里挂 auth context（接缝点）。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # sessions 和 workspace 默认放在 .agent/ 下（跟现有诊断日志一致）
        self.sessions_root = Path(settings.workspace_dir) / "sessions"
        self.workspaces_root = Path(settings.workspace_dir) / "workspaces"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.store = JsonlSessionStore(root=self.sessions_root)


def _build_runtime(
    state: AppState,
    workspace: Path,
    max_steps: int,
    auto_approve: bool,
) -> AgentRuntime:
    """复用 demo/live_agent.py 的 runtime 配方，绑定 9 个 Coding Tools。"""
    config = ModelConfig.from_settings(state.settings)
    model = create_chat_model(config)

    sandbox = LocalSubprocessSandbox(workspace_root=workspace)
    registry = ToolRegistry()
    for tool_cls in (
        ReadTool, WriteTool, BashTool, EditTool, ApplyPatchTool,
        GlobTool, GrepTool, GitStatusTool, GitDiffTool,
    ):
        registry.register(tool_cls(sandbox))

    if auto_approve:
        policy = PermissionPolicy.WORKSPACE_WRITE
        approval_callback: Any = lambda _req: ApprovalResponse(approved=True, reason="auto-approve")
    else:
        # manual 模式 V1：拒绝所有危险操作（不阻塞 demo 主流程）。
        # 真正的交互式审批留到 WebSocket / pending queue（接缝点）。
        policy = PermissionPolicy.WORKSPACE_WRITE
        approval_callback = lambda _req: ApprovalResponse(approved=False, reason="manual approval not yet wired")

    return AgentRuntime(
        model=model,
        registry=registry,
        executor=ToolExecutor(registry, policy=policy, approval_callback=approval_callback),
        max_steps=max_steps,
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
    }
    return {"data": json.dumps(payload, ensure_ascii=False)}


def create_app(settings: Settings | None = None, *, enable_cors: bool = True) -> FastAPI:
    """装配 FastAPI 应用。测试可注入 test settings；生产默认从 .env 读。"""
    if settings is None:
        settings = Settings()

    app = FastAPI(title="Agent Harness Inspector", version="0.1.0")
    state = AppState(settings)
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

    # ── 多用户 auth 中间件空壳（Q2=A 接缝点）──
    # V1 no-op；后续接 auth provider 时在这里注入 user context 到 request.state。
    @app.middleware("http")
    async def auth_seam(request: Any, call_next: Any):
        # request.state.user_id = "local-user"  # 占位，多用户时启用
        return await call_next(request)

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
            summaries.append(SessionSummary(
                session_id=sid,
                event_count=len(events),
                first_event_time=events[0].time,
                last_event_time=events[-1].time,
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

        runtime = _build_runtime(state, workspace, req.max_steps, req.auto_approve)

        async def event_generator():
            """SSE 事件源：消费 run_stream，转成 SSE 帧。

            断连时 EventSourceResponse 自动取消这个 generator——不泄漏 producer。
            """
            async for event in runtime.run_stream(session, req.task):
                yield _event_to_sse_dict(event, session.session_id)

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
