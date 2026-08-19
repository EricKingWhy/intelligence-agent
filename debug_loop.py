"""临时调试脚本：把 Agent Loop 每一轮喂给模型的消息链打印出来，肉眼可观察。

这不属于 Day 4 的产品代码或测试，只是一个可观测性辅助工具——
让你亲眼看见 Runtime 内部消息怎么流动、ToolMessage 怎么配对、历史怎么累积。

Day 4 Task 5 迁移：add 从裸函数升级为 Tool Contract（AddTool），
Runtime 不再持有 tools dict，而是绑 registry + ToolExecutor（工具怎么跑全下沉）。
用法（项目根目录）：
    $env:PYTHONUTF8="1"; uv run python debug_loop.py
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.scripted_model import ScriptedModel


class AddArgs(BaseModel):
    first_number: Annotated[float, Field(..., description="第一个加数")]
    second_number: Annotated[float, Field(..., description="第二个加数")]


class AddTool(Tool):
    @property
    def name(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "计算两个数的和。参数：first_number、second_number 为加数。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return AddArgs

    async def execute(self, args: AddArgs) -> ToolResult:
        return ToolResult.success(
            message=f"{args.first_number} + {args.second_number} = {args.first_number + args.second_number}",
            data={"sum": args.first_number + args.second_number},
        )


def _runtime(model: ScriptedModel, max_steps: int = 20) -> AgentRuntime:
    reg = ToolRegistry()
    reg.register(AddTool())
    return AgentRuntime(model=model, registry=reg, executor=ToolExecutor(reg), max_steps=max_steps)


async def show_continuous_two_rounds() -> None:
    """场景③：连续两轮工具的消息链全程可见。"""
    A, B = "call_two_round_A", "call_two_round_B"
    r1 = AIMessage(
        content="",
        tool_calls=[{"name": "add", "args": {"first_number": 1, "second_number": 2}, "id": A, "type": "tool_call"}],
    )
    r2 = AIMessage(
        content="",
        tool_calls=[{"name": "add", "args": {"first_number": 3, "second_number": 4}, "id": B, "type": "tool_call"}],
    )
    r3 = AIMessage(content="两笔的和分别是 3 和 7")
    scripted = ScriptedModel([r1, r2, r3])
    runtime = _runtime(scripted)

    result = await runtime.run("连续算两笔")

    for i, snap in enumerate(scripted.snapshots, 1):
        print(f"=== 第 {i} 轮 ainvoke：Runtime 喂给模型 {len(snap.messages)} 条消息 ===")
        for j, m in enumerate(snap.messages):
            t = type(m).__name__
            if t == "HumanMessage":
                print(f"  [{j}] HumanMessage: {m.content!r}")
            elif t == "AIMessage":
                tc = m.tool_calls[0] if m.tool_calls else None
                print(f"  [{j}] AIMessage: content={m.content!r} tool_calls={tc}")
            elif t == "ToolMessage":
                print(f"  [{j}] ToolMessage: {m.content!r} (tool_call_id={m.tool_call_id})")
        print()
    print(f">>> 最终结果：status={result.status}, steps={result.steps}, final_text={result.final_text!r}")


async def show_max_steps() -> None:
    """场景④：max_steps 不收敛兜底的消息链全程可见。"""
    loop_round = lambda i: AIMessage(
        content="",
        tool_calls=[{"name": "add", "args": {"first_number": i, "second_number": i}, "id": "call_loop", "type": "tool_call"}],
    )
    scripted = ScriptedModel([loop_round(0), loop_round(1), loop_round(2)])
    runtime = _runtime(scripted, max_steps=3)

    result = await runtime.run("永远算不完")

    for i, snap in enumerate(scripted.snapshots, 1):
        print(f"=== 第 {i} 轮 ainvoke：喂给模型 {len(snap.messages)} 条消息（模型仍要工具，继续烧轮数）===")
    print(f">>> 最终结果：status={result.status}, steps={result.steps}, final_text={result.final_text!r}")
    print(">>> 注意：模型被调用恰好 3 次就熔断，final_text 为空（绝不伪造最终回答）")


if __name__ == "__main__":
    print("========== 场景③：连续两轮工具 ==========\n")
    asyncio.run(show_continuous_two_rounds())
    print("\n========== 场景④：max_steps 兜底 ==========\n")
    asyncio.run(show_max_steps())