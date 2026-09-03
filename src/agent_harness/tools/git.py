"""GitStatusTool + GitDiffTool：只读 git 查询 Coding Tools。

V1 仅 status / diff 等只读能力（05_SANDBOX_CODING_TOOLS.md）：
MUST NOT 自动 commit / push / reset --hard。

命令硬编码 + shlex.quote 转义 → 模型无法注入子命令。
遵循 ADR-0002：exit_code 非零（如不是 git 仓库）仍 ok=True。

READ_ONLY 副作用：不改外部状态，可与同批其他 READ_ONLY 工具并发。
"""

from __future__ import annotations

import shlex

from pydantic import BaseModel, Field

from agent_harness.sandbox import Sandbox
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission


class _GitStatusArgs(BaseModel):
    pathspec: str = Field(default="", description="可选路径过滤，如 'src/' 或 'README.md'")


class GitStatusTool(Tool):
    """git_status 工具：只读查询 workspace git 状态（porcelain 格式）。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "git_status"

    @property
    def description(self) -> str:
        return (
            "查询 workspace 的 git 状态（只读）。返回 porcelain 格式的改动文件列表。"
            "参数：pathspec 可选路径过滤。不会执行 commit / push / reset 等写操作。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _GitStatusArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.READ_ONLY

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, args: _GitStatusArgs) -> ToolResult:
        """exec 硬编码 git status；ADR-0002：exit_code 非零仍 ok=True。"""
        command = "git status --porcelain=v1"
        if args.pathspec:
            command += " " + shlex.quote(args.pathspec)
        result = self._sandbox.exec(command)
        return ToolResult.success(
            message=f"git status 已执行，exit_code={result.exit_code}。",
            data={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )


class _GitDiffArgs(BaseModel):
    staged: bool = Field(default=False, description="True 时查看暂存区差异（git diff --staged）")
    path: str = Field(default="", description="可选路径过滤")


class GitDiffTool(Tool):
    """git_diff 工具：只读查询 workspace git 差异内容。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "git_diff"

    @property
    def description(self) -> str:
        return (
            "查询 workspace 的 git 差异（只读）。"
            "参数：staged=True 看暂存区差异，path 可选路径过滤。"
            "不会执行 commit / push / reset 等写操作。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _GitDiffArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.READ_ONLY

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, args: _GitDiffArgs) -> ToolResult:
        """exec 硬编码 git diff；ADR-0002：exit_code 非零仍 ok=True。"""
        command = "git diff"
        if args.staged:
            command += " --staged"
        if args.path:
            command += " " + shlex.quote(args.path)
        result = self._sandbox.exec(command)
        return ToolResult.success(
            message=f"git diff 已执行，exit_code={result.exit_code}。",
            data={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
