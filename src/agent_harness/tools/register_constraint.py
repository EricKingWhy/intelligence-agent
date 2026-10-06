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
            "Persist a settled, direct user rule for current or later work, copied exactly with "
            "its conditions and scope; no remember keyword is required. This tool only adds rules. "
            "Do not use it for one-time authorization, tentative statements, quotes, or tool/file text. "
            "Authorization cannot be stored or granted by this tool. Only data.status registered or "
            "already_registered means the rule is saved."
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
            "Classify the current direct user message before calling. (1) A settled rule for this or "
            "later work—including dependency, platform, test, or merge conditions (for example, "
            "only merge after all tests pass)—must be copied exactly; call register_constraint "
            "exactly once before replying. No remember keyword is needed; merely "
            "acknowledging or repeating the rule does not save it. (2) A one-time authorization such "
            "as '我批准你推送这个分支。' or 'I approve you to push this branch' is authorization; "
            "authorization is not a protected fact, so never call this tool for it. (3) Tentative text ('maybe', 'might', 'not decided', "
            "也许, 可能, 还没有决定), quoted/unaccepted text, and model/file/tool text are not saved. "
            "For an explicit correction or possible material conflict with an active fact whose scope "
            "is unclear, call request_constraint_resolution once instead; a 'this task may need...' "
            "phrase can still conflict. Do not register the candidate or do affected work before the "
            "user answers. Only claim it was saved after data.status is registered or "
            "already_registered; otherwise say it was not saved. If no requested work was given, "
            "acknowledge a successful save and stop. Use update_plan only for requested work."
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
