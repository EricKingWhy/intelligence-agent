"""MEM-V2-1 的 provider-neutral 记忆边界。

# 这个 Protocol 存在的意义（PRD §7.1 第 2 条 / §7.2 第 1 条）

记忆必须停在 Capability + Context Provider 的既有 seam 后面：AgentRuntime 不得长出
provider 专属分支。所以调用方（工具、API、后续的 formation 流水线）只依赖
`MemoryV2Capability` 这七个方法，而**不**知道背后是 SQLite 还是别的 provider。

`MemoryV2Service` 是当前唯一实现，但它不是"边界"本身：边界是 Protocol。
任何满足这七个方法的对象都可以替换它——包括测试替身与后续 ticket 引入的
非 LangMem provider。这就是"adapter boundary 得以保留"的可判定形态：
换实现不需要改调用方，也不需要改本模块的签名。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from typing import Protocol

import aiosqlite

from agent_harness.memory.v2.index import MemoryV2IndexRelay, MemoryV2VectorIndex
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.policy import find_secret
from agent_harness.memory.v2.recall import (
    RankedMemory,
    keyword_overlap,
    keyword_terms,
    rank_memory,
)
from agent_harness.memory.v2.store import (
    MemoryDeletionReceiptV2,
    MemorySettingsV2,
    SqliteMemoryV2Store,
)
from agent_harness.memory.v2.types import (
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryPayload,
    MemoryRecordV2,
    MemoryScope,
    MemoryStatus,
    MemoryTombstoneV2,
    SourceType,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.write_approval import StagedWriteStore

logger = logging.getLogger(__name__)


class MemoryV2Capability(Protocol):
    """V2 记忆的公开操作面（create / read / search / update / invalidate）。

    方法名刻意**不**照抄 `SqliteMemoryV2Store`（`read` ↔ `get`、`versions` ↔
    `list_versions`）：这个面描述的是"记忆操作"，store 描述的是"SQLite 行操作"。
    换掉底层实现时该改的只有组合实现（`MemoryV2Service`），不是调用方。
    """

    async def create(self, draft: MemoryDraftV2, trusted: TrustedMemoryIdentity) -> MemoryRecordV2: ...

    async def update(self, previous_id: str, draft: MemoryDraftV2,
                     trusted: TrustedMemoryIdentity) -> MemoryRecordV2: ...

    async def invalidate(self, memory_id: str, trusted: TrustedMemoryIdentity) -> MemoryRecordV2: ...

    async def read(self, memory_id: str, trusted: TrustedMemoryIdentity) -> MemoryRecordV2: ...

    async def list_active(
        self, trusted: TrustedMemoryIdentity, *, scope: MemoryScope, limit: int, offset: int = 0,
    ) -> list[MemoryRecordV2]: ...

    async def versions(self, root_id: str, trusted: TrustedMemoryIdentity) -> list[MemoryRecordV2]: ...

    async def search(
        self, query: str, trusted: TrustedMemoryIdentity, *, scope: MemoryScope, limit: int,
    ) -> list[MemoryRecordV2]: ...


class MemoryV2Service:
    """SQLite 权威 + 派生索引 + 检索权威复核的组合实现。

    `search` 刻意**不**顺手 flush 索引：写路径与索引收敛是两件事，混在一起会让
    "检索一次"变成"写一次数据库"（在并发下还会与 relay 抢锁）。索引由 relay 收敛，
    调用方看到的是"当前已收敛的可见记忆"——这与 V1 的既有语义一致（PRD §7.2 第 3 条
    要求复用既有 substrate 与其语义）。
    """

    def __init__(
        self, store: SqliteMemoryV2Store, index: MemoryV2VectorIndex,
        *, relay: MemoryV2IndexRelay | None = None,
        markdown: MarkdownMemoryStore | None = None,
        staged: StagedWriteStore | None = None,
    ) -> None:
        self._store = store
        self._index = index
        self._relay = relay
        #: #1353：markdown 权威面。为 `None` 时退化为纯 SQLite（既有测试与旧部署路径），
        #: 装配层注入后 create/update/invalidate 先落 markdown 再投影 SQLite。
        self._markdown = markdown
        #: #1353 M6：`write_approval` 门开时的待审区。跟着 markdown 走——没有权威面就没有
        #: 可审批的写入，工具也不会注册。
        self._staged = staged
        self._tombstone_purger: asyncio.Task | None = None

    def start_tombstone_purger(self, *, interval_seconds: float = 3600) -> None:
        """Purge expired tombstones hourly for long-lived processes."""
        if interval_seconds <= 0:
            raise ValueError("tombstone purge interval must be positive")
        if self._tombstone_purger is None or self._tombstone_purger.done():
            self._tombstone_purger = asyncio.create_task(
                self._purge_tombstones_loop(interval_seconds),
                name="memory-v2-tombstone-purger",
            )

    async def _purge_tombstones_loop(self, interval_seconds: float) -> None:
        while True:
            try:
                await self._store.purge_expired_tombstones()
            except Exception as error:  # noqa: BLE001 — a purge failure must not stop later runs.
                logger.warning("Memory V2 tombstone purge failed (%s)", type(error).__name__)
            await asyncio.sleep(interval_seconds)

    async def aclose(self) -> None:
        task = self._tombstone_purger
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._tombstone_purger = None

    async def create(self, draft: MemoryDraftV2, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        self._reject_secret(draft)
        await self._mirror_markdown(draft, previous_id=None)
        return await self._store.create(draft, trusted)

    async def update(self, previous_id: str, draft: MemoryDraftV2,
                     trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        self._reject_secret(draft)
        await self._mirror_markdown(draft, previous_id=previous_id, trusted=trusted)
        return await self._store.update(previous_id, draft, trusted)

    async def invalidate(self, memory_id: str, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        await self._mirror_markdown(None, previous_id=memory_id, trusted=trusted)
        return await self._store.invalidate(memory_id, trusted)

    # ----------------------------------------------------------------------------------
    # markdown 权威面（#1353 §2）
    # ----------------------------------------------------------------------------------
    #
    # 顺序是 **markdown 先、SQLite 后**：markdown 是权威，SQLite 是派生/历史层。SQLite 失败
    # 时 markdown 已落盘、调用方看到异常——这是"权威已改、投影未成"的正确方向（重放一次
    # 写入即可收敛，反方向会丢用户的记忆）。反过来（SQLite 先）一旦 markdown 写失败，
    # 权威面就永远缺这条事实，而派生层看起来一切正常。
    #
    # 事务内写（`*_in`）**不做** markdown 镜像：它们跑在 formation job 的事务里，markdown
    # 写无法参与那笔提交；由执行器在提交成功后调用 `mirror_committed` 补落（见 executor）。

    @property
    def markdown(self) -> MarkdownMemoryStore | None:
        return self._markdown

    @property
    def staged(self) -> StagedWriteStore | None:
        """#1353 M6 待审区；`None` = 这个部署没有审批面（门恒关）。"""
        return self._staged

    @staticmethod
    def markdown_target(record: MemoryDraftV2 | MemoryRecordV2) -> str:
        """分拣规则：`kind=user` 或 `tier=profile` → USER.md，其余 → MEMORY.md。

        与 §4 迁移的分拣**同一句话**——两条路分拣不一致，迁移后第一条自动写入就会把事实
        挪到另一个文件里，用户看到"刚写的记忆跑到别处去了"。
        """
        if record.tier.value == "profile" or record.kind.value == "user":
            return "user"
        return "memory"

    async def _mirror_markdown(
        self, draft: MemoryDraftV2 | None, *, previous_id: str | None = None,
        trusted: TrustedMemoryIdentity | None = None,
    ) -> None:
        store = self._markdown
        if store is None:
            return
        if draft is None:
            assert previous_id is not None and trusted is not None  # 调用方保证
            try:
                previous = await self._store.get(previous_id, trusted)
            except KeyError:
                return
            if not store.target_enabled(self.markdown_target(previous)):
                return
            await store.remove(
                self.markdown_target(previous), previous.content, matched_entry=previous.content,
            )
            return
        target = self.markdown_target(draft)
        if not store.target_enabled(target):
            return
        if previous_id is None:
            await store.add(target, draft.content)
            return
        assert trusted is not None  # 调用方保证
        try:
            previous = await self._store.get(previous_id, trusted)
        except KeyError:
            await store.add(target, draft.content)
            return
        previous_target = self.markdown_target(previous)
        if previous_target != target:
            # 分拣变了（tier/kind 被改）：先删旧文件里的条目，再写新文件。
            await store.remove(previous_target, previous.content, matched_entry=previous.content)
            await store.add(target, draft.content)
            return
        # `matched_entry` 钉住**审阅时的那条原文**：并发手改后条目已变，replace 会以
        # stale 失败而不是改到另一条上。stale 时退回 add（新内容仍应被记住）。
        response = await store.replace(
            target, previous.content, draft.content, matched_entry=previous.content,
        )
        if not response.get("success"):
            await store.add(target, draft.content)

    async def _mirror_deleted(self, memories: Sequence[MemoryRecordV2]) -> None:
        """硬删（delete / bulk_delete）后把条目从 markdown 移除。"""
        store = self._markdown
        if store is None:
            return
        for memory in memories:
            target = self.markdown_target(memory)
            if store.target_enabled(target):
                await store.remove(target, memory.content, matched_entry=memory.content)

    async def mirror_committed(
        self, records: Sequence[MemoryRecordV2], *, invalidated: Sequence[MemoryRecordV2] = (),
    ) -> None:
        """事务内写（formation job）**提交之后**补落 markdown。

        formation 的写入走 `create_in/update_in/invalidate_in`，它们与 job 终态在同一笔
        SQLite 事务里；markdown 写不可能参与那笔提交，所以只能在这里补。补写失败**不回滚**
        已提交的 SQLite（事实已经成立），只记一条告警：markdown 下一次读取/投影会收敛，
        而把已提交的事实回滚掉才是真的丢数据。
        """
        store = self._markdown
        if store is None:
            return
        for record in records:
            target = self.markdown_target(record)
            if store.target_enabled(target):
                try:
                    await store.add(target, record.content)
                except Exception as error:  # noqa: BLE001 — 见 docstring
                    logger.warning(
                        "markdown 投影失败（%s），SQLite 已提交：%s",
                        type(error).__name__, record.id,
                    )
        for record in invalidated:
            target = self.markdown_target(record)
            if store.target_enabled(target):
                try:
                    await store.remove(target, record.content, matched_entry=record.content)
                except Exception as error:  # noqa: BLE001 — 同上
                    logger.warning(
                        "markdown 撤回投影失败（%s），SQLite 已提交：%s",
                        type(error).__name__, record.id,
                    )

    async def list_records(
        self, trusted: TrustedMemoryIdentity, *, query: str | None = None,
        kind: MemoryKind | None = None, status: MemoryStatus | None = None,
        scope: MemoryScope | None = None, project_id: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[MemoryRecordV2]:
        return await self._store.list_records(
            trusted, query=query, kind=kind, status=status, scope=scope,
            project_id=project_id, limit=limit, offset=offset,
        )

    async def list_tombstones(
        self, trusted: TrustedMemoryIdentity, *, query: str | None = None,
        scope: MemoryScope | None = None, project_id: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[MemoryTombstoneV2]:
        return await self._store.list_tombstones(
            trusted, query=query, scope=scope, project_id=project_id,
            limit=limit, offset=offset,
        )

    async def read_tombstone(
        self, memory_id: str, trusted: TrustedMemoryIdentity,
    ) -> MemoryTombstoneV2:
        return await self._store.get_tombstone(memory_id, trusted)

    async def tombstone_versions(
        self, root_id: str, trusted: TrustedMemoryIdentity,
    ) -> list[MemoryTombstoneV2]:
        return await self._store.list_tombstone_versions(root_id, trusted)

    async def edit(
        self, memory_id: str, trusted: TrustedMemoryIdentity, *, expected_version: int,
        content: str, payload: MemoryPayload, importance: float | None = None,
        strength: float | None = None,
    ) -> MemoryRecordV2:
        previous = await self._store.get(memory_id, trusted)
        if previous.status is not MemoryStatus.ACTIVE:
            raise ValueError("only an active memory can be edited")
        if previous.version != expected_version:
            raise StaleMemoryVersion(memory_id)
        if payload.kind != previous.kind.value:
            raise InvalidMemoryPayload("payload kind must match the existing memory kind")
        evidence_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        draft = MemoryDraftV2(
            kind=previous.kind, tier=previous.tier, scope=previous.scope,
            project_id=previous.project_id, content=content, payload=payload,
            importance=previous.importance if importance is None else importance,
            strength=previous.strength if strength is None else strength,
            source_type=SourceType.USER_EDIT, source_session_id=None,
            source_event_ids=[],
            evidence=[EvidenceItem(role="user_edit", excerpt="", hash=evidence_hash)],
        )
        self._reject_secret(draft)
        await self._mirror_markdown(draft, previous_id=memory_id, trusted=trusted)
        return await self._store.update(memory_id, draft, trusted)

    async def delete(
        self, memory_id: str, trusted: TrustedMemoryIdentity,
    ) -> MemoryDeletionReceiptV2:
        receipt = await self._store.delete(memory_id, trusted)
        await self._mirror_deleted(receipt.memories)
        if receipt.memories:
            try:
                await self._delete_vectors(receipt.memories)
            except Exception as error:
                raise MemoryIndexDeletePending(
                    tuple(memory.memory_id for memory in receipt.memories), affected_count=1,
                ) from error
        return receipt

    async def bulk_delete(
        self, trusted: TrustedMemoryIdentity, *, kind: MemoryKind | None,
    ) -> list[MemoryDeletionReceiptV2]:
        receipts = await self._store.bulk_delete(trusted, kind=kind)
        memories = tuple(memory for receipt in receipts for memory in receipt.memories)
        await self._mirror_deleted(memories)
        if memories:
            try:
                await self._delete_vectors(memories)
            except Exception as error:
                raise MemoryIndexDeletePending(
                    tuple(memory.memory_id for memory in memories),
                    affected_count=sum(receipt.deleted for receipt in receipts),
                ) from error
        return receipts

    async def _delete_vectors(self, memories) -> None:
        if self._relay is not None:
            await self._relay.delete_now(memories)
            return
        for memory in memories:
            await self._index.delete(
                memory.memory_id, tenant_id=memory.tenant_id, user_id=memory.user_id,
                scope=memory.scope, project_id=memory.project_id,
            )

    async def get_settings(self, trusted: TrustedMemoryIdentity) -> MemorySettingsV2:
        return await self._store.get_settings(trusted)

    async def update_settings(
        self, trusted: TrustedMemoryIdentity, *,
        extraction_enabled: bool | None = None, recall_enabled: bool | None = None,
        fallback_enabled: bool | None = None, write_approval: bool | None = None,
    ) -> MemorySettingsV2:
        return await self._store.update_settings(
            trusted, extraction_enabled=extraction_enabled, recall_enabled=recall_enabled,
            fallback_enabled=fallback_enabled, write_approval=write_approval,
        )

    async def purge_expired_tombstones(self) -> int:
        return await self._store.purge_expired_tombstones()

    # ----------------------------------------------------------------------------------
    # 事务内写（#298 T6 的形成执行器专用）
    # ----------------------------------------------------------------------------------
    #
    # 上面七个方法是"一次调用一个事务"。执行器需要的边界更宽：一批裁决动作要与
    # **记忆记录 + outbox + formation job 终态**一起落地（AC6 / AC7）。
    # "为什么必须同一次提交"的完整论证只在
    # `jobs.SqliteMemoryV2JobStore.commit_with_outcome` 一处（§16.1）。
    #
    # 它们**不是**新的 provider 契约：`MemoryV2Capability` 仍然冻结在七个方法上，
    # 这三个是"在别人的事务里干活"的额外面，只有组合实现（本类）需要提供。

    async def create_in(self, connection: aiosqlite.Connection, draft: MemoryDraftV2,
                        trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        self._reject_secret(draft)
        return await self._store.create(draft, trusted, connection=connection)

    async def update_in(self, connection: aiosqlite.Connection, previous_id: str,
                        draft: MemoryDraftV2, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        self._reject_secret(draft)
        return await self._store.update(previous_id, draft, trusted, connection=connection)

    async def invalidate_in(self, connection: aiosqlite.Connection, memory_id: str,
                            trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        return await self._store.invalidate(memory_id, trusted, connection=connection)

    async def read(self, memory_id: str, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        return await self._store.get(memory_id, trusted)

    async def list_active(
        self, trusted: TrustedMemoryIdentity, *, scope: MemoryScope, limit: int, offset: int = 0,
    ) -> list[MemoryRecordV2]:
        return await self._store.list_active(trusted, scope=scope, limit=limit, offset=offset)

    async def versions(self, root_id: str, trusted: TrustedMemoryIdentity) -> list[MemoryRecordV2]:
        return await self._store.list_versions(root_id, trusted)

    async def search(
        self, query: str, trusted: TrustedMemoryIdentity, *, scope: MemoryScope, limit: int,
    ) -> list[MemoryRecordV2]:
        """词法检索（FTS5）。#1353 §5：没有 dense 腿，`search` 直接走权威 SQLite。"""
        return await self._store.keyword_search(
            keyword_terms(query), trusted, scope=scope, limit=limit,
        )

    async def list_profiles(
        self, trusted: TrustedMemoryIdentity, *, limit: int = 256,
    ) -> list[MemoryRecordV2]:
        return await self._store.list_profiles(trusted, limit=limit)

    async def hybrid_search(
        self, query: str, trusted: TrustedMemoryIdentity, *,
        scopes: Sequence[MemoryScope], limit: int,
    ) -> list[RankedMemory]:
        """FTS5 词法召回 + SQLite 身份/状态复核。

        #1353 §5：dense 语义腿已删除（派生向量索引已退出记忆域），
        名字保留是因为调用方（`retrieve_memory` / recall）与既有契约都按它取记忆；
        `explanation["dense"]` 恒 0，排序由 `rank_memory` 的关键词/时效/权威因子决定。
        """
        if not query.strip() or limit <= 0:
            return []
        if self._relay is not None:
            await self._relay.flush()
        terms = keyword_terms(query)
        candidate_limit = min(128, max(20, limit * 4))
        candidates: dict[str, MemoryRecordV2] = {}
        for scope in dict.fromkeys(scopes):
            if scope is MemoryScope.PROJECT and trusted.project_id is None:
                continue
            for record in await self._store.keyword_search(
                terms, trusted, scope=scope, limit=candidate_limit,
            ):
                candidates.setdefault(record.id, record)

        ranked = [RankedMemory(
            record=record,
            explanation=rank_memory(
                record, dense=0.0, keyword=keyword_overlap(record.content, terms),
            ),
        ) for record in candidates.values()]
        ranked.sort(key=lambda hit: (-float(hit.explanation["score"]), hit.record.id))
        return ranked[:min(limit, 128)]

    @staticmethod
    def _reject_secret(draft: MemoryDraftV2) -> None:
        texts = [draft.content, draft.payload.model_dump_json()]
        texts.extend(item.excerpt for item in draft.evidence)
        if find_secret(*texts) is not None:
            raise PermissionError("memory content contains a credential or secret")


class StaleMemoryVersion(ValueError):
    """The API edit was based on an older version than the active record."""


class MemoryIndexDeletePending(RuntimeError):
    """SQLite tombstone committed but immediate derived-index deletion failed."""

    def __init__(self, memory_ids: tuple[str, ...], *, affected_count: int) -> None:
        self.memory_ids = memory_ids
        self.affected_count = affected_count
        super().__init__("memory index deletion pending")


class InvalidMemoryPayload(ValueError):
    """Edit payload is well-formed but incompatible with the target memory's kind."""
