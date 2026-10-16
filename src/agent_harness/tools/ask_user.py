"""#1204：通用版运行中提问 `ask_user`——对话流内嵌卡片的工具端。

形态与参数抄 Claude Code `AskUserQuestion`（一手
https://code.claude.com/docs/en/agent-sdk/user-input）：`questions[]` 1–4 题、
每题 options 2–4 个、`header` ≤12 字符（前端作 chip）。`recommended`（推荐选项
下标，0 基）为本仓自创——源自 dsh 效果图"推荐"标，越界即参数校验拒绝。

与窄场景 `request_constraint_resolution` 同源：执行期落一条持久化
`USER_INPUT_REQUESTED`（`data.kind == "ask_user"`）并暂停 run；准入（deadline /
预算余量闸、委派子会话禁用）与「每 run 去重」复用 `user_input_common` 的共享
语义，唯不校验"来源须是可核实的直接用户原文"。
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_harness.session.context import (
    current_constraint_tool_context_var,
    current_session_var,
    run_context_var,
)
from agent_harness.session.event import USER_INPUT_REQUESTED
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode
from agent_harness.tools.user_input_common import (
    SCOPE_REJECTION_NOT_ALLOWED,
    SCOPE_REJECTION_SOURCE_UNAVAILABLE,
    admission_rejection_message,
    pending_user_input_request,
    rejected_result,
    scope_rejection_code,
)


class AskOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[
        str,
        Field(
            ..., strict=True, min_length=1,
            description="The option label shown on a selectable card (verbatim answer text).",
        ),
    ]
    description: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional short explanation of what choosing this option means.",
        ),
    ]

    @field_validator("label")
    @classmethod
    def _label_not_blank(cls, value: str) -> str:
        # #1204 审查 P1：纯空白 label 被前端投影整条丢弃（projection.ts `!label.trim()`
        # ⇒ parsedOptions 长度不符 ⇒ 整卡不投影），但 run 已暂停 ⇒ 无 UI 出口卡死。
        # 参数校验直接拒绝空白标签（不 strip 原值——正常 label 仍逐字作答案文本）。
        if not value.strip():
            raise ValueError("option label must not be blank")
        return value


class AskQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str,
        Field(
            ..., strict=True, min_length=1,
            description="The question to ask the user, clear and self-contained.",
        ),
    ]
    header: Annotated[
        str,
        Field(
            ..., strict=True, min_length=1, max_length=12,
            description="Very short label (max 12 chars) for the card chip, e.g. 'Scope'.",
        ),
    ]
    options: Annotated[
        list[AskOption],
        Field(
            ..., min_length=2, max_length=4,
            description="2 to 4 mutually exclusive choices for this question.",
        ),
    ]
    recommended: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Optional 0-based index of the recommended option; must be within "
                "range. The frontend renders a 'recommended' badge on it."
            ),
        ),
    ]

    @model_validator(mode="after")
    def _recommended_within_options(self) -> AskQuestion:
        if self.recommended is not None and not (
            0 <= self.recommended < len(self.options)
        ):
            raise ValueError(
                "recommended must be a valid 0-based index into options"
            )
        return self


class AskUserArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: Annotated[
        list[AskQuestion],
        Field(
            ..., min_length=1, max_length=4,
            description="1 to 4 questions to ask the user in this single card.",
        ),
    ]


class AskUserTool(Tool):
    def __init__(self, timeout_seconds: int = 300) -> None:
        # #1224：随请求落盘的超时档（秒）——与 `ask_user_timeout_message` 渲染的注入
        # 消息（`minutes = max(1, timeout_seconds // 60)`）同源，前端据此渲染一致的数字。
        # 缺省 300 **仅供直接构造 / 测试**：生产装配（`assembly.py` 两处）恒注入
        # `Settings.ask_user_question_timeout`，故此处不随档位配置联动（#1220 复审 B3）。
        self._timeout_seconds = int(timeout_seconds)

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "Pause this run by creating a persisted in-conversation question card and wait "
            "for the user's answer. Use it whenever a decision genuinely belongs to the user "
            "and you cannot resolve it from context or by reasonable assumption. Provide 1 to 4 "
            "questions; each question has a short header (max 12 chars) and 2 to 4 options. "
            "This is not the same as an approval gate; do not use it to confirm an ordinary step "
            "you are already authorized to take, and do not use a prose question alone."
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return AskUserArgs

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
            "Call this tool when a real decision is the user's to make and you cannot infer it "
            "from the conversation or any active constraint. Prefer it over guessing or over "
            "stopping to ask in prose. Ask at most once per run: group related decisions into "
            "one card of up to 4 questions, each with 2 to 4 concrete options. Keep each header "
            "to 12 characters or fewer. Set recommended only when one option is clearly best. "
            "While the card is pending, do not use another tool and do not do the affected work; "
            "wait for the answer. If asking returns rejected, do not guess or retry with "
            "rewritten options; continue with a stated reasonable assumption or stop."
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

    async def execute(self, args: AskUserArgs) -> ToolResult:
        session = current_session_var.get()
        run_id = run_context_var.get()
        if session is None or run_id is None:
            return ToolResult.failure(
                message="ask_user requires an active Session and run context",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        context = current_constraint_tool_context_var.get()
        scope_code = scope_rejection_code(session, run_id, context)
        if scope_code == SCOPE_REJECTION_SOURCE_UNAVAILABLE:
            return rejected_result(
                scope_code,
                "当前模型输入中没有可归因的 run 上下文，未发起问题。",
            )
        if scope_code == SCOPE_REJECTION_NOT_ALLOWED:
            return rejected_result(
                scope_code, "当前执行范围不能发起提问。",
            )
        assert context is not None
        if context.user_input_request_rejection_code is not None:
            code = context.user_input_request_rejection_code
            return rejected_result(code, admission_rejection_message(code))

        pending = pending_user_input_request(session.events, run_id=run_id)
        if pending is not None:
            return ToolResult.success(
                message="本 run 已有待处理的提问；将恢复该问题，不会创建第二个请求。",
                data={
                    "status": "pending",
                    "request_id": pending.data["request_id"],
                },
            )

        questions = [question.model_dump() for question in args.questions]
        request_id = str(uuid4())
        payload = {
            "request_id": request_id,
            "kind": "ask_user",
            "run_id": run_id,
            "questions": questions,
            # #1224：超时档随请求落盘（前端直接读，不另开配置端点）。数值与后端注入
            # 消息同源（`minutes = max(1, timeout_seconds // 60)`，见 :131）。
            "timeout_seconds": self._timeout_seconds,
        }
        return ToolResult.success(
            message="提问已保存；当前 run 将暂停，等待你的回答后继续。",
            data={
                "status": "requested",
                "request_id": request_id,
                "questions": questions,
            },
            pending_events=[(USER_INPUT_REQUESTED, payload)],
        )
