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
新订阅者接入即撤销计时。**未登记**（`presence_managed=False`）的 run 到期仍零
订阅者 → 取消 run task（取消臂收尾 run/failed(reason=orphaned)）；**已登记**的
run 到期只记日志、继续跑（W-12 #356 选项 B，decision 8——零订阅者不是客户端离开
的证据，只有明确退出信号才停；见 `_reap_if_orphaned`）。显式 POST /cancel 与孤儿
回收是仅有的两个外部终止路径。

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
from pathlib import Path
from typing import Any

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.memory.types import memory_session_var
from agent_harness.session import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_STARTED,
    Session,
)

# W-12（#356）：明确退出信号的类型/常量与 quit-inspection 结果（`session/client_exit.py`）。
# 从本模块 re-export——调用方/测试经 `runmanager.CLIENT_EXIT_*` 取属性。
from agent_harness.session.client_exit import (
    CLIENT_EXIT_IGNORED_ALREADY_SETTLED,
    CLIENT_EXIT_IGNORED_NOT_MANAGED,
    CLIENT_EXIT_PAUSED,
    SETTLED_OPERATION_STATES,
    ClientExitError,
    ClientExitOutcome,
    ExitImpact,
)
from agent_harness.session.cwd import session_cwd
from agent_harness.session.progress import (
    ProgressWriteOutcome,
    progress_paths,
    write_progress_file,
)
from agent_harness.storage import OperationState, needs_reconcile

logger = logging.getLogger("agent_harness.session.runmanager")

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
        # #312（R4）：本执行以 `run/paused` 收口——非终态，但**同样不再调度新工作**
        # （`_drive` 的收口处解释为什么不接力）。与 terminal 分开记：terminal 描述
        # "这个 run task 结束了"（决定 get_active 是否把它算在途），paused 描述
        # "逻辑 run 还没完，只是让位给 ceiling"（决定要不要接力投递下一条排队输入）。
        self.paused = False
        self.reap_requested = False
        # W-22（#366）：本 run 是否纳入产品客户端在场协议（launch 时定，单向）。
        # True ⇒ 孤儿回收计时器到期不再取消（那会走 run/failed(orphaned)），而是
        # 置 runtime 的缺席闸门，由循环顶准入点以 run/paused(client_absent) 收口
        # （`02 §5.2.1`）。False（默认）⇒ 旧语义逐字不变（`11 §6.2`：协议只接管
        # 明确登记的 Task；登记入口属 W-12 的注册协议）。
        self.presence_managed = False
        # #550：已有取消请求在途（公开 cancel / 孤儿回收 / 关停任一来源置位）。
        # 重复 cancel 不得再 task.cancel()——第二次注入会把 CancelledError 重新
        # 抛进第一次取消正在进行的收尾臂（shield 不消除 caller 收到的取消），
        # 终态事件因此丢失。
        self.cancel_requested = False
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
        if self.cancel_requested:
            # #550 review（A 轴 P3）：用户取消已在途（取消臂收尾中）——reaper
            # 不得二次注入：终态会被 #550 的运行时侧防线兜住，但收尾时读到的
            # `reap_requested` 会把 reason 翻成 orphaned（实为用户取消），且
            # 违背"重复取消不得再注入"不变量。run 正在收尾，回收也无必要。
            return
        if self.presence_managed:
            # W-12（#356）**选项 B**（design decision 8 / §5.1）：在场管理 run 的
            # 宽限到期**不再置缺席**——"零订阅者"不是客户端离开的证据（一次网络
            # 抖动 / 重连中的短暂断流，与"用户关掉了最后一个客户端"在服务端无法
            # 区分）。只有**明确退出信号**（`signal_client_exit`）才是权威的离开
            # 依据。这里只记日志、继续跑：run 照常推进，直到自然完成、显式
            # cancel、或后续收到明确退出信号。
            #
            # （旧 W-22 行为——宽限到期 `mark_absent()` 交循环顶准入点以
            # run/paused(client_absent) 收口——按其上覆盖的规格漂移声明作废；
            # `02 §5.2.1` 正文尚未同步，修订需用户另批。未登记分支不受影响，
            # 旧 orphaned 取消语义逐字不变。）
            logger.info(
                "产品客户端断线（session=%s，零订阅者超过 %.0fs）——无明确退出信号，"
                "继续跑（选项 B），不置缺席、不暂停",
                self.session.session_id, self._manager.disconnect_grace_seconds,
            )
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
        # #312：暂停（非终态）——不置 terminal（逻辑 run 未终结，get_active 的
        # 语义见其 docstring），只置 paused 供 `_drive` 抑制接力投递。
        if event.type == RUN_PAUSED:
            self.paused = True


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
        # #341：在途 run task 的独立集合——`_runs` 按 session_id 记账，同会话
        # resume 再 launch 会**覆盖**旧条目，而旧 task 的收尾（finalizer 落盘）
        # 可能还在跑；没有这张表，`aclose()` 会漏取消它（256 分支遗留加固，
        # 未随 #248 重构迁移）。
        self._tasks: set[asyncio.Task] = set()
        # `#312`：同会话恢复的 CAS 临界区锁（每会话一把，终身保留——与 `_runs`
        # 同一条"每会话一条、不回收"的口径）。
        #
        # 为什么锁挂在**这里**而不是 `SessionService`：Web 传输层每个请求都新建一个
        # 服务实例（`session_service(app.state.agent)` 在处理器里），锁挂在实例上等于
        # 没有互斥——两个并发 resume 会各自读到同一个 version 然后都启动。RunManager
        # 才是每会话状态的常驻 owner（本字典与 `_runs` 同寿命），所以它同时是这把锁
        # 的正确归属（AC-8：并发恢复只允许一个赢家）。
        self._session_locks: dict[str, asyncio.Lock] = {}
        # 关停中：`aclose()` 取消在途 run 会走 `_drive` 的 finally，而那条路径默认
        # 会触发接力投递——关机时又拉起新 run 显然是错的（进程马上没了，新 run
        # 只会被半个生命周期地拖死）。置位后终态回调直接跳过。
        self._closing = False

    def session_lock(self, session_id: str) -> asyncio.Lock:
        """取得该会话的串行化锁（同会话恢复临界区，`#312` / `#342`）。

        调用方可在恢复计划校验与最终提交期间持有它；最终提交会覆盖 CAS 重验、
        必要的恢复/对账、运行时装配及恢复事件写入，确保 CAS 输家不会先留下恢复事件
        或构造模型。锁内**可以**执行 launch（`#560` 起 `resume_and_launch` 的新任务
        路径刻意如此：锁内 `get_active` 复查 CAS + 装配 + launch，锁只串行化
        "判忙 → 启动"决策点，不跨 run 生命周期——launch 只创建 task 不等待它）；
        但不要把锁扩到整个 run 的收尾/等待。
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def launch(
        self, session: Session, runtime: AgentRuntime, user_input: str | None,
        user_input_metadata: dict[str, Any] | None = None,
        presence_managed: bool = False,
    ) -> tuple[ManagedRun, Subscriber]:
        """启动 detached run 并返回（run, 首个订阅者）。

        task 先建、后启动订阅（launch 返回后由调用方订阅）——事件不会丢：
        run 的首批 yield 发生在首个 await 点之后，而订阅者队列在 task 首次
        被调度前就已挂上（同一事件循环内无插队窗口）。

        ``user_input=None``（`#312` 同 run 续跑，无新任务文本）原样透传：
        由 runtime 决定"不落 user/message"——本层不替它编文案。

        ``presence_managed=True``（W-22 `#366`）：把本 run 纳入产品客户端在场
        协议——登记 runtime 的在场闸门，孤儿回收改走 client_absent 暂停（见
        `_reap_if_orphaned`）。生产装配里今天没有调用方传 True（登记协议与
        30s 宽限配置属 W-12）；默认 False = 旧语义逐字不变。
        """
        # #341：关停窗口内拒绝开新 run——进程马上没了，新 run 只会被半个
        # 生命周期地拖死（与 `_notify_run_terminal` 的 `_closing` 跳同一口径）。
        if self._closing:
            raise RuntimeError("RunManager is shutting down")
        run = ManagedRun(session, self)
        # #200：runtime 引用存到 ManagedRun——context-usage 端点从在途 run 的
        # builder 读最近一次 build 快照（launch 时刻 builder 还没 build 过，
        # _token_estimate_total=0 是诚实的"未 build"信号）。
        run.runtime = runtime
        run.presence_managed = presence_managed
        if presence_managed:
            runtime.client_presence.enroll()
        self._runs[session.session_id] = run
        run.task = asyncio.create_task(
            self._drive(run, runtime, user_input, user_input_metadata),
            name=f"agent-run-{session.session_id}",
        )
        # #341：task 进独立集合——`aclose()` 靠它兜住被 `_runs` 覆盖掉的旧 task。
        self._tasks.add(run.task)
        run.task.add_done_callback(self._tasks.discard)
        subscriber = run.subscribe()
        return run, subscriber

    async def _drive(
        self, run: ManagedRun, runtime: AgentRuntime, user_input: str | None,
        user_input_metadata: dict[str, Any] | None,
    ) -> None:
        """run task 本体：驱动 run_stream，终结时广播哨兵。"""
        token = memory_session_var.set(run.session.session_id)
        run.session.add_listener(run._on_session_event)

        def cancel_reason() -> str:
            return "orphaned" if run.reap_requested else "cancelled"

        try:
            async for event in runtime.run_stream(
                run.session, user_input, cancel_reason_supplier=cancel_reason,
                user_input_metadata=user_input_metadata,
            ):
                run.enqueue_agent_event(event)
        finally:
            run.session.remove_listener(run._on_session_event)
            memory_session_var.reset(token)
            run.finish()
            # #200：run 收口时把 builder 快照缓存下来（最后 build 是当前事实，
            # run 终结后 get_active=None，端点从缓存读——不重建假 registry）。
            self._capture_context_snapshot(run, runtime)
            # 快照取完即释放 runtime 引用：`_runs` 每会话保留一条 ManagedRun 终身，
            # 留着 runtime 就等于把模型客户端 / registry / sandbox 句柄一起钉住
            # （终端 run 的 runtime 再无读者——端点只读在途 run 的）。
            run.runtime = None
            # R4（#312）：暂停不是"这轮干完了"——接力投递会在暂停之上直接开一个
            # **新的 run_id**，而 ceiling 是按逻辑 run 记账的，那等于绕开刚生效的
            # 上限。暂停后的会话只由显式 resume（带 expected_version 的 CAS）唤醒，
            # 未投递输入继续留在队列里。
            if not run.paused:
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
            capture(run.session.session_id,
                    builder.usage_snapshot(run.session),
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
        """显式取消：有在途 run → task.cancel()（取消臂收尾）；否则 False。

        #550 幂等：已有取消在途（本方法重复调用，或孤儿回收/关停已置位）时
        返回 True 但**不再** task.cancel()——取消臂正在收尾时再注入取消会打断
        收尾本身（见 ManagedRun.cancel_requested 注释），终态事件随之丢失。
        """
        run = self.get_active(session_id)
        if run is None or run.task is None:
            return False
        if run.cancel_requested or run.reap_requested:
            return True
        run.cancel_requested = True
        run.task.cancel()
        return True

    # ── W-12（#356）选项 B：明确退出信号 → 安全暂停 ────────────────────────

    async def signal_client_exit(
        self, session_id: str, *, client_id: str,
    ) -> ClientExitOutcome:
        """客户端明确退出信号的服务端唯一入口（选项 B 写侧）。

        权威硬信号：接受即承诺只走 `run/paused(reason=client_absent)`，永不
        `run/failed(orphaned)`（decision 1）。三步顺序固定：只读 inspect → 严格
        W-05 写 → 立即 mark_absent（decision 3）；全程不调 `task.cancel()`
        （decision 5：在途 Tool 跑到自然稳定边界，由既有运行时链收口）。

        幂等（decision 7，判据只读持久化事实 `run.terminal` / `run.paused` /
        `gate.absent`）：
          未登记 run            → `ignored_not_managed`（旧语义逐字不变）
          已终态/已 paused/已缺席 → `ignored_already_settled`（绝不双写）
          正常                  → `paused`（恰好一条 `run/paused`，由既有链收口）

        fail-closed（decision 4）：严格 W-05 写失败抛 `ClientExitError`——run 不被
        置缺席、不被暂停，维持原状继续跑。

        `client_id` 仅用于诊断/日志归因，不构成在场登记（选项 B 不建登记表）。
        """
        # F3（B 轴 P3：并发重复信号）：整段信号处理串行化在本会话锁内——复用 #312 的
        # 每会话锁，不新造锁。第二个并发信号会等待第一个完成，随后在幂等检查看到
        # 已终态 / 已 paused / 已缺席（或已被替换）⇒ `ignored_already_settled`，不双写。
        # **死锁前提**：信号路径内（inspect 的只读账本/队列读、严格写的 to_thread、
        # mark_absent）没有任何 `session_lock` 获取点，当前也无其他持锁调用本方法的
        # 调用方——若未来新增持锁调用点，必须重审此前提（否则自锁死）。
        async with self.session_lock(session_id):
            run = self._runs.get(session_id)
            if run is None or not run.presence_managed:
                return ClientExitOutcome(
                    session_id=session_id,
                    status=CLIENT_EXIT_IGNORED_NOT_MANAGED,
                    run_id=run.run_id if run is not None else None,
                    uncertain=False,
                    impact=None,
                    progress=None,
                    paused_event_seq=None,
                    detail="run 未纳入产品客户端在场协议，信号零副作用（旧语义不变）",
                )

            runtime = run.runtime
            if (
                run.terminal
                or run.paused
                or (runtime is not None and runtime.client_presence.absent)
            ):
                return ClientExitOutcome(
                    session_id=session_id,
                    status=CLIENT_EXIT_IGNORED_ALREADY_SETTLED,
                    run_id=run.run_id,
                    uncertain=False,
                    impact=None,
                    progress=None,
                    paused_event_seq=None,
                    detail=(
                        "run 已终态 / 已 paused / 已置缺席——重复退出信号不双写"
                        "（decision 7）"
                    ),
                )

            # ① 只读 quit-inspection（副作用为零）：先知道"会打断什么"。
            impact = await self.inspect_exit_impact(session_id)
            # ② 严格 W-05 写：失败即抛 ClientExitError（不置缺席、不暂停，decision 4）。
            progress, progress_note = await self._strict_progress_write_for_exit(run)
            # ③ 立即置缺席，不等宽限（decision 3；宽限只服务无信号断线）。
            # 先做**身份校验**：两个 await 间隙里旧 run 可能已被替换（同会话 launch 了
            # 已登记的新 run R2）或已收口。按 session_id 重查会把信号**错靶到新 run**
            # （TOCTOU）——身份不符即不置位、如实报已收口，绝不双写（F1：合并修
            # TOCTOU 错靶 + 旧 `mark_client_absent` 旁路缝；本分支同时覆盖原"置缺席时
            # run 已收口"的竞态守卫，不留两套）。
            current = self._runs.get(session_id)
            if current is not run or run.runtime is None:
                return ClientExitOutcome(
                    session_id=session_id,
                    status=CLIENT_EXIT_IGNORED_ALREADY_SETTLED,
                    run_id=run.run_id,
                    uncertain=impact.uncertain,
                    impact=impact,
                    progress=progress,
                    paused_event_seq=None,
                    detail=(
                        "await 间隙 run 已被替换/收口——不对新 run 置缺席"
                        "（TOCTOU 修复），不双写"
                    ),
                )
            run.runtime.client_presence.mark_absent()

            detail_parts = [
                f"客户端明确退出信号已接受（client_id={client_id}）",
                progress_note,
                *impact.detail,
                (
                    "已立即置缺席；暂停由既有运行时链在稳定边界收口"
                    "（不 task.cancel()，decision 5）"
                ),
                (
                    "paused_event_seq 在信号返回时未知——本信号不等待暂停落盘，"
                    "实际 seq 以随后的 run/paused 事件为准"
                ),
            ]
            return ClientExitOutcome(
                session_id=session_id,
                status=CLIENT_EXIT_PAUSED,
                run_id=run.run_id,
                uncertain=impact.uncertain,
                impact=impact,
                progress=progress,
                paused_event_seq=None,
                detail="；".join(detail_parts),
            )

    async def inspect_exit_impact(self, session_id: str) -> ExitImpact:
        """只读 quit-inspection（decision 2/3/6）：绝不写盘、不改 Ledger / 闸门。

        维度（design §3.2，全部复用既有只读面）：
          - 账本：`executor.operation_ledger.list_for_session` 的 RUNNING 行、非
            settled 行，以及 `needs_reconcile(op)`；子代理维取同账本中 `agent_id`
            与本 run 不同的非终态行（不变量 #18：子代理走同一 ToolExecutor →
            同一账本）。
          - 队列：`runtime._steer_source.pending_count(session_id)`（duck-typing；
            无该属性/方法 → 该维 False，不算读失败）。

        任读失败 ⇒ 该维按偏 busy 计、`uncertain=True`、`detail` 追加原因原文，
        查询照常返回（decision 6）。账本句柄缺席 ⇒ 该维 False（缺信息 ≠ 读失败，
        design §3.2）。
        """
        run = self._runs.get(session_id)
        runtime = run.runtime if run is not None else None

        detail: list[str] = []
        uncertain = False
        has_inflight_tool = False
        has_inflight_child = False
        has_pending_operation = False
        impact_needs_reconcile = False
        has_queued_input = False

        ledger = runtime.executor.operation_ledger if runtime is not None else None
        if ledger is None:
            detail.append(
                "operation_ledger 缺席：退出影响按无该维信息处理（缺信息≠读失败）"
            )
        else:
            try:
                operations = await ledger.list_for_session(session_id)
            except Exception as exc:  # noqa: BLE001 — 只读查询读失败不得让信号失败
                uncertain = True
                has_inflight_tool = True
                has_inflight_child = True
                has_pending_operation = True
                impact_needs_reconcile = True
                detail.append(f"operation_ledger 读取失败：{exc!r}")
            else:
                agent_id = getattr(runtime, "_agent_id", None)
                for operation in operations:
                    if operation.state is OperationState.RUNNING:
                        has_inflight_tool = True
                    if operation.state not in SETTLED_OPERATION_STATES:
                        has_pending_operation = True
                        if agent_id is not None and operation.agent_id != agent_id:
                            has_inflight_child = True
                    if needs_reconcile(operation):
                        impact_needs_reconcile = True
                detail.append(
                    f"operation_ledger：{len(operations)} 行"
                    f"（RUNNING={has_inflight_tool}，未结清={has_pending_operation}，"
                    f"待对账={impact_needs_reconcile}）"
                )

        steer_source = (
            getattr(runtime, "_steer_source", None) if runtime is not None else None
        )
        pending_count = getattr(steer_source, "pending_count", None)
        if steer_source is None:
            detail.append("无 steer_source：排队输入维按无信息处理（该维 False）")
        elif not callable(pending_count):
            detail.append(
                "steer_source 无 pending_count（duck-typing 失败）：该维 False"
            )
        else:
            try:
                count = await pending_count(session_id)
            except Exception as exc:  # noqa: BLE001 — 同上：读失败只标不确定
                uncertain = True
                has_queued_input = True
                detail.append(f"会话队列 pending_count 读取失败：{exc!r}")
            else:
                has_queued_input = count > 0
                detail.append(f"会话队列未投递输入：{count}")

        return ExitImpact(
            session_id=session_id,
            has_inflight_tool=has_inflight_tool,
            has_inflight_child=has_inflight_child,
            has_pending_operation=has_pending_operation,
            needs_reconcile=impact_needs_reconcile,
            has_queued_input=has_queued_input,
            uncertain=uncertain,
            detail=tuple(detail),
        )

    async def _strict_progress_write_for_exit(
        self, run: ManagedRun,
    ) -> tuple[ProgressWriteOutcome | None, str]:
        """退出信号路径上的 W-05 **严格写**（design §4 / decision 4）。

        与常规触发点的 best-effort 不同：有锚点时写不成功就不暂停（fail-closed）。
        两类 N/A **不是失败**（没有可写位置，跳过、不抛错、照常进入 mark_absent）：
        无 cwd 锚、cwd 目录已被外部删除（沿用 `service.refresh_progress_file` 的
        同一守卫，不复活已删目录）。

        返回 `(outcome_or_None, note)`；失败时抛 `ClientExitError`。
        """
        # 快照事件流：strict 写在 `asyncio.to_thread` 里迭代，而 runtime 可能在同一
        # 事件循环里并发 append——对活列表的跨线程迭代不安全，取快照。
        events = tuple(run.session.events)
        if not events:
            return None, "空事件流 → 进度写 N/A（decision 4）"
        cwd = session_cwd(events)
        if not cwd:
            return None, "无 cwd 锚 → 进度写 N/A（不是失败，decision 4）"
        if not Path(cwd).is_dir():
            return None, "cwd 目录不存在 → 进度写 N/A（不复活已删目录，decision 4）"
        try:
            outcome = await asyncio.to_thread(
                write_progress_file, cwd, run.session.session_id, events,
            )
        except Exception as exc:
            # F2（B 轴 P2）：`write_progress_file` 正常契约返回 ProgressWriteOutcome，
            # 但线程内意外异常（如 RuntimeError）也必须收敛——design §4.4 要求严格写
            # 失败**一律**抛 ClientExitError。合成一个 ok=False 的结果如实记录；
            # fail-closed 本身已成立（mark_absent 未到达），这里补的是 contract。
            unexpected = ProgressWriteOutcome(
                ok=False, skipped=False,
                path=progress_paths(cwd, run.session.session_id).markdown,
                source_event_seq=-1, error_kind="unexpected_exception",
                reason=repr(exc),
            )
            raise ClientExitError(
                session_id=run.session.session_id,
                run_id=run.run_id,
                progress=unexpected,
                detail=(
                    f"严格 W-05 写抛非 outcome 异常（{exc!r}）——信号中止，run 维持"
                    "原状继续跑（fail-closed，decision 4）"
                ),
            ) from exc
        if outcome.ok or outcome.skipped:
            return outcome, (
                f"进度严格写通过（ok={outcome.ok}，skipped={outcome.skipped}，"
                f"source_event_seq={outcome.source_event_seq}）"
            )
        raise ClientExitError(
            session_id=run.session.session_id,
            run_id=run.run_id,
            progress=outcome,
            detail=(
                f"严格 W-05 写失败（error_kind={outcome.error_kind}，"
                f"reason={outcome.reason}）——信号中止，run 维持原状继续跑"
                "（fail-closed，decision 4）"
            ),
        )

    async def aclose(self) -> None:
        """应用关停：取消全部在途 run 并等待收尾（幂等）。"""
        self._closing = True  # 先置位：取消引发的终态回调不得再接力开新 run
        # #341：按 `_tasks` 集合取消而非只遍历 `_runs`——同会话 resume 覆盖
        # `_runs` 条目后，旧 task 的收尾仍在跑，只有这张表还引用着它。
        tasks = tuple(task for task in self._tasks if not task.done())
        for task in tasks:
            task.cancel()
        for run in self._runs.values():
            # #550：关停也是取消来源——置位幂等旗标，收尾期间用户 /cancel 不得
            # 再向同一个 task 注入取消。
            run.cancel_requested = True
            run.finish()
        if tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()
        # 锁与 `_runs` 同寿命（构造注释里的口径）：`aclose` 清一张表就清两张，
        # 免得"同一条口径"这句话在关停之后只剩一半是真的。
        self._session_locks.clear()
