"""Tool Runtime：统一 Tool Contract、ToolResult、Registry 与 Executor。

对外暴露组成 Tool Runtime 的最小数据模型 + 执行域层。
其他实现细节不对外导出，避免被外部代码偶然耦合。

Day4 Task 1 冻结抽象层（Contract / ToolResult / Registry）；
Day4 Task 2 加入单次执行链（Executor，Validation-first）。
Timeout / Retry / 批次调度是后续 Task。
"""

from agent_harness.tooling.contract import Tool, ToolSideEffect
from agent_harness.tooling.executor import ToolExecution, ToolExecutor
from agent_harness.tooling.registry import ToolRegistry
from agent_harness.tooling.result import ErrorCode, ToolResult

__all__ = [
    "ErrorCode",
    "Tool",
    "ToolExecution",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "ToolSideEffect",
]
