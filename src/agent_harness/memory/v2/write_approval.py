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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_harness.memory.v2 import reprojection
from agent_harness.memory.v2._sqlite import connect, stamp
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.store import (
    STAGED_SCHEMA,
    MemorySettingsV2,
    SqliteMemoryV2Store,
)
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.storage.sqlite import retry_on_busy

logger = logging.getLogger(__name__)

#: 上游 ``CONFIG_KEY``；本仓落在 `memory_v2_settings.write_approval`。
CONFIG_KEY = "write_approval"

#: ``status`` 的取值。approved/rejected 是**终态但保留行**——审计与
#: 「拒绝时保留 record」都要求行还在。``AWAITING`` 是 approve 的**抢占中间态**：
#: 先 CAS 抢到它再重放，并发 double-approve 才只有一个人真的落盘。
PENDING = "pending"
AWAITING = "approving"
APPROVED = "approved"
REJECTED = "rejected"

#: `approving` 的租约。进程被 SIGKILL/掉电时行会停在这个中间态，而 `pending()` 只列
#: PENDING ⇒ 这条待审写入从用户队列里静默消失。超过租约的行由 `pending()` 自动回
#: PENDING（记 warning）。取 5 分钟：一次 `approve` 的重放只是几次本地文件 IO +
#: 一次 SQLite 写，正常远小于 1 秒；留 300 倍余量足够覆盖满负载下的调度抖动，
#: 又短到用户不会被一条僵尸行卡住太久。
APPROVING_LEASE = timedelta(minutes=5)


def _identity(trusted: TrustedMemoryIdentity) -> str:
    """可信身份的规范化键。审批只对**同一个**身份可见，项目维度也参与。"""
    return json.dumps(
        {"tenant_id": trusted.tenant_id, "user_id": trusted.user_id,
         "project_id": trusted.project_id},
        sort_keys=True, separators=(",", ":"),
    )



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
    明确"拿不到审批通道 → stage，绝不静默拒绝"。

    消费者只有一个：``MemoryTool._gate``（前台 ``memory`` 工具）。**后台 formation
    不经过这里**——``executor`` 在 ``commit_with_outcome`` 的同一事务里直接
    ``create_in/update_in/invalidate_in``，把其中一条改成 stage 会让 job 终态与
    记录落地分属两个事务（AC6/AC7 要的"终态与副作用一起落地"正是那条事务）。
    后台写入的开关是 ``extraction_enabled``：关掉它就不形成，而不是形成后再逐条问。
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

    def __init__(
        self, database_path: Path, markdown: MarkdownMemoryStore,
        store: SqliteMemoryV2Store | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.markdown = markdown
        #: #1437：审批重放的写入也要进派生层。缺省 `None` = 纯 markdown 部署，重放只落权威面
        #: （与 `MemoryTool(markdown)` 无 service 时同形）。
        self.store = store

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with connect(self.database_path) as connection:
            await connection.executescript(STAGED_SCHEMA)
            # 旧库迁移：`claimed_at` 是 `approving` 的租约时间（见 APPROVING_LEASE）。
            # 不能只靠建表语句——`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作。
            async with connection.execute(
                    "PRAGMA table_info(memory_v2_staged)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            if "claimed_at" not in columns:
                await connection.execute(
                    "ALTER TABLE memory_v2_staged ADD COLUMN claimed_at TEXT")
            await connection.commit()

    # ------------------------------------------------------------------ stage

    @retry_on_busy
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
        """本身份的待审写入；顺带回收**租约过期**的 ``approving`` 行。

        回收是 `approve` 被 SIGKILL/掉电打断后的唯一出路：那些行既不在 PENDING 列表里，
        又会被 `approve`/`reject` 以 "already being processed" 拒绝，不回收就永久消失。
        CAS 回 PENDING（`status=approving AND claimed_at<=cutoff`）只会收回**超期**
        的僵尸行：一次正常 `approve` 的重放只是本地文件 IO + 一次 SQLite 写，远小于
        5 分钟租约，不会被误收。
        """
        identity = _identity(trusted)
        cutoff = stamp(datetime.now(UTC) - APPROVING_LEASE)
        async with connect(self.database_path) as connection:
            cursor = await connection.execute(
                "UPDATE memory_v2_staged SET status=?, claimed_at=NULL "
                "WHERE identity=? AND status=? AND (claimed_at IS NULL OR claimed_at<=?)",
                (PENDING, identity, AWAITING, cutoff),
            )
            reclaimed = cursor.rowcount
            await connection.commit()
            if reclaimed:
                logger.warning(
                    "reclaimed %d stale approving memory write(s) past %s lease",
                    reclaimed, APPROVING_LEASE)
            async with connection.execute(
                "SELECT * FROM memory_v2_staged WHERE identity=? "
                "AND status=? ORDER BY created_at", (identity, PENDING),
            ) as rows_cursor:
                rows = await rows_cursor.fetchall()
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

    # 不挂 `retry_on_busy`（#1353 复审 S-P2-1）：`approve` 是
    # get → CAS AWAITING → `_replay`（**外部 markdown 写**）→ CAS APPROVED 的长链，
    # 不是装饰器前提里的"单语句原子写"。整块重跑时第二遍读到的是自己刚写的 AWAITING，
    # 直接返回 blocked，而 markdown 已经落盘 ⇒ 状态机卡在 AWAITING、条目重复。
    # 锁竞争由每次 `_set_status` 自身的重试 + `busy_timeout=10s` 覆盖。
    async def approve(
        self, staged_id: str, trusted: TrustedMemoryIdentity,
    ) -> tuple[GateDecision, dict[str, Any] | None]:
        """重放一条待审写入。成功 → 落盘 + 行转 approved；pin 失效 → blocked 且**保留**行。

        并发 double-approve：读状态与落盘之间必须先 **CAS PENDING→AWAITING** 抢到这条
        记录，抢不到（另一次 approve/reject 已动过它）直接 blocked。markdown 写成功后
        才能转 APPROVED；重放失败把状态放回 PENDING——record 保留，用户还能重试或 reject。
        """
        staged = await self.get(staged_id, trusted)
        if staged is None:
            return GateDecision(blocked=True, message="No such pending memory write."), None
        if staged.status != PENDING:
            if staged.status == AWAITING:
                return GateDecision(
                    blocked=True,
                    message="This memory write is already being processed by another approval.",
                ), None
            return GateDecision(
                blocked=True, message=f"This memory write is already {staged.status}."), None
        if not await self._set_status(staged_id, AWAITING, expected=PENDING):
            return GateDecision(
                blocked=True,
                message="This memory write is already being processed by another approval.",
            ), None
        try:
            response = await self._replay(staged)
        except BaseException:
            await self._set_status(staged_id, PENDING, expected=AWAITING)
            raise
        if response is None:
            await self._set_status(staged_id, PENDING, expected=AWAITING)
            return GateDecision(
                blocked=True,
                message="Pending record has no pinned entry; refusing to re-resolve old_text.",
            ), None
        if not response.get("success"):
            # 保留 record：用户还能重新创建或明确 reject（票面 M6 第 2 条）。
            await self._set_status(staged_id, PENDING, expected=AWAITING)
            return GateDecision(
                blocked=True, message=str(response.get("error") or "Replay failed.")), None
        await self._set_status(staged_id, APPROVED, expected=AWAITING)
        await self._project(staged, trusted, response)
        return GateDecision(allow=True), response

    async def reject(
        self, staged_id: str, trusted: TrustedMemoryIdentity,
    ) -> GateDecision:
        """CAS PENDING→REJECTED；与 approve 抢同一条记录，谁先到谁赢。"""
        staged = await self.get(staged_id, trusted)
        if staged is None:
            return GateDecision(blocked=True, message="No such pending memory write.")
        if staged.status != PENDING:
            if staged.status == AWAITING:
                return GateDecision(
                    blocked=True,
                    message="This memory write is already being processed by another approval.",
                )
            return GateDecision(
                blocked=True, message=f"This memory write is already {staged.status}.")
        if not await self._set_status(staged_id, REJECTED, expected=PENDING):
            return GateDecision(
                blocked=True,
                message="This memory write is already being processed by another approval.",
            )
        return GateDecision(allow=True, message="Memory write rejected.")

    @retry_on_busy
    async def _set_status(
        self, staged_id: str, status: str, *, expected: str | None = None,
    ) -> bool:
        """状态转移；给了 ``expected`` 就是一次 CAS，返回是否抢到。

        进 ``AWAITING`` 时记租约起点 `claimed_at`，离开时清空——`pending()` 靠它判断
        一条 ``approving`` 行是被打断的僵尸还是正在重放的活行。
        """
        sql = "UPDATE memory_v2_staged SET status=?, claimed_at=? WHERE id=?"
        parameters: tuple[Any, ...] = (
            status, stamp() if status == AWAITING else None, staged_id)
        if expected is not None:
            sql += " AND status=?"
            parameters += (expected,)
        async with connect(self.database_path) as connection:
            cursor = await connection.execute(sql, parameters)
            await connection.commit()
            return cursor.rowcount == 1

    async def _project(
        self, staged: StagedWrite, trusted: TrustedMemoryIdentity, response: dict[str, Any],
    ) -> None:
        """#1437：审批重放落盘后补投影（与工具直写同一入口）。

        只在 approve **成功返回前**调用；失败只留 pending 账本与一条不含内容的 warning，
        绝不影响 `GateDecision(allow=True)`——markdown 已提交，重放不能因为派生层故障
        变成"没批准"。
        """
        if self.store is None:
            return
        payload = staged.payload
        target = str(payload["target"])
        try:
            await reprojection.apply_write(
                self.store, self.markdown, target, response, trusted,
                operations=payload.get("operations"),
                content=None if payload.get("action") == "remove" else payload.get("content"),
            )
        except Exception as error:  # noqa: BLE001 — 投影失败不得回滚已批准的写入
            logger.warning("staged projection failed (%s): %s", target, type(error).__name__)

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
        if len(pins) != len(operations):
            # 截断重放会让后面的操作静默套用别人的 pin：这条 record 只能拒绝。
            logger.warning("staged batch pin count mismatch (%d != %d)", len(pins), len(operations))
            return {
                "success": False,
                "error": "Pending record's pinned entries do not match its operations.",
            }
        pinned_ops = [
            {**op, **({"matched_entry": pin} if pin is not None else {})}
            for op, pin in zip(operations, pins)
        ]
        # 这些 pin 是 stage 时从磁盘钉住的全文（系统侧），只有重放通道认它们。
        return await self.markdown.apply_batch(target, pinned_ops, trusted_pins=True)


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
