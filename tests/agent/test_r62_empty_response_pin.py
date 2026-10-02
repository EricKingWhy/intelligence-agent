"""#507：R6-2 纯空响应分支的文案级钉住（纯测试补钉，零产品代码改动）。

失败语义已有测试钉住（test_runtime_failure_paths.py 的 stream/non-stream 参数化
断言 model/failed + run/failed），但文案字符串本身变化不会红——未来重构可能在
无人察觉的情况下改掉排障者依赖的措辞。本文件锁死 task_failed 诊断日志承载的
完整文案（OBS-008：文案只进诊断日志，事件侧仍只有类型名）。
"""

from __future__ import annotations

import logging

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.session import MODEL_COMPLETED, MODEL_FAILED, RUN_FAILED
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel
from tests.task_failed_errors import task_failed_errors

EXPECTED_MESSAGE = "model returned an empty response (no content, no tool calls)"


@pytest.mark.asyncio
async def test_pure_empty_response_message_pinned(tmp_path, caplog) -> None:
    """真·空响应（content/tool_calls/invalid_tool_calls 全空、非截断）⇒ 纯空响应
    文案逐字钉住；失败兜底与归因不变（model/failed + run/failed 收尾、零执行）。"""
    runtime = AgentRuntime(
        model=ScriptedModel([AIMessage(content="")]),
        registry=ToolRegistry(),
        executor=ToolExecutor(ToolRegistry()),
        max_agent_turns=5,
    )
    session = make_session(tmp_path)

    with caplog.at_level(logging.INFO, logger="agent_harness.agent"):
        async for _ in runtime.run_stream(session, "hi"):
            pass

    types = [e.type for e in session.events]
    assert MODEL_FAILED in types
    assert types[-1] == RUN_FAILED
    assert not [e for e in session.events if e.type == MODEL_COMPLETED]
    logged = task_failed_errors(caplog)
    assert any(e == EXPECTED_MESSAGE for e in logged), logged
