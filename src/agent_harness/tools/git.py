"""GitStatusTool + GitDiffTool：只读 git 查询 Coding Tools。

V1 仅 status / diff 等只读能力（05_SANDBOX_CODING_TOOLS.md）：
MUST NOT 自动 commit / push / reset --hard。

命令硬编码 + pathspec 白名单校验 → 模型无法注入子命令。注意：sandbox.exec
走 shell=True，win32 上是 cmd.exe——cmd 不认 shlex 单引号，仅靠 shlex.quote
挡不住 "&" 拆命令（READ_ONLY 工具会绕过审批门与串行语义）。白名单之外的
pathspec 一律 INVALID_ARGUMENT 拒绝（跨 shell 安全）。
遵循 ADR-0002：exit_code 非零（如不是 git 仓库）仍 ok=True。

READ_ONLY 副作用：不改外部状态，可与同批其他 READ_ONLY 工具并发。
"""

from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel, Field

from agent_harness.sandbox import Sandbox
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode

#: pathspec/path 白名单：文件路径的合法字符（字母数字、空格、/ . _ -）。
#: shell 元字符（& | < > ^ % " ' ` ! \\ 换行等）一律拒绝——cmd.exe 与 POSIX 的
#: 元字符集不同，白名单是唯一跨 shell 安全的拦法。\\ 也在拒绝之列：工具契约的
#: 路径是 workspace 相对 POSIX 风格（/ 分隔），且裸 \ 在双引号内语义因 shell 而异。
#: 空格在白名单内：插值时统一加双引号（见 execute），元字符已被白名单排除，
#: 双引号在 cmd.exe 与 POSIX sh 中都安全。
_SAFE_PATHSPEC = re.compile(r"^[\w /.\-]+$", re.UNICODE)


def _checked_pathspec(value: str) -> str:
    """校验 pathspec/path 是纯路径段；非法即抛 ValueError（调用方映射 INVALID_ARGUMENT）。"""
    if not _SAFE_PATHSPEC.fullmatch(value):
        raise ValueError(
            f"pathspec 含非法字符，只接受纯文件路径（字母/数字/空格/.-_/，"
            "不支持通配符与 pathspec magic）："
            f"{value!r}"
        )
    return value


def _quoted(value: str) -> str:
    """白名单内的 pathspec 统一双引号包裹：空格路径不再被 shell 拆成多个参数。"""
    return f'"{value}"' if value else ""


class _GitStatusArgs(BaseModel):
    pathspec: str = Field(default="", description="可选路径过滤，如 'src/' 或 'README.md'（纯路径，不支持通配符与 pathspec magic）")


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

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action="重新运行 git status，核对工作区状态是否与预期一致（只读查询，重跑安全）。",
        )

    async def execute(self, args: _GitStatusArgs) -> ToolResult:
        """exec 硬编码 git status；ADR-0002：exit_code 非零仍 ok=True。"""
        try:
            pathspec = _checked_pathspec(args.pathspec) if args.pathspec else ""
        except ValueError as error:
            return ToolResult.failure(
                message=str(error),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        command = "git status --porcelain=v1"
        if pathspec:
            command += f" {_quoted(pathspec)}"
        # 同步 sandbox.exec 卸载到工作线程，避免阻塞 event loop（D10）。
        result = await asyncio.to_thread(self._sandbox.exec, command)
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
    path: str = Field(default="", description="可选路径过滤（纯路径，不支持通配符）")


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

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action="重新运行 git diff，核对变更内容是否与预期一致（只读查询，重跑安全）。",
        )

    async def execute(self, args: _GitDiffArgs) -> ToolResult:
        """exec 硬编码 git diff；ADR-0002：exit_code 非零仍 ok=True。"""
        try:
            path = _checked_pathspec(args.path) if args.path else ""
        except ValueError as error:
            return ToolResult.failure(
                message=str(error),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        command = "git diff"
        if args.staged:
            command += " --staged"
        if path:
            command += f" {_quoted(path)}"
        # 同步 sandbox.exec 卸载到工作线程，避免阻塞 event loop（D10）。
        result = await asyncio.to_thread(self._sandbox.exec, command)
        return ToolResult.success(
            message=f"git diff 已执行，exit_code={result.exit_code}。",
            data={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
