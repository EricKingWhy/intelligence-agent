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
from agent_harness.agent.run_budget import TRIGGER_LOCAL_TURNS
from agent_harness.agent.types import (
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_PAUSED,
)
from agent_harness.context.builder import ContextWindowExceededError
from agent_harness.observability import LangfuseSink
from agent_harness.observability.port import NullSpan, NullTracer, Span, Tracer
from agent_harness.session import (
    CONTEXT_COMPACTED,
    MODEL_STARTED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
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


def test_memory_v2_run_omits_all_trace_content_even_when_global_mode_is_full(tmp_path):
    """Memory V2 traces retain approved metadata while excluding user/model/tool payloads."""
    from types import SimpleNamespace

    recorder = FakeRecorder()
    sink = LangfuseSink(
        public_key="pk", secret_key="sk", trace_content="full",
        client_factory=lambda **_kwargs: recorder.client(),
    )
    registry = ToolRegistry()
    runtime = AgentRuntime(
        ScriptedModel([]), registry, ToolExecutor(registry),
        memory_formation=object(), observability_sink=sink,
    )
    tracer = runtime._new_tracer(
        make_session(tmp_path), "run-1", "private user prompt", 1,
    )

    tracer.run_started()
    generation = tracer.model_call_started(
        step=1, messages=[{"content": "private prompt and evidence"}], model="memory-primary",
    )
    tracer.model_call_completed(
        generation,
        output_text="private memory response",
        usage={"total_tokens": 7},
        duration_ms=12,
        fallback_transitions=[SimpleNamespace(
            from_model="memory-primary", to_model="memory-fallback",
            reason="private provider response with credential=secret-value",
        )],
    )
    tool_span = tracer.tool_span_started(
        tool_name="memory_search", tool_call_id="call-1",
        args={"query": "private memory content"}, is_delegate=False,
    )
    tracer.tool_span_completed(
        tool_span, outcome="success", message="private tool output",
        attempts=[{"attempt": 1, "error_message": "credential=secret-value"}],
        session_id="session-1", extra={"unapproved": "private evidence"},
    )
    tracer.run_completed("private final response", usage_total={"total_tokens": 7})
    tracer.run_failed("credential=secret-value")

    captured = repr(recorder.spans)
    for forbidden in (
        "private user prompt", "private prompt and evidence", "private memory response",
        "private memory content", "private tool output", "private final response",
        "secret-value", "private evidence",
    ):
        assert forbidden not in captured
    root = recorder.spans[0]
    assert "input" not in root.kwargs
    assert root.kwargs["metadata"]["run_id"] == "run-1"
    assert len(root.kwargs["metadata"]["input_sha256"]) == 64
    generation_span = root.children[0]
    assert "input" not in generation_span.kwargs
    assert len(generation_span.kwargs["metadata"]["input_sha256"]) == 64
    assert generation_span.updates[-1]["usage_details"]["total"] == 7
    assert "output" not in generation_span.updates[-1]
    assert "fallback_reason" not in generation_span.updates[-1].get("metadata", {})
    assert generation_span.updates[-1]["metadata"]["fallback_from"] == "memory-primary"
    assert generation_span.updates[-1]["metadata"]["duration_ms"] == 12
    assert len(generation_span.updates[-1]["metadata"]["output_sha256"]) == 64
    tool = root.children[1]
    assert "input" not in tool.kwargs
    assert len(tool.kwargs["metadata"]["input_sha256"]) == 64
    assert "output" not in tool.updates[-1]
    assert tool.updates[-1]["metadata"]["outcome"] == "success"
    assert tool.updates[-1]["metadata"]["attempt_count"] == 1
    assert len(tool.updates[-1]["metadata"]["output_sha256"]) == 64
    assert "unapproved" not in tool.updates[-1]["metadata"]
    assert root.updates[-2]["metadata"]["usage_total"] == {"total_tokens": 7}


def test_memory_observation_uses_bounded_metadata_and_redacts_sdk_errors(caplog):
    class _BrokenObservation:
        def end(self):
            raise RuntimeError("api_key=never-log-this")

    class _Client:
        def start_observation(self, **_kwargs):
            return _BrokenObservation()

    sink = LangfuseSink(
        public_key="pk", secret_key="sk", breaker_threshold=1,
        client_factory=lambda **_kwargs: _Client(),
    )

    sink.memory_observation(
        stage="formation",
        metadata={
            "job_id": "job-1", "kind": "semantic", "scope": "user_global",
            "reason_code": "provider_error", "input_tokens": 42,
            "prompt": "must not be recorded",
        },
    )

    assert "never-log-this" not in caplog.text
    assert any(
        "Langfuse error details redacted" in repr(record.__dict__)
        for record in caplog.records
    )


def test_memory_observation_captures_only_allowlisted_metadata():
    recorder = FakeRecorder()
    sink = LangfuseSink(
        public_key="pk", secret_key="sk",
        client_factory=lambda **_kwargs: recorder.client(),
    )

    sink.memory_observation(
        stage="formation",
        metadata={
            "job_id": "job-1", "stage": "forming", "kind": "semantic",
            "scope": "user_global", "input_tokens": 42,
            "output_failure_kind": "contract_violation",
            "counts": {"accepted": 1, "secret": "private evidence"},
            "kind_counts": {"semantic": 1},
            "source_authority": ["user", "private evidence"],
            "prompt": "must not be recorded",
        },
    )

    observation = recorder.spans[0]
    assert observation.name == "memory-formation"
    assert observation.ended is True
    assert observation.kwargs["metadata"] == {
        "job_id": "job-1", "stage": "forming", "observation": "formation",
        "kind": "semantic", "scope": "user_global", "input_tokens": 42,
        "output_failure_kind": "contract_violation",
        "counts": {"accepted": 1},
        "kind_counts": {"semantic": 1},
        "source_authority": ["user"],
    }


def test_memory_metadata_hashes_are_accepted_but_raw_evidence_is_removed():
    from agent_harness.observability.sink import sanitize_memory_metadata

    digest = "a" * 64
    assert sanitize_memory_metadata({
        "input_sha256": digest,
        "evidence_sha256": digest,
        "evidence": "private content",
    }) == {"input_sha256": digest, "evidence_sha256": digest}


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
async def test_memory_v2_langfuse_outage_does_not_fail_agent_runtime(tmp_path, caplog):
    class _BrokenClient:
        def start_observation(self, **_kwargs):
            raise RuntimeError("api_key=private-value")

    sink = LangfuseSink(
        public_key="pk", secret_key="sk",
        client_factory=lambda **_kwargs: _BrokenClient(),
    )

    class _MemoryFormation:
        async def notify_run_finished(self, **_kwargs):
            return None

    registry = ToolRegistry()
    runtime = AgentRuntime(
        ScriptedModel([AIMessage(content="safe answer")]), registry,
        ToolExecutor(registry), memory_formation=_MemoryFormation(), observability_sink=sink,
    )
    session = make_session(tmp_path)

    result = await runtime.run(session, "private user prompt")

    assert result.status == "completed"
    assert result.final_text == "safe answer"
    assert session.events[-1].type == RUN_COMPLETED
    assert "private-value" not in caplog.text


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


class _CompactingContextBuilder:
    """ContextBuilder 替身：投影时落一条 context/compacted 事实（非异常路径）。

    runtime 读这条事件的 `compacted_turn_count` 喂给 context span 的收口——
    `_Telemetry` 只负责把它转发到端口，这里是那条 metadata 的端到端来源。
    """

    async def build(self, session: Any) -> list:
        session.append(CONTEXT_COMPACTED, {"compacted_turn_count": 2})
        return []


@pytest.mark.asyncio
async def test_compaction_count_reaches_the_context_span_metadata(tmp_path, monkeypatch):
    """压缩计数必须随 context span 的收口到达端口（AC1 的 metadata 面）。"""
    seen: list[int | None] = []

    class _CountSpy(NullTracer):
        def context_build_started(self, *, step: int) -> Any:
            return f"ctx-{step}"

        def context_build_completed(
            self, span: Any, *, compacted_turn_count: int | None = None,
        ) -> None:
            seen.append(compacted_turn_count)

    monkeypatch.setattr(runtime_module, "NullTracer", lambda: _CountSpy())
    registry = ToolRegistry()
    runtime = AgentRuntime(
        ScriptedModel([AIMessage(content="答")]), registry, ToolExecutor(registry),
        context_builder=_CompactingContextBuilder(),
    )

    await runtime.run(make_session(tmp_path), "hi")

    assert seen == [2], "compaction 事件的计数必须原样到端口（不是 None、不是别的值）"


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


class _IdentifiedNullSpan(NullSpan):
    """带 step 身份的 `NullSpan`（与生产同型 + 身份可辨）。

    `NullSpan` 是普通类（无 `__eq__`）⇒ 身份比较就是列表相等比较的语义，两个实例
    永远不相等。这正是"同一 span 收两次"与"两个 span 各收一次"的分辨力来源，同时
    类型与生产 / 共享替身一致（此前本替身返回 `str` 句柄，该分叉已收口——登记与结论见
    `docs/SDD_TICKET_TRACKER.md` 的 B-35 段）。
    """

    __slots__ = ("step",)

    def __init__(self, step: int) -> None:
        self.step = step

    def __repr__(self) -> str:
        return f"_IdentifiedNullSpan(step={self.step})"


class _ContextSpanRecordingNullTracer(RecordingNullTracer):
    """`RecordingNullTracer` + context span 的**身份与调用形状**（#285 的断言面）。

    只记方法名时，"同一 span 被收两次"与"两个不同 span 各收一次"是同一条记录
    （同方法名、同次数、同顺序）——把句柄换成另一个照样绿。本替身补三样：
    `context_build_started` 造的句柄（每次不同，带 step）、`context_build_completed`
    收到的句柄、以及它**带的关键字集合**（残余 R1：超限臂的调用是裸调，不传
    `compacted_turn_count`）。其余方法仍走父类的 `__getattr__` 记录，形状不变。

    （做成测试内的专用替身、不改进共享的 `RecordingNullTracer`：新身份面只服务这一条
    用例，而共享替身在本批之外另有 6 个读者（`test_tracer_port.py` 五条 +
    `test_tool_tracing.py` 一条），为一条用例扩一个共用替身的接口不划算。句柄类型
    与共享替身 / 生产**一致**（都是 `NullSpan` 家族）——此前"本替身返回 `str`"的分叉
    已收口，登记与结论见 `docs/SDD_TICKET_TRACKER.md` 的 B-35 段。）
    """

    def __init__(self, calls: list[str] | None = None) -> None:
        super().__init__(calls)
        self.started: list[Any] = []
        self.collected: list[Any] = []
        self.collected_kwargs: list[tuple[str, ...]] = []

    def context_build_started(self, *, step: int) -> Any:
        super().__getattr__("context_build_started")(step=step)  # 记名 + 委托（返回值丢弃）
        span = _IdentifiedNullSpan(step)
        self.started.append(span)
        return span

    def context_build_completed(self, span: Any, **kwargs: Any) -> None:
        super().__getattr__("context_build_completed")(span, **kwargs)  # 记名 + 委托
        self.collected.append(span)
        self.collected_kwargs.append(tuple(sorted(kwargs)))


def _record_context_spans(monkeypatch) -> _ContextSpanRecordingNullTracer:
    tracer = _ContextSpanRecordingNullTracer()
    monkeypatch.setattr(runtime_module, "NullTracer", lambda: tracer)
    return tracer


@pytest.mark.asyncio
async def test_context_window_exceeded_then_disconnect_collects_the_span_once(
    tmp_path, monkeypatch,
):
    """超限臂 + 终态帧上断连：同一 context span **只收一次**（#285 / 残余 R2 修复）。

    这条路径**生产可达**（消费方在终态帧上断连 ⇒ GeneratorExit 落进 `_drive` 的取消
    臂）。#264 之前超限臂收口后保留句柄，于是取消臂拿同一句柄**再收一次**（6 次端口
    调用）；`#265` 的等价重构逐字复刻了该形状并登记为残余 R2，`#285` 修掉它 ⇒ 5 次。

    断言面按残余⑧ 的要求补全：方法名序列（次数 + 顺序）+ **句柄身份**（收回来的就是
    起出去的那一个）+ **调用形状**（残余 R1：超限臂裸调，不带 `compacted_turn_count`）
    ——只断言方法名的话，把二次收口的句柄换成另一个同样会绿；形状不入断言的话，
    超限臂改回"带 `compacted_turn_count=None`"同样会绿。
    """
    tracer = _record_context_spans(monkeypatch)
    session = make_session(tmp_path)
    registry = ToolRegistry()
    runtime = AgentRuntime(
        ScriptedModel([AIMessage(content="不会走到这里")]),
        registry, ToolExecutor(registry),
        context_builder=_OverflowingContextBuilder(),
    )

    agen = runtime.run_stream(session, "hi")
    async for event in agen:
        if event.type == RUN_FAILED:
            break  # 悬空点：终态帧已出，消费方在此断开
    await agen.aclose()

    assert tracer.calls == [
        "run_started", "context_build_started", "context_build_completed", "run_failed",
        "run_failed",
    ], "取消臂不得对已收口的 span 再收一次——第二次 run_failed 是取消臂自己的归因"
    assert isinstance(tracer.started[0], NullSpan), \
        "句柄与生产 / 共享替身同型（此前这里是 `str` 分叉，见 tracker B-35 段）"
    assert [span.step for span in tracer.started] == [0]
    assert tracer.collected == tracer.started, "收口收到的句柄必须是起出去的那一个"
    assert tracer.collected_kwargs == [()], "超限臂的收口是裸调（残余 R1：不传 compacted_turn_count）"


@pytest.mark.asyncio
async def test_stuck_pause_does_not_drive_a_terminal_port_call(tmp_path, monkeypatch):
    """同错熔断臂（`#317` T9 起收口是**非终态** `run/paused`）：端口不得收到终态。

    旧契约下这条链路在 `end_run(failed)` 处收口，端口会拿到 `run_failed`；T9 把它换成
    暂停（ADR-0048 D5）⇒ 端口**一条终态调用都不该有**（`NullTracer` 今天也没有暂停钩子：
    暂停不是 run 的结局）。这条钉的正是"暂停别被当成失败报给可观测性"——若哪天有人把
    暂停接回 `run_failed`，本用例会红。
    """
    calls = _record_null_tracer_calls(monkeypatch)
    session = make_session(tmp_path)
    scripted = ScriptedModel([
        _tool_call("fail", {"command": "ls"}, index) for index in range(6)
    ])
    registry = ToolRegistry()
    registry.register(FailureTool())
    runtime = AgentRuntime(scripted, registry, ToolExecutor(registry), max_agent_turns=20)

    result = await runtime.run(session, "反复试同一个失败命令")

    assert result.status == STATUS_PAUSED
    assert "run_failed" not in calls
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
async def test_raising_tracer_cannot_change_the_budget_pause_verdict(tmp_path, monkeypatch):
    """预算暂停臂：端口抛异常也不得把暂停改写成失败（`#312` T4 起的保险丝出口）。

    这条臂**不调**任何终态端口（run 没结束，归因留给真正的终态），所以这里钉的是
    "第三方的 `run_failed` 抛错时，暂停依旧是一条 `run/paused`、没有任何 `run/failed`"。
    哪天有人给暂停路径加了未经保护的端口调用，本行会红（保护层见 `_GuardedTracer`）。
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
        ScriptedModel(rounds), registry, ToolExecutor(registry), max_agent_turns=2,
    )

    result = await runtime.run(session, "永远算不完")

    assert result.status == STATUS_PAUSED
    assert session.events[-1].type == RUN_PAUSED
    assert session.events[-1].data["trigger_dimension"] == TRIGGER_LOCAL_TURNS
    assert not [e for e in session.events if e.type == RUN_FAILED]
