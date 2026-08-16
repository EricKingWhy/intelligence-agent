"""Day2 Task3：用 ScriptedModel 验证两轮协议的消息顺序与 ID 配对。

全程零 API 调用、零 Token：模型响应是固定剧本，测试断言的是
Runtime（tool_call_demo.run）发给"模型"的请求是否符合协议。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from agent_harness.tool_call_demo import run
from tests.scripted_model import ScriptedModel

TOOL_CALL_ID = "call_scripted_0001"


def make_scripted_model() -> ScriptedModel:
    """剧本：第一轮提出 add 调用，第二轮基于结果给最终回答。"""
    round1 = AIMessage(
        content="我来帮你计算",
        tool_calls=[
            {
                "name": "add",
                "args": {"first_number": 123, "second_number": 456},
                "id": TOOL_CALL_ID,
                "type": "tool_call",
            }
        ],
    )
    round2 = AIMessage(content="123 + 456 = 579")
    return ScriptedModel([round1, round2])


class TestTwoRoundProtocol:
    @pytest.mark.asyncio
    async def test_exactly_two_calls_and_tools_bound(self):
        """两轮调用、工具已绑定：ScriptedModel 的基础行为。"""
        scripted = make_scripted_model()
        await run("计算 123 + 456", model=scripted)

        assert len(scripted.snapshots) == 2
        assert scripted.bound_tools is not None
        assert scripted.bound_tools[0]["name"] == "add"

    @pytest.mark.asyncio
    async def test_second_round_message_order_and_id_pairing(self):
        """核心断言：第二轮消息链顺序 + tool_call_id 配对。"""
        scripted = make_scripted_model()
        await run("计算 123 + 456", model=scripted)

        second_round = scripted.snapshots[1].messages

        # 3B：断言消息链顺序——3 条消息，类型依次为 Human / AI / Tool
        message_types = [type(m).__name__ for m in second_round]
        assert message_types == ["HumanMessage", "AIMessage", "ToolMessage"]

        # 3C：断言 ID 配对——回填的 ToolMessage 必须指向模型发出的那个 tool_call
        assert second_round[2].tool_call_id == TOOL_CALL_ID
        assert second_round[1].tool_calls[0]["id"] == TOOL_CALL_ID

    @pytest.mark.asyncio
    async def test_script_exhausted_raises_clearly(self):
        """剧本耗尽：明确报错，绝不静默循环或重复最后一条。"""
        # 只有第一轮（带 tool_call）的剧本：demo 会执行工具并发起第二次
        # 调用，此时剧本耗尽，必须明确报错而不是返回错误的数据
        round1_only = AIMessage(
            content="我来帮你计算",
            tool_calls=[
                {
                    "name": "add",
                    "args": {"first_number": 123, "second_number": 456},
                    "id": TOOL_CALL_ID,
                    "type": "tool_call",
                }
            ],
        )
        scripted = ScriptedModel([round1_only])
        with pytest.raises(RuntimeError, match="剧本耗尽"):
            await run("计算 123 + 456", model=scripted)
