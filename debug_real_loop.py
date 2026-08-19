"""临时调试脚本：用【真实模型】跑一遍你的 AgentRuntime（不是剧本）。

Day 4 Task 5 迁移：工具从裸函数升级为 Tool Contract，Runtime 绑 registry + ToolExecutor。
模型菜单不再手搓 dict，而是从 registry.export_model_definitions() 导出——
这就是 Task 1 "单一事实源"的兑现：模型看到的 name/description/parameters
与 Runtime 执行的 Tool 来自同一份 Contract。

用法（项目根目录）：
    $env:PYTHONUTF8="1"; uv run python debug_real_loop.py

注意：会消耗真实 API Token（很少，几次 add 调用）。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.config import Settings
from agent_harness.logging import setup_logging
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult


class AddArgs(BaseModel):
    """add 工具的参数 schema（和 Day 2 同名同结构）。"""
    first_number: Annotated[float, Field(..., description="第一个加数")]
    second_number: Annotated[float, Field(..., description="第二个加数")]


class AddTool(Tool):
    """add 工具：走统一 Contract，你的 Runtime 会真正执行它，不是剧本。"""

    @property
    def name(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "计算两个数的和，结果无上限，用于普通加法。参数：first_number、second_number。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return AddArgs

    async def execute(self, args: AddArgs) -> ToolResult:
        return ToolResult.success(
            message=f"{args.first_number} + {args.second_number} = {args.first_number + args.second_number}",
            data={"sum": args.first_number + args.second_number},
        )


class RiskyAddTool(Tool):
    """一个会故意炸的工具：结果 > 1000 就抛 ValueError，演示【失败回填】。

    execute 抛 ValueError -> Executor（Task 3）阶段3 兜底映射成 TOOL_EXECUTION_ERROR，
    以 ToolResult JSON 回填给模型。真实模型看到 error_code 和 message 里的
    "超过安全上限 1000"，就能理解是数字问题，从而自我纠错（改小数字或换工具）。
    """

    @property
    def name(self) -> str:
        return "risky_add"

    @property
    def description(self) -> str:
        return "计算两个数的和，但结果若超过 1000 会报错。参数：first_number、second_number。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return AddArgs

    async def execute(self, args: AddArgs) -> ToolResult:
        result = args.first_number + args.second_number
        if result > 1000:
            raise ValueError(f"结果 {result} 超过安全上限 1000，拒绝计算")
        return ToolResult.success(
            message=f"{args.first_number} + {args.second_number} = {result}",
            data={"sum": result},
        )


def build_runtime() -> tuple[AgentRuntime, ToolRegistry]:
    """构造绑了 registry + executor 的 Runtime，并返回 registry（导出模型菜单用）。

    注意：模型要 bind 的工具描述来自 registry.export_model_definitions()，
    和 Runtime 执行的 Tool 是同一份 Contract（唯一事实源）。
    """
    reg = ToolRegistry()
    reg.register(AddTool())
    reg.register(RiskyAddTool())
    return AgentRuntime(model=None, registry=reg, executor=ToolExecutor(reg)), reg


async def run_real_loop(registry: ToolRegistry, user_input: str) -> None:
    """接真实模型跑一遍 Agent Loop，打印每一轮的真实消息流动。"""
    settings = Settings()
    config = ModelConfig.from_settings(settings)
    model = create_chat_model(config)

    # 模型菜单 = registry 导出的 Contract（不再是手搓 dict）。
    bound_model = model.bind_tools(registry.export_model_definitions())

    # 复用同一个 registry 的 executor；runtime 需要绑定 model，重构造一次。
    runtime = AgentRuntime(model=bound_model, registry=registry, executor=ToolExecutor(registry))
    result = await runtime.run(user_input)

    print(f"用户输入：{user_input!r}")
    print(f">>> 最终结果：status={result.status}, steps={result.steps}")
    print(f">>> 最终回答：{result.final_text!r}")


if __name__ == "__main__":
    # 打开日志：setup_logging 后，AgentRuntime._log 不再是 no-op，
    # 会把 run/step/llm/tool/决策主线写进 logs/agent.jsonl。
    log_path = setup_logging()
    print(f"[日志] 结构化日志写入：{log_path}\n")

    # registry 只建一次（工具契约同一个）；每次 run 复用同一批 tool 定义。
    _, registry = build_runtime()

    # 第 1 题：普通加法，走 happy path（应该 add 一次完成）。
    # 第 2 题：大数加法，risky_add 会炸 -> 看真实模型看到 ToolResult JSON 后怎么纠错。
    questions = [
        "计算 123 + 456",               # happy path：add
        "用 risky_add 计算 800 + 500",  # 1300 > 1000，工具会炸 -> 自纠错
    ]
    for q in questions:
        print("=" * 60)
        asyncio.run(run_real_loop(registry, q))
        print()

    print("done")