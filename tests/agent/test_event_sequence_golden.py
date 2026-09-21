"""#263（#247 子票）— AgentRuntime 的 run/run_stream 事件序列基线（golden）。

**问题**（票面）：`_drive` 的终结分支散在六处（context 超限 / completed /
max_steps / 同错熔断硬触发 / 取消 / 顶层异常），每一处只被各自的窄用例零散覆盖
（断言"这条事件在/不在"），**没有一条"整段序列长什么样、按什么顺序"的可执行基线**。
#264 要把终结臂从 `_drive` 里提出来，先得有能在提取前后逐字比对的事实。

**本文件只加基线**：不抽结构、不改事件词汇、不改运行时行为（票面 Scope Lock）。
所有序列都由探针实测得出后冻结，不是照着代码抄的。

判据来源——机制正本在规格 / ADR，本文只写"这段代码自己看不出来的操作约束"：

- 事件词汇与 durable / stream-only 划分的正本是 `session/event.py`（生成物
  `docs/EVENT_VOCABULARY.md`）。本文不另立词汇清单，只断言实发类型 ⊆ 该词汇、
  且 ephemeral ⟺ `STREAM_ONLY_TYPES`。
- 03 §3：仅广播类型（`model/started`、`model/delta`）MUST NOT 落盘；
  03 §3.3：终态事件只有 `run/completed` / `run/failed` / `run/interrupted`。
- 03 §4 / 01 §8：`tool/call` ↔ `tool/result` 必须按 `tool_call_id` 一一配对，
  不得留下 dangling call。
- 04 §7 / §11：并行批次**允许**并发 ⇒ **跨工具的先后不是稳定事实**。本文件所有
  工具场景都是单工具，交错无从出现；配对的局部顺序（同一 id 的 call 先于 result）
  是稳定事实，予以断言；跨工具交错不钉（规格 SILENT）。
- 03 §3.1（#28）：checkpoint 是恢复辅助，**绝不写进事件流**（本文件用不含
  `checkpoint` 子串来钉它，含 `checkpoint/*` 词汇若被引入即红）。
- ADR-0016 §3.1：`text/delta` 是合帧事件，切分点由 30ms 窗口决定 ⇒ 它的**条数**
  不是稳定事实。基线把连续同类帧合并成一个位置（`_collapse`），只钉"出现在哪个
  位置、合计 ≥1 条"。本文件用的 `ScriptedModel.astream` 在 chunk 之间不 await，
  窗口不可能在流中途到期，故条数实测恒为 1；合并只是让基线不依赖这个巧合。

AC 映射（票面 §二值 AC）：

- AC1 durable 与 ephemeral 序列**分开**断言 → `test_durable_*` / `test_emitted_*`
  与 `test_durable_frames_mirror_the_persisted_log`（镜像只覆盖 durable 通道）。
- AC2 取消臂丢弃语义显式 → `test_cancel_arm_discards_while_exception_arm_yields`。
- AC3 tool pair + fallback + checkpoint + 终态顺序全覆盖 → 13 个场景表
  + `test_tool_calls_are_paired_by_id` + `test_terminal_is_unique_and_last`。
- AC4 变异任一终结臂 → 对应基线变红：红证在 `.workbuddy`（不进仓库）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.agent.guards import RepeatedToolFailureGuard
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
from agent_harness.session.event import RUN_TERMINAL_TYPES
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

PROMPT = "hi"

# 只在流式路径上存在的事实：run() 走 ainvoke 不会产生它们（ADR-0016 边界）。
# 与 tests/agent/test_run_stream.py 同一口径。
STREAM_FACT_TYPES = frozenset({
    TEXT_DELTA, "reasoning/started", "reasoning/delta",
    "reasoning/completed", "reasoning/interrupted",
})


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
        return ToolResult.failure(message="boom", error_code="TOOL_EXECUTION_ERROR")


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
Builder = Callable[[], AgentRuntime]


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
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer
    await asyncio.sleep(0)
    return seen


# ---------------------------------------------------------------------------
# 基线表：全部由探针实测冻结（.workbuddy/probe_263_sequences.py）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """一个场景的两条通道 + 它的终态。

    durable  = 落盘事实的类型序列（不含 session/started；连续同类已合并）
    emitted  = 流里 yield 的帧的类型序列（同上口径）
    discarded= 落盘了但**没进流**的那一段（取消臂的丢弃语义；正常路径为空）
    terminal = 期望的唯一终态类型；None = run 从未开始（不许补终结）
    terminal_payload = 终态 data 里必须逐键相等的载荷（各终结臂的**身份**）
    """

    name: str
    note: str
    build: Builder
    drive: Driver
    durable: tuple[str, ...]
    emitted: tuple[str, ...]
    terminal: str | None
    terminal_payload: dict[str, Any]
    discarded: tuple[str, ...] = ()
    run_twin: bool = True  # 该场景能否用 run()（ainvoke）跑出同一套 durable 事实


def _simple_model() -> ScriptedModel:
    return ScriptedModel([AIMessage(content="answer")])


def _two_rounds_model() -> ScriptedModel:
    return ScriptedModel([_tool_call(1), AIMessage(content="done")])


def _scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            name="success",
            note="无工具一轮到底",
            build=lambda: _runtime(_simple_model()),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, TEXT_DELTA,
                     MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "answer"},
        ),
        Scenario(
            name="tool_ok",
            note="一轮工具（成功）后收尾",
            build=lambda: _runtime(_two_rounds_model()),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_COMPLETED, TOOL_CALL,
                     TOOL_RESULT, MODEL_STARTED, TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "done"},
        ),
        Scenario(
            name="tool_error",
            note="一轮工具（ok=False）后收尾：失败只落在 tool/result 载荷里，不改序列",
            build=lambda: _runtime(_two_rounds_model(), registry=_registry(_FailingEchoTool())),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_COMPLETED, TOOL_CALL,
                     TOOL_RESULT, MODEL_STARTED, TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "done"},
        ),
        Scenario(
            name="fallback",
            note="primary 瞬时失败 → 切 fallback：model/fallback 落在首次增量之后",
            build=lambda: _runtime(
                _FailOnceModel(_simple_model(), TimeoutError("primary down")),
                fallback_model=ScriptedModel([AIMessage(content="fallback 的回答")]),
                fallback_policy=TwoLevelFallbackPolicy(),
                primary_model_name="primary-model",
                fallback_model_name="fallback-model",
            ),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, TEXT_DELTA, MODEL_FALLBACK,
                     MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, TEXT_DELTA, MODEL_FALLBACK,
                     MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "fallback 的回答"},
        ),
        Scenario(
            name="max_steps",
            note="模型不收敛撞保险丝：第 2 轮不再执行工具，直接 run/failed",
            build=lambda: _runtime(
                ScriptedModel([_tool_call(i) for i in range(6)]), max_steps=2,
            ),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     MODEL_COMPLETED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_COMPLETED, TOOL_CALL,
                     TOOL_RESULT, MODEL_STARTED, MODEL_COMPLETED, RUN_FAILED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": "max_steps_exceeded"},
        ),
        Scenario(
            name="context_exceeded",
            note="投影阶段超限：模型从未被调用，故流里一个 model/* 都没有",
            build=lambda: _runtime(_simple_model(), context_builder=_ExplodingContextBuilder()),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, RUN_FAILED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": "context_window_exceeded"},
        ),
        Scenario(
            name="provider_error",
            note="模型调用抛错：异常臂**允许 yield**，终结帧会进流（与取消臂相反）",
            build=lambda: _runtime(_ExplodingModel()),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_FAILED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_FAILED, RUN_FAILED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": "RuntimeError"},
        ),
        Scenario(
            name="hard_guard",
            note="同指纹连续失败：第 1 次 soft（注入纠正语），第 2 次 hard（强制终结）",
            build=lambda: _runtime(
                ScriptedModel([_tool_call(1, {"text": "same"}), _tool_call(2, {"text": "same"}),
                               AIMessage(content="done")]),
                registry=_registry(_FailingEchoTool()),
                failure_guard=RepeatedToolFailureGuard(soft_threshold=1, hard_threshold=1),
            ),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     TOOL_FAILURE_GUARD, USER_MESSAGE, MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT,
                     TOOL_FAILURE_GUARD, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, MODEL_COMPLETED, TOOL_CALL,
                     TOOL_RESULT, TOOL_FAILURE_GUARD, USER_MESSAGE, MODEL_STARTED,
                     MODEL_COMPLETED, TOOL_CALL, TOOL_RESULT, TOOL_FAILURE_GUARD, RUN_FAILED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": "identical_tool_failure_loop"},
        ),
        Scenario(
            name="checkpoint_error",
            note="checkpoint 落盘失败不毒化：序列必须与 success 逐条相同",
            build=lambda: _runtime(_simple_model(), checkpoint_policy=_ExplodingCheckpointPolicy()),
            drive=_drive_full,
            durable=(USER_MESSAGE, RUN_STARTED, TEXT_DELTA, MODEL_COMPLETED, RUN_COMPLETED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED, TEXT_DELTA,
                     MODEL_COMPLETED, RUN_COMPLETED),
            terminal=RUN_COMPLETED,
            terminal_payload={"final_text": "answer"},
        ),
        Scenario(
            name="generator_exit_pre_run",
            note="run/started 之前就关闭：run 未开始 → 不补终结，历史保持原样",
            build=lambda: _runtime(_simple_model()),
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
            build=lambda: _runtime(_simple_model()),
            drive=_close_after(2),
            durable=(USER_MESSAGE, RUN_STARTED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": "cancelled"},
            discarded=(RUN_FAILED,),
            run_twin=False,
        ),
        Scenario(
            name="cancel_while_blocked",
            note="模型在途被取消：在途模型调用补 model/failed，两段都不进流",
            build=lambda: _runtime(_BlockingModel()),
            drive=_cancel_while_blocked,
            durable=(USER_MESSAGE, RUN_STARTED, MODEL_FAILED, RUN_FAILED),
            emitted=(USER_MESSAGE, RUN_STARTED, MODEL_STARTED),
            terminal=RUN_FAILED,
            terminal_payload={"reason": "cancelled"},
            discarded=(MODEL_FAILED, RUN_FAILED),
            run_twin=False,
        ),
        Scenario(
            name="close_unstarted",
            note="从未 __anext__ 就关闭：生成器体一次没跑，零事件",
            build=lambda: _runtime(_simple_model()),
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
    基线只钉它出现在序列的哪个位置。
    """
    collapsed: list[str] = []
    for type_ in types:
        if not collapsed or collapsed[-1] != type_:
            collapsed.append(type_)
    return tuple(collapsed)


def _persisted(session: Session) -> list[Any]:
    """落盘事实，去掉 Session.start 自己写的 seq 0（它不是 run 的产出）。"""
    return [e for e in session.events if e.type != SESSION_STARTED]


async def _run(name: str, tmp_path: Any) -> tuple[Scenario, list[AgentEvent], Session]:
    scenario = GOLDENS[name]
    session = make_session(tmp_path)
    emitted = await scenario.drive(scenario.build(), session)
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
async def test_durable_frames_mirror_the_persisted_log(name: str, tmp_path: Any) -> None:
    """镜像不变量：流里带 seq 的帧逐条等于落盘事实（type/seq/data 全等），且是前缀。

    取消 / 关闭场景里取消臂写的那一段**只落盘、不进流**——所以这里断言的是
    "被截断的前缀"，截断长度由 `discarded` 另外钉住（见下一个用例）。
    """
    _scenario, emitted, session = await _run(name, tmp_path)
    frames = [e for e in emitted if e.seq is not None]
    persisted = [(e.type, e.seq, e.data) for e in _persisted(session)]
    assert [(e.type, e.seq, e.data) for e in frames] == persisted[:len(frames)]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SCENARIO_IDS)
async def test_discarded_segment_is_exactly_the_declared_one(
    name: str, tmp_path: Any,
) -> None:
    """落盘了但没进流的那一段必须恰好是声明的那一段（正常路径 = 空）。"""
    scenario, emitted, session = await _run(name, tmp_path)
    frames = [e for e in emitted if e.seq is not None]
    tail = [e.type for e in _persisted(session)][len(frames):]
    assert _collapse(tail) == scenario.discarded, \
        f"{scenario.name}：未进流的落盘段与声明不符"


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
    """终态至多一条；有终态时必须是最后一条，且载荷与基线逐键相等。

    载荷必须钉：四条失败臂（max_steps / context / 熔断 / 取消）的**类型序列完全
    相同**，只有 `reason` 区分它们——只钉类型的话，改错臂（例如把熔断的 reason
    写成 max_steps）不会被发现。
    """
    scenario, _emitted, session = await _run(name, tmp_path)
    terminals = [e.type for e in session.events if e.type in RUN_TERMINAL_TYPES]
    if scenario.terminal is None:
        assert terminals == [], f"{scenario.name}：run 未开始却补了终结事件"
    else:
        assert terminals == [scenario.terminal]
        assert session.events[-1].type == scenario.terminal, "终态必须是最后一条事实"
        data = session.events[-1].data
        expected = scenario.terminal_payload
        assert {k: data.get(k) for k in expected} == expected, \
            f"{scenario.name}：终态载荷偏离基线"


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
    """表内自洽：emitted 去掉仅广播帧、接上丢弃段，必须等于 durable。

    这条挡的是"我手抄基线表时抄错"——它不进运行时，只校验表本身。
    """
    scenario = GOLDENS[name]
    from_emitted = [t for t in scenario.emitted if t not in STREAM_ONLY_TYPES]
    combined = _collapse(from_emitted + list(scenario.discarded))
    assert combined == scenario.durable, f"{scenario.name}：两条通道的表互不自洽"


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
                if e.type == RUN_FAILED] == ["cancelled"]

    # 异常臂：同一条 run/failed 既落盘、也进流
    _scenario, emitted, session = await _run("provider_error", tmp_path / "provider_error")
    assert RUN_FAILED in [e.type for e in emitted], "异常臂必须 yield 终结帧"
    assert RUN_FAILED in [e.type for e in session.events]


@pytest.mark.asyncio
async def test_checkpoint_failure_does_not_change_the_sequence(tmp_path: Any) -> None:
    """checkpoint 落盘失败与成功的序列逐条相同（恢复辅助不参与 run 语义）。"""
    _s, _e, ok_session = await _run("success", tmp_path / "ok")
    _s2, _e2, broken_session = await _run("checkpoint_error", tmp_path / "broken")
    assert [(e.type, e.data) for e in broken_session.events] == \
           [(e.type, e.data) for e in ok_session.events]


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

    流式专属事实（ADR-0016 §3.1）不参与比对——`text/delta` 是合帧增量，
    `run()` 走 ainvoke 不会产生它。
    """
    scenario = GOLDENS[name]

    stream_session = make_session(tmp_path / "stream")
    await scenario.drive(scenario.build(), stream_session)
    stream_types = [e.type for e in _persisted(stream_session) if e.type not in STREAM_FACT_TYPES]

    invoke_session = make_session(tmp_path / "invoke")
    await scenario.build().run(invoke_session, PROMPT)
    invoke_types = [e.type for e in _persisted(invoke_session)]

    assert stream_types == invoke_types
    assert _collapse(invoke_types) == _collapse(
        [t for t in scenario.durable if t not in STREAM_FACT_TYPES]
    )
