"""RunManager：detached-run 生命周期托管（ADR-0016 §2.1，D-A 核心）。

run 与 HTTP 请求生命周期解耦：`POST /api/sessions` 经 launch() 把
`runtime.run_stream` 驱动为独立 asyncio.Task，SSE 响应只是该 run 的一个
订阅者——断连（generator 被取消）只做 unsubscribe，绝不杀 run。

事件流合并（幂等，不变量 #22 友好）：
- durable 事实的唯一来源是 Session.append → session listener 实时捕获
  （含工具执行期间追加的 tool/output_delta）；
- run_stream 的镜像 yield 经 seq 去重合并（listener 先入队 seq=N，镜像
  到达时 seq ≤ 已入队最大 seq → 跳过）；stream-only 信号（model/started）
  无 seq，总是入队；
- 订阅者队列有界（SUBSCRIBER_QUEUE_MAX）：满时丢最旧（客户端据 seq gap
  断线重连，after_seq 重放自愈，规格 02 §16.3）。

孤儿回收：最后一个订阅者离开后启动宽限计时（Settings.run_disconnect_grace_seconds），
到期仍零订阅者 → 取消 run task（取消臂收尾 run/failed(reason=orphaned)）；
新订阅者接入即撤销计时。显式 POST /cancel 与孤儿回收是仅有的两个外部终止路径。

run 终态驱动（ADR-0030 D4）：`_drive` 收口后调注入的 `on_run_terminal(session_id)`
——那是 queue/steer 接力投递的**唯一**触发点（Web / CLI 不各自实现）。回调由
session 层提供（默认 None = 不接力），RunManager 自身不认识 queue 语义。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.memory.types import memory_session_var
from agent_harness.session import RUN_COMPLETED, RUN_FAILED, RUN_STARTED, Session

logger = logging.getLogger("agent_harness.web.runmanager")

#: 订阅者队列上限（帧）：满时丢最旧（seq gap → 客户端重连自愈）。
SUBSCRIBER_QUEUE_MAX = 2000

_DONE = object()  # run 终结 sentinel（入队后订阅者迭代结束）


@dataclass
class Subscriber:
    """一个 SSE 连接对应一个订阅者：有界队列 + 断连移除。"""

    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX))


class ManagedRun:
    """一个在途 run 的托管状态：task + 订阅者集 + 幂等合并游标。"""

    def __init__(self, session: Session, manager: RunManager) -> None:
        self.session = session
        self._manager = manager
        self.task: asyncio.Task | None = None
        self.subscribers: dict[int, Subscriber] = {}
        self._next_subscriber_id = 0
        self._last_enqueued_seq = -1
        self.terminal = False
        self.reap_requested = False
        self._orphan_handle: asyncio.TimerHandle | None = None
        # run/started 落盘后填上（ADR-0030 §4.7）：服务层要用它写
        # `steer/requested.run_id` 与 `queue/consumed.run_id`——那两个字段的语义是
        # "这条输入被哪个 run 收编了"，只有 runtime 自己 append 的 run/started 是
        # 权威来源（launch 时刻还没有 run_id：begin_run 在 run 任务里跑）。
        self.run_id: str | None = None
        # #200：本 run 的 runtime（launch 时填）——context-usage 端点从它的
        # builder 读最近一次 build 快照。launch 之后即有值（launch 同步填）。
        self.runtime: AgentRuntime | None = None

    DONE = _DONE  # 订阅者侧哨兵引用

    # ── 订阅 ──

    def subscribe(self) -> Subscriber:
        """新订阅者接入：撤销孤儿计时。"""
        sub = Subscriber()
        self._next_subscriber_id += 1
        self.subscribers[self._next_subscriber_id] = sub
        self._cancel_orphan_timer()
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        """订阅者离开（断连/正常收尾）：移除；孤儿化则启动宽限计时。"""
        for key, value in list(self.subscribers.items()):
            if value is sub:
                self.subscribers.pop(key, None)
        if not self.subscribers and not self.terminal:
            self._arm_orphan_timer()

    # ── 入队（run task 上下文）──

    def enqueue_agent_event(self, event: AgentEvent) -> None:
        """合并入队：durable 按 seq 幂等（listener 先入、镜像跳过），流式总入队。"""
        if event.seq is not None:
            if event.seq <= self._last_enqueued_seq:
                return
            self._last_enqueued_seq = event.seq
        self._fanout(event)

    def enqueue_session_event(self, event) -> None:
        """listener 通道：SessionEvent → AgentEvent 镜像后合并入队。"""
        from agent_harness.agent.types import to_agent_event

        if event.seq <= self._last_enqueued_seq:
            return
        self._last_enqueued_seq = event.seq
        self._fanout(to_agent_event(event))

    def finish(self) -> None:
        """run task 终结：标记终态、撤销孤儿计时、广播哨兵。

        哨兵必达：满队列丢最旧腾位（review 修复——满队列吞哨兵会让该
        订阅者的 SSE 流永不收尾，直到客户端自行断开）。"""
        self.terminal = True
        self._cancel_orphan_timer()
        for sub in list(self.subscribers.values()):
            try:
                sub.queue.put_nowait(_DONE)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(_DONE)

    @property
    def last_enqueued_seq(self) -> int:
        """已入队的最大 durable seq（重连续传的一致性游标，ADR-0016 §2.3）。

        订阅者注册【后】读取：append 落盘先于 listener 入队，seq ≤ 本值的
        durable 事件此刻必然已在磁盘上——重放读盘 + 队列跳过 seq ≤ 本值，
        与 live 流无缝拼合、零重复。"""
        return self._last_enqueued_seq

    async def wait_run_id(self, timeout: float = 5.0) -> str | None:
        """等 run/started 落盘后取 run_id；超时返回 None（`RUN_ID_WAIT_TIMEOUT`）。

        存在这个等待窗口是因为 `RunManager.launch` 只负责 `create_task`——
        `session.begin_run()` 在 run 任务里跑，run/started 之前用户/系统都拿不到
        run_id。窗口极短（begin_run 紧跟 user 落盘与一次 checkpoint 写），但
        **不能假设它已经结束**：`send_message(mode="steer")` 与终态驱动的
        `queue/consumed` 都发生在 launch 返回后的同一事件循环刻度上。

        轮询而不是 `asyncio.Event`：listener（`_on_session_event`）按契约可在任意
        线程上下文被调用，跨线程 set 事件不安全；读一个属性并 sleep 是安全的。
        超时**不抛错**：拿不到 run_id 只让消费事件少一个字段（判据是 id），
        不该让投递本身失败。
        """
        deadline = time.monotonic() + timeout
        while self.run_id is None:
            if time.monotonic() >= deadline:
                logger.warning(
                    "run/started 在 %.1fs 内未落盘（session=%s）——run_id 未知",
                    timeout, self.session.session_id,
                )
                return None
            await asyncio.sleep(0.01)
        return self.run_id

    # ── 内部 ──

    def _fanout(self, event: AgentEvent) -> None:
        # 队列携带 AgentEvent（领域对象）；SSE 帧渲染由端点侧生成器做——
        # runmanager 不反向依赖 web.app（避免循环导入）。
        for sub in list(self.subscribers.values()):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # 慢订阅者：丢最旧保最新——seq gap 让客户端走重连自愈
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(event)

    def _arm_orphan_timer(self) -> None:
        grace = self._manager.disconnect_grace_seconds
        if grace <= 0:
            return
        loop = asyncio.get_running_loop()
        self._orphan_handle = loop.call_later(
            grace, self._reap_if_orphaned,
        )

    def _cancel_orphan_timer(self) -> None:
        if self._orphan_handle is not None:
            self._orphan_handle.cancel()
            self._orphan_handle = None

    def _reap_if_orphaned(self) -> None:
        if self.subscribers or self.terminal or self.task is None:
            return
        logger.warning(
            "run 孤儿回收（session=%s，零订阅者超过 %.0fs）",
            self.session.session_id, self._manager.disconnect_grace_seconds,
        )
        # 先置位再取消：cancel_reason_supplier 在取消臂收尾时读取（02 §17：
        # 孤儿回收 ≠ 用户取消，reason=orphaned 落 run/failed data）。
        self.reap_requested = True
        self.task.cancel()

    def _on_session_event(self, event) -> None:
        """session listener：任何线程上下文都安全（put_nowait）。"""
        self.enqueue_session_event(event)
        # run/started 是本 run 的归因身份（ADR-0030 §4.7）：只认第一条，后续步事件
        # 的 run_id 是同一个值，重复覆盖没有意义。
        if self.run_id is None and event.type == RUN_STARTED and event.run_id:
            self.run_id = event.run_id
        # 终态事实落盘即视为非在途（02 §17）：checkpoint/memory 回写等收尾
        # await 还会跑一会儿——只等 task.done() 会让 cancel/重连在此窗口
        # 误判"仍在途"（store 已见 run/completed 而 cancel 返回 cancelling）。
        if event.type in (RUN_COMPLETED, RUN_FAILED) and not self.terminal:
            self.terminal = True
            self._cancel_orphan_timer()


class RunManager:
    """在途 run 注册表：launch / subscribe / cancel / 孤儿回收 / 关停。"""

    DONE = _DONE

    def __init__(
        self,
        disconnect_grace_seconds: float = 300.0,
        on_run_terminal: Callable[[str], Awaitable[None]] | None = None,
        on_context_snapshot: Callable[[str, dict, list[dict]], None] | None = None,
    ) -> None:
        self.disconnect_grace_seconds = disconnect_grace_seconds
        # run 终态后的唯一驱动回调（ADR-0030 D4）：接力投递下一条未投递输入。
        # 默认 None ⇒ 测试与既有调用零改动，且 RunManager 不认识 queue 语义
        # （回调由 session 层提供，Web 层只负责"在正确的时刻叫它"）。
        self._on_run_terminal = on_run_terminal
        # #200：run 收口时的看板快照回调（builder 快照 + 工具定义）。默认 None
        # = 不缓存（CLI 等调用方不消费看板）；Web 层在正确的时刻叫它。
        self._on_context_snapshot = on_context_snapshot
        self._runs: dict[str, ManagedRun] = {}
        # 关停中：`aclose()` 取消在途 run 会走 `_drive` 的 finally，而那条路径默认
        # 会触发接力投递——关机时又拉起新 run 显然是错的（进程马上没了，新 run
        # 只会被半个生命周期地拖死）。置位后终态回调直接跳过。
        self._closing = False

    def launch(
        self, session: Session, runtime: AgentRuntime, user_input: str,
    ) -> tuple[ManagedRun, Subscriber]:
        """启动 detached run 并返回（run, 首个订阅者）。

        task 先建、后启动订阅（launch 返回后由调用方订阅）——事件不会丢：
        run 的首批 yield 发生在首个 await 点之后，而订阅者队列在 task 首次
        被调度前就已挂上（同一事件循环内无插队窗口）。
        """
        run = ManagedRun(session, self)
        # #200：runtime 引用存到 ManagedRun——context-usage 端点从在途 run 的
        # builder 读最近一次 build 快照（launch 时刻 builder 还没 build 过，
        # _token_estimate_total=0 是诚实的"未 build"信号）。
        run.runtime = runtime
        self._runs[session.session_id] = run
        run.task = asyncio.create_task(
            self._drive(run, runtime, user_input),
            name=f"agent-run-{session.session_id}",
        )
        subscriber = run.subscribe()
        return run, subscriber

    async def _drive(self, run: ManagedRun, runtime: AgentRuntime,
                     user_input: str) -> None:
        """run task 本体：驱动 run_stream，终结时广播哨兵。"""
        token = memory_session_var.set(run.session.session_id)
        run.session.add_listener(run._on_session_event)

        def cancel_reason() -> str:
            return "orphaned" if run.reap_requested else "cancelled"

        try:
            async for event in runtime.run_stream(
                run.session, user_input, cancel_reason_supplier=cancel_reason,
            ):
                run.enqueue_agent_event(event)
        finally:
            run.session.remove_listener(run._on_session_event)
            memory_session_var.reset(token)
            run.finish()
            # #200：run 收口时把 builder 快照缓存下来（最后 build 是当前事实，
            # run 终结后 get_active=None，端点从缓存读——不重建假 registry）。
            self._capture_context_snapshot(run, runtime)
            await self._notify_run_terminal(run.session.session_id)

    def _capture_context_snapshot(self, run: ManagedRun, runtime: AgentRuntime) -> None:
        """run 收口时缓存 context-usage 快照（#200）。快照在 run 收尾时计算
        （builder 与 session 都还活着）；异常吞掉——缓存失败不得污染 run 终态
        事实（快照只是看板数据，不是运行事实）。"""
        capture = self._on_context_snapshot
        if capture is None:
            return
        builder = runtime._context_builder
        if builder is None:
            return
        try:
            # 终审 P1 修复：收口快照带 skills_tokens——live 端点（app.py）算它，
            # 收口缓存不算的话技能桶静默折进"其他"残差，六个桶在 live 与缓存两个
            # 视图里不一致（run 终结后再开看板是常见路径）。同一份 helper（web 层
            # _skills_provider_tokens 的依赖倒置：RunManager 不认识 app.py，这里
            # 通过 builder 协议取同一文本）。
            from agent_harness.web.context_usage import skills_provider_tokens

            capture(run.session.session_id,
                    builder.usage_snapshot(
                        run.session, skills_tokens=skills_provider_tokens(builder)),
                    runtime.registry.export_model_definitions())
        except Exception:
            logging.getLogger(__name__).debug(
                "context-usage snapshot capture failed for %s",
                run.session.session_id, exc_info=True,
            )

    async def _notify_run_terminal(self, session_id: str) -> None:
        """通知 session 层"这个 run 收口了"（ADR-0030 §4.7 的唯一驱动点）。

        顺序硬约束：**必须在 `run.finish()` 之后**——回调会检查
        `get_active()` 来决定要不要接力，而 finish() 之前本 run 仍被算作在途，
        接力会被自己的守卫挡住（静默不投递）。

        异常一律吞掉并记日志：收尾失败不得污染 run 的终态事实（run/completed
        已经落盘，那是事实；接力失败只是"下一条输入晚一点被投递"，事件流
        仍然记着它）。CancelledError 例外上抛——那是关机 / 取消臂在收口本任务。
        """
        if self._on_run_terminal is None or self._closing:
            return
        try:
            await self._on_run_terminal(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("run 终态驱动失败（session=%s）——未投递输入留待下次", session_id)

    def get_active(self, session_id: str) -> ManagedRun | None:
        """在途 run（未终态）；重连续传接 live 流用。

        task 已 done 但 terminal 旗标未及置位（finally 在途）的收尾窗口
        视为非在途——否则取消/重连会在终态落盘后误判"仍在途"（store 已见
        run/completed 而 cancel 返回 cancelling 的竞态）。"""
        run = self._runs.get(session_id)
        if run is None or run.terminal:
            return None
        if run.task is not None and run.task.done():
            return None
        return run

    def is_busy(self, session_id: str) -> bool:
        """这个会话此刻是否还有"活没干完"的 run——破坏性操作（如硬删，ADR-0029）的前置。

        **与 `get_active` 的区别是刻意的，不是笔误**：`get_active` 把"task 已 done /
        terminal 旗标未及置位（finally 在途）"的收尾窗口**视为非在途**——对取消/重连
        是对的（见它的 docstring），但对"能不能把这个会话的地面抽走"是错的：那个窗口
        正是 finalizer（Checkpoint 落盘 / 记忆回写）还在跑的时刻。

        所以本判据只问「run 存在且它的 task 未 done」：
        - 没有 run → 不忙；
        - task 已 done → 不忙（终态收尾已完成）；
        - task 未 done（含 terminal 已置位的收尾窗口）→ 忙；
        - task 尚未挂上（`launch` 与赋值之间）→ **保守视为忙**。
        """
        run = self._runs.get(session_id)
        if run is None:
            return False
        task = run.task
        return task is None or not task.done()

    def cancel(self, session_id: str) -> bool:
        """显式取消：有在途 run → task.cancel()（取消臂收尾）；否则 False。"""
        run = self.get_active(session_id)
        if run is None or run.task is None:
            return False
        run.task.cancel()
        return True

    async def aclose(self) -> None:
        """应用关停：取消全部在途 run 并等待收尾（幂等）。"""
        self._closing = True  # 先置位：取消引发的终态回调不得再接力开新 run
        for run in list(self._runs.values()):
            if run.task is not None and not run.task.done():
                run.task.cancel()
        tasks = [run.task for run in self._runs.values()
                 if run.task is not None and not run.task.done()]
        for run in self._runs.values():
            run.finish()
        if tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()
