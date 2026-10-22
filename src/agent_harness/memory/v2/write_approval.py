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
  ``agent-harness memory pending|approve|reject`` 或 ``/api/memory-pending*``。

  **审批回调不接 `approval_queue`（追认偏离）**：#1438 已就此项作出裁决（用户
  2026-10-21 评论，issue #1438）——不强接队列，理由是上面那条作用域约束仍然成立，
  且 M6 自建的 ``memory_v2_staged`` 表已经承载了审批态与 pinning。该偏离已登记于
  #1353 `issuecomment-6315586413`。

# entry pinning（堵 TOCTOU）

stage 之前先 resolve：把 ``old_text`` **此刻**选中的条目全文存进
``memory_v2_staged.pinned_entry``。审批重放时只接受全文完全一致的条目
（`markdown_store.pinned_index`），找不到 → 拒绝且**保留** record；没有 pin 的
legacy record 直接拒绝，绝不按 ``old_text`` 重搜——重搜正是 TOCTOU 本体。

batch 是**一个** pending record：整批的 pin 按 operations 顺序存成一个 JSON 数组，
审批时整批重放，不逐条问。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from agent_harness.memory.v2._sqlite import connect, stamp
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.origin import BACKGROUND_REVIEW, ORIGINS, current_origin
from agent_harness.memory.v2.store import BLOCKED_SCHEMA, STAGED_SCHEMA, MemorySettingsV2
from agent_harness.memory.v2.types import (
    MemoryDraftV2,
    MemoryRecordV2,
    MemoryStatus,
    TrustedMemoryIdentity,
)
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


def evaluate_gate(settings: MemorySettingsV2, origin: str | None = None) -> GateDecision:
    """``memory`` 写入的门：gate 关 → allow；gate 开 → 按写入来源分流（#1438）。

    分流矩阵（票面 §5.1；``origin`` 省略时取 `origin.current_origin()`）：

    ==================  ==================  =======
    write_approval      origin              decision
    ==================  ==================  =======
    关                  任一                allow
    开                  ``foreground``      stage
    开                  ``background_review``  stage
    ==================  ==================  =======

    两格同值不是抄漏：本仓没有可问的通道（见模块 docstring），前台与后台都只能
    stage。矩阵留在这里是因为「按 origin 分流」是票面 M6 的语义契约——将来真有
    可问通道时，改的只是 ``foreground`` 那一格。``blocked`` **永远不由这里产出**：
    它是审批结果（人工 approve/reject），把"没人问过"和"用户说不"混在一起就分不清了。

    消费者只有 ``MemoryTool._gate``（前台 ``memory`` 工具）。后台 formation 走
    executor 自己的分流层（``executor._apply_blocked``，``#1461``）：那一层只读
    ``settings.write_approval`` 这个**布尔**、**不调用**本函数，gate 开时在同一笔
    ``commit_with_outcome`` 事务里落 blocked 行 + job ``outcome=BLOCKED``——所以
    "job 终态与副作用一起落地"（AC6/AC7）仍然成立，只是副作用从记录行换成了待办行。
    后台仍然**打标**（``BACKGROUND_REVIEW``）：标是给软校验和将来策略用的。

    本函数永不产出 ``blocked`` 的契约不变——那是**审批结果**（人工 promote/discard），
    与"没人问过"是两件事。
    """
    source = current_origin() if origin is None else origin
    if source not in ORIGINS:
        # 打标写错字必须响亮：静默 stage 会把"来源不明"伪装成"来源合法"。
        raise ValueError(f"unknown memory write origin {source!r}")
    if not settings.write_approval:
        return GateDecision(allow=True)
    return GateDecision(stage=True, message=_staged_message())


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

    def __init__(self, database_path: Path, markdown: MarkdownMemoryStore) -> None:
        self.database_path = Path(database_path)
        self.markdown = markdown

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


# --------------------------------------------------------------------------------------
# #1461：后台 formation 被拦下的写入（第二段处置通道）
# --------------------------------------------------------------------------------------

#: `memory_v2_blocked.status` 的取值。`blocked` 是唯一可处置态，另两个是终态。
BLOCKED = "blocked"
PROMOTED = "promoted"
DISCARDED = "discarded"

#: 行上的固定原因码；同时也是 job 终态的 `reason`（两处同值，便于对账）。
BLOCKED_REASON = "write_approval_blocked"

#: promote 时 pin 失配写进 `disposition_note` 的前缀（行保持 blocked）。
DRIFT_PREFIX = "target_drift:"


@dataclass(frozen=True)
class BlockedWrite:
    """一条被拦下的写入。`payload` 是 §3 的形状：`{"actions": [...]}`。"""

    id: str
    job_id: str
    identity: str
    project_id: str | None
    origin: str
    reason: str
    payload: dict[str, Any]
    status: str
    disposition_note: str | None
    created_at: str
    disposed_at: str | None


@dataclass(frozen=True)
class BlockedDecision:
    """处置结果：`applied` 为真时 `records` 是这次真正落盘的记录。

    `reason` 是**给用户看的**业务结论（已处置 / pin 冲突），不是异常：promote/discard
    的拒绝和 approve/reject 一样是业务结果，入口照常打印并退 0。行不存在（或不是本
    身份的行）**不**走这里，而是 `KeyError`——那是"这条 id 不存在"的语义，入口转 404。
    """

    applied: bool
    reason: str
    records: tuple[Any, ...] = ()
    invalidated: tuple[Any, ...] = ()
    superseded: tuple[Any, ...] = ()


class BlockedWriteStore:
    """``memory_v2_blocked`` 的读写与 promote/discard。

    `writer` 就是 executor 用的同一个 `MemoryRecordWriter`（`MemoryV2Service` 实现）。
    这里刻意**不** import `executor`：`executor` 要 import 本模块的 `_identity` 做分组，
    反向 import 会成环，所以参数按结构类型标注而不是那个 Protocol。
    """

    def __init__(self, database_path: Path, writer: Any) -> None:
        self.database_path = Path(database_path)
        self.writer = writer

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with connect(self.database_path) as connection:
            await connection.executescript(BLOCKED_SCHEMA)
            await connection.commit()

    # ------------------------------------------------------------------ 第一段：落行

    async def insert_in(
        self, connection: aiosqlite.Connection, *,
        job_id: str, identity: str, project_id: str | None, payload: dict[str, Any],
        now_stamp: str | None = None,
    ) -> str | None:
        """在**调用方的事务里**插一行；同一 identity 已有同内容 ⇒ 返回 `None`（不插）。

        查重不分 status：promoted / discarded 过的同内容已经有人下过结论，再插一行只会
        让同一个决定被重新问一遍（票面 §4.3）。
        """
        canonical = _canonical_payload(payload)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        async with connection.execute(
            "SELECT 1 FROM memory_v2_blocked WHERE identity=? AND content_hash=? LIMIT 1",
            (identity, digest),
        ) as cursor:
            if await cursor.fetchone() is not None:
                return None
        blocked_id = f"blk_{uuid4().hex}"
        await connection.execute(
            "INSERT INTO memory_v2_blocked "
            "(id, job_id, identity, project_id, origin, reason, payload, content_hash, "
            " status, disposition_note, created_at, disposed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
            (blocked_id, job_id, identity, project_id, BACKGROUND_REVIEW, BLOCKED_REASON,
             canonical, digest, BLOCKED, now_stamp or stamp()),
        )
        return blocked_id

    # ------------------------------------------------------------------ 查询

    async def list_for(
        self, tenant_id: str, user_id: str, *, project_id: str | None = None,
    ) -> list[BlockedWrite]:
        """本体身份可见的行。identity 的解析只在这一处（串格式由 `_identity` 独占）。"""
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_blocked ORDER BY created_at, id") as cursor,
        ):
            rows = await cursor.fetchall()
        wanted = (tenant_id, user_id, project_id)
        return [
            _to_blocked(row) for row in rows
            if _identity_owner(row["identity"])[:2] == wanted[:2]
            and (project_id is None or row["project_id"] == project_id)
        ]

    async def get(self, blocked_id: str) -> BlockedWrite | None:
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_blocked WHERE id=?", (blocked_id,)) as cursor,
        ):
            row = await cursor.fetchone()
        return None if row is None else _to_blocked(row)

    # ------------------------------------------------------------------ 第二段：处置

    async def promote(
        self, blocked_id: str, trusted: TrustedMemoryIdentity,
    ) -> BlockedDecision:
        """按 payload 重放一条被拦下的写入。

        三步都在**一笔**事务里：pin 复核（读库内**当前**行，不看 block 时的快照）→
        逐条落盘 → 行转 promoted。任一 pin 失配 ⇒ 整条拒绝（不逐条跳过：payload 是一组
        同 identity 的动作，只放行其中一部分会造出 block 时从未存在过的混合状态）。
        """
        row = await self._owned(blocked_id, trusted)
        if isinstance(row, BlockedDecision):
            return row
        payload = json.loads(row["payload"])
        actions = payload.get("actions") or []
        owner = _identity_trusted(row["identity"])
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                drift = await _pin_drift(connection, actions)
                if drift is None:
                    records, superseded, invalidated = await self._replay_in(
                        connection, actions, owner)
                    await connection.execute(
                        "UPDATE memory_v2_blocked SET status=?, disposed_at=?, "
                        "disposition_note=NULL WHERE id=? AND status=?",
                        (PROMOTED, stamp(), blocked_id, BLOCKED),
                    )
                    await connection.commit()
                else:
                    await connection.rollback()
            except BaseException:
                await connection.rollback()
                raise
        if drift is not None:
            await self._note(blocked_id, f"{DRIFT_PREFIX}{drift}")
            return BlockedDecision(
                applied=False,
                reason=f"目标已漂移（{drift}），整条拒绝；记录保持 blocked。",
            )
        # 提交后再补 markdown 权威面：与 executor 的 `_apply` 提交后镜像同一语义。
        await self.writer.mirror_committed(records, invalidated=(*invalidated, *superseded))
        return BlockedDecision(
            applied=True, reason="已放行并落盘。", records=records,
            invalidated=invalidated, superseded=superseded)

    async def discard(
        self, blocked_id: str, trusted: TrustedMemoryIdentity,
    ) -> BlockedDecision:
        """明确丢弃：只改状态，零记录写入。"""
        row = await self._owned(blocked_id, trusted)
        if isinstance(row, BlockedDecision):
            return row
        async with connect(self.database_path) as connection:
            cursor = await connection.execute(
                "UPDATE memory_v2_blocked SET status=?, disposed_at=? "
                "WHERE id=? AND status=?",
                (DISCARDED, stamp(), blocked_id, BLOCKED),
            )
            await connection.commit()
            if cursor.rowcount != 1:
                return BlockedDecision(applied=False, reason="这条写入已被处置。")
        return BlockedDecision(applied=True, reason="已丢弃，未写入任何记忆。")

    async def _owned(
        self, blocked_id: str, trusted: TrustedMemoryIdentity,
    ) -> Any | BlockedDecision:
        """取行并核对身份；返回行，或一个"不能处置"的结论。

        行不存在与"不是你的"都抛 `KeyError`（入口转 404）：两者必须同形，否则
        404/403 的差别会泄露别人的 id 是否存在。**已处置**是另一个结论——行在、
        只是没有待办可做（入口 409）。
        """
        row = await self._row(blocked_id)
        if row is None:
            raise KeyError(blocked_id)
        owner = _identity_trusted(row["identity"])
        if (owner.tenant_id, owner.user_id) != (trusted.tenant_id, trusted.user_id):
            raise KeyError(blocked_id)
        if row["status"] != BLOCKED:
            return BlockedDecision(
                applied=False, reason=f"这条写入已被处置（{row['status']}）。")
        return row

    async def _row(self, blocked_id: str) -> Any | None:
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_blocked WHERE id=?", (blocked_id,)) as cursor,
        ):
            return await cursor.fetchone()

    @retry_on_busy
    async def _note(self, blocked_id: str, note: str) -> None:
        async with connect(self.database_path) as connection:
            await connection.execute(
                "UPDATE memory_v2_blocked SET disposition_note=? WHERE id=?",
                (note, blocked_id),
            )
            await connection.commit()

    async def _replay_in(
        self, connection: aiosqlite.Connection, actions: list[dict[str, Any]],
        owner: TrustedMemoryIdentity,
    ) -> tuple[tuple[Any, ...], tuple[Any, ...], tuple[Any, ...]]:
        """逐条重放 payload；`(新记录, 被取代旧行, 被撤回行)`。"""
        records: list[Any] = []
        superseded: list[Any] = []
        invalidated: list[Any] = []
        for item in actions:
            action = item["action"]
            if action == "invalidate":
                record = await self.writer.invalidate_in(
                    connection, item["pin"]["memory_id"], owner)
                invalidated.append(record)
                records.append(record)
                continue
            draft = MemoryDraftV2.model_validate(item["draft"])
            if action == "add":
                records.append(await self.writer.create_in(connection, draft, owner))
                continue
            previous_id = item["pin"]["memory_id"]
            previous = await _current_row(connection, previous_id)
            if previous is None:
                raise KeyError(previous_id)
            superseded.append(
                MemoryRecordV2.model_validate(previous).model_copy(
                    update={"status": MemoryStatus.SUPERSEDED}))
            records.append(
                await self.writer.update_in(connection, previous_id, draft, owner))
        return tuple(records), tuple(superseded), tuple(invalidated)


async def _pin_drift(
    connection: aiosqlite.Connection, actions: list[dict[str, Any]],
) -> str | None:
    """逐条比对 pin 与库内**当前**行；返回失配的 memory_id，全合返回 `None`。"""
    for item in actions:
        pin = item.get("pin")
        if pin is None:
            continue
        current = await _current_row(connection, pin["memory_id"])
        if current is None:
            return pin["memory_id"]
        if (current["root_id"] != pin["root_id"]
                or current["version"] != pin["version"]
                or current["updated_at"] != pin["updated_at"]):
            return pin["memory_id"]
    return None


async def _current_row(connection: aiosqlite.Connection, memory_id: str) -> Any | None:
    """事务内按 id 读当前记录行（promote 的 pin 复核只认库内现状，不认快照）。"""
    async with connection.execute(
        "SELECT * FROM memory_v2_records WHERE memory_id=?", (memory_id,)) as cursor:
        return await cursor.fetchone()


def _canonical_payload(payload: dict[str, Any]) -> str:
    """规范 JSON（`sort_keys`，紧凑分隔符）——`content_hash` 与入库文本共用它。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identity_owner(identity: str) -> tuple[str, str, str | None]:
    """`_identity` 的逆：只在本模块解析那个串格式。"""
    raw = json.loads(identity)
    return raw["tenant_id"], raw["user_id"], raw["project_id"]


def _identity_trusted(identity: str) -> TrustedMemoryIdentity:
    tenant_id, user_id, project_id = _identity_owner(identity)
    return TrustedMemoryIdentity(tenant_id, user_id, project_id)


def _to_blocked(row: Any) -> BlockedWrite:
    return BlockedWrite(
        id=row["id"], job_id=row["job_id"], identity=row["identity"],
        project_id=row["project_id"], origin=row["origin"], reason=row["reason"],
        payload=json.loads(row["payload"]), status=row["status"],
        disposition_note=row["disposition_note"], created_at=row["created_at"],
        disposed_at=row["disposed_at"],
    )


__all__ = [
    "BLOCKED",
    "BLOCKED_REASON",
    "CONFIG_KEY",
    "DISCARDED",
    "DRIFT_PREFIX",
    "GateDecision",
    "PROMOTED",
    "BlockedDecision",
    "BlockedWrite",
    "BlockedWriteStore",
    "StagedWrite",
    "StagedWriteStore",
    "evaluate_gate",
]
