"""BashTool：在 workspace 内执行 shell 命令的 Coding Tool。

MUTATING 副作用（保守默认）：即使命令本身是只读的（如 ls），也无法静态保证，
所以整批串行执行。

核心不变量（ADR-0002）：bash 工具的 ToolResult.ok 永远 True（除非 Sandbox 本身崩了）。
非零 exit_code（如 pytest 失败）不是 Tool Runtime 异常——exit_code/stdout/stderr
放进 data 供模型读取，模型据此决定下一步，而不是被 Executor 自动重试。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_harness.sandbox import Sandbox
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.result import ErrorCode


class _BashArgs(BaseModel):
    command: str = Field(..., description="要在 workspace 内执行的 shell 命令")


class BashTool(Tool):
    """bash 工具：在 workspace 内执行 shell 命令，返回 exit_code/stdout/stderr。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            "在 workspace 内执行 shell 命令（如 pytest、ls、cat）。"
            "参数：command 为要执行的 shell 命令字符串。"
            "返回 exit_code、stdout、stderr——命令返回非零 exit_code 不代表工具调用失败，"
            "应读取 stdout/stderr 判断命令执行结果。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _BashArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER

    async def execute(self, args: _BashArgs) -> ToolResult:
        """调 sandbox.exec；exit_code 无论几都返回 ok=True（ADR-0002）。"""
        try:
            result = self._sandbox.exec(args.command)
        except PermissionError as e:
            return ToolResult.failure(
                message=str(e),
                error_code=ErrorCode.PERMISSION_DENIED,
            )
        except Exception as e:  # noqa: BLE001
            # Sandbox 本身崩了（如容器挂了、子进程底层故障）——这才是真正的工具失败。
            return ToolResult.failure(
                message=f"bash 执行环境异常: {type(e).__name__}: {e}",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        # 关键映射（ADR-0002）：命令业务失败（exit_code!=0）→ ok=True，
        # exit_code/stdout/stderr 在 data 里供模型读取。
        return ToolResult.success(
            message=f"命令已执行，exit_code={result.exit_code}。",
            data={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_ms": result.duration_ms,
            },
        )
