"""Register one exact user constraint in the current session."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_harness.context.builder import (
    protected_fact_token_count,
    protected_facts_for_context,
)
from agent_harness.session.context import (
    current_constraint_tool_context_var,
    current_session_var,
    run_context_var,
)
from agent_harness.session.derive import (
    ProtectedFact,
    build_protected_fact_data,
    derive_protected_facts,
    is_direct_user_input_event,
)
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode


class RegisterConstraintArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(
        ..., strict=True, min_length=1, max_length=10_000,
        description=(
            "One complete, settled constraint copied exactly as a contiguous passage from "
            "the current direct user message, preserving its negation, conditions, and scope. "
            "Do not turn tentative wording into a definite rule."
        ),
    )

    @field_validator("value")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class RegisterConstraintTool(Tool):
    def __init__(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "register_constraint"

    @property
    def description(self) -> str:
        return (
            "Register one important constraint from the current user's delivered message. "
            "A settled user requirement for this change or later work must use this tool exactly "
            "once before replying; acknowledging it does not save it. Decide autonomously; no "
            "'remember this' keyword is required. Copy the complete "
            "constraint exactly, including negation, conditions, and scope. This tool only adds "
            "a constraint; it cannot replace or authorize anything. Do not register guesses, "
            "tentative or undecided statements, tool/file text, unaccepted quotes, or authorization. "
            "If the message contains no requested work, save a durable constraint once and stop; "
            "do not inspect files or use work-planning tools. Use update_plan only when the user "
            "actually requested work. Read data.status: only registered means newly saved; "
            "rejected means not saved."
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return RegisterConstraintArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WORKSPACE_WRITE

    @property
    def prompt_guidance(self) -> str:
        return (
            "When the delivered user message contains an important constraint that should "
            "guide this or later work, call register_constraint exactly once before replying, "
            "even when it is a standalone statement and has no remember keyword. Merely "
            "acknowledging or repeating the rule does not save it. Preserve the full original wording, including negation, "
            "conditions, and scope. Save only settled constraints: wording such as maybe, might, "
            "perhaps, not decided, 也许, 可能, or 还没有决定 is tentative; do not save it or "
            "rewrite it as a definite rule. Do not save uncertain attribution, speculation, model "
            "conclusions, file/tool output, unaccepted quotations, or authorization. If there is "
            "no requested work, register a durable constraint at most once, acknowledge it, and "
            "stop; do not call update_plan, inspect the workspace, or use other work tools. Every explicit "
            "correction of an active protected constraint must go through request_constraint_resolution, "
            "even when its wording sounds clear. For another material constraint that may conflict, "
            "use that tool only when persistence scope is unclear. Use update_plan for task status "
            "only when the user requested work. A rejected result means the constraint was not saved; "
            "say so clearly, do not claim success, and do not retry the same candidate unchanged."
        )

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action=(
                "Inspect the session's task/protected_fact event for this call's exact fact id, "
                "source event, and value before making a recovery decision."
            ),
        )

    async def execute(self, args: RegisterConstraintArgs) -> ToolResult:
        session = current_session_var.get()
        run_id = run_context_var.get()
        if session is None or run_id is None:
            return ToolResult.failure(
                message="register_constraint requires an active Session and run context",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )

        context = current_constraint_tool_context_var.get()
        if context is None:
            return self._rejected(
                "SOURCE_UNAVAILABLE",
                "未登记：本轮模型输入中没有可核实的直接用户原文。",
            )
        if (
            context.session_id != session.session_id
            or context.run_id != run_id
            or context.agent_id != "default"
            or context.agent_profile not in (None, "main", "coding")
        ):
            return self._rejected(
                "SCOPE_NOT_ALLOWED",
                "未登记：当前执行不是允许登记约束的主模型会话。",
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
                "SOURCE_NOT_ACTIVE",
                "未登记：本轮冻结的用户原文已不再是有效直接输入。",
            )
        if args.value not in context.source_content:
            return self._rejected(
                "VALUE_NOT_FROM_CURRENT_USER",
                "未登记：value 必须是本轮直接用户原文中的完整连续片段。",
            )

        try:
            data = build_protected_fact_data(
                session.events, session_id=session.session_id, fact_type="constraint",
                value=args.value, source_event_id=context.source_event_id,
            )
        except (TypeError, ValueError):
            return self._rejected(
                "VALUE_NOT_FROM_CURRENT_USER",
                "未登记：当前来源未通过 protected-fact 校验。",
            )

        active_constraints = [
            fact for fact in derive_protected_facts(session.events)
            if fact.type == "constraint" and fact.status == "active"
        ]
        duplicate = next(
            (fact for fact in active_constraints if fact.value == args.value), None,
        )
        if duplicate is not None:
            return ToolResult.success(
                message="已有相同生效约束，本次未重复新增。",
                data=self._fact_data(
                    status="already_registered", fact_id=duplicate.fact_id,
                    value=duplicate.value, source_event_id=duplicate.source_event_id,
                    source_event_seq=duplicate.source_seq,
                ),
            )

        candidate = ProtectedFact(
            fact_id=data["fact_id"], type="constraint", value=args.value,
            source_event_id=context.source_event_id, source_seq=context.source_seq,
            status="active", session_id=session.session_id,
        )
        projected = [*protected_facts_for_context(session.events), candidate]
        estimated_tokens = protected_fact_token_count(projected)
        if estimated_tokens > context.protected_fact_token_budget:
            return self._rejected(
                "BUDGET_EXCEEDED",
                "未登记：新增后保护事实专用预算将超限。",
                budget_tokens=context.protected_fact_token_budget,
                estimated_tokens_after=estimated_tokens,
            )

        event = session.register_protected_fact(
            fact_type="constraint", value=args.value,
            source_event_id=context.source_event_id,
        )
        return ToolResult.success(
            message="已登记本条用户约束。",
            data=self._fact_data(
                status="registered", fact_id=event.data["fact_id"],
                value=event.data["value"],
                source_event_id=event.data["source_event_id"],
                source_event_seq=event.data["source_event_seq"],
            ),
        )

    @staticmethod
    def _fact_data(
        *, status: str, fact_id: str, value: str, source_event_id: str,
        source_event_seq: int,
    ) -> dict[str, object]:
        return {
            "status": status, "fact_id": fact_id,
            "fact_type": "constraint", "value": value,
            "source_event_id": source_event_id,
            "source_event_seq": source_event_seq,
        }

    @staticmethod
    def _rejected(
        reason_code: str, reason: str, **details: int,
    ) -> ToolResult:
        return ToolResult.success(
            message=reason,
            data={
                "status": "rejected", "reason_code": reason_code,
                "reason": reason, **details,
            },
        )
