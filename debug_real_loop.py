"""临时调试脚本：用【真实模型】跑一遍你的 AgentRuntime（不是剧本）。

复用 Day 1/2 的 ModelProvider（读 .env，当前是腾讯云 GLM-5.2），
在脚本里手动 bind_tools（复用 Day 2 的 add schema），再喂给 AgentRuntime。

为什么 bind_tools 放脚本里、而不改进 AgentRuntime？
- AgentRuntime 的职责是"只驱动循环"，不该知道"Python 函数怎么变成工具 schema"；
- "工具序列化"是 Day 4 ToolRegistry 要做的事，今天提前碰它就违背 Scope Lock；
- 所以今天在调用方（本脚本）手动 bind，Loop 保持干净。

用法（项目根目录）：
    $env:PYTHONUTF8="1"; uv run python debug_real_loop.py

注意：会消耗真实 API Token（很少，几次 add 调用）。
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.config import Settings
from agent_harness.logging import setup_logging
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model


class AddArgs(BaseModel):
    """add 工具的参数 schema（和 Day 2 同名同结构）。"""
    first_number: float = Field(..., description="第一个加数")
    second_number: float = Field(..., description="第二个加数")


def add(first_number: float, second_number: float) -> float:
    """真实的 add 工具——你的 Runtime 会真正执行它，不是剧本。"""
    return first_number + second_number


def risky_add(first_number: float, second_number: float) -> float:
    """一个会故意炸的工具：结果 > 1000 就抛异常，演示【失败回填】。

    这是 Task 3 的真实演示：真实模型调到它 -> 工具炸 ->
    Runtime 捕获 -> 错误以 ToolMessage 回填给模型 -> 模型看到错误自我纠错。
    """
    result = first_number + second_number
    if result > 1000:
        msg = f"结果 {result} 超过安全上限 1000，拒绝计算"
        raise ValueError(msg)
    return result


async def run_real_loop(user_input: str) -> None:
    """接真实模型跑一遍 Agent Loop，打印每一轮的真实消息流动。"""
    settings = Settings()
    config = ModelConfig.from_settings(settings)
    model = create_chat_model(config)

    # bind_tools：告诉模型"你有 add 和 risky_add 可用"。
    # risky_add 会炸，看真实模型会不会踩雷、踩雷后会不会自我纠错。
    bound_model = model.bind_tools([
        {
            "name": "add",
            "description": "计算两个数的和，结果无上限，用于普通加法",
            "parameters": AddArgs.model_json_schema(),
        },
        {
            "name": "risky_add",
            "description": "计算两个数的和，但结果若超过 1000 会报错",
            "parameters": AddArgs.model_json_schema(),
        },
    ])

    runtime = AgentRuntime(model=bound_model, tools={"add": add, "risky_add": risky_add})
    result = await runtime.run(user_input)

    print(f"用户输入：{user_input!r}")
    print(f">>> 最终结果：status={result.status}, steps={result.steps}")
    print(f">>> 最终回答：{result.final_text!r}")


if __name__ == "__main__":
    # 打开日志：setup_logging 后，AgentRuntime._log 不再是 no-op，
    # 会把 run/step/llm/tool/决策主线写进 logs/agent.jsonl。
    log_path = setup_logging()
    print(f"[日志] 结构化日志写入：{log_path}\n")

    # 第 1 题：普通加法，走 happy path（应该 add 一次完成）。
    # 第 2 题：大数加法，risky_add 会炸 -> 看模型失败回填后怎么纠错。
    questions = [
        "计算 123 + 456",               # happy path
        "用 risky_add 计算 800 + 500",  # 1300 > 1000，工具会炸
    ]
    for q in questions:
        print("=" * 60)
        asyncio.run(run_real_loop(q))
        print()
