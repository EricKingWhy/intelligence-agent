"""`_TerminalArms` 与五条终结臂的单元测试（#264 / T11 第一切片）。

**为什么要有这一层**：`_drive` 的六个终结点原先各自把收尾序列写在 360 行里，只能靠
ScriptedModel 全链验证"整段序列长什么样"（`tests/agent/test_event_sequence_golden.py`，
#263 建立）。#264 把收尾提到以 `_TerminalArms` 为唯一入参的方法上，于是**每条臂自己的
契约**可以脱离 Agent Loop 直接调用验证。两层合起来才是本票的验收面：

- golden（全链）：提取前后事件序列逐字相同 ⇒ **等价性**；
- 本文件（单臂）：每条臂的写入顺序、信封表达式、单终态、记忆/观测调用 ⇒ **臂自身正确**。

界线：本文件**不碰**"走哪条臂、何时 return"——那是 `_drive` 的职责（由 golden 覆盖）。

#265（T11 第二切片）把观测可变状态（tracer + 在途 ctx_span / generation）收进
`_Telemetry`，本文件随之只改**属性路径**（`arms.*` → `arms.telemetry.*`）——既有断言的
期望值一字未改，那正是"提取零行为变化"的一部分证据；另加一节直接钉 `_Telemetry` 自身的
成对 / 收口纪律（臂层断言此前只是顺带覆盖）。全链那一半仍由 golden 承担。

本文件同时补掉两条 #263 留下的残余（见 `docs/SDD_TICKET_TRACKER.md` B-27 段残余①）：
`_TerminalContext.interrupt_streams` 里 `streamer.interrupt(step=…)` 与 `MODEL_FALLBACK`
的 `self.steps + 1` 此前**没有任何冻结序列覆盖**（14 个场景无一在终结臂里产出
`model/fallback`、也无场景产出 `reasoning/interrupted`）——这里用一个带待取切换事实的
替身 coordinator 与一个开着的思考块把它们钉住。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import pytest

from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    TRIGGER_LOCAL_TURNS,
    BudgetConsumed,
    LaunchRunBudget,
)
from agent_harness.agent.runtime import (
    AgentRuntime,
    _RunFinalizer,
    _Telemetry,
    _TerminalArms,
    _TerminalStages,
)
from agent_harness.agent.streaming import BlockStreamer
from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
    STATUS_PAUSED,
    AgentRunResult,
)
from agent_harness.context.compactor import ContextWindowExceededError
from agent_harness.model.failure import (
    PROVIDER_ACCOUNT_UNAVAILABLE_MESSAGE,
    PROVIDER_ACCOUNT_UNAVAILABLE_REASON,
    UNCLASSIFIED_FAILURE_MESSAGE,
)
from agent_harness.model.fallback import FallbackTransition, ModelRequestAttempt
from agent_harness.session import (
    MODEL_FAILED,
    MODEL_FALLBACK,
    MODEL_REQUEST,
    REASONING_INTERRUPTED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_STARTED,
    TEXT_DELTA,
    Session,
    SessionEvent,
)
from agent_harness.session.store import SeqConflict
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.session.store_fixtures import RejectingStore

RUN_ID = "run-1"


# ---------------------------------------------------------------------------
# 替身：只实现终结臂真正调用的那几面
# ---------------------------------------------------------------------------


class _RecordingTracer:
    """记录臂调了哪些观测方法。

    golden 用 NullTracer（no-op）⇒ 观测调用在全链基线里**不可见**；而 #265 收敛的
    正是这套 telemetry 调用点，所以在臂这一层把它钉住：哪条臂调什么、带什么值。
    句柄也由这里造（`ctx-span-<step>` / `generation-<step>`）——#265 之后句柄不再
    流经调用方，只有一个消费者（`_Telemetry`）能测到"收到的是哪一个"。
    （真实实现：`agent_harness/observability/port.py`，不抛的保证由 `_GuardedTracer`
    在 Core 单点强制——本替身不模拟那一层。）

    ⚠ 句柄形状与生产**不同型**：这里返回 `str`，生产返回 `Span`（或降级后的 `None`）。
    对**这两条臂**无影响——臂对句柄只做"存 / 取 / 转交"，从不调它的方法。但本文件不止
    臂用例：`_Telemetry` 的单测（`test_telemetry_*`）**正在断言这个 `str` 形状**
    （`assert telemetry.ctx_span == "ctx-span-2"`），所以"把本文件的替身全换成同型句柄"
    是**有断言成本的改动**，不是机械替换——同型替身在
    `tests/observability/test_tracer_port.py`（`_IdentifiedNullSpan`），那里测的是
    "句柄怎么被收口"。本文件其余 `str` 句柄替身（`_CountSpy` / `_AritySpy`）同理保留，
    该分叉登记在 `docs/SDD_TICKET_TRACKER.md` 的 **B-34 残余②**（原始形状）与 **B-35 残余**
    第 9 条（本批的如实改写与"有断言成本"结论）。
    """

    trace_id = "trace-1"
    trace_url = "https://trace.example/trace-1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **fields: Any) -> None:
        self.calls.append((name, fields))

    def run_started(self) -> None:
        self._record("run_started")

    def run_completed(self, final_text: str, usage_total: dict[str, int] | None = None) -> None:
        self._record("run_completed", final_text=final_text, usage_total=usage_total)

    def run_failed(self, reason: str) -> None:
        self._record("run_failed", reason=reason)

    def context_build_started(self, *, step: int) -> Any:
        self._record("context_build_started", step=step)
        return f"ctx-span-{step}"

    def context_build_completed(
        self, span: Any, *, compacted_turn_count: int | None = None,
    ) -> None:
        self._record("context_build_completed", span=span)

    def model_call_started(
        self, *, step: int, messages: Any, model: str | None = None,
    ) -> Any:
        self._record("model_call_started", step=step)
        return f"generation-{step}"

    def model_call_completed(
        self, generation: Any, *, output_text: str, **rest: Any,
    ) -> None:
        self._record("model_call_completed", span=generation, output_text=output_text)

    def model_call_failed(self, generation: Any, *, error_type: str) -> None:
        # 句柄也记下来：`close_pending` 这条出口此前**没有身份断言**（窄复验 B 轴 C4 实测
        # "传 None 照样 59 条全绿"）⇒ 记 `span` 才能钉住"转交的是在途那个句柄"。
        self._record("model_call_failed", span=generation, error_type=error_type)


class _PendingCoordinator:
    """只实现 `drain_transitions()` / `drain_requests()` 的 coordinator 替身。

    终结臂对 coordinator 的两处调用就是取走切换事实与请求账目（编排/重试行为在
    `tests/agent/test_model_fallback_runtime.py` 覆盖），故这里只喂一个待取的
    `FallbackTransition`，用来把"切换事实在终结臂里的落盘形状"（含信封表达式）钉住。
    请求账目同理可以喂进来（`#313`）：调用失败/取消时**已经发出去**的请求照样要落
    `model/request`，那是终结臂的行为，不是编排层的。
    """

    def __init__(
        self, *transitions: FallbackTransition,
        requests: Iterable[ModelRequestAttempt] = (),
    ) -> None:
        self._pending = list(transitions)
        self._pending_requests = list(requests)
        # 调用计数：断言"臂只取一次"要断这个，不能断"第二次返回空"——后者断的是
        # 本替身自己的幂等实现，产品代码怎么改都会绿（自证式断言）。
        self.drains = 0
        self.request_drains = 0

    def drain_transitions(self) -> list[FallbackTransition]:
        self.drains += 1
        out, self._pending = self._pending, []
        return out

    def drain_requests(self) -> list[ModelRequestAttempt]:
        self.request_drains += 1
        out, self._pending_requests = self._pending_requests, []
        return out


class _CheckpointSpy:
    """记录边界 + **保存那一刻**最后一条已持久化事件（顺序证据）。

    `order` 是与 `_MemorySpy` **共享**的同一根时间线：两个替身各写各的 list 时，
    "记的都是当时最后一条事件类型"会恒等（`_write_memories` 不落事件），
    于是两条断言都锁不住彼此的先后（实测：把记忆抽取挪到 checkpoint 之后，
    各写各的版本全绿）。共享一根线才能钉住"终态 → 记忆 → checkpoint"这个三元序。
    """

    def __init__(self, order: list[str] | None = None) -> None:
        self.saves: list[tuple[str, str | None]] = []
        self.order = order if order is not None else []

    async def maybe_save(self, session: Session, boundary_type: Any) -> None:
        last = session.events[-1].type if session.events else None
        self.saves.append((boundary_type.value, last))
        self.order.append(f"checkpoint:{boundary_type.value}")


class _MemorySpy:
    """`memory_writer` 替身：只记"提交了几次、提交了哪些类型"（不落任何存储）。

    顺序证据同 `_CheckpointSpy`：往共享时间线里追加 `memory`。
    """

    def __init__(self, order: list[str] | None = None) -> None:
        self.submits: list[tuple[tuple[str, ...], str | None]] = []
        self.order = order if order is not None else []

    def submit(self, session: Session, events: list[Any]) -> None:
        last = session.events[-1].type if session.events else None
        self.submits.append((tuple(e.type for e in events), last))
        self.order.append("memory")


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path: Any) -> Session:
    session = make_session(tmp_path)
    session.append(RUN_STARTED, {})
    return session


def _runtime(
    *, memory_writer: Any = None, checkpoint_policy: Any = None,
) -> AgentRuntime:
    """只用来提供 `_write_memories` / `_save_checkpoint` 两个钩子。

    `model=object()`：终结臂不碰模型（臂在 loop 之外，模型调用早已结束/失败）。
    """
    registry = ToolRegistry()
    return AgentRuntime(
        model=object(), registry=registry, executor=ToolExecutor(registry),
        memory_writer=memory_writer, checkpoint_policy=checkpoint_policy,
    )


class _ArmsKit:
    """一次装配的全部零件（臂 + 便于断言的句柄）。

    `streamer` 声明成 `Any` 而不是 `BlockStreamer`：这里的故障注入用的是**鸭子类型**替身
    （`_FailingStreamer` 只实现 `interrupt`，刻意不继承生产类——继承会把"臂只调这一面"
    这条事实藏起来）。注成生产类型等于对类型检查器说谎。
    """

    def __init__(
        self, runtime: AgentRuntime, session: Session, *, step_base: int = 0,
        run_id: str | None = RUN_ID, memory_event_start: int = 0,
        streamer: Any = None, coord: Any = None,
        tracer: _RecordingTracer | None = None,
        cancel_reason_supplier: Any = None,
        usage_total: dict[str, int] | None = None,
    ) -> None:
        self.runtime = runtime
        self.session = session
        self.usage_total: dict[str, int] = {} if usage_total is None else usage_total
        self.tracer = tracer or _RecordingTracer()
        self.coord = coord if coord is not None else _PendingCoordinator()
        self.result_holder: list[AgentRunResult] = []
        terminal = _RunFinalizer(session, self.usage_total)
        if run_id is not None:
            # run id 的 owner 是 _RunFinalizer（_TerminalArms.run_id 只是它的读口）；
            # 生产里这一步是 _drive 的 terminal.begin_run(run_id)。
            terminal.begin_run(run_id)
        self.arms = _TerminalArms(
            session=session,
            terminal=terminal,
            usage_total=self.usage_total,
            model_coord=self.coord,
            result_holder=self.result_holder,
            cancel_reason_supplier=cancel_reason_supplier,
            step_base=step_base,
            memory_event_start=memory_event_start,
            telemetry=_Telemetry(tracer=self.tracer),
            streamer=streamer,
        )

    def types(self) -> list[str]:
        return [e.type for e in self.session.events]

    def since(self, mark: int) -> list[Any]:
        return list(self.session.events[mark:])


def _kit(
    session: Session, *, memory_writer: Any = None, checkpoint_policy: Any = None,
    **kw: Any,
) -> _ArmsKit:
    return _ArmsKit(
        _runtime(memory_writer=memory_writer, checkpoint_policy=checkpoint_policy),
        session, **kw,
    )


async def _drain(agen: Any) -> list[Any]:
    return [event async for event in agen]


# ---------------------------------------------------------------------------
# 上下文本身：信封表达式与接线
# ---------------------------------------------------------------------------


def test_envelope_step_keeps_the_session_base() -> None:
    """信封编号 = `step_base + steps`——**两半都在**。

    单轮会话 `step_base = max(max_step_id, user_turn_count) = 0` 让这个表达式可以
    被误换成 `steps` 而全链基线不红（#263 的多轮用例专门盯这一点）；在臂这一层直接
    用 `step_base != 0` 钉，成本最低。
    """
    arms = _TerminalArms(
        session=SimpleNamespace(), terminal=SimpleNamespace(run_id=None), usage_total={},
        model_coord=SimpleNamespace(), result_holder=[],
        cancel_reason_supplier=None, step_base=7,
    )
    assert arms.envelope_step(0) == 7
    assert arms.envelope_step(2) == 9


def test_context_passes_the_round_state_through(session: Session) -> None:
    """`context(steps)` 是取消臂/异常臂的单一转换点：round state 原样透传。"""
    streamer = BlockStreamer(session)
    streamer.begin_run(RUN_ID)
    coord = _PendingCoordinator()
    kit = _kit(session, streamer=streamer, coord=coord)
    kit.arms.telemetry.ctx_span = object()
    kit.arms.telemetry.generation = object()

    ctx = kit.arms.context(steps=3)

    assert ctx.steps == kit.arms.envelope_step(3)
    assert ctx.session is session
    assert ctx.run_id == RUN_ID
    assert ctx.terminal is kit.arms.terminal
    assert ctx.streamer is streamer
    assert ctx.model_coord is coord
    assert ctx.telemetry.tracer is kit.tracer
    assert ctx.telemetry.ctx_span is kit.arms.telemetry.ctx_span
    assert ctx.telemetry.generation is kit.arms.telemetry.generation


# ---------------------------------------------------------------------------
# `_Telemetry`：观测可变状态的单点 owner（#265）
# ---------------------------------------------------------------------------


def test_telemetry_pairs_the_in_flight_handles() -> None:
    """起 / 收成对：句柄不出对象，收口后不留悬空（#247 AC4 的"每个出口都释放"）。

    成功路径的端口调用是**无条件**的（句柄可能因 adapter 降级为 None，端口自己
    早退）——与取消/异常臂的"仅在途才调"是两条既有语义，各钉一条。
    """
    tracer = _RecordingTracer()
    telemetry = _Telemetry(tracer=tracer)

    telemetry.context_build_started(step=2)
    assert telemetry.ctx_span == "ctx-span-2", "句柄住在对象里，调用方拿不到"
    telemetry.context_build_completed(compacted_turn_count=1)
    assert telemetry.ctx_span is None
    telemetry.model_call_started(step=3, messages=[], model="m")
    assert telemetry.generation == "generation-3"
    telemetry.model_call_completed(output_text="答", usage={"total_tokens": 1})
    assert telemetry.generation is None

    assert [name for name, _ in tracer.calls] == [
        "context_build_started", "context_build_completed",
        "model_call_started", "model_call_completed",
    ]
    # 端口收到的是**本对象保管的那个句柄**（这层身份无法从别处观察）
    assert ("context_build_completed", {"span": "ctx-span-2"}) in tracer.calls
    assert ("model_call_completed", {"span": "generation-3", "output_text": "答"}) \
        in tracer.calls
    # trace 身份如实透传端口（缺席实现是 None，不伪造）
    assert telemetry.trace_id == "trace-1"
    assert telemetry.trace_url == "https://trace.example/trace-1"
    assert _Telemetry().trace_id is None


def test_telemetry_success_path_calls_the_port_even_with_a_degraded_handle() -> None:
    """adapter 降级（句柄 None）时成功路径仍调端口——端口对 None 句柄安全早退。"""

    class _DegradedTracer(_RecordingTracer):
        def context_build_started(self, *, step: int) -> Any:
            self._record("context_build_started", step=step)
            return None  # 降级 adapter 的如实返回（端口契约允许 None 句柄）

    tracer = _DegradedTracer()
    telemetry = _Telemetry(tracer=tracer)

    telemetry.context_build_started(step=0)
    telemetry.context_build_completed()

    assert [name for name, _ in tracer.calls] == [
        "context_build_started", "context_build_completed",
    ]
    assert ("context_build_completed", {"span": None}) in tracer.calls


def test_telemetry_close_pending_order_and_attribution() -> None:
    """收口三连：在途 ctx_span → 在途 generation（取消归因 "cancelled"）→ run_failed。

    句柄用 `object()` 而非字面量：`_Telemetry` 若把在途句柄换成别的值（或 `None`），
    `span=gen` 这条断言才红——窄复验 B 轴 C4 实测这条出口此前**没有身份断言**。
    """
    tracer = _RecordingTracer()
    gen = object()
    telemetry = _Telemetry(tracer=tracer, ctx_span="span-ctx", generation=gen)

    telemetry.close_pending(error_type=None, reason="cancelled", cancelled=True)

    assert [name for name, _ in tracer.calls] == [
        "context_build_completed", "model_call_failed", "run_failed",
    ]
    assert ("context_build_completed", {"span": "span-ctx"}) in tracer.calls
    assert ("model_call_failed", {"span": gen, "error_type": "cancelled"}) in tracer.calls
    assert telemetry.ctx_span is None and telemetry.generation is None


def test_telemetry_close_pending_keeps_the_exception_attribution() -> None:
    """异常臂：error_type 原样透传（未分类故障的可读文案由 `_TerminalContext` 负责）。"""
    tracer = _RecordingTracer()
    gen = object()
    telemetry = _Telemetry(tracer=tracer, generation=gen)

    telemetry.close_pending(error_type="TimeoutError", reason="TimeoutError")

    assert [name for name, _ in tracer.calls] == ["model_call_failed", "run_failed"]
    assert ("model_call_failed", {"span": gen, "error_type": "TimeoutError"}) in tracer.calls


def test_telemetry_close_pending_skips_handles_that_are_not_in_flight() -> None:
    """非在途不调端口（成功路径收过的句柄 / 模型尚未调用）——原臂的既有语义。"""
    tracer = _RecordingTracer()

    _Telemetry(tracer=tracer).close_pending(error_type=None, reason="boom")

    assert [name for name, _ in tracer.calls] == ["run_failed"]


def test_telemetry_forwards_the_compaction_count() -> None:
    """metadata 面：压缩计数随收口一起到端口，缺省如实 None（不伪造）。"""
    seen: list[int | None] = []

    class _CountSpy(_RecordingTracer):
        def context_build_completed(
            self, span: Any, *, compacted_turn_count: int | None = None,
        ) -> None:
            seen.append(compacted_turn_count)

    telemetry = _Telemetry(tracer=_CountSpy())

    telemetry.context_build_started(step=0)
    telemetry.context_build_completed(compacted_turn_count=3)
    telemetry.context_build_started(step=1)
    telemetry.context_build_completed()

    assert seen == [3, None]


def test_context_build_completed_keeps_the_pre_264_keyword_arity() -> None:
    """残余 R1：**关键字集合**逐字回到 #264 之前（端口输出无差异，差异在这层）。

    `_Telemetry` 的两个活调用点 + `close_pending` 的直呼端口，两种形状：成功路径传
    `compacted_turn_count`（值域内，`None` 也算传——那是"本轮没有压缩发生"的实参）；
    `close_pending` 与 context 超限臂**不传**。历史：#265 把**经 wrapper 的两处**统一成
    "一律带关键字"（超限臂随之从裸调变带关键字）；`close_pending` 一直直呼端口、
    从不经该参数，谈不上"被统一"。本票把 wrapper 的缺省形状改回"调用方没给就不传"
    （哨兵 `_UNSET`），本条钉住口径。

    为什么值得一条用例：显式 `None` 与不传在 `RunTracer` 那里**输出同效**
    （按 `is not None` 决定是否写 metadata 键），所以这条差异只能在这一层被观察到；
    而"调用形状变了"本身是接口契约变动（第三方 Tracer 实现的自定义签名会受影响）。

    本用例只钉 `_Telemetry` 自己的缺省语义；"**臂**有没有把形状用对"由
    `test_context_exceeded_arm_closes_and_clears_the_handle`（臂层裸调）与
    `test_context_window_exceeded_then_disconnect_collects_the_span_once`
    （端到端 kwargs 面）分别承载。

    末项（第 4 个 `()`）顺带钉住 `close_pending` 的形状：它**直呼端口**、从不经上面那个
    参数，所以它给出的关键字集合也是空——这正是它与前两项分属两条路径的证据。
    """
    seen: list[tuple[str, ...]] = []

    class _AritySpy(_RecordingTracer):
        def context_build_completed(self, span: Any, **kwargs: Any) -> None:
            seen.append(tuple(sorted(kwargs)))

    telemetry = _Telemetry(tracer=_AritySpy())

    telemetry.context_build_started(step=0)
    telemetry.context_build_completed(compacted_turn_count=3)   # 有压缩：
    telemetry.context_build_started(step=1)
    telemetry.context_build_completed(compacted_turn_count=None)  # 无压缩但调用方**给了**值
    telemetry.context_build_started(step=2)
    telemetry.context_build_completed()                         # 调用方没给（超限臂形状）
    telemetry.context_build_started(step=3)                     # 再起一次：close_pending 只在途才调
    telemetry.close_pending(error_type=None, reason="cancelled")

    assert seen == [("compacted_turn_count",), ("compacted_turn_count",), (), ()]


def test_telemetry_snapshot_leaves_live_handles_alone() -> None:
    """快照取一份：收口只置空快照自己那份，活值不动（#264 纪律的落点）。"""
    live = _Telemetry(tracer=_RecordingTracer(), ctx_span="span-ctx", generation="gen-1")

    snapshot = live.snapshot()
    snapshot.close_pending(error_type=None, reason="cancelled", cancelled=True)

    assert live.ctx_span == "span-ctx"
    assert live.generation == "gen-1"
    assert (snapshot.ctx_span, snapshot.generation) == (None, None)
    assert (snapshot.tracer, snapshot.trace_id) == (live.tracer, "trace-1")


def test_cancel_reason_defaults_to_cancelled_and_asks_the_supplier(session: Session) -> None:
    """reason 来源收在一处：无 supplier = "cancelled"，有 = 宿主决定（orphaned）。"""
    assert _kit(session).arms.cancel_reason() == "cancelled"

    calls: list[int] = []
    kit = _kit(session, cancel_reason_supplier=lambda: (calls.append(1), "orphaned")[1])
    assert kit.arms.cancel_reason() == "orphaned"
    assert kit.arms.cancel_reason() == "orphaned"
    assert len(calls) == 2, "每次调用都要重新问宿主（取消时点的事实，不缓存）"


# ---------------------------------------------------------------------------
# 完成臂
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_arm_order_terminal_then_memory_then_checkpoint(
    session: Session,
) -> None:
    """终态 → 记忆抽取 → 镜像 → FINAL_COMPLETED 边界 → 结果。

    三元序用**共享时间线**钉死：两个替身各写各的 list 时，"提交/保存那一刻的最后一条
    事件"恒为 `run/completed`（`_write_memories` 不落事件）⇒ 断言看不出记忆与 checkpoint
    谁先谁后（实测：把记忆抽取挪到 checkpoint 之后仍全绿）。共享一根 `order` 才有区分力。
    """
    order: list[str] = []
    memory = _MemorySpy(order)
    checkpoints = _CheckpointSpy(order)
    kit = _kit(session, memory_writer=memory, checkpoint_policy=checkpoints)
    kit.usage_total["total_tokens"] = 42
    mark = len(session.events)

    emitted = await _drain(
        kit.runtime._terminal_completed(kit.arms, steps=0, final="最终回答"),
    )

    assert len(emitted) == 1 and emitted[0].type == RUN_COMPLETED
    terminal = kit.since(mark)[-1]
    assert terminal.type == RUN_COMPLETED
    assert terminal.data["final_text"] == "最终回答"
    assert terminal.data["usage_total"] == {"total_tokens": 42}
    assert terminal.run_id == RUN_ID, "终态事件必须挂在本次 run 上（run_id 无第二来源）"
    # 记忆抽取：提交一次，且提交那一刻终态已落盘
    assert len(memory.submits) == 1
    submitted_types, last_at_submit = memory.submits[0]
    assert RUN_COMPLETED in submitted_types
    assert last_at_submit == RUN_COMPLETED
    # checkpoint：FINAL_COMPLETED，且保存时终态已落盘
    assert checkpoints.saves == [("FINAL_COMPLETED", RUN_COMPLETED)]
    # 三元序：记忆抽取在 checkpoint 之前（抖动这一序必须让本行变红）
    assert order == ["memory", "checkpoint:FINAL_COMPLETED"]
    # 结果：run() 的返回值载体（status 用状态常量，不是自由文本）
    assert len(kit.result_holder) == 1
    assert kit.result_holder[0].status == STATUS_COMPLETED
    assert kit.result_holder[0].final_text == "最终回答"
    assert kit.result_holder[0].steps == 0
    # 观测：run_completed 带 usage_total（与事件载荷同源）
    assert ("run_completed", {"final_text": "最终回答", "usage_total": {"total_tokens": 42}}) \
        in kit.tracer.calls


# ---------------------------------------------------------------------------
# 失败终态臂（`#312` T4 起只剩同错熔断硬触发一条生产路径；预算到顶走暂停臂）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_run_arm_writes_reason_and_stops_at_one_terminal(
    session: Session,
) -> None:
    """`reason` 同时喂 tracer / run/failed / AgentRunResult.status，且只终结一次。"""
    memory = _MemorySpy()
    kit = _kit(session, memory_writer=memory)
    mark = len(session.events)

    emitted = await _drain(
        kit.runtime._terminal_failed_run(
            kit.arms, steps=2, reason=STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
            message="连续 2 轮仍在请求工具",
        ),
    )

    assert [e.type for e in emitted] == [RUN_FAILED]
    terminal = kit.since(mark)[-1]
    assert terminal.data["reason"] == STATUS_IDENTICAL_TOOL_FAILURE_LOOP
    assert terminal.data["message"] == "连续 2 轮仍在请求工具"
    assert terminal.run_id == RUN_ID, "终态事件必须挂在本次 run 上"
    assert kit.result_holder[0].status == STATUS_IDENTICAL_TOOL_FAILURE_LOOP
    assert kit.tracer.calls == [
        ("run_failed", {"reason": STATUS_IDENTICAL_TOOL_FAILURE_LOOP}),
    ]
    assert memory.submits[0][1] == RUN_FAILED, "记忆抽取要在终态落盘之后"

    # 单终态不变量：再调一次不得补第二条终结（双终结 = 历史不可对账）
    before = len(session.events)
    assert await _drain(
        kit.runtime._terminal_failed_run(
            kit.arms, steps=2, reason=STATUS_CONTEXT_WINDOW_EXCEEDED,
        ),
    ) == []
    assert session.events[before:] == []


@pytest.mark.asyncio
async def test_pause_arm_is_nonterminal_and_closes_the_execution(
    session: Session,
) -> None:
    """预算暂停臂（`#312` T4）：落一条 `run/paused`、**没有**终态、不写记忆。

    `mark_terminal_written()` 在这里的含义不是"补终态"，而是"本次执行的收口事实已落盘"：
    紧随其后的任何终结臂都必须被单终态不变量拦住（否则一次暂停会追加一条假失败）。

    closeout：`LaunchRunBudget()` 无 run ceiling ⇒ 四维都还有余量（`closeout_capacity`）
    ⇒ 会尝试一次模型 closeout，但本 kit 的 model 是 `object()`（无 `ainvoke`）⇒ 回落
    确定性 continuation。**这次失败调用照样记 `model_requests`**（`#313`：请求发出去过
    就是请求，失败只是没有产出决策 ⇒ 不增 `agent_turns`），而它没报 usage / cost
    ⇒ 那两个维度记**未知**而不是 0（`11 §6.1`：不可得 ≠ 0）。
    """
    memory = _MemorySpy()
    kit = _kit(session, memory_writer=memory)
    mark = len(session.events)
    launch = LaunchRunBudget(consumed=BudgetConsumed(agent_turns=2))

    emitted = await _drain(
        kit.runtime._terminal_paused(
            kit.arms, launch=launch, steps=2,
            trigger_dimension=TRIGGER_LOCAL_TURNS,
        ),
    )

    assert [e.type for e in emitted] == [MODEL_REQUEST, RUN_PAUSED]
    paused = kit.since(mark)[-1]
    assert paused.run_id == RUN_ID
    assert paused.data["consumed"] == {
        "agent_turns": 2, "model_requests": 1, "total_tokens": None, "cost_usd": None,
    }
    assert paused.data["closeout_source"] == CLOSEOUT_DETERMINISTIC
    assert kit.result_holder[0].status == STATUS_PAUSED
    # 暂停不产生终态归因：run 尚未终结，tracer 的终态调用留给真正的终态
    assert kit.tracer.calls == []
    # 记忆形成只认终态（白名单无 paused）：暂停一次提交都不该有
    assert memory.submits == []

    # 收口已落盘 ⇒ 后续失败臂不得再补终态（单终态不变量的执行层落点）
    before = len(session.events)
    assert await _drain(
        kit.runtime._terminal_failed_run(
            kit.arms, steps=2, reason=STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
        ),
    ) == []
    assert session.events[before:] == []


@pytest.mark.asyncio
async def test_failed_run_arm_without_message_omits_the_key(session: Session) -> None:
    """同错熔断硬触发不带可读文案 ⇒ 事件里就没有 message 键（绝不编原因）。"""
    kit = _kit(session)
    await _drain(
        kit.runtime._terminal_failed_run(
            kit.arms, steps=1, reason=STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
        ),
    )
    terminal = session.events[-1]
    assert terminal.data["reason"] == STATUS_IDENTICAL_TOOL_FAILURE_LOOP
    assert "message" not in terminal.data


# ---------------------------------------------------------------------------
# context 超限臂
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_exceeded_arm_skips_memory_writeback(session: Session) -> None:
    """模型从未被调用 ⇒ 没有可抽取的对话内容，跳过 writeback（0 次是证据）。"""
    memory = _MemorySpy()
    kit = _kit(session, memory_writer=memory)

    emitted = await _drain(
        kit.runtime._terminal_context_exceeded(
            kit.arms, steps=0, error=ContextWindowExceededError("上下文超限"),
        ),
    )

    assert [e.type for e in emitted] == [RUN_FAILED]
    terminal = session.events[-1]
    assert terminal.data["reason"] == STATUS_CONTEXT_WINDOW_EXCEEDED
    assert terminal.data["message"] == "上下文超限"
    assert terminal.step_id == kit.arms.envelope_step(0)
    assert terminal.run_id == RUN_ID, "终态事件必须挂在本次 run 上"
    assert kit.result_holder[0].status == STATUS_CONTEXT_WINDOW_EXCEEDED
    assert memory.submits == []
    assert [name for name, _ in kit.tracer.calls] == ["context_build_completed", "run_failed"]


@pytest.mark.asyncio
async def test_context_exceeded_arm_closes_and_clears_the_handle(session: Session) -> None:
    """超限臂**收口即清口**（#285 / 残余 R2）：句柄不会被第二条收集臂再收一次。

    历史：#264 之前这条臂收口后**保留**句柄，于是"超限 + 消费方在终态帧上断连"
    （GeneratorExit 落在下面那次 yield 之后）这条**生产可达**路径会对同一 span 二次
    收口——`#265` 的等价重构逐字复刻了该形状（`keep_handle=True`）并把它登记为残余
    R2 / R3；`#285` 修掉它，本用例随新语义翻转（旧名
    `..._keeps_the_handle_it_closed`，此前钉的是"二次收口"这一缺陷事实）。

    钉四件事，缺一不可：① 收口后句柄为空；② 取消臂在同一 arms 上**不再**产生第二条
    `context_build_completed`；③ 终态语义不变（只有一条 `run/failed`）；④ 收口是
    **裸调**（残余 R1 在这一层的形状——只钉 `_Telemetry` 自己的缺省行为抓不住
    "臂把 `compacted_turn_count=None` 显式传回去"这种回归）。
    """
    ctx_kwargs: list[tuple[str, ...]] = []

    class _AritySpy(_RecordingTracer):
        def context_build_completed(self, span: Any, **kwargs: Any) -> None:
            ctx_kwargs.append(tuple(sorted(kwargs)))
            self._record("context_build_completed", span=span)

    kit = _kit(session, step_base=2, tracer=_AritySpy())
    kit.arms.telemetry.ctx_span = "span-ctx"

    emitted = await _drain(
        kit.runtime._terminal_context_exceeded(
            kit.arms, steps=3, error=ContextWindowExceededError("上下文超限"),
        ),
    )

    assert [e.type for e in emitted] == [RUN_FAILED]
    assert [name for name, _ in kit.tracer.calls] == ["context_build_completed", "run_failed"]
    # 收口即清口（与成功路径同形）
    assert kit.arms.telemetry.ctx_span is None
    assert ctx_kwargs == [()], "超限臂的收口是裸调（R1：不传 compacted_turn_count）"

    # 取消臂（终态帧之后断连）拿不到句柄 ⇒ **没有**第二次收口
    kit.runtime._terminal_cancelled(kit.arms, steps=3)

    assert [name for name, _ in kit.tracer.calls] == [
        "context_build_completed", "run_failed", "run_failed",
    ]
    assert ctx_kwargs == [()], "第二条收集臂根本不该调端口"


@pytest.mark.asyncio
async def test_context_exceeded_arm_append_failure_does_not_recollect_the_span(
    session: Session, tmp_path: Any,
) -> None:
    """R3 的第二条收集出口 = **异常臂**：超限臂落终态的 `append` 失败也只收一次（#285）。

    构造方式用的是仓库既有夹具（`tests/session/store_fixtures.py`，残余⑦ 指明的成本口径）：
    终态 `append` 抛 `SeqConflict` ⇒ 生产路径把异常交给顶层异常臂处理，异常臂经
    `arms.context(steps)` 取**快照**再收口。修 R2/R3 之前，快照里仍有那个已被超限臂
    收过的句柄 ⇒ 端口收到第二次 `context_build_completed`（与取消臂同源，只是出口不同）。

    只钉观测面（端口调用计数与句柄归属）：异常臂自己的持久化路径在同一次故障下也会失败
    （存储坏着），那不是本用例的被测面。
    """
    kit = _kit(session, step_base=2)
    kit.arms.telemetry.ctx_span = "span-ctx"
    session._store = RejectingStore(tmp_path)

    with pytest.raises(SeqConflict):
        await _drain(
            kit.runtime._terminal_context_exceeded(
                kit.arms, steps=3, error=ContextWindowExceededError("上下文超限"),
            ),
        )

    assert [name for name, _ in kit.tracer.calls] == ["context_build_completed", "run_failed"]
    assert kit.arms.telemetry.ctx_span is None

    # 顶层异常臂（生产里由 _drive 的 except 调用）——同一 arms、同一份快照语义
    with pytest.raises(SeqConflict):  # 存储故障仍在：异常臂的终态写同样失败
        await _drain(
            kit.runtime._terminal_exception(kit.arms, steps=3, error=SeqConflict("写不进去")),
        )

    assert [name for name, _ in kit.tracer.calls] == [
        "context_build_completed", "run_failed", "run_failed",
    ], "异常臂不得对已收口的 span 再收一次（R3）"


# ---------------------------------------------------------------------------
# 取消臂：丢弃自己的事件，但事实必须落盘
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_arm_discards_events_but_persists_them(session: Session) -> None:
    """取消臂的返回值被丢掉（生成器关闭中禁止产出），事件仍逐条落盘。

    顺带钉住 `interrupt_streams` 的两处 `self.steps + 1`（#263 遗留的未覆盖面）：
    - 开着的思考块被打断落 `reasoning/interrupted`，用 **envelope + 1**；
    - 待取的切换事实落 `model/fallback`，`step_id` 同为 envelope + 1；
    - 终态 `run/failed` 用 **envelope**（两个表达式**不同**，不是笔误）。
    """
    streamer = BlockStreamer(session)
    streamer.begin_run(RUN_ID)
    # 开着的思考块（注意：不能再调 offer_text——那会把思考块按 completed 收口）
    streamer.offer_reasoning("想了一半", step=1)
    coord = _PendingCoordinator(
        FallbackTransition(from_model="primary", to_model="fallback", reason="TimeoutError"),
    )
    kit = _kit(session, streamer=streamer, coord=coord, step_base=2)
    kit.arms.terminal.model_call_open = True
    # 取消发生在模型调用在途：ctx_span / generation 都在（观测收口的两个前置）。
    kit.arms.telemetry.ctx_span = "span-ctx"
    kit.arms.telemetry.generation = "gen-1"
    mark = len(session.events)
    envelope = kit.arms.envelope_step(3)

    assert kit.runtime._terminal_cancelled(kit.arms, steps=3) is None

    written = kit.since(mark)
    by_type = {e.type: e for e in written}
    assert REASONING_INTERRUPTED in by_type
    assert by_type[REASONING_INTERRUPTED].step_id == envelope + 1
    assert by_type[MODEL_FALLBACK].step_id == envelope + 1
    assert by_type[MODEL_FAILED].step_id == envelope + 1
    assert by_type[MODEL_FAILED].data["message"] == "model call cancelled"
    assert [e.type for e in written][-1] == RUN_FAILED
    assert by_type[RUN_FAILED].step_id == envelope
    assert by_type[RUN_FAILED].run_id == RUN_ID
    assert by_type[MODEL_FAILED].run_id == RUN_ID
    assert by_type[RUN_FAILED].data["reason"] == "cancelled"
    # 切换事实只**取走一次**：断的是产品行为（替代了原来那句
    # `drain_transitions() == []`——它只反映替身自己清空了队列，臂一次都不取也照样绿）
    assert coord.drains == 1
    assert [e.type for e in written].count(MODEL_FALLBACK) == 1
    # 观测：取消臂也要收口 generation（error_type="cancelled" 而非异常类型）。
    # 句柄按**身份**断言（B 轴 P3）：此前替身 `_record("model_call_failed", error_type=…)`
    # 把 generation 丢了，于是 `close_pending` 传 `None` 也全绿——"收口的是哪一个句柄"
    # 无人钉住；现在替身记下它，这里连值一起断（取消臂这一步收的就是在途那个）。
    assert ("model_call_failed", {"span": "gen-1", "error_type": "cancelled"}) in kit.tracer.calls
    assert ("context_build_completed", {"span": "span-ctx"}) in kit.tracer.calls
    assert ("run_failed", {"reason": "cancelled"}) in kit.tracer.calls
    assert [c[0] for c in kit.tracer.calls] == [
        "context_build_completed", "model_call_failed", "run_failed",
    ]
    assert not [c for c in kit.tracer.calls if c[0] == "run_completed"]
    # ctx 是**快照**：收口置空的是快照，臂上那份活值不动（臂写完即 return，
    # 回写没有读者——写回反而会掩盖"谁拥有这两个句柄"）。
    assert kit.arms.telemetry.ctx_span == "span-ctx"
    assert kit.arms.telemetry.generation == "gen-1"


@pytest.mark.asyncio
async def test_cancelled_arm_flushes_partial_text_with_the_interrupt_step(
    session: Session,
) -> None:
    """残余文本按"打断时的步号"落盘（部分内容保留，ADR-0016 §3.3）。

    `offer_text` 用 step=1、打断用 envelope + 1 ⇒ 落盘步号能区分"用哪个 step"
    （沿用旧步号 = 前端把中断内容折进上一轮）。
    """
    streamer = BlockStreamer(session)
    streamer.begin_run(RUN_ID)
    streamer.offer_text("答了一半", step=1)
    kit = _kit(session, streamer=streamer, step_base=2)
    kit.arms.terminal.model_call_open = True

    kit.runtime._terminal_cancelled(kit.arms, steps=3)

    delta = next(e for e in session.events if e.type == TEXT_DELTA)
    assert delta.data["delta"] == "答了一半"
    assert delta.step_id == kit.arms.envelope_step(3) + 1


@pytest.mark.asyncio
async def test_cancelled_arm_never_begun_writes_nothing(session: Session) -> None:
    """begin_run 之前被取消：没有 run 可终结，已写事件保持原样。"""
    streamer = BlockStreamer(session)
    kit = _kit(session, streamer=streamer, run_id=None)
    mark = len(session.events)

    kit.runtime._terminal_cancelled(kit.arms, steps=0)

    assert session.events[mark:] == []
    assert kit.result_holder == []


# ---------------------------------------------------------------------------
# 顶层异常臂
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_arm_classifies_only_while_the_model_call_is_open(
    session: Session,
) -> None:
    """分类窗口 = 模型调用在途（`model_call_open`）。

    本臂同时兜底工具/执行器异常，它们的错误文本可能**恰好引用**供应商标记
    （例如抓到的网页/计费文档里的字样）——不在途就不许误标（ADR-0033 §2.1）。
    """
    # 在途 + 命中分类表：run/failed 与 model/failed 都带固定可读文案
    kit = _kit(session)
    kit.arms.terminal.model_call_open = True
    mark = len(session.events)
    emitted = await _drain(
        kit.runtime._terminal_exception(
            kit.arms, steps=1, error=RuntimeError("upstream: billing account frozen"),
        ),
    )
    assert [e.type for e in emitted] == [MODEL_FAILED, RUN_FAILED]
    run_failed = kit.since(mark)[-1]
    assert run_failed.data["reason"] == PROVIDER_ACCOUNT_UNAVAILABLE_REASON
    assert run_failed.data["message"] == PROVIDER_ACCOUNT_UNAVAILABLE_MESSAGE
    assert kit.since(mark)[-2].data["message"] == PROVIDER_ACCOUNT_UNAVAILABLE_MESSAGE
    assert run_failed.run_id == RUN_ID
    # ⚠ 本条钉的是**既有缺陷的当前实测形状**，不是期望语义：#263 段登记的
    # failure_terminal(steps=…) 不被转发（Session.end_run 无 step_id 形参）⇒ max_steps /
    # 同错熔断 / 异常三条臂的终态 step_id 恒 None。让参数生效是行为变更（本行与 golden
    # 会一起变红，那是预期的红），不在本票——#264 的提取保持原状。
    assert run_failed.step_id is None
    assert kit.since(mark)[-2].run_id == RUN_ID, "model/failed 与终态挂同一个 run"

    # 同类文本但不在途：退回类型名，不误标 provider 归因
    other = _kit(session)
    mark = len(session.events)
    await _drain(
        other.runtime._terminal_exception(
            other.arms, steps=1, error=RuntimeError("tool output mentioned billing account"),
        ),
    )
    terminal = other.since(mark)[-1]
    assert terminal.data["reason"] == "RuntimeError"
    assert terminal.data["message"] == UNCLASSIFIED_FAILURE_MESSAGE.format(
        error_type="RuntimeError",
    )
    assert not [e for e in other.since(mark) if e.type == MODEL_FAILED], \
        "不在途不得写 model/failed（归因窗口未开）"


@pytest.mark.asyncio
async def test_failed_arm_yields_nothing_when_no_run_was_started(session: Session) -> None:
    """异常发生在 begin_run 之前：没有 run 可终结，臂自己安静退场。"""
    kit = _kit(session, run_id=None, step_base=0)
    mark = len(session.events)

    assert await _drain(
        kit.runtime._terminal_exception(kit.arms, steps=0, error=ValueError("用户消息写入失败")),
    ) == []
    assert session.events[mark:] == []


@pytest.mark.asyncio
async def test_failed_arm_uses_the_unclassified_message_for_unknown_errors(
    session: Session,
) -> None:
    """在途但分类表未命中：终态仍有可读兜底（#222），且只代入类型名（脱敏边界）。"""
    kit = _kit(session)
    kit.arms.terminal.model_call_open = True
    await _drain(kit.runtime._terminal_exception(kit.arms, steps=0, error=ValueError("boom")))

    assert session.events[-1].data["message"] == UNCLASSIFIED_FAILURE_MESSAGE.format(
        error_type="ValueError",
    )
    assert session.events[-2].data["message"] == "model call failed: ValueError"


# ---------------------------------------------------------------------------
# R4：收口段抛错不得让后续段消失（2026-09-22）
# ---------------------------------------------------------------------------


class _FailingStreamer:
    """`interrupt(step=…)` 抛错的 streamer 替身（R4 的故障注入点，鸭子类型）。

    生产里这里坐的是 `BlockStreamer`；本替身只实现终结臂真正调用的那一面，用来把
    "流收口段抛错"做成可复现的故障（修前它让两臂后面的每一行都不执行）。
    """

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[int] = []

    def interrupt(self, *, step: int) -> list[Any]:
        self.calls.append(step)
        raise self.error


class _ExplodingPortTracer(_RecordingTracer):
    """在 `model_call_failed` 上抛错的端口替身（R4 的第二段故障注入点）。"""

    def model_call_failed(self, generation: Any, *, error_type: str) -> None:
        raise RuntimeError("观测端口炸了")


@pytest.mark.asyncio
async def test_failed_arm_keeps_everything_after_the_failing_stream_stage(
    session: Session,
) -> None:
    """R4（异常臂出口）：流收口段抛错时，观测收口与终态事件仍然执行。

    修前实测形状：`interrupt_streams()` 一抛错，本臂后面的每一行都不执行 ⇒ 在途的
    ctx_span / generation **0 次收口**、session 里也**没有** `run/failed`（消费方只看到
    一个异常）。本用例钉三件事：① 端口收到三条收口调用（context_build_completed /
    model_call_failed / run_failed）；② 终态 `run/failed` 落盘；③ 第一处异常在**全部
    收尾跑完后**原样再抛（类型与文本都不替换）。
    """
    streamer = _FailingStreamer(RuntimeError("streamer 收口炸了"))
    kit = _kit(session, streamer=streamer, step_base=2)
    kit.arms.terminal.model_call_open = True
    kit.arms.telemetry.ctx_span = "span-ctx"
    kit.arms.telemetry.generation = "gen-1"
    mark = len(session.events)

    with pytest.raises(RuntimeError, match="streamer 收口炸了"):
        await _drain(
            kit.runtime._terminal_exception(kit.arms, steps=3, error=ValueError("模型调用炸了")),
        )

    assert streamer.calls == [kit.arms.envelope_step(3) + 1], \
        "流收口段（interrupt）确实被走到过——故障注入点有效"
    assert [name for name, _ in kit.tracer.calls] == [
        "context_build_completed", "model_call_failed", "run_failed",
    ], "观测收口不得被前一段的故障跳过（R4）"
    written = kit.since(mark)
    assert [e.type for e in written] == [MODEL_FAILED, RUN_FAILED]
    assert written[-1].data["reason"] == "ValueError", \
        "归因仍来自 run 的原始错误，不因收尾段故障而变"


@pytest.mark.asyncio
async def test_cancelled_arm_keeps_everything_after_the_failing_stream_stage(
    session: Session,
) -> None:
    """R4（取消臂出口）：同一故障在取消臂上同样不得让收口与终态事件消失。

    取消臂是**另一条出口**（生成器关闭中调用、事件被丢弃），与异常臂各写一遍才是这族
    缺口的完整判据面（与 R2/R3 的处置口径一致）。
    """
    streamer = _FailingStreamer(RuntimeError("streamer 收口炸了"))
    kit = _kit(session, streamer=streamer, step_base=2)
    kit.arms.terminal.model_call_open = True
    kit.arms.telemetry.ctx_span = "span-ctx"
    kit.arms.telemetry.generation = "gen-1"
    mark = len(session.events)

    with pytest.raises(RuntimeError, match="streamer 收口炸了"):
        kit.runtime._terminal_cancelled(kit.arms, steps=3)

    assert streamer.calls == [kit.arms.envelope_step(3) + 1]
    assert [name for name, _ in kit.tracer.calls] == [
        "context_build_completed", "model_call_failed", "run_failed",
    ]
    written = kit.since(mark)
    assert [e.type for e in written] == [MODEL_FAILED, RUN_FAILED]
    assert written[-1].data["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_arm_still_writes_the_terminal_event_when_the_port_raises(
    session: Session,
) -> None:
    """R4（第二段故障）：观测收口自己抛错时，终态事件仍然落盘。

    与上一条的差别是故障段不同（`close_observability` 里的端口调用）：它证明"逐段兜底"
    覆盖的是整条收尾序列，而不是只护住 `interrupt_streams` 这一段。
    """
    kit = _kit(session, step_base=2, tracer=_ExplodingPortTracer())
    kit.arms.terminal.model_call_open = True
    kit.arms.telemetry.ctx_span = "span-ctx"
    kit.arms.telemetry.generation = "gen-1"
    mark = len(session.events)

    with pytest.raises(RuntimeError, match="观测端口炸了"):
        kit.runtime._terminal_cancelled(kit.arms, steps=3)

    assert [name for name, _ in kit.tracer.calls] == ["context_build_completed"], \
        "抛错那一步之前仍执行过；它之后的 run_failed 没到（故障确实发生在段中间）"
    written = kit.since(mark)
    assert [e.type for e in written] == [MODEL_FAILED, RUN_FAILED], \
        "终态事件不得因收口段故障而消失（R4）"
    assert written[-1].data["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_both_stages_failing_re_raises_the_first_one_after_running_both(
    session: Session,
) -> None:
    """两段都炸：`raise_first()` 还原**第一处**，且第二段照样跑到（不是短路退出）。

    "先到先得"是 `_TerminalStages` 的契约之一（只存第一处）。不钉住的话，把它改成
    "每次都覆盖"不会有任何用例变红——而调用方看到的失败会从"流收口的错"翻成"观测收口的
    错"，归因随之翻转（排障时先看到的是完全不相干的那一段）。
    """
    streamer = _FailingStreamer(RuntimeError("streamer 收口炸了"))
    kit = _kit(session, streamer=streamer, step_base=2, tracer=_ExplodingPortTracer())
    kit.arms.terminal.model_call_open = True
    kit.arms.telemetry.ctx_span = "span-ctx"
    kit.arms.telemetry.generation = "gen-1"
    mark = len(session.events)

    with pytest.raises(RuntimeError, match="streamer 收口炸了"):
        kit.runtime._terminal_cancelled(kit.arms, steps=3)

    assert streamer.calls == [kit.arms.envelope_step(3) + 1], "第一段（流收口）跑过"
    assert [name for name, _ in kit.tracer.calls] == ["context_build_completed"], \
        "第二段也跑到了（否则不会有第二处异常）；它内部抛错，故只到这一步"
    written = kit.since(mark)
    assert [e.type for e in written] == [MODEL_FAILED, RUN_FAILED], \
        "两段皆炸也不影响终态事件落盘"


@pytest.mark.asyncio
async def test_failed_arm_still_writes_the_terminal_event_when_the_port_raises(
    session: Session,
) -> None:
    """R4（异常臂第二段）：观测收口自己抛错时，`run/failed` 仍然落盘。

    **为什么异常臂要单独一遍**（两轴审查 B 轴 P2）：两臂的收尾**不是同一段代码**——
    取消臂逐段直呼，异常臂在段之间把收口产出的事件逐条镜像给流消费者。把
    `_terminal_exception` 里那句 `stages.run("close_observability", …)` 换回直呼
    （`for streamed in ctx.close_observability(…)`）时，取消臂的同类用例
    （`test_cancelled_arm_still_writes_the_terminal_event_when_the_port_raises`）
    **仍然全绿**，只有本用例变红：异常在第二段当场穿透，`failure_terminal` 那几行
    再也到不了，`run/failed` 随之消失——正是 R4 要堵的形状（四轮复验实测：那处改动让
    arms 文件内 28 条保持绿、AC5 五文件集 1 红 / 301 绿，红的正是本用例）。
    """
    kit = _kit(session, step_base=2, tracer=_ExplodingPortTracer())
    kit.arms.terminal.model_call_open = True
    kit.arms.telemetry.ctx_span = "span-ctx"
    kit.arms.telemetry.generation = "gen-1"
    mark = len(session.events)

    with pytest.raises(RuntimeError, match="观测端口炸了"):
        await _drain(
            kit.runtime._terminal_exception(kit.arms, steps=3, error=ValueError("模型调用炸了")),
        )

    assert [name for name, _ in kit.tracer.calls] == ["context_build_completed"], \
        "抛错那一步之前仍执行过；它之后的 run_failed 没到（故障确实发生在段中间）"
    written = kit.since(mark)
    assert [e.type for e in written] == [MODEL_FAILED, RUN_FAILED], \
        "终态事件不得因收口段故障而消失（R4）"
    assert written[-1].data["reason"] == "ValueError", \
        "归因仍来自 run 的原始错误，不因收尾段故障而变"


def test_a_failing_stage_is_reported_in_the_structured_log(caplog: pytest.LogCaptureFixture) -> None:
    """段抛错**不静默**：一条结构化日志带上段名 + 异常类型 + 文本（两轴审查 B 轴 P3）。

    类 docstring 承诺"记一条结构化日志（类型 + 文本；不静默）并继续跑后续段"，但删掉
    `_TerminalStages.run` 里那次 `log_event(...)` 后全部用例照绿——收尾故障只剩调用方
    看到的第一处异常，而"哪一段炸的""后续段已继续执行"这两个事实无人承载，排障时只能
    从原始异常的调用栈里猜。这里不经两条臂、直接调执行器，把日志面本身钉成判据。
    """
    stages = _TerminalStages()

    def boom() -> list[SessionEvent]:
        raise RuntimeError("收口炸了")

    with caplog.at_level(logging.WARNING, logger="agent_harness.agent"):
        assert stages.run("interrupt_streams", boom) == []

    records = [r for r in caplog.records if getattr(r, "event_type", None) == "system_log"]
    assert len(records) == 1, "收尾段故障必须留下恰好一条结构化日志"
    record = records[0]
    assert record.stage == "interrupt_streams"
    assert record.error_type == "RuntimeError"
    assert record.error_message == "收口炸了"
    assert record.outcome == "stage_failed"
    assert "interrupt_streams" in record.getMessage(), "段名也要在人类可读的消息里"
    assert stages.first is not None, "日志之外，第一处异常仍要留给 raise_first()"
