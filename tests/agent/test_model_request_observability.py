"""#520 一期（IMP-01/04 裁决：provider 能力配置 + usage 可观察先行）。

`model/request` 诊断面的四组事实：

1. cached/uncached token 成对记录——归一化 `cache_read` 与 DeepSeek 原生
   `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 两个来源都认；
   缺失省略，绝不写 0（`11 §6.1`）。
2. monotonic 耗时：每次实际请求（primary / fallback / closeout / 失败尝试）
   落 `duration_ms`（并发槽获取后到成功/失败，排队不计——与 stall 看门狗同口径）。
3. fallback 复用同语义 messages（裁决放弃「必不同」断言后的不变量钉）。
4. 请求预算：每次实际请求都计入 `model_requests`（run 预算唯一计数点的钉）。
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from langchain_core.messages import AIMessage, AnyMessage
from pydantic import BaseModel, Field

from agent_harness.agent.run_budget import (
    REASON_BUDGET_EXHAUSTED,
    TRIGGER_LOCAL_TURNS,
    consumed_from_events,
)
from agent_harness.agent.runtime import AgentRuntime
from agent_harness.model.fallback import TwoLevelFallbackPolicy
from agent_harness.session import MODEL_REQUEST, RUN_PAUSED
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _AddArgs(BaseModel):
    first_number: Annotated[float, Field(..., description="第一个加数")]
    second_number: Annotated[float, Field(..., description="第二个加数")]


class AddTool(Tool):
    """closeout 场景的最小工具：剧本轮请求 add，回填后继续。"""

    @property
    def name(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "计算两个数的和。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _AddArgs

    async def execute(self, args: _AddArgs) -> ToolResult:
        return ToolResult.success(
            message=str(args.first_number + args.second_number),
            data={"sum": args.first_number + args.second_number},
        )


class _FailOnceModel:
    """第一次调用抛指定异常（记录收到的 messages），之后委托 inner。"""

    def __init__(self, inner: Any, error: Exception) -> None:
        self._inner = inner
        self._error = error
        self.calls = 0
        self.seen_messages: list[list[AnyMessage]] = []

    def bind_tools(self, tools, **kwargs):
        self._inner.bind_tools(tools, **kwargs)
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        self.seen_messages.append(list(messages))
        if self.calls == 1:
            raise self._error
        return await self._inner.ainvoke(messages, **kwargs)


def _runtime(
    primary: Any, fallback: Any | None = None, *, max_agent_turns: int = 10,
) -> AgentRuntime:
    reg = ToolRegistry()
    reg.register(AddTool())
    return AgentRuntime(
        model=primary, registry=reg, executor=ToolExecutor(reg),
        max_agent_turns=max_agent_turns,
        fallback_model=fallback, fallback_policy=TwoLevelFallbackPolicy(),
        primary_model_name="primary-model", fallback_model_name="fallback-model",
    )


def _requests(session) -> list:
    return [e for e in session.events if e.type == MODEL_REQUEST]


class TestCachedUncachedTokenPair:
    @pytest.mark.asyncio
    async def test_uncached_derived_from_normalized_cache_read(self, tmp_path):
        """归一化 `cache_read`（OpenAI 线）：uncached = input - cached。"""
        model = ScriptedModel([AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                "input_token_details": {"cache_read": 3},
            },
        )])
        session = make_session(tmp_path)
        await _runtime(model).run(session, "你好")

        completed = [e for e in _requests(session) if e.data["outcome"] == "completed"]
        assert len(completed) == 1
        usage = completed[0].data["usage"]
        assert usage["prompt_tokens"] == 10
        assert usage["cached_tokens"] == 3
        assert usage["uncached_tokens"] == 7

    @pytest.mark.asyncio
    async def test_deepseek_native_hit_miss_recorded(self, tmp_path):
        """DeepSeek 原生 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
        只在 `response_metadata.token_usage` 可见（langchain 不归一化它们）：
        cached 取 hit、uncached 取显式 miss。"""
        model = ScriptedModel([AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            response_metadata={
                "model_name": "deepseek-chat",
                "token_usage": {
                    "prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9,
                    "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 2,
                },
            },
        )])
        session = make_session(tmp_path)
        await _runtime(model).run(session, "你好")

        completed = [e for e in _requests(session) if e.data["outcome"] == "completed"]
        assert len(completed) == 1
        usage = completed[0].data["usage"]
        assert usage["cached_tokens"] == 5
        assert usage["uncached_tokens"] == 2
        # 实际 model ID 随响应元数据落事件（裁决项 1）
        assert completed[0].data["model"] == "deepseek-chat"

    @pytest.mark.asyncio
    async def test_missing_cache_fields_omitted_never_zero(self, tmp_path):
        """provider 没报缓存明细 ⇒ 两个键都省略（不可得 ≠ 0）。"""
        model = ScriptedModel([AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
        )])
        session = make_session(tmp_path)
        await _runtime(model).run(session, "你好")

        completed = [e for e in _requests(session) if e.data["outcome"] == "completed"]
        usage = completed[0].data["usage"]
        assert "cached_tokens" not in usage
        assert "uncached_tokens" not in usage


class TestRequestDurationMs:
    @pytest.mark.asyncio
    async def test_completed_and_failed_attempts_carry_duration(self, tmp_path):
        """primary 失败 + fallback 完成：两格 `model/request` 都带 `duration_ms`。"""
        primary = _FailOnceModel(
            ScriptedModel([AIMessage(content="unused")]),
            error=TimeoutError("primary down"),
        )
        fallback = ScriptedModel([AIMessage(content="fallback 的回答")])
        session = make_session(tmp_path)

        result = await _runtime(primary, fallback).run(session, "你好")

        assert result.status == "completed"
        requests = _requests(session)
        assert [(e.data["role"], e.data["outcome"]) for e in requests] == [
            ("primary", "failed"), ("fallback", "completed"),
        ]
        for event in requests:
            duration = event.data["duration_ms"]
            assert isinstance(duration, int) and duration >= 0

    @pytest.mark.asyncio
    async def test_closeout_request_carries_duration(self, tmp_path):
        """closeout 绕过 coordinator，由收口调用点自己测量——同样带 `duration_ms`。"""
        loop_rounds = [
            AIMessage(content="", tool_calls=[{
                "name": "add", "args": {"first_number": i, "second_number": i},
                "id": f"call_loop_{i:04d}", "type": "tool_call",
            }])
            for i in range(4)
        ]
        model = ScriptedModel(loop_rounds)
        session = make_session(tmp_path)

        result = await _runtime(model, max_agent_turns=3).run(session, "永远算不完")

        assert result.status == "paused"
        assert any(e.type == RUN_PAUSED for e in session.events)
        closeout = [
            e for e in _requests(session) if e.data["role"] == "closeout"
        ]
        assert len(closeout) == 1
        assert isinstance(closeout[0].data["duration_ms"], int)
        assert closeout[0].data["duration_ms"] >= 0


class TestFallbackMessagesInvariance:
    @pytest.mark.asyncio
    async def test_fallback_receives_same_semantic_messages(self, tmp_path):
        """裁决放弃「primary/fallback messages 必不同」：fallback 收到与 primary
        **同语义**的 messages（同内容序列），钉住不回归。"""
        primary = _FailOnceModel(
            ScriptedModel([AIMessage(content="unused")]),
            error=TimeoutError("primary down"),
        )
        fallback = ScriptedModel([AIMessage(content="fallback 的回答")])
        session = make_session(tmp_path)

        await _runtime(primary, fallback).run(session, "你好")

        assert len(primary.seen_messages) == 1
        fallback_seen = fallback.snapshots[0].messages
        assert [m.content for m in primary.seen_messages[0]] == [
            m.content for m in fallback_seen
        ]


class TestRequestBudgetAccounting:
    @pytest.mark.asyncio
    async def test_every_actual_request_counts_into_budget(self, tmp_path):
        """请求预算钉：失败与成功的每次实际请求都占 `model_requests` 一格。"""
        primary = _FailOnceModel(
            ScriptedModel([AIMessage(content="unused")]),
            error=TimeoutError("primary down"),
        )
        fallback = ScriptedModel([AIMessage(
            content="fallback 的回答",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )])
        session = make_session(tmp_path)

        await _runtime(primary, fallback).run(session, "你好")

        requests = _requests(session)
        assert len(requests) == 2
        consumed = consumed_from_events(session.events)
        assert consumed.model_requests == 2
        # 失败的那格没有自报 usage ⇒ run 级 token 维转未知并粘住（`11 §6.1`
        # 「不可得 ≠ 0」的计数纪律），不是把已知部分报成假精确总数。
        assert consumed.total_tokens is None

    @pytest.mark.asyncio
    async def test_closeout_request_counts_into_budget(self, tmp_path):
        """closeout 那次真实请求同样计数（`02 §5.1`：不进 agent_turns，进请求数）。"""
        loop_rounds = [
            AIMessage(content="", tool_calls=[{
                "name": "add", "args": {"first_number": i, "second_number": i},
                "id": f"call_loop_{i:04d}", "type": "tool_call",
            }])
            for i in range(4)
        ]
        model = ScriptedModel(loop_rounds)
        session = make_session(tmp_path)

        result = await _runtime(model, max_agent_turns=3).run(session, "永远算不完")

        assert result.status == "paused"
        paused = [e for e in session.events if e.type == RUN_PAUSED]
        assert paused[0].data["consumed"]["model_requests"] == 4
        assert paused[0].data["reason"] == REASON_BUDGET_EXHAUSTED
        assert paused[0].data["trigger_dimension"] == TRIGGER_LOCAL_TURNS
