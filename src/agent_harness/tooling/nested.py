"""Invocation-scoped bridge ports; no runtime globals or executor ownership."""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from agent_harness.tooling.result import ToolResult

NestedToolCall = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]
nested_tool_call_var: ContextVar[NestedToolCall | None] = ContextVar(
    'nested_tool_call', default=None,
)
parent_exec_call_id_var: ContextVar[str | None] = ContextVar(
    'parent_exec_call_id', default=None,
)


def parent_exec_metadata() -> dict[str, str]:
    parent = parent_exec_call_id_var.get()
    return {'parent_exec_call_id': parent} if parent is not None else {}


class NestedExecutionFailure(BaseException):
    """Carry child infrastructure failure across the Tool retry boundary."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(str(cause))
