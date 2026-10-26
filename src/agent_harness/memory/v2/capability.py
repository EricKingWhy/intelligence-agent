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
from enum import Enum
from typing import Any, Protocol

import aiosqlite

from agent_harness.memory.v2 import reprojection
from agent_harness.memory.v2.commit import MemoryCommit
from agent_harness.memory.v2.index import MemoryV2IndexRelay, MemoryV2VectorIndex
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore, target_for
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
from agent_harness.memory.v2.threat_patterns import first_threat_message
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
from agent_harness.memory.v2.write_approval import BlockedWriteStore, StagedWriteStore

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
        commit: MemoryCommit | None = None,
        staged: StagedWriteStore | None = None,
        blocked: BlockedWriteStore | None = None,
    ) -> None:
        self._store = store
        self._index = index
        self._relay = relay
        #: #1494 / ADR-0058 D2：当前内容的**唯一**可变入口。为 `None` 时退化为纯 SQLite
        #: （既有测试与旧部署路径），装配层注入后 create/update/invalidate 先经 commit
        #: 落 markdown，再投影 SQLite。没有任何一条写路径能绕过它直接改文件。
        self._commit = commit
        #: #1353 M6：`write_approval` 门开时的待审区。跟着 markdown 走——没有权威面就没有
        #: 可审批的写入，工具也不会注册。
        self._staged = staged
        #: #1461：后台 formation 被拦下的写入（第二段处置通道）。executor 的 gate 开路径
        #: 往里落行，CLI/Web 从里面 promote/discard。
        self._blocked = blocked
        #: #1494 D9：最近一次旧状态导入读数。未收敛（settings 冲突 / 补发布不可读）时
        #: CLI `memory reconcile` 要能看见并计入退出码，不能只留在启动日志。
        self._legacy_import_report: dict[str, Any] | None = None
        self._tombstone_purger: asyncio.Task | None = None

    @property
    def legacy_import_report(self) -> dict[str, Any] | None:
        return self._legacy_import_report

    def record_legacy_import(self, report: dict[str, Any]) -> None:
        self._legacy_import_report = dict(report)

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
    # 工具写入 → 派生层投影（#1437 F-01 甲案 / F-06 / F-06b）
    # ----------------------------------------------------------------------------------
    #
    # 刻意**不**复用 `create()`：它第一步就是 `_mirror_markdown`，而这里的 markdown 写入
    # 已经由 `MemoryTool` 提交过了，再镜像一遍会把 replace/batch 的内容写第二次。
    # 这一节只读 markdown、只写 SQLite。

    async def project_tool_write(
        self, target: str, response: dict[str, Any], trusted: TrustedMemoryIdentity, *,
        session_id: str | None = None, event_id: str | None = None,
        operations: Sequence[dict[str, Any]] | None = None, content: str | None = None,
    ) -> dict[str, Any]:
        """把一次已落盘的 markdown 写入投影到派生层；失败只留 pending 账本，不抛。

        写入成功后顺带 drain 一次历史账本（设计稿 §4.2 的重试触发点 1）：派生层此刻是
        健康的，之前积压的行可以在同一轮收敛。本次写入自己失败时跳过——那时派生层正
        不可用，重试只会再失败一次并抬高 `attempts`。
        """
        markdown = self.markdown
        if markdown is None:
            return {
                "desired": 0, "created": 0, "deleted": 0, "skipped": [], "pending": False,
                "unreadable": [],
            }
        summary = await reprojection.apply_write(
            self._store, markdown, target, response, trusted,
            operations=operations, content=content,
            session_id=session_id, event_id=event_id,
        )
        if not summary["pending"]:
            await reprojection.drain_pending(self._store, markdown, trusted)
        return summary

    async def reproject(
        self, trusted: TrustedMemoryIdentity, target: str,
    ) -> dict[str, Any]:
        """F-06b 运维/漂移入口：先把 pending 账本排空，再全量对账该 target。

        返回 summary 额外带两项供 CLI 判失败（#1482 Finding 二）：`unreadable`（本轮没读到的
        **文件**，来自 `reproject_target`）与 `remaining`（对账**之后**该 target 仍在账本里的
        行数——服务层没有其它可读面，不透传的话"账本有积压"在入口侧不可见）。
        """
        markdown = self.markdown
        if markdown is None:
            return {
                "desired": 0, "created": 0, "deleted": 0, "skipped": [], "pending": False,
                "unreadable": [], "remaining": 0,
            }
        await reprojection.drain_pending(self._store, markdown, trusted)
        summary = await reprojection.reproject_target(self._store, markdown, trusted, target)
        # 必须在对账**之后**回读，且只数本 target 的行：拿 drain 的快照会让一次已经收敛的
        # 运行仍报「账本积压」并退 2，而那个计数是全 target 的，双 target 循环里会被重复
        # 计入（#1482 F3）。
        summary["remaining"] = sum(
            1 for row in await self._store.pending_projections(trusted)
            if str(row["target"]) == target
        )
        return summary

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
    # 写无法参与那笔提交；执行器把 `mirror_committed` 挂在 `commit_with_outcome` 的
    # `before_commit` 上——发布失败则记录事务回滚、job 不写终态（ADR-0058 D8）。

    @property
    def markdown(self) -> MarkdownMemoryStore | None:
        """只读的权威面引擎（供 recall/reprojection 读取）；**写**必须经 `commit`。"""
        return self._commit.engine if self._commit is not None else None

    @property
    def commit(self) -> MemoryCommit | None:
        return self._commit

    @property
    def state_root(self):
        """操作状态根（#1494）：job/staged/blocked/receipt 与记录同生共死。"""
        return self._store.state_root

    @property
    def staged(self) -> StagedWriteStore | None:
        """#1353 M6 待审区；`None` = 这个部署没有审批面（门恒关）。"""
        return self._staged

    @property
    def blocked(self) -> BlockedWriteStore | None:
        """#1461 被拦下的后台写入；`None` = 装配没接处置通道（gate 开时 executor 会响亮失败）。"""
        return self._blocked

    def attach_blocked(self, blocked: BlockedWriteStore) -> None:
        """装配期接线：`BlockedWriteStore` 要拿本 service 当 writer（promote 重放），
        所以它只能在 service 构造**之后**建——构造参数接不了这个先有鸡还是先有蛋。"""
        self._blocked = blocked

    @staticmethod
    def markdown_target(record: MemoryDraftV2 | MemoryRecordV2) -> str:
        """分拣规则：`kind=user` 或 `tier=profile` → USER.md，其余 → MEMORY.md。

        与 §4 迁移的分拣**同一句话**——两条路分拣不一致，迁移后第一条自动写入就会把事实
        挪到另一个文件里，用户看到"刚写的记忆跑到别处去了"。定义落在
        `markdown_store.target_for`（叶子模块），这里只是记录形状上的适配。
        """
        return target_for(record.kind.value, record.tier.value)

    async def _previous_record(
        self, previous_id: str, trusted: TrustedMemoryIdentity,
    ) -> MemoryRecordV2 | None:
        """取上一版记录；不存在（已被删/不可见）→ `None`，由调用方决定如何补写。"""
        try:
            return await self._store.get(previous_id, trusted)
        except KeyError:
            return None

    @staticmethod
    def _require_markdown_success(response: dict[str, Any], operation: str) -> None:
        """markdown 的失败是**返回值**（`success=False`），不是异常。

        权威面没写成就不能继续写 SQLite 投影：调用方看到的必须是一次失败写入，
        而不是"派生层有、权威层没有"的分叉。错误文案带 store 的 message；**预算类失败
        例外地带上当前条目列表**（`current_entries`），供模型就地合并——条目只进模型
        可见的 message，不进事件流（不变量 #22）。
        """
        if response.get("success"):
            return
        message = str(response.get("error") or "unknown error")
        operation_id = response.get("operation_id")
        if operation_id:
            # 失败也要能追到回执：用户/CLI 用这个 id 跑 `memory reconcile`。
            message += f"（operation {operation_id}）"
        entries = response.get("current_entries")
        # `usage` 只由预算类失败携带（`_failure_with_entries` / `_batch_failure`）；
        # 零命中之类的可自纠失败带 `current_entries` 但不带 `usage`，不能报成预算耗尽。
        if response.get("usage") and entries:
            # #1440 F-12：带上活条目列表的预算失败——当前用量与上限已在 store 文案里，
            # 条目是模型就地合并的唯一依据（与 `memory_tool._result` 同形）。
            # 条目只进模型可见的 message，不进事件流（不变量 #22）。
            message += "\n\n当前条目：\n" + "\n".join(f"- {entry}" for entry in entries)
            raise MarkdownBudgetExhausted(f"markdown {operation} failed: {message}")
        raise MarkdownAuthorityError(f"markdown {operation} failed: {message}")

    async def _mirror_markdown(
        self, draft: MemoryDraftV2 | None, *, previous_id: str | None = None,
        trusted: TrustedMemoryIdentity | None = None,
    ) -> None:
        """把一次权威写入投影到 markdown；失败抛 `MarkdownAuthorityError`。

        顺序是 markdown 先、SQLite 后：权威面写不成时 SQLite 尚未落盘，抛出即整体失败，
        两侧仍然一致。`mirror_committed` 不同——它跑在 SQLite 已提交之后，只能留痕。

        唯一写入口是 `MemoryCommit`（#1494 AC1）：本方法只决定**写什么**，
        匹配/校验/回执/发布都在 commit 内完成。
        """
        commit = self._commit
        if commit is None:
            return
        engine = commit.engine

        async def _add(target: str, content: str) -> None:
            if engine.target_enabled(target):
                self._require_markdown_success(await commit.add(target, content), "add")

        async def _remove(record: MemoryRecordV2) -> None:
            target = self.markdown_target(record)
            if engine.target_enabled(target):
                self._require_markdown_success(
                    await commit.remove(target, record.content, matched_entry=record.content),
                    "remove",
                )

        async def _replace(target: str, record: MemoryRecordV2, content: str) -> None:
            # `matched_entry` 钉住**审阅时的那条原文**：并发手改后条目已变，replace 会以
            # stale 失败而不是改到另一条上。stale 时退回 add（新内容仍应被记住）；
            # 预算/扫描失败时 add 同样失败，交给 `_add` 抛出。目标未启用则无事可做。
            if not engine.target_enabled(target):
                return
            response = await commit.replace(
                target, record.content, content, matched_entry=record.content,
            )
            if not response.get("success"):
                await _add(target, content)

        if draft is None:
            if previous_id is None or trusted is None:
                raise ValueError("invalidate mirror requires previous_id and a trusted identity")
            previous = await self._previous_record(previous_id, trusted)
            if previous is not None:
                await _remove(previous)
            return
        target = self.markdown_target(draft)
        if previous_id is None:
            await _add(target, draft.content)
            return
        if trusted is None:
            raise ValueError("update mirror requires a trusted identity")
        previous = await self._previous_record(previous_id, trusted)
        if previous is None:
            await _add(target, draft.content)
            return
        if self.markdown_target(previous) != target:
            # 分拣变了（tier/kind 被改）：先删旧文件里的条目，再写新文件。
            await _remove(previous)
            await _add(target, draft.content)
            return
        await _replace(target, previous, draft.content)

    async def _mirror_deleted(
        self, memories: Sequence[MemoryRecordV2], *, digests: Sequence[str] = (),
    ) -> None:
        """硬删（delete / bulk_delete）把条目从 markdown 移除；失败即上抛。

        调用方（#1494）把本方法放在 records 删除**之前**：markdown 不成功时记录仍在，
        重试同一次删除是幂等的，不会出现"记录已删、权威面还在"的分叉。
        """
        commit = self._commit
        if commit is None:
            return
        engine = commit.engine
        for memory in memories:
            target = self.markdown_target(memory)
            if not engine.target_enabled(target):
                continue
            response = await commit.remove(
                target, memory.content, matched_entry=memory.content, missing_ok=True,
                operation_id=f"del_{memory.memory_id}",
            )
            self._require_markdown_success(response, "remove")
        if digests:
            # 墓碑-only 删除：记录行已不在，只能按正文哈希清理。零命中就是已经清理过。
            await commit.remove_by_digest(
                digests, operation_id=_digest_operation_id(digests))

    async def mirror_committed(
        self, records: Sequence[MemoryRecordV2], *, invalidated: Sequence[MemoryRecordV2] = (),
        operation_prefix: str | None = None,
    ) -> None:
        """formation job 的内容发布：**在 records 事务提交前**执行（ADR-0058 D8）。

        发布失败必须上抛：`commit_with_outcome` 会回滚记录事务、不写终态，恢复扫描
        用同一批确定性 id / `jobp_` 回执重跑。这里绝不把失败降成 warning——那会让
        job 以 COMMITTED 收尾而权威面没有内容（AC4）。

        先做一次全量内容预检（delimiter / threat），任何一条不合格都在碰盘前失败，
        避免"前几条已发布、后一条被拒"的半批状态。
        """
        commit = self._commit
        if commit is None:
            return
        engine = commit.engine
        planned = [
            (record, self.markdown_target(record)) for record in records
            if engine.target_enabled(self.markdown_target(record))
        ]
        for record, _target in planned:
            if error := commit.content_error(record.content):
                raise MarkdownAuthorityError(f"markdown mirror rejected content: {error}")

        def _op(kind: str, memory_id: str) -> str | None:
            return f"{operation_prefix}_{kind}_{memory_id}" if operation_prefix else None

        # 先删旧、再加新：后台 update 的新旧内容可能重合，反序会把刚写的新条目删掉。
        for record in invalidated:
            target = self.markdown_target(record)
            if not engine.target_enabled(target):
                continue
            self._require_markdown_success(await commit.remove(
                target, record.content, matched_entry=record.content, missing_ok=True,
                operation_id=_op("rm", record.id),
            ), "remove")
        for record, target in planned:
            self._require_markdown_success(await commit.add(
                target, record.content, operation_id=_op("add", record.id),
            ), "add")

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
        # D7/D8：墓碑 → 内容 → records。内容清理失败时记录仍在，重试删除是幂等的。
        prepared = await self._store.prepare_tombstones(trusted, memory_id=memory_id)
        await self._mirror_deleted(prepared)
        receipt = await self._store.delete(memory_id, trusted)
        if not receipt.memories:
            # 记录行早已不在（墓碑-only）：只剩按正文哈希清理残留条目。
            await self._mirror_deleted((), digests=receipt.content_hashes)
        else:
            mirrored = {memory.memory_id for memory in prepared}
            # 竞态窗口里新建/更新的行未被 prepare 看到：按内容再清一次（不在 prepared 集内）。
            await self._mirror_deleted(
                tuple(memory for memory in receipt.memories
                      if memory.memory_id not in mirrored))
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
        prepared = await self._store.prepare_tombstones(
            trusted, kind=kind, reason="bulk_delete")
        await self._mirror_deleted(prepared)
        receipts = await self._store.bulk_delete(trusted, kind=kind)
        memories = tuple(memory for receipt in receipts for memory in receipt.memories)
        mirrored = {memory.memory_id for memory in prepared}
        extra = tuple(memory for memory in memories if memory.memory_id not in mirrored)
        if extra:
            await self._mirror_deleted(extra)
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
    # `jobs.MemoryV2JobStore.commit_with_outcome` 一处（§16.1）。
    #
    # 它们**不是**新的 provider 契约：`MemoryV2Capability` 仍然冻结在七个方法上，
    # 这三个是"在别人的事务里干活"的额外面，只有组合实现（本类）需要提供。

    async def create_in(self, connection: aiosqlite.Connection, draft: MemoryDraftV2,
                        trusted: TrustedMemoryIdentity, *,
                        memory_id: str | None = None) -> MemoryRecordV2:
        self._reject_secret(draft)
        return await self._store.create(
            draft, trusted, connection=connection, memory_id=memory_id)

    async def update_in(self, connection: aiosqlite.Connection, previous_id: str,
                        draft: MemoryDraftV2, trusted: TrustedMemoryIdentity, *,
                        new_memory_id: str | None = None) -> MemoryRecordV2:
        self._reject_secret(draft)
        return await self._store.update(
            previous_id, draft, trusted, connection=connection, new_memory_id=new_memory_id)

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
        """凭证与注入模式在**入口**拒绝，与 markdown 的 strict 扫描同源。

        只查 `find_secret` 不够：markdown 侧还会用 `first_threat_message(scope="strict")`
        拒掉注入/外泄样本，两层判据不等价时 SQLite 会收下权威面永远写不进的记忆。
        """
        texts = [draft.content, draft.payload.model_dump_json()]
        texts.extend(item.excerpt for item in draft.evidence)
        if find_secret(*texts) is not None:
            raise MemoryContentRejected(
                SECRET_CONTENT_REJECTION,
                MemoryRejectionReason.SECRET_CONTENT,
            )
        for text in texts:
            if first_threat_message(text, scope="strict") is not None:
                raise MemoryContentRejected(
                    "该内容匹配注入或外泄模式，不能写入长期记忆。",
                    MemoryRejectionReason.THREAT_PATTERN,
                )


def _digest_operation_id(digests: Sequence[str]) -> str:
    """同一批墓碑哈希 ⇒ 同一个 operation id（重试回放，不重复清理）。"""
    joined = "\x00".join(sorted(str(digest) for digest in digests))
    return "del_dg_" + hashlib.sha256(joined.encode()).hexdigest()[:32]


class MemoryRejectionReason(str, Enum):
    """内容被拒的稳定归因码（进 `MemoryContentRejected.reason_code`）。

    字面量是**观测契约**的一部分：金标判定与排障都按它对账，不随中文文案变动
    （文案会为了可读性改写，码不会——#1440 C 案）。码值由
    `tests/evaluation/test_memory_v2_gold_reason_contract.py` 的字面量断言钉住。
    """

    SECRET_CONTENT = "secret_content"
    THREAT_PATTERN = "threat_pattern"


#: 凭证拒绝的中文真因文案：入口 `_reject_secret` 与显式路径 `tools.py` 共用一份，
#: 两处措辞才不会有第二份真源（#1440 C 案）。**文案可变、`reason_code` 不变**。
SECRET_CONTENT_REJECTION = "该内容匹配凭证或密钥规则，不能写入长期记忆。"


class MemoryContentRejected(PermissionError):
    """内容本身被拒（凭证/密钥/注入模式），与"身份无权"是两件事（#1440 F-12）。

    继承 `PermissionError` 是为了保持入口层的 403 语义：`memory_http_error` 沿 MRO 查
    `_MEMORY_ERROR_STATUS`，本类命中父类那一行（#1440 P2-1 之前是精确类型索引，子类
    会 KeyError 成 500）。垫片层先捕获本类、再捕获裸 `PermissionError`，真因才不会被
    "当前身份无权"吞掉。

    `reason_code` 是机器可读的稳定归因（`MemoryRejectionReason`），随异常对象传播；
    缺省 `None` 只用于构造兼容，判定方对缺失一律 fail-closed（#1440 C 案）。
    """

    def __init__(
        self, message: str, reason_code: MemoryRejectionReason | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class MarkdownAuthorityError(RuntimeError):
    """markdown 权威面拒绝了这次写入（成功与否是返回值，不是异常）。"""


class MarkdownBudgetExhausted(MarkdownAuthorityError):
    """权威面因**预算/条目合并**拒绝——永久条件，重试落在同一条上限上（#1440 F-12）。"""


class StaleMemoryVersion(ValueError):
    """The API edit was based on an older version than the active record."""


class MemoryIndexDeletePending(RuntimeError):
    """Tombstone committed but immediate derived-index deletion failed."""

    def __init__(self, memory_ids: tuple[str, ...], *, affected_count: int) -> None:
        self.memory_ids = memory_ids
        self.affected_count = affected_count
        super().__init__("memory index deletion pending")


class InvalidMemoryPayload(ValueError):
    """Edit payload is well-formed but incompatible with the target memory's kind."""
