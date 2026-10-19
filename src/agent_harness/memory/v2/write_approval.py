"""write_approval + entry pinning（#1353 M6）。

移植自 NousResearch/hermes-agent ``tools/write_approval.py``（commit
b324b1f7ed52369ab4bd3b1a03378729120b0f01，2026-10-02，MIT，Copyright (c) 2025
Nous Research）。上游是 409 行的通用门（memory 与 skills 两个子系统、磁盘
``pending/<subsystem>/<id>.json``、控制台行内提问）；本仓按单用户 + 单写入口收敛成
一个 SQLite 表 + 一个纯函数，保留**语义**而不是形状。

# 三态 gate

``GateDecision(allow | blocked | stage)`` 恰好一个为真。``evaluate_gate`` 只产出
allow / stage——``blocked`` 是**审批结果**（用户明确拒绝）而不是门本身的判断，
由 `approve`/`reject` 返回。把两者混在门里会让"没人问过"和"用户说不"看起来一样。

# 为什么没有 skills 分支、也没有行内提问

- **skills**：本仓的技能写入已经是两段式——``promote_skill`` 只写待审草稿，
  ``register_skill`` 本身走 ToolExecutor 的审批闸门（DANGER）。再套一层
  ``write_approval`` 会让同一次技能落盘问两遍。上游需要它是因为它的 ``skill_manage``
  直接写正式目录。
- **行内提问**：上游在 TTY 里同步问一句。本仓的工具执行在事件循环里，交互面是
  `tooling/approval_queue.py` 的会话队列——而那个队列按 tool_call 作用域登记、
  由 ToolExecutor 消费，``memory`` 工具拿不到它。票面给的兜底正是本仓的答案：
  **拿不到通道 → stage，绝不静默拒绝**。所以门开时 memory 写入一律 stage，审批走
  ``/memory pending|approve|reject``。

# entry pinning（堵 TOCTOU）

stage 之前先 resolve：把 ``old_text`` **此刻**选中的条目全文存进
``memory_v2_staged.pinned_entry``。审批重放时只接受全文完全一致的条目
（`markdown_store.pinned_index`），找不到 → 拒绝且**保留** record；没有 pin 的
legacy record 直接拒绝，绝不按 ``old_text`` 重搜——重搜正是 TOCTOU 本体。

batch 是**一个** pending record：整批的 pin 按 operations 顺序存成一个 JSON 数组，
审批时整批重放，不逐条问。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_harness.memory.v2._sqlite import connect, stamp
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.store import STAGED_SCHEMA, MemorySettingsV2
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.storage.sqlite import retry_on_busy

logger = logging.getLogger(__name__)

#: 上游 ``CONFIG_KEY``；本仓落在 `memory_v2_settings.write_approval`。
CONFIG_KEY = "write_approval"


def _identity(trusted: TrustedMemoryIdentity) -> str:
    """可信身份的规范化键。审批只对**同一个**身份可见，项目维度也参与。"""
    return json.dumps(
        {"tenant_id": trusted.tenant_id, "user_id": trusted.user_id,
         "project_id": trusted.project_id},
        sort_keys=True, separators=(",", ":"),
    )

#: ``status`` 的三个取值。approved/rejected 是**终态但保留行**——审计与
#: 「拒绝时保留 record」都要求行还在。
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"


@dataclass(frozen=True)
class GateDecision:
    """门的三态：``allow`` / ``blocked`` / ``stage``，恰好一个为真。"""

    allow: bool = False
    blocked: bool = False
    stage: bool = False
    message: str = ""

    def __post_init__(self) -> None:
        if sum((self.allow, self.blocked, self.stage)) != 1:
            raise ValueError("exactly one of allow / blocked / stage must be true")


def evaluate_gate(settings: MemorySettingsV2) -> GateDecision:
    """``memory`` 写入的门。开 → stage；关 → allow。

    门开时**总是** stage：本仓没有可用的行内审批通道（见模块 docstring），而票面
    明确"拿不到审批通道 → stage，绝不静默拒绝"。后台 review 与前台走同一条路——
    上游对后台强制 stage 的理由（无人可问）在这里对两者都成立。
    """
    if settings.write_approval:
        return GateDecision(stage=True, message=_staged_message())
    return GateDecision(allow=True)


def _staged_message() -> str:
    # 上游逐字：``Staged for approval (<subsystem>.write_approval is on). Not yet saved
    # — review with /<subsystem> pending.`` 本仓没有 `/memory` 聊天斜杠命令（待审区是
    # CLI + API 面），所以提示改成用户**真能敲到**的两个入口。
    return (
        f"Staged for approval ({CONFIG_KEY} is on). Not yet saved — review with "
        f"`agent-harness memory pending` or GET /api/memory-pending."
    )


def _stale_message(entry: str) -> str:
    return (
        f"Entry changed since it was staged, so this write was not applied: '{entry}' is no "
        f"longer in memory as reviewed. Recreate the change against the current entry or "
        f"reject it; the pending record has been preserved."
    )


@dataclass(frozen=True)
class StagedWrite:
    id: str
    payload: dict[str, Any]
    pinned_entry: list[str | None] | None
    status: str
    created_at: str


class StagedWriteStore:
    """``memory_v2_staged`` 的读写与重放。

    刻意**不**复用 `PendingApprovalQueue`（每 session 纯内存、按 tool_call 作用域、
    不存已批准状态）也**不**扩展 ``memory_v2_jobs``（那是 job 态，staged 是审批态）。
    """

    def __init__(self, database_path: Path, markdown: MarkdownMemoryStore) -> None:
        self.database_path = Path(database_path)
        self.markdown = markdown

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with connect(self.database_path) as connection:
            await connection.executescript(STAGED_SCHEMA)
            await connection.commit()

    # ------------------------------------------------------------------ stage

    async def stage(
        self, payload: dict[str, Any], pins: list[str | None] | None,
        trusted: TrustedMemoryIdentity,
    ) -> StagedWrite:
        record_id = f"stg_{uuid4().hex}"
        created = stamp()
        async with connect(self.database_path) as connection:
            await connection.execute(
                "INSERT INTO memory_v2_staged "
                "(id, identity, payload, pinned_entry, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record_id, _identity(trusted),
                    json.dumps(payload, ensure_ascii=False),
                    None if pins is None else json.dumps(pins, ensure_ascii=False),
                    PENDING, created,
                ),
            )
            await connection.commit()
        return StagedWrite(record_id, payload, pins, PENDING, created)

    async def resolve_pins(
        self, payload: dict[str, Any],
    ) -> tuple[list[str | None] | None, dict[str, Any] | None]:
        """stage 前把每条 replace/remove 选中的条目**全文**钉住。

        返回 ``(pins, error)``。``error`` 非空时**不 stage**：一条解析不出来的写入
        存进待审区只会让用户点一次注定失败的"批准"。add-only 的写入没有 pin（None）。
        """
        target = payload["target"]
        operations = payload.get("operations")
        if operations is None:
            action = payload.get("action")
            if action == "add":
                return None, None
            resolved = await self.markdown.resolve_entry(
                target, payload.get("old_text") or "", action or "replace")
            if not resolved.get("success"):
                return None, resolved
            return [resolved["matched_entry"]], None
        resolved = await self.markdown.resolve_batch_entries(target, operations)
        if not resolved.get("success"):
            return None, resolved
        return list(resolved["matched_entries"]), None

    # ------------------------------------------------------------------ 查询

    async def pending(self, trusted: TrustedMemoryIdentity) -> list[StagedWrite]:
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_staged WHERE identity=? "
                "AND status=? ORDER BY created_at", (_identity(trusted), PENDING),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [_to_staged(row) for row in rows]

    async def get(self, staged_id: str, trusted: TrustedMemoryIdentity) -> StagedWrite | None:
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_staged WHERE id=? AND identity=?",
                (staged_id, _identity(trusted)),
            ) as cursor,
        ):
            row = await cursor.fetchone()
        return None if row is None else _to_staged(row)

    # ------------------------------------------------------------------ 裁决

    @retry_on_busy
    async def approve(
        self, staged_id: str, trusted: TrustedMemoryIdentity,
    ) -> tuple[GateDecision, dict[str, Any] | None]:
        """重放一条待审写入。成功 → 落盘 + 行转 approved；pin 失效 → blocked 且**保留**行。"""
        staged = await self.get(staged_id, trusted)
        if staged is None:
            return GateDecision(blocked=True, message="No such pending memory write."), None
        if staged.status != PENDING:
            return GateDecision(
                blocked=True, message=f"This memory write is already {staged.status}."), None
        response = await self._replay(staged)
        if response is None:
            return GateDecision(
                blocked=True,
                message="Pending record has no pinned entry; refusing to re-resolve old_text.",
            ), None
        if not response.get("success"):
            # 保留 record：用户还能重新创建或明确 reject（票面 M6 第 2 条）。
            return GateDecision(blocked=True, message=str(response.get("error") or "Replay failed.")), None
        await self._set_status(staged_id, APPROVED)
        return GateDecision(allow=True), response

    async def reject(
        self, staged_id: str, trusted: TrustedMemoryIdentity,
    ) -> GateDecision:
        staged = await self.get(staged_id, trusted)
        if staged is None:
            return GateDecision(blocked=True, message="No such pending memory write.")
        if staged.status != PENDING:
            return GateDecision(
                blocked=True, message=f"This memory write is already {staged.status}.")
        await self._set_status(staged_id, REJECTED)
        return GateDecision(allow=True, message="Memory write rejected.")

    async def _set_status(self, staged_id: str, status: str) -> None:
        async with connect(self.database_path) as connection:
            await connection.execute(
                "UPDATE memory_v2_staged SET status=? WHERE id=?", (status, staged_id))
            await connection.commit()

    async def _replay(self, staged: StagedWrite) -> dict[str, Any] | None:
        """按 stage 时的形状重放；``None`` = 缺 pin 的 legacy record（拒绝）。"""
        payload = staged.payload
        target = payload["target"]
        pins = staged.pinned_entry
        operations = payload.get("operations")
        if operations is None:
            action = payload.get("action")
            if action == "add":
                return await self.markdown.add(target, payload.get("content") or "")
            if pins is None or not pins or pins[0] is None:
                return None
            if action == "replace":
                return await self.markdown.replace(
                    target, payload.get("old_text") or "", payload.get("content") or "",
                    matched_entry=pins[0])
            return await self.markdown.remove(
                target, payload.get("old_text") or "", matched_entry=pins[0])
        if pins is None:
            # batch 里可能全是 add（无 pin 也安全）；只要有一条 replace/remove 就必须有 pin。
            if any(op.get("action") in ("replace", "remove") for op in operations):
                return None
            return await self.markdown.apply_batch(target, operations)
        pinned_ops = [
            {**op, **({"matched_entry": pin} if pin is not None else {})}
            for op, pin in zip(operations, pins, strict=False)
        ]
        return await self.markdown.apply_batch(target, pinned_ops)


def _to_staged(row: Any) -> StagedWrite:
    raw_pins = row["pinned_entry"]
    return StagedWrite(
        id=row["id"],
        payload=json.loads(row["payload"]),
        pinned_entry=None if raw_pins is None else json.loads(raw_pins),
        status=row["status"],
        created_at=row["created_at"],
    )


__all__ = [
    "CONFIG_KEY",
    "GateDecision",
    "StagedWrite",
    "StagedWriteStore",
    "evaluate_gate",
]
