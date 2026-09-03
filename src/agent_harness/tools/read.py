"""ReadTool：读取 workspace 内文本文件的 Coding Tool。

READ_ONLY 副作用：不改变外部状态，可与同批其他 READ_ONLY 工具并发执行。
路径边界由 Sandbox 强制（ADR-0001），越界抛 PermissionError → 本工具映射成
ToolResult.failure(error_code=PERMISSION_DENIED)。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_harness.sandbox import Sandbox
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.result import ErrorCode


class _ReadArgs(BaseModel):
    path: str = Field(..., description="workspace 内的文件相对路径，如 'src/main.py'")


class ReadTool(Tool):
    """read 工具：读取 workspace 内文本文件内容。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "读取 workspace 内的文本文件内容。"
            "参数：path 为 workspace 内的文件路径（相对路径或绝对路径，必须在 workspace 范围内）。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _ReadArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.READ_ONLY

    async def execute(self, args: _ReadArgs) -> ToolResult:
        """调 sandbox.read_text；文件不存在或路径越界映射成失败 ToolResult。"""
        try:
            content = self._sandbox.read_text(args.path)
        except PermissionError as e:
            return ToolResult.failure(
                message=str(e),
                error_code=ErrorCode.PERMISSION_DENIED,
            )
        except FileNotFoundError:
            return ToolResult.failure(
                message=f"文件 '{args.path}' 不存在。",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        return ToolResult.success(
            message=f"已读取 '{args.path}'（{len(content)} 字符）。",
            data={"path": args.path, "content": content},
        )
