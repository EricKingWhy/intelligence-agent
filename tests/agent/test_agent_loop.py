"""Day3 Task1+2：用 ScriptedModel 验证最小 Agent Loop 的四象限行为。

零 API、零 Token：模型响应是固定剧本，断言的是 Runtime 实际发给模型的
消息链（snapshots）和返回的 AgentRunResult，而不是只看最终字符串。

四组场景（覆盖 Day 3 Definition of Done 的行为矩阵）：
- A（Task1）：无工具——模型首轮就给最终回答，Runtime 只调用 1 次。
- B（Task1）：一次工具往返——首轮提议 add，回填，第二轮给最终回答。
- C（Task2）：连续两轮工具——证明 Loop 能跨第 2 轮继续走第 3 轮。
- D（Task2）：max_steps 兜底——模型不收敛时保险丝正确熔断。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_MAX_STEPS_EXCEEDED
from tests.scripted_model import ScriptedModel

TOOL_CALL_ID = "call_agent_0001"


# ---- 夹具：今天 Runtime 用到的唯一工具，一个同步 add ----
def _add(first_number: float, second_number: float) -> float:
    """add 工具：和 Day 2 同名同参，方便比对协议一致性。"""
    return first_number + second_number


def _tools() -> dict[str, object]:
    """tools dict：name -> 可调用对象。今天只有 add。"""
    return {"add": _add}


# ---------- 路径 A：无工具直接完成 ----------
def _scripted_no_tool() -> ScriptedModel:
    """剧本：唯一一轮，模型直接给最终回答，不提议任何工具。"""
    return ScriptedModel([AIMessage(content="你好，我是助手")])


class TestAgentLoopNoTool:
    @pytest.mark.asyncio
    async def test_completes_in_one_step(self):
        """A：无工具 -> completed + steps=1 + 模型只被调用 1 次。"""
        scripted = _scripted_no_tool()
        runtime = AgentRuntime(model=scripted, tools=_tools())

        result = await runtime.run("你好")

        # —— 你来断言（A 核心）——
        # 1. status 必须是 completed
        # 2. steps 必须是 1
        # 3. final_text 必须等于剧本那一轮的 content
        # 4. scripted.snapshots 长度必须是 1（模型只被调用 1 次）
        assert result.status == STATUS_COMPLETED
        assert result.steps == 1
        assert result.final_text == "你好，我是助手"
        assert len(scripted.snapshots) == 1


# ---------- 路径 B：一次工具往返 ----------
def _scripted_one_tool() -> ScriptedModel:
    """剧本：第一轮提议 add，第二轮基于结果给最终回答。"""
    round1 = AIMessage(
        content="",
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


class TestAgentLoopOneTool:
    @pytest.mark.asyncio
    async def test_two_steps_and_completed(self):
        """B：一次工具往返 -> completed + steps=2 + 模型被调用 2 次。"""
        scripted = _scripted_one_tool()
        runtime = AgentRuntime(model=scripted, tools=_tools())

        result = await runtime.run("计算 123 + 456")

        # —— 你来断言（B 基本）——
        # 1. status == completed
        # 2. steps == 2
        # 3. final_text == "123 + 456 = 579"
        # 4. len(scripted.snapshots) == 2
        assert result.status == STATUS_COMPLETED
        assert result.steps == 2
        assert result.final_text == "123 + 456 = 579"
        assert len(scripted.snapshots) == 2

    @pytest.mark.asyncio
    async def test_second_round_message_order_and_id_pairing(self):
        """B 核心：第二轮发给模型的消息链顺序 + tool_call_id 配对。

        这是 Task 1 最值得保护的协议不变量——和 Day 2 同样的断言，
        现在落在新 Runtime 上，证明循环维护了相同的消息契约。
        """
        scripted = _scripted_one_tool()
        runtime = AgentRuntime(model=scripted, tools=_tools())
        await runtime.run("计算 123 + 456")

        second_round = scripted.snapshots[1].messages

        # —— 你来断言（B 协议核心）——
        # 1. 第二轮消息链类型顺序 == ["HumanMessage", "AIMessage", "ToolMessage"]
        #    提示：message_types = [type(m).__name__ for m in second_round]
        # 2. AIMessage 里的 tool_call id == TOOL_CALL_ID
        #    提示：second_round[1].tool_calls[0]["id"]
        # 3. ToolMessage 的 tool_call_id == TOOL_CALL_ID（回填必须用原 id 配对）
        #    提示：second_round[2].tool_call_id
        message_types = [type(m).__name__ for m in second_round]
        assert message_types == ["HumanMessage", "AIMessage", "ToolMessage"]
        assert second_round[1].tool_calls[0]["id"] == TOOL_CALL_ID
        assert second_round[2].tool_call_id == TOOL_CALL_ID


# ---------- Task 2 · 场景③：连续两轮工具 ----------
# 剧本设计：模型第一轮提议 add(1,2)，第二轮提议 add(3,4)，第三轮给最终回答。
# 关键：两个 tool_call 必须用【不同的 id】，证明 Loop 不是误重用同一个 id。
TOOL_CALL_ID_A = "call_two_round_A"
TOOL_CALL_ID_B = "call_two_round_B"


def _scripted_two_tool_rounds() -> ScriptedModel:
    """剧本：两轮工具提议（id 各不同）+ 第三轮最终回答。"""
    round1 = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "add",
                "args": {"first_number": 1, "second_number": 2},
                "id": TOOL_CALL_ID_A,
                "type": "tool_call",
            }
        ],
    )
    round2 = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "add",
                "args": {"first_number": 3, "second_number": 4},
                "id": TOOL_CALL_ID_B,
                "type": "tool_call",
            }
        ],
    )
    round3 = AIMessage(content="第一笔 3，第二笔 7")
    return ScriptedModel([round1, round2, round3])


class TestAgentLoopTwoConsecutiveToolRounds:
    @pytest.mark.asyncio
    async def test_three_steps_completed_and_snapshot_count(self):
        """连续两轮工具 -> completed + steps=3 + 模型被调用恰好 3 次。

        验收清单（你来断言）：
        1. status == STATUS_COMPLETED
        2. steps == 3
        3. len(scripted.snapshots) == 3
        4. result.final_text 是第三轮剧本的 content
        """
        scripted = _scripted_two_tool_rounds()
        runtime = AgentRuntime(model=scripted, tools=_tools())

        result = await runtime.run("连续算两笔")

        assert result.status == STATUS_COMPLETED
        assert result.steps == 3
        assert len(scripted.snapshots) == 3
        assert result.final_text == "第一笔 3，第二笔 7"

    @pytest.mark.asyncio
    async def test_third_round_full_message_trace_and_id_pairing(self):
        """场景③ 协议核心：第三轮发给模型的完整消息链（5 条）+ 两组 id 配对。

        这是 Task 2 最值得保护的协议不变量——证明 Loop 真的把两轮工具调用
        的请求与回填都累积进了历史，且各自的 tool_call_id 独立配对、互不串台。

        验收清单（你来断言）：
        1. 第三轮快照 scripted.snapshots[2].messages 共 5 条（模型第 3 次 ainvoke
           的输入），类型顺序严格为：
           ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage", "ToolMessage"]
        2. 第 1 条 AIMessage（下标 1）的 tool_call id == TOOL_CALL_ID_A
        3. 第 1 条 ToolMessage（下标 2）的 tool_call_id == TOOL_CALL_ID_A
        4. 第 2 条 AIMessage（下标 3）的 tool_call id == TOOL_CALL_ID_B
        5. 第 2 条 ToolMessage（下标 4）的 tool_call_id == TOOL_CALL_ID_B
        6. 两个 tool_call 的 id 不相等（A != B）——证明不是误重用同一个 id
        """
        scripted = _scripted_two_tool_rounds()
        runtime = AgentRuntime(model=scripted, tools=_tools())
        await runtime.run("连续算两笔")

        third_round = scripted.snapshots[2].messages

        assert len(third_round) == 5
        message_types = [type(m).__name__ for m in third_round]
        assert message_types == [
            "HumanMessage",
            "AIMessage",
            "ToolMessage",
            "AIMessage",
            "ToolMessage",
        ]
        assert third_round[1].tool_calls[0]["id"] == TOOL_CALL_ID_A
        assert third_round[2].tool_call_id == TOOL_CALL_ID_A
        assert third_round[3].tool_calls[0]["id"] == TOOL_CALL_ID_B
        assert third_round[4].tool_call_id == TOOL_CALL_ID_B
        assert TOOL_CALL_ID_A != TOOL_CALL_ID_B


# ---------- Task 2 · 场景④：max_steps 不收敛兜底 ----------
# 剧本设计：模型每轮都提议 add，永不收敛。
# 技巧提示：ScriptedModel 剧本吐完会抛 RuntimeError("剧本耗尽")。
#   要测"模型一直要工具"，你需要让剧本够长（>= max_steps 条）。
#   本次建议 max_steps=3，剧本放 3 条带 tool_call 的 AIMessage 即可——
#   Runtime 第 3 次 ainvoke 后 steps 到 3，触发兜底，不会再调第 4 次，剧本恰好够用。
TOOL_CALL_ID_LOOP = "call_loop"


class TestAgentLoopMaxSteps:
    @pytest.mark.asyncio
    async def test_max_steps_exceeded_with_exact_step_count(self):
        """模型不收敛 + max_steps=3 -> max_steps_exceeded + steps=3 + final_text="" + 恰好 3 次调用。

        这是 Task 1 Q3 的 off-by-N 答案在代码上的实证：steps 数模型轮数，
        所以 max_steps=3 意味着"允许调用模型 3 次"，第 3 次调用已发生、
        steps 到 3，此时检测到不收敛 -> 兜底，绝不调用第 4 次。

        验收清单（你来断言）：
        1. result.status == STATUS_MAX_STEPS_EXCEEDED
        2. result.final_text == ""            —— 兜底绝不伪造最终回答
        3. result.steps == 3                  —— 恰好撞线，不是 4
        4. len(scripted.snapshots) == 3       —— 模型被调用恰好 3 次，不多不少
        """
        rounds = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add",
                        "args": {"first_number": i, "second_number": i},
                        "id": TOOL_CALL_ID_LOOP,
                        "type": "tool_call",
                    }
                ],
            )
            for i in range(3)
        ]
        scripted = ScriptedModel(rounds)
        runtime = AgentRuntime(model=scripted, tools=_tools(), max_steps=3)

        result = await runtime.run("永远算不完")

        assert result.status == STATUS_MAX_STEPS_EXCEEDED
        assert result.final_text == ""
        assert result.steps == 3
        assert len(scripted.snapshots) == 3


# ---------- Task 3 · 场景⑤：失败边界（故障注入，剧本可控可重现） ----------
# 为什么这三类故障用剧本而不用真实模型？
#   真实模型看到 bind_tools([add]) 后永远不会调 multiply（测不了"未知工具"）；
#   工具异常和空 content 靠模型随机表现，不可稳定重现。
#   剧本是故障注入：精确复现真实模型"不愿配合"的路径，每次必现、零 Token。


class TestAgentLoopUnknownTool:
    """未知工具名：模型提议了 tools 里不存在的工具，必须回填错误而不是崩溃。"""

    @pytest.mark.asyncio
    async def test_unknown_tool_backfills_error_not_crash(self):
        """模型调 multiply（不存在）-> 不抛异常 -> 错误以 ToolMessage 回填 -> 模型最终回答。"""
        round1 = AIMessage(
            content="",
            tool_calls=[{
                "name": "multiply",
                "args": {"first_number": 3, "second_number": 4},
                "id": "call_unknown_001",
                "type": "tool_call",
            }],
        )
        round2 = AIMessage(content="抱歉，乘法工具不可用，我直接计算：3 × 4 = 12")
        scripted = ScriptedModel([round1, round2])
        runtime = AgentRuntime(model=scripted, tools=_tools())  # tools 里只有 add

        result = await runtime.run("计算 3 乘 4")

        # 不崩 + 正常完成（模型看到错误后自我纠错给了最终回答）
        assert result.status == STATUS_COMPLETED
        assert result.steps == 2
        # 核心：第二轮喂给模型的消息链里，ToolMessage 的 content 是错误信息，
        # 且 tool_call_id 依然配对原 id（错误回执也要能配对请求）。
        second_round = scripted.snapshots[1].messages
        assert [type(m).__name__ for m in second_round] == [
            "HumanMessage", "AIMessage", "ToolMessage",
        ]
        tool_msg = second_round[2]
        assert tool_msg.tool_call_id == "call_unknown_001"
        assert "multiply" in tool_msg.content   # 错误信息必须提到是哪个工具失败
        assert "KeyError" in tool_msg.content   # 用户实现带了异常类型，锁住它


class TestAgentLoopToolException:
    """工具内部异常：工具存在但执行时炸了，同样回填错误而不是崩溃。"""

    @pytest.mark.asyncio
    async def test_tool_exception_backfills_error_not_crash(self):
        """模型调 boom -> boom 抛 ValueError -> Runtime 捕获回填 -> 模型给最终回答。"""

        def boom(explode: bool = True) -> float:
            msg = "故意炸：工具内部状态错误"
            raise ValueError(msg)

        round1 = AIMessage(
            content="",
            tool_calls=[{
                "name": "boom",
                "args": {"explode": True},
                "id": "call_boom_001",
                "type": "tool_call",
            }],
        )
        round2 = AIMessage(content="boom 工具内部出错了，我换个方式回答")
        scripted = ScriptedModel([round1, round2])
        runtime = AgentRuntime(model=scripted, tools={"add": _add, "boom": boom})

        result = await runtime.run("触发爆炸")

        assert result.status == STATUS_COMPLETED
        assert result.steps == 2
        tool_msg = scripted.snapshots[1].messages[2]
        assert tool_msg.tool_call_id == "call_boom_001"
        assert "ValueError" in tool_msg.content     # 异常类型回填给了模型
        assert "故意炸" in tool_msg.content          # 异常消息也带上了


class TestAgentLoopEmptyContentWithToolCalls:
    """空 content + tool_calls：content 为空但模型在要工具，绝不能误停。"""

    @pytest.mark.asyncio
    async def test_empty_content_does_not_stop_loop(self):
        """第一轮 content='' 且带 tool_call -> 必须继续执行工具 -> 第二轮完成。

        这是 Task 1 Q2 的锁死版：content 空不空从来不是停止信号，
        tool_calls 空不空才是。这条测试防止未来有人"顺手"把停止条件改成 content。
        """
        round1 = AIMessage(
            content="",  # 空 content + 要工具：最容易被误判为"模型没话说"的场景
            tool_calls=[{
                "name": "add",
                "args": {"first_number": 2, "second_number": 3},
                "id": "call_empty_001",
                "type": "tool_call",
            }],
        )
        round2 = AIMessage(content="2 + 3 = 5")
        scripted = ScriptedModel([round1, round2])
        runtime = AgentRuntime(model=scripted, tools=_tools())

        result = await runtime.run("计算 2 + 3")

        # 关键断言：模型被调用了 2 次（没有在第 1 轮误停）
        assert result.status == STATUS_COMPLETED
        assert result.steps == 2
        assert result.final_text == "2 + 3 = 5"