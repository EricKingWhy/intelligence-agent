"""MEM-V2-1 的派生索引面：索引端口、outbox relay 与检索结果的权威复核。

# 为什么索引需要"复核"（AC7 / PRD §6.6）

Milvus 是**派生**索引，不是事实源。任何一条检索命中在被送进模型上下文之前，
都必须回到 SQLite 确认三件事：记录存在、状态是 active、调用方对它有权。
少了这一步，"索引里残留一条已失效/跨用户的向量"就等于"把别人的内容注入本轮对话"。
所以本模块把复核做成检索路径上的**必经函数**（`resolve_active_hits`），
而不是调用方的可选步骤。

# 为什么 relay 是一个新类而不是复用 V1 的 `OutboxRelay`

V1 的 relay 绑定 V1 的 `PendingMemory`（携带 `MemoryEntry`）与 V1 的 `VectorIndexStore`。
#297 的 Must Not Do 要求 V1 路径继续可运行，所以 V1 的类不动；V2 有自己的
`PendingMemoryChangeV2` 与 V2 索引端口。两边的**策略**（失败保留意图、连续失败后死信、
ack 按 revision 匹配）是一致的，这是有意的，不是重复实现——它们服务两套不同的
记录契约，合并任何一侧都会让另一侧的冻结契约被改写。
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Protocol

from agent_harness.memory.v2.store import (
    MemoryOperationV2,
    PendingMemoryChangeV2,
    SqliteMemoryV2Store,
)
from agent_harness.memory.v2.types import (
    MemoryRecordV2,
    MemoryScope,
    MemoryStatus,
    TrustedMemoryIdentity,
)

logger = logging.getLogger(__name__)


class MemoryV2VectorIndex(Protocol):
    """派生索引端口：只认 record id 与查询分数，不持有权威事实。

    `delete` 携带完整路由事实（而不是只给 memory_id）：真实后端（Milvus）按
    namespace 过滤删除，而删除发生时记录行可能已经不在——路由事实只能来自 outbox。
    """

    async def upsert(self, record: MemoryRecordV2) -> None: ...

    async def delete(self, memory_id: str, *, tenant_id: str, user_id: str,
                     scope: MemoryScope, project_id: str | None) -> None: ...

    async def search(self, query: str, trusted: TrustedMemoryIdentity,
                     scope: MemoryScope, limit: int) -> list[tuple[str, float]]: ...


class InMemoryMemoryV2Index:
    """进程内索引实现：契约的第二个适配者（第一个是真实 Milvus 后端）。

    它同时是**测试替身**：`fail_upsert` / `fail_delete` 用来注入索引侧故障，
    证明"索引失败不回滚已提交事实、且可恢复地恰好收敛一次"（AC6）。
    检索是朴素的子串匹配而不是向量相似度——本票不测召回质量（那属 MEM-V2-6），
    只测"谁能被看到"，朴素匹配让这些断言不依赖 embedding 的行为。
    """

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, tuple[str, str, str, str | None]]] = {}
        self.fail_upsert: Exception | None = None
        self.fail_delete: Exception | None = None
        self.upsert_calls: list[str] = []
        self.delete_calls: list[str] = []

    async def upsert(self, record: MemoryRecordV2) -> None:
        if self.fail_upsert is not None:
            raise self.fail_upsert
        self.upsert_calls.append(record.id)
        self._rows[record.id] = (
            record.content,
            (record.tenant_id, record.user_id, record.scope.value, record.project_id),
        )

    async def delete(self, memory_id: str, *, tenant_id: str, user_id: str,
                     scope: MemoryScope, project_id: str | None) -> None:
        if self.fail_delete is not None:
            raise self.fail_delete
        self.delete_calls.append(memory_id)
        self._rows.pop(memory_id, None)

    async def search(self, query: str, trusted: TrustedMemoryIdentity,
                     scope: MemoryScope, limit: int) -> list[tuple[str, float]]:
        if not query or limit <= 0:
            return []
        hits = [
            (memory_id, 1.0) for memory_id, (content, route) in self._rows.items()
            if query in content and self._route_matches(route, trusted, scope)
        ]
        return hits[:limit]

    async def contains(self, memory_id: str, trusted: TrustedMemoryIdentity,
                       scope: MemoryScope) -> bool:
        row = self._rows.get(memory_id)
        return row is not None and self._route_matches(row[1], trusted, scope)

    @staticmethod
    def _route_matches(route: tuple[str, str, str, str | None],
                       trusted: TrustedMemoryIdentity, scope: MemoryScope) -> bool:
        tenant_id, user_id, row_scope, project_id = route
        if (tenant_id, user_id, row_scope) != (trusted.tenant_id, trusted.user_id, scope.value):
            return False
        if scope is MemoryScope.PROJECT:
            return project_id is not None and project_id == trusted.project_id
        return True


class MemoryV2IndexRelay:
    """SQLite outbox → 派生索引的收敛。

    失败一律保留 outbox 行、下轮重试；连续 `MAX_CONSECUTIVE_FAILURES` 次后进入
    本进程死信（不再空转，outbox 行保留可观察）。计数器活在进程内存里 ⇒ 重启自愈。

    刻意没有 asyncio 锁：`acknowledge` 是按 `revision` 原子匹配的，两个并发 flush
    只会有一个 ack 成功、另一个返回 `False`（不重复计数）；upsert/delete 自身幂等。
    """

    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, store: SqliteMemoryV2Store, index: MemoryV2VectorIndex) -> None:
        self._store = store
        self._index = index
        self._failure_counts: dict[str, int] = {}
        self._failure_revisions: dict[str, str] = {}

    async def flush(self) -> int:
        count = 0
        after_id = ""
        while page := await self._store.pending(after_id=after_id):
            for change in page:
                if self._failure_revisions.get(change.memory_id) != change.revision:
                    # revision 变化（含首次出现）= 新版本不是旧毒丸的证据，重置重试预算。
                    self._failure_counts.pop(change.memory_id, None)
                self._failure_revisions[change.memory_id] = change.revision
                if self._failure_counts.get(change.memory_id, 0) >= self.MAX_CONSECUTIVE_FAILURES:
                    continue
                try:
                    await self._apply(change)
                except Exception as error:  # noqa: BLE001 — 保留 durable outbox，下轮重试。
                    self._count_failure(change, error)
                    continue
                try:
                    count += await self._store.acknowledge(change)
                except Exception as error:  # noqa: BLE001 — ack 失败与索引失败分开归因。
                    self._count_failure(change, error)
                else:
                    self._failure_counts.pop(change.memory_id, None)
                    self._failure_revisions.pop(change.memory_id, None)
            after_id = page[-1].memory_id
        return count

    async def _apply(self, change: PendingMemoryChangeV2) -> None:
        if change.operation is MemoryOperationV2.DELETE:
            await self._index.delete(
                change.memory_id, tenant_id=change.tenant_id, user_id=change.user_id,
                scope=change.scope, project_id=change.project_id)
            return
        record = change.record
        assert record is not None  # PendingMemoryChangeV2 不变量：upsert ⟹ 携带 record
        await self._index.upsert(record)

    def _count_failure(self, change: PendingMemoryChangeV2, error: Exception) -> None:
        failures = self._failure_counts.get(change.memory_id, 0) + 1
        self._failure_counts[change.memory_id] = failures
        detail = f"{type(error).__name__}"
        if failures >= self.MAX_CONSECUTIVE_FAILURES:
            logger.error("Memory V2 index sync abandoned after %d consecutive failures (%s); "
                         "%s change for %s stays in outbox", failures, detail,
                         change.operation.value, change.memory_id)
        else:
            logger.warning("Memory V2 index sync deferred (%s); outbox retained", detail)

    async def stop(self) -> None:
        """占位：进程内 relay 无常驻任务，保留给装配层的统一关闭序列。"""
        with suppress(Exception):  # pragma: no cover - 无资源可释放
            return


async def resolve_active_hits(
    store: SqliteMemoryV2Store, hits: list[tuple[str, float]],
    trusted: TrustedMemoryIdentity, scope: MemoryScope,
) -> list[MemoryRecordV2]:
    """把索引命中收敛成"可以进模型的记录"，顺序按传入的排名保留。

    三重过滤缺一不可：
    - 记录在 SQLite 里必须存在（伪造/过期命中）；
    - 状态必须是 `active`（superseded / invalidated 不得被检索到 → AC5）；
    - 归属必须落在调用方的可信身份内（`store.get` 的授权语义 → AC7 的跨用户/跨项目/跨租户）。
    """
    resolved: list[MemoryRecordV2] = []
    for memory_id, _score in hits:
        try:
            record = await store.get(memory_id, trusted)
        except KeyError:
            continue
        if record.status is not MemoryStatus.ACTIVE or record.scope is not scope:
            continue
        resolved.append(record)
    return resolved
