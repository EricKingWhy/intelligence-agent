"""GrepTool：在 workspace 文件内容里做正则搜索的 Coding Tool。

READ_ONLY 副作用。调 Sandbox.list_files 枚举，对每个文件 read_text 逐行 re.search。
- 坏正则 → INVALID_ARGUMENT
- 二进制/编码失败文件 → 跳过（不算错误）
- max_results 截断防上下文爆炸
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from agent_harness.sandbox import Sandbox
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.result import ErrorCode


class _GrepArgs(BaseModel):
    pattern: str = Field(..., description="正则表达式")
    path: str = Field(default=".", description="搜索范围子目录，相对 workspace")
    include: str = Field(default="*", description="文件名 glob 过滤（如 '*.py'）")
    max_results: int = Field(default=100, ge=1, description="匹配上限，防上下文爆炸")


class GrepTool(Tool):
    """grep 工具：在 workspace 文件内容里做正则搜索，返回文件路径+行号+匹配行。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "在 workspace 文件内容里做正则搜索。"
            "参数：pattern 正则表达式，include 文件名 glob 过滤（如 '*.py'），"
            "path 搜索范围子目录，max_results 匹配上限（默认 100）。"
            "返回匹配的文件路径、行号、行文本。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _GrepArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.READ_ONLY

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, args: _GrepArgs) -> ToolResult:
        """re.compile → list_files → 逐文件逐行 search → 收集匹配。"""
        try:
            regex = re.compile(args.pattern)
        except re.error as e:
            return ToolResult.failure(
                message=f"无效的正则表达式 '{args.pattern}': {e}",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )

        try:
            candidates = self._sandbox.list_files(
                f"{args.path.rstrip('/')}/" if args.path != "." else ""
            )
        except PermissionError as e:
            return ToolResult.failure(
                message=str(e),
                error_code=ErrorCode.PERMISSION_DENIED,
            )

        # 如果 path 不是 "."，过滤只保留该子树下的文件
        if args.path and args.path != ".":
            prefix = args.path.rstrip("/") + "/"
            candidates = [f for f in candidates if f.startswith(prefix) or f == args.path]

        # include 文件名过滤
        import fnmatch

        if args.include and args.include != "*":
            from pathlib import PurePosixPath

            candidates = [
                f for f in candidates if fnmatch.fnmatch(PurePosixPath(f).name, args.include)
            ]

        matches: list[dict] = []
        truncated = False
        for filepath in candidates:
            if len(matches) >= args.max_results:
                truncated = True
                break
            try:
                content = self._sandbox.read_text(filepath)
            except (FileNotFoundError, PermissionError, UnicodeDecodeError):
                # 二进制文件或编码失败 → 跳过
                continue

            for line_number, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    matches.append({
                        "path": filepath,
                        "line_number": line_number,
                        "line": line,
                    })
                    if len(matches) >= args.max_results:
                        truncated = True
                        break

        return ToolResult.success(
            message=f"找到 {len(matches)} 处匹配"
                    + ("（已截断）" if truncated else "")
                    + "。",
            data={
                "matches": matches,
                "count": len(matches),
                "truncated": truncated,
            },
        )
