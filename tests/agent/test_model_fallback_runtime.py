"""Model Fallback 在真实 AgentRuntime 循环中的集成测试（T5, #80, ADR-0014）。

验证：
1. primary 瞬时失败 → 切 fallback 重试 → model/fallback 事件持久化（白盒）；
2. 非瞬时错误（参数错）不切 → 走统一失败兜底；
3. 未配 fallback → 原样失败，无 fallback 事件；
4. 切换后本 run 后续步骤沿用 fallback（never 切回，只一条事件）；
5. 流式路径（run_stream）同样支持切换。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent.runtime import AgentRuntime
from agent_harness.model.fallback import TwoLevelFallbackPolicy
from agent_harness.session import MODEL_FAILED, MODEL_FALLBACK
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _EchoArgs(BaseModel):
    text: str = Field(default="x", description="回显文本")


class EchoTool(Tool):
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


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EchoTool())
    return reg


def _tool_call(name: str, args: dict, idx: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"call_{idx:04d}", "name": name, "args": args}],
    )


class FailOnceModel:
    """前 fail_times 次 ainvoke/astream 抛指定异常，之后委托 inner（记录切换）。"""

    def __init__(self, inner: Any, fail_times: int, error: Exception) -> None:
        self._inner = inner
        self._fail_times = fail_times
        self._error = error
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        self._inner.bind_tools(tools, **kwargs)
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        return await self._inner.ainvoke(messages, **kwargs)

    async def astream(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        async for chunk in self._inner.astream(messages, **kwargs):
            yield chunk


def _runtime(primary: Any, fallback: Any | None) -> AgentRuntime:
    return AgentRuntime(
        model=primary, registry=_registry(), executor=ToolExecutor(_registry()),
        max_steps=10,
        fallback_model=fallback,
        fallback_policy=TwoLevelFallbackPolicy(),
        primary_model_name="primary-model",
        fallback_model_name="fallback-model",
    )


class TestModelFallbackInLoop:
    @pytest.mark.asyncio
    async def test_transient_failure_switches_and_emits_event(self, tmp_path):
        """primary 首轮超时 → 切 fallback 完成回答 + model/fallback 事件持久化。"""
        primary = FailOnceModel(
            ScriptedModel([AIMessage(content="unused")]),
            fail_times=1, error=TimeoutError("primary down"),
        )
        fallback = ScriptedModel([AIMessage(content="fallback 的回答")])
        runtime = _runtime(primary, fallback)
        session = make_session(tmp_path)

        result = await runtime.run(session, "你好")

        assert result.status == "completed"
        assert result.final_text == "fallback 的回答"
        events = [e for e in session._events if e.type == MODEL_FALLBACK]
        assert len(events) == 1
        data = events[0].data
        assert data["from_model"] == "primary-model"
        assert data["to_model"] == "fallback-model"
        assert data["reason"] == "TimeoutError"

    @pytest.mark.asyncio
    async def test_non_transient_error_does_not_switch(self, tmp_path):
        """参数错（ValueError）非瞬时：不切 fallback，走统一失败兜底。"""
        primary = FailOnceModel(
            ScriptedModel([AIMessage(content="unused")]),
            fail_times=99, error=ValueError("bad request shape"),
        )
        fallback = ScriptedModel([AIMessage(content="不该被用到")])
        runtime = _runtime(primary, fallback)
        session = make_session(tmp_path)

        result = await runtime.run(session, "你好")

        assert result.status == "failed"
        assert not any(e.type == MODEL_FALLBACK for e in session._events)
        # model/failed 归因到具体一步（失败兜底不变量）
        assert any(e.type == MODEL_FAILED for e in session._events)

    @pytest.mark.asyncio
    async def test_no_fallback_configured_fails_plainly(self, tmp_path):
        """未配 fallback：瞬时错误也无处可切 → 原样失败，无 fallback 事件。"""
        primary = FailOnceModel(
            ScriptedModel([AIMessage(content="unused")]),
            fail_times=99, error=TimeoutError("primary down"),
        )
        runtime = _runtime(primary, None)
        session = make_session(tmp_path)

        result = await runtime.run(session, "你好")

        assert result.status == "failed"
        assert not any(e.type == MODEL_FALLBACK for e in session._events)

    @pytest.mark.asyncio
    async def test_fallback_persists_across_steps_never_switch_back(self, tmp_path):
        """切换发生在带 tool_calls 的轮次：后续步骤沿用 fallback，全程仅一条事件。"""
        primary = FailOnceModel(
            ScriptedModel([AIMessage(content="unused")]),
            fail_times=1, error=TimeoutError("primary down"),
        )
        # fallback 剧本：第 1 轮发 tool_call，第 2 轮给最终回答
        fallback = ScriptedModel([
            _tool_call("echo", {"text": "hi"}, 0),
            AIMessage(content="工具之后的最终回答"),
        ])
        runtime = _runtime(primary, fallback)
        session = make_session(tmp_path)

        result = await runtime.run(session, "用 echo 工具")

        assert result.status == "completed"
        assert result.final_text == "工具之后的最终回答"
        assert result.steps == 2
        events = [e for e in session._events if e.type == MODEL_FALLBACK]
        assert len(events) == 1  # never 切回：全程只切一次
        assert primary.calls == 1  # 切换后不再碰 primary

    @pytest.mark.asyncio
    async def test_stream_path_also_switches(self, tmp_path):
        """流式入口（run_stream）：primary 瞬时失败 → fallback 接管完成回答。"""
        primary = FailOnceModel(
            ScriptedModel([AIMessage(content="unused")]),
            fail_times=1, error=TimeoutError("primary down"),
        )
        fallback = ScriptedModel([AIMessage(content="流式回答")])
        runtime = _runtime(primary, fallback)
        session = make_session(tmp_path)

        final_text = ""
        async for event in runtime.run_stream(session, "你好"):
            if event.type == "model/delta":
                final_text += event.data["delta"]

        assert "流式回答" in final_text
        events = [e for e in session._events if e.type == MODEL_FALLBACK]
        assert len(events) == 1
        assert events[0].data["reason"] == "TimeoutError"
