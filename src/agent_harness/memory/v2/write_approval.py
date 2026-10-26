"""write_approval + entry pinning（#1353 M6；#1494 起审批态由文件 owner 承载）。

移植自 NousResearch/hermes-agent ``tools/write_approval.py``（commit
b324b1f7ed52369ab4bd3b1a03378729120b0f01，2026-10-02，MIT，Copyright (c) 2025
Nous Research）。上游是 409 行的通用门（memory 与 skills 两个子系统、磁盘
``pending/<subsystem>/<id>.json``、控制台行内提问）；本仓按单用户 + 单写入口收敛，
保留**语义**而不是形状。

# 三态 gate

``GateDecision(allow | blocked | stage)`` 恰好一个为真。``evaluate_gate`` 只产出
allow / stage——``blocked`` 是**审批结果**（用户明确拒绝）而不是门本身的判断，
由 `approve`/`reject` 返回。把两者混在门里会让"没人问过"和"用户说不"看起来一样。

# #1494 / ADR-0058 D5：非 SQLite 的审批态

``memory_v2_staged`` / ``memory_v2_blocked`` 两张表迁到
``<workspace>/memory-state/staged/`` 与 ``blocked/``：

- 一条待审/被拦写入一个 JSON 文件；**文件锁就是 CAS**——``approve``/``promote``
  在整段重放期间持有该文件的锁，并发的第二个审批者会阻塞到第一个落盘后，再看到
  终态并返回"已处理"。这替代了旧表的 ``status=approving`` 抢占中间态 + 租约回收：
  崩溃由内核释放锁，状态仍是 pending/blocked，重试即可。
- 重放**只经** `MemoryCommit`（ADR-0058 D2）：单条用 ``stgp_<staged id>``、
  blocked 用 ``blkp_<blocked id>_<index>`` 的确定性 operation id，审批重试因此
  是回执回放，不会重复追加/删除。
- 落盘后转 ``approved`` / ``promoted`` 是**文件写**；崩溃在内容已发布、状态未转
  之间，重试按同一 operation id 回放内容，再转状态——收敛且不重复。

# entry pinning（堵 TOCTOU）

stage 之前先 resolve：把 ``old_text`` **此刻**选中的条目全文存进 ``pinned_entry``。
审批重放时只接受全文完全一致的条目（`markdown_store.pinned_index`），找不到 → 拒绝且
**保留** record；没有 pin 的 legacy record 直接拒绝，绝不按 ``old_text`` 重搜——重搜
正是 TOCTOU 本体。batch 是**一个** pending record：整批的 pin 按 operations 顺序存成
JSON 数组，审批时整批重放，不逐条问。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from agent_harness.memory.v2 import reprojection
from agent_harness.memory.v2._sqlite import stamp, write_connection
from agent_harness.memory.v2.commit import MemoryCommit
from agent_harness.memory.v2.file_state import (
    MemoryStateRoot,
    async_file_lock,
    read_json,
    safe_id,
    write_json,
)
from agent_harness.memory.v2.origin import ORIGINS, current_origin
from agent_harness.memory.v2.store import (
    MemorySettingsV2,
    SqliteMemoryV2Store,
    _to_record,
)
from agent_harness.memory.v2.types import (
    MemoryDraftV2,
    MemoryStatus,
    TrustedMemoryIdentity,
)

logger = logging.getLogger(__name__)

#: 上游 ``CONFIG_KEY``；本仓落在 `memory-state/settings.json` 的 `write_approval`。
CONFIG_KEY = "write_approval"

#: ``status`` 的取值。approved/rejected 是**终态但保留文件**——审计与
#: 「拒绝时保留 record」都要求文件还在。
PENDING = "pending"
#: legacy 中间态（旧 SQLite 表可能残留）：读时按租约回收成 PENDING，不新写。
AWAITING = "approving"
APPROVED = "approved"
REJECTED = "rejected"

#: legacy `approving` 的回收租约（只用于读旧文件；新写入不再产生该状态）。
APPROVING_LEASE = timedelta(minutes=5)
APPROVING_LEASE_SECONDS = APPROVING_LEASE.total_seconds()

SCHEMA_VERSION = 1


def _identity(trusted: TrustedMemoryIdentity) -> str:
    """可信身份的规范化键。审批只对**同一个**身份可见，项目维度也参与。"""
    return json.dumps(
        {"tenant_id": trusted.tenant_id, "user_id": trusted.user_id,
         "project_id": trusted.project_id},
        sort_keys=True, separators=(",", ":"),
    )


def _identity_trusted(identity: str) -> TrustedMemoryIdentity:
    raw = json.loads(identity)
    return TrustedMemoryIdentity(
        tenant_id=raw["tenant_id"], user_id=raw["user_id"], project_id=raw.get("project_id"))


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

    ==================  ==================  =======
    write_approval      origin              decision
    ==================  ==================  =======
    关                  任一                allow
    开                  ``foreground``      stage
    开                  ``background_review``  stage
    ==================  ==================  =======

    两格同值不是抄漏：本仓没有可问的通道（见模块 docstring），前台与后台都只能
    stage。``blocked`` **永远不由这里产出**：它是审批结果（人工 approve/reject）。
    """
    source = current_origin() if origin is None else origin
    if source not in ORIGINS:
        raise ValueError(f"unknown memory write origin {source!r}")
    if not settings.write_approval:
        return GateDecision(allow=True)
    return GateDecision(stage=True, message=_staged_message())


def _staged_message() -> str:
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
    """``memory-state/staged/`` 的读写与重放。"""

    def __init__(
        self, root: MemoryStateRoot, commit: MemoryCommit,
        store: SqliteMemoryV2Store | None = None,
    ) -> None:
        self.root = root
        self.commit = commit
        self.store = store

    async def initialize(self) -> None:
        await asyncio.to_thread(self.root.ensure)

    # ------------------------------------------------------------------ 路径与读写

    def path_for(self, staged_id: str) -> Path:
        return self.root.staged_dir / f"{safe_id(staged_id, what='staged id')}.json"

    def _lock_path(self, staged_id: str) -> Path:
        return self.root.staged_dir / safe_id(staged_id, what="staged id")

    def _read(self, staged_id: str) -> dict[str, Any] | None:
        return read_json(self.path_for(staged_id))

    @staticmethod
    def _to_staged(payload: dict[str, Any]) -> StagedWrite:
        return StagedWrite(
            id=str(payload["id"]), payload=dict(payload.get("payload") or {}),
            pinned_entry=payload.get("pinned_entry"),
            status=str(payload.get("status")), created_at=str(payload.get("created_at")),
        )

    @staticmethod
    def _effective_status(payload: dict[str, Any], *, now: datetime) -> str:
        """legacy ``approving`` 超过租约 ⇒ 视作 PENDING（不写盘，只改读法）。"""
        status = str(payload.get("status"))
        if status != AWAITING:
            return status
        claimed = str(payload.get("claimed_at") or "")
        cutoff = datetime.fromtimestamp(
            now.timestamp() - APPROVING_LEASE_SECONDS, tz=UTC).isoformat()
        return PENDING if claimed <= cutoff else AWAITING

    def _scan(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        moment = now or datetime.now(UTC)
        directory = self.root.staged_dir
        if not directory.exists():
            return []
        found: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            payload = read_json(path)
            if payload is None:
                continue
            payload = {**payload, "status": self._effective_status(payload, now=moment)}
            found.append(payload)
        return found

    # ------------------------------------------------------------------ stage

    async def stage(
        self, payload: dict[str, Any], pins: list[str | None] | None,
        trusted: TrustedMemoryIdentity,
    ) -> StagedWrite:
        record_id = f"stg_{uuid4().hex}"
        created = stamp()
        document = {
            "schema": SCHEMA_VERSION,
            "id": record_id,
            "identity": _identity(trusted),
            "project_id": trusted.project_id,
            "payload": payload,
            "pinned_entry": pins,
            "status": PENDING,
            "claimed_at": None,
            "created_at": created,
        }

        await asyncio.to_thread(write_json, self.path_for(record_id), document)
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
            resolved = await self.commit.resolve_entry(
                target, payload.get("old_text") or "", action or "replace")
            if not resolved.get("success"):
                return None, resolved
            return [resolved["matched_entry"]], None
        resolved = await self.commit.resolve_batch_entries(target, operations)
        if not resolved.get("success"):
            return None, resolved
        return list(resolved["matched_entries"]), None

    # ------------------------------------------------------------------ 查询

    async def pending(self, trusted: TrustedMemoryIdentity) -> list[StagedWrite]:
        """本身份的待审写入（旧→新）。legacy ``approving`` 超租约按 PENDING 读。"""
        identity = _identity(trusted)
        rows = await asyncio.to_thread(self._scan)
        wanted = [
            self._to_staged(payload) for payload in rows
            if payload.get("identity") == identity and payload.get("status") == PENDING
        ]
        wanted.sort(key=lambda item: item.created_at)
        return wanted

    async def get(self, staged_id: str, trusted: TrustedMemoryIdentity) -> StagedWrite | None:
        payload = await asyncio.to_thread(self._read, staged_id)
        if payload is None or payload.get("identity") != _identity(trusted):
            return None
        return self._to_staged(
            {**payload, "status": self._effective_status(payload, now=datetime.now(UTC))})

    # ------------------------------------------------------------------ 裁决

    async def approve(
        self, staged_id: str, trusted: TrustedMemoryIdentity,
    ) -> tuple[GateDecision, dict[str, Any] | None]:
        """重放一条待审写入。成功 → 落盘 + 文件转 approved；pin 失效 → blocked 且**保留**。

        文件锁在整段重放期间持有：并发的第二个 approve 会等到第一个写完状态，然后
        看到 ``approved`` 并返回 "already approved"——不需要 ``approving`` 中间态。
        """
        async with async_file_lock(self._lock_path(staged_id)):
            payload = await asyncio.to_thread(self._read, staged_id)
            if payload is None or payload.get("identity") != _identity(trusted):
                return GateDecision(blocked=True, message="No such pending memory write."), None
            status = self._effective_status(payload, now=datetime.now(UTC))
            if status != PENDING:
                if status == AWAITING:
                    return GateDecision(
                        blocked=True,
                        message="This memory write is already being processed by another approval.",
                    ), None
                return GateDecision(
                    blocked=True, message=f"This memory write is already {status}."), None
            staged = self._to_staged({**payload, "status": status})
            response = await self._replay(staged)
            if response is None:
                return GateDecision(
                    blocked=True,
                    message="Pending record has no pinned entry; refusing to re-resolve old_text.",
                ), None
            if not response.get("success"):
                # 保留 record：用户还能重新创建或明确 reject（票面 M6 第 2 条）。
                return GateDecision(
                    blocked=True, message=str(response.get("error") or "Replay failed.")), None
            await asyncio.to_thread(
                write_json, self.path_for(staged_id),
                {**payload, "status": APPROVED, "claimed_at": None,
                 "disposed_at": stamp()},
            )
            await self._project(staged, trusted, response)
            return GateDecision(allow=True), response

    async def reject(
        self, staged_id: str, trusted: TrustedMemoryIdentity,
    ) -> GateDecision:
        """CAS PENDING→REJECTED；与 approve 抢同一条记录，谁先到谁赢（文件锁）。"""
        async with async_file_lock(self._lock_path(staged_id)):
            payload = await asyncio.to_thread(self._read, staged_id)
            if payload is None or payload.get("identity") != _identity(trusted):
                return GateDecision(blocked=True, message="No such pending memory write.")
            status = self._effective_status(payload, now=datetime.now(UTC))
            if status != PENDING:
                if status == AWAITING:
                    return GateDecision(
                        blocked=True,
                        message="This memory write is already being processed by another approval.",
                    )
                return GateDecision(
                    blocked=True, message=f"This memory write is already {status}.")
            await asyncio.to_thread(
                write_json, self.path_for(staged_id),
                {**payload, "status": REJECTED, "claimed_at": None, "disposed_at": stamp()},
            )
            return GateDecision(allow=True, message="Memory write rejected.")

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
                self.store, self.commit.engine, target, response, trusted,
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
        operation_id = f"stgp_{staged.id}"
        if operations is None:
            action = payload.get("action")
            if action == "add":
                return await self.commit.add(
                    target, payload.get("content") or "", operation_id=operation_id)
            if pins is None or not pins or pins[0] is None:
                return None
            if action == "replace":
                return await self.commit.replace(
                    target, payload.get("old_text") or "", payload.get("content") or "",
                    matched_entry=pins[0], operation_id=operation_id)
            return await self.commit.remove(
                target, payload.get("old_text") or "", matched_entry=pins[0],
                operation_id=operation_id)
        if pins is None:
            # batch 里可能全是 add（无 pin 也安全）；只要有一条 replace/remove 就必须有 pin。
            if any(op.get("action") in ("replace", "remove") for op in operations):
                return None
            return await self.commit.apply_batch(
                target, operations, operation_id=operation_id)
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
        return await self.commit.apply_batch(
            target, pinned_ops, trusted_pins=True, operation_id=operation_id)


# --------------------------------------------------------------------------------------
# #1461：后台 formation 被拦下的写入（第二段处置通道）
# --------------------------------------------------------------------------------------

#: `blocked/<id>.json` 的 ``status`` 取值。blocked 是唯一可处置态，另两个是终态。
BLOCKED = "blocked"
PROMOTED = "promoted"
DISCARDED = "discarded"

#: 文件上的固定原因码；同时也是 job 终态的 `reason`（两处同值，便于对账）。
BLOCKED_REASON = "write_approval_blocked"

#: promote 时 pin 失配写进 `disposition_note` 的前缀（文件保持 blocked）。
DRIFT_PREFIX = "target_drift:"

SCHEMA_VERSION_BLOCKED = 1


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
    """处置结果：`applied` 为真时 `records` 是这次真正落盘的记录。"""

    applied: bool
    reason: str
    records: tuple[Any, ...] = ()
    invalidated: tuple[Any, ...] = ()
    superseded: tuple[Any, ...] = ()


def blocked_id_for(identity: str, content_hash: str) -> str:
    """同 identity + 同内容 ⇒ 同一个 id（文件存在即已处置过，不再问第二遍）。"""
    digest = hashlib.sha256(f"{identity}\x00{content_hash}".encode()).hexdigest()[:32]
    return f"blk_{digest}"


class BlockedWriteStore:
    """``memory-state/blocked/`` 的读写与 promote/discard。

    `writer` 是 executor 用的同一个 `MemoryRecordWriter`（`MemoryV2Service` 实现），
    `commit` 是唯一内容写者，`store` 用来打开记录层事务做 pin 复核。
    """

    def __init__(
        self, root: MemoryStateRoot, writer: Any, commit: MemoryCommit,
        store: SqliteMemoryV2Store,
    ) -> None:
        self.root = root
        self.writer = writer
        self.commit = commit
        self.store = store

    async def initialize(self) -> None:
        await asyncio.to_thread(self.root.ensure)

    # ------------------------------------------------------------------ 路径

    @staticmethod
    def _blocked_id(identity: str, content_hash: str) -> str:
        return blocked_id_for(identity, content_hash)

    def path_for(self, blocked_id: str) -> Path:
        return self.root.blocked_dir / f"{safe_id(blocked_id, what='blocked id')}.json"

    def _lock_path(self, blocked_id: str) -> Path:
        return self.root.blocked_dir / safe_id(blocked_id, what="blocked id")

    def _read(self, blocked_id: str) -> dict[str, Any] | None:
        return read_json(self.path_for(blocked_id))

    @staticmethod
    def _to_blocked(payload: dict[str, Any]) -> BlockedWrite:
        return BlockedWrite(
            id=str(payload["id"]), job_id=str(payload["job_id"]),
            identity=str(payload["identity"]), project_id=payload.get("project_id"),
            origin=str(payload.get("origin") or ""), reason=str(payload.get("reason") or ""),
            payload=dict(payload.get("payload") or {}), status=str(payload.get("status")),
            disposition_note=payload.get("disposition_note"),
            created_at=str(payload.get("created_at")),
            disposed_at=payload.get("disposed_at"),
        )

    # ------------------------------------------------------------------ 第一段：落文件

    async def insert(
        self, *, job_id: str, identity: str, project_id: str | None, payload: dict[str, Any],
        now_stamp: str | None = None,
    ) -> str | None:
        """落一条 blocked；同一 identity 已有同内容（任意状态）⇒ 返回 `None`。

        查重不分 status：promoted / discarded 过的同内容已经有人下过结论，再插一条只会
        让同一个决定被重新问一遍（票面 §4.3）。确定性 id 让"查重 + 写入"是一次
        文件存在性判断，不需要先读全目录。
        """
        canonical = _canonical_payload(payload)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        blocked_id = self._blocked_id(identity, digest)
        document = {
            "schema": SCHEMA_VERSION_BLOCKED,
            "id": blocked_id,
            "job_id": job_id,
            "identity": identity,
            "project_id": project_id,
            "origin": "background_review",
            "reason": BLOCKED_REASON,
            "payload": payload,
            "content_hash": digest,
            "status": BLOCKED,
            "disposition_note": None,
            "created_at": now_stamp or stamp(),
            "disposed_at": None,
        }

        def _create() -> str | None:
            existing = read_json(self.path_for(blocked_id))
            if existing is not None:
                # 同一 job 重跑（记录已提交、终态未写的窗口）⇒ 回放自己的 id，
                # 终态里的 blocked_ids 与首次一致；不同 job 的同内容 ⇒ 已有人处置，
                # 不再问第二遍（票面 §4.3）。
                return blocked_id if existing.get("job_id") == job_id else None
            write_json(self.path_for(blocked_id), document)
            return blocked_id

        async with async_file_lock(self._lock_path(blocked_id)):
            return await asyncio.to_thread(_create)

    # ------------------------------------------------------------------ 查询

    def _scan(self) -> list[dict[str, Any]]:
        directory = self.root.blocked_dir
        if not directory.exists():
            return []
        found: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            payload = read_json(path)
            if payload is not None:
                found.append(payload)
        return found

    async def list_for(
        self, tenant_id: str, user_id: str, *, project_id: str | None = None,
    ) -> list[BlockedWrite]:
        """本体身份可见的行。identity 的解析只在这一处（串格式由 `_identity` 独占）。"""
        rows = await asyncio.to_thread(self._scan)
        wanted: list[BlockedWrite] = []
        for payload in rows:
            owner = _identity_trusted(str(payload["identity"]))
            if (owner.tenant_id, owner.user_id) != (tenant_id, user_id):
                continue
            if project_id is not None and payload.get("project_id") != project_id:
                continue
            wanted.append(self._to_blocked(payload))
        wanted.sort(key=lambda item: (item.created_at, item.id))
        return wanted

    async def get(self, blocked_id: str) -> BlockedWrite | None:
        payload = await asyncio.to_thread(self._read, blocked_id)
        return None if payload is None else self._to_blocked(payload)

    # ------------------------------------------------------------------ 第二段：处置

    async def promote(
        self, blocked_id: str, trusted: TrustedMemoryIdentity,
    ) -> BlockedDecision:
        """按 payload 重放一条被拦下的写入。

        顺序（ADR-0058 D8）：pin 复核 → 记录事务（确定性 id）→ 内容镜像（`blkp_`
        回执）→ 文件转 promoted。文件锁在整段期间持有；任一 pin 失配 ⇒ 整条拒绝
        （不逐条跳过：payload 是一组同 identity 的动作，只放行一部分会造出 block 时
        从未存在过的混合状态）。
        """
        async with async_file_lock(self._lock_path(blocked_id)):
            payload = await asyncio.to_thread(self._read, blocked_id)
            if payload is None:
                raise KeyError(blocked_id)
            owner = _identity_trusted(str(payload["identity"]))
            if (owner.tenant_id, owner.user_id) != (trusted.tenant_id, trusted.user_id):
                raise KeyError(blocked_id)
            if payload.get("status") != BLOCKED:
                return BlockedDecision(
                    applied=False, reason=f"这条写入已被处置（{payload.get('status')}）。")
            actions = list((payload.get("payload") or {}).get("actions") or [])
            async with write_connection(self.store.database_path) as connection:
                drift = await _pin_drift(connection, actions)
                if drift is not None:
                    await asyncio.to_thread(
                        self._set_note, blocked_id, payload, f"{DRIFT_PREFIX}{drift}")
                    return BlockedDecision(
                        applied=False,
                        reason=f"目标已漂移（{drift}），整条拒绝；记录保持 blocked。")
                records, superseded, invalidated = await self._replay_in(
                    connection, actions, owner, blocked_id=blocked_id)
            # 记录已提交；内容镜像失败只留痕（与 executor 的 `_apply` 同一语义）。
            await self._mirror(records, invalidated=(*invalidated, *superseded),
                               blocked_id=blocked_id)
            await asyncio.to_thread(write_json, self.path_for(blocked_id), {
                **payload, "status": PROMOTED, "disposition_note": None,
                "disposed_at": stamp(),
            })
            return BlockedDecision(
                applied=True, reason="已放行并落盘。", records=records,
                invalidated=invalidated, superseded=superseded)

    async def discard(
        self, blocked_id: str, trusted: TrustedMemoryIdentity,
    ) -> BlockedDecision:
        """明确丢弃：只改状态，零记录写入。"""
        async with async_file_lock(self._lock_path(blocked_id)):
            payload = await asyncio.to_thread(self._read, blocked_id)
            if payload is None:
                raise KeyError(blocked_id)
            owner = _identity_trusted(str(payload["identity"]))
            if (owner.tenant_id, owner.user_id) != (trusted.tenant_id, trusted.user_id):
                raise KeyError(blocked_id)
            if payload.get("status") != BLOCKED:
                return BlockedDecision(applied=False, reason="这条写入已被处置。")
            await asyncio.to_thread(write_json, self.path_for(blocked_id), {
                **payload, "status": DISCARDED, "disposed_at": stamp(),
            })
            return BlockedDecision(applied=True, reason="已丢弃，未写入任何记忆。")

    def _set_note(self, blocked_id: str, payload: dict[str, Any], note: str) -> None:
        write_json(self.path_for(blocked_id), {**payload, "disposition_note": note})

    async def _mirror(
        self, records: tuple[Any, ...], *, invalidated: tuple[Any, ...], blocked_id: str,
    ) -> None:
        """把已提交记录投影到 markdown（确定性 `blkp_` operation id，可回执回放）。"""
        for index, record in enumerate(invalidated, 1):
            target = self.writer.markdown_target(record)
            if not self.commit.engine.target_enabled(target):
                continue
            try:
                await self.commit.remove(
                    target, record.content, matched_entry=record.content,
                    operation_id=f"blkp_{blocked_id}_rm_{index}")
            except Exception as error:  # noqa: BLE001 — 派生投影失败不回滚记录
                logger.warning(
                    "blocked promote mirror failed (%s): %s",
                    type(error).__name__, record.id)
        for index, record in enumerate(records, 1):
            if record.status is not MemoryStatus.ACTIVE:
                continue
            target = self.writer.markdown_target(record)
            if not self.commit.engine.target_enabled(target):
                continue
            try:
                response = await self.commit.add(
                    target, record.content, operation_id=f"blkp_{blocked_id}_add_{index}")
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "blocked promote mirror failed (%s): %s",
                    type(error).__name__, record.id)
                continue
            if not response.get("success"):
                logger.warning("blocked promote mirror not applied: %s", record.id)

    async def _replay_in(
        self, connection: aiosqlite.Connection, actions: list[dict[str, Any]],
        owner: TrustedMemoryIdentity, *, blocked_id: str,
    ) -> tuple[tuple[Any, ...], tuple[Any, ...], tuple[Any, ...]]:
        """逐条重放 payload；`(新记录, 被取代旧行, 被撤回行)`。

        每条动作的 `memory_id` 由 `(blocked id, index)` 确定性派生：重试命中已有行
        就原样返回（AC4「同 key 只一次逻辑更新」）。
        """
        records: list[Any] = []
        superseded: list[Any] = []
        invalidated: list[Any] = []
        for index, item in enumerate(actions, 1):
            action = item["action"]
            memory_id = f"blkp-{blocked_id}-{index}"
            if action == "invalidate":
                record = await self.writer.invalidate_in(
                    connection, item["pin"]["memory_id"], owner)
                invalidated.append(record)
                records.append(record)
                continue
            draft = MemoryDraftV2.model_validate(item["draft"])
            if action == "add":
                records.append(await self.writer.create_in(
                    connection, draft, owner, memory_id=memory_id))
                continue
            previous_id = item["pin"]["memory_id"]
            previous = await _current_row(connection, previous_id)
            if previous is not None:
                superseded.append(
                    _to_record(previous).model_copy(
                        update={"status": MemoryStatus.SUPERSEDED}))
            records.append(await self.writer.update_in(
                connection, previous_id, draft, owner, new_memory_id=memory_id))
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
    """规范 JSON（`sort_keys`，紧凑分隔符）——`content_hash` 与文件文本共用它。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "BLOCKED",
    "BLOCKED_REASON",
    "CONFIG_KEY",
    "DISCARDED",
    "DRIFT_PREFIX",
    "PROMOTED",
    "BlockedDecision",
    "BlockedWrite",
    "BlockedWriteStore",
    "GateDecision",
    "StagedWrite",
    "StagedWriteStore",
    "evaluate_gate",
]
