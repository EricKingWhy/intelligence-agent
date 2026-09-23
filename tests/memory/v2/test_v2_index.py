"""MEM-V2-1（#297）派生索引 seam：outbox relay、故障恢复与权威复核。

Seam：`MemoryV2IndexRelay.flush()`（索引收敛）与 `resolve_active_hits()`（检索结果的
权威复核）——两者都是可观察的公开行为，不测内部结构。

覆盖 AC5（被取代/失效的版本不出现在可检索面）、AC6（索引失败不回滚已提交的 SQLite
事实、可恢复地恰好收敛一次）、AC7（伪造/过期索引命中不得暴露缺失、非活跃、
跨用户、跨项目、跨租户的内容）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agent_harness.memory.v2 import (
    MemoryScope,
    MemoryStatus,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.index import (
    InMemoryMemoryV2Index,
    MemoryV2IndexRelay,
    resolve_active_hits,
)
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from tests.memory.v2._records import make_draft

USER_A = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a")
PROJECT_X = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a", project_id="project-x")
USER_B = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-b")
OTHER_TENANT = TrustedMemoryIdentity(tenant_id="tenant-b", user_id="user-a")


@pytest_asyncio.fixture
async def store(tmp_path) -> SqliteMemoryV2Store:
    instance = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await instance.initialize()
    return instance


@pytest_asyncio.fixture
async def index() -> InMemoryMemoryV2Index:
    return InMemoryMemoryV2Index()


def relay(store: SqliteMemoryV2Store, index: InMemoryMemoryV2Index) -> MemoryV2IndexRelay:
    return MemoryV2IndexRelay(store, index)


# --------------------------------------------------------------------------------------
# 切片 6：relay 的收敛行为（AC5 索引面 / AC6）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_pushes_created_record_into_the_index(
    store: SqliteMemoryV2Store, index: InMemoryMemoryV2Index,
) -> None:
    created = await store.create(make_draft(content="用户偏好深色主题"), USER_A)

    assert await relay(store, index).flush() == 1

    assert [hit[0] for hit in await index.search("深色", USER_A, MemoryScope.USER_GLOBAL, 10)] == [created.id]
    assert await store.pending() == []


@pytest.mark.asyncio
async def test_superseded_version_leaves_the_index(
    store: SqliteMemoryV2Store, index: InMemoryMemoryV2Index,
) -> None:
    """AC5：被取代的版本不得留在可检索面。"""
    first = await store.create(make_draft(content="偏好浅色主题"), USER_A)
    second = await store.update(first.id, make_draft(content="偏好深色主题"), USER_A)

    await relay(store, index).flush()

    assert await index.search("主题", USER_A, MemoryScope.USER_GLOBAL, 10) == [(second.id, 1.0)]
    assert not await index.contains(first.id, USER_A, MemoryScope.USER_GLOBAL)


@pytest.mark.asyncio
async def test_invalidated_record_leaves_the_index(
    store: SqliteMemoryV2Store, index: InMemoryMemoryV2Index,
) -> None:
    created = await store.create(make_draft(content="偏好浅色主题"), USER_A)
    await relay(store, index).flush()

    await store.invalidate(created.id, USER_A)
    await relay(store, index).flush()

    assert await index.search("主题", USER_A, MemoryScope.USER_GLOBAL, 10) == []


@pytest.mark.asyncio
async def test_index_failure_keeps_the_committed_fact_and_the_intent(
    store: SqliteMemoryV2Store, index: InMemoryMemoryV2Index,
) -> None:
    """AC6：索引写失败不得回滚已提交的 SQLite 事实；outbox 意图保留，供下轮重试。"""
    created = await store.create(make_draft(content="用户偏好深色主题"), USER_A)
    index.fail_upsert = RuntimeError("milvus unavailable")

    assert await relay(store, index).flush() == 0

    assert (await store.get(created.id, USER_A)).content == "用户偏好深色主题"
    assert [change.memory_id for change in await store.pending()] == [created.id]


@pytest.mark.asyncio
async def test_replay_converges_the_index_exactly_once(
    store: SqliteMemoryV2Store, index: InMemoryMemoryV2Index,
) -> None:
    """AC6：故障恢复后重放恰好收敛一次——索引只有一条，且 outbox 清空。"""
    created = await store.create(make_draft(content="用户偏好深色主题"), USER_A)
    index.fail_upsert = RuntimeError("milvus unavailable")
    await relay(store, index).flush()

    index.fail_upsert = None
    assert await relay(store, index).flush() == 1
    assert await relay(store, index).flush() == 0  # 已 ack：不重复收敛

    assert index.upsert_calls.count(created.id) == 1
    assert [hit[0] for hit in await index.search("深色", USER_A, MemoryScope.USER_GLOBAL, 10)] == [created.id]
    assert await store.pending() == []


@pytest.mark.asyncio
async def test_relay_deletes_are_idempotent_when_the_index_row_is_gone(
    store: SqliteMemoryV2Store, index: InMemoryMemoryV2Index,
) -> None:
    created = await store.create(make_draft(), USER_A)
    await store.invalidate(created.id, USER_A)

    assert await relay(store, index).flush() == 1  # 从未 upsert 过也要能删（幂等）
    assert await store.pending() == []


# --------------------------------------------------------------------------------------
# 切片 7：检索结果的权威复核（AC7）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_visible_active_hits(
    store: SqliteMemoryV2Store,
) -> None:
    created = await store.create(make_draft(content="用户偏好深色主题"), USER_A)

    resolved = await resolve_active_hits(
        store, [(created.id, 0.9)], USER_A, MemoryScope.USER_GLOBAL)

    assert [record.id for record in resolved] == [created.id]


@pytest.mark.asyncio
async def test_resolve_drops_hits_with_no_sqlite_record(
    store: SqliteMemoryV2Store,
) -> None:
    """伪造命中：索引里有一个 SQLite 不存在的 id。"""
    assert await resolve_active_hits(
        store, [("ghost-id", 0.99)], USER_A, MemoryScope.USER_GLOBAL) == []


@pytest.mark.asyncio
async def test_resolve_drops_hits_whose_sqlite_record_is_not_active(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(content="v1"), USER_A)
    await store.update(first.id, make_draft(content="v2"), USER_A)

    assert await resolve_active_hits(
        store, [(first.id, 0.99)], USER_A, MemoryScope.USER_GLOBAL) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("intruder,scope", [
    (USER_B, MemoryScope.USER_GLOBAL),
    (OTHER_TENANT, MemoryScope.USER_GLOBAL),
])
async def test_resolve_drops_hits_the_caller_is_not_authorized_for(
    store: SqliteMemoryV2Store, intruder: TrustedMemoryIdentity, scope: MemoryScope,
) -> None:
    created = await store.create(make_draft(), USER_A)

    assert await resolve_active_hits(store, [(created.id, 0.99)], intruder, scope) == []


@pytest.mark.asyncio
async def test_resolve_keeps_user_global_hits_for_a_caller_with_a_project_context(
    store: SqliteMemoryV2Store,
) -> None:
    """带项目上下文的调用方照样能看到自己的 user_global 记忆（§4.3）。"""
    created = await store.create(make_draft(), USER_A)

    assert [record.id for record in await resolve_active_hits(
        store, [(created.id, 0.99)], PROJECT_X, MemoryScope.USER_GLOBAL)] == [created.id]


@pytest.mark.asyncio
async def test_resolve_drops_project_hits_outside_the_project(
    store: SqliteMemoryV2Store,
) -> None:
    created = await store.create(
        make_draft(scope=MemoryScope.PROJECT, project_id="project-x"), PROJECT_X)

    assert await resolve_active_hits(
        store, [(created.id, 0.99)], USER_A, MemoryScope.PROJECT) == []
    assert [record.id for record in await resolve_active_hits(
        store, [(created.id, 0.99)], PROJECT_X, MemoryScope.PROJECT)] == [created.id]


@pytest.mark.asyncio
async def test_resolve_preserves_the_ranking_order_of_surviving_hits(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(content="第一条"), USER_A)
    second = await store.create(make_draft(content="第二条"), USER_A)

    resolved = await resolve_active_hits(
        store, [(second.id, 0.5), ("ghost", 0.7), (first.id, 0.4)], USER_A, MemoryScope.USER_GLOBAL)

    assert [record.id for record in resolved] == [second.id, first.id]
    assert all(record.status is MemoryStatus.ACTIVE for record in resolved)
