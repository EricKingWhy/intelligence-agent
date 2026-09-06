"""T2 #118：runtime ↔ RunTracer 接线——trace=run、generation=model call、trace_id 回填。

装配语义（ADR-0018 D5/D7）：sink 启用时 run/completed.data.trace_id 回填真实
Langfuse trace id（绝不伪造）；sink 缺席时事件流与 Phase 14 逐字节一致
（trace_id=null）；generation 与 llm_call 诊断行同源同时点。
"""

from __future__ import annotations

from typing import Annotated

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.observability import LangfuseSink
from agent_harness.session import RUN_COMPLETED
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _AddArgs(BaseModel):
    first_number: Annotated[float, Field(...)]
    second_number: Annotated[float, Field(...)]


class _AddTool(Tool):
    @property
    def name(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "求和。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _AddArgs

    async def execute(self, args: _AddArgs) -> ToolResult:
        return ToolResult.success(
            message="ok", data={"sum": args.first_number + args.second_number},
        )


class _FakeRecorder:
    def __init__(self):
        self.spans: list = []
        self.next_trace_id = "tr-fake-001"

    def client(self):
        recorder = self

        class _Client:
            def start_observation(self, *, name: str, as_type: str = "span", **kwargs):
                return _FakeSpan(recorder, name=name, kind=as_type, kwargs=kwargs)

        return _Client()


class _FakeSpan:
    def __init__(self, recorder: _FakeRecorder, *, name: str, kind: str, kwargs: dict):
        self._recorder = recorder
        self.name = name
        self.kind = kind
        self.kwargs = kwargs
        self.updates: list[dict] = []
        self.ended = False
        self.children: list[_FakeSpan] = []
        self.trace_id = recorder.next_trace_id
        recorder.spans.append(self)

    def start_observation(self, *, name: str, as_type: str = "span", **kwargs):
        child = _FakeSpan(self._recorder, name=name, kind=as_type, kwargs=kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def end(self):
        self.ended = True


def _sink(recorder: _FakeRecorder) -> LangfuseSink:
    return LangfuseSink(
        public_key="pk", secret_key="sk", base_url="https://example.invalid",
        client_factory=lambda **kwargs: recorder.client(),
    )


def _runtime(scripted: ScriptedModel, sink: LangfuseSink | None) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(_AddTool())
    return AgentRuntime(
        scripted, registry, ToolExecutor(registry), observability_sink=sink,
    )


@pytest.mark.asyncio
async def test_completed_run_backfills_real_trace_id_and_records_generation(tmp_path):
    recorder = _FakeRecorder()
    scripted = ScriptedModel([AIMessage(
        content="你好",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )])
    session = make_session(tmp_path)
    await _runtime(scripted, _sink(recorder)).run(session, "打个招呼")

    completed = next(e for e in session.events if e.type == RUN_COMPLETED)
    assert completed.data["trace_id"] == "tr-fake-001"  # 真实回填，非 None

    root = recorder.spans[0]
    assert root.name == "agent-run"
    assert root.kwargs["input"] == "打个招呼"
    assert root.kwargs["metadata"]["run_id"] == completed.run_id
    assert root.ended
    assert root.updates[-1]["output"] == "你好"

    assert len(root.children) == 1
    gen = root.children[0]
    assert gen.kind == "generation"
    assert gen.ended
    assert gen.kwargs["model"] == "primary"  # 创建期默认链名
    update = gen.updates[-1]
    assert update["output"] == "你好"
    assert update["usage_details"] == {"input": 10, "output": 5, "total": 15}
    assert update["metadata"]["response_model"] == "qwen-plus-0911"


@pytest.mark.asyncio
async def test_disabled_sink_keeps_event_stream_byte_compatible(tmp_path):
    scripted = ScriptedModel([AIMessage(content="你好")])
    session = make_session(tmp_path)
    # key 空 = 完全缺席：事件流与 Phase 14 行为一致（trace_id=null，无任何 span）。
    await _runtime(scripted, LangfuseSink(public_key="", secret_key="")).run(session, "hi")

    completed = next(e for e in session.events if e.type == RUN_COMPLETED)
    assert completed.data["trace_id"] is None


@pytest.mark.asyncio
async def test_no_sink_construction_keeps_trace_id_null(tmp_path):
    """未注入 sink（默认 None）= 与既有构造完全兼容。"""
    scripted = ScriptedModel([AIMessage(content="你好")])
    session = make_session(tmp_path)
    registry = ToolRegistry()
    registry.register(_AddTool())
    await AgentRuntime(scripted, registry, ToolExecutor(registry)).run(session, "hi")

    completed = next(e for e in session.events if e.type == RUN_COMPLETED)
    assert completed.data["trace_id"] is None
