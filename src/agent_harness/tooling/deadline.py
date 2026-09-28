"""Executor 拥有的执行预算：跨 Tool 边界向 Sandbox 传递同一个绝对 deadline。

与 tool_output_sink_var（tooling/output_stream.py）同款通道：走 contextvar 而
不改 `Tool.execute` 签名，Tool 与 Sandbox 的公共形状保持不变。

操作约束：只有 ToolExecutor 写这个变量（每次 attempt 一份），Tool 只读并原样
转发给阻塞执行后端，后端只消费、不重新起算。机制与取舍见 ADR-0039。
"""

import contextvars

# 当前 Executor attempt 的绝对 time.perf_counter() 边界。None 有两种来路（不经 Executor
# 的直接调用 / 工具没转发它，ADR-0039 D2），后端退回自己的默认预算；收到 None **不能**
# 反推"没经过 Executor"。
tool_execution_deadline_var: contextvars.ContextVar[float | None] = (
    contextvars.ContextVar("tool_execution_deadline", default=None)
)
