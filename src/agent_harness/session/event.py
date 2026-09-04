"""SessionEvent：Agent 交互历史的 append-only 类型化事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

# ── Event vocabulary V1（Phase 1 子集） ──
# ── + Phase 9 流式信号（MODEL_STARTED / MODEL_DELTA） ──
# ── + Phase 4 恢复信号（OPERATION_RECONCILE_REQUIRED） ──

SESSION_STARTED = "session/started"
SESSION_RESUMED = "session/resumed"
RUN_STARTED = "run/started"
RUN_COMPLETED = "run/completed"
RUN_FAILED = "run/failed"
USER_MESSAGE = "user/message"
MODEL_STARTED = "model/started"
MODEL_DELTA = "model/delta"
MODEL_COMPLETED = "model/completed"
MODEL_FAILED = "model/failed"
TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"
OPERATION_RECONCILE_REQUIRED = "operation/reconcile-required"
ARTIFACT_CREATED = "artifact/created"
CONTEXT_COMPACTED = "context/compacted"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        SESSION_STARTED,
        SESSION_RESUMED,
        RUN_STARTED,
        RUN_COMPLETED,
        RUN_FAILED,
        USER_MESSAGE,
        MODEL_STARTED,
        MODEL_DELTA,
        MODEL_COMPLETED,
        MODEL_FAILED,
        TOOL_CALL,
        TOOL_RESULT,
        OPERATION_RECONCILE_REQUIRED,
        ARTIFACT_CREATED,
        CONTEXT_COMPACTED,
    }
)


def _utc_now_iso() -> str:
    """ISO-8601 毫秒精度 UTC 时间戳。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_event_id() -> str:
    return str(uuid4())


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """一条持久化、append-only、类型化的会话事实事件。

    事件一旦写入即不可原地修改；修订用新事件表达（附带 source_event_ids 指向原事件）。
    """

    event_id: str = field(default_factory=_new_event_id)
    seq: int = 0
    time: str = field(default_factory=_utc_now_iso)
    type: str = ""
    session_id: str = ""
    run_id: str | None = None
    agent_id: str | None = None
    step_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    source_event_ids: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSONL 行字典（None 字段省略以保持行紧凑）。"""
        result: dict[str, Any] = {
            "event_id": self.event_id,
            "seq": self.seq,
            "time": self.time,
            "type": self.type,
            "session_id": self.session_id,
        }
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        if self.step_id is not None:
            result["step_id"] = self.step_id
        if self.data:
            result["data"] = self.data
        if self.source_event_ids is not None:
            result["source_event_ids"] = self.source_event_ids
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SessionEvent:
        """从 JSONL 解析出的字典重建 SessionEvent。"""
        return cls(
            event_id=raw.get("event_id", _new_event_id()),
            seq=raw.get("seq", 0),
            time=raw.get("time", _utc_now_iso()),
            type=raw.get("type", ""),
            session_id=raw.get("session_id", ""),
            run_id=raw.get("run_id"),
            agent_id=raw.get("agent_id"),
            step_id=raw.get("step_id"),
            data=raw.get("data", {}),
            source_event_ids=raw.get("source_event_ids"),
        )
