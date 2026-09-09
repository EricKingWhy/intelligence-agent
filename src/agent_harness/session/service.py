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

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

import anyio

from agent_harness.assembly import build_runtime
from agent_harness.config import Settings
from agent_harness.session.event import (
    MESSAGE_QUEUED,
    MODEL_CHANGED,
    QUEUE_CANCELLED,
    SESSION_STARTED,
    STEER_REQUESTED,
    TOOL_APPROVAL_REQUESTED,
    SessionEvent,
    _utc_now_iso,
)
from agent_harness.session.queue import QueuedMessage, SteerRequest
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
    from agent_harness.web.app import AppState
    from agent_harness.web.runmanager import ManagedRun, RunManager, Subscriber


logger = logging.getLogger(__name__)


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


class QueueItemNotFound(SessionServiceError):
    """排队消息不存在 / 已消费 / 已取消。"""


class SteerTargetNotFound(SessionServiceError):
    """steer 目标 run 不存在（无在途 run）。"""


class UnknownModel(SessionServiceError):
    """模型切换目标不在 catalog 中（provider + model_id 未命中）。"""


class InvalidForkBoundary(SessionServiceError):
    """fork 锚点非法（不是用户消息 seq / 前缀含未终态 run）。"""


#: session_id 安全校验正则——名字段，不是路径。
#: store.read_events 直接 ``self._root / session_id`` 拼路径：不校验时
#: 反斜杠段在 win32 上可越出 sessions 根目录，盘符段可整体替换基路径。
#: 字符集与 S3ArtifactStore 的 key 段规则一致。
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def validate_session_id(session_id: str) -> str:
    """校验 session_id 是否为单个安全名字段。

    正则 ``[A-Za-z0-9_-]+`` 保证只接受字母、数字、下划线、连字符，
    拒绝含 ``/`` ``\\\\`` ``.`` 等路径分隔符的输入（路径穿越防护）。

    :returns: 校验通过的 session_id（原值返回）。
    :raises InvalidSessionId: session_id 含非法字符或为空。
    """
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise InvalidSessionId(
            f"session_id 只接受单个安全名字段：{session_id!r}"
        )
    return session_id


# ── 数据载体 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AmendOptions:
    """staged amend 字段：续跑/续聊时覆盖运行时可配置项。

    全部可空——None = 使用 session 既有配置（默认行为不变）。
    service 层 create / resume / send_message / drain 共用这一束，
    避免 4 个字段在每个签名里平铺（Data Clump）。
    """

    reasoning_effort: str | None = None
    agent_profile: str | None = None
    context_providers: list[str] | None = None
    model: str | None = None

    @classmethod
    def from_request(cls, request: Any) -> AmendOptions:
        """从请求模型组装（Pydantic 或任何带同名属性的对象）。

        web 层三个请求体（create / resume / messages）字段同形，组装逻辑
        收敛到这里，避免在 app.py 重复三遍。
        """
        return cls(
            reasoning_effort=getattr(request, "reasoning_effort", None),
            agent_profile=getattr(request, "agent_profile", None),
            context_providers=getattr(request, "context_providers", None),
            model=getattr(request, "model", None),
        )

    def to_runtime_kwargs(self) -> dict[str, Any]:
        """转成 ``build_runtime`` 的 amend 相关关键字参数。

        ``model`` → ``model_name``（build_runtime 的参数名）；四个字段总是
        全部给出（None 即默认行为），让调用点无需重复 None 判断。
        """
        return {
            "model_name": self.model,
            "reasoning_effort": self.reasoning_effort,
            "agent_profile": self.agent_profile,
            "context_providers": self.context_providers,
        }


def _amend_kwargs(amend: AmendOptions | None) -> dict[str, Any]:
    """amend → build_runtime 关键字参数；None 等价于全 None（当前行为不变）。"""
    return (amend or AmendOptions()).to_runtime_kwargs()


def current_model_selection(events: list[SessionEvent]) -> tuple[str | None, str | None]:
    """从事件流派生会话当前模型 (provider, model_id)。

    优先级：最后一条 ``model/changed`` 的 to_* > ``session/started`` 的初始值
    > (None, None)（= 默认链）。append-only 语义下"最后一次切换"即当前模型，
    replay 确定性（不变量 #3）。
    """
    for event in reversed(events):
        if event.type == MODEL_CHANGED:
            return event.data.get("to_provider"), event.data.get("to_model_id")
    for event in reversed(events):
        if event.type == SESSION_STARTED:
            return event.data.get("provider"), event.data.get("model_id")
    return None, None


def _amend_with_session_model(
    amend: AmendOptions | None, events: list[SessionEvent], settings: Settings
) -> AmendOptions | None:
    """未显式指定 model 时，用 session 派生的当前模型补齐。

    显式 amend.model 永远优先（一次性覆盖）；session 也没记录模型则原样返回
    （走默认链，行为不变）。session 记录的模型若已不在 catalog（配置变更），
    回落默认链并记 warning——历史选择不该让续聊 500。AGENT_MODELS 本身畸形
    仍是配置错误，`parse_model_catalog` 照旧响亮失败（不静默降级）。
    """
    if amend is not None and amend.model is not None:
        return amend
    provider, model_id = current_model_selection(events)
    if model_id is None:
        return amend

    from agent_harness.model.config import find_catalog_entry

    if find_catalog_entry(settings, provider or "", model_id) is None:
        logger.warning(
            "会话当前模型 %s/%s 已不在 catalog，本轮回落默认链", provider, model_id
        )
        return amend
    return replace(amend or AmendOptions(), model=model_id)


def _default_model_id(settings: Settings) -> str:
    """默认链在 ``GET /api/models`` 里的 picker id（= 默认模型名）。"""
    from agent_harness.model.config import ModelConfig

    return ModelConfig.from_settings(settings).model_name


def _is_default_selection(settings: Settings, provider: str, model_id: str) -> bool:
    """判断目标是否是 ``GET /api/models`` 的默认条目（is_default=true）。

    默认条目不是一个 catalog 条目，但前端 picker 会展示它——选中它 = 清除会话级
    覆盖、回到默认链（事件写 ``to_model_id=None``）。
    """
    from agent_harness.model.config import ConfigError

    if provider != settings.model_provider:
        return False
    try:
        default_name = _default_model_id(settings)
    except ConfigError:
        return False
    return model_id in {default_name, "default"}


@dataclass(frozen=True)
class ModelTarget:
    """一次模型切换的解析结果（T7 #137）。

    ``model_id=None`` = 切回默认链（``GET /api/models`` 的 is_default 条目）；
    ``effective_model_id`` 是 picker 应显示的 id——catalog 条目名，或默认链的默认
    模型名（HTTP 响应回传它，避免 handler 自己推导领域值）。
    """

    provider: str
    model_id: str | None
    effective_model_id: str


def resolve_model_target(
    settings: Settings, provider: str, model_id: str
) -> ModelTarget | None:
    """解析 (provider, model_id) → ModelTarget；未命中返回 None。

    默认条目优先于同名 catalog 条目：picker 的默认选项 id 就是默认模型名，
    catalog 恰有同名条目时必须按「默认条目 = 清覆盖」语义处理，否则前端选默认
    反而锁死在该 catalog 条目的 base_url / temperature 上。
    """
    from agent_harness.model.config import find_catalog_entry

    if _is_default_selection(settings, provider, model_id):
        return ModelTarget(
            provider=provider, model_id=None,
            effective_model_id=_default_model_id(settings),
        )
    entry = find_catalog_entry(settings, provider, model_id)
    if entry is None:
        return None
    return ModelTarget(
        provider=entry.provider, model_id=entry.name, effective_model_id=entry.name,
    )


@dataclass(frozen=True)
class ModelChange:
    """一次模型切换的结果。

    ``from_*`` 可能为 None = 此前走默认链；``to_model_id`` 为 None = 切回默认链
    （选中 ``GET /api/models`` 的 ``is_default`` 条目），后续 run 不再带 catalog 覆盖。
    ``effective_model_id`` = 前端 picker 应对齐的 id（见 ``ModelTarget``）。
    """

    from_provider: str | None
    from_model_id: str | None
    to_provider: str
    to_model_id: str | None
    effective_model_id: str


def append_model_change(session: Session, target: ModelTarget) -> ModelChange:
    """追加 ``model/changed``——MODEL_CHANGED 的唯一写入口（T7 #137）。

    走 ``Session.append``（无 resume 副作用，不变量 #7）；``from_*`` 从事件流派生。
    service 与 CLI demo 共用，避免两处各自拼事件 data。
    """
    from_provider, from_model_id = current_model_selection(session.events)
    session.append(MODEL_CHANGED, {
        "from_provider": from_provider,
        "from_model_id": from_model_id,
        "to_provider": target.provider,
        "to_model_id": target.model_id,
    })
    return ModelChange(
        from_provider=from_provider,
        from_model_id=from_model_id,
        to_provider=target.provider,
        to_model_id=target.model_id,
        effective_model_id=target.effective_model_id,
    )


def inherit_parent_model(child: Session, parent_events: list[SessionEvent]) -> None:
    """child 继承父会话的当前模型（fork seed 不含父 ``session/started``，T7 #137）。

    父走默认链时什么都不做；父有会话级模型时补一条 ``model/changed``——否则父创建
    时选的 catalog 模型会在 child 静默回落默认链（切换过的父反而会继承，语义不一致）。
    """
    provider, model_id = current_model_selection(parent_events)
    if provider is None or model_id is None:
        return
    append_model_change(
        child,
        ModelTarget(
            provider=provider, model_id=model_id, effective_model_id=model_id,
        ),
    )


def assert_model_resolvable(settings: Settings, target: ModelTarget) -> None:
    """校验目标模型真能装配（provider 已知 + 有可用 key），否则 UnknownModel。

    AC「校验 provider 可用性」：catalog 成员资格之外，还确认 ``ModelConfig`` 能
    构造出来——与创建路径（``create_and_launch`` 的 ``from_catalog``）同一判据，
    避免 POST /model 接受一个 POST /sessions 会拒绝的目标。CLI 与 Web 共用。
    """
    from agent_harness.model.config import ConfigError, ModelConfig

    try:
        if target.model_id is None:
            ModelConfig.from_settings(settings)
        else:
            ModelConfig.from_catalog(settings, target.model_id)
    except (ConfigError, KeyError) as error:
        raise UnknownModel(str(error)) from error


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


@dataclass(frozen=True)
class SendMessageResult:
    """send_message 的返回束——三种结果分支。

    - launched：idle session，直接起了新 run。
    - queued：活跃 run，消息已排队。
    - steered：活跃 run + mode=steer，请求已注册。
    """

    status: str  # "launched" | "queued" | "steered"
    session: Session | None = None
    run: ManagedRun | None = None
    subscriber: Subscriber | None = None
    queued_message: QueuedMessage | None = None
    steer_request: SteerRequest | None = None

    def to_response(self) -> dict[str, str]:
        """Web 层 queued/steered 分支返回体（launched 走 SSE 不经此）。

        始终包含 ``status``；queued 附 ``queue_id``，steered 附
        ``steer_id``——前端据此更新本地占位状态/取消按钮。
        """
        if self.status == "queued" and self.queued_message is not None:
            return {"status": "queued", "queue_id": self.queued_message.queue_id}
        if self.status == "steered" and self.steer_request is not None:
            return {"status": "steered", "steer_id": self.steer_request.steer_id}
        return {"status": self.status}


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

    @property
    def session_meta_store(self):
        """Session 元信息存储（fork 需要写 child 的 provenance）。"""
        return self._state.session_meta_store

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
        amend: AmendOptions | None = None,
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

        # 模型 catalog 校验（在落盘前，避免孤儿）；合法则记为会话初始模型，
        # 使后续 run 不传 amend 也能从事件流派生出「当前模型」（T7 #137）。
        initial_model_data: dict[str, Any] = {}
        if amend is not None and amend.model is not None:
            try:
                initial_model = ModelConfig.from_catalog(
                    self._state.settings, amend.model
                )
            except ConfigError as error:
                raise InvalidDecision(str(error)) from error
            initial_model_data = {
                "provider": initial_model.provider,
                "model_id": amend.model,
            }

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
            **_amend_kwargs(amend),
        )
        session = Session.start(
            self._state.store, session_id=session_id,
            started_data=initial_model_data or None,
        )

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
        amend: AmendOptions | None = None,
    ) -> LaunchResult:
        """恢复已有 Session 并追加一轮新 user input（原 POST /resume）。

        这是「续跑」而非精确恢复中断 run：Session.resume 重建 append-only
        历史与 dangling 修复，RunManager.launch 驱动一轮新 Agent Loop。
        在途 session 拒绝（ActiveRunConflict），避免并发。

        staged amend 字段（amend）透传给 build_runtime，与
        create_and_launch 对齐。默认 None = 当前行为不变。
        """
        self._validate_session_id(session_id)
        existing = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        if not existing:
            raise SessionNotFound(f"session '{session_id}' not found")
        # T7 #137：未显式指定 model 时用会话派生的当前模型（切换后下一轮生效）。
        amend = _amend_with_session_model(amend, existing, self._state.settings)
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
            **_amend_kwargs(amend),
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

    # ── 续聊（Phase Multiturn T2 / PRD §5.3）──────────────────────

    def _live_session(self, session_id: str) -> Session | None:
        """在途 run 持有的 Session 聚合（seq 计数器与 listener 都是活的）；无则 None。

        旁路追加必须优先用它：两个 Session 实例各自从同一磁盘快照推算 seq 会撞号
        （run 的内存计数器看不到旁路追加，写出重复 seq 让会话不可 resume），且
        run 的 listener 在册才能把旁路事件实时广播给 SSE/WS 订阅者。
        """
        active = self._state.run_manager.get_active(session_id)
        return active.session if active is not None else None

    def _append_session_event(
        self, session_id: str, event_type: str, **data: object
    ) -> None:
        """追加 typed 事件（queue / steer 路径共用 helper）。

        在途 run 存在时走 ``_live_session``；否则走 ``Session.append_event``
        （只 append，不做 resume 的 dangling 修复 / ``session/resumed``，不变量 #7）。
        """
        live = self._live_session(session_id)
        if live is not None:
            live.append(event_type, dict(data))
            return
        Session.append_event(self._state.store, session_id, event_type, dict(data))

    async def send_message(
        self,
        *,
        session_id: str,
        content: str,
        mode: str = "queue",
        max_steps: int = 10,
        amend: AmendOptions | None = None,
    ) -> SendMessageResult:
        """续聊消息入口（统一 CLI / Web 续聊路径）。

        双模式（PRD 锁定决策 D-7）：

          * ``mode="queue"``（默认）：
              - idle（无在途 run）→ ``resume_and_launch`` 拉起新 run，返回
                ``SendMessageResult(status="launched", ...)``；Web 层据此打开
                SSE 流（同创建端点语义，PRD D-10）。
              - 活跃 run → 入队等下个 run 自然消费，写一条 ``MESSAGE_QUEUED``
                SessionEvent，返回 ``status="queued"``。
          * ``mode="steer"``：仅在途 run 时合法——注册 SteerRequest，写
            ``STEER_REQUESTED`` 事件，返回 ``status="steered"``；无在途 run →
            ``SteerTargetNotFound``（409）。

        staged amend 字段透传给 ``resume_and_launch``（idle 分支），
        与 create 路径对齐。默认 None = 当前行为不变。

        不抢断、不改写历史事件（不变量 #3 / #22）；queue 与 steer 的消费由
        run 边界 / runtime 自身驱动，本方法只做注册与 durable 记录。
        """
        self._validate_session_id(session_id)
        if not await self.has_session(session_id):
            raise SessionNotFound(f"session '{session_id}' not found")

        active_run = self._state.run_manager.get_active(session_id)

        if mode == "steer":
            if active_run is None:
                raise SteerTargetNotFound(
                    "steer requires an active run; use mode='queue' to enqueue"
                )
            steer_req = await self._state.message_queues.register_steer(
                session_id=session_id,
                content=content,
                run_id=active_run.run_id,
                created_at=_utc_now_iso(),
            )
            self._append_session_event(
                session_id, STEER_REQUESTED,
                steer_id=steer_req.steer_id,
                content=content,
                run_id=steer_req.run_id,
            )
            return SendMessageResult(status="steered", steer_request=steer_req)

        # mode == "queue"
        if active_run is None:
            # idle → 直接拉起新 run（同 resume 路径）。
            launched = await self.resume_and_launch(
                session_id=session_id, task=content, max_steps=max_steps,
                amend=amend,
            )
            return SendMessageResult(
                status="launched",
                session=launched.session,
                run=launched.run,
                subscriber=launched.subscriber,
            )

        # 活跃 run → 入队（FIFO）+ 写 MESSAGE_QUEUED。
        queued = await self._state.message_queues.enqueue(
            session_id=session_id, content=content, created_at=_utc_now_iso()
        )
        self._append_session_event(
            session_id, MESSAGE_QUEUED,
            queue_id=queued.queue_id,
            content=content,
        )
        return SendMessageResult(status="queued", queued_message=queued)

    async def cancel_queue(self, *, session_id: str, queue_id: str) -> bool:
        """取消尚未消费的排队消息（PRD §5.3）。

        幂等失败语义：queue_id 已取消 / 已消费 / 不存在 →
        ``QueueItemNotFound``（前端 404）；取消成功 → 写一条
        ``QUEUE_CANCELLED`` SessionEvent，返回 True。
        """
        self._validate_session_id(session_id)
        if not await self.has_session(session_id):
            raise SessionNotFound(f"session '{session_id}' not found")
        cancelled = await self._state.message_queues.cancel(
            session_id=session_id, queue_id=queue_id
        )
        if not cancelled:
            raise QueueItemNotFound(
                f"queue item '{queue_id}' not found, already consumed, or cancelled"
            )
        self._append_session_event(session_id, QUEUE_CANCELLED, queue_id=queue_id)
        return True

    async def drain_queued_message(
        self,
        *,
        session_id: str,
        max_steps: int = 10,
        amend: AmendOptions | None = None,
    ) -> LaunchResult | None:
        """run 结束后自动消费下一条排队消息（PRD §5.3 FIFO 续聊链）。

        无排队或全部已取消 → 返回 None（调用方静默收尾）；有可用消息 →
        drain 出来 + ``resume_and_launch`` 拉起下一轮 run。此方法是
        「续聊链接力」的唯一驱动入口，避免 Web / CLI 各自实现而漂移。
        staged amend 字段透传给 ``resume_and_launch``。
        """
        self._validate_session_id(session_id)
        msg = await self._state.message_queues.drain_next(session_id)
        if msg is None:
            return None
        return await self.resume_and_launch(
            session_id=session_id, task=msg.content, max_steps=max_steps,
            amend=amend,
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

    # ── 模型切换 / Fork（T7 #137）────────────────────────────────────

    async def change_model(
        self, *, session_id: str, provider: str, model_id: str
    ) -> ModelChange:
        """切换会话当前模型并写 ``model/changed``（PRD §2.3）。

        model_id 命中 catalog 条目名或上游模型名；provider 必须与条目一致
        （防止跨 provider 误选）。切换只写事件，不打断在途 run——下一轮
        run 经 ``resume_and_launch`` 从事件流派生当前模型生效。
        """
        from agent_harness.model.config import parse_model_catalog

        self._validate_session_id(session_id)
        existing = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        if not existing:
            raise SessionNotFound(f"session '{session_id}' not found")

        target = resolve_model_target(self._state.settings, provider, model_id)
        if target is None:
            raise UnknownModel(
                f"未知模型: provider={provider!r} model_id={model_id!r}，可选: "
                f"{[(e.provider, e.name) for e in parse_model_catalog(self._state.settings)]}"
            )
        assert_model_resolvable(self._state.settings, target)
        # 在途 run 存在时用它的 Session 聚合追加（seq 不撞号 + listener 实时广播）；
        # 否则只读加载一个聚合。两条路径都只 append，不走 resume。
        live = self._live_session(session_id) or Session(
            session_id, self._state.store, existing
        )
        return append_model_change(live, target)

    async def fork(
        self, *, session_id: str, from_seq: int, with_tail_summary: bool = False
    ) -> str:
        """从 ``from_seq``（用户消息 seq）派生 child session，返回其 id（PRD §2.4）。

        复用 ``fork.py``，不重写。在途 run 拒绝（ActiveRunConflict），锚点非法
        → InvalidForkBoundary。HTTP 端点默认不生成 tail summary（无模型调用，
        确定性；CLI ``fork`` 仍可显式开启）。

        child 继承父会话的当前模型：fork seed 按设计不含父 ``session/started``，
        不补一条 ``model/changed`` 的话，父创建时选的 catalog 模型会在 child 静默
        回落到默认链（切换过的父则会继承，语义不一致）。
        """
        from agent_harness.session.fork import ForkBoundaryError, fork_session

        self._validate_session_id(session_id)
        existing = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        if not existing:
            raise SessionNotFound(f"session '{session_id}' not found")
        if self._state.run_manager.get_active(session_id) is not None:
            raise ActiveRunConflict("session has an active run; fork needs settled history")
        await self._state.ensure_stores()
        try:
            child = await fork_session(
                self._state.store,
                self._state.session_meta_store,
                session_id,
                boundary_user_message_seq=from_seq,
                workspace_registry=self._state.workspace_registry,
                with_tail_summary=with_tail_summary,
            )
        except ForkBoundaryError as error:
            raise InvalidForkBoundary(str(error)) from error
        inherit_parent_model(child, existing)
        return child.session_id

    # ── 内部方法 ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        """委托公开函数 validate_session_id（保持调用方 self._validate_session_id 不变）。"""
        validate_session_id(session_id)

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
            return _InteractiveCallbackHolder(
                queue=queue,
                timeout_seconds=self._state.settings.approval_timeout_seconds,
            )
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

    def __init__(self, *, queue: PendingApprovalQueue, timeout_seconds: float) -> None:
        self._queue = queue
        self._session: Session | None = None
        #: ≤0 → None（无限等待，旧行为）；>0 → fail-closed 超时（PRD T6 §2.2 C）。
        #: 无默认值：审批等待是安全边界，超时值必须由调用方（Settings）显式给出。
        self._timeout: float | None = timeout_seconds if timeout_seconds > 0 else None

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
        try:
            response = await self._queue.wait_for(approval_id, timeout=self._timeout)
        except TimeoutError:
            # fail-closed（PRD T6 §2.2 C）：无人决策 = 拒绝，绝不默认放行。
            assert self._timeout is not None  # 未配置超时不会抛 TimeoutError
            timeout_deny = ApprovalResponse(
                approved=False,
                reason=f"审批超时（{self._timeout:g}s 无决策），按 fail-closed 拒绝",
                decision=PermissionDecision.DENY,
            )
            if self._queue.expire(approval_id, timeout_deny):
                response = timeout_deny
            else:
                # 极端竞态：外部 /approve 与超时同刻到达，且 /approve 已抢先写入
                # _resolved（future 已被 wait_for 取消 → 本协程收到 TimeoutError）。
                # 先写入者胜（一次性语义）：采用人类决策，不覆盖。
                settled = self._queue.resolved_response(approval_id)
                response = settled if settled is not None else timeout_deny
        self._session.append(
            "permission/resolved",
            {
                "approval_id": approval_id,
                "decision": response.decision.value,
                "reason": response.reason,
            },
        )
        return response
