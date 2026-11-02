"""#1356 方案 2：`submit_plan`——计划提交与审批（对标 Claude Code `ExitPlanMode`）。

语义与 `update_plan` **正交、不合并**（票面 §3.4）：

| | `submit_plan` | `update_plan` |
|---|---|---|
| 时点 | 事前，一次性 | 事中，反复整表覆盖 |
| 目的 | 请求**授权** | 汇报**进度** |
| 是否暂停 run | 是（`USER_INPUT_REQUESTED`，等裁决） | 否（追加事件） |
| 事件 | `user/input-requested`（`kind="plan_approval"`） | `task/plan_updated` |

暂停机制逐字复用 `ask_user`：`pending_events=[(USER_INPUT_REQUESTED, payload)]`
由 Executor 的 durable-先行路径落盘并暂停；准入（deadline / 预算余量 / 委派子会话
禁用）与「每 run 单请求去重」复用 `user_input_common`。**不发** `task/plan_updated`
——否则 `ConversationState.plan` 的进度清单投影被计划正文污染。

与 #1193 的边界：批准后的切档发生在**用户显式点击"批准"之后**（用户动作的一部分，
与用户自己切回 normal 同性质），**不是**异常路径的自动回滚；实现落在
`session/service.py` 的恢复提交处，不在异常臂。
"""

from __future__ import annotations

import hashlib
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_harness.session.context import (
    current_constraint_tool_context_var,
    current_session_var,
    run_context_var,
)
from agent_harness.session.event import USER_INPUT_REQUESTED
from agent_harness.session.workflow import WorkflowMode, effective_workflow_mode
from agent_harness.storage.artifact import ArtifactStore
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

#: 计划正文上限，对齐 `web/app.py` `custom_text` 的 100_000 量级（同为"用户可读的
#: 大块文本"输入面）。超限整单拒绝：无事件、无 artifact。
PLAN_MARKDOWN_MAX_CHARS = 100_000

#: 工具名单源定义；根工具注册表的纯投影直接读取此常量，不构造工具实例。
SUBMIT_PLAN_TOOL_NAME = "submit_plan"


class SubmitPlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_markdown: Annotated[
        str,
        Field(
            ...,
            strict=True,
            max_length=PLAN_MARKDOWN_MAX_CHARS,
            description="The full plan in markdown, shown to the user for approval.",
        ),
    ]
    touched_files: Annotated[
        list[str],
        Field(
            ...,
            description=(
                "Paths this plan will create or modify, so the user can see the blast "
                "radius before approving."
            ),
        ),
    ]

    # 审查 P4-2：`max_length` 的默认文案（"String should have at most N characters"）
    # 不带任何下一步指引。票面要求超限时告诉模型"缩短正文或拆分计划"。`mode="before"`
    # 让这条先于 `max_length` 触发，故模型看到的是下面这句而非 pydantic 默认文案；
    # `max_length` 保留为声明式契约（schema 导出仍带上限，模型菜单里看得见）。
    @field_validator("plan_markdown", mode="before")
    @classmethod
    def _within_limit(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > PLAN_MARKDOWN_MAX_CHARS:
            raise ValueError(
                f"plan_markdown 超过 {PLAN_MARKDOWN_MAX_CHARS} 字符上限；"
                "请缩短正文或把计划拆成多次提交后重试。"
            )
        return value

    @field_validator("plan_markdown")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("plan_markdown must not be blank")
        return value


class SubmitPlanTool(Tool):
    """提交计划并暂停等待裁决。构造期注入 artifact store（同 `InspectArtifactTool`）。"""

    # #526 B1：Plan 模式豁免——本工具**只在** Plan 档可用，必须能在该档执行。
    plan_mode_exempt: bool = True

    def __init__(self, artifact_store: ArtifactStore) -> None:
        """The store is used by execute; name projections use SUBMIT_PLAN_TOOL_NAME."""
        self._store = artifact_store

    @property
    def name(self) -> str:
        return SUBMIT_PLAN_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "Submit a plan for the user's approval. Use this in plan mode once you have "
            "finished researching and have a concrete plan: it persists the plan, pauses "
            "the run, and shows the user an approval card with the plan and the files it "
            "will touch. On approval the session leaves plan mode and you execute the plan; "
            "on revision you get the user's feedback and submit again; on rejection you "
            "reconsider. This is not the progress checklist—use update_plan for that."
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return SubmitPlanArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        # 与 update_plan 同款映射：写会话状态（事件 + artifact），非破坏性持久写。
        return ToolPermission.WORKSPACE_WRITE

    @property
    def batch_exclusive(self) -> bool:
        return True

    @property
    def prompt_guidance(self) -> str:
        return (
            "In plan mode, call submit_plan once your plan is concrete instead of asking "
            "for approval in prose. Do not call it outside plan mode, and do not use it as "
            "a progress checklist—that is update_plan. While the approval is pending, do "
            "not start the work. If the user asks for revisions, revise and submit again; "
            "if rejected, reconsider the approach."
        )

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action=(
                "Inspect the durable user/input-requested event (kind=plan_approval) and "
                "the matching user/message answer before resolving this operation."
            ),
        )

    async def execute(self, args: SubmitPlanArgs) -> ToolResult:
        session = current_session_var.get()
        run_id = run_context_var.get()
        if session is None or run_id is None:
            return ToolResult.failure(
                message="submit_plan requires an active Session and run context",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )

        # 行为级分界（票面 §3.2 行为 1）：非 PLAN 档直接失败，文案指路 update_plan。
        if effective_workflow_mode(session.events) != WorkflowMode.PLAN:
            return ToolResult.failure(
                message=(
                    "当前不在 plan 档；维护进度清单请用 `update_plan`。"
                    "切换到 plan 档后重新调用 `submit_plan`。"
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )

        context = current_constraint_tool_context_var.get()
        scope_code = scope_rejection_code(session, run_id, context)
        if scope_code == SCOPE_REJECTION_SOURCE_UNAVAILABLE:
            return rejected_result(
                scope_code, "当前模型输入中没有可归因的 run 上下文，未提交计划。",
            )
        if scope_code == SCOPE_REJECTION_NOT_ALLOWED:
            return rejected_result(scope_code, "当前执行范围不能提交计划。")
        assert context is not None
        if context.user_input_request_rejection_code is not None:
            code = context.user_input_request_rejection_code
            return rejected_result(code, admission_rejection_message(code))

        pending = pending_user_input_request(session.events, run_id=run_id)
        if pending is not None:
            return ToolResult.success(
                message="本 run 已有待处理的请求；将恢复该请求，不会创建第二个。",
                data={
                    "status": "pending",
                    "request_id": pending.data["request_id"],
                },
            )

        artifact = await self._store.save(
            session.session_id,
            args.plan_markdown,
            mime_type="text/markdown",
            source_tool="submit_plan",
            # execute 期拿不到 tool_call_id（executor 在 execute 返回后才绑定调用 id），
            # 与 multiagent/tools.py 的既有取值一致。
            tool_call_id="",
        )
        request_id = str(uuid4())
        payload = {
            "request_id": request_id,
            "kind": "plan_approval",
            "run_id": run_id,
            "plan_markdown": args.plan_markdown,
            "touched_files": list(args.touched_files),
            "artifact_id": artifact.artifact_id,
            # 正文指纹：审批后模型侧可核对"批的就是这份"，也供 e2e 断言。
            "plan_sha256": hashlib.sha256(
                args.plan_markdown.encode("utf-8")
            ).hexdigest(),
        }
        return ToolResult.success(
            message="计划已提交；当前 run 将暂停，等待用户批准、要求修改或拒绝。",
            data={
                "status": "requested",
                "request_id": request_id,
                "artifact_id": artifact.artifact_id,
            },
            pending_events=[(USER_INPUT_REQUESTED, payload)],
        )
