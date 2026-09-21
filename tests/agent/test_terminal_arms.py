"""`_TerminalArms` 与五条终结臂的单元测试（#264 / T11 第一切片）。

**为什么要有这一层**：`_drive` 的六个终结点原先各自把收尾序列写在 360 行里，只能靠
ScriptedModel 全链验证"整段序列长什么样"（`tests/agent/test_event_sequence_golden.py`，
#263 建立）。#264 把收尾提到以 `_TerminalArms` 为唯一入参的方法上，于是**每条臂自己的
契约**可以脱离 Agent Loop 直接调用验证。两层合起来才是本票的验收面：

- golden（全链）：提取前后事件序列逐字相同 ⇒ **等价性**；
- 本文件（单臂）：每条臂的写入顺序、信封表达式、单终态、记忆/观测调用 ⇒ **臂自身正确**。

界线：本文件**不碰**"走哪条臂、何时 return"——那是 `_drive` 的职责（由 golden 覆盖）。

本文件同时补掉两条 #263 留下的残余（见 `docs/SDD_TICKET_TRACKER.md` B-27 段残余①）：
`_TerminalContext.interrupt_streams` 里 `streamer.interrupt(step=…)` 与 `MODEL_FALLBACK`
的 `self.steps + 1` 此前**没有任何冻结序列覆盖**（14 个场景无一在终结臂里产出
`model/fallback`、也无场景产出 `reasoning/interrupted`）——这里用一个带待取切换事实的
替身 coordinator 与一个开着的思考块把它们钉住。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent_harness.agent.runtime import AgentRuntime, _RunFinalizer, _TerminalArms
from agent_harness.agent.streaming import BlockStreamer
from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
    STATUS_MAX_STEPS_EXCEEDED,
    AgentRunResult,
)
from agent_harness.context.compactor import ContextWindowExceededError
from agent_harness.model.failure import (
    PROVIDER_ACCOUNT_UNAVAILABLE_MESSAGE,
    PROVIDER_ACCOUNT_UNAVAILABLE_REASON,
    UNCLASSIFIED_FAILURE_MESSAGE,
)
from agent_harness.model.fallback import FallbackTransition
from agent_harness.session import (
    MODEL_FAILED,
    MODEL_FALLBACK,
    REASONING_INTERRUPTED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    TEXT_DELTA,
    Session,
)
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session

RUN_ID = "run-1"


# ---------------------------------------------------------------------------
# 替身：只实现终结臂真正调用的那几面
# ---------------------------------------------------------------------------


class _RecordingTracer:
    """记录臂调了哪些观测方法。

    golden 用 NullTracer（no-op）⇒ 观测调用在全链基线里**不可见**；而 #265 要动的
    正是这套 telemetry 调用点，所以在臂这一层把它钉住：哪条臂调什么、带什么值。
    （真实实现：`agent_harness/observability/port.py`，不抛的保证由 `_GuardedTracer`
    在 Core 单点强制——本替身不模拟那一层。）
    """

    trace_id = "trace-1"
    trace_url = "https://trace.example/trace-1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **fields: Any) -> None:
        self.calls.append((name, fields))

    def run_completed(self, final_text: str, usage_total: dict[str, int] | None = None) -> None:
        self._record("run_completed", final_text=final_text, usage_total=usage_total)

    def run_failed(self, reason: str) -> None:
        self._record("run_failed", reason=reason)

    def context_build_completed(
        self, span: Any, *, compacted_turn_count: int | None = None,
    ) -> None:
        self._record("context_build_completed", span=span)

    def model_call_failed(self, generation: Any, *, error_type: str) -> None:
        self._record("model_call_failed", error_type=error_type)


class _PendingCoordinator:
    """只实现 `drain_transitions()` 的 coordinator 替身。

    终结臂对 coordinator 的唯一调用就是取走切换事实（编排/重试行为在
    `tests/agent/test_model_fallback_runtime.py` 覆盖），故这里只喂一个待取的
    `FallbackTransition`，用来把"切换事实在终结臂里的落盘形状"（含信封表达式）钉住。
    """

    def __init__(self, *transitions: FallbackTransition) -> None:
        self._pending = list(transitions)
        # 调用计数：断言"臂只取一次"要断这个，不能断"第二次返回空"——后者断的是
        # 本替身自己的幂等实现，产品代码怎么改都会绿（自证式断言）。
        self.drains = 0

    def drain_transitions(self) -> list[FallbackTransition]:
        self.drains += 1
        out, self._pending = self._pending, []
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
    """一次装配的全部零件（臂 + 便于断言的句柄）。"""

    def __init__(
        self, runtime: AgentRuntime, session: Session, *, step_base: int = 0,
        run_id: str | None = RUN_ID, memory_event_start: int = 0,
        streamer: BlockStreamer | None = None, coord: Any = None,
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
            tracer=self.tracer,
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
    kit.arms.ctx_span = object()
    kit.arms.generation = object()

    ctx = kit.arms.context(steps=3)

    assert ctx.steps == kit.arms.envelope_step(3)
    assert ctx.session is session
    assert ctx.run_id == RUN_ID
    assert ctx.terminal is kit.arms.terminal
    assert ctx.streamer is streamer
    assert ctx.model_coord is coord
    assert ctx.tracer is kit.tracer
    assert ctx.ctx_span is kit.arms.ctx_span
    assert ctx.generation is kit.arms.generation


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
# 失败终态臂（max_steps / 同错熔断硬触发共用）
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
            kit.arms, steps=2, reason=STATUS_MAX_STEPS_EXCEEDED, message="连续 2 轮仍在请求工具",
        ),
    )

    assert [e.type for e in emitted] == [RUN_FAILED]
    terminal = kit.since(mark)[-1]
    assert terminal.data["reason"] == STATUS_MAX_STEPS_EXCEEDED
    assert terminal.data["message"] == "连续 2 轮仍在请求工具"
    assert terminal.run_id == RUN_ID, "终态事件必须挂在本次 run 上"
    assert kit.result_holder[0].status == STATUS_MAX_STEPS_EXCEEDED
    assert kit.tracer.calls == [("run_failed", {"reason": STATUS_MAX_STEPS_EXCEEDED})]
    assert memory.submits[0][1] == RUN_FAILED, "记忆抽取要在终态落盘之后"

    # 单终态不变量：再调一次不得补第二条终结（双终结 = 历史不可对账）
    before = len(session.events)
    assert await _drain(
        kit.runtime._terminal_failed_run(kit.arms, steps=2, reason=STATUS_IDENTICAL_TOOL_FAILURE_LOOP),
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
    kit.arms.ctx_span = "span-ctx"
    kit.arms.generation = "gen-1"
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
    # 切换事实只**取走一次**（断的是产品行为，不是替身的幂等实现）
    assert coord.drains == 1
    assert coord.drain_transitions() == []
    # 观测：取消臂也要收口 generation（error_type="cancelled" 而非异常类型）
    assert ("model_call_failed", {"error_type": "cancelled"}) in kit.tracer.calls
    assert ("context_build_completed", {"span": "span-ctx"}) in kit.tracer.calls
    assert ("run_failed", {"reason": "cancelled"}) in kit.tracer.calls
    assert [c[0] for c in kit.tracer.calls] == [
        "context_build_completed", "model_call_failed", "run_failed",
    ]
    assert not [c for c in kit.tracer.calls if c[0] == "run_completed"]
    # ctx 是**快照**：收口置空的是快照，臂上那份活值不动（臂写完即 return，
    # 回写没有读者——写回反而会掩盖"谁拥有这两个字段"）。
    assert kit.arms.ctx_span == "span-ctx"
    assert kit.arms.generation == "gen-1"


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
    assert run_failed.step_id is None, (
        "死参数实测：#263 段记录的 failure_terminal(steps=…) 不被转发（Session.end_run "
        "无 step_id 形参）⇒ 异常臂终态 step_id 恒 None。让参数生效是行为变更，不在本票。"
    )
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
