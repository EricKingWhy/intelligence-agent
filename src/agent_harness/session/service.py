"""SessionService — CLI/Web 共享的会话领域服务（T1 / #131）。

从 Web FastAPI handlers 抽出的 session 生命周期逻辑，统一供 CLI 与 Web 调用。
HTTP 是传输层，不进本模块；领域异常由调用方翻译为 HTTP/CLI 响应。

设计原则（PRD §4）：
- Web FastAPI handler 只做参数校验 + 调用 SessionService + 响应封装。
- CLI 直接调用 SessionService（T3 重构后）。
- 所有真相来自 append-only JSONL + Session 单一事实源（不变量 #22）。
- Tool 只有一条执行路径（不变量 #7）：审批回传统一汇聚到 ToolExecutor callback。

本模块是 T1 纯重构产物——无行为变化，现有测试保持 green。

候选 2（架构深化，纯结构重构）把三块高内聚的领域逻辑抽到兄弟模块，本模块保留
``SessionService`` 门面并重新导出全部公开符号，因此所有既有导入路径
（``from agent_harness.session.service import X``）与调用点均不变：

- ``session/errors.py``       —— 领域异常层级（14 个类）
- ``session/model_switch.py`` —— 会话级模型切换（MODEL_CHANGED 唯一写入口）
- ``session/approval.py``     —— 交互式审批 callback 构建 + 延迟绑定容器

``build_runtime`` 仍在本模块以模块级名字导入：``tests/web/test_web_phase5_permission.py``
用 ``monkeypatch.setattr(service_module, "build_runtime", ...)`` 打桩，名字必须在场。
"""

from __future__ import annotations

import logging
import os
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

import anyio

from agent_harness.assembly import build_runtime
from agent_harness.logging import log_event
from agent_harness.sandbox.paths import canonical_workspace_path, is_absolute_path
from agent_harness.session.amend import AmendOptions, amend_kwargs
from agent_harness.session.approval import (
    SESSION_AUTO_APPROVE_KEY,
    SESSION_PERMISSION_MODE_KEY,
)
from agent_harness.session.approval import (
    InteractiveCallbackHolder as _InteractiveCallbackHolder,
)
from agent_harness.session.approval import (
    build_approval_callback as _build_approval_callback_impl,
)
from agent_harness.session.approval import (
    declared_auto_approve as _declared_auto_approve,
)
from agent_harness.session.approval import (
    declared_permission_mode as _declared_permission_mode,
)
from agent_harness.session.derive import (
    KIND_QUEUE,
    UndeliveredInput,
    detect_dangling,
    undelivered_inputs,
)
from agent_harness.session.errors import (
    ActiveRunConflict,
    ApprovalAlreadyResolved,
    ApprovalQueueMissing,
    ApprovalRequestMissing,
    InvalidDecision,
    InvalidForkBoundary,
    InvalidSessionId,
    QueueItemNotFound,
    RecoveryConflict,
    SeqConflict,
    SessionHasChildren,
    SessionNotFound,
    SessionServiceError,
    SteerTargetNotFound,
    SupersedeTargetInvalid,
    UnknownModel,
    WorkspaceNameInvalid,
    WorkspaceNotFound,
    WorkspacePathInvalid,
)
from agent_harness.session.event import (
    MESSAGE_QUEUED,
    MESSAGE_SUPERSEDED,
    QUEUE_CANCELLED,
    QUEUE_CONSUMED,
    STEER_APPLIED,
    STEER_REQUESTED,
    TOOL_APPROVAL_REQUESTED,
    USER_MESSAGE,
    _utc_now_iso,
)
from agent_harness.session.interrupt import detect_unterminated_runs
from agent_harness.session.model_switch import (
    ModelChange,
    ModelTarget,
    append_model_change,
    assert_model_resolvable,
    current_model_selection,
    inherit_parent_model,
    resolve_model_target,
)
from agent_harness.session.model_switch import (
    amend_with_session_model as _amend_with_session_model,
)
from agent_harness.session.queue import QueuedMessage, SteerRequest
from agent_harness.session.session import Session, validate_event_seq
from agent_harness.session.store import (
    JsonlSessionStore,
    SessionSummaryStats,
    WorkspaceRef,
)
from agent_harness.storage.artifact import SESSION_KEY_PATTERN
from agent_harness.storage.local_artifact import discard_local_artifacts
from agent_harness.storage.session_meta import SessionMeta
from agent_harness.tooling.approval import (
    ApprovalCallback,
    ApprovalResponse,
    PermissionDecision,
)
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.contract import PermissionPolicy

if TYPE_CHECKING:
    from agent_harness.recovery.scan import InterruptionScanResult
    from agent_harness.web.app import AppState
    from agent_harness.web.runmanager import ManagedRun, RunManager, Subscriber
    from agent_harness.workspace.index import WorkspaceIndex


logger = logging.getLogger(__name__)

#: 归档审计里的入口标识（#171 AC6 的字段之一）。两个路由（归档 / 取消归档）共用同一份
#: 字面量——审计格式只此一处定义，免得两个入口各写一套、迟早漂移成对不上账的记录
#: （`memory/audit.py::ENTRY_*` 同款）。
ARCHIVE_ENTRY_API = "api"


# ── 领域异常 ──────────────────────────────────────────────────────────
# 定义已移至 session/errors.py（候选 2 纯结构重构）；此处重新导出，
# 保持 `from agent_harness.session.service import SessionNotFound` 等导入路径不变。

#: session_id 安全校验正则——名字段，不是路径。
#: store.read_events 直接 ``self._root / session_id`` 拼路径：不校验时
#: 反斜杠段在 win32 上可越出 sessions 根目录，盘符段可整体替换基路径。
#: 规则本体在 `storage/artifact.py`（artifact 存储键的 session 段是同一条规则，
#: 那边要把它拼进 artifact 根目录的路径——一份定义，两处使用）。
_SESSION_ID_PATTERN = SESSION_KEY_PATTERN

#: 非 live 追加路径撞上 seq 冲突时的重试次数（BUG-011）。
#: 「读快照 → 取号 → append」不是原子的：并发写者可能在本请求读完之后落盘同号，
#: 此时 store 的 seq 守卫拒写。重读快照即可拿到新号——3 次足够覆盖真实并发度
#: （真机现场是双击产生的两个请求），持续竞争则把 SeqConflict 抛给上层。
_WRITE_CONFLICT_ATTEMPTS = 3


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
# AmendOptions 已移至 session/amend.py（候选 2 后续修正：消除 model_switch ↔
# service 的双向导入环）。本模块从那里重新导出，既有导入路径不变。


# ── 模型切换 / Fork 领域逻辑 ────────────────────────────────────────
# current_model_selection / resolve_model_target / append_model_change /
# inherit_parent_model / assert_model_resolvable 已移至 session/model_switch.py
# （候选 2）；本模块从那里重新导出，CLI / Web 调用点不变。


@dataclass(frozen=True)
class LaunchResult:
    """create_and_launch / resume_and_launch 的返回束。

    Web 层用 run + subscriber 组装 SSE；CLI 层可直接 await run.task
    或忽略 run（自有驱动路径）。

    #204：`launch=False` 的只建路径返回 `run=None`/`subscriber=None`——
    没有 run 就没有订阅句柄，None 是诚实的"不存在"，调用方据此不组 SSE。
    """

    session: Session
    run: ManagedRun | None
    subscriber: Subscriber | None


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


@dataclass(frozen=True)
class SessionDeletionStats:
    """`delete_session` 的结果束（#172 / ADR-0029）：删了什么，可核对。

    计数不是装饰：前端要拿它写"已删除 N 条事件、从 M 个项目解除"的确认回执；
    也因为七个删除步骤全是幂等的，"删了 0 条事件"必须能被看见（重跑自愈时）。
    """

    session_id: str
    #: 删除前日志里的事件条数（取自 `read_session_summary`，与列表页同一条判据）。
    events: int
    #: 本次**真的**从多少个账本里摘掉了这个会话——直接取 `detach_session` 的返回值
    #: （它修剪的是原始账本）。正常 0/1；账本里重复收录同一 id 时会 >1。
    detached_from_projects: int


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

    async def list_sessions(
        self, *, workspace_id: str | None = None, include_archived: bool = False
    ) -> list[SessionSummaryStats]:
        """列出 session 摘要。

        - **默认**：全部会话，按**最近活动**倒序（既有契约不变；未分组会话照旧在列表里
          可见，AC5）。
        - **`workspace_id` 给出时**：只列该项目的会话，顺序 = **账本的手工序**
          （AC4——活动时间永不重排，否则用户手工拖过的顺序每次刷新就丢）；项目未注册
          → `WorkspaceNotFound`（404），**不**伪装成空列表。
        - **`include_archived=False`（默认）**：#171 起**不返回**已归档会话；置 true 时
          照常返回（前端"显示已归档"开关用）。两条路径（默认列表 / 项目视图）用**同一
          规则**，避免"侧栏藏了、项目里还露着"。

        每行回填 `workspace`（AC1/AC2）：未分组 → `None`，绝不伪造。项目归属只来自
        `WorkspaceIndex`（账本 ∩ header cwd），**不读** sandbox 的 `WorkspaceRegistry`
        映射表——那是沙箱生命周期记录，不是成员资格真源（#152 的交接约束）。

        `archived`（#171）来自 `session_meta`，与 `workspace` 一样是服务层回填：列表是
        **文件系统驱动**的（扫 `<root>/<sid>/events.jsonl`），归档标记在 DB ⇒ 必须 join
        两边，且 **`session_meta` 无该行一律视为未归档**（AC3 的口径要点）。

        直接返回领域 dataclass（`SessionSummaryStats` 自带 `session_id`），不拼一层
        只做形状复述、没有校验与行为的 dict 中转——列表行的字段因此在领域层就有带类型的
        唯一定义点。由调用方（`web/app.py`）映射为 API response model。
        """
        await self._state.ensure_stores()
        store = self._state.store
        index = self._state.workspace_index

        if workspace_id is not None:
            # `index.get()` 经 `_view → _visible_ids → _filter_visible → _read_header`
            # 真读每个账本候选的 `events.jsonl` 头部——该读取有意**不缓存**
            # （见 `WorkspaceIndex._read_header` 的 AC6 理由）。与下面的
            # `read_session_summary` 同理：同步磁盘 I/O 必须离开事件循环，否则一次
            # 列表请求就阻塞整个 asyncio loop（该处文档承诺"同步磁盘 I/O 仍走
            # to_thread 卸载"，本行必须与之一致）。
            workspace = (
                await anyio.to_thread.run_sync(index.get, workspace_id)
                if index is not None
                else None
            )
            if workspace is None:
                raise WorkspaceNotFound(f"workspace '{workspace_id}' not found")
            ids: list[str] = list(workspace.session_ids)
            fixed_ref = WorkspaceRef(id=workspace.id, title=workspace.title)
            refs: dict[str, WorkspaceRef] = {}
        else:
            ids = await anyio.to_thread.run_sync(store.list_session_ids)
            fixed_ref = None
            # 一次 list() 建出全量 {session_id: 项目引用} 映射：逐行调
            # `workspace_of_session` 会是 O(行数 × 项目数 × 账本长度)。`list()` 每条
            # 账本只读一次 header，且 session_ids 已过成员资格过滤。
            # `run_sync` 只接位置参数；读 header 同样是同步 I/O，一并卸载。
            refs = await anyio.to_thread.run_sync(
                self._workspace_refs_by_session, index
            )

        # #171：归档标记在 DB、列表在文件系统 ⇒ join 两边。一次 `list_all()` 建出
        # {已归档 id}（逐行 `get()` 是 O(行数) 次 DB 往返）；**无 meta 行的会话不在
        # 集合里** ⇒ 一律视为未归档（AC3）。过滤放在读摘要之前：已归档的行连日志头
        # 都不用读。`include_archived=True` 时不过滤，但每行仍带真值徽标。
        archived_ids = await self._archived_session_ids()
        if not include_archived:
            ids = [sid for sid in ids if sid not in archived_ids]

        summaries: list[SessionSummaryStats] = []
        for sid in ids:
            stats = await anyio.to_thread.run_sync(store.read_session_summary, sid)
            if stats is None or stats.event_count == 0:
                continue
            backfill: dict[str, Any] = {"archived": sid in archived_ids}
            ref = fixed_ref if workspace_id is not None else refs.get(sid)
            if ref is not None:
                backfill["workspace"] = ref
            summaries.append(replace(stats, **backfill))
        return summaries

    async def _archived_session_ids(self) -> set[str]:
        """已归档的 session_id 集合（`session_meta.archived` 为真的行）。

        无 meta 行的会话**不在**集合里 ⇒ 视为未归档（AC3 的口径要点——`session_meta`
        行由 lineage/fork 懒补，不能假设"每个会话恒有一行"）。
        """
        metas = await self._state.session_meta_store.list_all()
        return {meta.session_id for meta in metas if meta.archived}

    @staticmethod
    def _workspace_refs_by_session(
        index: WorkspaceIndex | None,
    ) -> dict[str, WorkspaceRef]:
        """`{session_id: 项目引用}`（`index` 为 None → 空映射 = 全部未分组）。"""
        if index is None:
            return {}
        refs: dict[str, WorkspaceRef] = {}
        for workspace in index.list():
            ref = WorkspaceRef(id=workspace.id, title=workspace.title)
            for session_id in workspace.session_ids:
                refs[session_id] = ref
        return refs

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
        cwd: str | None = None,
        max_steps: int = 10,
        permission_mode: PermissionPolicy = PermissionPolicy.WORKSPACE_WRITE,
        permission_mode_explicit: bool = False,
        auto_approve_explicit: bool = False,
        auto_approve: bool = True,
        amend: AmendOptions | None = None,
        launch: bool = True,
    ) -> LaunchResult:
        """创建新 Session 并启动 run（原 POST /api/sessions 的领域逻辑）。

        返回 LaunchResult(session, run, subscriber)。调用方：
        - Web：消费 subscriber.queue 组装 SSE 流。
        - CLI：可 await run.task 或忽略（自有驱动路径）。

        `launch`（#204，默认 True ⇒ 既有行为逐字不变）：
        - True：创建 + 启动 run（现有路径）；
        - False：**只建会话**——session/started 与全部会话元数据照常落盘，
          但不调 RunManager.launch（无在途 run、无订阅者），返回束的
          run/subscriber 为 None。空会话入口（前端"在项目中新建任务"弹窗）
          与未来的空会话创建共用这一条路径，不再各自造。

        组装顺序（R6-6）：先建 workspace + runtime，最后才 Session.start 落盘，
        避免 runtime 组装失败时留下只含 session/started 的孤儿 session。

        `workspace_name` 与 `cwd` 二选一（ADR-0027）：
        - `workspace_name`（旧契约，逐字节不变）：单个目录名，目录在
          `workspaces_root` 下由 Harness **创建**；
        - `cwd`（#169 新增）：**已存在**的绝对目录，会话直接以它为操作目录
          （不创建、不复制），并自动注册为项目 + 归组。
        """
        from uuid import uuid4

        from agent_harness.model.config import ConfigError, ModelConfig

        # 校验顺序即契约（PRD §4.1）：先"二选一"（两个都给了就没有优先级问题可言），
        # 再各走各的形态校验。"非空"按 strip 后的内容判；只给了一个但内容空白（如
        # `cwd=""`）**不**当作缺省——显式传的字段必须给出明确的形态错误，静默忽略是
        # 最坏的一种"宽容"（`_resolve_cwd` / `_validate_workspace_name` 各自报出）。
        has_cwd = cwd is not None and bool(cwd.strip())
        has_workspace = workspace_name is not None and bool(workspace_name.strip())
        if has_cwd and has_workspace:
            raise WorkspacePathInvalid("workspace 与 cwd 只能二选一")
        workspace_name = self._validate_workspace_name(workspace_name)

        session_id = str(uuid4())
        if cwd is not None:
            workspace = self._resolve_cwd(cwd)
        elif workspace_name is not None:
            workspace = self._state.workspaces_root / workspace_name
            workspace.mkdir(parents=True, exist_ok=True)
        else:
            workspace = self._state.workspaces_root / session_id
            workspace.mkdir(parents=True, exist_ok=True)

        # 模型 catalog 校验（在落盘前，避免孤儿）；合法则记为会话初始模型，
        # 使后续 run 不传 amend 也能从事件流派生出「当前模型」（T7 #137）。
        # 统一解析点 resolve_selection（catalog + 自定义供应商 fallback）：
        # /api/models 广告的自定义条目必须在这里也能解析，否则 UI 能选、一提交
        # 就 422（feature promise 断裂）。
        initial_model_data: dict[str, Any] = {}
        if amend is not None and amend.model is not None:
            from agent_harness.model.provider_store import ProviderStore

            try:
                store = ProviderStore.for_settings(self._state.settings)
                initial_model = ModelConfig.resolve_selection(
                    self._state.settings, amend.model, store,
                )
            except ConfigError as error:
                raise InvalidDecision(str(error)) from error
            initial_model_data = {
                "provider": initial_model.provider,
                "model_id": amend.model,
            }

        # F15 #234：会话级**权限决策**（档位 + 是否自动批准）也是会话的属性，必须随
        # session/started 落进事件流。续聊路径（resume_and_launch）读不到创建请求，
        # 只能从这里派生——不落盘就等于用户在创建时做的选择从第二轮起静默失效。
        # 不显式声明 → 不写键 → 与历史会话逐字不可区分（与模型初始值同一条规矩：
        # 有才写）。auto_approve 只在「未声明档位」时才成为唯一决策依据（deny 路由），
        # 但同样必须落盘，否则那条路由的"不自动批准"承诺在续聊时消失。
        session_start_data: dict[str, Any] = dict(initial_model_data)
        if permission_mode_explicit:
            session_start_data[SESSION_PERMISSION_MODE_KEY] = permission_mode.value
        if auto_approve_explicit:
            session_start_data[SESSION_AUTO_APPROVE_KEY] = auto_approve

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
            steer_source=self._state.message_queues,
            **amend_kwargs(amend),
        )
        session = Session.start(
            self._state.store, session_id=session_id,
            started_data=session_start_data or None,
            # WS-1 #151：会话侧 cwd 锚由**创建者**赋予（这里），与 build_runtime
            # 写进映射表的 workspace_root 是同一路径、同一套规范化（AC5）。
            #
            # AC6「会话先落盘、之后才 attach 到项目」的两半：① cwd 与会话同在第一
            # 条事件里，所以"存在但没有 cwd 的会话"结构上不可能（这一半已成立）；
            # ② attach 本身是 WS-2 的 attachSession，尚不存在。上面的 build_runtime
            # 先写了 sandbox 映射表，但那张表不是 attach（它是 sandbox 生命周期
            # 记录，R6-6 刻意让它先于会话落盘，避免组装失败留下孤儿 session）——
            # WS-2 的 attachSession 必须自己按会话 header 的规范 cwd 校验，不得
            # 反过来信任映射表。
            cwd=workspace,
        )

        # WS-2 / #152 AC5/AC6/AC16：会话**落盘之后**才 attach 到项目（顺序即 AC6 的
        # "先建会话再 attach"）。只对**显式选定了目录**的会话做：未命名/未给 cwd 时目录是
        # workspaces_root/<session_id>（"用户没选项目"的实现痕迹），把它注册成项目会
        # 给每个未命名会话凭空造出一个项目（ADR-0025 D6）。
        # 项目实体由 create 幂等建立（同路径的多会话共享同一项目）。
        # #169：`cwd` 走同一条路（title=None → `Workspace.default_title` 取目录末段名）。
        explicit_dir = workspace_name is not None or cwd is not None
        if explicit_dir and self._state.workspace_index is not None:
            await self._state.workspace_index.create(workspace, title=workspace_name)
            await self._state.workspace_index.attach_session(session_id)

        # 交互式审批：把真实 session 注入 callback 闭包
        if interactive and isinstance(approval_callback, _InteractiveCallbackHolder):
            approval_callback.bind_session(session)

        if not launch:
            # #204：只建会话。session/started 与元数据已照常落盘（上面的路径
            # 完全共享）；不调 RunManager.launch——没有 run 就没有订阅句柄，
            # None 是诚实的"不存在"，调用方据此不组 SSE。
            # review 修复：interactive 路径在 _build_approval_callback 里已把
            # 队列登记进 approval_queues，而没有 run 就没有终结回调来 GC 它
            # （登记点永远等不到 pop）——只建路径当场撤掉登记，队列不泄漏。
            if interactive:
                self._state.approval_queues.pop(session_id, None)
            return LaunchResult(session=session, run=None, subscriber=None)

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

        # T8 #138：崩溃遗留（悬空 tool_call / 无终态 run）必须走 Ledger
        # reconcile——Session.resume 的 dangling 兜底对 RUNNING/UNKNOWN 的
        # tool_call 一律伪造「结果未知」，等于替高风险副作用猜结论
        # （不变量 #13/#14）。recover() 是唯一恢复入口：UNKNOWN 无 callback
        # 时安全拒绝（→ 409），确定性项精确回填后再 load 继续跑。
        #
        # 不在这里 catch ValueError → SessionNotFound（BUG-011 移除）：会话存在性
        # 已在上方 `if not existing` 判定过，此后的异常都不是「不存在」。旧映射把
        # `Session.load` 的 seq 冲突（数据完整性）一律谎报成 404，客户端只能显示
        # `Send failed: 404`。现在 load/resume 抛类型化领域异常（SeqConflict /
        # SessionNotFound），由端点各自的 except 元组精确翻译。
        if detect_dangling(existing) or detect_unterminated_runs(existing):
            await self.recover(session_id)
            session = Session.load(
                self._state.store,
                session_id,
                workspace_registry=self._state.workspace_registry,
            )
        else:
            session = Session.resume(
                self._state.store,
                session_id,
                workspace_registry=self._state.workspace_registry,
            )

        workspace = self._state.workspaces_root / session_id
        workspace.mkdir(parents=True, exist_ok=True)
        _, wiring = await self._state.get_wiring()
        await self._state.ensure_stores()

        # F15 #234：权限决策（档位 + 是否自动批准）从事件流派生（创建时显式声明过才
        # 作数）。续聊路径没有创建请求可读，硬编码默认值就等于"用户的选择只管第一条
        # 消息"——所以在续聊入口把会话级决策与审批回调一起重建，和 create_and_launch
        # 同一套装配。
        #
        # 未声明（历史会话 / 用户没选）→ 行为逐字不变：workspace-write + None
        # （build_runtime 对 None 的语义 = 安全默认 auto-approve）。
        declared_mode = _declared_permission_mode(existing)
        declared_auto = _declared_auto_approve(existing)
        interactive = False
        approval_callback: ApprovalCallback | None | _InteractiveCallbackHolder
        if declared_mode is None:
            permission_mode = PermissionPolicy.WORKSPACE_WRITE
            if declared_auto is False:
                # deny 路由（创建时声明了"不自动批准"且未选档位）也只能从事件流复原：
                # 它同样不落盘的话，第二条消息起会变成全自动批准——与该路由的承诺相反。
                approval_callback = await self._build_approval_callback(
                    interactive=False,
                    auto_approve_explicit=True,
                    permission_mode_explicit=False,
                    auto_approve=False,
                    session_id=session_id,
                )
            else:
                approval_callback = None
        else:
            permission_mode = declared_mode
            # danger-full-access 是"无需审批"档，与创建路径同判据（那边的 interactive
            # 同样排除它），不要在这里发明第二套判定。
            interactive = declared_mode is not PermissionPolicy.DANGER_FULL_ACCESS
            approval_callback = await self._build_approval_callback(
                interactive=interactive,
                # 续聊请求体不承载这两个创建期标志；interactive 分支在前，二者不参与
                # 判定（非 interactive 时 permission_mode_explicit=True 会让 deny 分支
                # 也不成立 → None，即 danger 档的正确结果）。
                auto_approve_explicit=False,
                permission_mode_explicit=True,
                auto_approve=False,
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
            steer_source=self._state.message_queues,
            **amend_kwargs(amend),
        )
        # 交互式审批：session 已存在，直接绑定（创建路径是"先 holder 后 Session.start"，
        # 这里顺序反过来，但注入点相同）。
        if interactive and isinstance(approval_callback, _InteractiveCallbackHolder):
            approval_callback.bind_session(session)
        run, subscriber = self._state.run_manager.launch(session, runtime, task)
        # run 终结时 GC approval_queue（与创建路径同一条防泄漏路径）。
        if interactive:
            self._attach_approval_queue_gc(run, session_id)
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
        supersedes_seq: int | None = None,
        queue_id: str | None = None,
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

        编辑语义（ADR-0030 §4.4，两个可选字段，默认 None = 现有行为逐字不变）：

          * ``queue_id``——本条内容**替换**某条排队项：先取消旧项
            （``QUEUE_CANCELLED``），再按 mode 投递新内容。排队项的编辑不需要新
            事件类型：cancel(旧) + queued(新) 已足够表达，前端按 queue_id 过滤。
          * ``supersedes_seq``——本条内容**取代** seq 为它的那条 user/message 及其
            整轮（只改投影，不改历史事件，D3）。目标必须是本会话最新一条非注入的
            用户消息，否则 ``SupersedeTargetInvalid``（409）。

        顺序硬约束（§4.4）：**先投递 B、后写 ``message/superseded``**。若在两步之间
        崩溃，最坏结果是"B 未投递、A 已被取代"（该轮为空，用户看得见、可重发）；
        反过来则会得到"A 被取代但 B 从未被接受"——用户无从恢复。

        staged amend 字段透传给 ``resume_and_launch``（idle 分支），
        与 create 路径对齐。默认 None = 当前行为不变。

        不抢断、不改写历史事件（不变量 #3 / #22）；queue 与 steer 的消费由
        run 边界（``on_run_terminal``）/ runtime 循环头驱动，本方法只做注册与
        durable 记录。
        """
        self._validate_session_id(session_id)
        if not await self.has_session(session_id):
            raise SessionNotFound(f"session '{session_id}' not found")

        # 第 1 步：取代校验**先于**取消（校验是纯读，无顺序依赖）——Spec 审查
        # P3：若先 cancel 再发现 supersedes_seq 不合法（409），请求失败却留下了
        # QUEUE_CANCELLED 副作用，用户排队项丢失。校验通过后才取消旧项。
        # queue_id 与 supersedes_seq 可同传（取代一条已落盘消息并取消一条排队项
        # 是两个独立合法动作）；**取代校验在任何分支下都必须跑**——P2 审查缺口：
        # 原 elif 会因同传 queue_id 而跳过校验，第 3 步照样写 superseded 事件，
        # 客户端可借排队项捎带绕过 D8。
        if supersedes_seq is not None:
            await self._assert_supersedable(session_id, supersedes_seq)
        if queue_id is not None:
            await self.cancel_queue(session_id=session_id, queue_id=queue_id)

        active_run = self._state.run_manager.get_active(session_id)

        if mode == "steer":
            if active_run is None:
                raise SteerTargetNotFound(
                    "steer requires an active run; use mode='queue' to enqueue"
                )
            # run_id 可能尚未落盘（launch 与 run/started 之间）——等一小会儿，
            # 拿不到就登记 None：runtime 侧对"run_id 未知"的 steer 一律丢弃
            # （不会注入错误的 run），该请求仍会被终态驱动当普通输入投递。
            steer_req = await self._state.message_queues.register_steer(
                session_id=session_id,
                content=content,
                run_id=await active_run.wait_run_id(),
                created_at=_utc_now_iso(),
            )
            self._append_session_event(
                session_id, STEER_REQUESTED,
                steer_id=steer_req.steer_id,
                content=content,
                run_id=steer_req.run_id,
            )
            result = SendMessageResult(status="steered", steer_request=steer_req)
        elif active_run is None:
            # idle → 直接拉起新 run（同 resume 路径）。
            launched = await self.resume_and_launch(
                session_id=session_id, task=content, max_steps=max_steps,
                amend=amend,
            )
            result = SendMessageResult(
                status="launched",
                session=launched.session,
                run=launched.run,
                subscriber=launched.subscriber,
            )
        else:
            # 活跃 run → 入队（FIFO）+ 写 MESSAGE_QUEUED。
            queued = await self._state.message_queues.enqueue(
                session_id=session_id, content=content, created_at=_utc_now_iso()
            )
            self._append_session_event(
                session_id, MESSAGE_QUEUED,
                queue_id=queued.queue_id,
                content=content,
            )
            result = SendMessageResult(status="queued", queued_message=queued)

        # 第 3 步（§4.4）：B 已经登记成功，现在才宣告 A 被取代。
        if supersedes_seq is not None:
            self._append_session_event(
                session_id, MESSAGE_SUPERSEDED,
                superseded_seq=supersedes_seq,
                carrier=mode,
            )
        return result

    async def _assert_supersedable(self, session_id: str, superseded_seq: int) -> None:
        """校验 supersede 目标（ADR-0030 §4.4 第 1 步 / D8）。不合法 → 409。"""
        events = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        target = next((e for e in events if e.seq == superseded_seq), None)
        if target is None or target.type != USER_MESSAGE:
            raise SupersedeTargetInvalid(
                f"seq {superseded_seq} 不是本会话的 user/message"
            )
        if target.data.get("injected_by"):
            raise SupersedeTargetInvalid(
                f"seq {superseded_seq} 是运行时注入的消息，用户不可编辑"
            )
        if any(
            e.type == MESSAGE_SUPERSEDED and e.data.get("superseded_seq") == superseded_seq
            for e in events
        ):
            # 已被取代：拒绝而不是幂等放行。放行意味着还要再投递一次 B——
            # 那会在排队场景里堆出两条同样的待发送消息，比一个 409 糟得多。
            # 投影侧的幂等（同一 seq 被取代两次以最早一条为准）在 derive_messages
            # 里独立成立，与这里的写侧校验不矛盾（ADR-0030 §4.2.3）。
            raise SupersedeTargetInvalid(f"seq {superseded_seq} 已被取代过")
        # D8：与前端 latestEditableTurn 同一判据——「最新**可编辑**用户消息」
        # 排除 runtime 注入（injected_by）：failure-guard 纠正消息排在最后时，
        # 若把它算进 latest，用户真实末条将永远 409，而前端却给出编辑按钮
        # （两端必有一端在说谎）。注入消息本身不可被取代（上面已拒）。
        latest_user_seq = max(
            (
                e.seq
                for e in events
                if e.type == USER_MESSAGE and not e.data.get("injected_by")
            ),
            default=None,
        )
        if latest_user_seq != superseded_seq:
            raise SupersedeTargetInvalid(
                f"seq {superseded_seq} 不是最新一条用户消息（最新为 {latest_user_seq}）"
            )

    async def cancel_queue(self, *, session_id: str, queue_id: str) -> bool:
        """取消尚未消费的排队消息（PRD §5.3）。

        幂等失败语义：queue_id 已取消 / 已消费 / 不存在 →
        ``QueueItemNotFound``（前端 404）；取消成功 → 写一条
        ``QUEUE_CANCELLED`` SessionEvent，返回 True。

        判据**不只看内存镜像**（D5：事件流是唯一事实）：镜像只是缓存，重启后
        "编辑排队项"请求照常合法——按事件流上的未投递集合校验（同
        `list_undelivered_inputs` 的口径），命中即取消（镜像里没有就只写事件，
        缓存由重建/摘除语义对齐）。
        """
        self._validate_session_id(session_id)
        if not await self.has_session(session_id):
            raise SessionNotFound(f"session '{session_id}' not found")
        cancelled = await self._state.message_queues.cancel(
            session_id=session_id, queue_id=queue_id
        )
        if not cancelled:
            # 镜像未命中：按事件流复核（重启后镜像为空 / 该项从未进过本进程缓存）。
            # 只有事件流判定"确实不存在/已取消/已消费"才 404。
            pending = await self.list_undelivered_inputs(session_id)
            if not any(p.kind == KIND_QUEUE and p.input_id == queue_id for p in pending):
                raise QueueItemNotFound(
                    f"queue item '{queue_id}' not found, already consumed, or cancelled"
                )
        self._append_session_event(session_id, QUEUE_CANCELLED, queue_id=queue_id)
        return True

    # ── 未投递输入：投递驱动（ADR-0030 D4 / D7 / §4.7）────────────────

    async def list_undelivered_inputs(self, session_id: str) -> list[UndeliveredInput]:
        """本会话尚未变成 run 的输入，按到达顺序（seq）——`GET /queue` 与投递共用。

        判据全在事件流上（``derive.undelivered_inputs``），**不读内存队列**：
        只有事件流同时看得见 queue 与 steer 的到达顺序，也只有它跨崩溃存活
        （D5）。内存那份 `MessageQueueManager` 是缓存，不是事实来源。
        """
        self._validate_session_id(session_id)
        if not await self.has_session(session_id):
            raise SessionNotFound(f"session '{session_id}' not found")
        events = await anyio.to_thread.run_sync(
            self._state.store.read_events, session_id
        )
        return undelivered_inputs(events)

    async def deliver_next_undelivered(
        self,
        *,
        session_id: str,
        max_steps: int = 10,
        amend: AmendOptions | None = None,
    ) -> LaunchResult | None:
        """取 1 条未投递输入接力开新 run（ADR-0030 §4.5.5）。None = 无待投递。

        **一次只投递一条**：投递会开新 run，后续输入在下一个终态继续接力——
        否则一次终态会并行开出 N 个 run。

        消费事实在**投递成功之后**才写（``queue/consumed`` / ``steer/applied``）：
        崩溃窗口的两种坏结果不对称——"已消费但没投递"= 消息永久静默丢失
        （用户看不见、无从恢复），"已投递但没记消费"= 重启后再投一次
        （重复回答，用户看得见）。选后者（同 §4.4 的取舍方向）。

        `ActiveRunConflict` 直接上抛：调用方（HTTP flush）翻 409；终态驱动侧
        自己吞掉并记日志——此时输入仍在事件流里，下一个终态会再试（不会丢）。
        """
        pending = await self.list_undelivered_inputs(session_id)
        if not pending:
            return None
        nxt = pending[0]
        launched = await self.resume_and_launch(
            session_id=session_id, task=nxt.content, max_steps=max_steps, amend=amend,
        )
        # run_id 拿不到（超时）不阻塞投递：消费判据是 input_id，run_id 只是归因。
        run_id = await launched.run.wait_run_id()
        if nxt.kind == KIND_QUEUE:
            await self._state.message_queues.take_queue_item(
                session_id=session_id, queue_id=nxt.input_id,
            )
            self._append_session_event(
                session_id, QUEUE_CONSUMED, queue_id=nxt.input_id, run_id=run_id,
            )
        else:
            await self._state.message_queues.take_steer(
                session_id=session_id, steer_id=nxt.input_id,
            )
            self._append_session_event(
                session_id, STEER_APPLIED,
                steer_id=nxt.input_id, applied_seq=None, run_id=run_id,
            )
        return launched

    async def on_run_terminal(self, session_id: str) -> None:
        """run 终态后的唯一驱动点（ADR-0030 D4 / §4.7）——接力投递下一条输入。

        幂等：无待投递输入、或已有别的入口拉起了在途 run 时都是 no-op。后者是
        竞态护栏：用户手动发消息（`/messages` 的 idle 分支）与终态驱动可能同时
        想开 run，先到者赢，另一方不重复开（输入留在事件流里等下一个终态）。

        由 `RunManager` 在 run 收口后调用（Web 层接线）；CLI 未接线时本方法
        不被调用，行为与接线前一致。
        """
        if self._state.run_manager.get_active(session_id) is not None:
            return  # 已有在途 run（用户手动开了）：不要双驱
        try:
            await self.deliver_next_undelivered(session_id=session_id)
        except ActiveRunConflict:
            # 心跳式竞态：另一个入口在同一刻拉起 run。不丢事实——输入仍在事件流，
            # 下一个终态会再次尝试（这里刻意不 requeue：我们从没把它弹出内存镜像，
            # 投递决策读的是事件流，见 deliver_next_undelivered 的说明）。
            logger.info(
                "终态驱动遇在途 run 竞态（session=%s）——输入留在事件流待下次接力",
                session_id,
            )
        except SessionNotFound:
            logger.warning("终态驱动：session=%s 不存在，跳过接力", session_id)

    # ── 取消 ─────────────────────────────────────────────────────────

    async def cancel(self, session_id: str) -> bool:
        """取消在途 run。返回 True 表示已取消，False 表示无在途 run（幂等）。

        session 不存在 → SessionNotFound。
        """
        if not await self.has_session(session_id):
            raise SessionNotFound(f"session '{session_id}' not found")
        return self._state.run_manager.cancel(session_id)

    # ── 归档（#171）──────────────────────────────────────────────────

    async def set_archived(
        self, session_id: str, *, archived: bool, entry_point: str
    ) -> bool:
        """归档 / 取消归档一个会话（#171）：只改列表可见性，不动任何事实。

        **只动 `session_meta.archived` 一列**——事件日志一字不改（spec 03 的 Full
        SessionEvent History 硬约束：系统不许偷删历史），项目账本一个字不动（归档与
        "在哪个项目"是两个正交轴），沙箱工件、checkpoint、operation ledger 也都不碰。
        归档可逆，所以入口层不需要二次确认（对比 ADR-0026 对硬删的要求）。

        三条守卫，顺序即契约（之后是两步写：懒补行 → 审计）：

        1. 422 `InvalidSessionId`：形态非法（路径穿越防线）必须最先，且先于 404
           —— 否则 `../x` 会先变成一个"不存在"。
        2. 404 `SessionNotFound`：与列表**同一条判据**（`read_session_summary` 读得出
           内容），所以"归档得掉"与"看得见"永远一致，不会给出一个列表里没有的 id 的
           假回执。
        3. 409 `ActiveRunConflict`（**只有归档方向**）：有在途 run 时拒绝。判据是
           `RunManager.get_active` 而**不是**硬删用的 `is_busy`——后者多出来的那一半覆盖
           "task 已 done / terminal 旗标未及置位"的 finalizer 窗口，那个窗口对"抽走地面"
           的删除是致命的（ADR-0029 D4），而归档不删任何东西、只写一行标记，故按票面用
           `get_active`。取消归档不成冲突：它只是把行放回列表，任何时候都安全。

        ①②③ 与 `delete_session` 的对应三步逐字同形（同一套判据、同一句文案来源），所以
        抽成 `_require_existing_session`（删除路径还要事件数，故它返回摘要对象）。

        ④ 懒补 `session_meta` 行：那些行只有 lineage 建树 / fork 写 provenance / run 存
        checkpoint 三处会补，普通会话可能根本没有行（AC3 明说"无行 = 未归档"），而
        `SessionMetaStore.set_archived` 对不存在的行**抛 `KeyError`**（那是 store 的
        contract 原文，且被 `tests/storage/test_sqlite_checkpoint_store.py` 钉住——所以
        不能把"自动建行"塞进 store，那是改一个所有调用方共用的契约）⇒ 没有行时先
        `upsert` 一行（`created_at` 与 lineage 建行同一口径）。不这么做，真机上"第一次
        归档一个从没 fork 过的会话"就是 500。
        ⑤ 审计。

        **已知的窄竞态**：④ 是"先读后写"，与并发的硬删交错时理论上会留下一条孤儿
        `session_meta` 行（日志已不在）。本票不加锁——`delete_session` 自身也是无锁的
        （见它的并发说明），且同一窗口在 lineage / checkpoint 两处懒补上本来就有；真要
        收敛得给 Store 加单语句 upsert，属另一票的范围。

        **幂等**：重复归档 / 重复取消都返回目标状态、不报错，且行数恒为 1（重跑只更新
        同一行）。审计每次被接受的请求写一条：被审计的事实是"用户动了这条会话"，不是
        "值变了"——重复归档同样值得留痕。
        """
        # ①②③ 存在性与在途 run（存在性那三步与删除共用，见 `_require_existing_session`）
        await self._require_existing_session(session_id)
        if archived and self._state.run_manager.get_active(session_id) is not None:
            raise ActiveRunConflict(
                f"session '{session_id}' has a run in flight; archive it after it finishes"
            )

        # ④ 懒补行 → 写标记
        meta = await self._state.session_meta_store.get(session_id)
        if meta is None:
            await self._state.session_meta_store.upsert(
                SessionMeta(
                    session_id=session_id,
                    created_at=_utc_now_iso(),
                    archived=archived,
                )
            )
        else:
            await self._state.session_meta_store.set_archived(session_id, archived)

        # ⑤ 审计：归档不是会话真相（不变量 #16/#22），痕迹只落结构化日志。只带 id 与
        #    动作——会话正文（标题 / 任务文本）是用户数据，进日志只是多余的泄露面
        #    （`memory/audit.py` 同款取舍）。
        log_event(
            logger,
            "session_archive",
            f"session archived={archived}: {session_id}",
            session_id=session_id,
            archived=archived,
            entry_point=entry_point,
        )
        return archived

    # ── 硬删（#172 / ADR-0029）────────────────────────────────────────

    async def delete_session(self, session_id: str) -> SessionDeletionStats:
        """用户显式硬删会话：事件日志 + 辅助 DB 行 + harness 自造的沙箱工件。

        与项目**软删除**口径的对照：删会话就是删会话（ADR-0029 D1——无墓碑、无回收站、
        不可恢复），项目只是"用户想留着的目录"，所以两者语义不同是有意的。

        **五条守卫，顺序即契约**（D4；①② 与归档共用 `_require_existing_session`）：

        1. 422 `InvalidSessionId`：形态非法——路径穿越防线，必须最先（后面每条都要用
           `session_id` 拼路径），且必须比 404 先判（否则 `../x` 会先变成一个"不存在"）。
        2. 404 `SessionNotFound`：摘要读不出内容（与 `GET /api/sessions` 同一条判据），
           所以"删得掉"与"看得见"永远一致，不给出一个列表里没有的 id 的删除假回执。
        3. 409 `ActiveRunConflict`：在途 run——用 `RunManager.is_busy` 而不是 `get_active`。
           `get_active` 把"task 已 done / terminal 未及置位"的收尾窗口视为**非**在途
           （那是重连续传的正确判据），而那个窗口恰恰是 finalizer 还在写盘的瞬间：
           删除不许在别人还在写日志时抽走地面。
        4. 409 `ActiveRunConflict`：有挂起审批。审批队列由 run 的 done-callback 回收，
           那个回调可能滞后于 task 收尾（#172 要求这道与上一道**都要**——只靠 ③ 会在
           滞后窗口里把"还有人等着被批准"的会话判成可删）。
        5. 409 `SessionHasChildren`：有 **fork** 子会话（`session_meta`）。
           刻意不级联（用户没选中的子会话不能静默消失）、刻意不 orphan（子会话会带着
           悬空来源链接），由用户先处理子会话。**委派**子会话不在此列（D5）：它是内部
           构造，且边由父日志承载、只是被 lineage 索引进 `session_meta`（下面的补偿步
           读的就是那份索引）——若也拒绝，任何用过子 Agent 的会话将永远删不掉。

        **删除顺序**（D3）：先 DB 行、后文件，每步幂等。列表是**文件系统驱动**的
        （`list_session_ids` 扫 `<root>/<sid>/events.jsonl`），所以"DB 已删、文件还在"是
        唯一可自愈的崩溃窗口——用户再删一次即收敛；反过来（文件先删）会留下无法自愈的
        孤儿 `session_meta` 行（lineage 里的幽灵父节点）。

        其中 DB 段多一步**跨会话的补偿**（D5）：`clear_delegation_parent` 会把"父是被删
        会话的委派子行"的父链接清空——委派子会话不阻止删除，但也不许留下指着死父的悬空
        链接。它动的是**别人**的行，所以行数照实计数并写进审计日志
        （`repaired_delegation_links`），前端响应体里不带它（#172 锁定的形状只有 id /
        deleted / events / detached_from_projects）。

        **删除面是白名单**（D2）：只删 harness 用 `workspace_dir + session_id` 自己拼出的
        `sessions/<sid>/`、`workspaces/<sid>.json`、`workspaces/<sid>/`，**永不读**沙箱映射
        里的 `workspace_root`——ADR-0027 之后它可以是用户的真实仓库（回归锁：
        `test_cwd_session_delete_never_touches_the_user_directory`）。也因此不必怕
        `resume_and_launch` 把 cwd 会话的映射改写成默认目录（见 ADR-0029 D2 末段）：
        删除目标是由 id 算出来的，与映射此刻写着什么无关。

        **并发**：本方法不加锁。每一步都幂等，删除面又是由 id 唯一确定的固定三条路径，
        所以并发的两次删除最坏也只是各自删掉同一批字节的一部分，永远碰不到白名单之外的
        东西；随后再删一次是 404（文件已不在，`test_second_delete_after_success_is_404`）。
        """
        # ①② 形态 + 存在性（要删多少，在删之前就必须知道——删完就只能编了；
        #     这两步与归档共用 `_require_existing_session`）
        summary = await self._require_existing_session(session_id)
        store = self._state.store

        # ③ 在途 run
        if self._state.run_manager.is_busy(session_id):
            raise ActiveRunConflict(
                f"session '{session_id}' has a run in flight; cancel it first"
            )

        # ④ 挂起审批
        queue = self._state.approval_queues.get(session_id)
        if queue is not None and queue.pending_ids():
            raise ActiveRunConflict(
                f"session '{session_id}' has a pending approval; resolve it first"
            )

        # ⑤ fork 子会话（`origin != "delegation"`：NULL 也当 fork 处理——保守拒绝，
        #    宁可让用户先去处理子会话，也不留一个悬空父链接）
        metas = await self._state.session_meta_store.list_all()
        children = [
            m.session_id
            for m in metas
            if m.parent_session_id == session_id and m.origin != "delegation"
        ]
        if children:
            raise SessionHasChildren(
                f"session '{session_id}' is the fork parent of {len(children)} "
                f"session(s): delete the child session(s) first"
            )

        # ⑥ DB 行（先）——四张含 session_id 的表各自清自己的行。跨 Store 级联由调用方
        #    编排，不让任一 Store 隐式拥有别人的写语义（`SessionMetaStore.cleanup` 的原话）。
        #    解除计数用 `detach_session` 的返回值：它修剪的是原始账本，是**实际发生**的
        #    动作；自己再扫一遍 `list()` 只会得到一个近似值，还多一次同步磁盘 I/O。
        detached = await self._state.workspace_index.detach_session(session_id)
        # 委派子行的父链接要一起清：委派子会话放行（D5），但不许留下指着死父的悬空链接
        # （`build_lineage_tree` 会渲染成 `(parent missing)`，而那条边永远无法自愈）。
        repaired_links = await self._state.session_meta_store.clear_delegation_parent(
            session_id
        )
        await self._state.session_meta_store.cleanup(session_id)
        await self._state.checkpoint_store.delete_for_session(session_id)
        await self._state.operation_ledger.delete_for_session(session_id)

        # ⑦ 文件（后）——白名单三条路径；同步磁盘 I/O 一律离开事件循环。
        await anyio.to_thread.run_sync(store.delete_session, session_id)
        await anyio.to_thread.run_sync(
            self._state.workspace_registry.discard_session_artifacts, session_id
        )
        # 本地 artifact 目录（#192）：与 sandbox 工件同一条纪律——只删 harness 用
        # setting + session_id 自己拼出来的路径，**不读映射**（ADR-0029 D2）。配了对象
        # 存储时远端对象不在此列（那些 Provider 没有 delete，记录为已知边界）。
        await anyio.to_thread.run_sync(
            discard_local_artifacts, self._state.settings, session_id
        )

        # ⑧ 进程内残留——排队消息与审批队列都按 session_id 索引，会话没了它们永远等不到
        #    消费者，且会让同 id 重建的会话继承上一世的队列。
        await self._state.message_queues.cleanup(session_id)
        self._state.approval_queues.pop(session_id, None)

        # ⑨ 审计：领域数据里不留墓碑（D1/D7），"它存在过"只在结构化日志里可查。
        #    只带 id 与计数——删除不可撤销，审计要能回答"谁在何时删了哪个会话"，
        #    但不需要（也不该）带走会话内容。`repaired_delegation_links` 是这次删除
        #    **动过的别人的行**（委派子会话的父链接），属于"删除的副作用"里最该被看见的一项。
        log_event(
            logger,
            "session_delete",
            f"session hard-deleted: {session_id}",
            session_id=session_id,
            events=summary.event_count,
            detached_from_projects=detached,
            repaired_delegation_links=repaired_links,
        )
        return SessionDeletionStats(
            session_id=session_id,
            events=summary.event_count,
            detached_from_projects=detached,
        )

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

    async def scan_interrupted(self) -> list[InterruptionScanResult]:
        """进程启动扫描（T8 #138）：无终态 run 补记 ``run/interrupted`` + 强制 reconcile。

        返回被处理的 session 结论（无中断的 session 不出现在结果里）。
        单个 session 失败不抛出——扫描的职责是把全部中断如实标记出来。
        """
        from agent_harness.recovery.scan import scan_interrupted_sessions

        await self._state.ensure_stores()
        results = await scan_interrupted_sessions(
            session_store=self._state.store,
            operation_ledger=self._state.operation_ledger,
            workspace_registry=self._state.workspace_registry,
            database_path=self._state.harness_db,
        )
        return results

    async def rebuild_message_queues(self) -> int:
        """按事件流重建内存队列镜像（ADR-0030 §4.8，D5）。返回重建的会话数。

        **不自动起 run**：进程刚起、没有客户端订阅，起了会被孤儿回收；用户不在场
        时自动跑 token 更不可接受。用户回到会话后前端据 `GET /queue` 显示「待发送 N」，
        点「立即发送」→ `POST /queue/flush`（或直接在会话里发消息，走同一条投递路径）。

        幂等：`restore` 是替换语义，重复执行得到同一集合。跨崩溃存活由事件流保证
        ——`message/queued` 本身就是 durable 事实，"队列还在"不依赖任何内存快照。

        代价与边界：这里对每个会话做一次全量事件读取（`list_session_ids` 的顺序即
        最近修改倒序）。重建只在启动时跑一次，耗时随会话总量线性增长。
        """
        await self._state.ensure_stores()
        session_ids = await anyio.to_thread.run_sync(self._state.store.list_session_ids)
        started = time.monotonic()
        rebuilt = 0
        for session_id in session_ids:
            events = await anyio.to_thread.run_sync(
                self._state.store.read_events, session_id
            )
            if not events:
                continue
            pending = undelivered_inputs(events)
            if not pending:
                continue
            queue_items = [
                QueuedMessage(
                    queue_id=item.input_id,
                    content=item.content,
                    session_id=session_id,
                    created_at=item.created_at,
                )
                for item in pending
                if item.kind == KIND_QUEUE
            ]
            steers = [
                SteerRequest(
                    steer_id=item.input_id,
                    content=item.content,
                    session_id=session_id,
                    run_id=item.run_id,
                    created_at=item.created_at,
                )
                for item in pending
                if item.kind != KIND_QUEUE
            ]
            await self._state.message_queues.restore(
                session_id=session_id, queue_items=queue_items, steers=steers,
            )
            rebuilt += 1
            logger.info(
                "重启重建未投递输入：session=%s queue=%d steer=%d",
                session_id, len(queue_items), len(steers),
            )
        elapsed = time.monotonic() - started
        if session_ids:
            # 启动路径上的线性成本要可见：会话多了之后这里是最可能被感知的慢点。
            logger.info(
                "未投递输入重建完成：扫描 %d 个会话，命中 %d 个，耗时 %.2fs",
                len(session_ids), rebuilt, elapsed,
            )
        return rebuilt

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
        from agent_harness.model.provider_store import ProviderStore

        assert_model_resolvable(
            self._state.settings, target,
            ProviderStore.for_settings(self._state.settings),
        )
        # 在途 run 存在时用它的 Session 聚合追加（seq 不撞号 + listener 实时广播）；
        # 否则只读加载一个聚合。两条路径都只 append，不走 resume。
        #
        # 冲突重试（BUG-011）：非 live 路径的「读快照 → 取号 → append」不是原子的
        # ——并发写者（真机现场是模型项被双击的两个请求）可能在本请求读完之后落盘
        # 同号，此时 store 的 seq 守卫拒写。重新读快照再试即可拿到新号，from_* 也
        # 随之从新快照重算。live 路径由事件循环串行化、且该聚合不可重建，不重试。
        #
        # 存在性与 seq 校验放在**循环内**、append 的 try 之外：重试期间会话可能消失
        # （→404）或日志暴露损坏（→409）。校验抛出的 SeqConflict 是**终态**（日志已
        # 损坏、重读同一文件无用），不能被下面的 except 当成可重试的写时冲突吞掉。
        attempts_left = _WRITE_CONFLICT_ATTEMPTS
        while True:
            attempts_left -= 1
            live = self._live_session(session_id)
            if live is not None:
                return append_model_change(live, target)
            if not existing:
                raise SessionNotFound(f"session '{session_id}' not found")
            validate_event_seq(session_id, existing)  # 判据 owner：session 模块
            try:
                return append_model_change(
                    Session(session_id, self._state.store, existing), target
                )
            except SeqConflict:
                if attempts_left <= 0:
                    raise
                existing = await anyio.to_thread.run_sync(
                    self._state.store.read_events, session_id
                )

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
        # WS-2 / #152：fork child 继承了父的 header cwd（#151 AC4），所以它应当出现在
        # 父所属的项目里。attach 只加入**已注册**的项目：父是未命名会话（其目录未注册）
        # → child 也保持 Ungrouped，与父一致。SubAgent 子会话不走这里（内部子代理
        # 不进项目列表，见 ADR-0025 D6 说明）。
        if self._state.workspace_index is not None:
            await self._state.workspace_index.attach_session(child.session_id)
        return child.session_id

    # ── 内部方法 ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        """委托公开函数 validate_session_id（保持调用方 self._validate_session_id 不变）。"""
        validate_session_id(session_id)

    async def _require_existing_session(self, session_id: str) -> SessionSummaryStats:
        """形态 + 存在性两道守卫，返回摘要；**顺序即契约**。

        1. 422 `InvalidSessionId` 必须最先，且先于 404——否则 `../x` 会先变成一个
           "不存在"，把一次路径穿越说成"这个会话没有"。
        2. 404 `SessionNotFound` 的判据与列表页**同一条**（`read_session_summary` 读得出
           内容），所以"操作得掉"与"看得见"永远一致。

        硬删（`delete_session`）与归档（`set_archived`）共用本方法：共享的是**契约**
        （同一判据、同一句文案、同一顺序），不是"省一次 I/O"——两边都要用返回值
        （删除要事件数）。一处改了另一处不会漂。
        """
        self._validate_session_id(session_id)
        await self._state.ensure_stores()
        summary = await anyio.to_thread.run_sync(
            self._state.store.read_session_summary, session_id
        )
        if summary is None or summary.event_count == 0:
            raise SessionNotFound(f"session '{session_id}' not found")
        return summary

    @staticmethod
    def _resolve_cwd(cwd: str) -> Path:
        """校验并规范化 `cwd`（ADR-0027 / #169 AC1），返回会话的操作目录。

        顺序即契约（PRD §4.1 第 2–4 行）：绝对形态 → 存在 → 是目录。绝对形态必须
        在 `realpath` **之前**判：`os.path.realpath("relative/dir")` 会按**进程当前
        工作目录**解析，把一次用户笔误变成"会话落在服务器启动目录"的静默锚定
        （与 `web/projects.py::_require_absolute_path` 同一类防护，共用
        `sandbox.paths.is_absolute_path`——平台分支只在这里定义一次）。

        形态判定之后用 `os.stat` 而不是 `os.path.exists` / `isdir`：后两者把
        `PermissionError` 之类**吞成 False**，于是一个"存在但读不到"的目录会被报成
        "目录不存在"（不诚实的 4xx 文案）；`os.stat` 让每种 errno 走到自己的分支。
        **不代创建**——路径不存在是用户的输入错误，不是"帮他把目录建出来"的信号
        （只有 `workspace` 名字那条路才由 Harness 创建目录）。
        """
        if not is_absolute_path(cwd):
            raise WorkspacePathInvalid(f"cwd 必须是绝对路径：{cwd!r}")
        if "\x00" in cwd:
            # 必须在 realpath 之前挡：POSIX 的 `realpath` 遇到 NUL 抛 `ValueError`
            # （不是 OSError），会穿透到 500。`_require_absolute_path` 早已有这道闸。
            raise WorkspacePathInvalid(f"cwd 含非法字符（NUL）：{cwd!r}")
        canonical = canonical_workspace_path(cwd)
        try:
            mode = os.stat(canonical).st_mode
        except FileNotFoundError as error:
            raise WorkspacePathInvalid(f"目录不存在：{canonical}") from error
        except PermissionError as error:
            raise WorkspacePathInvalid(f"无权限访问：{canonical}") from error
        except OSError as error:
            # 裸 OSError：非法字符 / 超长路径等（EINVAL / ENAMETOOLONG）。作者可控的
            # 路径换来 500 不诚实——这类 errno 本身就是"你给的路径不可用"。
            raise WorkspacePathInvalid(f"cwd 路径不可用：{canonical}") from error
        if not stat.S_ISDIR(mode):
            raise WorkspacePathInvalid(f"不是目录：{canonical}")
        return Path(canonical)

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
        """构建审批 callback（三种路由，与原 handler 行为完全一致）。

        委托 ``session/approval.py`` 的模块级函数（候选 2）；把 ``self._state``
        的两处依赖显式传入，本方法签名与调用点保持不变。
        """
        return _build_approval_callback_impl(
            interactive=interactive,
            auto_approve_explicit=auto_approve_explicit,
            permission_mode_explicit=permission_mode_explicit,
            auto_approve=auto_approve,
            approval_queues=self._state.approval_queues,
            session_id=session_id,
            approval_timeout_seconds=self._state.settings.approval_timeout_seconds,
        )

    def _attach_approval_queue_gc(self, run: ManagedRun, session_id: str) -> None:
        """run 终结时 GC approval_queue（防长期泄漏）。"""
        _task = run.task

        def _gc_approval_queue(_t):
            self._state.approval_queues.pop(session_id, None)

        if _task is not None:
            _task.add_done_callback(_gc_approval_queue)


# ── 公开符号重导出（候选 2 纯结构重构）────────────────────────────────
# 异常 / 模型切换领域逻辑已移到兄弟模块，这里统一 re-export，
# 使 `from agent_harness.session.service import X` 等既有导入路径全部不变。

__all__ = [
    "ActiveRunConflict",
    "AmendOptions",
    "ApprovalAlreadyResolved",
    "ApprovalDecision",
    "ApprovalQueueMissing",
    "ApprovalRequestMissing",
    "InvalidDecision",
    "InvalidForkBoundary",
    "InvalidSessionId",
    "LaunchResult",
    "ModelChange",
    "ModelTarget",
    "QueueItemNotFound",
    "RecoveryConflict",
    "SendMessageResult",
    "SessionNotFound",
    "SessionService",
    "SessionServiceError",
    "SteerTargetNotFound",
    "StreamReconnectHandle",
    "UnknownModel",
    "WorkspaceNameInvalid",
    "_InteractiveCallbackHolder",
    "append_model_change",
    "assert_model_resolvable",
    "current_model_selection",
    "inherit_parent_model",
    "resolve_model_target",
    "validate_session_id",
]
