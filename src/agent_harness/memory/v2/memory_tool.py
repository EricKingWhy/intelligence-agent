"""`memory` 工具（M1）：markdown 权威的 curated memory 写入面。

移植自 NousResearch/hermes-agent ``tools/memory_tool.py``（commit
b324b1f7ed52369ab4bd3b1a03378729120b0f01，2026-10-02，MIT，Copyright (c) 2025
Nous Research）。#1353 §3 M1 的照搬面与改动面：

- **照搬**：``MEMORY_SCHEMA`` 的 ``description`` 全文、参数形状（``action`` /
  ``target`` / ``content`` / ``old_text`` / ``new_text`` / ``operations``）、
  ``old_text`` 短子串寻址（``markdown_store.find_unique_match``，0 命中 / ≥2 命中
  回 ``current_entries`` 让模型自纠）、三段式超限文案（``markdown_store`` 内）。
- **改**：dispatch 换 Owlet ``Tool`` ABC + pydantic ``args_schema``（票面已批准
  偏差 4）；动态 target 枚举固定为 ``{memory, user}``（pydantic 静态 schema，不再
  按启用开关收窄）；``add`` 增加**软校验**（票面 §1 清单第 3 条）。

**已批准偏差（文案）**：``description`` 里两处引用了 Hermes 才有的工具名——
``skill_manage`` 换成本仓的 ``promote_skill``，``session_search`` 换成"会话历史"。
除此之外逐字保留；不换会让模型去调不存在的工具。

**batch 的形状**：上游 ``action`` 枚举只有 ``add``/``replace``/``remove``，
batch 由 ``operations`` 数组表达（票面文字写作 ``action ∈ {…, batch}``）。
两边都接受：``operations`` 非空即走 batch 路径（上游 dispatcher 的实际行为），
``action="batch"`` 因此是无害的别名，写进枚举不会改变语义。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from agent_harness.memory.types import memory_session_var
from agent_harness.memory.v2 import origin
from agent_harness.memory.v2.commands import (
    current_user_message,
    trusted_identity_for_session,
)
from agent_harness.memory.v2.commit import MemoryCommit, MemoryCommitError
from agent_harness.memory.v2.guidance import MEMORY_GUIDANCE
from agent_harness.memory.v2.write_approval import evaluate_gate
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode

logger = logging.getLogger(__name__)

#: 逐字照搬 Hermes ``MEMORY_SCHEMA["description"]``（两处工具名已按本仓改名，见模块文档）。
MEMORY_DESCRIPTION = (
    "Save durable facts to persistent memory that survive across sessions. Memory is "
    "injected into every future turn, so keep entries compact and high-signal.\n\n"
    "HOW: make ALL your changes in ONE call via an 'operations' array (each item: "
    "{action, content?, old_text?}). The batch applies atomically and the char limit is "
    "checked only on the FINAL result — so a single call can remove/replace stale entries "
    "to free room AND add new ones, even when an add alone would overflow. The response "
    "reports current/limit chars and confirms completion; one batch call finishes the "
    "update, so don't repeat it. Use the bare action/content/old_text fields only for a "
    "single lone change.\n\n"
    "WHEN: only for facts that apply to EVERY session regardless of task: who the user "
    "is, stable environment facts, standing conventions with no task home. Anything "
    "learned while doing a task (procedures, pitfalls, and the user's preferences and "
    "corrections for that kind of work) belongs in the task's skill via promote_skill, "
    "where it loads only when relevant; memory is injected into every turn and must "
    "stay small.\n\n"
    "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that "
    "removes or shortens enough stale entries and adds the new one together.\n\n"
    "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
    "notes (environment, conventions, tool quirks, lessons).\n\n"
    "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
    "completed-work logs, temporary TODO state (use the session history for those). Reusable "
    "procedures belong in a skill, not memory."
)

_TARGETS = ("memory", "user")
_ACTIONS = ("add", "replace", "remove", "batch")


class _MemoryOp(BaseModel):
    """batch 里的单个 op。``new_text`` 是 ``content`` 的别名（上游同义）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["add", "replace", "remove"]
    content: str | None = None
    new_text: str | None = None
    old_text: str | None = None
    #: 仅为 schema 兼容保留（``extra="forbid"`` 下模型多传它会报错）：任何路径都不转发它，
    #: 审批重放的 pin 由 ``write_approval._replay`` 从 stage 时的 ``pinned_entry`` 注入。
    matched_entry: str | None = None


class _MemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 上游 schema 把 target 标为 required；这里给默认值，容忍 JSON null（严格供应商
    #: 会给可选字段发 null），与上游 dispatcher 的 ``target = "memory" if target is None`` 同义。
    target: Literal["memory", "user"] = "memory"
    action: Literal["add", "replace", "remove", "batch"] | None = None
    content: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    operations: list[_MemoryOp] | None = None

    @model_validator(mode="after")
    def _shape(self) -> _MemoryArgs:
        # 上游把 required 校验放在运行时 handler（strict backend 拒绝 allOf/if-then），
        # 这里用 pydantic 做同一件事：错误信息进 ToolResult，模型据此自纠。
        if self.operations is not None and not self.operations:
            raise ValueError("operations must be a non-empty list of {action, ...} objects.")
        if self.operations is not None:
            if self.action not in (None, "batch"):
                raise ValueError("omit 'action' when using 'operations' (or set action='batch').")
            return self
        if self.action is None:
            raise ValueError("action is required for a single-op write: add, replace, or remove.")
        if self.action == "batch":
            raise ValueError("action='batch' requires a non-empty 'operations' array.")
        content = self.content if self.content is not None else self.new_text
        if self.action == "add" and not (content or "").strip():
            raise ValueError("content is required for 'add'.")
        if self.action in ("replace", "remove") and not (self.old_text or "").strip():
            raise ValueError(
                f"'{self.action}' needs old_text — a short unique substring of the entry to "
                f"{self.action}. Reissue with old_text set to part of one of the current entries."
            )
        if self.action == "replace" and not (content or "").strip():
            raise ValueError("content is required for 'replace' (use action='remove' to delete).")
        return self

    def content_or_alias(self) -> str:
        return (self.content if self.content is not None else self.new_text) or ""

    def operations_payload(self) -> list[dict[str, Any]]:
        # `matched_entry` 是系统 pin（审批重放时由 `write_approval._replay` 注入），
        # 模型填什么这里都不转发：它一旦被采信就能绕过 old_text 改任意条目。
        return [
            {
                "action": op.action,
                "content": op.content if op.content is not None else op.new_text,
                "old_text": op.old_text,
            }
            for op in self.operations or []
        ]


def _payload(args: _MemoryArgs) -> dict[str, Any]:
    """stage 的持久化形状：一次工具调用 = 一条待审记录（batch 整批一条，见 write_approval）。"""
    payload: dict[str, Any] = {"target": args.target}
    if args.operations is not None:
        payload["action"] = "batch"
        payload["operations"] = args.operations_payload()
        return payload
    payload["action"] = args.action
    payload["content"] = args.content_or_alias()
    payload["old_text"] = args.old_text
    return payload


def _normalise(text: str) -> str:
    return " ".join(text.casefold().split())


def _added_contents(args: _MemoryArgs) -> list[str]:
    """本次调用里所有 ``action=add`` 的正文（单条 add 或 batch 里的 add）。"""
    if args.operations is not None:
        return [
            op.content if op.content is not None else op.new_text or ""
            for op in args.operations if op.action == "add"
        ]
    return [args.content_or_alias()] if args.action == "add" else []


def _grounded_in_user_message(content: str, user_text: str) -> bool:
    """软校验：content 必须能在本 run 的 user message 里找到依据（票面 §1 清单第 3 条）。

    比 Hermes 的 ``remember_this`` 逐字摘录宽松：只要求**规范化后的子串**命中，
    允许模型把用户原话压缩/拼接成一条条目；但内容凭空捏造仍然拦下。
    """
    folded = _normalise(content)
    return bool(folded) and folded in _normalise(user_text)


class MemoryTool(Tool):
    """`memory`：MEMORY.md / USER.md 的 add / replace / remove / batch 写入面。"""

    def __init__(
        self, commit: MemoryCommit, sessions: Any | None = None, *,
        service: Any | None = None, staged: Any | None = None,
        workspace_index: Any | None = None,
    ) -> None:
        #: #1494：唯一内容写者。工具面只决定「写什么」，匹配/校验/回执/发布都在 commit 内。
        #: 构造器只存依赖（工具面静态对账会用 None 实例化），`commit.engine` 在执行期再取。
        self._commit = commit
        self._sessions = sessions
        #: M6：`service` 读 `write_approval` 开关，`staged` 是门开时的待审区。两者缺席
        #: （纯 markdown 部署 / 直调）时门恒关——没有开关就没有可开的东西。
        self._service = service
        self._staged = staged
        self._workspace_index = workspace_index

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return MEMORY_DESCRIPTION

    @property
    def args_schema(self) -> type[BaseModel]:
        return _MemoryArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WORKSPACE_WRITE

    @property
    def prompt_guidance(self) -> str:
        """M8：静态 guidance，进 system prompt 的 `tool:memory` section（见 guidance 模块）。"""
        return MEMORY_GUIDANCE

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action="重新读取 MEMORY.md / USER.md 核对条目；不要盲目重发同一批操作。",
        )

    async def execute(self, args: _MemoryArgs) -> ToolResult:
        target = args.target
        if not self._commit.engine.target_enabled(target):
            label = "USER.md" if target == "user" else "MEMORY.md"
            return ToolResult.failure(
                message=f"内置 {label} 写入已在 memory 配置中关闭。",
                error_code=ErrorCode.PERMISSION_DENIED,
            )
        # batch 里的每条 add 都要过软校验：整批只查一次等于给批量写入开后门。
        for content in _added_contents(args):
            denial = await self._add_soft_check(content)
            if denial is not None:
                return denial
        try:
            gated = await self._gate(_payload(args))
            if gated is not None:
                return gated
        except PermissionError:
            return ToolResult.failure(
                message="user memory 范围未获授权",
                error_code=ErrorCode.PERMISSION_DENIED,
            )
        # #1494 review P3-3：工具面把「内容发布 + 派生投影」放进 service 的 content
        # 临界区，避免 delete 在两者之间按旧快照清理正文。`service` 缺席（静态对账 /
        # 只接 commit 的测试）时没有共享锁，保持原行为。
        scope_factory = getattr(self._service, "content_write_scope", None)
        scope = scope_factory() if callable(scope_factory) else contextlib.nullcontext()
        async with scope:
            try:
                if args.operations is not None:
                    response = await self._commit.apply_batch(
                        target, args.operations_payload())
                elif args.action == "add":
                    response = await self._commit.add(target, args.content_or_alias())
                elif args.action == "replace":
                    response = await self._commit.replace(
                        target, args.old_text or "", args.content_or_alias(),
                    )
                else:
                    response = await self._commit.remove(target, args.old_text or "")
            except MemoryCommitError as error:
                # AC5：发布失败的回执停在 PREPARED（未 committed），operation id 是唯一能
                # 把这次失败与盘上回执对上的键，必须进工具结果给模型/用户做 reconcile。
                logger.warning(
                    "memory tool commit failed (%s, operation %s)",
                    type(error).__name__, error.operation_id,
                )
                return ToolResult.failure(
                    message=(
                        f"记忆写入失败（{type(error).__name__}），操作 {error.operation_id} "
                        "可能未落盘；请用 reconcile 核对，不要盲目重发。"
                    ),
                    error_code=ErrorCode.TRANSIENT_ERROR,
                    metadata={"operation_id": error.operation_id, "target": target},
                )
            except Exception as error:  # noqa: BLE001 — 不记内容，只记类型
                logger.warning("memory tool failed (%s)", type(error).__name__)
                return ToolResult.failure(
                    message=f"记忆写入失败（{type(error).__name__}）；存储可能未改变。",
                    error_code=ErrorCode.TRANSIENT_ERROR,
                )
            projection = await self._project(args, response)
        return _result(response, target, projection)

    async def _gate(self, payload: dict[str, Any]) -> ToolResult | None:
        """M6：门开 → 钉住条目、整批入待审区并**返回 stage 结果**（``None`` = 直接写）。

        ``staged: True`` 必须一路带到模型与 M7 的摘要里——这条写入**没有落盘**，把它报成
        "已更新"会让用户在下一个 session 里找不到记忆。

        进门先打 ``FOREGROUND``（#1438）：钉住的是 `evaluate_gate` 读到的来源，退出时
        还原。**不覆盖** `execute` 里更早跑的 `_add_soft_check`——那条守卫按环境标判定
        （生产中后台 job 在独立 task 内且 `finally` 还原，前台调用看不到后台标）。
        """
        if self._service is None or self._staged is None:
            return None
        token = origin.set_origin(origin.FOREGROUND)
        try:
            session_id = memory_session_var.get() or ""
            trusted = trusted_identity_for_session(session_id, self._workspace_index)
            decision = evaluate_gate(await self._service.get_settings(trusted))
        finally:
            origin.reset_origin(token)
        if decision.allow:
            return None
        pins, error = await self._staged.resolve_pins(payload)
        if error is not None:
            # 选不中的 old_text 不进待审区：批准它只会再失败一次。与直接写入的响应同形，
            # 模型照样能拿 current_entries 自纠。
            return _result(error, payload["target"])
        staged = await self._staged.stage(payload, pins, trusted)
        return ToolResult.success(
            message=f"{decision.message} Pending id: {staged.id}.",
            data={
                "success": True, "staged": True, "pending_id": staged.id,
                "target": payload["target"], "message": decision.message,
            },
        )

    async def _add_soft_check(self, content: str) -> ToolResult | None:
        """前台 add 的软校验；后台 review 提炼的内容不适用（来源不是某条 user message）。"""
        if origin.current_origin() != origin.FOREGROUND or self._sessions is None:
            return None
        session_id = memory_session_var.get() or ""
        source = await current_user_message(self._sessions, session_id)
        if source is None:
            return None  # 拿不到本轮消息 ≠ 违规（直调 / 无会话场景）
        user_text = str(source.data.get("content", ""))
        if _grounded_in_user_message(content, user_text):
            return None
        return ToolResult.failure(
            message=(
                "add 的内容必须能在本轮用户消息里找到依据（软校验）。"
                "请把 content 改成用户本轮消息里的原话或它的连续片段后重试；没有写入。"
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )

    async def _project(self, args: _MemoryArgs, response: dict[str, Any]) -> dict[str, Any] | None:
        """把已落盘的写入投影到派生层（#1437 F-01 甲案）；``None`` = 本部署无投影面。

        派生层失败**不得**让权威写入报错：markdown 已经提交，这里只留 pending 账本与一条
        不含内容的 warning（与 `mirror_committed` 的"提交后失败只留痕"同口径）。

        session 读取（`event_id` 的唯一来源）也包在同一个 try 里：它只增强 provenance，
        读不到就该降级成"无事件"继续投影，而不是让整次投影消失（审查 Corr P3-1）。

        无可信身份时返回 ``{"pending": True}`` 而不是 ``None``：``None`` 会让模型看到
        一次"已完成"的投影，实际派生层里什么都没有。``pending`` 至少如实告诉模型
        "权威已落盘、派生层待收敛"——与投影失败时的可见口径一致（审查 Std P4-4）。
        """
        if self._service is None:
            return None
        session_id = memory_session_var.get() or ""
        try:
            trusted = trusted_identity_for_session(session_id, self._workspace_index)
        except PermissionError:
            logger.warning("memory projection skipped (%s): no trusted identity", args.target)
            return {
                "created": 0, "deleted": 0, "skipped": [], "pending": True,
                "desired": 0, "unreadable": [],
            }
        event_id = None
        if self._sessions is not None and session_id:
            try:
                source = await current_user_message(self._sessions, session_id)
            except Exception as error:  # noqa: BLE001 — provenance 读失败不该阻断投影
                logger.warning(
                    "memory projection without event id (%s): %s",
                    args.target, type(error).__name__)
            else:
                event_id = source.event_id if source is not None else None
        try:
            return await self._service.project_tool_write(
                args.target, response, trusted,
                session_id=session_id or None, event_id=event_id,
                operations=args.operations_payload() if args.operations is not None else None,
                content=args.content_or_alias() if args.action != "remove" else None,
            )
        except Exception as error:  # noqa: BLE001 — 投影失败不能吞掉权威写入的成功
            logger.warning("memory projection failed (%s): %s", args.target, type(error).__name__)
            return {
                "created": 0, "deleted": 0, "skipped": [], "pending": True,
                "desired": 0, "unreadable": [],
            }


def _result(
    response: dict[str, Any], target: str, projection: dict[str, Any] | None = None,
) -> ToolResult:
    """把 store 的响应映射成 ToolResult（模型可见内容走 ``message`` + ``data``）。

    失败时把 ``current_entries`` 拼进 ``message``：AC1 要求 0 命中 / ≥2 命中时模型能
    就地自纠，而上游把整份 JSON 直接回给模型。这里两条通道都给（message 可读、
    ``data`` 结构化），语义与上游一致。

    #1437：``projection`` 只进 ``data``（``projection`` / ``projection_skipped``），**不改**
    message——记忆确实已存进权威面，成功语义不变。
    """
    if response.get("success"):
        data: dict[str, Any] = {**response, "target": target}
        if projection is not None:
            data["projection"] = "pending" if projection.get("pending") else "done"
            if skipped := projection.get("skipped"):
                data["projection_skipped"] = skipped
        return ToolResult.success(
            message=str(response.get("message") or response.get("note") or "记忆已更新。"),
            data=data,
        )
    message = str(response.get("error") or "记忆写入被拒绝。")
    if entries := response.get("current_entries"):
        message += "\n\n当前条目：\n" + "\n".join(f"- {entry}" for entry in entries)
    if matches := response.get("matches"):
        message += "\n\n命中的条目（请用更精确的 old_text）：\n" + "\n".join(
            f"- {entry}" for entry in matches
        )
    return ToolResult.failure(
        message=message,
        error_code=ErrorCode.INVALID_ARGUMENT,
        metadata={"response": response, "target": target},
    )


__all__ = ["MEMORY_DESCRIPTION", "MemoryTool"]
