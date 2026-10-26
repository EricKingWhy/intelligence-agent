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
  blocked 用 ``blkp_<blocked id>_apply`` 的确定性 operation id（一份多目标回执），
  审批重试因此是回执回放，不会重复追加/删除。
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
from agent_harness.memory.v2.commit import (
    MemoryBatchRejected,
    MemoryCommit,
    MemoryCommitError,
)
from agent_harness.memory.v2.file_state import (
    CorruptStateFile,
    MemoryStateRoot,
    StateFileError,
    async_file_lock,
    read_json,
    safe_id,
    write_json,
)
from agent_harness.memory.v2.origin import ORIGINS, current_origin
from agent_harness.memory.v2.receipts import ReceiptState
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
        try:
            payload = read_json(self.path_for(staged_id))
        except (CorruptStateFile, StateFileError) as error:
            # 坏文件不参与审批：抛出去会让整个待审列表不可用；当「读不到」处理并留证据。
            logger.warning("staged write unreadable (%s): %s", staged_id, error)
            return None
        if payload is None:
            return None
        try:
            self._to_staged(payload)
        except (KeyError, TypeError, ValueError) as error:
            # 合法 JSON 但结构坏（缺 id/status）：与坏 JSON 同语义，跳过而不是打崩审批面。
            logger.warning("staged write malformed (%s): %s", staged_id, error)
            return None
        return payload

    @staticmethod
    def _to_staged(payload: dict[str, Any]) -> StagedWrite:
        staged_id = str(payload["id"])
        status = str(payload.get("status"))
        body = payload.get("payload")
        if not isinstance(body, dict):
            raise TypeError(f"staged write {staged_id} payload is not an object")
        target = body.get("target")
        if target not in {"memory", "user"}:
            raise ValueError(f"staged write {staged_id} has unknown target {target!r}")
        operations = body.get("operations")
        if operations is None:
            if body.get("action") not in {"add", "replace", "remove"}:
                raise ValueError(
                    f"staged write {staged_id} has unknown action {body.get('action')!r}")
        elif not isinstance(operations, list) or not operations:
            raise TypeError(f"staged write {staged_id} has invalid operations")
        else:
            for operation in operations:
                if not isinstance(operation, dict) or operation.get("action") not in {
                    "add", "replace", "remove",
                }:
                    raise TypeError(
                        f"staged write {staged_id} has an invalid batch operation")
        pins = payload.get("pinned_entry")
        actions = (
            [str(operation.get("action")) for operation in operations]
            if operations is not None
            else [str(body.get("action"))]
        )
        if pins is not None:
            if not isinstance(pins, list):
                raise TypeError(f"staged write {staged_id} pinned_entry is not a list")
            if operations is None and len(pins) > 1:
                raise ValueError(f"staged write {staged_id} has too many pinned entries")
            if operations is not None and len(pins) != len(operations):
                raise ValueError(
                    f"staged write {staged_id} pinned entries do not match its operations")
            for action, pin in zip(actions, pins, strict=False):
                if action in {"replace", "remove"}:
                    # replace/remove 的 pin 是 stage 时钉住的完整条目；missing pin
                    # 只能回退到 old_text 重新定位，等于绕过审批的漂移边界。
                    if not isinstance(pin, str) or not pin.strip():
                        raise TypeError(
                            f"staged write {staged_id} {action} operation has no pinned entry")
                elif pin is not None:
                    raise TypeError(
                        f"staged write {staged_id} add operation must not carry a pinned entry")
        return StagedWrite(
            id=staged_id, payload=dict(body or {}),
            pinned_entry=pins,
            status=status, created_at=str(payload.get("created_at")),
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
            try:
                payload = read_json(path)
            except (CorruptStateFile, StateFileError) as error:
                logger.warning("unreadable staged write skipped (%s): %s", path.name, error)
                continue
            if payload is None:
                continue
            payload = {**payload, "status": self._effective_status(payload, now=moment)}
            try:
                self._to_staged(payload)
            except (KeyError, TypeError, ValueError) as error:
                logger.warning(
                    "malformed staged write skipped (%s): %s", path.name, error)
                continue
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
        self._to_staged(document)  # 坏 pin 不入盘，避免生成 pending 里看不见的记录
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
            try:
                response = await self._replay(staged)
            except MemoryCommitError as error:
                # 发布阶段失败：record 保留，用户可按 operation id 对账后重试。
                return GateDecision(
                    blocked=True,
                    message=(
                        "Memory write did not commit; record kept for retry or reconcile "
                        f"(operation {error.operation_id})."),
                ), None
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                # 结构损坏（例如篡改过的 pinned_entry/operations）不得打崩审批入口。
                logger.warning(
                    "staged replay rejected (%s): %s", staged_id, type(error).__name__)
                return GateDecision(
                    blocked=True,
                    message=(
                        "Pending record is malformed; refusing to replay. "
                        "Reject it or recreate the write."),
                ), None
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
                response = await self.commit.replace(
                    target, payload.get("old_text") or "", payload.get("content") or "",
                    matched_entry=pins[0], operation_id=operation_id)
                return self._restore_replay_bodies(
                    response, action="replace", pins=pins)
            response = await self.commit.remove(
                target, payload.get("old_text") or "", matched_entry=pins[0],
                operation_id=operation_id)
            return self._restore_replay_bodies(response, action="remove", pins=pins)
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
        response = await self.commit.apply_batch(
            target, pinned_ops, trusted_pins=True, operation_id=operation_id)
        return self._restore_replay_bodies(
            response, operations=pinned_ops, pins=pins)

    @staticmethod
    def _restore_replay_bodies(
        response: dict[str, Any] | None, *, action: str | None = None,
        operations: list[dict[str, Any]] | None = None,
        pins: list[Any] | tuple[Any, ...] | None = None,
    ) -> dict[str, Any] | None:
        """终态回执的正文已 scrub；重放时用 stage 时的 pin 把它补回给派生层。

        `reprojection.apply_write` 需要被删/被替换的原文才能撤回派生行；回执只保留
        digest 是隐私要求，但 pin 本来就是系统侧在 stage 时钉住的全文。缺了这一步，
        审批重试会拿到「成功但没有正文」的回放响应，派生层就会残留已删除内容
        （review P2-1）。首次调用（非回放）响应本就带正文，``setdefault`` 不覆盖。
        """
        if not isinstance(response, dict) or not response.get("success"):
            return response
        if operations is None:
            key = {
                "remove": "removed_entry", "replace": "replaced_entry",
            }.get(action or "")
            if key is not None and pins and pins[0] is not None:
                response.setdefault(key, pins[0])
            return response
        removed = dict(response.get("removed_entries") or {})
        replaced = dict(response.get("replaced_entries") or {})
        for index, (operation, pin) in enumerate(
            zip(operations, pins or []), 1,
        ):
            if pin is None or not isinstance(operation, dict):
                continue
            if operation.get("action") == "remove":
                removed.setdefault(index, pin)
            elif operation.get("action") == "replace":
                replaced.setdefault(index, pin)
        if removed:
            response["removed_entries"] = removed
        if replaced:
            response["replaced_entries"] = replaced
        return response


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
        try:
            payload = read_json(self.path_for(blocked_id))
        except (CorruptStateFile, StateFileError) as error:
            logger.warning("blocked write unreadable (%s): %s", blocked_id, error)
            return None
        if payload is None:
            return None
        try:
            self._to_blocked(payload)
        except (KeyError, TypeError, ValueError) as error:
            logger.warning("blocked write malformed (%s): %s", blocked_id, error)
            return None
        return payload

    @staticmethod
    def _to_blocked(payload: dict[str, Any]) -> BlockedWrite:
        blocked_id = str(payload["id"])
        job_id = str(payload["job_id"])
        identity = str(payload["identity"])
        body = payload.get("payload")
        if not isinstance(body, dict):
            raise TypeError(f"blocked write {blocked_id} payload is not an object")
        actions = body.get("actions")
        if not isinstance(actions, list):
            raise TypeError(f"blocked write {blocked_id} has non-list actions")
        for action in actions:
            if not isinstance(action, dict):
                raise TypeError(f"blocked write {blocked_id} has a non-object action")
            kind = action.get("action")
            # formation 的动作词汇：新增 / 更新旧行 / 撤回旧行，与 `_replay_in` 一一对应。
            if kind not in {"add", "update", "invalidate"}:
                raise ValueError(
                    f"blocked write {blocked_id} has unknown action {kind!r}")
            pin = action.get("pin")
            if pin is not None:
                # `_pin_drift` / `_replay_in` 按这四个键直接索引 pin；形状只校验到
                # `dict` 会让 `{}` 在 promote 阶段变成未捕获的 KeyError（500），
                # 而不是「这条被拦写入不可重放，跳过」。字段校验必须在这里完成。
                _require_pin_shape(pin, blocked_id)
            if kind == "invalidate":
                if pin is None:
                    raise ValueError(
                        f"blocked write {blocked_id} invalidate action has no pin")
                continue
            if not isinstance(action.get("draft"), dict):
                raise TypeError(f"blocked write {blocked_id} action has no draft")
            if kind == "update" and pin is None:
                raise TypeError(
                    f"blocked write {blocked_id} update action has no pin")
        return BlockedWrite(
            id=blocked_id, job_id=job_id, identity=identity,
            project_id=payload.get("project_id"),
            origin=str(payload.get("origin") or ""), reason=str(payload.get("reason") or ""),
            payload=dict(body), status=str(payload.get("status")),
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
            try:
                payload = read_json(path)
            except (CorruptStateFile, StateFileError) as error:
                logger.warning("unreadable blocked write skipped (%s): %s", path.name, error)
                continue
            if payload is not None:
                try:
                    self._to_blocked(payload)
                except (KeyError, TypeError, ValueError) as error:
                    logger.warning(
                        "malformed blocked write skipped (%s): %s", path.name, error)
                    continue
                found.append(payload)
        return found

    async def list_for(
        self, tenant_id: str, user_id: str, *, project_id: str | None = None,
    ) -> list[BlockedWrite]:
        """本体身份可见的行。identity 的解析只在这一处（串格式由 `_identity` 独占）。"""
        rows = await asyncio.to_thread(self._scan)
        wanted: list[BlockedWrite] = []
        for payload in rows:
            try:
                owner = _identity_trusted(str(payload["identity"]))
            except (KeyError, TypeError, ValueError) as error:
                logger.warning("blocked write has bad identity, skipped: %s", error)
                continue
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

        顺序（ADR-0058 D8）：pin 复核 → records（未提交）→ 内容发布（`blkp_` 回执）→
        SQLite 提交 → 文件转 promoted。内容发布失败 ⇒ 记录事务回滚、文件保持 blocked，
        绝不宣称"已放行并落盘"。文件锁在整段期间持有；任一 pin 失配 ⇒ 整条拒绝
        （不逐条跳过：payload 是一组同 identity 的动作，只放行一部分会造出 block 时
        从未存在过的混合状态）。
        """
        async with async_file_lock(self._lock_path(blocked_id)):
            payload = await asyncio.to_thread(self._read, blocked_id)
            if payload is None:
                raise KeyError(blocked_id)
            try:
                owner = _identity_trusted(str(payload["identity"]))
            except (KeyError, TypeError, ValueError) as error:
                # 坏 identity 与「不是你的记录」同语义：拒绝处置而不是 500（review P3-4c）。
                raise KeyError(blocked_id) from error
            if (owner.tenant_id, owner.user_id) != (trusted.tenant_id, trusted.user_id):
                raise KeyError(blocked_id)
            if payload.get("status") != BLOCKED:
                return BlockedDecision(
                    applied=False, reason=f"这条写入已被处置（{payload.get('status')}）。")
            actions = list((payload.get("payload") or {}).get("actions") or [])
            operation_id = f"blkp_{blocked_id}_apply"
            receipt = self.commit.inspect_receipt(operation_id)
            if receipt is not None and receipt.state == ReceiptState.NEED_RECONCILE:
                await asyncio.to_thread(
                    self._set_note, blocked_id, payload,
                    f"needs_reconcile: {receipt.reason}")
                return BlockedDecision(
                    applied=False,
                    reason=(
                        "内容回执处于 NEED_RECONCILE，整条拒绝；记录保持 blocked。"
                        f"（operation {operation_id}: {receipt.reason}）"))
            try:
                async with write_connection(self.store.database_path) as connection:
                    already_applied = (
                        receipt is not None
                        and receipt.state == ReceiptState.COMMITTED
                        and await _records_already_applied(
                            connection, actions, blocked_id=blocked_id))
                    # records 已提交后，pin 必然被本次 promote 自己的更新改写：
                    # 这时的“漂移”是预期的，不能把已生效的写入永久卡在 blocked。
                    drift = (
                        None if already_applied
                        else await _pin_drift(connection, actions))
                    if drift is not None:
                        await asyncio.to_thread(
                            self._set_note, blocked_id, payload, f"{DRIFT_PREFIX}{drift}")
                        return BlockedDecision(
                            applied=False,
                            reason=f"目标已漂移（{drift}），整条拒绝；记录保持 blocked。")
                    records, superseded, invalidated = await self._replay_in(
                        connection, actions, owner, blocked_id=blocked_id)
                    # 内容在 records 事务**提交前**发布：失败 ⇒ 整个事务回滚。
                    await self._mirror(records, invalidated=(*invalidated, *superseded),
                                       blocked_id=blocked_id)
            except (MemoryCommitError, ValueError, KeyError, TypeError) as error:
                logger.warning(
                    "blocked promote publish failed (%s): %s",
                    type(error).__name__, blocked_id)
                operation = getattr(error, "operation_id", None)
                suffix = (
                    f" 回执 {operation} 记录了已发布/未发布的目标。"
                    if operation else "")
                return BlockedDecision(
                    applied=False,
                    reason=(
                        "内容发布失败，记录已回滚；这条写入仍保持 blocked，可重试。"
                        + suffix))
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
            try:
                owner = _identity_trusted(str(payload["identity"]))
            except (KeyError, TypeError, ValueError) as error:
                raise KeyError(blocked_id) from error
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
        """把记录投影到 markdown（确定性 `blkp_` operation id，可回执回放）。

        调用点在 records 事务提交前；失败必须上抛让事务回滚。所有目标共用**一份**
        `blkp_` 回执并先做全量预检：任何一条不合格都在写第一个文件之前拒绝；
        预检后某一目标发布失败时，已发布目标也留在同一份回执里供重试/对账
        （review P1-2）。
        """
        commit = self.commit
        operations: dict[str, list[dict[str, Any]]] = {"memory": [], "user": []}
        for record in invalidated:
            target = self.writer.markdown_target(record)
            if commit.engine.target_enabled(target):
                operations[target].append({
                    "action": "remove", "old_text": record.content,
                    "matched_entry": record.content, "missing_ok": True,
                })
        for record in records:
            if record.status is not MemoryStatus.ACTIVE:
                continue
            target = self.writer.markdown_target(record)
            if commit.engine.target_enabled(target):
                operations[target].append({"action": "add", "content": record.content})
        plans = [(target, ops) for target, ops in operations.items() if ops]
        if not plans:
            return
        try:
            response = await commit.apply_batch_many(
                plans, trusted_pins=True, allow_empty=True,
                operation_id=f"blkp_{blocked_id}_apply",
            )
        except MemoryBatchRejected as error:
            raise ValueError(f"markdown preflight rejected: {error.reason}") from error
        if not response.get("success"):
            raise ValueError(str(response.get("error") or "markdown apply failed"))

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


async def _records_already_applied(
    connection: aiosqlite.Connection, actions: list[dict[str, Any]], *, blocked_id: str,
) -> bool:
    """promote 的 records 事务是否已经提交（用于识别「内容已发布、状态未写」的窗口）。

    每条动作的 records 结果都是确定性的：add/update 落在 `blkp-<id>-<index>`，
    invalidate 落在 pin 指向的现有行上。全部可见 ⇒ 事务已提交；否则（含部分可见）
    按未提交处理，仍走 pin 复核，避免跳过真正的并发漂移。
    """
    if not actions:
        return False
    for index, item in enumerate(actions, 1):
        action = item.get("action")
        if action == "invalidate":
            pin = item.get("pin") or {}
            row = await _current_row(connection, pin.get("memory_id"))
            if row is None or row["status"] != MemoryStatus.INVALIDATED.value:
                return False
            continue
        if action not in {"add", "update"}:
            return False
        if await _current_row(connection, f"blkp-{blocked_id}-{index}") is None:
            return False
    return True


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


def _require_pin_shape(pin: Any, blocked_id: str) -> None:
    """校验 pin 的**可重放形状**（`_pin_drift` / `_replay_in` 的直接索引面）。"""
    if not isinstance(pin, dict):
        raise TypeError(f"blocked write {blocked_id} pin is not an object")
    for field in ("memory_id", "root_id", "updated_at"):
        value = pin.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"blocked write {blocked_id} pin has invalid {field}: {value!r}")
    version = pin.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise TypeError(
            f"blocked write {blocked_id} pin has invalid version: {version!r}")


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
