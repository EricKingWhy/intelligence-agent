"""SessionEvent：Agent 交互历史的 append-only 类型化事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

#: RuntimeEvent 信封版本（SDD 03 §3）：SSE 帧 / JSONL 行 / AgentEvent 统一携带，
#: 客户端据此判断 schema 兼容。当前冻结为 ``runtime_event/v1``。
RUNTIME_EVENT_SCHEMA_VERSION = "runtime_event/v1"

# ── Event vocabulary V1（Phase 1 子集） ──
# ── + Phase 9 流式信号（MODEL_STARTED / MODEL_DELTA） ──
# ── + Phase 4 恢复信号（OPERATION_RECONCILE_REQUIRED） ──

SESSION_STARTED = "session/started"
SESSION_RESUMED = "session/resumed"
# Phase 14（ADR-0017 决策 8）：只落 child 文件的 provenance 事件
SESSION_FORKED = "session/forked"
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
MEMORY_DEGRADED = "memory/degraded"
# ── + Phase 12 Reliability 信号（同错熔断 + 模型 fallback，ADR-0014） ──
TOOL_FAILURE_GUARD = "tool/failure-guard"
MODEL_FALLBACK = "model/fallback"
# ── + Phase 13 Multi-Agent（delegation 白盒事件，ADR-0015 决策 8） ──
AGENT_DELEGATION_STARTED = "agent/delegation-started"
AGENT_DELEGATION_FINISHED = "agent/delegation-finished"
# ── + Streaming UI 生产级改造（reasoning 事件族 + 工具输出流，ADR-0016） ──
# 协作约束（Phase 14 并行开发约定）：session/event.py 双方只做加法改动——
# 合帧文本增量启用新类型 text/delta（规格 02 §7.4 text 事件族命名），
# MODEL_DELTA 保持 stream-only 词汇原样（运行时不再发射，作为 legacy 保留）。
REASONING_STARTED = "reasoning/started"
REASONING_DELTA = "reasoning/delta"
REASONING_COMPLETED = "reasoning/completed"
REASONING_INTERRUPTED = "reasoning/interrupted"
# ── + Phase 5 Composer（SDD 06 Phase 5）：交互式审批——run 在审批关卡暂停，
# 向前端广播 tool/approval-requested（durable）；前端 /approve 后续解。 ──
# ── + Batch 5.1：permission/resolved 补审计 trail（03 §9 PermissionResolvedData）。
# 前端据 approval_id 把 requested 与 resolved 配对，JSONL 可回放完整决策历史。 ──
TOOL_APPROVAL_REQUESTED = "tool/approval-requested"
PERMISSION_RESOLVED = "permission/resolved"
TOOL_OUTPUT_DELTA = "tool/output_delta"
TEXT_DELTA = "text/delta"

# Durable event vocabulary — these are the ONLY types that may appear in the
# append-only SessionEvent log (via Session.append). Anything in STREAM_ONLY_TYPES
# below is an ephemeral streaming signal (Phase 9 AgentEvent) and MUST NOT be
# persisted (invariant #4: Event ≠ Diagnostic Log).
# ADR-0016 §3.1 修订：合帧后的 text/delta 与 reasoning/tool 输出增量是运行事实
# （规格 02 §9.2 "meaningful raw/coalesced runtime chunks"），转 durable——S19
# 禁止的是 per-token 行，不是合帧 chunk 本身。
EVENT_TYPES: frozenset[str] = frozenset(
    {
        SESSION_STARTED,
        SESSION_RESUMED,
        SESSION_FORKED,
        RUN_STARTED,
        RUN_COMPLETED,
        RUN_FAILED,
        USER_MESSAGE,
        MODEL_COMPLETED,
        MODEL_FAILED,
        TOOL_CALL,
        TOOL_RESULT,
        TOOL_OUTPUT_DELTA,
        TEXT_DELTA,
        OPERATION_RECONCILE_REQUIRED,
        ARTIFACT_CREATED,
        CONTEXT_COMPACTED,
        MEMORY_DEGRADED,
        TOOL_FAILURE_GUARD,
        MODEL_FALLBACK,
        AGENT_DELEGATION_STARTED,
        AGENT_DELEGATION_FINISHED,
        REASONING_STARTED,
        REASONING_DELTA,
        REASONING_COMPLETED,
        REASONING_INTERRUPTED,
        TOOL_APPROVAL_REQUESTED,
        PERMISSION_RESOLVED,
    }
)

# Ephemeral streaming-only types — produced by run_stream() as AgentEvents, never
# appended to the durable log. Listed here so the full event vocabulary is in one
# place; NOT part of EVENT_TYPES, and Session.append must reject them.
# ADR-0016：model/delta 词汇保留但运行时不再发射（由 text/delta 承接，见上）。
STREAM_ONLY_TYPES: frozenset[str] = frozenset({MODEL_STARTED, MODEL_DELTA})


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
    # 流式块标识（ADR-0016 §3.2，规格 02 §5 的语义等价物）：同一段思考/文本/
    # 输出的 delta 共享同一 block_id，post-tool 新段取新 id。None = 不适用
    # （文本由既有 step/turn 语义聚合，工具输出按 data.tool_call_id 聚合）。
    block_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    source_event_ids: list[str] | None = None
    # RuntimeEvent 信封字段（SDD 03 §3，Phase 2 加法）：
    # - schema_version：信封版本，恒 RUNTIME_EVENT_SCHEMA_VERSION；
    # - capability：事件归属的 capability id（Phase 6 capability-aware 投影注入点），
    #   当前 Runtime 不发射归属 → 运行时恒 None；None 时 to_dict 省略（同 block_id 模式）。
    # SessionEvent 恒 durable（append 已校验词汇表），durability 不存为字段——
    # 序列化路径（to_dict / SSE）固定写 "durable"。
    schema_version: str = RUNTIME_EVENT_SCHEMA_VERSION
    capability: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSONL 行字典（None 字段省略以保持行紧凑）。"""
        result: dict[str, Any] = {
            "event_id": self.event_id,
            "seq": self.seq,
            "time": self.time,
            "type": self.type,
            "session_id": self.session_id,
            "schema_version": self.schema_version,
        }
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        if self.step_id is not None:
            result["step_id"] = self.step_id
        if self.block_id is not None:
            result["block_id"] = self.block_id
        if self.data:
            result["data"] = self.data
        if self.source_event_ids is not None:
            result["source_event_ids"] = self.source_event_ids
        if self.capability is not None:
            result["capability"] = self.capability
        return result

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SessionEvent:
        """从 JSONL 解析出的字典重建 SessionEvent。

        向后兼容：旧 JSONL 行缺 ``schema_version`` 时回落当前版本（加法字段）。
        """
        return cls(
            event_id=raw.get("event_id", _new_event_id()),
            seq=raw.get("seq", 0),
            time=raw.get("time", _utc_now_iso()),
            type=raw.get("type", ""),
            session_id=raw.get("session_id", ""),
            run_id=raw.get("run_id"),
            agent_id=raw.get("agent_id"),
            step_id=raw.get("step_id"),
            block_id=raw.get("block_id"),
            data=raw.get("data", {}),
            source_event_ids=raw.get("source_event_ids"),
            schema_version=raw.get("schema_version", RUNTIME_EVENT_SCHEMA_VERSION),
            capability=raw.get("capability"),
        )
