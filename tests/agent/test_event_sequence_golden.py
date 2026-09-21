"""#263（#247 子票）— AgentRuntime 的 run/run_stream 事件序列基线（golden）。

**问题**（票面）：`_drive` 的终结分支散在六处（context 超限 / completed /
max_steps / 同错熔断硬触发 / 取消 / 顶层异常），每一处只被各自的窄用例零散覆盖
（断言"这条事件在/不在"），**没有一条"整段序列长什么样、按什么顺序"的可执行基线**。
#264 要把终结臂从 `_drive` 里提出来，先得有能在提取前后逐字比对的事实。

**本文件只加基线**：不抽结构、不改事件词汇、不改运行时行为（票面 Scope Lock）。
所有序列都由探针实测得出后冻结，不是照着代码抄的。

判据来源——机制正本在规格 / ADR / 代码，本文只写"这段代码自己看不出来的操作约束"：

- 事件词汇与 durable / stream-only 划分的**唯一事实源**是 `session/event.py`
  （`RUN_TERMINAL_TYPES` :32、`EVENT_TYPES` :116、`STREAM_ONLY_TYPES` :167），
  权威枚举是它的生成物 `docs/EVENT_VOCABULARY.md`（有漂移守卫用例）。本文不另立
  词汇清单：只断言实发类型 ⊆ `EVENT_TYPES`、ephemeral ⟺ `STREAM_ONLY_TYPES`、
  终态 ∈ `RUN_TERMINAL_TYPES`。（03 §3 只解释语义、不枚举 run/* 类型；§3.3 是
  "勿按此实现"的草案节——两者都不作判据。）
- 03 §2：`step_id` 是信封里的**可选**字段（`step_id?`），语义是步号——而
  `runtime.py:632-671` 的实现是 `step_base = max(session.max_step_id,
  session.user_turn_count)`，即**跨轮全局连续**。单轮会话里 `step_base == 0`，
  于是 `step_base + steps` 与 `steps` 同值、换掉一个看不出来；多轮才分得开，
  故信封由单轮的 `terminal_step_id` + 多轮的
  `test_terminal_step_id_is_session_global_across_turns` 两处合起来钉。
- 03 §3：仅广播类型（`model/started`、`model/delta`）MUST NOT 落盘。
- 03 §4 / 01 §8：`tool/call` ↔ `tool/result` 必须按 `tool_call_id` 一一配对，
  不得留下 dangling call。**本文件能证明的范围**只到"本文件这些场景里配对完整、
  同 id 唯一、call 先于 result"——它**不是**"生产路径永不产生 dangling"的证人
  （那需要跨工具并发场景，本文件没有；真证人在 04 的 Tool Runtime 用例）。
- 04 §7 / §11：并行批次**允许**并发 ⇒ **跨工具的先后不是稳定事实**。本文件所有
  工具场景都是单工具，交错无从出现；配对的局部顺序（同一 id 的 call 先于 result）
  是稳定事实，予以断言；跨工具交错不钉（规格 SILENT）。
- 03 §3.1（#28）：checkpoint 是恢复辅助，**绝不写进事件流**（本文件用不含
  `checkpoint` 子串来钉它，含 `checkpoint/*` 词汇若被引入即红）。
- ADR-0016 §3.1 / §3.3（`docs/adr/0016-streaming-ui-detached-run.md`）：
  `text/delta` 是合帧事件，切分点由 30ms 窗口决定 ⇒ 它的**条数**不是稳定事实。
  基线把连续同类帧合并成一个位置（`_collapse`），只钉"出现在哪个位置"。本文件用的
  `ScriptedModel.astream` 在 chunk 之间不 await，窗口不可能在流中途到期，故条数实测
  恒为 1；合并只是让基线不依赖这个巧合。**合并只豁免合帧族**：其余每个类型的
  **条数**都由 `Counter` 精确比对（见下），否则"同一事件 append 两次 + 镜像两次"
  这整类回归会静默通过。

**计数也钉**（两条独立 review 轴都指出过这里曾有洞）：`_collapse` 吸收的只是合帧族
的条数；`test_*_counts_match_golden` 对三条通道（durable / emitted / discarded）逐
类型精确计数比对。理由是 #264 的验收标准逐字要求"次数与顺序不变"，而提取终结臂最
典型的落地事故正是"把 append 搬进被提取的方法，调用点又留一次"。

**信封也钉**：`Scenario.terminal_step_id` 逐场景冻结终态事件的 `step_id`。各臂形状
本就**不一致**（`end_run` 派不带 step_id ⇒ None；context 臂与取消臂显式带
`step_base + steps` ⇒ 0）——#264 若"顺手统一"它们，基线必须变红。

**接线是冻结的**：默认场景用 `ToolExecutor(registry)`，即**无 Ledger** ⇒
`tracks_operations=False` ⇒ `model/completed` 在工具批次之前立即落盘。生产接线
（`assembly.py:368`）**总是**传 `operation_ledger`，时序不同（`tool/call` 先于
`model/completed`）——`tool_ok_with_ledger` 场景单独钉住这个差异。Ledger 必须先
`await initialize()`（否则写 Ledger 失败、直接走异常臂），故它的 builder 是 async。

**memory 提交也钉**：每个场景都接一个计数替身（`memory_writer=`），断言 `submit`
的**次数**与**被提交的事件类型序列**。context 臂跳过记忆写入（`runtime.py:736-751`
在 `_write_memories` 之前 return）是当前事实，不是巧合；只有把替身接上，"0 次"
才是证据而不是没测。

AC 映射（票面 §二值 AC）：

- AC1 durable 与 ephemeral 序列**分开**断言 → `test_durable_*` / `test_emitted_*`
  与 `test_durable_frames_mirror_the_persisted_log`（镜像只覆盖 durable 通道）。
- AC2 取消臂丢弃语义显式 → `test_cancel_arm_discards_while_exception_arm_yields`。
- AC3 tool pair + fallback + checkpoint + 终态顺序全覆盖 → 14 个场景表
  + `test_tool_calls_are_paired_by_id` + `test_terminal_is_unique_and_last`
  + `test_memory_writeback_submits_the_declared_events`。
- AC4 变异任一终结臂 → 对应基线变红：红证在 `.workbuddy`（不进仓库）。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.agent.guards import RepeatedToolFailureGuard
from agent_harness.agent.types import (
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
    STATUS_MAX_STEPS_EXCEEDED,
)
from agent_harness.context.compactor import ContextWindowExceededError
from agent_harness.model.fallback import TwoLevelFallbackPolicy
from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_DELTA,
    MODEL_FAILED,
    MODEL_FALLBACK,
    MODEL_STARTED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    SESSION_STARTED,
    STREAM_ONLY_TYPES,
    TEXT_DELTA,
    TOOL_CALL,
    TOOL_FAILURE_GUARD,
    TOOL_RESULT,
    USER_MESSAGE,
    Session,
)
from agent_harness.session.event import EVENT_TYPES, RUN_TERMINAL_TYPES
from agent_harness.storage import SqliteOperationLedger
from agent_harness.tooling import (
    ErrorCode,
    Tool,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

PROMPT = "hi"

# 只在流式路径上存在的事实：run() 走 ainvoke 不会产生它们（ADR-0016 边界）。
# 与 tests/agent/test_run_stream.py 同一口径。
STREAM_FACT_TYPES = frozenset({
    TEXT_DELTA, "reasoning/started", "reasoning/delta",
    "reasoning/completed", "reasoning/interrupted",
})

# 终态 data 里"给人读的文案"：**键必须在、值必须是非空 str**，但措辞不逐字钉——
# 措辞不是序列事实（max_steps 的具体文案另有 tests/agent/test_agent_loop.py 精确断言）。
PROSE = "<prose>"
PROSE_KEYS = frozenset({"message"})

# 取消臂的 reason 没有导出常量（正本是 `_RunFinalizer.cancelled_terminal` 的默认形参，
# 见 agent/types.py），故在此显式命名一次，避免同一字面量散落在多个场景里。
CANCEL_REASON = "cancelled"


# ---------------------------------------------------------------------------
# 替身：只做"注入一种故障"，不模拟逻辑
# ---------------------------------------------------------------------------


class _EchoArgs(BaseModel):
    text: str = Field(default="x", description="回显文本")


class _EchoTool(Tool):
    """成功工具：READ_ONLY、无副作用。"""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "原样回显文本的测试工具。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EchoArgs

    async def execute(self, args: _EchoArgs) -> ToolResult:
        return ToolResult.success(message=args.text, data={"text": args.text})


class _FailingEchoTool(_EchoTool):
    """失败工具：ok=False 的普通业务失败（不是执行器异常）。"""

    async def execute(self, args: _EchoArgs) -> ToolResult:
        return ToolResult.failure(
            message="boom", error_code=ErrorCode.TOOL_EXECUTION_ERROR.value,
        )


class _ExplodingModel:
    """Provider 中断：ainvoke / astream 都抛错。"""

    async def ainvoke(self, messages: list, **kwargs: Any) -> AIMessage:
        raise RuntimeError("模拟 API 中断")

    async def astream(self, messages: list, **kwargs: Any):
        raise RuntimeError("模拟 API 中断")
        yield AIMessage(content="")  # 不可达：仅为声明 async generator


class _BlockingModel:
    """模型调用永久挂起：让取消能精确打在生成器的悬挂点上。"""

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def ainvoke(self, messages: list, **kwargs: Any) -> AIMessage:
        await self.gate.wait()
        return AIMessage(content="never")

    async def astream(self, messages: list, **kwargs: Any):
        await self.gate.wait()
        yield AIMessage(content="never")


class _FailOnceModel:
    """首次调用抛瞬时错误，之后委托 inner（触发 model/fallback 切换）。"""

    def __init__(self, inner: Any, error: BaseException, fail_times: int = 1) -> None:
        self._inner = inner
        self._error = error
        self._fail_times = fail_times
        self.calls = 0

    async def ainvoke(self, messages: list, **kwargs: Any) -> AIMessage:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        return await self._inner.ainvoke(messages, **kwargs)

    async def astream(self, messages: list, **kwargs: Any):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        async for chunk in self._inner.astream(messages, **kwargs):
            yield chunk


class _ExplodingContextBuilder:
    """投影阶段就超限：模型从未被调用（无 model/* 事件）。"""

    async def build(self, session: Any) -> list:
        raise ContextWindowExceededError("context window exceeded: 上下文超限")


class _ExplodingCheckpointPolicy:
    """checkpoint 落盘故障：只是恢复辅助，不得毒化 run（_save_checkpoint 吞掉）。"""

    async def maybe_save(self, session: Any, boundary_type: Any) -> Any:
        raise OSError("checkpoint 存储不可写")


class _CountingMemoryWriter:
    """memory_writer 替身：只记"提交了几次、提交了哪些类型"，不做任何写入。

    `MemoryWriteback.submit` 是同步方法（`memory/writeback.py`），接线点
    `runtime.py:1316-1326` 按 `self._memory_writer is not None` 判断是否提交——
    所以替身接上之后的"0 次"才是证据（臂跳过了记忆写入），而不是"没测"。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def submit(self, session: Session, events: list[Any]) -> None:
        self.calls.append(tuple(e.type for e in events))


@dataclass
class _Wiring:
    """场景构造所需的运行期资源（由用例创建，builder 只消费）。

    `tmp_path` 给需要落盘资源的场景（Ledger 的 sqlite 文件）；`memory` 是记忆
    提交计数替身，**每个场景都接**，这样"提交 0 次"也是被断言过的事实。
    """

    tmp_path: Any
    memory: _CountingMemoryWriter


def _registry(tool: Tool | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool or _EchoTool())
    return registry


def _runtime(model: Any, *, registry: ToolRegistry | None = None, **kw: Any) -> AgentRuntime:
    reg = registry if registry is not None else _registry()
    return AgentRuntime(model=model, registry=reg, executor=ToolExecutor(reg), **kw)


def _tool_call(idx: int, args: dict | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"tc{idx}", "name": "echo", "args": args or {"text": f"t{idx}"}}],
    )


# ---------------------------------------------------------------------------
# 驱动：run_stream 的四种收尾方式
# ---------------------------------------------------------------------------

Driver = Callable[[AgentRuntime, Session], Awaitable[list[AgentEvent]]]
# builder 可以是 async 的：Ledger 接线的场景必须先 await ledger.initialize()
# （不 initialize 就写 Ledger 会失败，run 直接走异常臂，序列完全不同）。
Builder = Callable[[_Wiring], AgentRuntime | Awaitable[AgentRuntime]]


async def _build(scenario: Scenario, wiring: _Wiring) -> AgentRuntime:
    runtime = scenario.build(wiring)
    return await runtime if inspect.isawaitable(runtime) else runtime


async def _drive_full(runtime: AgentRuntime, session: Session) -> list[AgentEvent]:
    """跑到底：正常路径（含逐条终结臂自己 return 的路径）。"""
    return [e async for e in runtime.run_stream(session, PROMPT)]


def _close_after(frames: int) -> Driver:
    """消费 N 帧后 aclose → GeneratorExit 打进当时的悬挂点。"""

    async def drive(runtime: AgentRuntime, session: Session) -> list[AgentEvent]:
        agen = runtime.run_stream(session, PROMPT)
        seen: list[AgentEvent] = []
        async for frame in agen:
            seen.append(frame)
            if len(seen) >= frames:
                break
        await agen.aclose()
        await asyncio.sleep(0)
        return seen

    return drive


async def _close_unstarted(runtime: AgentRuntime, session: Session) -> list[AgentEvent]:
    """从未 __anext__ 就 aclose：生成器体一次都没跑。"""
    agen = runtime.run_stream(session, PROMPT)
    await agen.aclose()
    await asyncio.sleep(0)
    return []


async def _cancel_while_blocked(runtime: AgentRuntime, session: Session) -> list[AgentEvent]:
    """模型挂起时取消消费者 → CancelledError 打进 `_drive` 的 await 点。"""
    agen = runtime.run_stream(session, PROMPT)
    seen: list[AgentEvent] = []

    async def consume() -> None:
        async for frame in agen:
            seen.append(frame)

    consumer = asyncio.create_task(consume())
    for _ in range(300):  # 等三帧出齐（user/message, run/started, model/started）
        await asyncio.sleep(0.01)
        if len(seen) >= 3:
            break
    if len(seen) < 3:  # 轮询没等到 → 取消点会比基线早，错误信息必须指名道姓
        pytest.fail(f"取消前只等到 {len(seen)} 帧（需 3 帧），本场景的基线不成立")
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer
    await asyncio.sleep(0)
    return seen


# ---------------------------------------------------------------------------
# 基线表：全部由探针实测冻结（.workbuddy/probe_263_sequences.py、
# probe_263_fixdata.py、probe_263_multiturn.py）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """一个场景的两条通道 + 它的终态与信封。

    durable  = 落盘事实的类型序列（不含 session/started；连续同类已合并）
    emitted  = 流里 yield 的帧的类型序列（同上口径）
    discarded= 落盘了但**没进流**的那一段（取消臂的丢弃语义；正常路径为空）
    terminal = 期望的唯一终态类型；None = run 从未开始（不许补终结）
    terminal_payload   = 终态 data 的**整个键集**与逐键取值（各终结臂的**身份**）
    terminal_step_id   = 终态事件的信封 step_id（各臂形状不一致，见文件头）
    memory_submits     = memory_writer 收到的每次提交的**事件类型序列**（空 = 从未提交）
    """

    name: str
    note: str
    build: Builder
    drive: Driver
    durable: tuple[str, ...]
    emitted: tuple[str, ...]
    terminal: str | None
    terminal_payload: dict[str, Any]
    terminal_step_id: int | None = None
    discarded: tuple[str, ...] = ()
    memory_submits: tuple[tuple[str, ...], ...] = ()
    run_twin: bool = True  # 该场景能否用 run()（ainvoke）跑出同一套 durable 事实


def _simple_model() -> ScriptedModel:
    return ScriptedModel([AIMessage(content="answer")])


def _two_rounds_model() -> ScriptedModel:
    return ScriptedModel([_tool_call(1), AIMessage(content="done")])


async def _ledger_runtime(wiring: _Wiring) -> AgentRuntime:
    """生产接线：`ToolExecutor(registry, operation_ledger=...)`。

    Ledger 必须先 initialize（否则写 Ledger 抛错、run 直接走异常臂），故 builder
    是 async。注意这里的时序与无 Ledger 场景**不同**：`model/completed` 被推迟到
    工具批次之后（`runtime.py:924-936` 的 `defer_model_event`）。
    """
    registry = _registry()
    ledger = SqliteOperationLedger(wiring.tmp_path / "state.db")
    await ledger.initialize()
    return AgentRuntime(
        model=_two_rounds_model(),
        registry=registry,
        executor=ToolExecutor(registry, operation_ledger=ledger),
        memory_writer=wiring.memory,
    )


def _scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            name="success",
            note="无工具一轮到底",
            build=lambda w: _runtime(_simple_model(), memory_writer=w.memory),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, TEXT_DELTA,
                     MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "answer", "cost_usd": None,
                              "trace_id": None, "trace_url": None},
            memory_submits=((USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED,
                             RUN_COMPLETED),),
        ),
        Scenario(
            name="tool_ok",
            note="一轮工具（成功）后收尾；无 Ledger 接线 ⇒ model/completed 先于 tool/call",
            build=lambda w: _runtime(_two_rounds_model(), memory_writer=w.memory),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_COMPLETED, TOOL_CALL,
                     TOOL_RESULT, MODEL_STARTED, TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "done", "cost_usd": None,
                              "trace_id": None, "trace_url": None},
            memory_submits=((USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL,
                             TOOL_RESULT, MODEL_COMPLETED, RUN_COMPLETED),),
        ),
        Scenario(
            name="tool_error",
            note="一轮工具（ok=False）后收尾：失败只落在 tool/result 载荷里，不改序列",
            build=lambda w: _runtime(_two_rounds_model(),
                                     registry=_registry(_FailingEchoTool()),
                                     memory_writer=w.memory),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_COMPLETED, TOOL_CALL,
                     TOOL_RESULT, MODEL_STARTED, TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "done", "cost_usd": None,
                              "trace_id": None, "trace_url": None},
            memory_submits=((USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL,
                             TOOL_RESULT, MODEL_COMPLETED, RUN_COMPLETED),),
        ),
        Scenario(
            name="tool_ok_with_ledger",
            note="生产接线（带 Ledger）：model/completed 被推迟到工具批次之后",
            build=_ledger_runtime,
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, TOOL_CALL, MODEL_COMPLETED, TOOL_RESULT,
                     TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, TOOL_CALL, MODEL_COMPLETED,
                     TOOL_RESULT, MODEL_STARTED, TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "done", "cost_usd": None,
                              "trace_id": None, "trace_url": None},
            memory_submits=((USER_MESSAGE, RUN_STARTED, TOOL_CALL, MODEL_COMPLETED,
                             TOOL_RESULT, MODEL_COMPLETED, RUN_COMPLETED),),
        ),
        Scenario(
            name="fallback",
            note="primary 瞬时失败 → 切 fallback。text/delta 落在 model/fallback "
                 "**之前**是语句顺序派生的位置（end_step 早于 drain_transitions），"
                 "不是规格承诺；#264 若调整 drain 时点，这条基线会红——那是行为变更",
            build=lambda w: _runtime(
                _FailOnceModel(_simple_model(), TimeoutError("primary down")),
                fallback_model=ScriptedModel([AIMessage(content="fallback 的回答")]),
                fallback_policy=TwoLevelFallbackPolicy(),
                primary_model_name="primary-model",
                fallback_model_name="fallback-model",
                memory_writer=w.memory,
            ),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, TEXT_DELTA, MODEL_FALLBACK,
                     MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, TEXT_DELTA, MODEL_FALLBACK,
                     MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "fallback 的回答", "cost_usd": None,
                              "trace_id": None, "trace_url": None},
            memory_submits=((USER_MESSAGE, RUN_STARTED, MODEL_FALLBACK,
                             MODEL_COMPLETED, RUN_COMPLETED),),
        ),
        Scenario(
            name="max_steps",
            note="模型不收敛撞保险丝：第 2 轮不再执行工具，直接 run/failed",
            build=lambda w: _runtime(
                ScriptedModel([_tool_call(i) for i in range(6)]), max_steps=2,
                memory_writer=w.memory,
            ),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     MODEL_COMPLETED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_COMPLETED, TOOL_CALL,
                     TOOL_RESULT, MODEL_STARTED, MODEL_COMPLETED, RUN_FAILED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": STATUS_MAX_STEPS_EXCEEDED, "message": PROSE,
                              "trace_id": None, "trace_url": None},
            memory_submits=((USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL,
                             TOOL_RESULT, MODEL_COMPLETED, RUN_FAILED),),
        ),
        Scenario(
            name="context_exceeded",
            note="投影阶段超限：模型从未被调用，故流里一个 model/* 都没有；"
                 "该臂在 `_write_memories` 之前 return ⇒ 记忆提交 0 次",
            build=lambda w: _runtime(_simple_model(),
                                     context_builder=_ExplodingContextBuilder(),
                                     memory_writer=w.memory),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, RUN_FAILED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": STATUS_CONTEXT_WINDOW_EXCEEDED, "message": PROSE,
                              "trace_id": None, "trace_url": None},
            terminal_step_id=0,  # 该臂显式写 step_base + steps（其余臂走 end_run ⇒ None）
        ),
        Scenario(
            name="provider_error",
            note="模型调用抛错：异常臂**允许 yield**，终结帧会进流（与取消臂相反）；"
                 "异常臂也不写记忆",
            build=lambda w: _runtime(_ExplodingModel(), memory_writer=w.memory),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_FAILED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_FAILED, RUN_FAILED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": "RuntimeError", "message": PROSE,
                              "trace_id": None, "trace_url": None},
        ),
        Scenario(
            name="hard_guard",
            note="同指纹连续失败：第 1 次 soft（注入纠正语），第 2 次 hard（强制终结）",
            build=lambda w: _runtime(
                ScriptedModel([_tool_call(1, {"text": "same"}), _tool_call(2, {"text": "same"}),
                               AIMessage(content="done")]),
                registry=_registry(_FailingEchoTool()),
                failure_guard=RepeatedToolFailureGuard(soft_threshold=1, hard_threshold=1),
                memory_writer=w.memory,
            ),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     TOOL_FAILURE_GUARD, USER_MESSAGE, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     TOOL_FAILURE_GUARD, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_COMPLETED, TOOL_CALL,
                     TOOL_RESULT, TOOL_FAILURE_GUARD, USER_MESSAGE, MODEL_STARTED,
                     MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT, TOOL_FAILURE_GUARD, RUN_FAILED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
                              "trace_id": None, "trace_url": None},
            memory_submits=((USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL,
                             TOOL_RESULT, TOOL_FAILURE_GUARD, USER_MESSAGE, MODEL_COMPLETED,
                             TOOL_CALL, TOOL_RESULT, TOOL_FAILURE_GUARD, RUN_FAILED),),
        ),
        Scenario(
            name="checkpoint_error",
            note="checkpoint 落盘失败不毒化：序列必须与 success 逐条相同",
            build=lambda w: _runtime(_simple_model(),
                                     checkpoint_policy=_ExplodingCheckpointPolicy(),
                                     memory_writer=w.memory),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, TEXT_DELTA,
                     MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "answer", "cost_usd": None,
                              "trace_id": None, "trace_url": None},
            memory_submits=((USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED,
                             RUN_COMPLETED),),
        ),
        Scenario(
            name="generator_exit_pre_run",
            note="run/started 之前就关闭：run 未开始 → 不补终结，历史保持原样",
            build=lambda w: _runtime(_simple_model(), memory_writer=w.memory),
            drive=_close_after(1),
            durable=(USER_MESSAGE,),
            emitted=(USER_MESSAGE,),
            terminal=None,
            terminal_payload={},
            run_twin=False,
        ),
        Scenario(
            name="generator_exit_post_run",
            note="run/started 之后关闭：取消臂补终结但**不 yield**（丢弃段 = 终态）",
            build=lambda w: _runtime(_simple_model(), memory_writer=w.memory),
            drive=_close_after(2),
            durable=(USER_MESSAGE, RUN_STARTED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": CANCEL_REASON, "trace_id": None, "trace_url": None},
            terminal_step_id=0,
            discarded=(RUN_FAILED,),
            run_twin=False,
        ),
        Scenario(
            name="cancel_while_blocked",
            note="模型在途被取消：在途模型调用补 model/failed，两段都不进流",
            build=lambda w: _runtime(_BlockingModel(), memory_writer=w.memory),
            drive=_cancel_while_blocked,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_FAILED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": CANCEL_REASON, "trace_id": None, "trace_url": None},
            terminal_step_id=0,
            discarded=(MODEL_FAILED, RUN_FAILED),
            run_twin=False,
        ),
        Scenario(
            name="close_unstarted",
            note="从未 __anext__ 就关闭：生成器体一次没跑，零事件",
            build=lambda w: _runtime(_simple_model(), memory_writer=w.memory),
            drive=_close_unstarted,
            durable=(),
            emitted=(),
            terminal=None,
            terminal_payload={},
            run_twin=False,
        ),
    )


GOLDENS = {scenario.name: scenario for scenario in _scenarios()}
SCENARIO_IDS = tuple(GOLDENS)


def _collapse(types: list[str]) -> tuple[str, ...]:
    """把连续同类帧合并成一个位置。

    `text/delta` 的条数由合帧窗口（30ms 计时器）决定，不是稳定事实——
    基线只钉它出现在序列的哪个位置。**合并只豁免合帧族**：其余类型的条数由
    `_counted` 另外精确比对（否则"同一事件写两次 + 镜像两次"会静默通过）。
    """
    collapsed: list[str] = []
    for type_ in types:
        if not collapsed or collapsed[-1] != type_:
            collapsed.append(type_)
    return tuple(collapsed)


def _counted(types: list[str]) -> Counter[str]:
    """逐类型计数，但豁免**条数不稳定**的合帧族（`STREAM_FACT_TYPES`）。"""
    return Counter(t for t in types if t not in STREAM_FACT_TYPES)


def _shape(data: dict[str, Any]) -> dict[str, Any]:
    """终态载荷的**键集 + 取值**，其中人读文案（`PROSE_KEYS`）只保留形状。"""
    return {k: (PROSE if k in PROSE_KEYS else v) for k, v in data.items()}


# `tool/result` 的 content 里带**实测耗时**（`metadata.duration_ms`）——那是环境事实，
# 不是契约，两次执行本就不相等，跨入口比较时必须先抹掉（其余字段照比）。
_MEASURED_TOOL_RESULT_FIELDS = ("duration_ms", "total_duration_ms")


def _stable_data(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """抹掉载荷里的实测量，得到可跨执行比较的等价物。"""
    if event_type != TOOL_RESULT:
        return data
    content = json.loads(data["content"])
    metadata = content.get("metadata") or {}
    for field in _MEASURED_TOOL_RESULT_FIELDS:
        metadata.pop(field, None)
    return {**data, "content": json.dumps(content, sort_keys=True, ensure_ascii=False)}


def _persisted(session: Session) -> list[Any]:
    """落盘事实，去掉 Session.start 自己写的 seq 0（它不是 run 的产出）。"""
    return [e for e in session.events if e.type != SESSION_STARTED]


def _wiring(tmp_path: Any) -> _Wiring:
    return _Wiring(tmp_path=tmp_path, memory=_CountingMemoryWriter())


async def _run(
    name: str, tmp_path: Any, *, memory: _CountingMemoryWriter | None = None,
) -> tuple[Scenario, list[AgentEvent], Session]:
    scenario = GOLDENS[name]
    session = make_session(tmp_path)
    wiring = _Wiring(tmp_path=tmp_path, memory=memory or _CountingMemoryWriter())
    emitted = await scenario.drive(await _build(scenario, wiring), session)
    return scenario, emitted, session


# ---------------------------------------------------------------------------
# AC1：两条通道各自一条基线
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_durable_sequence_matches_golden(name: str, tmp_path: Any) -> None:
    """落盘（durable）序列逐条等于基线。"""
    scenario, _emitted, session = await _run(name, tmp_path)
    types = [e.type for e in _persisted(session)]
    assert _collapse(types) == scenario.durable, f"{scenario.name}：durable 序列偏离基线"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_durable_counts_match_golden(name: str, tmp_path: Any) -> None:
    """落盘序列的**逐类型条数**也等于基线（只豁免合帧族的条数）。

    这条挡的是"同一事件被 append 两次、也被镜像两次"——`_collapse` 对相邻重复
    完全无感，只钉顺序的话这整类回归会静默通过，而 #264 的验收标准要求"次数不变"。
    """
    scenario, _emitted, session = await _run(name, tmp_path)
    counted = _counted([e.type for e in _persisted(session)])
    expected = _counted(list(scenario.durable))
    assert counted == expected, (
        f"{scenario.name}：durable 计数偏离基线"
        f"（多出 {sorted((counted - expected).elements())}，"
        f"少了 {sorted((expected - counted).elements())}）"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_emitted_sequence_matches_golden(name: str, tmp_path: Any) -> None:
    """流（含 ephemeral 帧）序列逐条等于基线。"""
    scenario, emitted, _session = await _run(name, tmp_path)
    assert _collapse([e.type for e in emitted]) == scenario.emitted, \
        f"{scenario.name}：emitted 序列偏离基线"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_emitted_ephemeral_are_exactly_the_stream_only_types(
    name: str, tmp_path: Any,
) -> None:
    """只有 STREAM_ONLY_TYPES 能不带 seq；其余帧一律带 seq。"""
    _scenario, emitted, session = await _run(name, tmp_path)
    for event in emitted:
        if event.seq is None:
            assert event.type in STREAM_ONLY_TYPES, \
                f"{event.type} 不带 seq 但不在 STREAM_ONLY_TYPES 里"
            assert not event.is_durable
        else:
            assert event.type not in STREAM_ONLY_TYPES, \
                f"{event.type} 是仅广播类型，不得落盘（带 seq）"
            assert event.is_durable
    # 反向：声明为 stream-only 的类型绝不出现在落盘日志里
    persisted_types = {e.type for e in session.events}
    assert not (persisted_types & STREAM_ONLY_TYPES)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_emitted_counts_match_golden(name: str, tmp_path: Any) -> None:
    """流序列的**逐类型条数**也等于基线（`model/started` 的条数 = 模型调用轮数）。"""
    scenario, emitted, _session = await _run(name, tmp_path)
    counted = _counted([e.type for e in emitted])
    expected = _counted(list(scenario.emitted))
    assert counted == expected, (
        f"{scenario.name}：emitted 计数偏离基线"
        f"（多出 {sorted((counted - expected).elements())}，"
        f"少了 {sorted((expected - counted).elements())}）"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_every_event_type_is_in_the_vocabulary(name: str, tmp_path: Any) -> None:
    """实发类型必须都在 `session/event.py` 的词汇表内（本文不另立清单）。

    词汇 = 可落盘的 `EVENT_TYPES` + 仅广播的 `STREAM_ONLY_TYPES`（:167 的注释明确
    写了后者"NOT part of EVENT_TYPES"）。
    """
    _scenario, emitted, session = await _run(name, tmp_path)
    observed = {e.type for e in emitted} | {e.type for e in session.events}
    vocabulary = EVENT_TYPES | STREAM_ONLY_TYPES
    assert observed <= vocabulary, f"词汇表外的事件类型：{sorted(observed - vocabulary)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_durable_frames_mirror_the_persisted_log(name: str, tmp_path: Any) -> None:
    """镜像不变量：流里带 seq 的帧逐条等于落盘事实（type/seq/step_id/data 全等），
    且是前缀。

    取消 / 关闭场景里取消臂写的那一段**只落盘、不进流**——所以这里断言的是
    "被截断的前缀"，截断长度由 `discarded` 另外钉住（见下一个用例）。
    `step_id` 也进比较元组：信封（03 §2）是流与历史的同一份事实。
    """
    _scenario, emitted, session = await _run(name, tmp_path)
    frames = [e for e in emitted if e.seq is not None]
    persisted = [(e.type, e.seq, e.step_id, e.data) for e in _persisted(session)]
    assert [(e.type, e.seq, e.step_id, e.data) for e in frames] == persisted[:len(frames)]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_discarded_segment_is_exactly_the_declared_one(
    name: str, tmp_path: Any,
) -> None:
    """落盘了但没进流的那一段必须恰好是声明的那一段（正常路径 = 空）。

    顺序与**条数**都比：取消臂多补一条非终态事件（`_collapse` 看不见）也变红。
    """
    scenario, emitted, session = await _run(name, tmp_path)
    frames = [e for e in emitted if e.seq is not None]
    tail = [e.type for e in _persisted(session)][len(frames):]
    assert _collapse(tail) == scenario.discarded, \
        f"{scenario.name}：未进流的落盘段与声明不符"
    assert _counted(tail) == _counted(list(scenario.discarded)), \
        f"{scenario.name}：未进流的落盘段条数与声明不符"


# ---------------------------------------------------------------------------
# AC3：序列级不变量（与基线表相互独立，用来抓"表改对了但结构坏了"）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_seq_is_contiguous_from_zero(name: str, tmp_path: Any) -> None:
    """seq 从 0（session/started）连续递增，无空洞、无重复。"""
    _scenario, _emitted, session = await _run(name, tmp_path)
    seqs = [e.seq for e in session.events]
    assert seqs == list(range(len(seqs)))
    assert session.events[0].type == SESSION_STARTED


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_terminal_is_unique_and_last(name: str, tmp_path: Any) -> None:
    """终态至多一条；有终态时必须是最后一条，且载荷的**整个键集**与信封与基线相等。

    载荷必须钉：各失败臂的**终态类型相同**（都是 `run/failed`），只有 `reason`
    区分它们——只钉类型的话，改错臂（例如把熔断的 reason 写成 max_steps）不会被
    发现。人读文案（`message`）只钉"键在、值是非空 str"（措辞不是序列事实）。

    信封也必须钉：`end_run` 派不带 step_id（None），context 臂与取消臂显式带
    `step_base + steps`（单轮 = 0）——#264 若统一它们，这里变红。
    """
    scenario, _emitted, session = await _run(name, tmp_path)
    terminals = [e.type for e in session.events if e.type in RUN_TERMINAL_TYPES]
    if scenario.terminal is None:
        assert terminals == [], f"{scenario.name}：run 未开始却补了终结事件"
        return
    assert terminals == [scenario.terminal], f"{scenario.name}：终态不唯一或类型不对"
    last = session.events[-1]
    assert last.type == scenario.terminal, "终态必须是最后一条事实"
    assert _shape(last.data) == scenario.terminal_payload, \
        f"{scenario.name}：终态载荷的键集/取值偏离基线"
    assert last.step_id == scenario.terminal_step_id, \
        f"{scenario.name}：终态信封 step_id 偏离基线"
    for key in PROSE_KEYS & last.data.keys():
        assert isinstance(last.data[key], str) and last.data[key], \
            f"{scenario.name}：{key} 必须是非空字符串"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_tool_calls_are_paired_by_id(name: str, tmp_path: Any) -> None:
    """每个 tool/call 恰好一个同 id 的 tool/result，且 call 先于 result。

    跨工具的先后不钉：04 §7/§11 允许并行批次并发执行（本文件场景均为单工具）。
    """
    _scenario, _emitted, session = await _run(name, tmp_path)
    calls = [e for e in session.events if e.type == TOOL_CALL]
    results = [e for e in session.events if e.type == TOOL_RESULT]
    call_ids = [e.data["tool_call_id"] for e in calls]
    result_ids = [e.data["tool_call_id"] for e in results]
    # 先钉 id 唯一，再谈配对：字典/多重集比较对"同 id 出现两次"是盲的
    # （同名条目互相遮蔽），而重复的 tool/call 正是提取终结臂时的事故形态。
    assert len(set(call_ids)) == len(call_ids), f"{name}：tool/call id 重复"
    assert len(set(result_ids)) == len(result_ids), f"{name}：tool/result id 重复"
    assert sorted(result_ids) == sorted(call_ids), "tool_call_id 未一一配对"
    by_id = {e.data["tool_call_id"]: e for e in calls}
    for result in results:
        assert by_id[result.data["tool_call_id"]].seq < result.seq, "result 早于 call"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_checkpoint_facts_never_enter_the_event_stream(
    name: str, tmp_path: Any,
) -> None:
    """checkpoint 只是存储层恢复辅助：任何 checkpoint 事实都不得出现在事件流里。"""
    _scenario, emitted, session = await _run(name, tmp_path)
    everything = [e.type for e in emitted] + [e.type for e in session.events]
    assert not [t for t in everything if "checkpoint" in t]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_legacy_per_token_delta_is_never_emitted(name: str, tmp_path: Any) -> None:
    """model/delta 是保留词汇，运行时不发射（由合帧 text/delta 承接，ADR-0016）。"""
    _scenario, emitted, session = await _run(name, tmp_path)
    types = [e.type for e in emitted] + [e.type for e in session.events]
    assert MODEL_DELTA not in types


@pytest.mark.parametrize("name", SCENARIO_IDS)
def test_goldens_two_channels_are_mutually_consistent(name: str) -> None:
    """表内自洽：emitted 去掉仅广播帧、接上丢弃段，必须等于 durable（顺序 + 计数）。

    这条挡的是"我手抄基线表时抄错"——它不进运行时，只校验表本身。
    """
    scenario = GOLDENS[name]
    from_emitted = [t for t in scenario.emitted if t not in STREAM_ONLY_TYPES]
    combined = from_emitted + list(scenario.discarded)
    assert _collapse(combined) == scenario.durable, f"{scenario.name}：两条通道的表互不自洽"
    assert _counted(combined) == _counted(list(scenario.durable)), \
        f"{scenario.name}：两条通道的计数互不自洽"


# ---------------------------------------------------------------------------
# AC2 与对照：取消臂 / 异常臂的 yield 策略必须显式
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_arm_discards_while_exception_arm_yields(tmp_path: Any) -> None:
    """取消臂丢弃终结帧、异常臂 yield 终结帧——这是两臂唯一的差异。

    取消（GeneratorExit / CancelledError）时生成器正在关闭，禁止再产出：
    终结事件必须落盘，但绝不进流；异常臂相反，终结帧必须逐条镜像给消费者。
    """
    # 取消臂：两条路径（GeneratorExit / CancelledError）产出同一结论
    for name in ("generator_exit_post_run", "cancel_while_blocked"):
        _scenario, emitted, session = await _run(name, tmp_path / name)
        assert RUN_FAILED not in [e.type for e in emitted], f"{name}：取消臂不应 yield 终结帧"
        assert RUN_FAILED in [e.type for e in session.events], f"{name}：取消臂必须补终结帧"
        assert [e.data.get("reason") for e in session.events
                if e.type == RUN_FAILED] == [CANCEL_REASON]

    # 异常臂：同一条 run/failed 既落盘、也进流
    _scenario, emitted, session = await _run("provider_error", tmp_path / "provider_error")
    assert RUN_FAILED in [e.type for e in emitted], "异常臂必须 yield 终结帧"
    assert RUN_FAILED in [e.type for e in session.events]


@pytest.mark.asyncio
async def test_checkpoint_failure_does_not_change_the_sequence(tmp_path: Any) -> None:
    """checkpoint 落盘失败与成功的序列逐条相同（恢复辅助不参与 run 语义）。

    连信封（seq / step_id）一起比：失败不得让任何事件挪位置或换编号。
    """
    _s, _e, ok_session = await _run("success", tmp_path / "ok")
    _s2, _e2, broken_session = await _run("checkpoint_error", tmp_path / "broken")
    assert [(e.type, e.seq, e.step_id, e.data) for e in broken_session.events] == \
           [(e.type, e.seq, e.step_id, e.data) for e in ok_session.events]


@pytest.mark.asyncio
async def test_hard_guard_levels_are_soft_then_hard(tmp_path: Any) -> None:
    """同错熔断的两次触发必须如实标软/硬：软触发注入纠正语，硬触发强制终结。"""
    _scenario, _emitted, session = await _run("hard_guard", tmp_path)
    levels = [e.data["level"] for e in session.events if e.type == TOOL_FAILURE_GUARD]
    assert levels == ["soft", "hard"]


@pytest.mark.asyncio
async def test_tool_result_payload_carries_ok_flag(tmp_path: Any) -> None:
    """工具失败只改 tool/result 载荷里的 ok，不改事件序列（两个场景序列相同）。"""
    _s, _e, ok_session = await _run("tool_ok", tmp_path / "ok")
    _s2, _e2, fail_session = await _run("tool_error", tmp_path / "fail")

    def _ok_flags(session: Session) -> list[bool]:
        return [json.loads(e.data["content"])["ok"] for e in session.events
                if e.type == TOOL_RESULT]

    assert _ok_flags(ok_session) == [True]
    assert _ok_flags(fail_session) == [False]
    assert [e.type for e in ok_session.events] == [e.type for e in fail_session.events]


# ---------------------------------------------------------------------------
# run() 与 run_stream() 的 durable 事实必须一致（票面同时覆盖两条入口）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [n for n in SCENARIO_IDS if GOLDENS[n].run_twin])
async def test_run_and_run_stream_agree_on_durable_facts(name: str, tmp_path: Any) -> None:
    """run() 与 run_stream() 的落盘事实相同，唯一差别是流式路径多出 text/delta。

    比的是 `(type, 载荷)` 而不只是类型：`run()` 是另一条入口（ainvoke），载荷
    必须同源——只比类型的话，"同类型但载荷不同"会静默通过。载荷里唯一的实测量
    （`tool/result` 的 `duration_ms`）先被 `_stable_data` 抹掉再比。

    流式专属事实（ADR-0016 §3.1）不参与比对——`text/delta` 是合帧增量，
    `run()` 走 ainvoke 不会产生它。
    """
    scenario = GOLDENS[name]

    stream_session = make_session(tmp_path / "stream")
    await scenario.drive(await _build(scenario, _wiring(tmp_path / "stream")),
                         stream_session)
    stream_facts = [(e.type, _stable_data(e.type, e.data))
                    for e in _persisted(stream_session)
                    if e.type not in STREAM_FACT_TYPES]

    invoke_session = make_session(tmp_path / "invoke")
    await (await _build(scenario, _wiring(tmp_path / "invoke"))).run(
        invoke_session, PROMPT,
    )
    invoke_facts = [(e.type, _stable_data(e.type, e.data))
                    for e in _persisted(invoke_session)]

    assert stream_facts == invoke_facts
    assert _collapse([t for t, _ in invoke_facts]) == _collapse(
        [t for t in scenario.durable if t not in STREAM_FACT_TYPES]
    )


# ---------------------------------------------------------------------------
# 记忆提交：次数与被提交的事件类型序列（#264 AC3 的"次数与顺序不变"）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_memory_writeback_submits_the_declared_events(name: str, tmp_path: Any) -> None:
    """memory_writer 收到的每次提交，其事件类型序列必须与基线相等。

    每个场景都接了计数替身，所以"空元组"是"该臂跳过了记忆写入"的证据，而不是
    "没测"。context 臂（在 `_write_memories` 之前 return）与取消/异常臂正是后者。
    """
    scenario = GOLDENS[name]
    memory = _CountingMemoryWriter()
    await _run(name, tmp_path, memory=memory)
    assert tuple(memory.calls) == scenario.memory_submits, \
        f"{scenario.name}：记忆提交的次数/内容偏离基线（实际 {len(memory.calls)} 次）"
    # 提交的事件里不得含增量与思考（ADR-0016 / 02 §15：CoT 与合帧增量不进记忆，
    # 否则抽取器的 50 条窗口被重复内容挤占）
    for call in memory.calls:
        assert not [t for t in call if t in STREAM_FACT_TYPES]


# ---------------------------------------------------------------------------
# 信封：step_id 必须**跨轮全局连续**（单轮会话里 step_base == 0，看不出差别）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["context_exceeded", "generator_exit_post_run"])
async def test_terminal_step_id_is_session_global_across_turns(
    arm: str, tmp_path: Any,
) -> None:
    """第二轮的终结臂必须拿到 `step_base + steps`，不是每轮从 0 重数的 `steps`。

    单轮会话里 `step_base = max(max_step_id, user_turn_count) = 0`，两个表达式同值
    （`runtime.py:632-671`）；只有先在同一个 session 上跑完一轮，才会分岔——这正是
    TICKET_STEP_ID_COLLISION_MULTI_TURN 那类事故的形态。
    """
    scenario = GOLDENS[arm]
    session = make_session(tmp_path)
    await _runtime(_simple_model()).run(session, PROMPT)  # 第一轮：max_step_id = 1
    assert session.max_step_id == 1 and session.user_turn_count == 1, \
        "第一轮必须留下 step 1，否则本用例失去区分力"

    emitted = await scenario.drive(
        await _build(scenario, _wiring(tmp_path / "turn2")), session,
    )
    last = session.events[-1]  # 终态必须是最后一条事实（第一轮的 run/completed 在前）
    assert last.type == RUN_FAILED, f"第二轮应当以 run/failed 收尾，实际 {last.type}"
    assert last.step_id == 1, (
        f"第二轮 {arm} 的终态 step_id 必须是 step_base(1) + steps(0) = 1，"
        f"实际 {last.step_id}"
    )
    assert _collapse([e.type for e in emitted]) == scenario.emitted, \
        "第二轮驱动的 emitted 序列应与单轮基线一致"
