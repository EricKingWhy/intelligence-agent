"""UpdatePlanTool：进度清单整表覆盖（W-26 / #380，PRD §7.3）。

`update_plan` 是 PORT DESIGN（自 Codex `update_plan` / Gemini `write_todos`）：
每次调用提交**全量清单**，harness 硬校验（PRD §7.2 四条，权威实现在
`session/plan.py` 的 handler——工具只是它的翻译层）后落一个
`task/plan_updated` 事件。选整表覆盖而非增量补丁的依据：弱模型不易写坏；
天然解决并发合并。

走 ToolExecutor 统一执行路径（不变量 #7）：审批/重试/预算/事件壳（TOOL_CALL /
TOOL_RESULT）全部由 Executor 承担，本工具只负责"校验 + append + 翻译结果"。
校验失败返回 `INVALID_ARGUMENT`（不重试、回模型自纠错），并把**当前完整清单**
随 message / data 带回——那是 PRD §7.2 整表覆盖范式的自修复通道。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.session import (
    TASK_PLAN_UPDATED,
    apply_plan_update,
    current_session_var,
)
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode


class _PlanItemArg(BaseModel):
    """清单行（PRD §7.1 五字段）。`extra="forbid"`：多塞字段直接非法参数。

    `activeForm` 直接以 camelCase 作 python 字段名、不绕 alias：registry 导出
    模型菜单用 `args_schema.model_json_schema()`，不带 `by_alias`
    （tooling/contract.py 的 args_schema 契约）——alias 会让模型面看到
    `active_form`，与事件 payload / PRD §7.1 相悖。直名让 schema、payload、
    python 三处同名，零转译。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="稳定字符串，整表覆盖时的对齐键")
    content: str = Field(..., min_length=1, description="任务描述（一句话，可执行）")
    activeForm: str = Field(
        ..., min_length=1,
        description="进行中显示形式（Claude Code TaskCreate 同款字段）",
    )
    status: str = Field(..., description="pending | in_progress | completed")
    source: str = Field(..., description="initializer | user | agent")


class _UpdatePlanArgs(BaseModel):
    """整表覆盖：items 是**全量**清单，不是增量补丁。

    刻意不在 pydantic 上设 `max_length`：>50 必须由 handler（`apply_plan_update`）
    拒绝，错误文案才带"合并相邻项 + 当前完整清单"（PRD §7.2 的自修复通道）——
    参数校验层短路会把这条通道掐断。校验长在 handler 上，不在工具上。
    """

    model_config = ConfigDict(extra="forbid")

    items: list[_PlanItemArg] = Field(
        ...,
        description="完整任务清单（每次提交全表，至多 50 条）。"
                    "单 in_progress；completed 不可回退。",
    )


class UpdatePlanTool(Tool):
    """`update_plan`：提交进度清单全表（handler 硬校验，拒绝即整表不落）。"""

    # #526 B1：Plan 模式豁免——仅写会话状态（进度清单），无外部资源副作用。
    plan_mode_exempt: bool = True

    def __init__(self) -> None:
        # 零依赖：会话经 `current_session_var` 在执行期取得（context.py）。
        # 显式无参构造还有一层机械作用：对账测试的 `_name_of` 按签名枚举构造参数，
        # 继承的 object.__init__ 会被读成两个必需参数而构造失败。
        pass

    @property
    def name(self) -> str:
        return "update_plan"

    @property
    def description(self) -> str:
        return (
            "提交/更新任务进度清单（每次提交**完整清单**，不是增量）。"
            "什么时候用：开始一项工作时把它置为 in_progress（同时把上一项置回"
            " pending 或 completed）；完成一项立即置 completed。"
            "约束：同一时刻至多一项 in_progress；completed 不可回退（要撤销就新增"
            "一项）；清单至多 50 条（超了就合并相邻项）。"
            "校验失败时错误信息会附当前清单——按它修正后重新提交全表。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _UpdatePlanArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        # ADR-0031 §3.2 同款映射：会话内可加、可撤销、非破坏性的持久写 →
        # WORKSPACE_WRITE（不新增权限枚举成员）。
        return ToolPermission.WORKSPACE_WRITE

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=False,
            suggested_action=(
                "清单状态以事件流重放为准；重复提交同一张表是幂等的，重跑安全。"
            ),
        )

    async def execute(self, args: _UpdatePlanArgs) -> ToolResult:
        session = current_session_var.get()
        if session is None:
            # 装配回归 / 旁路调用：如实失败，不假装可用（事件一个都不落）。
            return ToolResult.failure(
                message="update_plan 需要 run 上下文中的会话（装配缺失，非模型问题）",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        outcome = apply_plan_update(
            session, [item.model_dump() for item in args.items],
        )
        if not outcome.ok:
            # message 已含"违反规则 + 当前完整清单"（reason 的自修复文案，模型面）；
            # metadata 再带一份结构化清单给工具面/测试。
            return ToolResult.failure(
                message=outcome.reason or "清单更新被拒绝",
                error_code=ErrorCode.INVALID_ARGUMENT,
                metadata={"current_items": [
                    item.to_payload() for item in outcome.current
                ]},
            )
        in_progress = sum(1 for item in outcome.applied if item.status == "in_progress")
        return ToolResult.success(
            message=(
                f"清单已更新：{len(outcome.applied)} 项"
                f"（in_progress {in_progress} 项）。"
                f"{json.dumps([item.to_payload() for item in outcome.applied], ensure_ascii=False)}"
            ),
            data={
                "items": [item.to_payload() for item in outcome.applied],
                "event_type": TASK_PLAN_UPDATED,
            },
        )
