"""MessageQueueManager — 续聊排队与 steer 的内存镜像（T2 / #132，ADR-0030 改造）。

PRD §5.3 / §6：活跃 run 时新消息默认进队列（MESSAGE_QUEUED），
run 结束后自动消费队列开下一轮；用户可选 steer 模式注入当前 run
（STEER_REQUESTED → STEER_APPLIED）。

设计要点：
- **事实源是 SessionEvent 流**（单一事实源 #22）；本模块是那份事实的进程内缓存
  ——事件流说了算，缓存只为性能与并发（见类 docstring）。
- 投递决策（取哪一条、什么顺序）由 `SessionService.deliver_next_undelivered`
  读事件流做，本模块只提供镜像写入/摘除与 runtime 侧的 steer 取用。
- cancel_queue 取消队列项 → QUEUE_CANCELLED 事件（用户动作，保留在列表里）。
- steer 请求先写 STEER_REQUESTED 事件；runtime 在循环头注入后写 STEER_APPLIED
  （注入本身在 runtime 侧，本管理器只负责登记与取用）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

logger = logging.getLogger("agent_harness.session.queue")


@dataclass
class QueuedMessage:
    """一个排队中的续聊消息。"""

    queue_id: str
    content: str
    session_id: str
    created_at: str
    cancelled: bool = False


@dataclass
class SteerRequest:
    """一个 steer 请求（立即注入当前 run）。"""

    steer_id: str
    content: str
    session_id: str
    #: 注入目标 run。理论上恒有值；启动竞态下（run 刚 launch、``run/started``
    #: 尚未落盘）可能为 None——runtime 侧对"run_id 不匹配/未知"的请求一律丢弃
    #: （ADR-0030 §4.3），该请求随后由终态驱动当普通输入投递，不会静默丢失。
    run_id: str | None
    created_at: str


class SteerSource(Protocol):
    """runtime 读取待注入 steer 的端口（ADR-0030 §4.3）。

    刻意只有这一个方法：runtime 不该知道 steer 是怎么被登记的（HTTP / WS /
    内存缓存 / 未来别的载体），只需要在循环头问一句"这个 run 有没有要注入的引导"。
    由 `assembly.build_runtime` 注入 `MessageQueueManager`；缺席（CLI / 大多数
    单测）时 runtime 跳过整段注入，行为逐字不变。
    """

    async def drain_steers(self, session_id: str) -> list[SteerRequest]: ...


class MessageQueueManager:
    """每 session 的消息队列 + steer 请求注册表（**内存镜像**，非事实源）。

    事实源是事件流（不变量 #22）：`message/queued` / `queue/cancelled` /
    `queue/consumed` / `steer/requested` / `steer/applied` 的集合定义了"哪些输入
    还没被投递"（`derive.undelivered_inputs`）。本管理器是那份事实的**进程内缓存**，
    存在的理由是性能与并发：runtime 在循环头要同步地问"现在有没有 steer"，不能
    每轮去读盘。

    因此两侧的职责被刻意分开（ADR-0030 §4.7 实现说明）：
    - **投递决策**（取哪一条、按什么顺序）读事件流——只有它同时看得见 queue 与
      steer 的到达顺序（D7）；
    - **本管理器**提供写入（enqueue / register_steer）、取消（cancel）、runtime
      侧批量取用（drain_steers）与镜像摘除（take_*），以及启动重建（restore）。

    取消/消费只有一条路：终态驱动按 id 摘除镜像项（`take_queue_item` /
    `take_steer`），所以不存在"内存弹出了、事实还在"的静默丢失窗口。
    """

    def __init__(self) -> None:
        # session_id → 有界队列（按 FIFO 消费）
        self._queues: dict[str, list[QueuedMessage]] = {}
        # session_id → steer 请求列表（runtime 在下一步前检查）
        self._steers: dict[str, list[SteerRequest]] = {}
        self._lock = asyncio.Lock()

    async def enqueue(
        self, *, session_id: str, content: str, created_at: str
    ) -> QueuedMessage:
        """把消息放入 session 的队列，返回 QueuedMessage。"""
        queue_id = str(uuid4())
        msg = QueuedMessage(
            queue_id=queue_id,
            content=content,
            session_id=session_id,
            created_at=created_at,
        )
        async with self._lock:
            self._queues.setdefault(session_id, []).append(msg)
        return msg

    async def take_queue_item(self, *, session_id: str, queue_id: str) -> QueuedMessage | None:
        """按 id 摘除一条排队项（已被消费 / 被编辑替换）。

        与 `cancel` 的区别：cancel 是**用户动作**（保留在列表里、标记 cancelled，
        事件流写 `queue/cancelled`）；take 是**镜像同步**（该项的事实已由
        `queue/consumed` 或"被新项替换"表达，缓存里不再需要它）。
        """
        async with self._lock:
            queue = self._queues.get(session_id)
            if not queue:
                return None
            for index, msg in enumerate(queue):
                if msg.queue_id == queue_id:
                    queue.pop(index)
                    if not queue:
                        self._queues.pop(session_id, None)
                    return msg
            return None

    async def cancel(self, *, session_id: str, queue_id: str) -> bool:
        """标记队列项为已取消。返回 True 表示找到并取消。"""
        async with self._lock:
            queue = self._queues.get(session_id)
            if not queue:
                return False
            for msg in queue:
                if msg.queue_id == queue_id and not msg.cancelled:
                    msg.cancelled = True
                    return True
            return False

    async def pending_count(self, session_id: str) -> int:
        """未取消的排队项数量。"""
        async with self._lock:
            queue = self._queues.get(session_id, [])
            return sum(1 for m in queue if not m.cancelled)

    async def list_pending(self, session_id: str) -> list[QueuedMessage]:
        """列出未取消的排队项（只读）。"""
        async with self._lock:
            queue = self._queues.get(session_id, [])
            return [m for m in queue if not m.cancelled]

    async def register_steer(
        self, *, session_id: str, content: str, run_id: str | None, created_at: str
    ) -> SteerRequest:
        """注册一个 steer 请求。runtime 在下一步前检查并注入。"""
        steer_id = str(uuid4())
        req = SteerRequest(
            steer_id=steer_id,
            content=content,
            session_id=session_id,
            run_id=run_id,
            created_at=created_at,
        )
        async with self._lock:
            self._steers.setdefault(session_id, []).append(req)
        return req

    async def take_steer(self, *, session_id: str, steer_id: str) -> SteerRequest | None:
        """按 id 摘除一条 steer（已被降级成新 run / 已注入且需要清缓存）。"""
        async with self._lock:
            steers = self._steers.get(session_id)
            if not steers:
                return None
            for index, req in enumerate(steers):
                if req.steer_id == steer_id:
                    steers.pop(index)
                    if not steers:
                        self._steers.pop(session_id, None)
                    return req
            return None

    async def drain_steers(self, session_id: str) -> list[SteerRequest]:
        """取出并清除 session 的所有待处理 steer 请求（runtime 循环头调用）。"""
        async with self._lock:
            return self._steers.pop(session_id, [])

    async def restore(
        self, *, session_id: str, queue_items: list[QueuedMessage],
        steers: list[SteerRequest],
    ) -> None:
        """用事件流派生的集合**替换**该 session 的镜像（启动重建，ADR-0030 §4.8）。

        替换而非追加：重复执行得到同一结果（幂等），不会把上一轮重建的残留叠加
        进来。重建只在进程启动时跑一次，且**不自动起 run**——刚启动没有订阅者，
        起了会被 orphan 回收，用户也不在场（§4.8 的理由）。
        """
        async with self._lock:
            if queue_items:
                self._queues[session_id] = list(queue_items)
            else:
                self._queues.pop(session_id, None)
            if steers:
                self._steers[session_id] = list(steers)
            else:
                self._steers.pop(session_id, None)

    async def cleanup(self, session_id: str) -> None:
        """session 结束 / GC 时清理队列和 steer 状态。"""
        async with self._lock:
            self._queues.pop(session_id, None)
            self._steers.pop(session_id, None)
