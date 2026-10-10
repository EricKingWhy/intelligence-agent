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

from agent_harness.attachments.projection import (
    IMAGE_OMITTED_PLACEHOLDER,
    content_block_with_text,
    parse_image_refs,
    text_with_omitted_images,
)
from agent_harness.session.errors import AttachmentNotReferenced
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

#: #566：审批门前悬空调用的诚实合成文案（封闭词表，绑定系统实际掌握的事实）。
#: 执行域顺序（`04 §9.1`）是审批闸门 → 接纳点（Ledger PENDING）→ execute——
#: 「审批未通过 / 已批准未执行」与 Ledger 无账行组合即可证明工具**从未运行**，
#: 不能再说"结果未知"夸大不确定性。保守方向不变：无法证明未执行的场合
#: （执行可能已开始）仍然只说 DANGLING_TOOL_CONTENT。
DANGLING_NOT_EXECUTED_DENIED = "工具未执行（审批未通过）"
DANGLING_NOT_EXECUTED_APPROVED = "工具未执行（已批准，但尚未开始执行）"
DANGLING_NOT_EXECUTED = "工具未执行（尚未开始执行）"

#: 「修改文件」写工具语义（W-31.5 #417）——唯一权威定义。
#: 消费方：`multiagent/provider.py` 的 changed_files（SubAgentResult 字段，
#: 只活在 delegate 回传里）与本模块 `derive_modified_file_paths`（PRD §4.6
#: 第 4 项 build 注入清单）。读文件不在此列：摘要第 8 节「文件清单」是
#: 另一口径（读+改都进，见 compactor._programmatic_summary_sections）。
WRITE_TOOL_NAMES = frozenset({"write", "edit", "apply_patch"})

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


def _cancelled_queue_ids(events: list[SessionEvent]) -> set[str]:
    """被 `queue/cancelled` 取消掉的 `queue_id` 集合（顺序无关的纯集合运算）。

    取消判据的**唯一**实现：投影（`derive_protected_facts`）与事件口径来源校验
    （`is_direct_user_input_event` 经 `user_source_events`）都从这里取，不再各写一份
    （Call 3 P4-1）。
    """
    return {
        event.data.get("queue_id")
        for event in events
        if event.type == QUEUE_CANCELLED
        and isinstance(event.data.get("queue_id"), str)
    }


def _is_cancelled_queue_source(event: SessionEvent, cancelled_ids: set[str]) -> bool:
    queue_id = event.data.get("queue_id")
    return (
        event.type == MESSAGE_QUEUED
        and isinstance(queue_id, str)
        and queue_id in cancelled_ids
    )


def user_source_events(events: list[SessionEvent]) -> list[SessionEvent]:
    """形状上算用户来源的事件，按 seq 升序——**不含** supersede 判定。

    判据：类型在 `_USER_SOURCE_TYPES`、`content` 是非空 string、无 `injected_by`、
    无 `replace`、且不是被取消的排队项。

    这是投影与事件口径来源校验共用的**唯一**判据（Call 3 P3-1/P4-1）。取消规则只有
    对 `message/queued` 才可达：`is_direct_user_input_event` 先要求 `USER_MESSAGE`，
    而取消标记只可能落在 `message/queued` 上，所以那边的取消分支不可达——取消规则的
    真实作用面是投影（`MESSAGE_QUEUED` 也在 `_USER_SOURCE_TYPES` 里）。
    """
    cancelled_ids = _cancelled_queue_ids(events)
    return [
        event
        for event in events
        if event.type in _USER_SOURCE_TYPES
        and isinstance(event.data.get("content"), str)
        and bool(event.data["content"].strip())
        and not event.data.get("injected_by")
        and not event.data.get("replace")
        and not _is_cancelled_queue_source(event, cancelled_ids)
    ]


def live_supersede_markers(events: list[SessionEvent]) -> list[tuple[int, int]]:
    """**真的发生过**的 supersede 标记，返回 `(目标 seq, 替换槽 seq)` 列表（#614①）。

    替换槽规则：目标与标记之间必须存在一条形状有效的用户事件（`user_source_events`），
    否则这次 supersede 实际没有发生过——替换排队后被取消是可达形态
    （`MESSAGE_QUEUED → MESSAGE_SUPERSEDED → QUEUE_CANCELLED`），标记作废、目标保持
    active。

    投影（`derive_protected_facts`）与事件口径来源校验（`is_direct_user_input_event`）
    共用这一份，判据不再两写（Call 3 P3-1）。**作用面只有这两处**：与
    `superseded_event_seqs` 刻意分开，那个是**纯解析**的 seq 集合，喂给
    `derive_messages_with_source_ranges` 算投影 shadow 区间——#614① 的替换槽规则
    **不作用于消息投影**（见 Call 5 P3：同一条作废标记在事实表里目标保持 active，
    在消息投影里仍按解析口径被 shadow、模型看不到目标原文）。修那条要动可见面、
    属语义变更，需先裁决；此处只如实标注两者的口径差。
    """
    event_by_seq = {event.seq: event for event in events}
    source_seqs = [event.seq for event in user_source_events(events)]
    markers: list[tuple[int, int]] = []
    for event in events:
        if event.type != MESSAGE_SUPERSEDED:
            continue
        target_seq = event.data.get("superseded_seq")
        if not isinstance(target_seq, int) or isinstance(target_seq, bool):
            continue
        target = event_by_seq.get(target_seq)
        if (
            target is None
            or target.type not in _USER_SOURCE_TYPES
            or target.data.get("injected_by")
            or event.seq <= target.seq
            or event.session_id != target.session_id
        ):
            continue
        replacement_index = bisect_right(source_seqs, target.seq)
        replacement_end = bisect_left(source_seqs, event.seq)
        if replacement_end <= replacement_index:
            # 替换槽空（#614①）：这次 supersede 没有实际发生，标记作废。
            continue
        markers.append((target_seq, source_seqs[replacement_end - 1]))
    return markers


def superseded_event_seqs(events: list[SessionEvent]) -> set[int]:
    """`message/superseded` 指向的 seq 集合（ADR-0030）。

    `derive_messages_with_source_ranges`（投影）与 `is_direct_user_input_event`
    （事件口径来源校验）共用这一份解析，避免两处各写一遍、日后判据漂移。
    """
    seqls: set[int] = set()
    for event in events:
        if event.type != MESSAGE_SUPERSEDED:
            continue
        raw = event.data.get("superseded_seq")
        # 一行坏数据只损失该行（存储模块契约）：非 int 就跳过并警告，不 brick 恢复。
        if isinstance(raw, int) and not isinstance(raw, bool):
            seqls.add(raw)
        elif raw is not None:
            logger.warning(
                "MESSAGE_SUPERSEDED.superseded_seq 形状非法（%s），忽略该条",
                type(raw).__name__,
            )
    return seqls

#: W-06（#350）：经 POST /progress/resolve 确认的手改指令所带的用户消息标记。
#: derive_protected_facts 把带此标记的 USER_MESSAGE 也收为 user_goal 来源——
#: "确认后才更新真相"要求确认指令在后续重读（重启/压缩后）中对模型可见。
#: 只认事件上的 origin 标记，不认文件文本；不改变既有"首条用户消息"口径。
PROGRESS_CONFIRM_ORIGIN = "progress_external_edit_confirm"
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
    """保护事实表的投影序列化（#346 注册值 → 模型可见注入体 / 摘要 §2）。

    #430（W-02.1）：只投影 ``status == "active"`` 的条目——superseded 是已被
    取代/撤权的旧真相，继续全额占用 ``protected_fact_token_budget`` 会让长
    会话的注入体单调膨胀直至预算闸永久 fail-closed（#411 审查 P1）。注册表
    语义不变：``derive_protected_facts`` 仍返回全量（撤销链 / fork 重映射 /
    service 校验消费全量列表）；撤权信息由 active 的 ``authorization_revocation``
    条目与事件流承载（OpenHands「抑制在视图层、历史不删」同构）。注入与摘要
    §2 两个消费方共用本函数，投影口径天然一致（旧摘要里的 §2 不回改、不参与
    跨代比对——compactor 恒现算）。
    """
    return json.dumps(
        [fact.to_dict() for fact in facts if fact.status == "active"],
        ensure_ascii=False,
        sort_keys=True,
    )


#: user_goal 注入值硬上限（#430 验收 2）：API 层单条消息可接受 100,000 字符，
#: 逐字全文进注册表投影会让保护事实注入体单独击穿独立预算（fail-closed 无
#: 自愈）。上限取 2000 = Pi 序列化截断（TOOL_RESULT_MAX_CHARS）同源、#415
#: 子代理结论 1500 字符头同家族。fact_id 仍按全文内容寻址（截断只改投影值，
#: 不改注册表身份，已持久化的 supersedes 引用不断链）；全文逐字活在转录；
#: ≤2000 的值经 #556 裁决 C 进摘要 §1（当前生效目标），超长的由本截断标记
#: 自带 source_event_id 回读指针（摘要 §1 不再承载超长全文）。
_USER_GOAL_VALUE_MAX_CHARS = 2000


def _bounded_goal_value(content: str, source_event_id: str) -> str:
    if not isinstance(content, str) or len(content) <= _USER_GOAL_VALUE_MAX_CHARS:
        return content
    return (
        content[:_USER_GOAL_VALUE_MAX_CHARS]
        + f"…［已截断：显示前{_USER_GOAL_VALUE_MAX_CHARS}字符，"
        + f"全文共{len(content)}字符，见首条用户消息 "
        + f"source_event_id={source_event_id}］"
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
    superseded_sources: set[str] = set()
    cancelled_ids = _cancelled_queue_ids(events)
    superseded_sources.update(
        event.event_id
        for event in events
        if _is_cancelled_queue_source(event, cancelled_ids)
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

    # 形状有效的用户来源只有一份判据（`user_source_events`，Call 3 P4-1）——
    # 取消规则、`replace` / `injected_by` 排除、非空 content 都在那一处。
    direct_user_events = user_source_events(events)
    first_user_source = direct_user_events[0] if direct_user_events else None
    user_goal_sources = {first_user_source.event_id} if first_user_source else set()
    # W-06（#350）：确认过的外部编辑指令（见 PROGRESS_CONFIRM_ORIGIN）以
    # user_goal 保护事实身份进入投影。首条用户消息口径保持不变；这里只是
    # 追加来源，不改变 supersede/取消等既有判定。
    user_goal_sources.update(
        event.event_id
        for event in direct_user_events
        if event.type == USER_MESSAGE
        and event.data.get("origin") == PROGRESS_CONFIRM_ORIGIN
    )
    # supersede 标记是否成立（替换槽非空，#614①）由 `live_supersede_markers` 判——
    # 与 `is_direct_user_input_event` 共用同一份，不再两写（Call 3 P3-1/P4-1）。
    for target_seq, replacement_seq in live_supersede_markers(events):
        target = event_by_seq[target_seq]
        superseded_sources.add(target.event_id)
        if target.type == USER_MESSAGE:
            replacement = next(
                (e for e in direct_user_events if e.seq == replacement_seq), None
            )
            if replacement is not None:
                user_goal_sources.add(replacement.event_id)
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
                    value=_bounded_goal_value(data["content"], event.event_id),
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


def derive_messages(
    events: list[SessionEvent], *, supports_vision: bool = False,
) -> list[AnyMessage]:
    """从事件序列投影出 messages 列表（`supports_vision` 语义见 `derive_messages_with_source_ranges`）。"""
    return [
        message
        for message, _source_range in derive_messages_with_source_ranges(
            events, supports_vision=supports_vision
        )
    ]


def referenced_attachment_ids(events: list[SessionEvent]) -> set[str]:
    """本会话 `user/message` 事件真实引用过的附件 id 集合（纯函数）。

    这是 #823 MM-02 补回的受控读回授权判据（PRD D5 / DSH `ATTACHMENT_NOT_REFERENCED`）：
    只有被某条用户消息引用过的 `attachment_id` 才允许读回；未引用（含上传后从未发送）
    一律 404。坏形状的引用条目按 `parse_image_refs` 的容错纪律逐条跳过。
    """
    referenced: set[str] = set()
    for event in events:
        if event.type != USER_MESSAGE:
            continue
        for ref in parse_image_refs(event.data.get("attachments")):
            referenced.add(ref.attachment_id)
    return referenced


def assert_attachment_referenced(
    events: list[SessionEvent], attachment_id: str,
) -> None:
    """附件读回的**唯一授权入口**（#934 M-05 / M-06）。

    谓词（`referenced_attachment_ids`）与判断收拢在同一模块——与 DSH 一手
    源码（`packages/api/session-controller/src/commands.ts @ d7432673`：
    谓词 `referencedImage` 在 :682 定义，消费点 `attachment()` 在 :391、
    :405 调用谓词）同构：谓词和消费点住同一模块边界。调用方（`web/` 传输层、
    将来的其他读路径）只调本函数，**不许**手写 `not in referenced_attachment_ids`
    式判断，否则两处语义会漂移。

    未被本会话某条 `user/message` 事件真实引用 → 抛 `AttachmentNotReferenced`
    （领域异常；调用方译为 HTTP 404，与"从未上传 / 属于别的会话"不可区分，
    不泄露存在性 —— PRD D5 / DSH `ATTACHMENT_NOT_REFERENCED` 语义）。

    纯函数：无副作用；M-05 的变异守卫（`tests/session/test_attachments_authz.py`）
    钉住"删掉下面的 `raise` 必须红"。
    """
    if attachment_id not in referenced_attachment_ids(events):
        raise AttachmentNotReferenced(
            f"attachment {attachment_id!r} 未被本会话的事件引用"
            "（不存在，或属于别的会话）"
        )


def derive_messages_with_source_ranges(
    events: list[SessionEvent],
    *,
    supports_vision: bool = False,
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

    #823 / MM-02（附件）：`user/message.data["attachments"]` 是**引用数组**。投影按
    `supports_vision` 分两条路：支持视觉 → 该 user 消息内容物化成
    `[text 块, 标准图片块…]`（`attachments.image_content_block`，**只带引用不含
    base64**，字节由请求装配层的 adapter 在发送前取回）；不支持视觉 → 原文本后追加
    固定占位符（`IMAGE_OMITTED_PLACEHOLDER`，不静默丢弃）。**无附件的消息逐字不变**
    （AC8）。默认 `supports_vision=False`，故 `derive_messages(events)` 的既有语义
    （纯文本逐字投影）不变。
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
    superseded_seqs = superseded_event_seqs(events)
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
            refs = parse_image_refs(event.data.get("attachments"))
            if refs:
                text = content if isinstance(content, str) else str(content)
                if supports_vision:
                    projected: str | list[dict[str, object]] = content_block_with_text(text, refs)
                else:
                    projected = text_with_omitted_images(text)
                messages.append((HumanMessage(content=projected), (event.seq, event.seq)))
            else:
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
    #: #823 / MM-02：附件引用数组（规范化后的 event-data 形状），投递时原样带进
    #: 新的 `user/message`，使排队/steer 的附图不静默丢失。
    attachments: list[dict[str, Any]] | None = None


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
                    attachments=_normalized_attachments(event),
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
                    attachments=_normalized_attachments(event),
                )
            )
    items.sort(key=lambda item: item.seq)
    return items


def _normalized_attachments(event: SessionEvent) -> list[dict[str, Any]] | None:
    """事件 data 的附件引用 → 规范化后的 dict 列表（坏条目跳过）；无则 None。"""
    refs = parse_image_refs(event.data.get("attachments"))
    if not refs:
        return None
    return [ref.model_dump() for ref in refs]


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


def derive_modified_file_paths(events: list[SessionEvent]) -> list[str]:
    """最近修改文件路径清单（W-31.5 #417，PRD §4.6 第 4 项）：只回路径。

    判据：`tool/call` 事件 `tool_name ∈ WRITE_TOOL_NAMES` 时取 `args["path"]`
    （str 且非空才收）；去重保序（首次出现顺序）。坏形状（缺 path / 非串 /
    args 非 dict / 未知工具）只损失该条，与 `collect_result_fields`
    （multiagent/provider.py，SubAgentResult.changed_files）的守卫同口径——
    那个函数只活在 delegate 回传里，本函数服务 build 注入；两者共用
    WRITE_TOOL_NAMES 一个常量、其余各走各的判据。

    与摘要第 8 节「文件清单」的口径差异（票面硬要求，禁止含糊）：§8 走
    `compactor._programmatic_summary_sections` 按 `_PATH_FIELDS` 从消息 dump
    提取——**读 + 改都进**、跨压缩累积；本函数**仅修改**。两处各钉用例
    （tests/session/test_derive_modified_paths.py 与
    tests/context/test_modified_paths_injection.py T7）。

    纯函数：只读事件流（**含被 bracket shadow 的事件**——压缩只 shadow 不
    删除，故跨压缩派生结果天然稳定，票面 AC「跨压缩不丢」的实现根基），
    无 wall-clock、无实例状态；重放 / 换实例同结果。只看 `tool/call` 单源，
    不做 MODEL_COMPLETED 兜底（票面定死；`collect_result_fields` 先例）。
    """
    paths: list[str] = []
    seen: set[str] = set()
    for event in events:
        if event.type != TOOL_CALL:
            continue
        if event.data.get("tool_name") not in WRITE_TOOL_NAMES:
            continue
        args = event.data.get("args")
        path = args.get("path") if isinstance(args, dict) else None
        if isinstance(path, str) and path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _projected_user_text(message: AnyMessage) -> str | None:
    """从投影出的 user 消息还原**事件原始 content**（#823 / MM-02 A7）。

    带图 user 消息的投影不再是纯文本：视觉下 content 是块列表
    （`[{"type":"text","text":原文}, {"type":"image",...}]`），非视觉下是
    ``原文 + "\\n" + 占位符``。两者都比不上 `event.data["content"]`，故
    `is_direct_user_input_event` / `latest_direct_user_input_event` 会漏掉带图
    消息（A7）。这里统一还原回原文，供它们按**事件原始 content**比对；无附件的
    纯文本消息逐字不变（还原即原文本身）。非 user 文本形态返回 None。
    """
    content = message.content
    if isinstance(content, str):
        suffix = f"\n{IMAGE_OMITTED_PLACEHOLDER}"
        if content == IMAGE_OMITTED_PLACEHOLDER:
            return ""
        if content.endswith(suffix):
            return content[: -len(suffix)]
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
            elif isinstance(block, str):
                return block
    return None


def is_direct_user_input_event(
    events: list[SessionEvent], event_id: str,
) -> bool:
    """Return whether an event is an active direct user message.

    判据以**事件事实**为主体（类型 / 内容形状 / 注入与取代标记），不依赖"这条消息这次
    有没有被注入给模型"。唯一一处投影比对（下条 `#823 / MM-02`）刻意做成"命中即通过、
    未命中不拒绝"，因此压缩（#663 P2）不会改变结论：

    - compaction 只改注入预算，不删原始 session entry，也不改"这是用户原话"这一
      事实。以模型可见投影为判据会让压缩后的 run 再也登记不了约束——投影里只剩
      summary，原文那条事件被判成"非直接输入"（#663 P2）。
      设计来源: pi 28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9
      packages/coding-agent/docs/sessions.md:40（"Compaction adds a summary and keeps
      recent messages. It does not delete the original session entries."）⇒ 来源靠
      原始事件 id，而不是当前注入文本；
    - `user/message(replace)` 是 compaction 摘要的替身，虽然是 `USER_MESSAGE`
      但**不是**用户说的话；`injected_by` 同理（与 `derive_protected_facts` 共用
      `user_source_events`，同一份判据不再两写）；
    - `steer/requested` / `message/queued` 是未投递请求，本函数只认 `USER_MESSAGE`；
    - 带 `input_request_id` 的是澄清答复，不是新的约束来源；
    - 被取代的用户消息不再是来源；本函数与 `derive_protected_facts` 共用
      `live_supersede_markers`（含 #614① 的替换槽规则——替换排队项被取消时 supersede
      未实际发生，目标仍有效）。这是**唯一的负向闸门**；
    - 投影一致比对（#823 / MM-02 A7）：事件若在消息投影里仍有**单事件来源范围**
      `(seq, seq)` 的 `HumanMessage`，用 `_projected_user_text` 把带图消息的投影
      （视觉为块列表、非视觉为原文 + 占位符后缀）还原回**事件原始 content** 再比对。
      此项是**防御性正向信号、当前不承重**：对普通活跃用户消息它常为 True，只是与负向
      闸门的 `¬S`（未被作废）**取并后恒不改变 OR 结果**（正向冗余项），故带图消息判 True
      并不依赖它；保留它是为闸门语义日后变动留一个正向兜底，不代表"带图消息靠它才不漏"，
      更不表示"本项恒为 False 或可删除"；
    - 本条只做正向补充：与负向闸门**取并**，比对命中即判真，未命中不据此拒绝（#663 P2
      的论证保留——压缩后单事件 bracket 的 summary 投影来源范围也是 `(seq, seq)`，当
      硬闸门就会退回 #663 P2 的 bug）；唯一的负向判定由 `live_supersede_markers`（含
      #614① 替换槽规则）承担；
    - C2 收紧：`(seq, seq)` 若命中一条 compaction summary（`message.name ==
      COMPACTION_SUMMARY_MESSAGE_NAME`）则**不计入**投影项。否则「已被 live-supersede
      的事件 s，其单事件 bracket 的 summary 文本恰好等于 s 的 content」会让投影项对一条
      已撤回的消息返回 True，与 #663 单边（False）分叉。

    设计意图（P4）：函数尾部是上述投影项与负向闸门的 **OR**。7 场景探针实测：在全部现实
    输入上，此 OR 与 #663 单边（只用 `live_supersede_markers`）**逐位相同**——投影项只在
    巧合输入（C2）上才会单独点亮，而 C2 已被上面的 summary 排除收紧。故投影项当前是一个
    **恒不改变 OR 结果的正向冗余项**：既不该被当成"带图消息的判别依据"而依赖，也不该因其
    冗余而删除；它的价值是防御性的（闸门语义若变动，正向项仍是兜底），不承载 #823 的行为。

    ⚠ **#614① 只覆盖「保护事实投影 + 本闸门」这两处口径**，**不含**消息投影
    （`derive_messages_with_source_ranges` 的 `superseded_ranges`，仍是纯解析的
    `superseded_event_seqs`）。后果：同一条作废标记下，事实表判目标 active、本闸门判
    True，可该目标原文在消息投影里仍被 shadow——模型看不到它，于是
    `latest_direct_user_input_event`（只从消息投影取源）也选不中它。这是**既有行为**，
    不是本票回归；修它要改可见面、属语义变更，须先裁决（Call 5 P3，本票只做披露）。

    取消失效的排队项不在本函数的作用面内：取消标记只落在 `message/queued` 上，而
    本函数先要求 `USER_MESSAGE`，两者无交集（Call 3 P3-2 证过那条分支不可达）。
    """
    event = next((item for item in events if item.event_id == event_id), None)
    if (
        event is None
        or event.type != USER_MESSAGE
        or event.data.get("injected_by")
        or event.data.get("replace")
        or "input_request_id" in event.data
        or not isinstance(event.data.get("content"), str)
        or not event.data["content"].strip()
    ):
        return False
    # #823 / MM-02（A7，本线）：投影一致比对，图片感知。带图 user 消息的投影是块
    # 列表（视觉）或带占位符后缀的字符串（非视觉），用 `_projected_user_text` 还原
    # 回**事件原始 content** 再比对。这是**防御性正向项、当前不承重**（设计意图见
    # docstring）：普通活跃用户消息上常为 True，但与下方 `¬S` 取并后**恒不改变结果**
    # （正向冗余项），并非恒 False，也不该被删除。
    #
    # #663 P2（main 侧）：这条比对**只能当正向信号，不能当拒绝依据**——compaction 把
    # 原文收进 summary 后，单事件 bracket 的 summary 投影来源范围恰好也是 `(seq, seq)`，
    # 被携带者只剩那条摘要、与原文逐字对不上；若拿它当硬闸门，压缩后的原文事件会被判成
    # "非直接输入"（正是 #663 P2 要修的 bug）。因此与作废标记判定**取并**：投影比对命中
    # ⇒ 是直接输入；命中不了（含压缩、含 #614① 空槽）不据此拒绝，负向闸门只由
    # `live_supersede_markers`（含 #614① 替换槽规则）承担。
    #
    # C2：投影项必须排除 compaction summary——否则「已被 live-supersede 的 s，其单事件
    # bracket 的 summary 恰等于 s 的 content」会让本项对一条已撤回的消息返回 True。
    return any(
        source_range == (event.seq, event.seq)
        and isinstance(message, HumanMessage)
        and message.name != COMPACTION_SUMMARY_MESSAGE_NAME
        and _projected_user_text(message) == event.data["content"]
        for message, source_range in derive_messages_with_source_ranges(events)
    ) or event.seq not in {
        target_seq for target_seq, _replacement_seq in live_supersede_markers(events)
    }


def latest_direct_user_input_event(
    events: list[SessionEvent], model_messages: list[AnyMessage],
) -> SessionEvent | None:
    """Find the newest direct user message still present in model input.

    候选闸门与 `is_direct_user_input_event` 的事件事实闸门**同口径**（#862 C2 + #911 P3-1）：

    - C2（#862）：候选投影项排除 compaction summary（`message.name ==
      COMPACTION_SUMMARY_MESSAGE_NAME`）。不排除时，「已被 live-supersede 的事件 s 的单事件
      bracket 的 summary 文本恰等于 s 的 content」会让 `(s, s)` 那项把一条**已作废**的
      消息重选为"最新直接用户输入"——summary 是投影替身、不是用户原话，不得据此复活已撤回
      消息。
    - P3-1（#911）：候选**事件**排除 `replace`（compaction 摘要替身）与 `input_request_id`
      （澄清答复）——事实闸门的两条既有排除。两者的可达性证据见候选循环内的注释与
      `tests/session/test_derive_direct_user_input.py` 的 P3-1 用例。
    """
    latest_direct_message = next(
        (
            event for event in reversed(events)
            if event.type == USER_MESSAGE
            and not event.data.get("injected_by")
            and isinstance(event.data.get("content"), str)
            and event.data["content"].strip()
        ),
        None,
    )
    # A resumed input-request answer is a decision about an existing question, not
    # a source constraint. Never fall back to an older user message in this run.
    if latest_direct_message is not None and isinstance(
        latest_direct_message.data.get("input_request_id"), str
    ):
        return None

    # #823 / MM-02（A7）：按**事件的原始 content**比对，而不是投影消息的 content
    # ——带图 user 消息的投影是块列表（视觉）或带占位符后缀的字符串（非视觉），
    # 旧写法（要求 message.content 是 str 且等于 event content）会漏掉它们。
    final_user_texts = {
        text
        for message in model_messages
        if isinstance(message, HumanMessage)
        for text in (_projected_user_text(message),)
        if text is not None
    }
    events_by_seq = {event.seq: event for event in events}
    candidates: list[SessionEvent] = []
    for message, source_range in derive_messages_with_source_ranges(events):
        if (
            source_range is None
            or source_range[0] != source_range[1]
            or not isinstance(message, HumanMessage)
            # C2 同源（#862）：排除 compaction summary，根因叙述见本函数 docstring。
            or message.name == COMPACTION_SUMMARY_MESSAGE_NAME
        ):
            continue
        text = _projected_user_text(message)
        if text is None or text not in final_user_texts:
            continue
        event = events_by_seq.get(source_range[0])
        # P3-1（#911）：`replace`（compaction 摘要替身）与 `input_request_id`（澄清答复）
        # 都不是"新的约束来源"，在事实闸门里被排除，此处同样排除。
        # 口径说明（仅就 `input_request_id` 而言）：该键**存在即排除**（`in`），非逐字取 str 后判 ——
        # 非 str 的畸形载荷（如 `{"input_request_id": 123}`）同样被排除，两处保持一致。
        # `replace` 则与事实闸门同为**真值判定**（`event.data.get("replace")`）。
        # 另注意本函数开头的 `latest_direct_message` 早退守卫用的是 `isinstance(..., str)`（既有写法）：
        # 同一函数内三种写法并存，与上面两条都不是同一条判据。
        #
        # 两类事件都是真 `HumanMessage`、投影文本与自身 content 逐字相等 ⇒ 天然满足候选
        # 的「单事件来源范围 + 投影文本 == 事件 content」，此前只有 C2（summary 命名）那一支
        # 被拦下。分叉可达性（对照 `tests/session/test_derive_direct_user_input.py` 的 P3-1 用例）：
        #   * `input_request_id`：**可达**——答复之后又有 user 消息、而那条后续消息被
        #     `message/superseded` 取代（整轮 shadow）时，投影里 seq 最高的可见
        #     `HumanMessage` 正是答复本身（函数开头的 `latest_direct_message` 早退守卫只在
        #     答复**就是最后一条**事件时才触发）。此时闸门把澄清答复选成"最新直接用户输入"。
        #   * `replace=True`：**无生产写点**（全 `src/` 与全部 git 历史零写点）。手工构造只见于
        #     `tests/context/test_constraint_registration_context.py:217` 与本票新增的两处用例。
        #     `context/builder.py` 只落 START / CONTEXT_COMPACTED / END，不落
        #     `session/event.py` 登记的那条替身）。但"不可达"的理由**不是**它被 shadow——
        #     投影只 shadow `source_seq_start..source_seq_end`，替身写在 SUMMARY 之后、
        #     区间之外 ⇒ 旧日志 / 外部导入里一旦出现该形状就会被投影成 `(seq, seq)` 并被
        #     选中。按票面"同源修法"一并补齐。
        if (
            event is None
            or event.type != USER_MESSAGE
            or event.data.get("injected_by")
            or event.data.get("replace")
            or "input_request_id" in event.data
            or not isinstance(event.data.get("content"), str)
            or not event.data["content"].strip()
            or event.data["content"] != text
        ):
            continue
        candidates.append(event)
    return max(candidates, key=lambda event: event.seq, default=None)
