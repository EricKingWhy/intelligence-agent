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
from agent_harness.observability import LangfuseSink
from agent_harness.observability.port import NullTracer, Span, Tracer
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.observability.test_tool_tracing import _FlakyTool
from tests.observability.test_tracer import FakeRecorder, _tracer
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


def _plain_runtime(
    scripted: ScriptedModel, sink: LangfuseSink | None = None,
) -> AgentRuntime:
    registry = ToolRegistry()
    return AgentRuntime(scripted, registry, ToolExecutor(registry), observability_sink=sink)


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
_TIMING_RE = re.compile(r'"(?:total_)?duration_ms":\s*[0-9.eE+-]+')


def _timing_normalized(value: Any) -> Any:
    """把计时字段归零后比较（含 tool/result content 这个 JSON 字符串里的）。"""
    if isinstance(value, str):
        return _TIMING_RE.sub('"duration_ms":0', value)
    if isinstance(value, dict):
        return {
            key: _timing_normalized(item)
            for key, item in value.items()
            if key not in ("duration_ms", "total_duration_ms")
        }
    if isinstance(value, list):
        return [_timing_normalized(item) for item in value]
    return value


class _RecordingNullTracer:
    """记录端口调用名，行为委托 NullTracer（测试 seam：证明"谁被调用"，零副作用）。"""

    trace_id: str | None = None
    trace_url: str | None = None

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self._inner = NullTracer()

    def __getattr__(self, name: str):
        def _record(*args: Any, **kwargs: Any) -> Any:
            self._calls.append(name)
            return getattr(self._inner, name)(*args, **kwargs)

        return _record


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
    monkeypatch.setattr(runtime_module, "NullTracer", lambda: _RecordingNullTracer(calls))
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

        def get_trace_url(self, *, trace_id: str | None):
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
