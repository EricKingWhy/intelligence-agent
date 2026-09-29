"""derive_messages：从 SessionEvent 序列投影出模型可见 messages 列表。

纯函数，无副作用。负责：
    1. user/message → HumanMessage
    2. model/completed → AIMessage（含 tool_calls）
    3. tool/result → ToolMessage（按 tool_call_id 配对到 AIMessage）
    4. dangling tool_call 检测 → 注入合成 ToolMessage
    5. ADR-0030（#196）：`message/superseded` 区间剔除（投影级"编辑替换问句"）

本模块同时是**事件流派生**的集散地：`collect_dangling` / `detect_dangling` /
`undelivered_inputs` 都是同一种东西——只读事件、产出领域事实的纯函数，供
runtime / recovery / service 复用，避免各调用点各写一遍扫描逻辑。

配对算法：以 AIMessage 为单位。一条 model/completed 带多个 tool_calls 时，
投影成一条 AIMessage(tool_calls=[...])，后续 tool/result 按 tool_call_id
匹配成各自 ToolMessage（符合 OpenAI / Anthropic / LangChain 标准消息格式）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from bisect import bisect_left, bisect_right
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    ToolMessage,
)

from agent_harness.session.event import (
    ARTIFACT_CREATED,
    ARTIFACT_EXTERNALIZED,
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    MESSAGE_QUEUED,
    MESSAGE_SUPERSEDED,
    MODEL_COMPLETED,
    OPERATION_RECONCILE_REQUIRED,
    OPERATION_RECONCILED,
    PERMISSION_CHANGED,
    QUEUE_CANCELLED,
    QUEUE_CONSUMED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_PAUSED,
    SESSION_STARTED,
    STEER_APPLIED,
    STEER_REQUESTED,
    TASK_PROTECTED_FACT,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)

logger = logging.getLogger("agent_harness.session.derive")

#: 合成 dangling ToolMessage 的固定内容（模型可见，引导自主决策）
DANGLING_TOOL_CONTENT = "工具执行被中断，结果未知"

_USER_FACT_TYPES = frozenset(
    {
        "user_instruction",
        "user_goal",
        "constraint",
        "authorization",
        "authorization_revocation",
        "acceptance_criterion",
        "exact_identifier",
        "confirmed_decision",
        "task_progress",
    }
)
_USER_INPUT_FACT_TYPES = _USER_FACT_TYPES - {"authorization_revocation"}
_USER_SOURCE_TYPES = frozenset({USER_MESSAGE, MESSAGE_QUEUED, STEER_REQUESTED})
_RUN_BOUNDARY_TYPES = frozenset(
    {RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED, RUN_PAUSED}
)
_ARTIFACT_FACT_FIELDS = (
    "artifact_id",
    "artifact_ref",
    "source_tool",
    "tool_call_id",
    "size",
    "mime_type",
    "sha256",
)


@dataclass(frozen=True, slots=True)
class ProtectedFact:
    """A lossless task fact projected from its immutable source event."""

    fact_id: str
    type: str
    value: Any
    source_event_id: str
    source_seq: int
    status: str
    session_id: str
    evidence_event_id: str | None = None
    evidence_seq: int | None = None
    superseded_by_fact_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", deepcopy(self.value))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "fact_id": self.fact_id,
            "type": self.type,
            "value": deepcopy(self.value),
            "source_event_id": self.source_event_id,
            "source_seq": self.source_seq,
            "status": self.status,
            "session_id": self.session_id,
        }
        if self.evidence_event_id is not None:
            result["evidence_event_id"] = self.evidence_event_id
            result["evidence_seq"] = self.evidence_seq
        if self.superseded_by_fact_id is not None:
            result["superseded_by_fact_id"] = self.superseded_by_fact_id
        return result


def serialize_protected_facts(facts: list[ProtectedFact]) -> str:
    return json.dumps(
        [fact.to_dict() for fact in facts],
        ensure_ascii=False,
        sort_keys=True,
    )


def _system_fact_value(source: SessionEvent, fact_type: str) -> Any:
    if fact_type == "authorization":
        if source.type == PERMISSION_CHANGED:
            return dict(source.data)
        if source.type == SESSION_STARTED:
            return {
                key: source.data[key]
                for key in ("permission_mode", "auto_approve")
                if key in source.data
            }
    if fact_type == "work_boundary" and source.type in _RUN_BOUNDARY_TYPES:
        return {
            "state": source.type,
            "run_id": source.run_id,
            **({"reason": source.data["reason"]} if "reason" in source.data else {}),
        }
    if (
        fact_type == "unresolved_operation"
        and source.type == OPERATION_RECONCILE_REQUIRED
    ):
        return {
            key: source.data[key]
            for key in ("tool_call_id", "tool_name", "state")
            if key in source.data
        }
    if fact_type == "evidence_ref" and source.type in {
        ARTIFACT_CREATED,
        ARTIFACT_EXTERNALIZED,
    }:
        return {
            key: source.data[key] for key in _ARTIFACT_FACT_FIELDS if key in source.data
        }
    raise ValueError("source event is not a confirmed system fact for this type")


def _user_value_matches(value: Any, source: SessionEvent) -> bool:
    content = source.data.get("content")
    if not isinstance(content, str):
        return False
    if isinstance(value, str):
        return bool(value) and value in content
    try:
        return json.loads(content) == value
    except (json.JSONDecodeError, TypeError):
        return False


def validate_user_protected_fact_annotations(
    content: Any, annotations: Any
) -> list[dict[str, Any]]:
    """Validate explicit, source-bound fact annotations on a direct user input."""
    if not isinstance(content, str) or not content:
        raise ValueError("protected facts require a direct user message")
    if not isinstance(annotations, list) or len(annotations) > 32:
        raise ValueError("protected_facts must be a list of at most 32 annotations")

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for annotation in annotations:
        if not isinstance(annotation, dict) or set(annotation) - {
            "fact_type",
            "value",
            "supersedes_fact_id",
        }:
            raise ValueError("protected fact annotation has unsupported fields")
        fact_type = annotation.get("fact_type")
        value = annotation.get("value")
        supersedes_id = annotation.get("supersedes_fact_id")
        if not isinstance(fact_type, str) or fact_type not in _USER_INPUT_FACT_TYPES:
            raise ValueError("protected fact annotation type is unsupported")
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 10_000
            or value not in content
        ):
            raise ValueError(
                "protected fact value must match the direct user message"
            )
        if supersedes_id is not None and (
            not isinstance(supersedes_id, str)
            or not supersedes_id
            or len(supersedes_id) > 200
        ):
            raise ValueError("supersedes_fact_id must be a non-empty fact id")
        identity = (fact_type, value, supersedes_id)
        if identity in seen:
            raise ValueError("duplicate protected fact annotation")
        seen.add(identity)
        normalized.append(
            {
                "fact_type": fact_type,
                "value": value,
                **(
                    {"supersedes_fact_id": supersedes_id}
                    if supersedes_id is not None
                    else {}
                ),
            }
        )
    return normalized


def _failed_tool_result(evidence: SessionEvent) -> dict[str, Any] | None:
    if evidence.type != TOOL_RESULT:
        return None
    content = evidence.data.get("content")
    if not isinstance(content, str):
        return None
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(result, dict)
        or result.get("ok") is not False
        or not isinstance(result.get("error_code"), str)
        or not result["error_code"]
    ):
        return None
    return result


def _tool_attempt_value(source: SessionEvent) -> dict[str, Any] | None:
    if source.type != TOOL_CALL:
        return None
    call_id = source.data.get("tool_call_id")
    tool_name = source.data.get("tool_name")
    args = source.data.get("args")
    if (
        not isinstance(call_id, str)
        or not call_id
        or not isinstance(tool_name, str)
        or not isinstance(args, dict)
    ):
        return None
    return {"tool_call_id": call_id, "tool_name": tool_name, "args": args}


def _is_refuting_event(source: SessionEvent, evidence: SessionEvent) -> bool:
    if evidence.seq <= source.seq or evidence.session_id != source.session_id:
        return False
    if evidence.type in _USER_SOURCE_TYPES:
        return (
            isinstance(evidence.data.get("content"), str)
            and not evidence.data.get("injected_by")
            and evidence.data.get("refutes_event_id") == source.event_id
        )
    if source.type != TOOL_CALL:
        return False
    return (
        source.data.get("tool_call_id") == evidence.data.get("tool_call_id")
        and _failed_tool_result(evidence) is not None
    )


def validate_user_fact_links(
    events: list[SessionEvent],
    data: dict[str, Any],
    *,
    session_id: str,
    require_active_revocation: bool = False,
) -> list[str]:
    """Validate explicit user links to earlier protected facts or attempted events."""
    refs: list[str] = []
    revoke_id = data.get("revoke_fact_id")
    refutes_id = data.get("refutes_event_id")
    annotations = data.get("protected_facts")
    if annotations is not None:
        if data.get("injected_by"):
            raise ValueError("only direct user input can register protected facts")
        normalized_annotations = validate_user_protected_fact_annotations(
            data.get("content"), annotations
        )
        referenced_facts = {
            fact.fact_id: fact
            for fact in derive_protected_facts(events)
            if fact.session_id == session_id
        }
        for annotation in normalized_annotations:
            supersedes_id = annotation.get("supersedes_fact_id")
            if supersedes_id is None:
                continue
            superseded = referenced_facts.get(supersedes_id)
            if superseded is None or superseded.type != annotation["fact_type"]:
                raise ValueError(
                    "supersedes_fact_id must reference an earlier fact of the same type"
                )
            if superseded.source_event_id not in refs:
                refs.append(superseded.source_event_id)
    if data.get("injected_by") and (revoke_id is not None or refutes_id is not None):
        raise ValueError("only direct user input can link protected facts")

    if revoke_id is not None:
        if not isinstance(revoke_id, str) or not revoke_id:
            raise ValueError("revoke_fact_id must be a non-empty string")
        facts = {
            fact.fact_id: fact
            for fact in derive_protected_facts(events)
            if fact.session_id == session_id
        }
        fact = facts.get(revoke_id)
        if fact is None or fact.type != "authorization":
            raise ValueError("revoke_fact_id must reference an authorization fact")
        if require_active_revocation and fact.status != "active":
            raise ValueError("revoke_fact_id must reference an active authorization")
        refs.append(fact.source_event_id)

    if refutes_id is not None:
        if not isinstance(refutes_id, str) or not refutes_id:
            raise ValueError("refutes_event_id must be a non-empty string")
        source = next(
            (
                event for event in events
                if event.event_id == refutes_id
                and event.session_id == session_id
            ),
            None,
        )
        if source is None or source.type not in _USER_SOURCE_TYPES | {TOOL_CALL}:
            raise ValueError("refutes_event_id must reference a user or tool attempt")
        if source.type in _USER_SOURCE_TYPES:
            if not isinstance(source.data.get("content"), str) or source.data.get(
                "injected_by"
            ):
                raise ValueError("refutes_event_id must reference a direct user input")
        elif _tool_attempt_value(source) is None:
            raise ValueError("refutes_event_id must reference a valid tool attempt")
        refs.append(refutes_id)
    return refs


def _expected_fact_id(data: dict[str, Any]) -> str:
    identity = {
        key: data.get(key)
        for key in (
            "fact_type",
            "value",
            "source_event_id",
            "supersedes_fact_id",
            "evidence_event_id",
        )
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "pf-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def protected_fact_id(data: dict[str, Any]) -> str:
    """Return the stable identity for a protected-fact registration payload."""
    return _expected_fact_id(data)


def validate_protected_fact_data(
    events: list[SessionEvent],
    data: dict[str, Any],
    *,
    session_id: str,
    event_index: dict[str, SessionEvent] | None = None,
    fact_index: dict[str, tuple[int, str]] | None = None,
    latest_tool_result_seq: dict[str, int] | None = None,
    latest_reconciled_seq: dict[str, int] | None = None,
) -> tuple[SessionEvent, SessionEvent | None]:
    """Validate a registration against earlier, same-session source events."""
    fact_type = data.get("fact_type")
    if not isinstance(fact_type, str):
        raise TypeError("protected fact type must be a string")
    source_id = data.get("source_event_id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("protected fact source event is required")
    if event_index is None:
        event_index = {event.event_id: event for event in events}
    source = event_index.get(source_id)
    if source is None or source.session_id != session_id:
        raise ValueError("protected fact source event must exist in this session")
    source_seq = data.get("source_event_seq")
    if (
        not isinstance(source_seq, int)
        or isinstance(source_seq, bool)
        or source_seq != source.seq
    ):
        raise ValueError("protected fact source sequence does not match its event")

    value = data.get("value")
    if fact_type in _USER_FACT_TYPES or fact_type in {"authorization", "work_boundary"}:
        allowed_user = (
            fact_type != "work_boundary"
            and source.type in _USER_SOURCE_TYPES
            and not source.data.get("injected_by")
        )
        if allowed_user and _user_value_matches(value, source):
            pass
        elif (
            fact_type == "authorization"
            and source.type in {SESSION_STARTED, PERMISSION_CHANGED}
            and bool(_system_fact_value(source, fact_type))
            or fact_type == "work_boundary"
            and source.type in _RUN_BOUNDARY_TYPES
        ):
            if value != _system_fact_value(source, fact_type):
                raise ValueError(
                    "protected fact value must match the confirmed system source"
                )
        else:
            raise ValueError(
                "protected fact source is not a direct user event or confirmed fact"
            )
    elif fact_type in {"unresolved_operation", "evidence_ref"}:
        expected_source_types = (
            {OPERATION_RECONCILE_REQUIRED}
            if fact_type == "unresolved_operation"
            else {ARTIFACT_CREATED, ARTIFACT_EXTERNALIZED}
        )
        if source.type not in expected_source_types or value != _system_fact_value(
            source, fact_type
        ):
            raise ValueError("protected fact source is not a confirmed system fact")
        if fact_type == "unresolved_operation":
            if (
                not isinstance(source.data.get("tool_call_id"), str)
                or not source.data["tool_call_id"]
                or source.data.get("state") != "NEED_RECONCILE"
            ):
                raise ValueError("unresolved operation source is missing its identity or state")
            if latest_tool_result_seq is None:
                latest_tool_result_seq = {
                    event.data["tool_call_id"]: event.seq
                    for event in events
                    if event.type == TOOL_RESULT
                    and isinstance(event.data.get("tool_call_id"), str)
                }
            result_seq = latest_tool_result_seq.get(
                source.data.get("tool_call_id", ""), -1
            )
            if latest_reconciled_seq is None:
                latest_reconciled_seq = {
                    event.data["tool_call_id"]: event.seq
                    for event in events
                    if event.type == OPERATION_RECONCILED
                    and isinstance(event.data.get("tool_call_id"), str)
                }
            if (
                result_seq > source.seq
                or latest_reconciled_seq.get(
                    source.data.get("tool_call_id", ""), -1
                ) > source.seq
            ):
                raise ValueError("operation has a later result and is already reconciled")
    elif fact_type == "failed_approach":
        if source.type not in _USER_SOURCE_TYPES | {TOOL_CALL}:
            raise ValueError(
                "failed approach source must describe a user or tool attempt"
            )
        if source.type in _USER_SOURCE_TYPES and source.data.get("injected_by"):
            raise ValueError("failed approach source must be a direct user event")
        expected_value = (
            _tool_attempt_value(source)
            if source.type == TOOL_CALL
            else source.data.get("content")
        )
        if value != expected_value:
            raise ValueError(
                "failed approach value must preserve its source attempt verbatim"
            )
    else:
        raise ValueError("unsupported protected fact type")

    evidence_id = data.get("evidence_event_id")
    evidence: SessionEvent | None = None
    if fact_type == "failed_approach":
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError("failed approach evidence event is required")
        evidence = event_index.get(evidence_id)
        if (
            evidence is None
            or evidence.session_id != session_id
            or not _is_refuting_event(source, evidence)
        ):
            raise ValueError("failed approach evidence must be a later refuting event")
        evidence_seq = data.get("evidence_event_seq")
        if (
            not isinstance(evidence_seq, int)
            or isinstance(evidence_seq, bool)
            or evidence_seq != evidence.seq
        ):
            raise ValueError(
                "failed approach evidence sequence does not match its event"
            )
    elif evidence_id is not None or data.get("evidence_event_seq") is not None:
        raise ValueError("only failed approaches carry a separate evidence event")

    supersedes_id = data.get("supersedes_fact_id")
    if fact_type == "authorization_revocation" and not supersedes_id:
        raise ValueError(
            "authorization revocation must name the superseded authorization"
        )
    if supersedes_id is not None:
        if (
            source.type not in _USER_SOURCE_TYPES
            or source.data.get("injected_by")
            or not isinstance(supersedes_id, str)
        ):
            raise ValueError("only a later user event can supersede a protected fact")
        if fact_index is None:
            fact_index = {
                fact.fact_id: (fact.source_seq, fact.type)
                for fact in derive_protected_facts(events)
            }
        prior = fact_index.get(supersedes_id)
        if prior is None or source.seq <= prior[0]:
            raise ValueError("superseded fact must exist before the later user event")
        if (
            fact_type == "authorization_revocation"
            and prior[1] != "authorization"
        ):
            raise ValueError(
                "authorization revocation must supersede an authorization fact"
            )
        if fact_type != "authorization_revocation" and prior[1] != fact_type:
            raise ValueError("updated protected fact must supersede the same fact type")

    fact_id = data.get("fact_id")
    if not isinstance(fact_id, str) or fact_id != _expected_fact_id(data):
        raise ValueError("protected fact id is invalid")
    return source, evidence


def build_protected_fact_data(
    events: list[SessionEvent],
    *,
    session_id: str,
    fact_type: str,
    value: Any,
    source_event_id: str | None,
    supersedes_fact_id: str | None = None,
    evidence_event_id: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic event payload and reject untraceable registrations."""
    if not isinstance(source_event_id, str) or not source_event_id:
        raise ValueError("protected fact source event is required")
    try:
        copied_value = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("protected fact value must be JSON-compatible") from exc
    source = next(
        (event for event in events if event.event_id == source_event_id), None
    )
    if source is None:
        raise ValueError("protected fact source event must exist in this session")
    evidence = None
    if evidence_event_id is not None:
        evidence = next(
            (event for event in events if event.event_id == evidence_event_id), None
        )
    data: dict[str, Any] = {
        "fact_type": fact_type,
        "value": copied_value,
        "source_event_id": source_event_id,
        "source_event_seq": source.seq,
    }
    if supersedes_fact_id is not None:
        data["supersedes_fact_id"] = supersedes_fact_id
    if evidence_event_id is not None:
        data["evidence_event_id"] = evidence_event_id
        if evidence is not None:
            data["evidence_event_seq"] = evidence.seq
    data["fact_id"] = _expected_fact_id(data)
    validate_protected_fact_data(events, data, session_id=session_id)
    return data


def derive_protected_facts(events: list[SessionEvent]) -> list[ProtectedFact]:
    """Rebuild protected task facts from the immutable event prefix."""
    event_by_seq = {event.seq: event for event in events}
    event_by_id = {event.event_id: event for event in events}
    superseded_sources = {
        source.event_id
        for event in events
        if event.type == MESSAGE_SUPERSEDED
        and isinstance(event.data.get("superseded_seq"), int)
        and not isinstance(event.data.get("superseded_seq"), bool)
        and (source := event_by_seq.get(event.data["superseded_seq"])) is not None
        and event.seq > source.seq
        and source.session_id == event.session_id
        and source.type in _USER_SOURCE_TYPES
        and not source.data.get("injected_by")
    }
    cancelled_queue_ids = {
        event.data.get("queue_id")
        for event in events
        if event.type == QUEUE_CANCELLED
        and isinstance(event.data.get("queue_id"), str)
    }

    def is_cancelled_queue_source(event: SessionEvent) -> bool:
        queue_id = event.data.get("queue_id")
        return (
            event.type == MESSAGE_QUEUED
            and isinstance(queue_id, str)
            and queue_id in cancelled_queue_ids
        )

    superseded_sources.update(
        event.event_id
        for event in events
        if is_cancelled_queue_source(event)
    )
    latest_tool_result_seq = {
        event.data["tool_call_id"]: event.seq
        for event in events
        if event.type == TOOL_RESULT
        and isinstance(event.data.get("tool_call_id"), str)
    }
    latest_reconciled_seq = {
        event.data["tool_call_id"]: event.seq
        for event in events
        if event.type == OPERATION_RECONCILED
        and isinstance(event.data.get("tool_call_id"), str)
    }
    facts: list[ProtectedFact] = []
    fact_ids: set[str] = set()
    prior_event_by_id: dict[str, SessionEvent] = {}
    prior_fact_by_id: dict[str, tuple[int, str]] = {}
    superseded_by: dict[str, str] = {}
    latest_permission_fact_id: str | None = None
    def is_active_user_source(event: SessionEvent) -> bool:
        content = event.data.get("content")
        return (
            event.type in _USER_SOURCE_TYPES
            and isinstance(content, str)
            and bool(content.strip())
            and not event.data.get("injected_by")
            and not event.data.get("replace")
            and not is_cancelled_queue_source(event)
        )

    first_user_source = next(
        (event for event in events if is_active_user_source(event)),
        None,
    )
    user_goal_sources = {first_user_source.event_id} if first_user_source else set()
    direct_user_events = [
        event
        for event in events
        if is_active_user_source(event)
    ]
    direct_user_seqs = [event.seq for event in direct_user_events]
    for event in events:
        target_seq = event.data.get("superseded_seq")
        if (
            event.type != MESSAGE_SUPERSEDED
            or not isinstance(target_seq, int)
            or isinstance(target_seq, bool)
        ):
            continue
        target = event_by_seq.get(target_seq)
        if (
            target is None
            or target.type != USER_MESSAGE
            or target.data.get("injected_by")
            or event.seq <= target.seq
            or event.session_id != target.session_id
        ):
            continue
        replacement_index = bisect_right(direct_user_seqs, target.seq)
        replacement_end = bisect_left(direct_user_seqs, event.seq)
        if replacement_end > replacement_index:
            user_goal_sources.add(
                direct_user_events[replacement_end - 1].event_id
            )
    tool_results_by_call: dict[str, list[SessionEvent]] = {}
    for event in events:
        call_id = event.data.get("tool_call_id")
        if event.type == TOOL_RESULT and isinstance(call_id, str):
            tool_results_by_call.setdefault(call_id, []).append(event)

    def add_fact(fact: ProtectedFact) -> bool:
        if fact.fact_id in fact_ids:
            return False
        fact_ids.add(fact.fact_id)
        prior_fact_by_id[fact.fact_id] = (fact.source_seq, fact.type)
        facts.append(fact)
        return True

    for event in events:
        data = event.data
        if event.event_id in user_goal_sources:
            fact_data = {
                "fact_type": "user_goal",
                "value": data["content"],
                "source_event_id": event.event_id,
            }
            add_fact(
                ProtectedFact(
                    fact_id=_expected_fact_id(fact_data),
                    type="user_goal",
                    value=data["content"],
                    source_event_id=event.event_id,
                    source_seq=event.seq,
                    status="active",
                    session_id=event.session_id,
                )
            )
        if event.type in {SESSION_STARTED, PERMISSION_CHANGED}:
            permission_value = _system_fact_value(event, "authorization")
            if permission_value:
                fact = _fact_from_system_event(
                    event, "authorization", permission_value
                )
                if latest_permission_fact_id is not None:
                    successor_id = superseded_by.get(latest_permission_fact_id)
                    if (
                        successor_id is not None
                        and prior_fact_by_id.get(successor_id, (0, ""))[1]
                        == "authorization_revocation"
                    ):
                        superseded_by[successor_id] = fact.fact_id
                    else:
                        superseded_by[latest_permission_fact_id] = fact.fact_id
                latest_permission_fact_id = fact.fact_id
                add_fact(fact)
        elif event.type in _RUN_BOUNDARY_TYPES:
            add_fact(
                _fact_from_system_event(
                    event, "work_boundary", _system_fact_value(event, "work_boundary")
                )
            )
        elif event.type == OPERATION_RECONCILE_REQUIRED:
            tool_call_id = data.get("tool_call_id", "")
            if (
                latest_tool_result_seq.get(tool_call_id, -1) <= event.seq
                and latest_reconciled_seq.get(tool_call_id, -1) <= event.seq
            ):
                add_fact(
                    _fact_from_system_event(
                        event,
                        "unresolved_operation",
                        _system_fact_value(event, "unresolved_operation"),
                    )
                )
        elif event.type == USER_MESSAGE:
            revoke_id = data.get("revoke_fact_id")
            if (
                not data.get("injected_by")
                and isinstance(revoke_id, str)
                and prior_fact_by_id.get(revoke_id, (0, ""))[1] == "authorization"
            ):
                fact_data = {
                    "fact_type": "authorization_revocation",
                    "value": data.get("content", ""),
                    "source_event_id": event.event_id,
                    "supersedes_fact_id": revoke_id,
                }
                fact_id = _expected_fact_id(fact_data)
                if add_fact(
                    ProtectedFact(
                        fact_id=fact_id,
                        type="authorization_revocation",
                        value=fact_data["value"],
                        source_event_id=event.event_id,
                        source_seq=event.seq,
                        status="active",
                        session_id=event.session_id,
                    )
                ):
                    successor_id = superseded_by.get(revoke_id)
                    if successor_id is None:
                        superseded_by[revoke_id] = fact_id
                    else:
                        superseded_by[fact_id] = successor_id

            try:
                annotations = validate_user_protected_fact_annotations(
                    data.get("content"), data.get("protected_facts", [])
                )
            except ValueError:
                annotations = []
            if not data.get("injected_by"):
                for annotation in annotations:
                    fact_data = {
                        "fact_type": annotation["fact_type"],
                        "value": annotation["value"],
                        "source_event_id": event.event_id,
                        "source_event_seq": event.seq,
                    }
                    supersedes_id = annotation.get("supersedes_fact_id")
                    if supersedes_id is not None:
                        fact_data["supersedes_fact_id"] = supersedes_id
                    fact_data["fact_id"] = _expected_fact_id(fact_data)
                    try:
                        validate_protected_fact_data(
                            events,
                            fact_data,
                            session_id=event.session_id,
                            event_index=event_by_id,
                            fact_index=prior_fact_by_id,
                            latest_tool_result_seq=latest_tool_result_seq,
                            latest_reconciled_seq=latest_reconciled_seq,
                        )
                    except (TypeError, ValueError):
                        logger.warning(
                            "Ignoring invalid protected fact annotation on %s",
                            event.event_id,
                        )
                        continue
                    fact_id = fact_data["fact_id"]
                    added = add_fact(
                        ProtectedFact(
                            fact_id=fact_id,
                            type=annotation["fact_type"],
                            value=annotation["value"],
                            source_event_id=event.event_id,
                            source_seq=event.seq,
                            status="active",
                            session_id=event.session_id,
                        )
                    )
                    if added and supersedes_id is not None:
                        successor_id = superseded_by.get(supersedes_id)
                        if successor_id is None:
                            superseded_by[supersedes_id] = fact_id
                        else:
                            superseded_by[fact_id] = successor_id

            refutes_id = data.get("refutes_event_id")
            source = (
                prior_event_by_id.get(refutes_id)
                if isinstance(refutes_id, str)
                else None
            )
            if source is not None and _is_refuting_event(source, event):
                value = (
                    _tool_attempt_value(source)
                    if source.type == TOOL_CALL
                    else source.data.get("content")
                )
                fact_data = {
                    "fact_type": "failed_approach",
                    "value": value,
                    "source_event_id": source.event_id,
                    "evidence_event_id": event.event_id,
                }
                add_fact(
                    ProtectedFact(
                        fact_id=_expected_fact_id(fact_data),
                        type="failed_approach",
                        value=value,
                        source_event_id=source.event_id,
                        source_seq=source.seq,
                        status="active",
                        session_id=event.session_id,
                        evidence_event_id=event.event_id,
                        evidence_seq=event.seq,
                    )
                )
        elif event.type in {ARTIFACT_CREATED, ARTIFACT_EXTERNALIZED}:
            add_fact(
                _fact_from_system_event(
                    event, "evidence_ref", _system_fact_value(event, "evidence_ref")
                )
            )
        elif event.type == TOOL_CALL:
            attempt = _tool_attempt_value(event)
            if attempt is not None:
                for evidence in tool_results_by_call.get(
                    attempt["tool_call_id"], []
                ):
                    if not _is_refuting_event(event, evidence):
                        continue
                    fact_data = {
                        "fact_type": "failed_approach",
                        "value": attempt,
                        "source_event_id": event.event_id,
                        "evidence_event_id": evidence.event_id,
                    }
                    add_fact(
                        ProtectedFact(
                            fact_id=_expected_fact_id(fact_data),
                            type="failed_approach",
                            value=attempt,
                            source_event_id=event.event_id,
                            source_seq=event.seq,
                            status="active",
                            session_id=event.session_id,
                            evidence_event_id=evidence.event_id,
                            evidence_seq=evidence.seq,
                        )
                    )
        elif event.type == TASK_PROTECTED_FACT:
            try:
                source, evidence = validate_protected_fact_data(
                    events,
                    data,
                    session_id=event.session_id,
                    event_index=prior_event_by_id,
                    fact_index=prior_fact_by_id,
                    latest_tool_result_seq=latest_tool_result_seq,
                    latest_reconciled_seq=latest_reconciled_seq,
                )
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring invalid protected fact event %s", event.event_id
                )
                continue
            fact_id = data["fact_id"]
            if not add_fact(
                ProtectedFact(
                    fact_id=fact_id,
                    type=data["fact_type"],
                    value=data["value"],
                    source_event_id=source.event_id,
                    source_seq=source.seq,
                    status="active",
                    session_id=event.session_id,
                    evidence_event_id=evidence.event_id if evidence else None,
                    evidence_seq=evidence.seq if evidence else None,
                )
            ):
                continue
            prior_fact_id = data.get("supersedes_fact_id")
            if prior_fact_id:
                successor_id = superseded_by.get(prior_fact_id)
                if successor_id is None:
                    superseded_by[prior_fact_id] = fact_id
                else:
                    superseded_by[fact_id] = successor_id

        prior_event_by_id[event.event_id] = event

    for index, fact in enumerate(facts):
        successor_id = superseded_by.get(fact.fact_id)
        if fact.source_event_id in superseded_sources or successor_id is not None:
            facts[index] = replace(
                fact,
                status="superseded",
                superseded_by_fact_id=successor_id or fact.superseded_by_fact_id,
            )
    return facts


def _fact_from_system_event(
    event: SessionEvent, fact_type: str, value: Any
) -> ProtectedFact:
    return ProtectedFact(
        fact_id=f"{fact_type}:{event.event_id}",
        type=fact_type,
        value=value,
        source_event_id=event.event_id,
        source_seq=event.seq,
        status="active",
        session_id=event.session_id,
    )


#: bracket 元数据事件——不投影成消息，仅标记 shadowed 区间。
_BRACKET_META_TYPES = frozenset({
    COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END,
})
COMPACTION_SUMMARY_MESSAGE_NAME = "context_compaction_summary"


def _normalize_tool_calls_for_projection(
    raw: object,
) -> list[dict[str, object]]:
    """把 MODEL_COMPLETED.tool_calls 规整成投影安全的 list[dict]。

    单事件层容错：tool_calls 可能被旧日志、手工写入、序列化路径污染成非 list
    （dict / str / None）。derive_messages 是恢复链的必经节点，一行坏数据不能
    brick 整个 session 的恢复（违反存储模块"一行坏数据只损失该行"的契约）。
    非法形状降级为无 tool_calls（只丢该事件的工具调用，不让整个投影抛错）；
    单条非 dict 元素跳过。

    缺 id 在投影层保留空串（与事件原始形状一致，仅做消息重建），不在投影层
    合成占位 id；Tool Runtime 边界的 ToolCall.normalize 才升级为 gen_<hex> 占位
    （那是 Ledger 主键与 ToolMessage 配对的关键）。两层策略刻意不同：投影只读、
    Runtime 才赋身份。详见 contract.py:ToolCall.normalize。
    """
    if not isinstance(raw, list):
        if raw:
            logger.warning(
                "MODEL_COMPLETED.tool_calls 形状非法（%s），降级为无 tool_calls",
                type(raw).__name__,
            )
        return []
    normalized: list[dict[str, object]] = []
    for tc in raw:
        if not isinstance(tc, dict):
            logger.warning(
                "tool_call 元素非 dict（%s），跳过该项", type(tc).__name__
            )
            continue
        normalized.append(
            {
                "id": tc.get("id", ""),
                "name": tc.get("name", ""),
                "args": tc.get("args", {}),
            }
        )
    return normalized


def derive_messages(events: list[SessionEvent]) -> list[AnyMessage]:
    """从事件序列投影出 messages 列表。"""
    return [message for message, _source_range in derive_messages_with_source_ranges(events)]


def derive_messages_with_source_ranges(
    events: list[SessionEvent],
) -> list[tuple[AnyMessage, tuple[int, int] | None]]:
    """从事件序列投影出 messages 列表。

    纯函数：不修改输入 events，不产生副作用。
    dangling tool_call（有 tool/call 无匹配 tool/result）会注入合成 ToolMessage。

    每条事件消息带有对应 seq 范围；摘要消息带被替代的完整范围；合成的 dangling
    ToolMessage 没有来源范围。压缩器用它为滚动摘要计算下一个 bracket 区间。

    T4 (#134)：识别 4-event compaction bracket。bracket 标记的 source_seq 区间
    内的原始投影事件被 shadowed（跳过），CONTEXT_COMPACTED 的 summary 投影成
    HumanMessage 替代被压缩段，避免把不可信历史内容提升为系统指令。

    ADR-0030 (#196)：识别 `message/superseded`——被取代的 user/message 及其整轮
    （答 + tool_call/result）走同一条 shadowed 跳过路径，所以"编辑了问句"在模型可见
    上下文里表现为"旧问句那一轮整段消失、只剩新问句"。dangling 合成发生在 shadow
    之后，被取代轮里的 tool_call 不会被补一条合成 ToolMessage。
    """
    # 第一遍：只接受持久化完整的 compaction bracket。写入中途失败时，
    # append-only 日志可能留下 START 或 SUMMARY；不完整 bracket 不能遮蔽原事件。
    starts: dict[str, tuple[int, int, int]] = {}
    summaries: dict[str, tuple[str, int, int, int]] = {}
    ends: dict[str, int] = {}
    duplicate_ids: set[str] = set()
    start_order: list[str] = []
    for position, event in enumerate(events):
        bracket_id = event.data.get("bracket_id")
        if not isinstance(bracket_id, str) or not bracket_id:
            continue
        if event.type == COMPACTION_START:
            start = event.data.get("source_seq_start")
            end = event.data.get("source_seq_end")
            if (not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)
                    or start < 0 or end < start):
                continue
            if bracket_id in starts:
                duplicate_ids.add(bracket_id)
            else:
                starts[bracket_id] = (start, end, position)
                start_order.append(bracket_id)
        elif event.type == CONTEXT_COMPACTED:
            summary = event.data.get("summary")
            start = event.data.get("source_seq_start")
            end = event.data.get("source_seq_end")
            if (not isinstance(summary, str) or not summary.strip()
                    or not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)):
                continue
            if bracket_id in summaries:
                duplicate_ids.add(bracket_id)
            else:
                summaries[bracket_id] = (summary, start, end, position)
        elif event.type == COMPACTION_END:
            if bracket_id in ends:
                duplicate_ids.add(bracket_id)
            else:
                ends[bracket_id] = position

    valid_brackets: list[tuple[int, int, str, int]] = []
    for bracket_id in start_order:
        start = starts[bracket_id]
        summary = summaries.get(bracket_id)
        end_position = ends.get(bracket_id)
        if (bracket_id in duplicate_ids or summary is None or end_position is None
                or not start[2] < summary[3] < end_position
                or (start[0], start[1]) != (summary[1], summary[2])):
            continue
        valid_brackets.append((start[0], start[1], summary[0], summary[3]))

    # Later compactions normally cover the full range of an earlier summary and
    # extend it. Keep only that later summary for nested ranges. A crossing range,
    # or a later range nested inside an older one, is not produced by the compactor;
    # ignore both brackets so replay exposes the original events instead of choosing
    # an ambiguous summary.
    invalid_brackets: set[int] = set()
    for left_index, left in enumerate(valid_brackets):
        for right_index in range(left_index + 1, len(valid_brackets)):
            right = valid_brackets[right_index]
            if left[1] < right[0] or right[1] < left[0]:
                continue
            if left[:2] == right[:2]:
                older = left_index if left[3] < right[3] else right_index
                invalid_brackets.add(older)
                continue
            left_contains = left[0] <= right[0] and left[1] >= right[1]
            right_contains = right[0] <= left[0] and right[1] >= left[1]
            if left_contains != right_contains:
                outer = left_index if left_contains else right_index
                inner = right_index if left_contains else left_index
                if valid_brackets[outer][3] > valid_brackets[inner][3]:
                    invalid_brackets.add(inner)
                else:
                    invalid_brackets.update((outer, inner))
            else:
                invalid_brackets.update((left_index, right_index))

    retained_brackets = sorted(
        (bracket for index, bracket in enumerate(valid_brackets)
         if index not in invalid_brackets),
        key=lambda bracket: (bracket[0], bracket[3]),
    )
    shadowed_ranges = [(start, end) for start, end, _summary, _position in retained_brackets]
    bracket_summaries = [summary for _start, _end, summary, _position in retained_brackets]

    # ADR-0030 §4.2（#196）：supersede 区间——被取代的那个问句**连同它的整轮**
    # （答、tool_call、tool_result、delta）都不进模型可见投影。
    #
    # 区间 = `[s, n)`，s = 被取代的 user/message seq，n = s 之后第一条**未被取代**的
    # user/message seq；没有这样一条就一直 shadow 到末尾（该轮之后不再有用户输入）。
    # 判据只看 seq 与"该 seq 是否也被取代"，与事件到达顺序无关 ⇒ 纯函数性质不破。
    #
    # ⚠ 刻意与 compaction 的 `shadowed_ranges` **分成两个列表**：下面吐 summary 的循环用
    # `enumerate(shadowed_ranges)` 的下标去索引 `bracket_summaries`（一个 bracket 一条
    # summary）；把 supersede 区间并进去会让下标错位，summary 落到错误的位置甚至不吐。
    # 跳过逻辑仍然只有一处（`is_shadowed`），没有第二套跳过实现。
    superseded_ranges: list[tuple[int, int]] = []
    superseded_seqs: set[int] = set()
    for event in events:
        if event.type != MESSAGE_SUPERSEDED:
            continue
        raw = event.data.get("superseded_seq")
        # 一行坏数据只损失该行（存储模块契约）：非 int 就跳过并警告，不 brick 恢复。
        if isinstance(raw, int) and not isinstance(raw, bool):
            superseded_seqs.add(raw)
        elif raw is not None:
            logger.warning(
                "MESSAGE_SUPERSEDED.superseded_seq 形状非法（%s），忽略该条",
                type(raw).__name__,
            )
    if superseded_seqs:
        user_seqs = [e.seq for e in events if e.type == USER_MESSAGE]
        last_seq = max((e.seq for e in events), default=0)
        for seq in sorted(superseded_seqs):
            following = next(
                (u for u in user_seqs if u > seq and u not in superseded_seqs),
                None,
            )
            # n = 下一条未被取代的 user 消息 ⇒ 区间末尾（含）是 n - 1；没有则到末尾。
            end = (following - 1) if following is not None else last_seq
            if end >= seq:
                superseded_ranges.append((seq, end))

    def is_shadowed(seq: int) -> bool:
        if any(start <= seq <= end for start, end in shadowed_ranges):
            return True
        return any(start <= seq <= end for start, end in superseded_ranges)

    # 第二遍：从事件按顺序投影 messages（不含 dangling 合成）。
    #
    # summary HumanMessage 必须在被压缩段的**原位置**注入——即遇到第一个
    # shadowed 事件时插入 summary，而不是在 CONTEXT_COMPACTED 事件的位置
    # 插入。原因：bracket 事件可能排在当前用户消息之后（compaction 在
    # context build 阶段触发，此时 user/message 已经 append），如果按
    # CONTEXT_COMPACTED 的位置投影 summary，summary 会落到当前用户消息
    # 之后，破坏"摘要在前、当前请求在后"的语义。
    messages: list[tuple[AnyMessage, tuple[int, int] | None]] = []
    summary_emitted = [False] * len(bracket_summaries)

    for event in events:
        # 遇到某个 bracket 的第一个 shadowed 事件时，先吐 summary
        for bi, (s, _e) in enumerate(shadowed_ranges):
            if not summary_emitted[bi] and event.seq == s:
                if bi < len(bracket_summaries):
                    summary = bracket_summaries[bi]
                    if summary:
                        start, end = shadowed_ranges[bi]
                        messages.append((
                            HumanMessage(
                                content=summary,
                                name=COMPACTION_SUMMARY_MESSAGE_NAME,
                            ),
                            (start, end),
                        ))
                summary_emitted[bi] = True
                break

        # CONTEXT_COMPACTED / COMPACTION_START / COMPACTION_END 不投影成消息
        if event.type in _BRACKET_META_TYPES:
            continue

        # 跳过被 bracket shadowed 的原始事件
        if is_shadowed(event.seq):
            continue

        if event.type == USER_MESSAGE:
            content = event.data.get("content", "")
            messages.append((HumanMessage(content=content), (event.seq, event.seq)))

        elif event.type == MODEL_COMPLETED:
            content = event.data.get("content", "")
            tool_calls = _normalize_tool_calls_for_projection(
                event.data.get("tool_calls", [])
            )
            if tool_calls:
                messages.append((
                    AIMessage(content=content, tool_calls=tool_calls),
                    (event.seq, event.seq),
                ))
            else:
                messages.append((AIMessage(content=content), (event.seq, event.seq)))

        elif event.type == TOOL_RESULT:
            tool_call_id = event.data.get("tool_call_id", "")
            content = event.data.get("content", "")
            messages.append((
                ToolMessage(content=content, tool_call_id=tool_call_id),
                (event.seq, event.seq),
            ))

    # 第二遍：为 dangling tool_call 注入合成 ToolMessage。
    # 只在 AIMessage 的某个 tool_call 在紧跟的 ToolMessage 块中没有对应结果时注入。
    # 合成消息插入在该 AIMessage 后紧跟的 ToolMessage 块的末尾。
    result: list[tuple[AnyMessage, tuple[int, int] | None]] = []
    i = 0
    while i < len(messages):
        msg, source_range = messages[i]
        result.append((msg, source_range))

        if isinstance(msg, AIMessage) and msg.tool_calls:
            # 收集这条 AIMessage 之后紧跟的连续 ToolMessage 块
            block_ids: set[str] = set()
            block_end = i + 1
            while block_end < len(messages) and isinstance(
                messages[block_end][0], ToolMessage
            ):
                block_ids.add(messages[block_end][0].tool_call_id)
                result.append(messages[block_end])
                block_end += 1

            # 为块中缺失的 tool_call 追加合成 ToolMessage
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                if tc_id and tc_id not in block_ids:
                    logger.warning(
                        "dangling tool_call 检测：tool_call_id=%s 无匹配 tool/result，"
                        "注入合成 ToolMessage",
                        tc_id,
                    )
                    result.append((
                        ToolMessage(content=DANGLING_TOOL_CONTENT, tool_call_id=tc_id),
                        None,
                    ))
            i = block_end
        else:
            i += 1

    return result


def collect_dangling(
    events: list[SessionEvent],
) -> tuple[set[str], set[str]]:
    """单遍扫描事件，返回 (dangling tool_call_ids, 已有 tool/call 事件的 ids)。

    dangling 的判定必须同时覆盖两个事实源（此前 RecoveryCoordinator 与
    detect_dangling 各维护一份近重复实现，2026-09-09 统一到这里）：
    - tool/call 事件（resume 一致性用）；
    - model/completed 的 tool_calls 字段（derive_messages 的 AIMessage 投影用）。

    MODEL_COMPLETED.tool_calls 经 ``_normalize_tool_calls_for_projection``
    容错（非 list / 非 dict 元素降级跳过——恢复链必经节点，一行坏数据不能
    brick 整个 session 的恢复）。
    """
    requested: set[str] = set()
    call_event_ids: set[str] = set()
    resolved: set[str] = set()
    for event in events:
        if event.type == MODEL_COMPLETED:
            for tc in _normalize_tool_calls_for_projection(
                event.data.get("tool_calls", [])
            ):
                tc_id = tc.get("id", "")
                if tc_id:
                    requested.add(tc_id)
        elif event.type == TOOL_CALL:
            tc_id = event.data.get("tool_call_id", "")
            if tc_id:
                requested.add(tc_id)
                call_event_ids.add(tc_id)
        elif event.type == TOOL_RESULT:
            tc_id = event.data.get("tool_call_id", "")
            if tc_id:
                resolved.add(tc_id)
    return requested - resolved, call_event_ids


def detect_dangling(events: list[SessionEvent]) -> list[str]:
    """返回事件序列中 dangling 的 tool_call_id 列表（有请求无结果）。

    供 Session.append 在 resume 时决定是否需要合成 tool/result 事件。

    真相源与 derive_messages 对齐：请求侧同时看 TOOL_CALL 事件和
    MODEL_COMPLETED.tool_calls。崩溃可能发生在 MODEL_COMPLETED 已持久化但
    TOOL_CALL 还没写的窗口——只看 TOOL_CALL 会让这种 dangling 静默漏掉，
    Session.resume 不合成 tool/result，历史永久悬空（derive_messages 每次
    投影都重复触发 dangling 警告）。
    """
    dangling, _ = collect_dangling(events)
    return sorted(dangling)


#: 未投递输入的两种载体（ADR-0030 §2 术语表）。
KIND_QUEUE = "queue"
KIND_STEER = "steer"


@dataclass(frozen=True)
class UndeliveredInput:
    """一条尚未变成 run 的用户输入（queue 项或未生效的 steer 请求）。

    `seq` 是**到达顺序**（ADR-0030 D7）：queue 与 steer 混排时按它排序，而不是
    "先队列后 steer"——两种载体的差别只在投递边界，不在优先级。
    """

    kind: str  # KIND_QUEUE / KIND_STEER
    input_id: str  # queue_id（queue）或 steer_id（steer）
    content: str
    seq: int
    created_at: str
    #: steer 的注入目标 run（重建内存注册表时还原）。queue 恒 None。
    run_id: str | None = None
    revoke_fact_id: str | None = None
    refutes_event_id: str | None = None
    protected_facts: list[dict[str, Any]] | None = None


def undelivered_inputs(events: list[SessionEvent]) -> list[UndeliveredInput]:
    """未投递输入，按到达顺序（seq）升序——D4 驱动点与 `GET /queue` 的唯一判据。

    纯函数（与 derive_messages 同族：只读事件、不碰内存态）。判据：

    - `message/queued` 中未被 `queue/cancelled` 取消、未被 `queue/consumed` 消费的；
    - `steer/requested` 中未被 `steer/applied` 收口的。

    这是"队列跨崩溃存活"（D5）的落点：未投递 = **事件流上的事实**，与进程内那
    份缓存无关，所以重启后按它重建即可（ADR-0030 §4.8）。一行坏数据（缺 id /
    id 非法形状）只损失该行，不让整个派生抛错——与 collect_dangling 的容错口径一致。
    """
    cancelled: set[str] = set()
    consumed: set[str] = set()
    applied: set[str] = set()
    for event in events:
        if event.type == QUEUE_CANCELLED or event.type == QUEUE_CONSUMED:
            target = cancelled if event.type == QUEUE_CANCELLED else consumed
        elif event.type == STEER_APPLIED:
            target = applied
        else:
            continue
        value = event.data.get("steer_id" if event.type == STEER_APPLIED else "queue_id")
        if isinstance(value, str) and value:
            target.add(value)

    items: list[UndeliveredInput] = []
    for event in events:
        if event.type == MESSAGE_QUEUED:
            input_id = event.data.get("queue_id")
            if not isinstance(input_id, str) or not input_id:
                continue
            if input_id in cancelled or input_id in consumed:
                continue
            content = event.data.get("content", "")
            annotations = _undelivered_fact_annotations(event, content)
            items.append(
                UndeliveredInput(
                    kind=KIND_QUEUE,
                    input_id=input_id,
                    content=str(event.data.get("content", "")),
                    seq=event.seq,
                    created_at=event.time,
                    revoke_fact_id=event.data.get("revoke_fact_id"),
                    refutes_event_id=event.data.get("refutes_event_id"),
                    protected_facts=annotations,
                )
            )
        elif event.type == STEER_REQUESTED:
            input_id = event.data.get("steer_id")
            if not isinstance(input_id, str) or not input_id:
                continue
            if input_id in applied:
                continue
            run_id = event.data.get("run_id")
            content = event.data.get("content", "")
            annotations = _undelivered_fact_annotations(event, content)
            items.append(
                UndeliveredInput(
                    kind=KIND_STEER,
                    input_id=input_id,
                    content=str(event.data.get("content", "")),
                    seq=event.seq,
                    created_at=event.time,
                    run_id=run_id if isinstance(run_id, str) else None,
                    revoke_fact_id=event.data.get("revoke_fact_id"),
                    refutes_event_id=event.data.get("refutes_event_id"),
                    protected_facts=annotations,
                )
            )
    items.sort(key=lambda item: item.seq)
    return items


def _undelivered_fact_annotations(
    event: SessionEvent, content: Any
) -> list[dict[str, Any]] | None:
    raw = event.data.get("protected_facts")
    if raw is None:
        return None
    try:
        return validate_user_protected_fact_annotations(content, raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid protected fact annotations on queued input %s",
            event.event_id,
        )
        return None
