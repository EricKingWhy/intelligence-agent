"""#449：length 截断轮的 toolCalls 暴露面记录与判错防线。

§聚合事实（交付物 1 的机读记录，与探针 probe_449_salvage.py 同源）：langchain-core
聚合 tool_call_chunks 时用 parse_partial_json（JSON salvage）分桶——截断 args 几乎
总被 salvage 成 dict 落进【合法 tool_calls 桶】（丢尾或凭空补全），invalid_tool_calls
只在 salvage 结果非 dict / 抛异常时才有条目；finish_reason 与 content 聚合后存活。
修复前 runtime 从不读 finish_reason ⇒ 合法桶直通 executor（B 形态凭空补全的 args
静默执行真实副作用）；合法桶为空时非空 content 被当最终答复静默收口。

防线（交付物 2）：finish_reason == "length" 且本轮存在 tool_calls（含 invalid 桶）
时全部判错——准入前拒绝（零执行、零配额、显式 0 增量），错误即消息（04 §4）回给
模型由它重发；无截断（finish_reason="stop" 同形状剧本）行为不变（金钉）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage
from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.agent.types import STATUS_COMPLETED
from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_FAILED,
    RUN_COMPLETED,
    RUN_FAILED,
    TOOL_CALL,
    TOOL_RESULT,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

TRUNCATED_CALL_ID = "call_truncated_1"


# ---- 夹具：带可选参数的 Echo 工具（suffix 可选是有意的：防线要盯的正是
# "salvage 补全后仍能通过 schema 校验"的形态——必填字段缺失会被 pydantic 挡住，
# 掩盖暴露面） ----


class _EchoArgs(BaseModel):
    command: str = Field(..., description="要回显的命令")
    suffix: str | None = Field(None, description="可选后缀")


class EchoTool(Tool):
    """echo 工具：记录每次真实执行拿到的 args（判错断言的证据）。"""

    def __init__(self) -> None:
        self.executed_args: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显 command（可选拼接 suffix）。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EchoArgs

    async def execute(self, args: _EchoArgs) -> ToolResult:
        self.executed_args.append(args.model_dump())
        return ToolResult.success(message=args.command + (args.suffix or ""))


def _runtime(model: Any, tool: EchoTool) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(tool)
    return AgentRuntime(
        model=model,
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=5,
    )


def _salvage_chunk_model_scripts_truncated_args() -> list[AIMessageChunk]:
    """B 形态流式剧本：args 在字符串值中间被切（'"suffix": "y' 缺收尾）。"""
    return [
        AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "name": "echo",
                "args": '{"command": "rm -rf x", "suffix": "y',
                "id": TRUNCATED_CALL_ID,
                "index": 0,
                "type": "tool_call_chunk",
            }],
        ),
        AIMessageChunk(content="正在删除", response_metadata={"finish_reason": "length"}),
    ]


class _SalvageChunkModel:
    """吐原始 AIMessageChunk 的流式替身（ScriptedModel.astream 只携带已解析的
    tool_calls，到不了 salvage 路径，故自备 chunk 级替身）。"""

    def __init__(self, scripts: list[list[AIMessageChunk]]) -> None:
        self._scripts = list(scripts)
        self._cursor = 0
        self.requests: list[list[AnyMessage]] = []

    def bind_tools(self, tools: list, **kwargs: Any) -> _SalvageChunkModel:
        return self

    async def astream(self, messages: list[AnyMessage]) -> Any:
        self.requests.append(list(messages))
        if self._cursor >= len(self._scripts):
            raise RuntimeError("_SalvageChunkModel 剧本耗尽：Runtime 调用次数超出预期")
        script = self._scripts[self._cursor]
        self._cursor += 1
        for chunk in script:
            yield chunk

    async def ainvoke(self, messages: list[AnyMessage]) -> AIMessage:
        raise AssertionError("本文件的用例只走 run_stream（ainvoke 是 run() 的路径）")


def _fold(chunks: list[AIMessageChunk]) -> AIMessageChunk:
    """逐字复刻 runtime 聚合表达式（reduce 风格），作为暴露面的直接观测点。"""
    ai: AIMessageChunk = chunks[0]
    for chunk in chunks[1:]:
        ai = ai + chunk
    return ai


# --------------------------------------------------------------------------------------
# §聚合事实（交付物 1）：salvage 分桶的机读记录——这两条钉的是 langchain-core 行为，
# 与防线无关，防线的回归判据是 §防线 各用例。
# --------------------------------------------------------------------------------------


def test_salvage_lands_truncated_args_in_valid_bucket() -> None:
    """A/B 两形态：截断 args 被 salvage 进合法 tool_calls 桶。"""
    cut_mid_key = _fold([
        AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "name": "echo",
                "args": '{"command": "rm -rf x", "secon',
                "id": TRUNCATED_CALL_ID,
                "index": 0,
                "type": "tool_call_chunk",
            }],
        ),
        AIMessageChunk(content="说明", response_metadata={"finish_reason": "length"}),
    ])
    assert cut_mid_key.response_metadata.get("finish_reason") == "length"
    assert cut_mid_key.invalid_tool_calls == []
    # 尾巴被静默丢弃：'"secon' 整段消失，args 成了"看似合法"的残缺参数
    assert cut_mid_key.tool_calls[0]["args"] == {"command": "rm -rf x"}

    balanced_prefix = _fold([
        AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "name": "echo",
                "args": '{"command": "rm -rf x", "suffix": "y',
                "id": TRUNCATED_CALL_ID,
                "index": 0,
                "type": "tool_call_chunk",
            }],
        ),
        AIMessageChunk(content="说明", response_metadata={"finish_reason": "length"}),
    ])
    assert balanced_prefix.invalid_tool_calls == []
    # 凭空补全：模型没写完的 '"y' 被补上收尾引号，args 成了完整合法参数
    assert balanced_prefix.tool_calls[0]["args"] == {
        "command": "rm -rf x", "suffix": "y",
    }


def test_unparseable_args_land_in_invalid_bucket() -> None:
    """C 形态：完全解析不了才进 invalid 桶（条目是 dict，带 error 键）。"""
    ai = _fold([
        AIMessageChunk(
            content="",
            tool_call_chunks=[{
                "name": "echo",
                "args": "not json at all",
                "id": TRUNCATED_CALL_ID,
                "index": 0,
                "type": "tool_call_chunk",
            }],
        ),
        AIMessageChunk(content="半句", response_metadata={"finish_reason": "length"}),
    ])
    assert ai.tool_calls == []
    assert len(ai.invalid_tool_calls) == 1
    invalid = ai.invalid_tool_calls[0]
    assert invalid["name"] == "echo"
    assert "error" in invalid


# --------------------------------------------------------------------------------------
# §防线（交付物 2）：finish_reason == "length" 且存在 tool_calls（含 invalid）⇒
# 全部判错、零执行、错误即消息、循环继续；无截断行为不变（金钉）。
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_length_truncated_tool_calls_refused_not_executed(tmp_path) -> None:
    """run() 路径：截断轮的 tool_call 全部判错——零执行、call/result 成对、错误即消息。"""
    tool = EchoTool()
    model = ScriptedModel([
        AIMessage(
            content="先删文件",
            tool_calls=[{
                "name": "echo",
                "args": {"command": "rm -rf x", "suffix": "y"},
                "id": TRUNCATED_CALL_ID,
                "type": "tool_call",
            }],
            response_metadata={"finish_reason": "length"},
        ),
        AIMessage(content="已按截断提示重新发起并完成。"),
    ])
    runtime = _runtime(model, tool)
    session = make_session(tmp_path)

    result = await runtime.run(session, "hi")

    assert result.status == STATUS_COMPLETED
    assert result.final_text == "已按截断提示重新发起并完成。"
    assert tool.executed_args == []  # 零执行：截断 args 绝不进 execute
    # durable 事实：tool/call（模型确实请求过）+ 恰一条判错 tool/result
    call_events = [e for e in session.events if e.type == TOOL_CALL]
    assert [e.data["tool_call_id"] for e in call_events] == [TRUNCATED_CALL_ID]
    result_events = [e for e in session.events if e.type == TOOL_RESULT]
    assert len(result_events) == 1
    payload = json.loads(result_events[0].data["content"])
    assert payload["ok"] is False
    assert payload["error_code"] == "ARGS_TRUNCATED"
    assert result_events[0].data["budget_delta"] == {
        "tool_name": "echo", "tool_calls": 0, "tool_attempts": 0,
    }
    # 错误即消息（04 §4）：第二轮请求里模型看得见截断拒绝，重发由模型决定
    tool_messages = [
        m for m in model.snapshots[1].messages if type(m).__name__ == "ToolMessage"
    ]
    assert len(tool_messages) == 1
    assert "ARGS_TRUNCATED" in tool_messages[0].content


@pytest.mark.asyncio
async def test_stream_salvaged_args_never_execute(tmp_path) -> None:
    """run_stream 路径（B 形态，salvage 凭空补全）：判错、零执行、循环继续到完成。"""
    tool = EchoTool()
    model = _SalvageChunkModel([
        _salvage_chunk_model_scripts_truncated_args(),
        [AIMessageChunk(content="已重新发起并完成。")],
    ])
    runtime = _runtime(model, tool)
    session = make_session(tmp_path)

    async for _ in runtime.run_stream(session, "hi"):
        pass

    assert tool.executed_args == []
    assert len(model.requests) == 2  # 循环继续：截断轮之后模型被再次调用
    result_events = [e for e in session.events if e.type == TOOL_RESULT]
    assert len(result_events) == 1
    payload = json.loads(result_events[0].data["content"])
    assert payload["ok"] is False
    assert payload["error_code"] == "ARGS_TRUNCATED"
    assert any(e.type == RUN_COMPLETED for e in session.events)
    # 错误即消息：第二次请求带上了判错 ToolMessage
    tool_messages = [
        m for m in model.requests[1] if type(m).__name__ == "ToolMessage"
    ]
    assert len(tool_messages) == 1
    assert "ARGS_TRUNCATED" in tool_messages[0].content


@pytest.mark.asyncio
async def test_unparseable_truncated_turn_is_not_silent_final(tmp_path) -> None:
    """C 形态（合法桶为空）：截断轮的非空 content 不得被当最终答复静默收口。"""
    tool = EchoTool()
    model = _SalvageChunkModel([
        [
            AIMessageChunk(
                content="",
                tool_call_chunks=[{
                    "name": "echo",
                    "args": "not json at all",
                    "id": TRUNCATED_CALL_ID,
                    "index": 0,
                    "type": "tool_call_chunk",
                }],
            ),
            AIMessageChunk(content="这是被截断的半句话", response_metadata={"finish_reason": "length"}),
        ],
        [AIMessageChunk(content="第二次才是真回答")],
    ])
    runtime = _runtime(model, tool)
    session = make_session(tmp_path)

    async for _ in runtime.run_stream(session, "hi"):
        pass

    assert tool.executed_args == []
    assert not [e for e in session.events if e.type == TOOL_CALL]  # 合法桶为空，无 tool 事件
    completed = [e for e in session.events if e.type == MODEL_COMPLETED]
    assert [e.data["content"] for e in completed] == [
        "这是被截断的半句话", "第二次才是真回答",
    ]
    assert any(e.type == RUN_COMPLETED for e in session.events)
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_c_form_empty_content_error_names_unparsable_chunks(tmp_path, caplog) -> None:
    """#479：C 形态且 content 为空 ⇒ R6-2 空响应守卫先于 #449 防线触发，失败
    兜底语义与归因不变；但错误消息必须区分两种形态——点名本轮存在无法解析的
    tool_call_chunks（likely truncated），不得再误导为纯 empty response。
    （消息按 OBS-008 不进 model/failed 事件，只由诊断日志承载——断言 log。）"""
    tool = EchoTool()
    model = _SalvageChunkModel([
        [
            AIMessageChunk(
                content="",
                tool_call_chunks=[{
                    "name": "echo",
                    "args": "not json at all",
                    "id": TRUNCATED_CALL_ID,
                    "index": 0,
                    "type": "tool_call_chunk",
                }],
            ),
            AIMessageChunk(content="", response_metadata={"finish_reason": "length"}),
        ],
    ])
    runtime = _runtime(model, tool)
    session = make_session(tmp_path)

    with caplog.at_level(logging.INFO, logger="agent_harness.agent"):
        async for _ in runtime.run_stream(session, "hi"):
            pass

    # 失败兜底与归因不变：model/failed + run/failed 收尾、无 model/completed、零执行
    assert tool.executed_args == []
    types = [e.type for e in session.events]
    assert MODEL_FAILED in types
    assert types[-1] == RUN_FAILED
    assert not [e for e in session.events if e.type == MODEL_COMPLETED]
    # 错误消息区分形态：点名 unparsable tool_call_chunks（数量 + likely truncated）。
    # task_failed 生命周期日志按项目约定走 INFO 级（outcome=error 字段承载严重度）。
    logged_errors = [
        str(getattr(r, "error", ""))
        for r in caplog.records
        if r.name == "agent_harness.agent" and getattr(r, "event_type", "") == "task_failed"
    ]
    assert any(
        "1 unparsable tool_call_chunks" in e and "likely truncated" in e
        for e in logged_errors
    ), logged_errors


@pytest.mark.asyncio
async def test_no_truncation_path_unchanged(tmp_path) -> None:
    """金钉：finish_reason="stop" 的同形状剧本行为逐字节不变——工具照常执行。"""
    tool = EchoTool()
    model = ScriptedModel([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "echo",
                "args": {"command": "rm -rf x", "suffix": "y"},
                "id": TRUNCATED_CALL_ID,
                "type": "tool_call",
            }],
            response_metadata={"finish_reason": "stop"},
        ),
        AIMessage(content="完成。"),
    ])
    runtime = _runtime(model, tool)
    session = make_session(tmp_path)

    result = await runtime.run(session, "hi")

    assert result.status == STATUS_COMPLETED
    assert tool.executed_args == [{"command": "rm -rf x", "suffix": "y"}]  # 照常执行
    result_events = [e for e in session.events if e.type == TOOL_RESULT]
    payload = json.loads(result_events[0].data["content"])
    assert payload["ok"] is True
    assert "error_code" not in payload or payload["error_code"] is None
