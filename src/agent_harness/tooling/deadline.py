"""ToolExecutor-owned execution deadline propagated through the Tool boundary."""

import contextvars

# Absolute ``time.perf_counter()`` value for the current Executor attempt.
# None means the Tool is being invoked outside ToolExecutor.
tool_execution_deadline_var: contextvars.ContextVar[float | None] = (
    contextvars.ContextVar("tool_execution_deadline", default=None)
)
