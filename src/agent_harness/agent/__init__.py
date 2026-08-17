"""Day 3 Agent 模块：透明最小 Agent Loop。

对外只暴露两个名字：AgentRuntime（驱动循环）、AgentRunResult（运行结果）。
其他实现细节（types 常量、内部辅助方法）不对外导出，避免被外部代码偶然耦合。
"""

from agent_harness.agent.runtime import AgentRuntime
from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_MAX_STEPS_EXCEEDED,
    AgentRunResult,
)

__all__ = [
    "STATUS_COMPLETED",
    "STATUS_MAX_STEPS_EXCEEDED",
    "AgentRunResult",
    "AgentRuntime",
]