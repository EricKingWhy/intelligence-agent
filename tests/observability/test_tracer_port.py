"""#249（审计 Top 3）：Tracer/Span 端口 + NullTracer——观测缺席不再以 None 表达。

红证三条（各自可判）：
1. NullTracer 覆盖 run/model/context/tool 全生命周期：零参数可构造、方法零抛错、
   句柄可安全 update/end、trace_id/trace_url 如实 None；
2. 端口方法集对两个实现都闭合（RunTracer 是 adapter，NullTracer 是缺席实现）；
3. Runtime 未配置 sink 时**仍然**驱动端口（NullTracer 收到完整生命周期），
   而抛异常的 sink 不改变 run 事件流与终态（旁路故障隔离，不变量 #21）。
"""

from __future__ import annotations

import re
import typing
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import agent_harness.agent.runtime as runtime_module
from agent_harness.agent import AgentRuntime
from agent_harness.agent.types import (
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_IDENTICAL_TOOL_FAILURE_LOOP,
    STATUS_MAX_STEPS_EXCEEDED,
)
from agent_harness.context.builder import ContextWindowExceededError
from agent_harness.observability import LangfuseSink
from agent_harness.observability.port import NullTracer, Span, Tracer
from agent_harness.session import (
    MODEL_STARTED,
    RUN_COMPLETED,
    RUN_FAILED,
)
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.agent.test_repeated_tool_failure_loop import FailureTool, _tool_call
from tests.agent.test_runtime_failure_paths import ExplodingModel
from tests.conftest import make_session
from tests.observability.test_tool_tracing import _FlakyTool
from tests.observability.test_tracer import FakeRecorder, _tracer
from tests.observability.tracer_fixtures import RecordingNullTracer
from tests.scripted_model import ScriptedModel


def _drive_full_lifecycle(tracer: Tracer) -> None:
    """端口全生命周期调用脚本：run → context → model → tool → 终态。

    RunTracer 与 NullTracer 必须接受同一段调用——端口的意义正在于此。
    """
    tracer.run_started()
    ctx = tracer.context_build_started(step=1)
    assert ctx is not None
    ctx.update(metadata={})
    ctx.end()
    tracer.context_build_completed(ctx, compacted_turn_count=1)
    generation = tracer.model_call_started(step=1, messages=[], model="m")
    assert generation is not None
    generation.update(output="hi")
    generation.end()
    tracer.model_call_completed(
        generation, output_text="hi", usage={"total_tokens": 1}, duration_ms=5,
        finish_reason="stop", provider_request_id="req-1", response_model="m",
        fallback_transitions=None, tool_call_names=None,
    )
    tracer.model_call_failed(generation, error_type="Boom")
    tool_span = tracer.tool_span_started(
        tool_name="t", tool_call_id="call-1", args={}, is_delegate=False,
    )
    assert tool_span is not None
    tool_span.update(output="o")
    tool_span.end()
    tracer.tool_span_completed(
        tool_span, outcome="success", message="o", attempts=[{"attempt": 1}],
        session_id="sess-1", extra={"artifact_ref": "art-1"},
    )
    tracer.run_completed("done", usage_total={"total_tokens": 1})
    tracer.run_failed("boom")


def _plain_runtime(model: Any, sink: LangfuseSink | None = None) -> AgentRuntime:
    registry = ToolRegistry()
    return AgentRuntime(model, registry, ToolExecutor(registry), observability_sink=sink)


def _tool_round_runtime(sink: LangfuseSink | None) -> AgentRuntime:
    """一轮工具调用（_FlakyTool 第一次可重试失败 → 重试链）后收尾的 run。"""
    registry = ToolRegistry()
    registry.register(_FlakyTool())
    scripted = ScriptedModel([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "flaky", "args": {"value": 1},
                "id": "call_1", "type": "tool_call",
            }],
        ),
        AIMessage(content="工具回来了"),
    ])
    return AgentRuntime(scripted, registry, ToolExecutor(registry), observability_sink=sink)


#: 计时字段：ToolResult.metadata 里两次运行必然不同的值。
_TIMING_RE = re.compile(r'"((?:total_)?duration_ms)":\s*[0-9.eE+-]+')


def _timing_normalized(value: Any) -> Any:
    """把计时字段的**值**归零后比较（键名保留——改名/丢键仍会被发现）。

    含 tool/result content 这个 JSON 字符串里的（序列化后键名在字符串内部）。
    """
    if isinstance(value, str):
        return _TIMING_RE.sub(r'"\1":0', value)
    if isinstance(value, dict):
        return {
            key: (
                0 if key in ("duration_ms", "total_duration_ms")
                else _timing_normalized(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_timing_normalized(item) for item in value]
    return value


def test_null_tracer_lifecycle_is_inert_and_returns_usable_handles():
    """NullTracer：零参数可构造，全生命周期零抛错，句柄是对象而非 None。"""
    tracer = NullTracer()
    assert isinstance(tracer, Tracer)
    assert tracer.trace_id is None
    assert tracer.trace_url is None

    _drive_full_lifecycle(tracer)

    handle = tracer.model_call_started(step=1, messages=[], model=None)
    assert isinstance(handle, Span), "空 handle 必须是对象——调用方不再判空"


def test_port_surface_is_closed_for_both_implementations():
    """端口声明的每个方法，两个实现都提供（否则 Core 会在缺席实现上 AttributeError）。"""
    declared = sorted(typing.get_protocol_members(Tracer))
    for impl in (NullTracer(), _tracer(FakeRecorder())):
        missing = [name for name in declared if not hasattr(impl, name)]
        assert missing == [], f"{type(impl).__name__} 缺端口成员: {missing}"


def test_run_tracer_drives_the_same_lifecycle_script():
    """同一段脚本对 RunTracer 成立：adapter 侧 golden 不变（trace 根仍被创建并收口）。"""
    recorder = FakeRecorder()
    run_tracer = _tracer(recorder)
    assert isinstance(run_tracer, Tracer)

    _drive_full_lifecycle(run_tracer)

    assert recorder.spans[0].name == "agent-run"
    assert recorder.spans[0].ended


@pytest.mark.asyncio
async def test_runtime_without_sink_drives_the_null_tracer_port(tmp_path, monkeypatch):
    """未配置观测 = NullTracer 收到完整生命周期（而不是"没有 tracer"）。"""
    calls: list[str] = []
    monkeypatch.setattr(runtime_module, "NullTracer", lambda: RecordingNullTracer(calls))
    scripted = ScriptedModel([AIMessage(content="你好")])
    session = make_session(tmp_path)

    await _plain_runtime(scripted).run(session, "hi")

    assert calls == [
        "run_started", "context_build_started", "context_build_completed",
        "model_call_started", "model_call_completed", "run_completed",
    ]


@pytest.mark.asyncio
async def test_exploding_sink_leaves_run_events_identical_to_no_sink(tmp_path):
    """sink 每个方法都抛：事件流（含工具重试链与 ToolResult）与"没有 sink"逐字段一致。"""

    class _ExplodingClient:
        def start_observation(self, **kwargs: Any):
            raise RuntimeError("langfuse 挂了")

    exploding_sink = LangfuseSink(
        public_key="pk", secret_key="sk",
        client_factory=lambda **kwargs: _ExplodingClient(),
    )
    assert exploding_sink.enabled

    baseline_session = make_session(tmp_path / "baseline")
    exploding_session = make_session(tmp_path / "exploding")
    await _tool_round_runtime(None).run(baseline_session, "跑一次工具")
    await _tool_round_runtime(exploding_sink).run(exploding_session, "跑一次工具")

    assert [e.type for e in exploding_session.events] == [
        e.type for e in baseline_session.events
    ]
    # 逐字段比较：含 tool/result 的 ToolResult 序列化（重试链 attempt/metadata
    # 在内）与 run 终态——旁路故障不得改变任何 durable 事实（不变量 #21）。
    # 唯一豁免：计时字段（同一逻辑跑两次必然不同，非确定性，不属 durable 事实）。
    assert [_timing_normalized(e.data) for e in exploding_session.events] == [
        _timing_normalized(e.data) for e in baseline_session.events
    ]


@pytest.mark.asyncio
async def test_sink_failing_only_in_trace_url_keeps_run_intact(tmp_path):
    """sink 只在 trace_url 合成处抛：run 照常完成，trace_id 如实回填、trace_url 降级 None。

    与上一条互补——上一条覆盖"每个方法都抛"（熔断后 URL 合成路径根本走不到），
    这一条单独钉住"客户端可用、只有 URL 合成失败"的形状：旁路故障不得吃掉
    已拿到的 trace_id，也不得伪造一个 URL。
    """

    class _UrlExplodingSpan:
        def __init__(self) -> None:
            self.trace_id = "tr-url-boom"

        def start_observation(self, **kwargs: Any):
            return _UrlExplodingSpan()

        def update(self, **kwargs: Any) -> None:
            return None

        def end(self, **kwargs: Any) -> None:
            return None

    class _UrlExplodingClient:
        def start_observation(self, **kwargs: Any):
            return _UrlExplodingSpan()

        def get_trace_url(self, *, trace_id: str | None):
            raise RuntimeError("URL 合成挂了")

    sink = LangfuseSink(
        public_key="pk", secret_key="sk",
        client_factory=lambda **kwargs: _UrlExplodingClient(),
    )
    session = make_session(tmp_path)

    await _plain_runtime(ScriptedModel([AIMessage(content="你好")]), sink=sink).run(session, "hi")

    completed = next(e for e in session.events if e.type == RUN_COMPLETED)
    assert completed.data["trace_id"] == "tr-url-boom"
    assert completed.data["trace_url"] is None


# ---- 终态臂：每条收尾路径都必须把端口带到终态（否则 Langfuse 上 run 永不 end） ----


def _record_null_tracer_calls(monkeypatch) -> list[str]:
    """把 Runtime 的缺席实现换成记录器：返回被调用的端口方法名列表。"""
    calls: list[str] = []
    monkeypatch.setattr(runtime_module, "NullTracer", lambda: RecordingNullTracer(calls))
    return calls


@pytest.mark.asyncio
async def test_failing_run_drives_terminal_port_lifecycle(tmp_path, monkeypatch):
    """异常臂（模型调用抛错）：在途 generation 必须收口 + run_failed 到达端口。"""
    calls = _record_null_tracer_calls(monkeypatch)
    session = make_session(tmp_path)

    result = await _plain_runtime(ExplodingModel()).run(session, "hi")

    assert result.status == "failed"
    assert calls == [
        "run_started", "context_build_started", "context_build_completed",
        "model_call_started", "model_call_failed", "run_failed",
    ]


@pytest.mark.asyncio
async def test_cancelled_run_drives_terminal_port_lifecycle(tmp_path, monkeypatch):
    """取消臂（消费方断连）：在途 generation 必须收口 + run_failed 必须到达端口。

    断点选在 model/started——生产断连（SSE 客户端关页面）最常见的形态就是
    "模型调用在途时断"，此时 generation 已开未收；在 run/started 处断开的话
    这条收口路径根本不会被走到（删掉 close_observability 里的 generation 块
    仍然全绿）。
    """
    calls = _record_null_tracer_calls(monkeypatch)
    session = make_session(tmp_path)
    runtime = _plain_runtime(ScriptedModel([AIMessage(content="第一轮就完成")]))
    agen = runtime.run_stream(session, "hi")
    async for event in agen:
        if event.type == MODEL_STARTED:
            break  # 悬空点：模型调用在途，消费者在此断开
    await agen.aclose()

    assert session.events[-1].type == RUN_FAILED
    assert calls == [
        "run_started", "context_build_started", "context_build_completed",
        "model_call_started", "model_call_failed", "run_failed",
    ]


class _OverflowingContextBuilder:
    """ContextBuilder 替身：投影阶段抛 ContextWindowExceededError（模型调用之前）。"""

    async def build(self, session: Any) -> list:
        raise ContextWindowExceededError("超限")


@pytest.mark.asyncio
async def test_context_window_exceeded_drives_terminal_port_lifecycle(tmp_path, monkeypatch):
    """超限臂：这一臂连 model_call_started 都不会发生，run_failed 仍必须到达端口。"""
    calls = _record_null_tracer_calls(monkeypatch)
    session = make_session(tmp_path)
    registry = ToolRegistry()
    runtime = AgentRuntime(
        ScriptedModel([AIMessage(content="不会走到这里")]),
        registry, ToolExecutor(registry),
        context_builder=_OverflowingContextBuilder(),
    )

    result = await runtime.run(session, "hi")

    assert result.status == STATUS_CONTEXT_WINDOW_EXCEEDED
    assert calls == [
        "run_started", "context_build_started", "context_build_completed", "run_failed",
    ]


@pytest.mark.asyncio
async def test_hard_guard_run_drives_terminal_port_lifecycle(tmp_path, monkeypatch):
    """硬熔断臂：run_failed 必须到达端口（该臂的端口调用在 failure_terminal 之前）。

    只钉尾部（终态必须是 run_failed、且没有 run_completed）——逐轮展开 tool span
    调用序列等于把实现抄一遍，改实现时会无意义地红。
    """
    calls = _record_null_tracer_calls(monkeypatch)
    session = make_session(tmp_path)
    scripted = ScriptedModel([
        _tool_call("fail", {"command": "ls"}, index) for index in range(6)
    ])
    registry = ToolRegistry()
    registry.register(FailureTool())
    runtime = AgentRuntime(scripted, registry, ToolExecutor(registry), max_steps=20)

    result = await runtime.run(session, "反复试同一个失败命令")

    assert result.status == STATUS_IDENTICAL_TOOL_FAILURE_LOOP
    assert calls[-1] == "run_failed"
    assert "run_completed" not in calls


@pytest.mark.asyncio
async def test_raising_tracer_cannot_swallow_the_terminal_event(tmp_path, monkeypatch):
    """第三方 tracer 抛异常：run/failed 仍必须落盘（不变量 #21：旁路故障不拖垮 Core）。

    端口契约要求实现不抛，但 Core 必须自己兜住——保护层在 `_new_tracer` 选定的
    实现外面（`_GuardedTracer`），所以每条收尾路径都受保护，不靠调用点自觉。
    """

    class _RaisingTracer(NullTracer):
        def run_failed(self, reason: str) -> None:
            raise RuntimeError("第三方 tracer 挂了")

    monkeypatch.setattr(runtime_module, "NullTracer", _RaisingTracer)
    session = make_session(tmp_path)

    result = await _plain_runtime(ExplodingModel()).run(session, "hi")

    assert result.status == "failed"
    assert session.events[-1].type == RUN_FAILED


@pytest.mark.asyncio
async def test_raising_tracer_cannot_turn_a_completed_run_into_failed(tmp_path, monkeypatch):
    """臂内端口调用抛异常（run_completed）：答案与终态不得被改写成失败。

    这条臂的端口调用在 `session.end_run` **之前**：没有保护层时异常会被顶层
    except 兜成 run/failed，一次成功的 run 连同答案一起被改写（不变量 #21）。
    """

    class _RaisingTracer(NullTracer):
        def run_completed(self, final_text: str, usage_total: Any = None) -> None:
            raise RuntimeError("第三方 tracer 挂了")

    monkeypatch.setattr(runtime_module, "NullTracer", _RaisingTracer)
    session = make_session(tmp_path)

    result = await _plain_runtime(ScriptedModel([AIMessage(content="答案正文")])).run(session, "hi")

    assert result.status == "completed"
    assert result.final_text == "答案正文"
    assert session.events[-1].type == RUN_COMPLETED


@pytest.mark.asyncio
async def test_raising_tracer_cannot_change_the_max_steps_verdict(tmp_path, monkeypatch):
    """max_steps 臂的端口调用抛异常：归因与终态仍是 max_steps_exceeded。

    该臂的 `run_failed` 在 `failure_terminal` 之前——没有保护层时 reason 会从
    max_steps_exceeded 变成 RuntimeError、status 变成 failed。
    """

    class _RaisingTracer(NullTracer):
        def run_failed(self, reason: str) -> None:
            raise RuntimeError("第三方 tracer 挂了")

    monkeypatch.setattr(runtime_module, "NullTracer", _RaisingTracer)
    session = make_session(tmp_path)
    rounds = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "flaky", "args": {"value": 1},
                "id": f"call_{index}", "type": "tool_call",
            }],
        )
        for index in range(2)
    ]
    registry = ToolRegistry()
    registry.register(_FlakyTool())
    runtime = AgentRuntime(
        ScriptedModel(rounds), registry, ToolExecutor(registry), max_steps=2,
    )

    result = await runtime.run(session, "永远算不完")

    assert result.status == STATUS_MAX_STEPS_EXCEEDED
    assert session.events[-1].type == RUN_FAILED
    assert session.events[-1].data["reason"] == STATUS_MAX_STEPS_EXCEEDED
