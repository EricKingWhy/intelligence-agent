"""Session：append-only typed SessionEvent + JSONL 存储 + derive 投影。

Phase 1 完整导出：SessionEvent DTO、JsonlSessionStore、derive_messages、Session 聚合根。
"""

from agent_harness.session.derive import (
    DANGLING_TOOL_CONTENT,
    derive_messages,
    detect_dangling,
)
from agent_harness.session.event import (
    EVENT_TYPES,
    MODEL_COMPLETED,
    MODEL_FAILED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    SESSION_RESUMED,
    SESSION_STARTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore

__all__ = [
    "DANGLING_TOOL_CONTENT",
    "EVENT_TYPES",
    "MODEL_COMPLETED",
    "MODEL_FAILED",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "RUN_STARTED",
    "SESSION_RESUMED",
    "SESSION_STARTED",
    "TOOL_CALL",
    "TOOL_RESULT",
    "USER_MESSAGE",
    "JsonlSessionStore",
    "Session",
    "SessionEvent",
    "derive_messages",
    "detect_dangling",
]
