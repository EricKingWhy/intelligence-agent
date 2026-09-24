"""#298 / MEM-V2-2 T7：把 durable job 接到运行时——资格判定 → 幂等入队 → 恢复 → 服务循环。

上游三块（`eligibility` 判该不该跑、`jobs` 存、`executor` 跑）都是纯的或只依赖显式入参。
本模块是它们的**宿主**：给"什么时候跑"和"谁在跑"提供唯一答案，并把 T7 之前刻意留空的
两个装配面（真实模型调用、事件落盘）填成具体实现。

# 顺序：**先入队，再让可见答复流出**（AC10 与 Must Do 的交点）

ticket 同时要求两件看起来互相拉扯的事：

- Must Do："Persist a recoverable memory job **before** user-visible run finalization
  releases ownership"——作业必须在运行时交出这一轮之前落盘；
- Must Do："Do not delay the visible answer until memory formation finishes"——可见答复
  不能等形成跑完。

交点就是**入队与执行分开**：`notify_run_finished` 在终态事件写完之后、`run/completed`
镜像出去之前 **await 一次 `enqueue`**（一次 SQLite INSERT，毫秒级），然后 `_schedule()`
把执行丢进后台任务就返回。于是"落盘"发生在交付之前，而"形成"发生在交付之后。

这不是把一次 await 挪个位置：入队之后进程死掉，job 仍在库里（AC6 的第一个 kill 窗口）。
反过来，若把入队放进后台任务，"还没入队就被杀"会丢掉整轮记忆，而没有任何东西报错。

# 事件切片**只从会话日志重建**（新鲜路径与恢复路径共用同一段代码）

执行一个 job 需要 `run_events`（`build_formation_input` 的必需输入）。它在入队那一刻
就在内存里（调用方手上就有），但本模块**刻意不用它**：每次执行都按
`(session_id, run_id)` 从 `JsonlSessionStore` 重新切一遍。

理由是恢复路径没有内存可用——它只有 job 行。让新鲜路径用内存、恢复路径读日志，等于把
"这一轮的事件是哪些"实现两遍；两遍一旦分叉（切片范围、去重、顺序），差别只在**崩溃之后**
才现形，而那正是最难复现、也最该确定的地方。统一走日志之后，恢复就只是"同一个 job 又跑了
一次"，不需要单独的恢复逻辑（`recover()` 只负责把待办排进服务循环）。

代价是每次执行多一次 JSONL 读。它发生在后台、且只在有 job 时发生，换取的是"崩溃前后的
输入逐字节相同"。

资格判定那一步仍用**调用方手上的内存事件**（`notify_run_finished(events=...)`）：那一步在
交付**之前**，不该为它付一次磁盘读，而它与日志是同一批事实的两种视图（`Session.append`
同步写穿到 store）。两侧分叉的失败方向是 fail closed——重建不出切片时执行器按
`run_events_unavailable` 终结、零写入，不会写进错误的记忆。

# 投影是允许清单，所以这里**不再抄一份排除清单**

`agent/runtime.py` 的 V1 路径用一份 `_MEMORY_EXCLUDED_EVENT_TYPES`（reasoning/*、流式
delta）过滤事件。V2 侧不需要第二份：`projection` 只读四类事件
（`user/message` / `model/completed` / `tool/call` / `tool/result`），是一张**允许清单**。
允许清单比拒绝清单严——新增长出的事件类型默认进不了投影。再抄一份拒绝清单只会多一处
需要跟着事件词表更新的地方，而漏更新的那一次是静默的。

# R11 的两半分别落在哪里

- **按用户串行**在数据库里（`jobs.claim` 的自占用子查询）：放进程内存做会随重启失效，
  而且两个进程会各串各的。
- **全局并发**只能在这里：它是**本进程**在飞 job 数的上限，`asyncio.Semaphore` 就是它。
  默认 4、可由装配层传入（`DEFAULT_MAX_CONCURRENCY`）。

# 服务循环的形状（为什么是"单泵 + 脏标记"而不是"每次事件起一个任务"）

同一时刻只允许一个**泵**在跑：`_pump` 反复 `claim` 直到没有可认领的 job，然后退出。
`notify_run_finished` 只置脏标记——正在跑的泵下一轮会顺手捞走新 job，空闲时才发现标记
则自己再起一个泵。这样不会出现两个泵各认领一半（那会让"这一轮该跑哪几个"变得不确定），
也不会漏掉任何入队。

**但"单泵"不等于"串行"**：泵认领到一个 job 就派发成任务（`_spawn`），由 `Semaphore`
兜住在飞数。所以"同时跑几个"由 R11 的并发上限决定，而"按用户串行"由数据库的 `claim`
决定——两条约束各在自己的层上生效。把派发改成 `await`（写起来更短）会让并发上限静默
退化成 1，而 `Semaphore` 变成永不竞争的死代码；那种实现表面上一样"能跑"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage

from agent_harness.identity import get_identity_context
from agent_harness.memory.v2.eligibility import decide_run_end_eligibility
from agent_harness.memory.v2.executor import (
    DegradedReason,
    MemoryJobEventSink,
    MemoryJobExecutor,
    MemoryJobResult,
    MemoryModelCall,
)
from agent_harness.memory.v2.jobs import (
    MemoryFormationJob,
    MemoryJobStage,
    SqliteMemoryV2JobStore,
)
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model
from agent_harness.session import USER_MESSAGE, Session, SessionEvent
from agent_harness.session.errors import SeqConflict
from agent_harness.session.store import JsonlSessionStore

logger = logging.getLogger(__name__)

#: R11 的全局并发默认值："Default global concurrency is four and configurable."
DEFAULT_MAX_CONCURRENCY = 4

#: 启动期恢复扫描一次最多看多少条待办（`list_recoverable` 的 limit）。
DEFAULT_RECOVERY_LIMIT = 100

#: 泵空闲但库里还有"等 lease 到期"的 job 时，最短睡多久再回来看一眼（秒）。
#: 下限存在的理由：`lease_expires_at` 与当前时刻的差可能已经极近，睡 0 秒会变成热循环。
_MIN_WAKE_SECONDS = 0.05

#: `drain` / `aclose` 等待在途服务循环的上限（秒）。超时即取消——被取消的 job 停在
#: 非终态，下次恢复扫描重跑（AC6 的四个窗口共用同一条论证）。
_DRAIN_TIMEOUT_SECONDS = 30.0

#: 一个会话日志的写者可能不止我们（run 结束之后用户马上又发一条），而 `Session.append`
#: 用的是**构造时**算出的 seq 计数器、store 只**拒写**不修号（BUG-011）。所以发事件前
#: 重读一次按当前盘上最大 seq 重建 `Session`，撞号则重试。次数给 3：每次重试都重读，
#: 连续撞三次意味着这个会话正处于极高的写入速率，那时丢一条观测事件比阻塞更合适。
_SINK_ATTEMPTS = 3


def idempotency_key(run_id: str) -> str:
    """R1 的稳定键：一次合格的终态 run ⇒ 恰好一个 job。

    `run_id` 由 `Session.begin_run` 用 `uuid4()` 生成（全局唯一），所以键只需要把它
    标上本模块的前缀——不需要再拼 tenant/user/session：它们都能从 job 行查回来，
    拼进去只是把同一件事写第二遍，而且给"分隔符出现在 id 里"留了折叠的可能。
    """
    return f"memory-v2:{run_id}"


def _seconds_until_claimable(
    pending: Sequence[MemoryFormationJob], now: datetime,
) -> float | None:
    """库里还有待办时，最早"某条会变得可认领"是多久之后（秒）；不会变则 `None`。

    只会变的一种情形是 **lease 到期**：`jobs.claim` 不收"属主还活着"的行（那是别的
    进程/别的协程在跑），而属主如果已经死了，唯一的解除条件就是时间。

    `None` 只表示"**没有任何租约**会到期"：没有待办，或待办全都没有属主 / 租约字段
    （典型是刚入队、还没被任何人认领的行）——那种情况由"有人认领它 / 终结它"来解除，
    而那件事发生时泵会自己再跑一轮。等时间是白等。

    ⚠ **有属主且租约未到期的行会照常计入延迟**，即使它正被**按用户串行**挡着（挡它的
    是同用户的另一条在途 job，而那条 job 本身可能远未终结）。这不是缺陷：代价只是多
    武装一个无害的定时器——到期那一下 `claim` 会重新判一次，判不过就静静地什么都不做。

    （2026-09-24 T8 两轴审查指出：此处原先写的是"待办被按用户串行挡住 ⇒ 返回 `None`"，
    与实现相反；本段按实测行为改写。）
    """
    delays: list[float] = []
    for job in pending:
        if job.lease_owner is None or job.lease_expires_at is None:
            continue
        try:
            expires = datetime.fromisoformat(job.lease_expires_at)
        except ValueError:
            # 时间戳不可解析（不该发生：写点只有 `_sqlite.stamp`）。当作"等不到"，
            # 不为此把泵挂在一个猜出来的时长上。
            logger.warning("Memory V2 job %s has an unparsable lease expiry", job.job_id)
            continue
        delays.append((expires - now).total_seconds())
    positive = [delay for delay in delays if delay > 0]
    if not positive:
        return None
    return max(min(positive), _MIN_WAKE_SECONDS)


class ChatModelInvoker:
    """`executor.MemoryModelInvoker` 的具体实现：把一次 `MemoryModelCall` 变成一次真实调用。

    三个要点都对应 R10 的一条数字：

    - `max_tokens=call.max_output_tokens`——"4k output tokens per call"。交付前实测过
      这条杠杆真的生效（`ChatOpenAI` 把它改名成 `max_completion_tokens` 放进请求体）；
      `RunnableBinding` 那条路（`model.bind(max_tokens=…)`）读不到，所以必须走调用参数。
    - `asyncio.timeout(call.timeout_seconds)`——"120 seconds per job"，在**调用边界**上
      兑现。超时抛内建 `TimeoutError`，`model.fallback.is_transient_model_error` 认它，
      于是走 R9 的瞬时序列而不是当成非瞬时致命错。
    - 模型按**公开字段**缓存：一次 job 最多 5 次调用，每次 `create_chat_model` 都会重建
      底层 httpx client（新连接池、重做 TLS）。键取 `(provider, model_name, base_url)`，
      不含 key——`ModelConfig.api_key` 是 `SecretStr`，本模块从不读它的明文，也从不打印
      任何 provider / 端点 / 凭据值。
    """

    def __init__(
        self, *, factory: Callable[..., Any] = create_chat_model,
        reasoning_effort: str | None = None,
    ) -> None:
        self._factory = factory
        self._reasoning_effort = reasoning_effort
        self._models: dict[tuple[str, str, str], Any] = {}

    async def __call__(self, call: MemoryModelCall) -> str:
        model = self._model_for(call.model)
        messages = [
            SystemMessage(content=call.system_prompt),
            HumanMessage(content=json.dumps(call.payload, ensure_ascii=False)),
        ]
        async with asyncio.timeout(call.timeout_seconds):
            response = await model.ainvoke(messages, max_tokens=call.max_output_tokens)
        content = getattr(response, "content", None)
        if not isinstance(content, str):
            # 非文本形状（多模态块列表等）不是"内容不合格"而是"provider 没按契约回话"：
            # 归 `provider_error` 而不是 `invalid_model_output`——后者是**解析**失败，
            # 而这里连可以解析的文本都没拿到。两者处置相同（都终态降级、零写入），
            # 但归因分开才看得出是哪一层坏了。
            raise TypeError(
                f"memory model returned non-text content: {type(content).__name__}"
            )
        return content

    def _model_for(self, config: ModelConfig) -> Any:
        key = (config.provider, config.model_name, config.base_url)
        cached = self._models.get(key)
        if cached is None:
            cached = self._factory(config, reasoning_effort=self._reasoning_effort)
            self._models[key] = cached
        return cached


class SessionEventSink:
    """`executor.MemoryJobEventSink` 的具体实现：把脱敏事件写进会话日志。

    **不持有**构造时快照的 `Session`：`Session.append` 用的是它构造时算出的 seq 计数器，
    而 store 对"seq ≤ 已落盘最大 seq"是**拒写**（`SeqConflict`，BUG-011）而不是修号。
    后台 job 跑完时，同一个会话早已可能被新一轮 run 追加过事件——沿用旧快照的计数器会
    撞号，异常被 `executor._emit` 吞掉（那是对的：观测失败不得改写已提交的事实），于是
    `memory/updated` **静默消失**，而 R12 要求它必须出现。

    所以每次都重读日志、按当前盘上最大 seq 重建 `Session` 再写；仍然撞号就重试（另一写者
    抢在我们前面落了盘）。最后一次仍失败时只告警——不抛给调用方，理由同上。
    """

    def __init__(
        self, store: JsonlSessionStore, session_id: str,
        *, attempts: int = _SINK_ATTEMPTS,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._attempts = max(1, attempts)

    def emit(self, event_type: str, data: dict[str, Any], *, run_id: str | None = None) -> None:
        for _ in range(self._attempts):
            try:
                session = Session(
                    self._session_id, self._store,
                    events=self._store.read_events(self._session_id),
                )
                session.append(event_type, data, run_id=run_id)
                return
            except SeqConflict:
                # 另一写者刚抢过这个号：重读一遍再试（不是错误，是重试条件）。
                continue
        logger.warning(
            "Memory V2 %s event could not be appended to session %s after %d attempts",
            event_type, self._session_id, self._attempts,
        )


def run_slice_bounds(
    events: Sequence[SessionEvent], run_id: str,
) -> tuple[int, int] | None:
    """在会话日志里定位这一次 run 的切片（半开区间 `[start, stop)`）；找不到返回 `None`。

    # 为什么不能只按 `run_id` 过滤（T7b 用探针实测后修正）

    `_drive` 的顺序是**先写 user 消息、再 `begin_run`**（`agent/runtime.py`），而写那条
    user 消息时**不传 `run_id`**（`Session.append` 的 `run_id` 默认就是 `None`，见
    `session/session.py` 的签名）——所以**本轮用户发言的 `run_id` 是 `None`**。按
    `run_id == job.run_id` 过滤会把它排除出 `run_events`
    并推进 `history`，而 `projection` 只给 `run_events` 发运行时别名 ⇒ 模型在**结构上**
    引不到用户原话（R6 要求 USER/profile 事实必须有直接用户证据），观测上只表现为
    "自动记忆写不出一条用户事实"。这与 T6b 修的 `event_id` 缺失是**同一类**静默失效：
    用例之所以全绿，是因为 fixture 把 user 消息写成了带 `run_id`——一个产出方到不了的形状。

    # 规则

    以本 run **首个**带 `run_id` 的事件为锚，向前吞掉**紧邻的那一条** `user/message`
    （那一轮的用户输入就在 `begin_run` 之前一行），向后取本 run 的**最后一个**事件。
    得到的区间与 V1 的 `_write_memories(session, arms.memory_event_start)` 语义对齐——
    那边的起点正是 `session.mark()`，即那条 user 消息**之前**。

    刻意取**连续区间**而不是"筛出所有带该 run_id 的事件"：会话逐轮串行，所以区间内的事件
    按定义都属于这一轮；而过滤器会把这一轮里其它 `run_id` 为 `None` 的事件重新丢掉——
    那正是本条修正要防的那个错误，写回过滤器就等于把 bug 换个地方重写一遍。

    # 为什么只吞**一条**而不是"一路往前吞"（T8 两轴审查后收紧）

    初版写成 `while ... == USER_MESSAGE`，理由是"上一轮的末尾是 `run/completed`，会挡住
    它"。那个论证只覆盖了"上一轮正常终结"这一种形态：`_drive` 在 `yield user_event` 与
    `begin_run` 之间被断连时，user 事件**已经落盘**，而 `run/started` 与终态都不会写
    （`_TerminalFinalizer` 对 `run_id is None` 直接 return）⇒ 日志里会出现**连续两条**
    `user/message`，`while` 会把那条孤儿一并吞进本轮（它会进 `current_run`、拿到别名，
    于是可以被当作本轮证据）。改成 `if` 之后，生产里两者在**合法输入上等价**
    （`begin_run` 紧邻 user 消息，且 `checkpoint/saved` 明确不写会话事件、不会插进中间），
    而孤儿形态下不再越界——错误方向变成"少吞"，那只会退化成初版那个"用户原话缺席"的
    可观测降级，不会把别的轮次拉进来。**不**写"该事件必须不带 `run_id`"那个附加条件：
    生产里它恒为真，是死分支，而且挡不住孤儿。
    """
    owned = [index for index, event in enumerate(events) if event.run_id == run_id]
    if not owned:
        return None
    start = owned[0]
    if start > 0 and events[start - 1].type == USER_MESSAGE:
        start -= 1
    return start, owned[-1] + 1


@runtime_checkable
class MemoryFormationNotifier(Protocol):
    """运行时把一轮终结交给记忆形成的唯一入口（#298 T7b）。

    为什么是 Protocol，而不是让 `agent/runtime.py` 直接收 `MemoryJobRunner`：Runtime
    需要的只是"把这一轮告诉宿主"这一件事，而 `MemoryJobRunner` 是**作业宿主子系统**
    ——它自己拥有服务循环、认领、恢复、模型调用器与事件出口。让 agent 核心依赖那一整套
    （连同它背后的 jobs / executor / policy / formation 全链）等于把"运行时收尾"与
    "记忆作业怎么跑"焊在一起；而这个端口只有一行签名。

    `MemoryJobRunner` 在结构上满足它（因此它没有被继承）。`runtime_checkable` 让装配层
    能对"给进来的东西真的满足契约"下断言，而不是只靠注解。
    """

    async def notify_run_finished(
        self, *, session_id: str, run_id: str, terminal_status: str,
        events: Sequence[SessionEvent],
    ) -> object | None: ...


class MemoryJobRunner:
    """记忆作业的宿主：资格判定、幂等入队、启动期恢复、单泵服务循环。

    常量与依赖都是显式入参（时钟在 `executor`，这里只读 `datetime.now(UTC)` 判 lease），
    所以 kill/重启与并发用例可以在真实 SQLite 上跑，而不是对着替身。
    """

    def __init__(
        self,
        *,
        jobs: SqliteMemoryV2JobStore,
        sessions: JsonlSessionStore,
        executor: MemoryJobExecutor,
        roles: MemoryModelRoles,
        sink: MemoryJobEventSink | None = None,
        extraction_enabled: bool = True,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        recovery_limit: int = DEFAULT_RECOVERY_LIMIT,
        worker_id: str | None = None,
        memory_capability: Any | None = None,
        workspace_index: Any | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._jobs = jobs
        self._sessions = sessions
        self._executor = executor
        self._roles = roles
        self.memory_capability = memory_capability
        self._workspace_index = workspace_index
        #: 默认按会话建 sink（`executor` 的端口没有 session 参数，只能在这里绑）。
        #: 允许注入是为了让用例能对着裸 list 断言事件，而不必去读会话日志。
        self._sink = sink
        self._extraction_enabled = extraction_enabled
        self._recovery_limit = recovery_limit
        self._max_concurrency = max_concurrency
        # lease 属主：**进程内稳定**（同一个 runner 的所有认领都用它）。带 pid 是为了
        # 让运维能从库里看出"这一行是哪个进程拿着"，带随机后缀是为了同机多进程不撞名。
        self._worker_id = worker_id or f"memory-v2:{os.getpid()}:{uuid4().hex[:8]}"
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._pump_task: asyncio.Task[None] | None = None
        #: 泵派发出去、还没跑完的 job（`Semaphore` 之外的第二道账）——`drain` 靠它
        #: 在泵被超时取消后把残留收干净。
        self._running: set[asyncio.Task[None]] = set()
        self._timer: asyncio.Task[None] | None = None
        self._dirty = False
        self._closed = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def max_concurrency(self) -> int:
        """R11 的全局并发上限（本进程在飞 job 数的上限）。装配层据此记一行可对账的日志，
        同时让"配置真的流到了这里"能用一条断言判，而不必靠计时反推（`Semaphore` 没有
        公开的容量读数）。"""
        return self._max_concurrency

    # ----------------------------------------------------------------------------------
    # 入队（Runtime 的终结臂调用）
    # ----------------------------------------------------------------------------------

    async def notify_run_finished(
        self, *, session_id: str, run_id: str, terminal_status: str,
        events: Sequence[SessionEvent],
    ) -> MemoryFormationJob | None:
        """一次 run 终结：合格就落一个 job 并排进服务循环，不合格什么都不做（AC1）。

        返回 job（含"这个幂等键早就有了"的那一行）或 `None`（不合格 / 已关闭）。
        调用方**不该**据此推断"记忆已形成"——形成在后台，与本函数返回无关（AC10）。

        身份在这里取（不是在后台任务里）：`asyncio.create_task` 快照的是**此刻**的上下文，
        而请求中间件会在 run 收尾后重置它。V1 的 `MemoryWriteback.submit` 踩过同一条。
        """
        if self._closed:
            logger.warning("Memory V2 runner is closed; dropping run %s", run_id)
            return None
        decision = decide_run_end_eligibility(
            terminal_status=terminal_status, events=events,
            extraction_enabled=self._extraction_enabled,
        )
        if not decision.eligible:
            logger.debug("Memory V2 skipped run %s: %s", run_id, decision.skip_reason)
            return None
        identity = get_identity_context()
        project_id = None
        if self._workspace_index is not None:
            try:
                workspace = self._workspace_index.workspace_of_session(session_id)
                project_id = workspace.id if workspace is not None else None
            except Exception as error:  # noqa: BLE001 — project failure narrows to user-global.
                logger.warning(
                    "Memory V2 project binding unavailable (%s); enqueueing user-global context",
                    type(error).__name__,
                )
        job = await self._jobs.enqueue(
            idempotency_key=idempotency_key(run_id),
            trusted=TrustedMemoryIdentity(
                tenant_id=identity.tenant_id, user_id=identity.user_id, project_id=project_id,
            ),
            session_id=session_id, run_id=run_id,
        )
        self._schedule()
        return job

    # ----------------------------------------------------------------------------------
    # 恢复与生命周期
    # ----------------------------------------------------------------------------------

    async def recover(self) -> int:
        """启动期恢复扫描：把库里未终结的 job 排进服务循环，返回看到的待办条数。

        只做"叫醒服务循环"这一件事——`list_recoverable` 是待办视图，真正决定归属的仍是
        `claim`。所以本函数不返回"恢复了几条"（那是跑完之后才知道的事），只返回扫描到的
        条数：调用方拿它记一行日志（"重启时还有 N 条记忆作业没跑完"）。

        lease 还没到期的行这一次认领不到（属主可能是刚被杀的进程）。`_pump` 会按最早到期
        时间定一个唤醒点，所以**不需要新流量**它们也会被捡起来——这条是"崩溃恢复"作为
        **机制**而不是"下次有人用才恢复"的分界线。
        """
        if self._closed:
            return 0
        pending = await self._jobs.list_recoverable(limit=self._recovery_limit)
        if not pending:
            return 0
        logger.info("Memory V2 recovered %d unfinished job(s) at startup", len(pending))
        self._schedule()
        return len(pending)

    async def drain(self, *, timeout_seconds: float = _DRAIN_TIMEOUT_SECONDS) -> None:
        """等在途的服务循环跑完（有界）。超时即取消——被取消的 job 仍可恢复。"""
        task = self._pump_task
        if task is not None and not task.done():
            try:
                async with asyncio.timeout(timeout_seconds):
                    await asyncio.gather(task, return_exceptions=True)
            except TimeoutError:
                logger.warning("Memory V2 runner drain timed out; cancelling the service loop")
        # 泵被超时取消时，它派发出去的在飞任务**不会**跟着被取消（它们是独立 task）——
        # 这里补一刀，否则 `aclose` 返回之后它们还在写库，而调用方以为已经收干净了。
        waiting = list(self._running)
        if waiting:
            for item in waiting:
                item.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)

    async def aclose(self, *, timeout_seconds: float = _DRAIN_TIMEOUT_SECONDS) -> None:
        """收尾：停止接受新 run，等（或取消）在途循环，撤掉唤醒定时器。

        顺序：先置 `_closed`（此后 `notify_run_finished` 直接拒绝，不再往库里加活），
        再撤定时器（它可能正睡在那个"等 lease 到期"的长觉上，不撤会让 `drain` 之后
        又冒出一个泵），最后有界地等循环。
        """
        self._closed = True
        self._cancel_timer()
        await self.drain(timeout_seconds=timeout_seconds)

    # ----------------------------------------------------------------------------------
    # 服务循环
    # ----------------------------------------------------------------------------------

    def _schedule(self) -> None:
        """叫醒服务循环。已在跑 ⇒ 只置脏标记（那一轮会顺手捞走新活）。"""
        if self._closed:
            return
        if self._pump_task is not None and not self._pump_task.done():
            self._dirty = True
            return
        self._cancel_timer()
        self._dirty = False
        self._pump_task = asyncio.create_task(self._pump())

    def _cancel_timer(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None and not timer.done():
            timer.cancel()

    async def _pump(self) -> None:
        """认领—派发，直到没有可认领的 job；等在飞的跑完再定去留。

        退出条件与唤醒点是同一个判断的两面：`claim` 返回 `None` 表示"此刻没有能跑的"，
        而"以后能不能跑"要看还有没有人在意（`list_recoverable` 非空）以及最早何时。

        **认领与执行必须分开**（T7 自查时改掉的）：写成 `await self._run_one(job)` 时
        循环是串行的，R11 的"全局并发 4"会恒等于 1，而 `Semaphore` 成了永远不竞争的
        死代码——那种实现看起来也"能跑"，只是把一条性能约束静默降级成串行。现在的形状是
        认领即派发、由 `Semaphore` 兜住在飞数，所以并发上限是**真的**在这一层。
        """
        try:
            while True:
                while (job := await self._jobs.claim(worker_id=self._worker_id)) is not None:
                    self._spawn(job)
                if self._dirty:
                    self._dirty = False
                    continue
                if self._running:
                    # 等在飞的跑完：它们的终态会释放同用户的在途槽位，于是可能又冒出新活。
                    # 不在这里等会让下面那次 `claim` 的 `None` 变成"暂时没有"而不是"没有了"。
                    await asyncio.gather(*list(self._running), return_exceptions=True)
                    continue
                delay = await self._idle_delay()
                if delay is None:
                    return
                self._arm_timer(delay)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # 泵自己崩了不该静默：它是唯一推进待办的东西。留在日志里，下一次入队/
            # 恢复会再起一个（`_pump_task` 已 done ⇒ `_schedule` 会新建）。
            logger.exception("Memory V2 service loop failed")

    def _spawn(self, job: MemoryFormationJob) -> None:
        """把一个已认领的 job 交给在飞集合（`Semaphore` 在 `_run_one` 里限并发）。"""
        task = asyncio.create_task(self._run_one(job))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _idle_delay(self) -> float | None:
        """空闲时该多久之后再来看一眼（`None` = 不用来了）。"""
        pending = await self._jobs.list_recoverable(limit=self._recovery_limit)
        if not pending:
            return None
        return _seconds_until_claimable(pending, datetime.now(UTC))

    def _arm_timer(self, delay: float) -> None:
        """睡 `delay` 秒后重新叫醒服务循环（等 lease 到期用）。"""
        self._cancel_timer()
        self._timer = asyncio.create_task(self._wake_after(delay))

    async def _wake_after(self, delay: float) -> None:
        """睡够了再叫醒服务循环。被取消（`aclose` / 有新活）时中途退出。

        不需要 `except CancelledError` 清理 `self._timer`：撤掉本任务的那条路径
        （`_cancel_timer`）先把引用置空再取消，所以解除时引用已经是对的。
        """
        await asyncio.sleep(delay)
        self._timer = None
        self._schedule()

    # ----------------------------------------------------------------------------------
    # 一个 job
    # ----------------------------------------------------------------------------------

    async def _run_one(self, job: MemoryFormationJob) -> MemoryJobResult | None:
        """跑一个已认领的 job。异常不向上抛——泵必须继续服务其余待办。

        两条异常路径的归因**不同**，因为它们要告诉运维的是两件事：

        - `_slice` 抛错 ⇒ **输入**拿不到（会话日志读不出来）⇒ `RUN_EVENTS_UNAVAILABLE`；
        - `executor.run` 抛错 ⇒ 异常穿过了执行器自己的全套降级处理，属于**契约之外**
          的失败（已知来源是检索层/存储层）⇒ `JOB_FAILED`。

        两条都收口成终态而不是"留着下次再试"：ticket 明确选了"exhausted work ends
        degraded with no memory write"这条路，而一个可以永远不终结的 job 会让 AC6 的
        "收敛到一个正确终态"不成立。代价（一次瞬时存储抖动也会让这轮记忆作废）是
        显式权衡：收益侧是资源有界，理由记在 `DegradedReason.JOB_FAILED` 上。
        """
        async with self._semaphore:
            try:
                run_events, history = await self._slice(job)
            except Exception:
                logger.exception("Memory V2 job %s: cannot rebuild the run slice", job.job_id)
                return await self._abandon(job, reason=DegradedReason.RUN_EVENTS_UNAVAILABLE)
            try:
                # `explicit_remember` 刻意**不传**（走执行器的默认 False）：本宿主只有
                # **自动形成**这一条入队路径（run 终结触发），而自动路径上同意信号恒为
                # False 是政策层写死的前提（`policy.py` 第 2 节：9 类 `sensitive` 候选在
                # 自动路径上必须被拒，AC3 的 unauthorized-sensitive）。"Remember X" 那条
                # 显式命令路径的生产方属于 #300（governance API），它**不能**搭这条入队口
                # ——那会让"用户要求记住"被静默降级成"自动形成"，而这一轮的候选带不带
                # 同意是一条**持久事实**（写在 job 上没有，恢复后就不可知）。
                return await self._executor.run(
                    job, worker_id=self._worker_id, run_events=run_events,
                    history=history, roles=self._roles, sink=self._sink_for(job),
                )
            except Exception:
                logger.exception("Memory V2 job %s failed outside the executor", job.job_id)
                return await self._abandon(job, reason=DegradedReason.JOB_FAILED)

    async def _abandon(
        self, job: MemoryFormationJob, *, reason: DegradedReason,
    ) -> MemoryJobResult | None:
        """把一条跑不动的 job 收口成降级终态（零写入）。

        刻意**不发** `memory/degraded` 事件。两个原因各占一半：本函数处理的两条路径
        （日志读不出来、执行器契约之外抛错）都意味着"事件要写进去的那个会话日志/
        那个依赖可能正好是坏掉的那一个"，而且——更硬的一条——执行器自己的降级路径
        已经负责发事件，这里再发一份会让"同一件事两个出口"再次出现（T6b 修的就是
        这类分叉）。事实留在日志（traceback）与 job 行的 `state` + `reason` 上。
        """
        updated = await self._jobs.transition(
            job_id=job.job_id, worker_id=self._worker_id, stage=MemoryJobStage.DEGRADED,
            state={"phase": "runner", "reason": reason.value}, reason=reason.value,
        )
        if updated is None:
            return None
        return MemoryJobResult(
            job_id=job.job_id, stage=updated.stage, outcome=None, reason=reason.value)

    async def _slice(
        self, job: MemoryFormationJob,
    ) -> tuple[list[SessionEvent], list[SessionEvent]]:
        """按 `(session_id, run_id)` 从会话日志切出（本轮事件，更早历史）。

        `run_id` 为 `None`（T7 之前入队的旧行）或日志里找不到这一次 run 时返回**空的本轮
        事件**：执行器把"没有本轮事件"当作一条可判定的降级（见其 `run` 的注释），所以这里
        不抛。

        切片的边界判定收在 `run_slice_bounds` 一处——**不要**在这里退回"按 run_id 过滤"，
        那条路会把本轮用户发言（`run_id is None`）挤出 `run_events`，而投影的别名只发给
        `run_events`（完整论证在该函数的 docstring 里）。

        历史取**序号在本轮之前**的全部事件——投影自己会截到最近 8 条 user/assistant
        消息，在这里再截一次就是把它的规则抄了第二遍。
        """
        if job.run_id is None:
            logger.warning(
                "Memory V2 job %s predates run_id persistence; nothing to form from",
                job.job_id,
            )
            return [], []
        events = await asyncio.to_thread(self._sessions.read_events, job.session_id)
        bounds = run_slice_bounds(events, job.run_id)
        if bounds is None:
            logger.warning(
                "Memory V2 job %s: session %s holds no events for run %s",
                job.job_id, job.session_id, job.run_id,
            )
            return [], []
        start, stop = bounds
        return events[start:stop], events[:start]

    def _sink_for(self, job: MemoryFormationJob) -> MemoryJobEventSink:
        """本次执行的事件出口。注入了就用注入的，否则按 job 的会话建一个。"""
        if self._sink is not None:
            return self._sink
        return SessionEventSink(self._sessions, job.session_id)
