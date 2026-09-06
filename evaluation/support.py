"""评测域工具集（deterministic P0 用；与 tests/ 无依赖——runner 是项目代码）。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from agent_harness.tooling import Tool, ToolResult
from agent_harness.tooling.result import ErrorCode


class AddArgs(BaseModel):
    first_number: Annotated[float, Field(...)]
    second_number: Annotated[float, Field(...)]


class AddTool(Tool):
    """求和工具：tool_selection 场景的确定性目标工具。"""

    @property
    def name(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "计算两个数之和。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return AddArgs

    async def execute(self, args: AddArgs) -> ToolResult:
        return ToolResult.success(
            message="ok", data={"sum": args.first_number + args.second_number},
        )


class FlakyAddTool(AddTool):
    """第一次调用返回可重试失败、第二次成功：recovery 场景的确定性故障注入。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: AddArgs) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            return ToolResult.failure(
                message="transient failure (deterministic)",
                error_code=ErrorCode.TIMEOUT,
                retryable=True,
            )
        return ToolResult.success(
            message="ok", data={"sum": args.first_number + args.second_number},
        )
