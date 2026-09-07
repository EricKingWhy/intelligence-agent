"""PendingApprovalQueue：会话级待审批队列（Phase 5 Composer 切片 C）。

每个 session 一个 queue，挂在 AppState。流程：

  ToolExecutor._check_approval（async）
    → ApprovalCallback（由 assembly 注入——见 build_runtime）
       → queue.register(session_id, request) → approval_id
       → session.append(SessionEvent(type=tool/approval-requested, data={approval_id, ...}))
       → await queue.wait_for(approval_id)（run 在此暂停）
       → 返回 ApprovalResponse（由 /approve 端点经 resolve() 注入）

外部（前端）调 POST /api/sessions/{id}/approve {approval_id, approved, reason?}
  → queue.resolve(approval_id, ApprovalResponse(...))
  → 唤醒上面 wait_for 的 Future

幂等性：approval_id 已 resolved 时 resolve() 返 409（防重复决策）；
未登记的 approval_id → 404。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from agent_harness.tooling.approval import ApprovalRequest, ApprovalResponse


@dataclass
class _Pending:
    """一条待审批请求：approval_id + Future（wait_for 等 resolve 唤醒）。"""

    approval_id: str
    request: ApprovalRequest
    future: asyncio.Future[ApprovalResponse] = field(
        default_factory=lambda: asyncio.get_event_loop().create_future()
    )


class PendingApprovalQueue:
    """单 session 的待审批队列：register / wait_for / resolve。

    线程模型：所有方法必须在同一事件循环（FastAPI 请求 + run task 同循环）。
    """

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}
        self._resolved: dict[str, ApprovalResponse] = {}  # approval_id → response

    def register(self, request: ApprovalRequest) -> str:
        """登记一个待审批请求，返回 approval_id（外部 /approve 引用）。"""
        approval_id = uuid.uuid4().hex
        self._pending[approval_id] = _Pending(approval_id=approval_id, request=request)
        return approval_id

    async def wait_for(self, approval_id: str, timeout: float | None = None) -> ApprovalResponse:
        """async 阻塞等决策：run 的审批 callback 在此暂停，等 /approve resolve 唤醒。"""
        if approval_id not in self._pending:
            # 已被 resolve（early）或外部错误 id——查历史映射，找不到就报 KeyError
            if approval_id in self._resolved:
                return self._resolved[approval_id]
            raise KeyError(approval_id)
        pending = self._pending[approval_id]
        if timeout is not None:
            return await asyncio.wait_for(pending.future, timeout=timeout)
        return await pending.future

    def resolve(self, approval_id: str, response: ApprovalResponse) -> bool:
        """外部 /approve 调用：写入决策，唤醒 wait_for 的 Future。

        返回：
          True → 成功 resolve（pending 存在，第一次解）
          False → approval_id 未找到（404）
        已 resolved → KeyError（409，调用方负责处理）
        """
        if approval_id not in self._pending:
            if approval_id in self._resolved:
                raise KeyError(f"approval_id {approval_id} already resolved")
            return False  # 404
        pending = self._pending.pop(approval_id)
        self._resolved[approval_id] = response
        if not pending.future.done():
            pending.future.set_result(response)
        return True

    def pending_ids(self) -> list[str]:
        """当前待审批 approval_id 列表（诊断 / 前端 polling 用）。"""
        return list(self._pending.keys())

    def request_for(self, approval_id: str) -> ApprovalRequest | None:
        """查询某 approval_id 的原始请求（前端展示审批面板用）。"""
        pending = self._pending.get(approval_id)
        return pending.request if pending else None
