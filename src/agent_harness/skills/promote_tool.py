"""promote_skill / register_skill：沉淀闭环的统一执行路径工具（#529 §5.2/§6.1/§8）。

不变量 #7（零旁路）：沉淀流程的全部写动作都走 ToolRegistry + ToolExecutor——
promote_skill 只写 staging（WORKSPACE_WRITE），register_skill 写 project skill
目录（DANGER，必经审批闸门）。不存在绕过 executor 的隐藏写入口。

不变量 #11（权限边界）：模型可以提议（promote_skill），但**注册必须经用户
审批**——register_skill 是 DANGER 工具，审批闸门（现有 approve 范式，§5.2）
的放行本身就是人审确认；工具内部在该放行之后才把草稿推进 human-approved →
registered。模型无法静默自助注册。

skill/removed 事件（§10-3 推荐形态）：由 discover diff 触发——每次沉淀面被
调用时对 catalog 做一次 diff，外部删除（文件即真相）在下一次调用时留痕。
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from agent_harness.identity import get_identity_context
from agent_harness.session.context import current_session_var, run_context_var
from agent_harness.session.event import SKILL_REGISTERED, SKILL_REMOVED, SKILL_UPDATED
from agent_harness.skills.capability import SkillCapability
from agent_harness.skills.discovery import single_line
from agent_harness.skills.lint import LintResult
from agent_harness.skills.promote import (
    STATUS_HUMAN_APPROVED,
    STATUS_LINT_PASS,
    ProposeOutcome,
)
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.result import ErrorCode

logger = logging.getLogger("agent_harness.skills.promote")


def _confirmed_by() -> str:
    """确认者标识（§6.2 载荷）：审批放行时的身份上下文。"""
    identity = get_identity_context()
    return f"user:{identity.user_id}"


class _PromoteSkillArgs(BaseModel):
    action: Literal["propose", "lint", "discard", "list"] = Field(
        description="propose=提交草稿（判据评估后落 staging）；lint=静态检查草稿；"
                    "discard=丢弃草稿；list=列出草稿与目录",
    )
    draft: str = Field(default="", description="propose：完整的 SKILL.md 草稿文本")
    name: str = Field(default="", description="lint / discard：草稿名")
    one_off: bool = Field(
        default=False,
        description="propose：本次经验是否单次偶发（True 会被负面清单否决）",
    )


class _RegisterSkillArgs(BaseModel):
    name: str = Field(min_length=1, description="要注册的草稿名（须经 lint-pass）")


class _PromotionBaseTool(Tool):
    """两工具共用：capability 引用 + discover diff 留痕。"""

    def __init__(self, capability: SkillCapability) -> None:
        self._capability = capability

    def _emit_removal_diff(self) -> None:
        """discover diff → skill/removed 留痕（§10-3）。观测写失败不阻断工具。"""
        try:
            removed = self._capability.poll_removals()
        except Exception:  # noqa: BLE001 — diff 失败只损失留痕及时性，不阻断沉淀
            return
        if not removed:
            return
        session = current_session_var.get()
        if session is None:
            return  # 无 run 上下文（直呼工具）：无事件流可留痕，如实跳过
        try:
            for name in removed:
                session.append(SKILL_REMOVED, {"name": name},
                               run_id=run_context_var.get())
        except Exception:
            logger.warning("skill/removed 事件落盘失败", exc_info=True)


class PromoteSkillTool(_PromotionBaseTool):
    """promote_skill：提议 / lint / 丢弃 / 列出沉淀草稿（只写 staging）。"""

    @property
    def name(self) -> str:
        return "promote_skill"

    @property
    def description(self) -> str:
        return (
            "把成功运行的经验沉淀为候选 Skill。propose 提交完整 SKILL.md 草稿"
            "（frontmatter 必备 name/description，推荐 when_to_use/allowed-tools；"
            "正文 CONTEXT→INSTRUCTIONS→EXAMPLES，CONTEXT 必须写明前置条件），"
            "系统按沉淀判据评估后落待审区；lint 做静态检查；discard 丢弃草稿；"
            "list 列出草稿与当前目录。注册需另行调用 register_skill 并经用户确认。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _PromoteSkillArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING  # 写 staging（workspace 内）

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WORKSPACE_WRITE

    async def execute(self, args: _PromoteSkillArgs) -> ToolResult:
        self._emit_removal_diff()
        promoter = self._capability.promoter
        if args.action == "list":
            return self._list(promoter.drafts())
        if args.action == "propose":
            return self._propose(promoter, args)
        if not args.name:
            return ToolResult.failure(
                message=f"action={args.action} 需要 name 参数",
                error_code=ErrorCode.INVALID_ARGUMENT, retryable=False,
            )
        if args.action == "discard":
            if promoter.discard(args.name):
                return ToolResult.success(message=f"已丢弃草稿 '{args.name}'。")
            return ToolResult.failure(
                message=f"草稿 '{single_line(args.name)}' 不存在",
                error_code=ErrorCode.INVALID_ARGUMENT, retryable=False,
            )
        # lint
        try:
            result = promoter.run_lint(args.name)
        except ValueError as error:
            return ToolResult.failure(
                message=single_line(str(error)),
                error_code=ErrorCode.INVALID_ARGUMENT, retryable=False,
            )
        return self._lint_result(args.name, result)

    def _propose(self, promoter, args: _PromoteSkillArgs) -> ToolResult:
        session = current_session_var.get()
        events = list(session.events) if session is not None else []
        outcome: ProposeOutcome = promoter.propose(
            args.draft, events=events, one_off=args.one_off,
            source_run_id=run_context_var.get(),
        )
        if not outcome.accepted:
            reasons = "\n".join(f"- {r}" for r in outcome.reasons)
            return ToolResult.failure(
                message=f"草稿未通过沉淀判据，未落盘：\n{single_line(reasons)}",
                error_code=ErrorCode.INVALID_ARGUMENT, retryable=False,
            )
        return ToolResult.success(
            message=f"草稿 '{outcome.name}' 已落待审区（draft 状态，未进目录）。"
                    f"下一步：action=lint 通过后调用 register_skill（需用户确认）。",
            data={"name": outcome.name, "status": "draft",
                  "staging_path": str(outcome.staging_path)},
        )

    def _lint_result(self, name: str, result: LintResult) -> ToolResult:
        if result.ok:
            return ToolResult.success(
                message=f"草稿 '{single_line(name)}' 通过静态 lint"
                        + (f"（警告 {len(result.warnings)} 条）" if result.warnings else "")
                        + "。可调用 register_skill 注册（需用户确认）。",
                data={"name": name, "status": "lint-pass",
                      "errors": result.errors, "warnings": result.warnings},
            )
        detail = "；".join(result.errors)
        return ToolResult.success(
            message=f"草稿 '{single_line(name)}' 未通过静态 lint，已回 draft：{single_line(detail)}",
            data={"name": name, "status": "draft",
                  "errors": result.errors, "warnings": result.warnings},
        )

    def _list(self, drafts) -> ToolResult:
        catalog = [e.name for e in self._capability.catalog()]
        lines = [f"- {d.name}（{d.status}）" for d in drafts] or ["（无草稿）"]
        return ToolResult.success(
            message="待审草稿：\n" + "\n".join(lines) + f"\n当前目录：{catalog}",
            data={"drafts": [{"name": d.name, "status": d.status} for d in drafts],
                  "catalog": catalog},
        )


class RegisterSkillTool(_PromotionBaseTool):
    """register_skill：草稿注册（DANGER——审批闸门放行 = 人审确认，§5.2）。"""

    @property
    def name(self) -> str:
        return "register_skill"

    @property
    def description(self) -> str:
        return (
            "把通过静态 lint 的沉淀草稿注册进技能目录（写入 project skill 目录并"
            "刷新 registry）。本操作需要用户确认（审批闸门）；未确认的草稿永不生效。"
            "参数：name 草稿名（须已 lint-pass）。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _RegisterSkillArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        # 不变量 #11：注册是外部可见动作，必须经用户确认——DANGER 级别在受限
        # policy 下必经审批闸门（executor._check_approval），无审批回调响亮拒绝。
        return ToolPermission.DANGER

    async def execute(self, args: _RegisterSkillArgs) -> ToolResult:
        self._emit_removal_diff()
        promoter = self._capability.promoter
        session = current_session_var.get()
        run_id = run_context_var.get()
        confirmed_by = _confirmed_by()
        record = next((d for d in promoter.drafts() if d.name == args.name), None)
        status = record.status if record is not None else "absent"
        if status == STATUS_LINT_PASS:
            # 审批闸门已放行 = 用户显式确认（§5.2 approve 范式）——在此推进人审态。
            promoter.approve(args.name, confirmed_by=confirmed_by)
        elif status != STATUS_HUMAN_APPROVED:
            return ToolResult.failure(
                message=single_line(
                    f"skill '{args.name}' 状态为 {status}：register 需先 lint-pass"
                    f"（当前状态不可注册）"
                ),
                error_code=ErrorCode.INVALID_ARGUMENT, retryable=False,
            )
        lint_summary = (
            {"errors": list(record.lint.errors), "warnings": list(record.lint.warnings)}
            if record is not None and record.lint is not None else None
        )
        already_listed = any(e.name == args.name for e in self._capability.catalog())
        try:
            entry = promoter.register(args.name, confirmed_by=confirmed_by)
        except ValueError as error:
            return ToolResult.failure(
                message=single_line(str(error)),
                error_code=ErrorCode.INVALID_ARGUMENT, retryable=False,
            )
        except OSError as error:
            # 部分成功语义（§7）：skill 写失败不影响本次 run 的 memory 产出；
            # 草稿已由 promoter 回 draft，此处只如实报错。
            return ToolResult.failure(
                message=single_line(f"skill '{args.name}' 写入失败：{error}"),
                error_code=ErrorCode.TOOL_EXECUTION_ERROR, retryable=False,
            )
        # 闭环自己的写入先对齐 diff 基线（§10-3）：skill/removed 只留给外部删除。
        self._capability.sync_known_names()
        if session is not None:
            # §6.2 事件留痕：append-only，载荷含 name / source_run_id / lint 摘要 / 确认者。
            try:
                session.append(
                    SKILL_UPDATED if already_listed else SKILL_REGISTERED,
                    {
                        "name": entry.name,
                        "source_run_id": record.source_run_id if record is not None else run_id,
                        "lint": lint_summary,
                        "confirmed_by": confirmed_by,
                    },
                    run_id=run_id,
                )
            except Exception:
                logger.warning("skill/registered 事件落盘失败", exc_info=True)
        return ToolResult.success(
            message=f"skill '{entry.name}' 已注册并生效（确认者 {confirmed_by}），"
                    f"可通过 load_skill 加载。",
            data={"name": entry.name, "confirmed_by": confirmed_by},
        )
