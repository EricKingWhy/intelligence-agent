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
`PendingMemoryChangeV2` 与 V2 索引端口。两边共有的策略是「失败保留意图、连续失败后死信、
ack 按 revision 匹配」；它们服务两套不同的记录契约，合并任何一侧都会让另一侧的冻结契约
被改写。

**一处刻意不跟 V1 的地方**：`ack` 失败是否消耗重试预算。V1 把 ack 失败也计入同一个连续失败
计数（`memory/outbox_relay.py` 的 `flush`，那里的注释写明理由是"ack 持续失败的条目必须死信，
不能靠每轮重复 upsert 空转"）。V2 不这么做——V2 的 outbox 行本身就是"未收敛"标记，
索引已经写成功之后再进死信只会让一个健康的 key 永久失去重试，而重试 ack 是幂等的、不会空转
（每轮只多一次 SQLite 写）。取而代之的是**独立**的 ack 计数器，只用于把持续账本故障升级到
`ERROR`（可观察），不改变重试行为。取舍记录在 ADR-0042 §D7。
"""

from __future__ import annotations

import logging
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

    失败一律保留 outbox 行、下轮重试；连续 `MAX_CONSECUTIVE_FAILURES` 次**索引侧**失败后
    进入本进程死信（不再空转，outbox 行保留可观察）。计数器活在进程内存里 ⇒ 重启自愈。

    重试预算**只**统计索引侧失败（`_count_index_failure`）：`acknowledge` 失败发生在索引
    **已经写成功之后**，把它计入预算会让一次账本故障永久毒住一条健康的 key——那条变更再也
    不会被重试，索引与实际状态就此静默分叉。重试 ack 本身无害（upsert/delete 幂等）。
    账本故障改用**独立**计数器（`_count_ack_failure`）在连续 `MAX_CONSECUTIVE_FAILURES` 次后
    升级到 `ERROR`：只把"账本卡死"变得可观察，不把它变成死信。

    刻意没有 asyncio 锁：`acknowledge` 是按 `revision` 原子匹配的，两个并发 flush
    只会有一个 ack 成功、另一个返回 `False`（不重复计数）；upsert/delete 自身幂等。
    """

    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, store: SqliteMemoryV2Store, index: MemoryV2VectorIndex) -> None:
        self._store = store
        self._index = index
        self._failure_counts: dict[str, int] = {}
        self._failure_revisions: dict[str, str] = {}
        # 与 `_failure_counts` / `_failure_revisions` 完全分开：这个只驱动告警升级，
        # 从不参与死信判断。**自己的** revision 记录是必需的——索引成功那条路径会 pop
        # `_failure_revisions`，若共用它，ack 计数每轮都会被误判成"revision 变了"而归零，
        # 升级分支永远不会触发。
        self._ack_failure_counts: dict[str, int] = {}
        self._ack_failure_revisions: dict[str, str] = {}

    async def flush(self) -> int:
        count = 0
        after_id = ""
        while page := await self._store.pending(after_id=after_id):
            for change in page:
                if self._failure_revisions.get(change.memory_id) != change.revision:
                    # revision 变化（含首次出现）= 新版本不是旧毒丸的证据，重置重试预算。
                    self._failure_counts.pop(change.memory_id, None)
                self._failure_revisions[change.memory_id] = change.revision
                if self._ack_failure_revisions.get(change.memory_id) != change.revision:
                    # ack 计数同理：新版本换了账本目标，旧的连续失败不再说明当前问题。
                    self._ack_failure_counts.pop(change.memory_id, None)
                self._ack_failure_revisions[change.memory_id] = change.revision
                if self._failure_counts.get(change.memory_id, 0) >= self.MAX_CONSECUTIVE_FAILURES:
                    continue
                try:
                    await self._apply(change)
                except Exception as error:  # noqa: BLE001 — 保留 durable outbox，下轮重试。
                    self._count_index_failure(change, error)
                    continue
                # 索引侧已经收敛 ⇒ 这条不再可能是毒丸，先清掉它的重试预算，
                # 再单独处理账本（ack 失败不该把一条健康的 key 记成失败）。
                self._failure_counts.pop(change.memory_id, None)
                self._failure_revisions.pop(change.memory_id, None)
                try:
                    count += await self._store.acknowledge(change)
                except Exception as error:  # noqa: BLE001 — 只影响账本，不影响索引健康度。
                    # 刻意**不**计入重试预算：索引已经写成功，进死信会让 outbox 行永久留存，
                    # 而重试 ack 本身是无害的（upsert/delete 幂等）⇒ 下轮再试，直到账记上。
                    # 「下轮再试」必须配一条升级路径，否则卡死的账本会静默到进程重启。
                    self._count_ack_failure(change, error)
                else:
                    self._ack_failure_counts.pop(change.memory_id, None)
                    self._ack_failure_revisions.pop(change.memory_id, None)
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

    def _count_index_failure(self, change: PendingMemoryChangeV2, error: Exception) -> None:
        """索引侧失败的计数（**只**给索引失败用；ack 失败走另一条分支，不消耗预算）。"""
        failures = self._failure_counts.get(change.memory_id, 0) + 1
        self._failure_counts[change.memory_id] = failures
        detail = f"{type(error).__name__}"
        if failures >= self.MAX_CONSECUTIVE_FAILURES:
            logger.error("Memory V2 index sync abandoned after %d consecutive failures (%s); "
                         "%s change for %s stays in outbox", failures, detail,
                         change.operation.value, change.memory_id)
        else:
            logger.warning("Memory V2 index sync deferred (%s); outbox retained", detail)

    def _count_ack_failure(self, change: PendingMemoryChangeV2, error: Exception) -> None:
        """账本（ack）失败的**独立**计数：只驱动告警升级，**不**参与死信预算。

        与 V1 `memory/outbox_relay.py` 的取舍相反（那里 ack 失败也计入同一预算）——
        理由见模块 docstring 与 ADR-0042 §D7：V2 的 outbox 行就是"未收敛"标记，
        索引已成功后再死信等于让健康的 key 永久停摆，而重试 ack 幂等、不会空转。
        """
        failures = self._ack_failure_counts.get(change.memory_id, 0) + 1
        self._ack_failure_counts[change.memory_id] = failures
        if failures >= self.MAX_CONSECUTIVE_FAILURES:
            logger.error("Memory V2 outbox acknowledge still failing after %d attempts (%s); "
                         "index converged, %s change for %s stays in outbox",
                         failures, type(error).__name__, change.operation.value, change.memory_id)
        else:
            logger.warning("Memory V2 outbox acknowledge deferred (%s); index already "
                           "converged, outbox row retained", type(error).__name__)


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
