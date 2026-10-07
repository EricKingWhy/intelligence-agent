"""Ask the user to resolve one uncertain protected-constraint conflict."""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_harness.session.context import (
    current_constraint_tool_context_var,
    current_session_var,
    run_context_var,
)
from agent_harness.session.derive import (
    derive_protected_facts,
    is_direct_user_input_event,
)
from agent_harness.session.event import USER_INPUT_REQUESTED, USER_MESSAGE
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode

_CHOICES = (
    {"id": "replace_persistently", "label": "永久替换旧约束"},
    {"id": "current_task_only", "label": "仅当前任务采用新约束"},
    {"id": "keep_existing", "label": "保留旧约束并忽略本次要求"},
    {"id": "custom", "label": "自定义回复"},
)

_ADMISSION_REJECTION_MESSAGES = {
    "DEADLINE_CONFIGURED": (
        "当前 run 或 session 配置了绝对 deadline，不能等待用户澄清；未创建问题。"
    ),
    "RESUME_UNAVAILABLE": (
        "剩余预算不足以保证收到回答后同一 run 能继续；未创建问题。"
    ),
}


class RequestConstraintResolutionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(
        ..., strict=True, min_length=1, max_length=128,
        description=(
            "Copy the exact old active constraint id being reconsidered, "
            "from the injected active protected constraints; never use the new proposal here, or guess/synthesize an id."
        ),
    )
    candidate: str = Field(
        ..., strict=True, min_length=1, max_length=10_000,
        description=(
            "Copy only the new proposed constraint as verbatim contiguous text from the "
            "current direct user message. Never use the old constraint as the candidate. Do not paraphrase or combine with the old constraint, "
            "or add text the user did not write. Omit correction framing such as 'I correct this "
            "rule:' or '我更正这条长期约束：'; include only the proposed rule itself."
        ),
    )

    @field_validator("candidate")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate must not be blank")
        return value


#: 给 `register_constraint` 的**增量**转接句：什么情况该从"登记约束"转向澄清。
#: 不重复本工具 guidance 已经说过的内容（那份全文经 `tool:request_constraint_resolution`
#: 独立进 system prompt）；两边都写一遍 = 每次请求付两份字符（Call 3 P2-1）。
#: 装配层只在 resolver 真在册时注入它（`assembly._build_tooling`）。
REGISTER_CONSTRAINT_HANDOFF = (
    "For an explicit correction or possible material conflict with an active fact "
    "whose scope is unclear, call request_constraint_resolution once instead; "
    "a 'this task may need...' phrase can still conflict."
)


class RequestConstraintResolutionTool(Tool):
    def __init__(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "request_constraint_resolution"

    @property
    def description(self) -> str:
        return (
            "Pause this run by creating a persisted choice card for an explicit correction or a "
            "possible material conflict with an active protected constraint. Use for every explicit "
            "correction, even when its persistence scope is clear, and for possible conflicts whose "
            "intent or persistence scope is unclear. Ordinary conversation and non-conflicting "
            "additions do not need a card. The argument roles are fixed: fact_id is the old active "
            "constraint id; candidate is the new proposed text from the current direct user message. "
            "A prose question alone does not pause this run."
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return RequestConstraintResolutionArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def batch_exclusive(self) -> bool:
        return True

    @property
    def prompt_guidance(self) -> str:
        return (
            "Call this tool exactly once for an explicit correction of an active constraint, or when "
            "a possible material requirement conflicts with an active constraint and its intent/scope "
            "is unclear. This includes 'this task may need...' even when the possible requirement is "
            "phrased as maybe or might; do not infer task-only scope from 'this task' phrasing alone "
            "and do not wait for the user to confirm the conflict. Keep argument roles distinct: "
            "fact_id is the old active constraint id; candidate is only the new proposed text from "
            "the direct message. Never swap them or put the old rule in candidate. Omit correction "
            "framing such as 'I correct this rule:'. Before the answer, do not register the candidate "
            "or do affected work. A pending card does not authorize the conflicting action: wait for "
            "the user and do not use another tool until they answer. If rejected, do not guess, retry, or substitute a prose question; report that no card was created and stop before affected work. "
            "Apply only the selected scope; the answer to this choice is not a new constraint to register. "
            "If there is no concrete work request, acknowledge and stop."
        )

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action=(
                "Inspect the durable user/input-requested event and the matching user/message "
                "answer before resolving this operation."
            ),
        )

    async def execute(self, args: RequestConstraintResolutionArgs) -> ToolResult:
        session = current_session_var.get()
        run_id = run_context_var.get()
        if session is None or run_id is None:
            return ToolResult.failure(
                message="request_constraint_resolution requires an active Session and run context",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        context = current_constraint_tool_context_var.get()
        if context is None:
            return self._rejected(
                "SOURCE_UNAVAILABLE",
                "当前模型输入中没有可核实的直接用户原文，未发起问题。",
            )
        if (
            context.session_id != session.session_id
            or context.run_id != run_id
            or context.agent_id != "default"
            or context.agent_profile not in (None, "main", "coding")
        ):
            return self._rejected(
                "SCOPE_NOT_ALLOWED", "当前执行范围不能发起约束澄清。",
            )

        source = next(
            (event for event in session.events if event.event_id == context.source_event_id),
            None,
        )
        if (
            source is None
            or source.seq != context.source_seq
            or source.data.get("content") != context.source_content
            or not is_direct_user_input_event(session.events, context.source_event_id)
        ):
            return self._rejected(
                "SOURCE_NOT_ACTIVE", "本轮用户原文已不再有效，未发起问题。",
            )
        if args.candidate not in context.source_content:
            return self._rejected(
                "VALUE_NOT_FROM_CURRENT_USER",
                "新约束必须是本轮直接用户原文中的完整连续片段。",
            )

        fact = next(
            (
                item for item in derive_protected_facts(session.events)
                if item.fact_id == args.fact_id
                and item.type == "constraint"
                and item.status == "active"
            ),
            None,
        )
        if fact is None or not isinstance(fact.value, str):
            return self._rejected(
                "FACT_NOT_ACTIVE", "指定的旧约束不再生效，未发起问题。",
            )
        if context.user_input_request_rejection_code is not None:
            code = context.user_input_request_rejection_code
            return self._rejected(
                code,
                _ADMISSION_REJECTION_MESSAGES.get(
                    code, "无法保证同一 run 在收到回答后继续；未创建问题。"
                ),
            )

        answered = {
            event.data.get("input_request_id")
            for event in session.events
            if event.type == USER_MESSAGE
            and isinstance(event.data.get("input_request_id"), str)
        }
        pending = next(
            (
                event for event in reversed(session.events)
                if event.type == USER_INPUT_REQUESTED
                and event.run_id == run_id
                and isinstance(event.data.get("request_id"), str)
                and event.data["request_id"] not in answered
            ),
            None,
        )
        if pending is not None:
            return ToolResult.success(
                message="本 run 已有待处理的约束问题；将恢复该问题，不会创建第二个请求。",
                data={
                    "status": "pending",
                    "request_id": pending.data["request_id"],
                },
            )

        request_id = str(uuid4())
        payload = {
            "request_id": request_id,
            "kind": "protected_fact_conflict",
            "run_id": run_id,
            "source_event_id": source.event_id,
            "source_event_seq": source.seq,
            "fact_id": fact.fact_id,
            "old_value": fact.value,
            "candidate": args.candidate,
            "question": "新要求与一条生效约束可能冲突。请选择如何处理。",
            "choices": [dict(choice) for choice in _CHOICES],
        }
        return ToolResult.success(
            message="约束问题已保存；当前 run 将暂停，等待你的选择后继续。",
            data={
                "status": "requested",
                "request_id": request_id,
                "question": payload["question"],
                "choices": payload["choices"],
                "old_value": fact.value,
                "candidate": args.candidate,
            },
            pending_events=[(USER_INPUT_REQUESTED, payload)],
        )

    @staticmethod
    def _rejected(reason_code: str, reason: str) -> ToolResult:
        return ToolResult.success(
            message=reason,
            data={
                "status": "rejected",
                "reason_code": reason_code,
                "reason": reason,
            },
        )
