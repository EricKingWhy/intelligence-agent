"""用 ScriptedModel 验证最小 Agent Loop 的四象限行为。

零 API、零 Token：模型响应是固定剧本，断言的是 Runtime 实际发给模型的
消息链（snapshots）和返回的 AgentRunResult，而不是只看最终字符串。

四组场景：
- A：无工具——模型首轮就给最终回答，Runtime 只调用 1 次。
- B：一次工具往返——首轮提议 add，回填，第二轮给最终回答。
- C：连续两轮工具——证明 Loop 能跨第 2 轮继续走第 3 轮。
- D：local fuse 到顶——模型不收敛时以**非终态** `run/paused` 收口（`#312` T4：撞保险丝
  不再是失败，而是一次可恢复的暂停；被暂停的 run 用同一 run_id 续跑）。
"""

from __future__ import annotations

from typing import Annotated

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    REASON_BUDGET_EXHAUSTED,
    TRIGGER_LOCAL_TURNS,
    TRIGGER_RUN_TURNS,
    LaunchRunBudget,
    RunLimits,
)
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_PAUSED
from agent_harness.session import RUN_COMPLETED, RUN_FAILED, RUN_PAUSED
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

TOOL_CALL_ID = "call_agent_0001"


# ---- 夹具：Tool Contract + Registry + Executor ----


class _AddArgs(BaseModel):
    first_number: Annotated[float, Field(..., description="第一个加数")]
    second_number: Annotated[float, Field(..., description="第二个加数")]


class AddTool(Tool):
    """add 工具：走统一 Contract（name/description/args_schema/execute）。"""

    @property
    def name(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "计算两个数的和。参数：first_number、second_number 为加数。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _AddArgs

    async def execute(self, args: _AddArgs) -> ToolResult:
        return ToolResult.success(
            message=f"{args.first_number} + {args.second_number} = "
            f"{args.first_number + args.second_number}",
            data={"sum": args.first_number + args.second_number},
        )


class _EmptyArgs(BaseModel):
    """boom 工具无必填参数（旧函数 boom(explode=True) 的 explode 有默认值）。"""


class BoomTool(Tool):
    """boom 工具：execute 内部抛 ValueError，用来测执行异常回填。"""

    @property
    def name(self) -> str:
        return "boom"

    @property
    def description(self) -> str:
        return "总是抛 ValueError 的测试工具。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EmptyArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        raise ValueError("故意炸：工具内部状态错误")


def _registry() -> ToolRegistry:
    """注册 add + boom 的 Registry。multiply 故意不注册（测 TOOL_NOT_FOUND）。"""
    reg = ToolRegistry()
    reg.register(AddTool())
    reg.register(BoomTool())
    return reg


def _runtime(
    model: ScriptedModel, max_agent_turns: int = 20,
    run_budget: LaunchRunBudget | None = None,
) -> AgentRuntime:
    """构造绑定了 executor + registry 的 Runtime。"""
    reg = _registry()
    return AgentRuntime(
        model=model, registry=reg, executor=ToolExecutor(reg),
        max_agent_turns=max_agent_turns, run_budget=run_budget,
    )


# ---------- 路径 A：无工具直接完成 ----------
def _scripted_no_tool() -> ScriptedModel:
    """剧本：唯一一轮，模型直接给最终回答，不提议任何工具。"""
    return ScriptedModel([AIMessage(content="你好，我是助手")])


class TestAgentLoopNoTool:
    @pytest.mark.asyncio
    async def test_completes_in_one_step(self, tmp_path):
        """A：无工具 -> completed + steps=1 + 模型只被调用 1 次。"""
        scripted = _scripted_no_tool()
        runtime = _runtime(scripted)

        result = await runtime.run(make_session(tmp_path), "你好")


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
    async def test_two_steps_and_completed(self, tmp_path):
        """B：一次工具往返 -> completed + steps=2 + 模型被调用 2 次。"""
        scripted = _scripted_one_tool()
        runtime = _runtime(scripted)

        result = await runtime.run(make_session(tmp_path), "计算 123 + 456")


        # 1. status == completed
        # 2. steps == 2
        # 3. final_text == "123 + 456 = 579"
        # 4. len(scripted.snapshots) == 2
        assert result.status == STATUS_COMPLETED
        assert result.steps == 2
        assert result.final_text == "123 + 456 = 579"
        assert len(scripted.snapshots) == 2

    @pytest.mark.asyncio
    async def test_second_round_message_order_and_id_pairing(self, tmp_path):
        """第二轮发给模型的消息链顺序 + tool_call_id 配对。

        协议不变量：ToolMessage 的 tool_call_id 必须与 AIMessage 里的 tool_call id 一致。
        """
        scripted = _scripted_one_tool()
        runtime = _runtime(scripted)
        await runtime.run(make_session(tmp_path), "计算 123 + 456")

        second_round = scripted.snapshots[1].messages

        message_types = [type(m).__name__ for m in second_round]
        assert message_types == ["HumanMessage", "AIMessage", "ToolMessage"]
        assert second_round[1].tool_calls[0]["id"] == TOOL_CALL_ID
        assert second_round[2].tool_call_id == TOOL_CALL_ID


# ---------- 场景③：连续两轮工具 ----------
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
    async def test_three_steps_completed_and_snapshot_count(self, tmp_path):
        """连续两轮工具 -> completed + steps=3 + 模型被调用恰好 3 次。"""
        scripted = _scripted_two_tool_rounds()
        runtime = _runtime(scripted)

        result = await runtime.run(make_session(tmp_path), "连续算两笔")

        assert result.status == STATUS_COMPLETED
        assert result.steps == 3
        assert len(scripted.snapshots) == 3
        assert result.final_text == "第一笔 3，第二笔 7"

    @pytest.mark.asyncio
    async def test_third_round_full_message_trace_and_id_pairing(self, tmp_path):
        """第三轮发给模型的完整消息链（5 条）+ 两组 id 配对。

        协议不变量：两轮工具调用的请求与回填都累积进历史，各自的 tool_call_id
        独立配对、互不串台。
        """
        scripted = _scripted_two_tool_rounds()
        runtime = _runtime(scripted)
        await runtime.run(make_session(tmp_path), "连续算两笔")

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


# ---------- 场景④：local fuse 到顶 -> 非终态暂停（#312 T4） ----------
TOOL_CALL_ID_LOOP = "call_loop"


def _loop_rounds(count: int) -> list[AIMessage]:
    """`count` 轮"仍在请求工具"的剧本（永不收敛）。"""
    return [
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
        for i in range(count)
    ]


class TestAgentLoopLocalFusePause:
    @pytest.mark.asyncio
    async def test_local_fuse_pauses_with_exact_step_count(self, tmp_path):
        """模型不收敛 + local fuse=3 -> 非终态 `run/paused`（不再是失败）+ steps=3。

        `#312` T4 起"撞保险丝"的裁决是暂停：本地 fuse 与 run ceiling 走**同一条**
        暂停臂（`02 §5.2`）。判定点在**循环顶部**（任何模型调用之前）。

        fuse 与 run ceiling 的容量口径不同（`02 §5.1` 是三个独立计数器）：ceiling 里
        含一轮 closeout 预留，fuse **不含**——它数的是"本执行接纳了多少轮"（PRD EB-2：
        500 步到顶就是到顶）。所以 fuse=3 放行 3 轮普通工作，第 4 次模型调用才是
        **有界 closeout**（它拿到的剧本不是 continuation JSON ⇒ 回落到确定性
        continuation，不伪造进展）。
        """
        scripted = ScriptedModel(_loop_rounds(4))
        runtime = _runtime(scripted, max_agent_turns=3)
        session = make_session(tmp_path)

        result = await runtime.run(session, "永远算不完")

        assert result.status == STATUS_PAUSED
        assert result.final_text == ""
        assert result.steps == 3
        # 3 次普通轮 + 1 次 closeout：closeout 也必须显式过一次模型（有界机会）
        assert len(scripted.snapshots) == 4
        closeout_request = scripted.snapshots[3].messages[-1]
        assert closeout_request.type == "human"
        assert TRIGGER_LOCAL_TURNS in closeout_request.content

        # 落盘事实：恰好一条 run/paused，**没有**任何终态（暂停不是完成也不是失败）
        paused_events = [e for e in session.events if e.type == RUN_PAUSED]
        assert len(paused_events) == 1
        assert not [e for e in session.events if e.type in (RUN_COMPLETED, RUN_FAILED)]
        data = paused_events[0].data
        assert data["reason"] == REASON_BUDGET_EXHAUSTED
        assert data["trigger_dimension"] == TRIGGER_LOCAL_TURNS
        assert data["budget_version"] == 1
        # 计数点是"被接纳的普通轮"：closeout 那一次是 model_requests，不进 agent_turns
        # （那次请求没报 usage / cost ⇒ 那两个维度记未知，不记 0——`11 §6.1`）
        assert data["consumed"] == {
            "agent_turns": 3, "model_requests": 4, "total_tokens": None, "cost_usd": None,
        }
        # 两个作用域各自的原生投影（命中哪个维度由 trigger_dimension 指明）
        assert data["limits"]["local"]["max_agent_turns"] == 3
        assert data["limits"]["run"]["max_agent_turns_total"] is None
        assert data["closeout_source"] == CLOSEOUT_DETERMINISTIC
        assert data["resume_requirements"] == []
        assert set(data["continuation"]) == {
            "completed", "remaining", "blockers", "next_safe_action",
        }

    @pytest.mark.asyncio
    async def test_convergence_before_the_fuse_completes(self, tmp_path):
        """回归：模型在保险丝**到顶之前**收敛（无 tool_calls）-> completed，不是暂停。

        停止信号（无 tool_calls）必须先于预算兜底判定，否则最终回答会被误报成
        "预算到顶"并把 final_text 丢掉（`#312` 保留了这条顺序）。
        """
        rounds = _loop_rounds(1) + [AIMessage(content="总算算完了")]
        scripted = ScriptedModel(rounds)
        runtime = _runtime(scripted, max_agent_turns=3)

        result = await runtime.run(make_session(tmp_path), "收敛")

        assert result.status == STATUS_COMPLETED
        assert result.final_text == "总算算完了"
        assert result.steps == 2
        assert len(scripted.snapshots) == 2

    @pytest.mark.asyncio
    async def test_reserved_turn_is_not_a_regular_turn(self, tmp_path):
        """run ceiling 的**最后一轮**属于 closeout，不再是普通轮。

        剧本里第 3 轮本来是"最终回答"，但该轮已被预留的 closeout 占用 ⇒ 它只会被
        当作 closeout 请求的答复（不是合法 continuation JSON），于是落 `run/paused`
        而不是 `completed`。这与 T4 之前的 `max_steps` 语义**相反**（那时最后一轮仍
        可以收敛）：预留容量属于 ceiling 之内（`02 §5.2`），不能既算 closeout 又当
        普通轮用。稳定态（生产 fuse=500）里这 1 轮可忽略；小 ceiling 下它正是"暂停前
        还剩一次收口机会"的兑现。

        预留**只对 run ceiling 生效**：同一剧本换成 fuse 判定时会跑满 3 轮（见上一条
        用例）——两个作用域各有各的容量算法，这是刻意的不对称，不要"顺手对齐"。
        """
        rounds = _loop_rounds(2) + [AIMessage(content="总算算完了")]
        scripted = ScriptedModel(rounds)
        runtime = _runtime(
            scripted, max_agent_turns=500,
            run_budget=LaunchRunBudget(limits=RunLimits(max_agent_turns_total=3)),
        )
        session = make_session(tmp_path)

        result = await runtime.run(session, "第三轮才收敛")

        assert result.status == STATUS_PAUSED
        assert result.steps == 2
        assert len(scripted.snapshots) == 3
        # 第 3 次请求是 closeout 请求（末尾是 closeout 指令），不是"第 3 个普通轮"；
        # 那句"总算算完了"因此没有成为最终回答（final_text=""），run 停在暂停态。
        closeout_request = scripted.snapshots[2].messages[-1]
        assert closeout_request.type == "human"
        assert TRIGGER_RUN_TURNS in closeout_request.content
        paused = next(e for e in session.events if e.type == RUN_PAUSED)
        assert paused.data["trigger_dimension"] == TRIGGER_RUN_TURNS
        assert paused.data["consumed"] == {
            "agent_turns": 2, "model_requests": 3, "total_tokens": None, "cost_usd": None,
        }
        assert paused.data["limits"]["run"]["max_agent_turns_total"] == 3
        # run/started 就把 ceiling 落盘了（重启后 limits 能重建成同一个值，`03 §3.4`）
        started = next(e for e in session.events if e.type == "run/started")
        assert started.data["budget"]["run"]["max_agent_turns_total"] == 3


# ---------- 场景⑤：失败边界（故障注入，剧本可控可重现） ----------


class TestAgentLoopUnknownTool:
    """未知工具名：模型提议了 tools 里不存在的工具，必须回填错误而不是崩溃。"""

    @pytest.mark.asyncio
    async def test_unknown_tool_backfills_error_not_crash(self, tmp_path):
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
        runtime = _runtime(scripted)  # tools 里只有 add

        result = await runtime.run(make_session(tmp_path), "计算 3 乘 4")

        # 不崩 + 正常完成（模型看到错误后自我纠错给了最终回答）
        assert result.status == STATUS_COMPLETED
        assert result.steps == 2
        # 第二轮喂给模型的消息链里，ToolMessage 的 content 是错误信息，
        # 且 tool_call_id 依然配对原 id（错误回执也要能配对请求）。
        second_round = scripted.snapshots[1].messages
        assert [type(m).__name__ for m in second_round] == [
            "HumanMessage", "AIMessage", "ToolMessage",
        ]
        tool_msg = second_round[2]
        assert tool_msg.tool_call_id == "call_unknown_001"
        # content 是 ToolResult JSON：multiply 未注册 -> TOOL_NOT_FOUND。
        assert "multiply" in tool_msg.content
        assert "TOOL_NOT_FOUND" in tool_msg.content


class TestAgentLoopToolException:
    """工具内部异常：工具存在但执行时炸了，同样回填错误而不是崩溃。"""

    @pytest.mark.asyncio
    async def test_tool_exception_backfills_error_not_crash(self, tmp_path):
        """模型调 boom -> boom 抛 ValueError -> Executor 映射 TOOL_EXECUTION_ERROR 回填 -> 模型给最终回答。"""

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
        runtime = _runtime(scripted)  # registry 已注册 add + boom

        result = await runtime.run(make_session(tmp_path), "触发爆炸")

        assert result.status == STATUS_COMPLETED
        assert result.steps == 2
        tool_msg = scripted.snapshots[1].messages[2]
        assert tool_msg.tool_call_id == "call_boom_001"
        # content 是 ToolResult JSON，error_code 是结构化码：
        # boom 抛 ValueError -> Executor 分类表未命中 -> TOOL_EXECUTION_ERROR。
        assert "TOOL_EXECUTION_ERROR" in tool_msg.content
        # 异常细节仍在 JSON 的 message 字段里（如 "ValueError: 故意炸..."）——模型能读到。
        assert "ValueError" in tool_msg.content
        assert "故意炸" in tool_msg.content


class TestAgentLoopEmptyContentWithToolCalls:
    """空 content + tool_calls：content 为空但模型在要工具，绝不能误停。"""

    @pytest.mark.asyncio
    async def test_empty_content_does_not_stop_loop(self, tmp_path):
        """第一轮 content='' 且带 tool_call -> 必须继续执行工具 -> 第二轮完成。

        锁死停止信号：content 空不空从来不是停止信号，tool_calls 空不空才是。
        这条测试防止未来有人把停止条件改成 content。
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
        runtime = _runtime(scripted)

        result = await runtime.run(make_session(tmp_path), "计算 2 + 3")

        # 关键断言：模型被调用了 2 次（没有在第 1 轮误停）
        assert result.status == STATUS_COMPLETED
        assert result.steps == 2
        assert result.final_text == "2 + 3 = 5"