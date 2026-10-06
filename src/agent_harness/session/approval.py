"""交互式审批支撑：callback 构建 + 延迟绑定容器（从 service.py 抽出，候选 2）。

- ``build_approval_callback`` —— 三种路由（交互 / deny / None），行为与
  ``SessionService._build_approval_callback`` 完全一致。
- ``InteractiveCallbackHolder`` —— session 在 callback 创建时尚未存在的延迟绑定容器
  （R6-6 组装顺序：先 runtime 后 Session.start）。

``service.py`` 以 ``_InteractiveCallbackHolder = InteractiveCallbackHolder`` 别名
重新导出，既有的私有名引用与测试导入路径不变。

F18-A（#282）追加：

- ``declared_permission_mode`` / ``declared_auto_approve`` —— 会话**创建时**显式声明
  的档位与 auto_approve（F15 #234）。
- ``effective_permission_mode`` / ``effective_auto_approve`` —— 会话**当下生效**的值：
  最后一次 ``permission/changed`` 胜，否则回落到创建时的声明。
- ``append_permission_change`` —— ``permission/changed`` 的唯一写入口。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from agent_harness.session.event import (
    PERMISSION_CHANGED,
    PERMISSION_GRANTED,
    PERMISSION_RESOLVED,
    PERMISSION_REVOKED,
    SESSION_STARTED,
    TOOL_APPROVAL_REQUESTED,
    SessionEvent,
)
from agent_harness.tooling.approval import (
    APPROVAL_GRANT_TTL_SECONDS,
    ApprovalCallback,
    ApprovalGrant,
    ApprovalIdentity,
    ApprovalRequest,
    ApprovalResponse,
    PermissionDecision,
)
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.contract import PermissionPolicy, ToolPermission

if TYPE_CHECKING:
    from agent_harness.session.session import Session

logger = logging.getLogger("agent_harness.session.approval")

#: ``session/started`` 里承载会话级权限档的键（F15 #234）。
SESSION_PERMISSION_MODE_KEY = "permission_mode"
#: ``session/started`` 里承载会话级「是否自动批准」声明的键（F15 #234）。
#: 与档位同一个病：创建期决策不落盘，续聊就只能猜（那里 ``None`` = 全自动批准）。
SESSION_AUTO_APPROVE_KEY = "auto_approve"
#: ``session/started`` 里承载「默认权限矩阵版本」的键（#358 / W-14）。新会话**恒写**。
PERMISSION_DEFAULTS_VERSION_KEY = "permission_defaults_version"
#: 当前默认权限矩阵版本：#358 D2 翻转后 = 2（v1 = 旧默认 workspace-write + auto-approve，
#: v2 = workspace-write + ask）。续聊据此区分"创建于收紧之前"的旧会话。
PERMISSION_DEFAULTS_VERSION = 2
#: 旧 Session 迁移标记（``permission/changed.data.permission_migration`` 的取值，#358）。
LEGACY_V1_DEFAULTS_MIGRATION = "legacy-v1-defaults"
#: 旧 Session 一次性提示文案（用户向，中文；三面：log / LaunchResult.warnings / 迁移事件）。
LEGACY_V1_DEFAULTS_WARNING = (
    "此会话创建于默认权限收紧（#358）之前，沿用旧默认 workspace-write + 自动批准。"
    "新会话默认为 workspace-write + 逐次询问。可在会话内改档切换。"
)


class InteractiveCallbackHolder:
    """交互式审批 callback 的延迟绑定容器。

    session 在 callback 创建时尚未存在（R6-6 组装顺序：先 runtime 后 Session.start），
    因此用 holder 延迟注入 session，再返回真正的 async callback。

    bind_session 后才可被当作 ApprovalCallback 使用（bind 前调用 raise）。
    """

    def __init__(self, *, queue: PendingApprovalQueue, timeout_seconds: float) -> None:
        self._queue = queue
        self._session: Session | None = None
        self._persisted_approval_ids: set[str] = set()
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
        approval_id = self._queue.register(req, on_resolve=self._persist_resolution)
        allowed_decisions = [
            PermissionDecision.DENY.value,
            PermissionDecision.APPROVE_ONCE.value,
        ]
        if req.approval_key is not None:
            # #526 A2：仅可缓存身份才提供「会话内批准」；Runtime 据此写
            # permission/approval-granted，同身份后续调用命中缓存。
            allowed_decisions.append(PermissionDecision.APPROVE_SESSION.value)
            # #684 Phase 1：同一可缓存身份也提供「以后都允许（持久规则）」——
            # Runtime 收到 APPROVE_POLICY 后按 response.policy_granularity 把
            # 精确规则安装进项目级 approve-policy.json。入口唯一：只能从这次
            # 显式审批创建，不另开创建通道（F21）。
            allowed_decisions.append(PermissionDecision.APPROVE_POLICY.value)
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
        except BaseException:
            # 等审批的一方再也不会回来了（run 取消 / 进程退出 / 其他异常）：durable 事实
            # 仍必须**成对**。否则这条 `tool/approval-requested` 永远等不到结清它的写入方，
            # 而完成闸门谓词 2（`02 §5.4`）读的正是这个配对 —— 一个取消掉的 run 会把整段
            # 会话锁成永不可完成（`#316` 的离线端到端用例复现过）。
            # fail-closed：没有批准就是拒绝；`expire` 同时清掉 pending（取消后迟到的 /approve
            # 拿 409 而不是静默生效）。`expire` 返回 False = 有人先裁决了（与超时分支同一把
            # 尺子：先写入者胜，绝不覆盖既有决策）。
            fallback = ApprovalResponse(
                approved=False,
                reason="审批等待被中断（run 取消或退出），按 fail-closed 拒绝",
                decision=PermissionDecision.DENY,
            )
            settled = (
                fallback
                if self._queue.expire(approval_id, fallback)
                else self._queue.resolved_response(approval_id) or fallback
            )
            try:
                self._persist_resolution(approval_id, settled)
            except Exception:
                # 结清写入自己失败（存储故障）时**不能**顶掉原异常：这条 except 分支
                # 在取消 / 退出的栈上，换掉它会让 runtime 的取消臂不匹配、run 被记成
                # `run/failed`（`02 §17` 要求取消与失败分开）。代价是这条请求在 durable
                # 面仍不成对——记 ERROR 供排查，不静默（ADR-0047 §4 残余 5）。
                logger.exception(
                    "审批中断时的 fail-closed 结清写入失败：approval_id=%s 在事件流里"
                    "仍不成对，完成闸门谓词 2 会继续阻断本会话",
                    approval_id,
                )
            raise
        self._persist_resolution(approval_id, response)
        return response

    def _persist_resolution(
        self, approval_id: str, response: ApprovalResponse,
    ) -> None:
        """Append each approval decision once through the run's live Session."""
        if approval_id in self._persisted_approval_ids:
            return
        if self._session is None:
            raise RuntimeError("interactive callback has no bound Session")
        self._session.append(
            PERMISSION_RESOLVED,
            {
                "approval_id": approval_id,
                "decision": response.decision.value,
                "reason": response.reason,
            },
        )
        self._persisted_approval_ids.add(approval_id)


def declared_permission_mode(events: list[SessionEvent]) -> PermissionPolicy | None:
    """派生会话创建时**显式声明的**权限档；未声明 → None（F15 #234）。

    权限档是会话的属性：创建时定、之后不可变（与 ``cwd`` 同级），所以只认第一条
    ``session/started`` 里的 ``permission_mode`` 键。返回 None 表示"这份日志来自
    未声明档位的会话"（历史会话 / 用户没选），调用方据此保持既有语义
    （``workspace-write`` + 安全默认回调），而不是替用户猜一个更严或更松的档。

    值不可解析（日志被手改）时记 warning 并按未声明处理——不静默改写成某个具体档位。
    """
    for event in events:
        if event.type != SESSION_STARTED:
            continue
        raw = event.data.get(SESSION_PERMISSION_MODE_KEY)
        if raw is None:
            return None
        try:
            return PermissionPolicy(raw)
        except ValueError:
            logger.warning(
                "session/started 的 %s=%r 不是合法权限档，按未声明处理",
                SESSION_PERMISSION_MODE_KEY, raw,
            )
            return None
    return None


def declared_auto_approve(events: list[SessionEvent]) -> bool | None:
    """派生会话创建时**显式声明的** ``auto_approve``；未声明 → None（F15 #234）。

    与 :func:`declared_permission_mode` 同一个病、同一把锁：创建期
    ``auto_approve_explicit=True, auto_approve=False`` 走审批路由（#423 弹审批卡；
    早期版本是 deny 路由），但这条决策此前不落盘 ⇒ 续聊落到
    "未声明"分支（``None`` = 全自动批准），用户勾的"不自动批准"从第二条消息起失效。

    只认第一条 ``session/started`` 里的 ``auto_approve`` 键。值不是 bool（日志被手改）
    时记 warning 并按未声明处理——**不猜**一个更松的值。
    """
    for event in events:
        if event.type != SESSION_STARTED:
            continue
        raw = event.data.get(SESSION_AUTO_APPROVE_KEY)
        if raw is None:
            return None
        if isinstance(raw, bool):
            return raw
        logger.warning(
            "session/started 的 %s=%r 不是 bool，按未声明处理",
            SESSION_AUTO_APPROVE_KEY, raw,
        )
        return None
    return None


def build_approval_callback(
    *,
    interactive: bool,
    auto_approve_explicit: bool,
    permission_mode_explicit: bool,
    auto_approve: bool,
    approval_queues: dict[str, PendingApprovalQueue],
    session_id: str,
    approval_timeout_seconds: float,
) -> ApprovalCallback | None | InteractiveCallbackHolder:
    """构建审批 callback（三种路由；#423 起第二支只剩历史/防御角色）。

    从 ``SessionService._build_approval_callback`` 抽出：把对 ``self._state`` 的
    三处依赖（approval_queues 字典 / settings.approval_timeout_seconds）改为显式
    参数，其余逻辑与分支条件逐字保持。
    #423 之后，web 的创建/续聊路径把「auto_approve=false 未选档位」直接判为
    interactive（第三支不再从这两条路进入）；第二支保留给直接调用本函数的
    历史组合与防御性兜底。
    """
    if interactive:
        queue = PendingApprovalQueue()
        approval_queues[session_id] = queue
        return InteractiveCallbackHolder(
            queue=queue,
            timeout_seconds=approval_timeout_seconds,
        )
    elif (
        auto_approve_explicit
        and not permission_mode_explicit
        and auto_approve is False
    ):
        async def _deny_callback(_req):
            # #423：措辞改为用户向（fail-closed 是行为，不是"没接线"的开发者笔记；
            # issue AC：全库用户可见通道无 "not yet wired" 类开发者文案）。
            return ApprovalResponse(
                approved=False,
                reason="自动批准未开启，且当前会话没有可用的审批通道；已按 fail-closed 拒绝本次工具执行",
            )

        return _deny_callback
    else:
        return None


# ── F18-A（#282）：会话内改权限档 ──────────────────────────────────────────
# 「创建时声明」与「会话内改档」写同一对键，故 ``effective_*`` = ``declared_*`` 之上
# 叠一层「最后一次 changed 胜」。决策与优先级：ADR-0041 §2 D3。


@dataclass(frozen=True)
class PermissionChange:
    """一次权限档切换的结果（F18-A #282）。

    ``permission_mode`` / ``auto_approve`` 是**改后当下生效**的值——HTTP 响应回传它，
    前端按回执对齐（不引入乐观本地状态，见 ``web/src/lib/api.ts``）。
    """

    permission_mode: PermissionPolicy
    auto_approve: bool


def effective_permission_mode(events: list[SessionEvent]) -> PermissionPolicy | None:
    """派生会话**当下生效**的权限档（F18-A #282；优先级见 ADR-0041 §2 D3）。

    约束：命中一条 ``permission/changed`` 就**不再往下找**——值不可解析时记 warning 并按
    未声明返回 None（不回落 declared、不猜更严或更松的档，与 :func:`declared_permission_mode`
    同一条规矩）。无 ``permission/changed`` 时逐字回落 F15 #234 的既有行为。
    """
    for event in reversed(events):
        if event.type != PERMISSION_CHANGED:
            continue
        raw = event.data.get(SESSION_PERMISSION_MODE_KEY)
        try:
            return PermissionPolicy(raw)
        except ValueError:
            logger.warning(
                "permission/changed 的 %s=%r 不是合法权限档，按未声明处理",
                SESSION_PERMISSION_MODE_KEY, raw,
            )
            return None
    return declared_permission_mode(events)


def effective_auto_approve(events: list[SessionEvent]) -> bool | None:
    """派生会话**当下生效**的 ``auto_approve``（F18-A #282）。规则同
    :func:`effective_permission_mode`；只认 bool，其余按未声明处理。"""
    for event in reversed(events):
        if event.type != PERMISSION_CHANGED:
            continue
        raw = event.data.get(SESSION_AUTO_APPROVE_KEY)
        if isinstance(raw, bool):
            return raw
        logger.warning(
            "permission/changed 的 %s=%r 不是 bool，按未声明处理",
            SESSION_AUTO_APPROVE_KEY, raw,
        )
        return None
    return declared_auto_approve(events)


def unresolved_approval_ids(events: list[SessionEvent]) -> list[str]:
    """会话里**已请求但没有裁决**的 ``approval_id``（`02 §5.4` 第 2 条的判据，T8 #316）。

    配对键是 ``approval_id``（两个写入者都在这个键上落事件）：``tool/approval-requested``
    由交互式 callback 在**等待决策之前**落盘，``permission/resolved`` 在决策（或
    fail-closed 超时）之后落盘——所以"有 requested 无 resolved"恰好是"这次审批还没
    结论"，与 `PendingApprovalQueue` 的进程内视图同源同义（队列本身不进 SessionEvent，
    不变量 #4）。

    按事件出现顺序返回（可复现），同一 id 多条 requested 只算一次；缺 ``approval_id``
    的事件跳过（腐烂数据只让这一项失去判据，不抛错——与 `declared_permission_mode`
    同一条纪律）。
    """
    requested: dict[str, None] = {}
    resolved: set[str] = set()
    for event in events:
        approval_id = event.data.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            continue
        if event.type == TOOL_APPROVAL_REQUESTED:
            requested.setdefault(approval_id, None)
        elif event.type == PERMISSION_RESOLVED:
            resolved.add(approval_id)
    return [approval_id for approval_id in requested if approval_id not in resolved]


class ApprovalOutcome(str, Enum):
    """单个 tool_call 的 durable 审批结局（#566 封闭词表）。

    状态值封闭：消费方（恢复合成 / resume 修复）只认这四个枚举值，不给
    自由文本漂移空间（票面 §1.3 的 enum 判据）。
    """

    NOT_REQUESTED = "not_requested"
    UNRESOLVED = "unresolved"
    APPROVED = "approved"
    DENIED = "denied"


#: 接纳放行的决策族（per-call scoping 只兑现 approve_once，其余留给后续批次）。
_APPROVE_DECISIONS = frozenset(
    {
        PermissionDecision.APPROVE_ONCE.value,
        PermissionDecision.APPROVE_SESSION.value,
        PermissionDecision.APPROVE_POLICY.value,
    }
)


def approval_outcome_for_call(
    events: list[SessionEvent], tool_call_id: str
) -> ApprovalOutcome:
    """读 durable 流上**该 tool_call** 的审批结局（配对键 ``approval_id``）。

    与 `unresolved_approval_ids` 同一把尺子（requested / resolved 同键配对），
    只是把视角从 approval_id 换到 tool_call_id。执行域顺序是审批闸门 →
    接纳点（Ledger PENDING，`04 §9.1`）→ execute：「审批未通过 / 已批准 /
    未请求审批」都说明控制流停在**接纳点之前**——与「Ledger 无账行」组合
    即可证明工具从未运行（#566 的证明结构，不是"缺 tool/result 就推定"）。

    本函数只产审批侧一半的真相；「Ledger 有没有行」由调用方（持有 Ledger 的
    恢复层）判定——没有账面的读数（如裸 `Session.resume`）不得据此断言未执行。
    """
    requested_ids: list[str] = []
    decisions: dict[str, str] = {}
    for event in events:
        approval_id = event.data.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            continue
        if event.type == TOOL_APPROVAL_REQUESTED:
            if event.data.get("tool_call_id") == tool_call_id:
                requested_ids.append(approval_id)
        elif event.type == PERMISSION_RESOLVED:
            decisions[approval_id] = str(event.data.get("decision", ""))
    if not requested_ids:
        return ApprovalOutcome.NOT_REQUESTED
    for requested_id in requested_ids:
        if requested_id not in decisions:
            return ApprovalOutcome.UNRESOLVED
    decision = decisions[requested_ids[-1]]
    if decision in _APPROVE_DECISIONS:
        return ApprovalOutcome.APPROVED
    if decision == PermissionDecision.DENY.value:
        return ApprovalOutcome.DENIED
    # 未知决策值：不能证明批准放行过 → 按未结清处理（fail-closed 诚实）。
    return ApprovalOutcome.UNRESOLVED


def append_permission_change(session: Session, change: PermissionChange) -> PermissionChange:
    """追加 ``permission/changed``——PERMISSION_CHANGED 的**唯一**写入口（F18-A #282）。

    data 形状（只写改后当前值、无 from/to）见 ADR-0041 §2 D2。走 ``Session.append``
    ——不产生 resume 副作用（不变量 #7）。
    """
    session.append(
        PERMISSION_CHANGED,
        {
            SESSION_PERMISSION_MODE_KEY: change.permission_mode.value,
            SESSION_AUTO_APPROVE_KEY: change.auto_approve,
        },
    )
    return change


def append_legacy_defaults_migration(session: Session) -> None:
    """追加旧 Session 的默认权限迁移事件（#358 / W-14；§3.6）。

    这条 ``permission/changed`` 同时承担两件事：① 让 ``effective_*`` 显式化旧值
    （WORKSPACE_WRITE + ``auto_approve=True``）——续聊行为与迁移前逐字一致；② 作为
    "已提示"标记——下次续聊 ``migrated`` 守卫命中即不再提示（幂等）。

    data 里带 ``permission_migration``（判据）与用户向 ``reason``（提示文案）。
    旧默认**永久沿用、不强制翻成 ask**：迁移只加提示与显式化，不改写用户的历史选择。
    """
    session.append(
        PERMISSION_CHANGED,
        {
            SESSION_PERMISSION_MODE_KEY: PermissionPolicy.WORKSPACE_WRITE.value,
            SESSION_AUTO_APPROVE_KEY: True,
            "permission_migration": LEGACY_V1_DEFAULTS_MIGRATION,
            "reason": LEGACY_V1_DEFAULTS_WARNING,
        },
    )




def append_approval_grant(session: Session, grant: ApprovalGrant) -> ApprovalGrant:
    """追加 ``permission/approval-granted``——会话级审批授予的唯一写入口（#526 A2）。

    append-only 审计：把一次人工「会话内批准」的**可复用身份**落成 durable 事件，
    不改变运行时判定；内存投影见 :func:`derive_approval_grants`。

    不变量：data 只承载身份字段与时间戳，不含参数原文——缓存键由 Runtime 从已校验
    参数派生（``tooling.approval.approval_identity``），模型输入永不直接充当 key；
    写入 ``grant.identity.key()``，同一操作恒得同键，供撤回 last-wins。
    """
    identity = grant.identity
    session.append(
        PERMISSION_GRANTED,
        {
            "approval_key": identity.key(),
            "tool_name": identity.tool_name,
            "kind": identity.kind,
            "canonical": identity.canonical,
            "args_hash": identity.args_hash,
            "permission": identity.permission.value,
            "policy_at_approval": identity.policy_at_approval.value,
            "granted_at": grant.granted_at,
            "expires_at": grant.expires_at,
        },
    )
    return grant


def append_approval_revoke(session: Session, approval_key: str) -> None:
    """追加 ``permission/approval-revoked``——会话级审批撤回的唯一写入口（#526 A2）。

    last-wins：同键后续再次授予会覆盖本撤回（投影按事件正序重放）。append-only，
    不产生 resume 副作用（不变量 #7）。
    """
    session.append(
        PERMISSION_REVOKED,
        {"approval_key": approval_key, "revoked_at": time.time()},
    )


def derive_approval_grants(events: list[SessionEvent]) -> dict[str, ApprovalGrant]:
    """正序重放事件流，投影出**内存态**的会话级审批授予字典（#526 A2）。

    ``permission/approval-granted`` → 以 ``approval_key`` 为键重建
    :class:`ApprovalGrant`；``permission/approval-revoked`` → 删除该键（last-wins，
    后写覆盖先写，与事件顺序一致）。

    不变量 / 纪律：durable 事件才是真相，本 dict 只是投影，销毁无副作用。data 缺字段
    或不可解析时跳过该事件并 ``logger.warning``——坏数据不猜（与
    :func:`effective_permission_mode` 同一条尺子）。TTL / policy / permission 上下界
    不在此预筛，交由 ``grant_valid`` 在使用时判定。
    """
    grants: dict[str, ApprovalGrant] = {}
    for event in events:
        if event.type == PERMISSION_REVOKED:
            approval_key = event.data.get("approval_key")
            if isinstance(approval_key, str) and approval_key:
                grants.pop(approval_key, None)
            continue
        if event.type != PERMISSION_GRANTED:
            continue
        data = event.data
        approval_key = data.get("approval_key")
        try:
            if not isinstance(approval_key, str) or not approval_key:
                raise ValueError("approval_key 缺失或非字符串")
            tool_name = data["tool_name"]
            kind = data["kind"]
            canonical = data["canonical"]
            args_hash = data["args_hash"]
            permission = data["permission"]
            policy_at_approval = data["policy_at_approval"]
            if not all(
                isinstance(v, str) and v
                for v in (tool_name, kind, canonical, args_hash,
                          permission, policy_at_approval)
            ):
                raise ValueError("身份字段缺失或非字符串")
            grant = ApprovalGrant(
                identity=ApprovalIdentity(
                    tool_name=tool_name,
                    kind=kind,
                    canonical=canonical,
                    args_hash=args_hash,
                    permission=ToolPermission(permission),
                    policy_at_approval=PermissionPolicy(policy_at_approval),
                ),
                expires_at=float(data["expires_at"]),
                granted_at=float(data.get("granted_at") or 0.0),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "permission/approval-granted 事件 data 不可解析，跳过：approval_key=%r",
                approval_key,
            )
            continue
        grants[approval_key] = grant
    return grants


def revoked_keys(events: list[SessionEvent]) -> set[str]:
    """返回事件流里所有被撤回的 ``approval_key`` 集合（#526 A2）。

    只读投影，供调用方对账 / 排查；不含顺序与时间语义（last-wins 归
    :func:`derive_approval_grants`）。缺 ``approval_key`` 的撤回事件跳过。
    """
    keys: set[str] = set()
    for event in events:
        if event.type != PERMISSION_REVOKED:
            continue
        approval_key = event.data.get("approval_key")
        if isinstance(approval_key, str) and approval_key:
            keys.add(approval_key)
    return keys


__all__ = [
    "APPROVAL_GRANT_TTL_SECONDS",
    "LEGACY_V1_DEFAULTS_MIGRATION",
    "LEGACY_V1_DEFAULTS_WARNING",
    "PERMISSION_DEFAULTS_VERSION",
    "PERMISSION_DEFAULTS_VERSION_KEY",
    "SESSION_AUTO_APPROVE_KEY",
    "SESSION_PERMISSION_MODE_KEY",
    "InteractiveCallbackHolder",
    "PermissionChange",
    "append_approval_grant",
    "append_approval_revoke",
    "append_legacy_defaults_migration",
    "append_permission_change",
    "build_approval_callback",
    "declared_auto_approve",
    "declared_permission_mode",
    "derive_approval_grants",
    "effective_auto_approve",
    "effective_permission_mode",
    "revoked_keys",
    "unresolved_approval_ids",
]
