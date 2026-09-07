"""SessionService — CLI/Web 共享的会话领域服务（T1 / #131）。

从 Web FastAPI handlers 抽出的 session 生命周期逻辑，统一供 CLI 与 Web 调用。
HTTP 是传输层，不进本模块；领域异常由调用方翻译为 HTTP/CLI 响应。

设计原则（PRD §4）：
- Web FastAPI handler 只做参数校验 + 调用 SessionService + 响应封装。
- CLI 直接调用 SessionService（T3 重构后）。
- 所有真相来自 append-only JSONL + Session 单一事实源（不变量 #22）。
- Tool 只有一条执行路径（不变量 #7）：审批回传统一汇聚到 ToolExecutor callback。

本模块是 T1 纯重构产物——无行为变化，现有测试保持 green。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

import anyio

from agent_harness.assembly import build_runtime
from agent_harness.session.event import TOOL_APPROVAL_REQUESTED
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling.approval import (
    ApprovalCallback,
    ApprovalRequest,
    ApprovalResponse,
    PermissionDecision,
)
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.contract import PermissionPolicy

if TYPE_CHECKING:
    from agent_harness.assembly import RecoveryStores
    from agent_harness.web.app import AppState
    from agent_harness.web.runmanager import ManagedRun, RunManager, Subscriber


# ── 领域异常 ──────────────────────────────────────────────────────────
# 调用方（Web handler / CLI）负责翻译为 HTTP status / CLI 错误消息。


class SessionServiceError(Exception):
    """SessionService 所有领域异常的基类。"""


class SessionNotFound(SessionServiceError):
    """session_id 不存在（store 中无事件）。"""


class InvalidSessionId(SessionServiceError):
    """session_id 格式不合法（安全校验失败）。"""


class ActiveRunConflict(SessionServiceError):
    """session 已有在途 run，不允许并发。"""


class ApprovalQueueMissing(SessionServiceError):
    """session 没有交互式审批队列（permission_mode 非交互，或 run 已结束）。"""


class ApprovalRequestMissing(SessionServiceError):
    """approval_id 在 session 事件中找不到对应的 approval-requested。"""


class ApprovalAlreadyResolved(SessionServiceError):
    """approval_id 已被决策（防重复）。"""


class InvalidDecision(SessionServiceError):
    """决策值不合法或不在 allowed_decisions 内。"""


class RecoveryConflict(SessionServiceError):
    """恢复需要人工裁决（UNKNOWN 工具状态）。"""


class WorkspaceNameInvalid(SessionServiceError):
    """workspace 名字不合法（路径逃逸风险）。"""


# ── 数据载体 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaunchResult:
    """create_and_launch / resume_and_launch 的返回束。

    Web 层用 run + subscriber 组装 SSE；CLI 层可直接 await run.task
    或忽略 run（自有驱动路径）。
    """

    session: Session
    run: ManagedRun
    subscriber: Subscriber


@dataclass(frozen=True)
class StreamReconnectHandle:
    """stream_reconnect 的返回束：重放事件 + 在途订阅句柄。"""

    events: list  # list[SessionEvent]
    run: ManagedRun | None
    subscriber: Subscriber | None
    replay_upto: int
    latest_seq: int


@dataclass(frozen=True)
class ApprovalDecision:
    """审批决策解析结果。"""

    decision: PermissionDecision
    response: ApprovalResponse


# ── SessionService ───────────────────────────────────────────────────


class SessionService:
    """会话领域服务：统一 CLI / Web 的 session 生命周期操作。

    包装 AppState（store / run_manager / approval_queues / capability 装配），
    对外暴露纯领域方法，不感知 HTTP / SSE / CLI 终端。
    """

    def __init__(self, state: AppState) -> None:
        self._state = state

    # ── 属性透传（调用方可直接用 service.store 等）────────────────────

    @property
    def store(self) -> JsonlSessionStore:
        return self._state.store

    @property
    def run_manager(self) -> RunManager:
        return self._state.run_manager

    @property
    def approval_queues(self) -> dict[str, PendingApprovalQueue]:
        return self._state.approval_queues

    @property
    def workspaces_root(self) -> Path:
        return self._state.workspaces_root

    @property
    def settings(self):
        return self._state.settings

    @property
    def workspace_registry(self):
        return self._state.workspace_registry

    # ── 只读操作 ─────────────────────────────────────────────────────

    async def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有 session 摘要（按最近活动倒序）。

        返回 list of dict（与 SessionSummary 字段一致），
        由调用方映射为 API response model。
        """
        store = self._state.store
        ids = await anyio.to_thread.run_sync(store.list_session_ids)
        summaries: list[dict[str, Any]] = []
        for sid in ids:
            stats = await anyio.to_thread.run_sync(store.read_session_summary, sid)
            if stats is None or stats.event_count == 0:
                continue
            summaries.append(
                {
                    "session_id": sid,
                    "event_count": stats.event_count,
                    "first_event_time": stats.first_event_time,
                    "last_event_time": stats.last_event_time,
                    "first_user_message": stats.first_user_message,
                }
            )
        return summaries

    async def get_events(self, session_id: str) -> list:
        """读取 session 的完整事件历史（只读，不 mutate）。

        用于前端刷新后重建视图（不变量 #22）；replay 等零副作用场景也用它。
        不调用 Session.resume（那会追加 session/resumed）。
        """
        self._validate_session_id(session_id)
        events = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        if not events:
            raise SessionNotFound(f"session '{session_id}' not found")
        return events

    async def has_session(self, session_id: str) -> bool:
        """检查 session 是否存在（用于 cancel/approve 等 404 前置校验）。"""
        self._validate_session_id(session_id)
        events = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        return bool(events)

    # ── 启动 + 运行 ──────────────────────────────────────────────────

    async def create_and_launch(
        self,
        *,
        task: str,
        workspace_name: str | None = None,
        max_steps: int = 10,
        permission_mode: PermissionPolicy = PermissionPolicy.WORKSPACE_WRITE,
        permission_mode_explicit: bool = False,
        auto_approve_explicit: bool = False,
        auto_approve: bool = True,
        model: str | None = None,
        reasoning_effort: str | None = None,
        agent_profile: str | None = None,
        context_providers: list[str] | None = None,
    ) -> LaunchResult:
        """创建新 Session 并启动 run（原 POST /api/sessions 的领域逻辑）。

        返回 LaunchResult(session, run, subscriber)。调用方：
        - Web：消费 subscriber.queue 组装 SSE 流。
        - CLI：可 await run.task 或忽略（自有驱动路径）。

        组装顺序（R6-6）：先建 workspace + runtime，最后才 Session.start 落盘，
        避免 runtime 组装失败时留下只含 session/started 的孤儿 session。
        """
        from uuid import uuid4

        from agent_harness.model.config import ConfigError, ModelConfig

        workspace_name = self._validate_workspace_name(workspace_name)

        session_id = str(uuid4())
        workspace = (
            self._state.workspaces_root / workspace_name
            if workspace_name is not None
            else self._state.workspaces_root / session_id
        )
        workspace.mkdir(parents=True, exist_ok=True)

        # 模型 catalog 校验（在落盘前，避免孤儿）
        if model is not None:
            try:
                ModelConfig.from_catalog(self._state.settings, model)
            except ConfigError as error:
                raise InvalidDecision(str(error)) from error

        _, wiring = await self._state.get_wiring()
        await self._state.ensure_stores()

        # 审批路由（三种，保留向后兼容）
        interactive = (
            permission_mode_explicit
            and permission_mode != PermissionPolicy.DANGER_FULL_ACCESS
        )

        approval_callback = await self._build_approval_callback(
            interactive=interactive,
            auto_approve_explicit=auto_approve_explicit,
            permission_mode_explicit=permission_mode_explicit,
            auto_approve=auto_approve,
            session_id=session_id,
        )

        runtime = await build_runtime(
            settings=self._state.settings,
            wiring=wiring,
            stores=self._state.stores,
            workspace_registry=self._state.workspace_registry,
            session_id=session_id,
            workspace=workspace,
            max_steps=max_steps,
            permission_mode=permission_mode,
            approval_callback=approval_callback,
            session_store=self._state.store,
            model_name=model,
            reasoning_effort=reasoning_effort,
            agent_profile=agent_profile,
            context_providers=context_providers,
        )
        session = Session.start(self._state.store, session_id=session_id)

        # 交互式审批：把真实 session 注入 callback 闭包
        if interactive and isinstance(approval_callback, _InteractiveCallbackHolder):
            approval_callback.bind_session(session)

        run, subscriber = self._state.run_manager.launch(session, runtime, task)

        # run 终结时 GC approval_queue（防泄漏）
        if interactive:
            self._attach_approval_queue_gc(run, session_id)

        return LaunchResult(session=session, run=run, subscriber=subscriber)

    async def resume_and_launch(
        self,
        *,
        session_id: str,
        task: str,
        max_steps: int = 10,
    ) -> LaunchResult:
        """恢复已有 Session 并追加一轮新 user input（原 POST /resume）。

        这是「续跑」而非精确恢复中断 run：Session.resume 重建 append-only
        历史与 dangling 修复，RunManager.launch 驱动一轮新 Agent Loop。
        在途 session 拒绝（ActiveRunConflict），避免并发。
        """
        self._validate_session_id(session_id)
        existing = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        if not existing:
            raise SessionNotFound(f"session '{session_id}' not found")
        if self._state.run_manager.get_active(session_id) is not None:
            raise ActiveRunConflict("session has an active run")

        try:
            session = Session.resume(
                self._state.store,
                session_id,
                workspace_registry=self._state.workspace_registry,
            )
        except ValueError as error:
            raise SessionNotFound(str(error)) from error

        workspace = self._state.workspaces_root / session_id
        workspace.mkdir(parents=True, exist_ok=True)
        _, wiring = await self._state.get_wiring()
        await self._state.ensure_stores()
        runtime = await build_runtime(
            settings=self._state.settings,
            wiring=wiring,
            stores=self._state.stores,
            workspace_registry=self._state.workspace_registry,
            session_id=session_id,
            workspace=workspace,
            max_steps=max_steps,
            permission_mode=PermissionPolicy.WORKSPACE_WRITE,
            approval_callback=None,
            session_store=self._state.store,
            model_name=None,
        )
        run, subscriber = self._state.run_manager.launch(session, runtime, task)
        return LaunchResult(session=session, run=run, subscriber=subscriber)

    # ── 重连续传 ─────────────────────────────────────────────────────

    async def stream_reconnect(
        self, *, session_id: str, after_seq: int = -1, max_replay_events: int = 1000
    ) -> StreamReconnectHandle:
        """重连续传（原 GET /stream）：重放 durable 事件 + 接上在途流。

        返回 StreamReconnectHandle，调用方（Web 层）用它组装 SSE 帧。
        不启动新 run，不 mutate session。
        """
        self._validate_session_id(session_id)
        events = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        if not events:
            raise SessionNotFound(f"session '{session_id}' not found")
        latest_seq = events[-1].seq

        # 先订阅后取游标（无缝无重复）
        run = self._state.run_manager.get_active(session_id)
        subscriber = run.subscribe() if run is not None else None
        replay_upto = run.last_enqueued_seq if run is not None else latest_seq

        return StreamReconnectHandle(
            events=events,
            run=run,
            subscriber=subscriber,
            replay_upto=replay_upto,
            latest_seq=latest_seq,
        )

    # ── 取消 ─────────────────────────────────────────────────────────

    async def cancel(self, session_id: str) -> bool:
        """取消在途 run。返回 True 表示已取消，False 表示无在途 run（幂等）。

        session 不存在 → SessionNotFound。
        """
        if not await self.has_session(session_id):
            raise SessionNotFound(f"session '{session_id}' not found")
        return self._state.run_manager.cancel(session_id)

    # ── 审批 ─────────────────────────────────────────────────────────

    async def resolve_approval(
        self,
        *,
        session_id: str,
        approval_id: str,
        approved: bool = True,
        decision: str | None = None,
        reason: str = "",
    ) -> ApprovalDecision:
        """解析审批决策并唤醒 run 内阻塞的 callback（原 POST /approve）。

        返回 ApprovalDecision(decision, response)。
        异常：SessionNotFound / ApprovalQueueMissing / ApprovalRequestMissing /
              InvalidDecision / ApprovalAlreadyResolved。
        """
        self._validate_session_id(session_id)
        existing = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        if not existing:
            raise SessionNotFound(f"session '{session_id}' not found")

        queue = self._state.approval_queues.get(session_id)
        if queue is None:
            raise ApprovalQueueMissing(
                f"session '{session_id}' has no interactive approval queue "
                "(permission_mode not interactive, or run already terminated)"
            )

        requested = next(
            (
                e
                for e in existing
                if e.type == TOOL_APPROVAL_REQUESTED
                and e.data.get("approval_id") == approval_id
            ),
            None,
        )
        if requested is None:
            raise ApprovalRequestMissing(
                f"approval_id '{approval_id}' not found in session '{session_id}'"
            )

        allowed = requested.data.get("allowed_decisions", [])
        if decision is not None:
            try:
                perm_decision = PermissionDecision(decision)
            except ValueError:
                raise InvalidDecision(
                    f"decision '{decision}' is not a valid PermissionDecision"
                ) from None
        else:
            perm_decision = (
                PermissionDecision.APPROVE_ONCE if approved else PermissionDecision.DENY
            )

        if allowed and perm_decision.value not in allowed:
            raise InvalidDecision(
                f"decision '{perm_decision.value}' not in allowed_decisions {allowed}"
            )

        response = ApprovalResponse(
            approved=(perm_decision != PermissionDecision.DENY),
            reason=reason,
            decision=perm_decision,
        )
        try:
            ok = queue.resolve(approval_id, response)
        except KeyError:
            raise ApprovalAlreadyResolved(
                f"approval_id '{approval_id}' already resolved"
            ) from None
        if not ok:
            raise ApprovalRequestMissing(
                f"approval_id '{approval_id}' not found in queue"
            )
        return ApprovalDecision(decision=perm_decision, response=response)

    # ── 恢复 ─────────────────────────────────────────────────────────

    async def recover(self, session_id: str) -> list:
        """崩溃恢复（原 POST /recover）：RecoveryCoordinator 唯一入口。

        修复 dangling tool_call、按 Ledger 终态回填结果；
        RUNNING/UNKNOWN 需要人工裁决 → RecoveryConflict（不变量 #14）。
        """
        from agent_harness.recovery.coordinator import (
            RecoveryCoordinator,
            RecoveryError,
        )

        self._validate_session_id(session_id)
        await self._state.ensure_stores()
        existing = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        if not existing:
            raise SessionNotFound(f"session '{session_id}' not found")

        coordinator = RecoveryCoordinator(
            session_store=self._state.store,
            workspace_registry=self._state.workspace_registry,
            operation_ledger=self._state.operation_ledger,
            database_path=self._state.harness_db,
        )
        try:
            recovered = await coordinator.recover(session_id)
        except RecoveryError as error:
            raise RecoveryConflict(str(error)) from error
        return recovered.events

    # ── 内部方法 ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        """session_id 安全校验：名字段，不是路径（路径逃逸防护）。

        store.read_events 直接 ``self._root / session_id`` 拼路径：不校验时
        反斜杠段在 win32 上可越出 sessions 根目录，盘符段可整体替换基路径。
        字符集与 S3ArtifactStore 的 key 段规则一致。
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise InvalidSessionId(
                f"session_id 只接受单个安全名字段：{session_id!r}"
            )

    def _validate_workspace_name(self, workspace: str | None) -> str | None:
        """workspace 名字校验（路径逃逸防护）。

        None → 返回 None（调用方用默认 session_id 目录，向后兼容）；
        单段相对名 → 返回该名字；绝对路径 / 盘符 / 含 / 或 \\ 的多段名 / "." ".." → 拒绝。
        必须在任何 mkdir / Session 落盘之前调用。
        """
        if workspace is None:
            return None
        candidate = PureWindowsPath(workspace)
        if (workspace.strip() in ("", ".", "..")
                or candidate.drive or candidate.root or candidate.is_absolute()
                or "/" in workspace or "\\" in workspace):
            raise WorkspaceNameInvalid(
                f"workspace 只接受单个目录名（不接受路径）：{workspace!r}"
            )
        resolved_root = self._state.workspaces_root.resolve()
        if not (resolved_root / workspace).resolve().is_relative_to(resolved_root):
            raise WorkspaceNameInvalid(
                f"workspace 越出 workspaces_root：{workspace!r}"
            )
        return workspace

    async def _build_approval_callback(
        self,
        *,
        interactive: bool,
        auto_approve_explicit: bool,
        permission_mode_explicit: bool,
        auto_approve: bool,
        session_id: str,
    ) -> ApprovalCallback | None | _InteractiveCallbackHolder:
        """构建审批 callback（三种路由，与原 handler 行为完全一致）。"""
        if interactive:
            queue = PendingApprovalQueue()
            self._state.approval_queues[session_id] = queue
            return _InteractiveCallbackHolder(queue=queue)
        elif (
            auto_approve_explicit
            and not permission_mode_explicit
            and auto_approve is False
        ):
            async def _deny_callback(_req):
                return ApprovalResponse(
                    approved=False, reason="manual approval not yet wired"
                )

            return _deny_callback
        else:
            return None

    def _attach_approval_queue_gc(self, run: ManagedRun, session_id: str) -> None:
        """run 终结时 GC approval_queue（防长期泄漏）。"""
        _task = run.task

        def _gc_approval_queue(_t):
            self._state.approval_queues.pop(session_id, None)

        if _task is not None:
            _task.add_done_callback(_gc_approval_queue)


class _InteractiveCallbackHolder:
    """交互式审批 callback 的延迟绑定容器。

    session 在 callback 创建时尚未存在（R6-6 组装顺序：先 runtime 后 Session.start），
    因此用 holder 延迟注入 session，再返回真正的 async callback。

    bind_session 后才可被当作 ApprovalCallback 使用——调用方需通过
    as_callback() 获取真正的 callable。
    """

    def __init__(self, queue: PendingApprovalQueue) -> None:
        self._queue = queue
        self._session: Session | None = None

    def bind_session(self, session: Session) -> None:
        self._session = session

    async def __call__(self, req: ApprovalRequest) -> ApprovalResponse:
        if self._session is None:
            raise RuntimeError(
                "interactive callback invoked before Session.start"
            )
        approval_id = self._queue.register(req)
        allowed_decisions = [
            PermissionDecision.DENY.value,
            PermissionDecision.APPROVE_ONCE.value,
        ]
        self._session.append(
            TOOL_APPROVAL_REQUESTED,
            {
                "approval_id": approval_id,
                "tool_name": req.tool_name,
                "tool_call_id": req.tool_call_id,
                "action_type": req.permission.value,
                "title": f"{req.tool_name} ({req.permission.value})",
                "description": req.reason,
                "arguments_preview": req.args,
                "permission": req.permission.value,
                "policy": req.policy.value,
                "reason": req.reason,
                "allowed_decisions": allowed_decisions,
            },
        )
        response = await self._queue.wait_for(approval_id)
        self._session.append(
            "permission/resolved",
            {
                "approval_id": approval_id,
                "decision": response.decision.value,
                "reason": response.reason,
            },
        )
        return response
