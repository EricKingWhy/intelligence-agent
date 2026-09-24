"""MEM-V2-1 的 provider-neutral 记忆边界。

# 这个 Protocol 存在的意义（PRD §7.1 第 2 条 / §7.2 第 1 条）

记忆必须停在 Capability + Context Provider 的既有 seam 后面：AgentRuntime 不得长出
provider 专属分支。所以调用方（工具、API、后续的 formation 流水线）只依赖
`MemoryV2Capability` 这七个方法，而**不**知道背后是 SQLite、Milvus，还是 LangMem。

`MemoryV2Service` 是当前唯一实现，但它不是"边界"本身：边界是 Protocol。
任何满足这七个方法的对象都可以替换它——包括测试替身与后续 ticket 引入的
非 LangMem provider。这就是"adapter boundary 得以保留"的可判定形态：
换实现不需要改调用方，也不需要改本模块的签名。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import aiosqlite

from agent_harness.memory.v2.index import (
    MemoryV2IndexRelay,
    MemoryV2VectorIndex,
    resolve_active_hits,
)
from agent_harness.memory.v2.recall import (
    RankedMemory,
    keyword_overlap,
    keyword_terms,
    rank_memory,
)
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import (
    MemoryDraftV2,
    MemoryRecordV2,
    MemoryScope,
    TrustedMemoryIdentity,
)


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
    ) -> None:
        self._store = store
        self._index = index
        self._relay = relay

    async def create(self, draft: MemoryDraftV2, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        return await self._store.create(draft, trusted)

    async def update(self, previous_id: str, draft: MemoryDraftV2,
                     trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        return await self._store.update(previous_id, draft, trusted)

    async def invalidate(self, memory_id: str, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
        return await self._store.invalidate(memory_id, trusted)

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
        return await self._store.create(draft, trusted, connection=connection)

    async def update_in(self, connection: aiosqlite.Connection, previous_id: str,
                        draft: MemoryDraftV2, trusted: TrustedMemoryIdentity) -> MemoryRecordV2:
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
        hits = await self._index.search(query, trusted, scope, limit)
        return await resolve_active_hits(self._store, hits, trusted, scope)

    async def list_profiles(
        self, trusted: TrustedMemoryIdentity, *, limit: int = 256,
    ) -> list[MemoryRecordV2]:
        return await self._store.list_profiles(trusted, limit=limit)

    async def hybrid_search(
        self, query: str, trusted: TrustedMemoryIdentity, *,
        scopes: Sequence[MemoryScope], limit: int,
    ) -> list[RankedMemory]:
        """Dense + SQLite keyword recall with SQLite identity/status revalidation."""
        if not query.strip() or limit <= 0:
            return []
        if self._relay is not None:
            await self._relay.flush()
        terms = keyword_terms(query)
        candidate_limit = min(128, max(20, limit * 4))
        candidates: dict[str, tuple[MemoryRecordV2, float]] = {}
        for scope in dict.fromkeys(scopes):
            if scope is MemoryScope.PROJECT and trusted.project_id is None:
                continue
            dense_hits = await self._index.search(query, trusted, scope, candidate_limit)
            dense_ids = [(memory_id, score) for memory_id, score in dense_hits
                         if isinstance(score, (int, float)) and math.isfinite(score)]
            for record in await resolve_active_hits(self._store, dense_ids, trusted, scope):
                if record.tier.value == "collection":
                    score = next((float(value) for memory_id, value in dense_ids
                                  if memory_id == record.id), 0.0)
                    candidates[record.id] = (record, max(0.0, min(1.0, score)))

            lexical = await self._store.keyword_search(
                terms, trusted, scope=scope, limit=candidate_limit,
            )
            for record in lexical:
                try:
                    current = await self._store.get(record.id, trusted)
                except KeyError:
                    continue
                if (current.status.value == "active" and current.tier.value == "collection"
                        and current.scope is scope):
                    candidates.setdefault(current.id, (current, 0.0))

        ranked = [RankedMemory(
            record=record,
            explanation=rank_memory(
                record, dense=dense, keyword=keyword_overlap(record.content, terms),
            ),
        ) for record, dense in candidates.values()]
        ranked.sort(key=lambda hit: (-float(hit.explanation["score"]), hit.record.id))
        return ranked[:min(limit, 128)]
