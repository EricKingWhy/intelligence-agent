"""WriteTool：覆盖写入 workspace 内文件的 Coding Tool。

MUTATING 副作用：改变外部状态，批次调度时整批串行执行。
是覆盖写而非追加——调用方应理解为"把文件内容整体替换为 content"。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_harness.sandbox import Sandbox
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.result import ErrorCode


class _WriteArgs(BaseModel):
    path: str = Field(..., description="workspace 内的文件相对路径，如 'src/main.py'")
    content: str = Field(..., description="要写入文件的完整文本内容（覆盖已有内容）")


class WriteTool(Tool):
    """write 工具：覆盖写入 workspace 内文件。父目录不存在时自动创建。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "覆盖写入 workspace 内的文件。"
            "参数：path 为文件路径，content 为要写入的完整文本（会完全替换原有内容）。"
            "适合创建新文件或整体重写已有文件。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _WriteArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    async def execute(self, args: _WriteArgs) -> ToolResult:
        """调 sandbox.write_text；路径越界映射成 PERMISSION_DENIED。"""
        try:
            self._sandbox.write_text(args.path, args.content)
        except PermissionError as e:
            return ToolResult.failure(
                message=str(e),
                error_code=ErrorCode.PERMISSION_DENIED,
            )
        return ToolResult.success(
            message=f"已写入 '{args.path}'（{len(args.content)} 字符）。",
            data={"path": args.path, "bytes_written": len(args.content.encode("utf-8"))},
        )
