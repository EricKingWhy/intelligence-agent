"""Gap 1 (P1)：model/completed 与 run/completed 携带 model / usage / cost。

契约（docs/BACKEND_GAP_PROMPT.md，前端 StepDetail MODEL 空槽消费）：
- model/completed.data 新增 `model`（本次推理所用模型名）与 `usage`
  {prompt_tokens, completion_tokens, total_tokens}——从模型响应如实抽取，
  响应没带就省略（绝不伪造，缺失时前端显示「—」）。
- run/completed.data 新增 `usage_total`（本轮聚合，省略当未消费任何 token）、
  `cost_usd`（spec 12 未定义费率表 → 恒 null + TODO，不伪造）、
  `trace_id`（Phase 15 接入 Langfuse 前恒 null）。
"""

from __future__ import annotations

from typing import Annotated

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.session import MODEL_COMPLETED, RUN_COMPLETED
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _AddArgs(BaseModel):
    first_number: Annotated[float, Field(...)]
    second_number: Annotated[float, Field(...)]


class AddTool(Tool):
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
        return ToolResult.success(message="ok", data={"sum": args.first_number + args.second_number})


def _usage(input_tokens: int, output_tokens: int) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens}


def _events_of_type(session, event_type: str) -> list:
    return [e for e in session.events if e.type == event_type]


def _runtime(scripted: ScriptedModel) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(AddTool())
    return AgentRuntime(scripted, registry, ToolExecutor(registry))


@pytest.mark.asyncio
async def test_model_completed_carries_model_and_usage(tmp_path):
    """响应带 usage_metadata / model_name 时如实映射进 model/completed。"""
    scripted = ScriptedModel([AIMessage(
        content="你好",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata=_usage(1234, 567),
    )])
    session = make_session(tmp_path)
    await _runtime(scripted).run(session, "打个招呼")

    completed = _events_of_type(session, MODEL_COMPLETED)
    assert len(completed) == 1
    assert completed[0].data["model"] == "qwen-plus-0911"
    assert completed[0].data["usage"] == {
        "prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801,
    }

    finished = _events_of_type(session, RUN_COMPLETED)[0]
    assert finished.data["usage_total"] == {
        "prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801,
    }
    # spec 12 未定义费率表：cost 恒 null（TODO），Langfuse 未接入：trace_id 恒 null。
    assert finished.data["cost_usd"] is None
    assert finished.data["trace_id"] is None


@pytest.mark.asyncio
async def test_run_usage_accumulates_across_turns(tmp_path):
    """多轮模型调用：run/completed.usage_total 是各轮之和。"""
    round1 = AIMessage(
        content="",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata=_usage(100, 10),
        tool_calls=[{"name": "add", "args": {"first_number": 1, "second_number": 2},
                     "id": "call_u1", "type": "tool_call"}],
    )
    round2 = AIMessage(content="1 + 2 = 3", response_metadata={"model_name": "qwen-plus-0911"},
                       usage_metadata=_usage(150, 20))
    session = make_session(tmp_path)
    await _runtime(ScriptedModel([round1, round2])).run(session, "计算")

    totals = [e.data["usage"] for e in _events_of_type(session, MODEL_COMPLETED)]
    assert totals == [
        {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        {"prompt_tokens": 150, "completion_tokens": 20, "total_tokens": 170},
    ]
    finished = _events_of_type(session, RUN_COMPLETED)[0]
    assert finished.data["usage_total"] == {
        "prompt_tokens": 250, "completion_tokens": 30, "total_tokens": 280,
    }


@pytest.mark.asyncio
async def test_missing_usage_is_omitted_never_fabricated(tmp_path):
    """响应没带 usage / model：字段省略而不是填 0；usage_total 同理。"""
    scripted = ScriptedModel([AIMessage(content="纯文本，无元数据")])
    session = make_session(tmp_path)
    await _runtime(scripted).run(session, "hi")

    completed = _events_of_type(session, MODEL_COMPLETED)[0]
    assert "usage" not in completed.data
    assert "model" not in completed.data

    finished = _events_of_type(session, RUN_COMPLETED)[0]
    assert "usage_total" not in finished.data
    assert finished.data["cost_usd"] is None
    assert finished.data["trace_id"] is None
