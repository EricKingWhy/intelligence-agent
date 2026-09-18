"""T3 #119：tool span（逐 attempt 链）+ SubAgent agent 型观测 + 嵌套绑定。

ADR-0018 D5/D7：工具调用 = ``tool`` 型观测；delegate 工具 = ``agent`` 型 +
具体目标命名（绝不用 tool/span 隐藏 SubAgent 结构），其存活期间设置嵌套
绑定——child run 的 RunTracer 认领 agent 观测为根，child 的 generation
全部挂在其下（官方结构：无 dispatch/execution 双节点）。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest
from pydantic import BaseModel, Field

from agent_harness.observability import LangfuseSink, RunTracer
from agent_harness.observability.port import NullTracer, Tracer
from agent_harness.observability.tracer import current_trace_binding
from agent_harness.storage import (
    OperationContext,
    OperationState,
    SqliteOperationLedger,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from agent_harness.tooling.result import ErrorCode
from tests.observability.test_tracer import FakeRecorder
from tests.observability.tracer_fixtures import RecordingNullTracer


class _Args(BaseModel):
    value: Annotated[int, Field(...)]


class _FlakyTool(Tool):
    """第一次执行返回可重试失败（驱动唯一 Retry Layer），第二次成功。"""

    def __init__(self):
        self.calls = 0

    @property
    def name(self) -> str:
        return "flaky"

    @property
    def description(self) -> str:
        return "重试链观测用。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _Args

    async def execute(self, args: _Args) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            return ToolResult.failure(
                message="transient boom",
                error_code=ErrorCode.TIMEOUT,
                retryable=True,
            )
        return ToolResult.success(message="ok")


class _DelegateLikeTool(Tool):
    """is_subagent_dispatch 工具：执行期间模拟 child runtime 建自己的 tracer。"""

    is_subagent_dispatch = True

    @property
    def name(self) -> str:
        return "delegate"

    @property
    def description(self) -> str:
        return "委派给 SubAgent。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _Args

    async def execute(self, args: _Args) -> ToolResult:
        binding = current_trace_binding.get()
        assert binding is not None, "delegate 执行期间必须有嵌套绑定"
        child = RunTracer(
            self._sink, session_id="child-sess", run_id="child-run",
            agent_id="researcher", user_input="子任务",
        )
        child.run_started()
        gen = child.model_call_started(step=1, messages=[{"role": "user", "content": "m"}])
        child.model_call_completed(gen, output_text="子回答", usage=None, duration_ms=1)
        child.run_completed("子回答")
        type(self).captured_child = child
        return ToolResult.success(message="委派完成", data={"child_session_id": "child-sess"})

    captured_child: RunTracer | None = None
    _sink: LangfuseSink


class _BlockingTool(Tool):
    """永远阻塞直到被取消（取消臂）或撞上 Executor 的 timeout 边界（超时臂）。"""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout_seconds = timeout_seconds

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def name(self) -> str:
        return "blocking"

    @property
    def description(self) -> str:
        return "阻塞直到被取消。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _Args

    async def execute(self, args: _Args) -> ToolResult:
        await asyncio.sleep(self._timeout_seconds)
        return ToolResult.success(message="不该走到这里")


class _FailingTerminalLedger(SqliteOperationLedger):
    """终态写入抛错的 Ledger：模拟存储故障（执行域外的异常）。"""

    async def update_state(self, session_id, tool_call_id, state, **kwargs):
        if state in (OperationState.SUCCEEDED, OperationState.FAILED):
            raise RuntimeError("ledger 挂了")
        return await super().update_state(session_id, tool_call_id, state, **kwargs)


def _sole_tool_span(recorder: FakeRecorder):
    """本次执行里唯一的 tool 型观测（root 的子观测）。"""
    root = recorder.spans[0]
    tool_spans = [c for c in root.children if c.kind == "tool"]
    assert len(tool_spans) == 1, f"应有且仅有一个 tool 观测：{tool_spans}"
    return tool_spans[0]


def _outcome_updates(span) -> list[dict]:
    """带 outcome 的 update 调用（= 收口调用）；span.update 的 kwargs 原样在列表里。"""
    return [u for u in span.updates if "metadata" in u and "outcome" in u["metadata"]]


def _sink(recorder: FakeRecorder) -> LangfuseSink:
    return LangfuseSink(
        public_key="pk", secret_key="sk", base_url="https://example.invalid",
        client_factory=lambda **kwargs: recorder.client(),
    )


def _tracer(recorder: FakeRecorder) -> RunTracer:
    tracer = RunTracer(
        _sink(recorder), session_id="sess", run_id="run", agent_id="default",
        user_input="任务",
    )
    tracer.run_started()
    return tracer


@pytest.mark.asyncio
async def test_tool_span_records_attempt_chain_and_completion():
    recorder = FakeRecorder()
    tracer = _tracer(recorder)
    root = recorder.spans[0]

    registry = ToolRegistry()
    tool = _FlakyTool()
    registry.register(tool)
    executor = ToolExecutor(registry)
    execution = await executor.execute(
        {"id": "call_1", "name": "flaky", "args": {"value": 1}}, tracer=tracer,
    )

    assert execution.result.ok
    tool_spans = [c for c in root.children if c.kind == "tool"]
    assert len(tool_spans) == 1
    span = tool_spans[0]
    assert span.name == "flaky"
    assert span.kwargs["input"] == {"value": 1}
    assert span.kwargs["metadata"]["tool_call_id"] == "call_1"
    final = span.updates[-1]
    assert final["metadata"]["outcome"] == "success"
    assert len(final["metadata"]["attempts"]) == 2
    assert final["metadata"]["attempts"][0]["ok"] is False
    assert final["metadata"]["attempts"][0]["error_code"] == "TIMEOUT"
    assert final["metadata"]["attempts"][1]["ok"] is True
    assert span.ended


@pytest.mark.asyncio
async def test_delegate_tool_uses_agent_observation_and_nests_child():
    recorder = FakeRecorder()
    tracer = _tracer(recorder)
    root = recorder.spans[0]

    registry = ToolRegistry()
    tool = _DelegateLikeTool()
    tool._sink = tracer._sink
    registry.register(tool)
    executor = ToolExecutor(registry)
    execution = await executor.execute(
        {"id": "call_d", "name": "delegate", "args": {"value": 1}}, tracer=tracer,
    )

    assert execution.result.ok
    agent_spans = [c for c in root.children if c.kind == "agent"]
    assert len(agent_spans) == 1, "delegate 必须是 agent 型观测（不是 tool/span）"
    agent_span = agent_spans[0]
    assert agent_span.name == "delegate"
    # child tracer 认领 agent 观测为根：generation 挂在其下、同一 trace。
    child = type(tool).captured_child
    assert child is not None
    assert child.trace_id == tracer.trace_id
    generations = [c for c in agent_span.children if c.kind == "generation"]
    assert len(generations) == 1
    assert generations[0].kwargs["input"] == [{"role": "user", "content": "m"}]
    # 根的生命周期归父侧 executor：agent 观测由 tool_span_completed 收口。
    assert agent_span.ended
    # 绑定已还原：执行结束后 contextvar 回到 None。
    assert current_trace_binding.get() is None


# ---- #250：Executor 端口化 + span 生命周期收口（取消 / 异常 / 超时都必须结束） ----


def _executor_module_source() -> str:
    from pathlib import Path

    import agent_harness.tooling.executor as executor_module

    return Path(executor_module.__file__).read_text(encoding="utf-8")


def test_executor_tracer_defaults_to_the_null_implementation():
    """AC1：`execute` / `execute_batch` 的 tracer 默认值是端口对象，不是 None。

    默认值必须是 NullTracer 实例——None 默认值正是"optional tracing 用 None
    分支表达"的形状，调用点会被迫判空。
    """
    import inspect

    for func in (ToolExecutor.execute, ToolExecutor.execute_batch):
        default = inspect.signature(func).parameters["tracer"].default
        assert isinstance(default, NullTracer), f"{func.__name__} 的默认值不是 NullTracer"
        assert isinstance(default, Tracer)


def test_executor_source_has_no_none_guard_on_tracer():
    """AC1（机械闸）：Executor 源码里不再出现 `if tracer is not None` 形状的分支。"""
    source = _executor_module_source()
    assert "tracer is not None" not in source


@pytest.mark.asyncio
async def test_executor_without_tracer_drives_the_null_tracer_port():
    """缺席观测时 Executor 仍驱动端口：NullTracer 收到 span 的完整生命周期。"""
    calls: list[str] = []
    recording = RecordingNullTracer(calls)

    registry = ToolRegistry()
    registry.register(_FlakyTool())
    executor = ToolExecutor(registry)
    execution = await executor.execute(
        {"id": "call_port", "name": "flaky", "args": {"value": 1}}, tracer=recording,
    )

    assert execution.result.ok
    assert calls == ["tool_span_started", "tool_span_completed"]


@pytest.mark.asyncio
async def test_cancelled_tool_execution_closes_the_span_once():
    """取消臂：工具执行被取消时 span 必须收口（否则观测上永远挂着一个未结束的观测）。"""
    recorder = FakeRecorder()
    tracer = _tracer(recorder)
    registry = ToolRegistry()
    registry.register(_BlockingTool())
    executor = ToolExecutor(registry)

    task = asyncio.create_task(executor.execute(
        {"id": "call_cancel", "name": "blocking", "args": {"value": 1}}, tracer=tracer,
    ))
    await asyncio.sleep(0.05)  # 让 span 真正开出来
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    span = _sole_tool_span(recorder)
    assert span.ended
    closes = _outcome_updates(span)
    assert len(closes) == 1, f"span 必须恰好收口一次：{closes}"
    assert closes[0]["metadata"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_tool_timeout_closes_the_span_once():
    """超时臂：Timeout 边界映射成 ToolResult 后，span 按失败收口且带 attempt 链。"""
    recorder = FakeRecorder()
    tracer = _tracer(recorder)
    registry = ToolRegistry()
    registry.register(_BlockingTool(timeout_seconds=0.01))
    executor = ToolExecutor(registry)

    execution = await executor.execute(
        {"id": "call_timeout", "name": "blocking", "args": {"value": 1}}, tracer=tracer,
    )

    assert not execution.result.ok
    assert execution.result.error_code == ErrorCode.TIMEOUT
    span = _sole_tool_span(recorder)
    closes = _outcome_updates(span)
    assert len(closes) == 1, f"span 必须恰好收口一次：{closes}"
    assert closes[0]["metadata"]["outcome"] == "failure"
    assert closes[0]["metadata"]["attempts"][0]["error_code"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_ledger_failure_closes_the_span_once(tmp_path):
    """异常臂：Ledger 终态写入失败时 span 必须收口，且异常原样传播（不吞不换）。"""
    recorder = FakeRecorder()
    tracer = _tracer(recorder)
    ledger = _FailingTerminalLedger(tmp_path / "state.db")
    await ledger.initialize()
    registry = ToolRegistry()
    registry.register(_FlakyTool())
    executor = ToolExecutor(registry, operation_ledger=ledger)

    with pytest.raises(RuntimeError, match="ledger 挂了"):
        await executor.execute(
            {"id": "call_ledger", "name": "flaky", "args": {"value": 1}},
            tracer=tracer,
            operation_context=OperationContext(session_id="sess-1"),
        )

    span = _sole_tool_span(recorder)
    assert span.ended
    closes = _outcome_updates(span)
    assert len(closes) == 1, f"span 必须恰好收口一次：{closes}"
    assert closes[0]["metadata"]["outcome"] == "exception"
    # 异常文本不进观测（脱敏）：metadata 里只有 outcome / attempts / session_id。
    assert set(closes[0]["metadata"]) == {"outcome", "attempts", "session_id"}
