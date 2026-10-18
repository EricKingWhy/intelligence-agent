"""#1305 §7：看板 WS 全量快照广播器（订阅集合 + 250ms leading/trailing 节流）。

## 节流形状：逐行抄 Orca（@ ``de1f6e3d``，MIT）

``useDashboardPopoutBridge.ts``：

* ``:13`` ``const PUBLISH_THROTTLE_MS = 250`` —— 常量原样搬成 ``PUBLISH_THROTTLE_MS``；
* ``:187-209`` ``publishThrottled`` —— 窗口外（``elapsed >= 阈值``）**先清掉**尾推
  定时器再立即发布（leading）；窗口内**只在还没有**尾推定时器时排一个
  ``阈值 - elapsed`` 的定时器（trailing），突发因此合并成一次。

只换了两处（票面「只换触发源和传输层」）：

* ``Date.now()`` → ``loop.time()``、``setTimeout`` → ``loop.call_later``
  （同一套"距上次发布的已过时间"判据，只是时钟换成事件循环单调钟）；
* 传输层从 Electron 的 ``window.api.dashboard.publishSnapshot`` 换成 asyncio 队列，
  触发源从 store 订阅换成 :meth:`BoardBroadcaster.mark_dirty`。

## 线程安全

``_persist_event`` 可能跑在 worker 线程 ⇒ :meth:`mark_dirty` **只**做一次
``loop.call_soon_threadsafe`` 回到事件循环，**不**在调用线程碰任何 asyncio 原语
（``Queue.put_nowait`` / ``call_later`` 都不是线程安全的）。

## 已知上限（ponytail）

快照是**全量**（等同 ``GET /api/sessions`` 不传 ``limit``）。会话数上千时单帧变大；
升级路径 = 复用 #516 的 ``limit``/``offset`` 分页快照或改增量 diff，**均不在本票**。
队列满时丢**最旧**的一帧而不是最新——快照是权威全量，"最新一帧"永远够用。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("agent_harness.web.board")

#: 节流窗口（毫秒）。照抄 Orca ``useDashboardPopoutBridge.ts:13``。
PUBLISH_THROTTLE_MS = 250

#: 单连接待发帧上限。满时丢最旧（见模块 docstring 的"已知上限"）。
#: 取值依据 = **≥ 一个节流窗口内可能堆积的帧数**：推送速率被 250ms 节流封顶在
#: 4 帧/秒，而泵任务（`websocket.py::_pump_board`）只是把帧搬进出站队列、几乎不
#: 停顿 ⇒ 8 帧 ≈ 2 秒消费余量；真满说明对端已卡死 ≥2s，此时丢最旧无损（每帧都是
#: 权威全量快照）。Orca 侧无对应常量，这是本票自定值。
_DEFAULT_QUEUE_MAXSIZE = 8

#: 快照工厂：返回与 ``GET /api/sessions`` 同形的行列表。
SnapshotFactory = Callable[[], Awaitable[list[dict[str, Any]]]]


class BoardBroadcaster:
    """板级快照广播：订阅集合 + 服务端一次性 250ms 节流。

    ``snapshot`` 是**注入的快照工厂**——生产里接 ``GET /api/sessions`` 的同一段
    行映射（``app.py::session_rows``），保证 WS 帧与 REST 行逐字段相等；
    测试里可换成计数替身。本类不认识会话、不认识 HTTP，只做"谁订阅、何时推什么"。
    """

    def __init__(
        self,
        *,
        snapshot: SnapshotFactory,
        throttle_ms: int = PUBLISH_THROTTLE_MS,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._snapshot = snapshot
        self._throttle_s = throttle_ms / 1000.0
        self._queue_maxsize = queue_maxsize
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        #: 事件循环：首次 ``subscribe`` 时绑定（那时必然在循环上）。未绑定 =
        #: 从没有人订阅过 ⇒ ``mark_dirty`` 是空操作。
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_publish_at = 0.0
        self._trailing: asyncio.TimerHandle | None = None
        #: 持有在飞发布 task 的强引用（否则可能被 GC 掉，任务中途消失）。
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def subscriber_count(self) -> int:
        """当前订阅者数（连接关闭 / 退订后必须归零）。"""
        return len(self._subscribers)

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """登记一个订阅者并**尽力**立即推一帧权威全量快照。

        "订阅即得全量"是票面 §7 的语义（前端拿到即可渲染，不必再拉一次 REST）——
        首帧在 ``await`` 返回前已入队，调用方可以内联下发，从而保证"订阅后的第一
        条业务帧就是快照"这条顺序对客户端可见。

        ⚠ **首帧可能缺席**：``_publish`` 按观察者纪律吞掉快照计算失败（只记日志、
        不入队，见其注释）⇒ 调用方**不得**假定队列非空。缺席时不下发首帧，靠后续
        投影更新点的帧补上（``websocket.py::_subscribe_board`` 用的是
        ``suppress(QueueEmpty)``）。不要为了"保证有首帧"在这里再算一次快照——
        那只是把同一个失败点搬到同一个地方。
        """
        loop = asyncio.get_running_loop()
        self._loop = loop
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subscribers.add(queue)
        self._last_publish_at = loop.time()
        await self._publish((queue,))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """摘掉一个订阅者（幂等）。订阅集合是"要不要算快照"的唯一开关。"""
        self._subscribers.discard(queue)

    def mark_dirty(self) -> None:
        """投影更新点调用：板上可能有变化了。**任意线程**可调（含 worker 线程）。

        无订阅者时是**空操作**（票面 §7 硬要求）——连 ``call_soon_threadsafe``
        都不发，免得为没人看的板排一堆循环回调。
        """
        if not self._subscribers:
            return
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._on_dirty)

    # ── 节流（Orca publishThrottled 的逐行对应）────────────────────────────

    def _on_dirty(self) -> None:
        """循环侧入口：leading/trailing 分支（对应 Orca ``:187-209``）。"""
        loop = self._loop
        if loop is None:
            return
        elapsed = loop.time() - self._last_publish_at
        if elapsed >= self._throttle_s:
            # 窗口外：清掉可能残留的尾推定时器，立即发布（leading）。
            if self._trailing is not None:
                self._trailing.cancel()
                self._trailing = None
            self._start_publish()
            return
        # 窗口内：只在还没有尾推定时器时排一个（突发合并成一次尾推）。
        if self._trailing is None:
            self._trailing = loop.call_later(
                self._throttle_s - elapsed, self._on_trailing
            )

    def _on_trailing(self) -> None:
        self._trailing = None
        self._start_publish()

    def _start_publish(self) -> None:
        loop = self._loop
        if loop is None:
            return
        self._last_publish_at = loop.time()
        if not self._subscribers:
            return
        task = loop.create_task(self._publish(tuple(self._subscribers)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish(
        self, targets: tuple[asyncio.Queue[dict[str, Any]], ...]
    ) -> None:
        """算一次快照并投给给定订阅者（快照只算**一次**，不是每个订阅者各算一次）。"""
        try:
            rows = await self._snapshot()
        except Exception:
            # 观察者纪律：算快照失败不得拖垮调用方（append 路径上的观察者同款）。
            logger.exception("看板快照计算失败（订阅者不受影响）")
            return
        frame = {
            "type": "board_snapshot",
            "generated_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "sessions": rows,
        }
        for queue in targets:
            # 算快照期间可能已退订 / 断连：只投给仍是订阅者的那些。
            if queue not in self._subscribers:
                continue
            if queue.full():
                # 慢消费者：丢最旧，保留最新（快照是权威全量）。
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(frame)
