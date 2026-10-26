"""#1494 / ADR-0058 D7：tombstone 的文件 owner（`memory-state/tombstones/`）。

# 职责与判据

ADR-0056 §3.5：墓碑是**删除抑制事实**——删掉一条记忆之后，旧输入（同正文、同来源事件）
的重放必须继续被拒绝；墓碑在既有 30 天过期策略前不得清理；碎片不足时保留原状、不编造。
本模块只存 content-free 的删除标记（正文/来源的 sha256），不存正文。

# 为什么一记忆一文件

墓碑是**按 memory_id 判定**的事实，读取路径要么按 id 取（`get` / `delete` 重入），要么
按所有者列（CLI / API），要么按 (identity, scope, content/source hash) 判重放。前三种都
能从文件名或一次目录扫描回答；一个文件一行让"写一条墓碑"退化成一次原子替换，不需要
再次引入一张被 ADR-0056 判走的 SQLite 表（红线：操作状态终态离开 SQLite）。

# 与删除事务的关系（ADR-0058 D8）

删除的持久顺序是 **墓碑 → 内容 → 记录**：

1. 墓碑先落盘：崩溃在它之后，旧输入已经不可能被重放（删除抑制先于删除本身生效）；
2. 内容经 `MemoryCommit.remove_by_digest`（`del_<memory_id>` 回执）移除；
3. 记录/outbox 在 SQLite 事务里删（派生态，可由墓碑与内容重建）。

任一步崩溃后重跑同一次删除都是幂等的：墓碑按 memory_id 覆盖写、内容按 digest 零命中、
记录删除按 root_id 重复执行。**不冒称**三步跨载体原子——墓碑存在但记录还在的窗口里，
读侧以墓碑为准（`blocked_replay` 与 `delete` 的重入分支都读它）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_harness.memory.v2.file_state import (
    CorruptStateFile,
    MemoryStateRoot,
    read_json,
    safe_id,
    write_json,
)
from agent_harness.memory.v2.markdown_store import entry_digest
from agent_harness.memory.v2.types import (
    MemoryScope,
    MemoryTombstoneV2,
    TrustedMemoryIdentity,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: 既有 30 天保留策略（旧 `store._delete_root_in` 的 `timedelta(days=30)`）。
RETENTION = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class TombstoneDraft:
    """写入墓碑所需的事实（由删除路径从记录行构造，不含正文）。"""

    memory_id: str
    root_id: str
    tenant_id: str
    user_id: str
    scope: MemoryScope
    project_id: str | None
    version: int | None
    content_hash: str
    source_hashes: tuple[str, ...]

    @classmethod
    def from_record(cls, row: Any) -> TombstoneDraft:
        source_ids = json.loads(row["source_event_ids"])
        return cls(
            memory_id=str(row["memory_id"]), root_id=str(row["root_id"]),
            tenant_id=str(row["tenant_id"]), user_id=str(row["user_id"]),
            scope=MemoryScope(row["scope"]), project_id=row["project_id"],
            version=row["version"], content_hash=entry_digest(str(row["content"])),
            source_hashes=tuple(entry_digest(str(value)) for value in source_ids),
        )


class FileTombstoneStore:
    """`memory-state/tombstones/<memory_id>.json` 的读写与重放判定。"""

    def __init__(self, root: MemoryStateRoot) -> None:
        self.root = root

    async def initialize(self) -> None:
        self.root.ensure()

    # ------------------------------------------------------------------ 路径

    @staticmethod
    def _stem(memory_id: str) -> str:
        """文件名 = memory_id（安全字符直用，否则取哈希）。两种映射都是确定性的。"""
        try:
            return safe_id(memory_id, what="memory id")
        except ValueError:
            return "h_" + hashlib.sha256(memory_id.encode("utf-8")).hexdigest()

    def path_for(self, memory_id: str) -> Path:
        return self.root.tombstones_dir / f"{self._stem(memory_id)}.json"

    # ------------------------------------------------------------------ 写

    def record(self, memories: Iterable[TombstoneDraft], *, reason: str,
               now: datetime | None = None) -> int:
        """写/覆盖墓碑（幂等）。返回写入条数。

        每条一次原子替换；同一 memory_id 的重复删除覆盖成最新一次删除的时间与理由
        （旧语义 `INSERT OR REPLACE` 逐字保留）。
        """
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        count = 0
        for item in memories:
            write_json(self.path_for(item.memory_id), {
                "schema": SCHEMA_VERSION,
                "memory_id": item.memory_id,
                "root_id": item.root_id,
                "tenant_id": item.tenant_id,
                "user_id": item.user_id,
                "scope": item.scope.value,
                "project_id": item.project_id,
                "version": item.version,
                "deleted_at": moment.isoformat(),
                "expires_at": (moment + RETENTION).isoformat(),
                "deletion_reason": reason,
                "content_hashes": [item.content_hash],
                "source_hashes": list(dict.fromkeys(item.source_hashes)),
            })
            count += 1
        return count

    async def record_async(
        self, memories: Iterable[TombstoneDraft], *, reason: str,
        now: datetime | None = None,
    ) -> int:
        return await asyncio.to_thread(self.record, list(memories), reason=reason, now=now)

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """删除已过期的墓碑文件；返回条数。"""
        cutoff = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        removed = 0
        directory = self.root.tombstones_dir
        if not directory.exists():
            return 0
        for path in directory.glob("*.json"):
            try:
                payload = read_json(path)
            except CorruptStateFile:
                # 坏文件不删：它可能是"删除抑制"的唯一证据，留给人工。
                logger.warning("unreadable tombstone left in place: %s", path.name)
                continue
            if payload is None:
                continue
            if str(payload.get("expires_at") or "") <= cutoff:
                try:
                    path.unlink()
                except OSError as error:
                    logger.warning("could not purge tombstone %s (%s)",
                                   path.name, type(error).__name__)
                    continue
                removed += 1
        return removed

    async def purge_expired_async(self, *, now: datetime | None = None) -> int:
        return await asyncio.to_thread(self.purge_expired, now=now)

    # ------------------------------------------------------------------ 读

    def _scan(self) -> list[dict[str, Any]]:
        directory = self.root.tombstones_dir
        if not directory.exists():
            return []
        found: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                payload = read_json(path)
            except CorruptStateFile:
                logger.warning("unreadable tombstone skipped: %s", path.name)
                continue
            if payload is not None:
                found.append(payload)
        return found

    def get_raw(self, memory_id: str) -> dict[str, Any] | None:
        try:
            return read_json(self.path_for(memory_id))
        except CorruptStateFile:
            return None

    async def get_raw_async(self, memory_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.get_raw, memory_id)

    @staticmethod
    def visible_payload(payload: dict[str, Any], trusted: TrustedMemoryIdentity,
                 *, now_stamp: str) -> bool:
        return (
            str(payload.get("expires_at") or "") > now_stamp
            and payload.get("tenant_id") == trusted.tenant_id
            and payload.get("user_id") == trusted.user_id
            and (payload.get("scope") != MemoryScope.PROJECT.value
                 or payload.get("project_id") == trusted.project_id)
        )

    @staticmethod
    def to_view(payload: dict[str, Any]) -> MemoryTombstoneV2:
        return MemoryTombstoneV2(
            id=str(payload["memory_id"]), root_id=str(payload["root_id"]),
            scope=MemoryScope(payload["scope"]), project_id=payload.get("project_id"),
            deleted_at=str(payload["deleted_at"]),
        )

    def visible(self, memory_id: str, trusted: TrustedMemoryIdentity,
                *, now: datetime | None = None) -> dict[str, Any] | None:
        payload = self.get_raw(memory_id)
        if payload is None:
            return None
        now_stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        return payload if self.visible_payload(payload, trusted, now_stamp=now_stamp) else None

    def list_for(
        self, trusted: TrustedMemoryIdentity, *, query: str | None = None,
        scope: MemoryScope | None = None, project_id: str | None = None,
        limit: int = 50, offset: int = 0, now: datetime | None = None,
    ) -> list[MemoryTombstoneV2]:
        if project_id is not None and project_id != trusted.project_id:
            raise PermissionError("project filter is not part of the trusted identity")
        if scope is MemoryScope.PROJECT and trusted.project_id is None:
            return []
        now_stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        needle = (query or "").lower()
        rows = [
            payload for payload in self._scan()
            if self.visible_payload(payload, trusted, now_stamp=now_stamp)
            and (scope is None or payload.get("scope") == scope.value)
            and (project_id is None or payload.get("project_id") == project_id)
            and (
                not needle
                or needle in str(payload.get("memory_id") or "").lower()
                or needle in str(payload.get("root_id") or "").lower()
            )
        ]
        rows.sort(
            key=lambda payload: (str(payload.get("deleted_at") or ""),
                                 str(payload.get("memory_id") or "")),
            reverse=True,
        )
        return [self.to_view(payload) for payload in rows[max(0, offset):][:max(0, limit)]]

    async def list_for_async(self, trusted: TrustedMemoryIdentity, **kwargs: Any):
        return await asyncio.to_thread(self.list_for, trusted, **kwargs)

    def versions(self, root_id: str, trusted: TrustedMemoryIdentity,
                 *, now: datetime | None = None) -> list[MemoryTombstoneV2]:
        now_stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        rows = [
            payload for payload in self._scan()
            if str(payload.get("root_id")) == root_id
            and self.visible_payload(payload, trusted, now_stamp=now_stamp)
        ]
        if not rows:
            raise KeyError(root_id)
        rows.sort(
            key=lambda payload: (
                int(payload.get("version") or 0),
                str(payload.get("deleted_at") or ""),
                str(payload.get("memory_id") or ""),
            ),
            reverse=True,
        )
        return [self.to_view(payload) for payload in rows]

    async def versions_async(self, root_id: str, trusted: TrustedMemoryIdentity):
        return await asyncio.to_thread(self.versions, root_id, trusted)

    def content_hashes(self, memory_id: str) -> tuple[str, ...]:
        payload = self.get_raw(memory_id)
        if payload is None:
            return ()
        return tuple(str(value) for value in (payload.get("content_hashes") or []))

    # ------------------------------------------------------------------ 重放抑制

    def blocks_replay(
        self, trusted: TrustedMemoryIdentity, *, scope: MemoryScope,
        project_id: str | None, content: str, source_event_ids: Iterable[str],
        now: datetime | None = None,
    ) -> bool:
        """同身份 + 同作用域下，正文或任一来源事件已被删除 ⇒ 拒绝这次创建。

        逐字保留旧 `_is_tombstoned_replay` 的判据（content_hash ∈ content_hashes 或
        source_hashes 有交集），只把载体从 SQLite 换成文件。扫描上界由 30 天保留策略
        给出；单机部署里是几十~几百个小文件，属于常数级。
        """
        now_stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        content_digest = entry_digest(content)
        sources = {entry_digest(value) for value in source_event_ids}
        for payload in self._scan():
            if not (
                payload.get("tenant_id") == trusted.tenant_id
                and payload.get("user_id") == trusted.user_id
                and payload.get("scope") == scope.value
                and payload.get("project_id") == project_id
                and str(payload.get("expires_at") or "") > now_stamp
            ):
                continue
            if content_digest in (payload.get("content_hashes") or []):
                return True
            if sources.intersection(payload.get("source_hashes") or []):
                return True
        return False


__all__ = ["RETENTION", "SCHEMA_VERSION", "FileTombstoneStore", "TombstoneDraft"]
