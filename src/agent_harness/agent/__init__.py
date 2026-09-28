"""Agent 模块：透明最小 Agent Loop + 流式事件。

对外暴露：AgentRuntime（驱动循环）、AgentRunResult（运行结果）、AgentEvent（流式信封）；
完成闸门的 seam（`#316`）：CompletionPolicy（域策略注入点）、QuiescenceReport（六条谓词
的聚合结果）、DefaultCompletionPolicy（默认通用策略）。
"""

from agent_harness.agent.completion import (
    CompletionDecision,
    CompletionPolicy,
    DefaultCompletionPolicy,
    QuiescenceBlocker,
    QuiescenceReport,
    collect_quiescence_report,
)
from agent_harness.agent.runtime import AgentRuntime
from agent_harness.agent.types import (
    STATUS_COMPLETED,
    STATUS_CONTEXT_WINDOW_EXCEEDED,
    STATUS_PAUSED,
    STATUS_QUIESCENCE_BLOCKED,
    AgentEvent,
    AgentRunResult,
    to_agent_event,
)

__all__ = [
    "STATUS_COMPLETED",
    "STATUS_CONTEXT_WINDOW_EXCEEDED",
    "STATUS_PAUSED",
    "STATUS_QUIESCENCE_BLOCKED",
    "AgentEvent",
    "AgentRunResult",
    "AgentRuntime",
    "CompletionDecision",
    "CompletionPolicy",
    "DefaultCompletionPolicy",
    "QuiescenceBlocker",
    "QuiescenceReport",
    "collect_quiescence_report",
    "to_agent_event",
]
