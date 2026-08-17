"""临时调试脚本：把 Agent Loop 每一轮喂给模型的消息链打印出来，肉眼可观察。

这不属于 Day 3 的产品代码或测试，只是一个可观测性辅助工具——
让你亲眼看见 Runtime 内部消息怎么流动、ToolMessage 怎么配对、历史怎么累积。
Day 3 Task 3 会用更系统的方式（结构化日志）替代这种 print 调试。

用法（在项目根目录）：
    $env:PYTHONUTF8="1"; uv run python debug_loop.py
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from tests.scripted_model import ScriptedModel


def add(first_number: float, second_number: float) -> float:
    return first_number + second_number


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
    runtime = AgentRuntime(model=scripted, tools={"add": add})

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
    runtime = AgentRuntime(model=scripted, tools={"add": add}, max_steps=3)

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