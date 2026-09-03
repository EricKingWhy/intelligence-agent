"""Agent 模块：透明最小 Agent Loop。

对外只暴露两个名字：AgentRuntime（驱动循环）、AgentRunResult（运行结果）。
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