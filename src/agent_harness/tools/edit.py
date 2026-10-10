"""EditTool：对 workspace 内文件做精确字符串替换的 Coding Tool。

exact old_string → new_string 语义（05_SANDBOX_CODING_TOOLS.md）：
- 0 match  → NOT_FOUND（映射 TOOL_EXECUTION_ERROR）
- 1 match  → success
- >1 match → AMBIGUOUS（映射 TOOL_EXECUTION_ERROR）
- replace_all=True 时 ≥1 match 全替换。

MUTATING 副作用：改文件，批次调度整批串行。
复用 Sandbox.read_text / write_text，不改 Sandbox 契约。
"""

from __future__ import annotations

import posixpath

from pydantic import BaseModel, Field

from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.sandbox import Sandbox
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode
from agent_harness.tools._diff_data import diff_data
from agent_harness.tools._line_endings import (
    not_found_hint,
    replace_with_line_ending_tolerance,
)


class _EditArgs(BaseModel):
    path: str = Field(..., description="workspace 内的文件相对路径")
    old_string: str = Field(..., min_length=1, description="要被替换的确切字符串（不能为空）")
    new_string: str = Field(..., description="替换后的新字符串")
    replace_all: bool = Field(default=False, description="True 时替换所有匹配；False 时仅允许唯一匹配")


class EditTool(Tool):
    """edit 工具：精确字符串替换，0/1/>1 三态匹配语义。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "对 workspace 内文件做精确字符串替换（exact old_string → new_string）。"
            "参数：path 文件路径，old_string 要替换的确切字符串，new_string 新字符串，"
            "replace_all=True 时替换所有匹配（默认 False，要求唯一匹配）。"
            "0 匹配返回未找到错误，多于 1 处匹配返回多个匹配错误。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EditArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    def resource_keys(self, args: _EditArgs) -> list[str]:
        """#525 一期（IMP-14）：同文件编辑跨批互斥。

        每个 edit 调用声明它将触碰的 workspace 文件 key（规范路径去重 ./ 与 prefix/）。
        执行域据此对相同 key 的并发调用串行化。
        """
        return [f"workspace-file:{posixpath.normpath(args.path)}"]

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action=(
                "读取目标文件核对编辑是否已应用："
                "含 new_string 说明已生效（确认成功），仍只有 old_string 说明未执行。"
            ),
        )

    async def execute(self, args: _EditArgs) -> ToolResult:
        """read_text → 行尾容忍匹配 → 替换或失败 → write_text。"""
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

        count, new_content = replace_with_line_ending_tolerance(
            content, args.old_string, args.new_string, replace_all=args.replace_all
        )
        if count == 0:
            return ToolResult.failure(
                message=(
                    f"在 '{args.path}' 中未找到匹配的字符串。"
                    f"{not_found_hint(content, args.old_string)}"
                ),
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        if count > 1 and not args.replace_all:
            return ToolResult.failure(
                message=(
                    f"在 '{args.path}' 中找到 {count} 处匹配，"
                    f"需缩小 old_string 范围或设置 replace_all=true。"
                ),
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )

        replacements = count if args.replace_all else 1

        try:
            self._sandbox.write_text(args.path, new_content)
        except PermissionError as e:
            return ToolResult.failure(
                message=str(e),
                error_code=ErrorCode.PERMISSION_DENIED,
            )

        return ToolResult.success(
            # `#519` BUG-12：diff 片段携带文件内容（data.diff），逐条不可信标注。
            message=f"{DEFAULT_REGISTRY.assemble('frame:untrusted_tool_output').fragment_text}"
                    f"已在 '{args.path}' 中替换 {replacements} 处。",
            data={
                "path": args.path,
                "replacements": replacements,
                **diff_data(content, new_content),
            },
        )
