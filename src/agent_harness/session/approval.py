"""交互式审批支撑：callback 构建 + 延迟绑定容器（从 service.py 抽出，候选 2）。

- ``build_approval_callback`` —— 三种路由（交互 / deny / None），行为与
  ``SessionService._build_approval_callback`` 完全一致。
- ``InteractiveCallbackHolder`` —— session 在 callback 创建时尚未存在的延迟绑定容器
  （R6-6 组装顺序：先 runtime 后 Session.start）。

``service.py`` 以 ``_InteractiveCallbackHolder = InteractiveCallbackHolder`` 别名
重新导出，既有的私有名引用与测试导入路径不变。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_harness.session.event import (
    SESSION_STARTED,
    TOOL_APPROVAL_REQUESTED,
    SessionEvent,
)
from agent_harness.tooling.approval import (
    ApprovalCallback,
    ApprovalRequest,
    ApprovalResponse,
    PermissionDecision,
)
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.contract import PermissionPolicy

if TYPE_CHECKING:
    from agent_harness.session.session import Session

logger = logging.getLogger("agent_harness.session.approval")

#: ``session/started`` 里承载会话级权限档的键（F15 #234）。
SESSION_PERMISSION_MODE_KEY = "permission_mode"
#: ``session/started`` 里承载会话级「是否自动批准」声明的键（F15 #234）。
#: 与档位同一个病：创建期决策不落盘，续聊就只能猜（那里 ``None`` = 全自动批准）。
SESSION_AUTO_APPROVE_KEY = "auto_approve"


class InteractiveCallbackHolder:
    """交互式审批 callback 的延迟绑定容器。

    session 在 callback 创建时尚未存在（R6-6 组装顺序：先 runtime 后 Session.start），
    因此用 holder 延迟注入 session，再返回真正的 async callback。

    bind_session 后才可被当作 ApprovalCallback 使用（bind 前调用 raise）。
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
    ``auto_approve_explicit=True, auto_approve=False`` 走 deny 路由
    （``build_approval_callback`` 的第二支），但这条决策此前不落盘 ⇒ 续聊落到
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
    """构建审批 callback（三种路由，与原 handler 行为完全一致）。

    从 ``SessionService._build_approval_callback`` 抽出：把对 ``self._state`` 的
    三处依赖（approval_queues 字典 / settings.approval_timeout_seconds）改为显式
    参数，其余逻辑与分支条件逐字保持。
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
            return ApprovalResponse(
                approved=False, reason="manual approval not yet wired"
            )

        return _deny_callback
    else:
        return None


__all__ = [
    "SESSION_AUTO_APPROVE_KEY",
    "SESSION_PERMISSION_MODE_KEY",
    "InteractiveCallbackHolder",
    "build_approval_callback",
    "declared_auto_approve",
    "declared_permission_mode",
]
