"""MEM-V2-1（#297）SQLite 权威存储与生命周期契约测试。

Seam：`SqliteMemoryV2Store` 的公开方法（§8.1 第 1 个 approved seam 的持久化半边）。
按垂直切片推进：一片测试 → 一片实现 → 下一片；每一片都先红后绿。

Milvus 派生/relay 与 provider 边界在各自的 seam 上另测（`test_v2_index.py` /
`test_v2_capability.py`）。
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from agent_harness.memory.v2 import (
    MemoryKind,
    MemoryRecordV2,
    MemoryScope,
    MemoryStatus,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from tests.memory.v2._records import make_draft

USER_A = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a")
USER_B = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-b")
OTHER_TENANT = TrustedMemoryIdentity(tenant_id="tenant-b", user_id="user-a")
PROJECT_X = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a", project_id="project-x")
PROJECT_Y = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a", project_id="project-y")


@pytest_asyncio.fixture
async def store(tmp_path) -> SqliteMemoryV2Store:
    instance = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await instance.initialize()
    return instance


# --------------------------------------------------------------------------------------
# 切片 1：create + get 往返（AC1）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", [MemoryKind.SEMANTIC, MemoryKind.EPISODIC, MemoryKind.PROCEDURAL],
)
async def test_create_then_get_roundtrips_every_field(
    store: SqliteMemoryV2Store, kind: MemoryKind,
) -> None:
    created = await store.create(make_draft(kind=kind), USER_A)

    fetched = await store.get(created.id, USER_A)

    assert fetched == created
    assert fetched.kind is kind
    assert fetched.status is MemoryStatus.ACTIVE
    assert fetched.version == 1
    assert fetched.root_id == created.id
    assert fetched.tenant_id == "tenant-a" and fetched.user_id == "user-a"
    assert fetched.created_at == fetched.updated_at


@pytest.mark.asyncio
async def test_create_ignores_caller_supplied_identity_and_timestamps(
    store: SqliteMemoryV2Store,
) -> None:
    """身份与时间戳由可信上下文/存储层拥有：draft 里根本没有这些字段可传。"""
    created = await store.create(make_draft(), USER_A)
    assert created.tenant_id == "tenant-a" and created.user_id == "user-a"


@pytest.mark.asyncio
async def test_get_unknown_id_raises_key_error(store: SqliteMemoryV2Store) -> None:
    with pytest.raises(KeyError):
        await store.get("does-not-exist", USER_A)


# --------------------------------------------------------------------------------------
# 切片 2：身份与作用域隔离（AC3 / R5）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("intruder", [USER_B, OTHER_TENANT])
async def test_user_global_record_is_invisible_to_other_identity(
    store: SqliteMemoryV2Store, intruder: TrustedMemoryIdentity,
) -> None:
    created = await store.create(make_draft(), USER_A)
    with pytest.raises(KeyError):
        await store.get(created.id, intruder)


@pytest.mark.asyncio
async def test_project_record_is_readable_across_sessions_of_same_project(
    store: SqliteMemoryV2Store,
) -> None:
    """AC3：在 project-x 的会话 A 写入，会话 B 仍读得到——store 不把 session 当作用域。"""
    created = await store.create(
        make_draft(scope=MemoryScope.PROJECT, project_id="project-x", source_session_id="session-a"),
        PROJECT_X,
    )

    assert (await store.get(created.id, PROJECT_X)).content == created.content


@pytest.mark.asyncio
@pytest.mark.parametrize("intruder", [PROJECT_Y, USER_B, OTHER_TENANT])
async def test_project_record_is_invisible_outside_its_project(
    store: SqliteMemoryV2Store, intruder: TrustedMemoryIdentity,
) -> None:
    created = await store.create(
        make_draft(scope=MemoryScope.PROJECT, project_id="project-x"), PROJECT_X,
    )
    with pytest.raises(KeyError):
        await store.get(created.id, intruder)


@pytest.mark.asyncio
async def test_project_draft_cannot_be_written_into_another_project(
    store: SqliteMemoryV2Store,
) -> None:
    """请求里写别的项目 id 只能被拒绝，不能落盘（§6.1 untrusted identity fields）。"""
    with pytest.raises(PermissionError):
        await store.create(
            make_draft(scope=MemoryScope.PROJECT, project_id="project-z"), PROJECT_X)


@pytest.mark.asyncio
async def test_user_global_record_is_readable_without_project_context(
    store: SqliteMemoryV2Store,
) -> None:
    """user_global 的可见性不依赖项目上下文（PRD §4.3：跨所有会话与项目复用）。"""
    created = await store.create(make_draft(), USER_A)
    assert (await store.get(created.id, TrustedMemoryIdentity("tenant-a", "user-a"))).id == created.id
    assert (await store.get(created.id, PROJECT_X)).id == created.id


# --------------------------------------------------------------------------------------
# 切片 3：版本与 supersession（AC4 / R3 / R4）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_creates_next_version_and_supersedes_previous(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(content="初版事实"), USER_A)

    second = await store.update(first.id, make_draft(content="修订后的事实"), USER_A)

    assert second.root_id == first.root_id
    assert second.version == 2
    assert second.status is MemoryStatus.ACTIVE
    assert second.id != first.id

    superseded = await store.get(first.id, USER_A)
    assert superseded.status is MemoryStatus.SUPERSEDED
    assert superseded.superseded_by == second.id
    assert superseded.content == "初版事实"  # 历史内容保留（§5.4.1）


@pytest.mark.asyncio
async def test_version_history_lists_all_versions_newest_first(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(content="v1"), USER_A)
    second = await store.update(first.id, make_draft(content="v2"), USER_A)
    third = await store.update(second.id, make_draft(content="v3"), USER_A)

    history = await store.list_versions(first.root_id, USER_A)

    assert [record.version for record in history] == [3, 2, 1]
    assert [record.id for record in history] == [third.id, second.id, first.id]
    assert history[0].status is MemoryStatus.ACTIVE
    assert {record.status for record in history[1:]} == {MemoryStatus.SUPERSEDED}


@pytest.mark.asyncio
async def test_at_most_one_active_version_per_logical_memory(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(content="v1"), USER_A)
    second = await store.update(first.id, make_draft(content="v2"), USER_A)
    third = await store.update(second.id, make_draft(content="v3"), USER_A)

    actives = await store.list_active(USER_A, limit=50)

    assert [record.id for record in actives] == [third.id]


@pytest.mark.asyncio
async def test_update_rejects_record_owned_by_another_identity(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(), USER_A)
    with pytest.raises(PermissionError):
        await store.update(first.id, make_draft(content="越权改写"), USER_B)


@pytest.mark.asyncio
async def test_update_rejects_record_that_is_not_active(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(content="v1"), USER_A)
    second = await store.update(first.id, make_draft(content="v2"), USER_A)
    with pytest.raises(ValueError):
        await store.update(first.id, make_draft(content="基于过期版本的分支"), USER_A)
    assert (await store.get(second.id, USER_A)).version == 2


@pytest.mark.asyncio
async def test_update_cannot_move_a_memory_into_another_scope(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(), USER_A)
    with pytest.raises(ValueError):
        await store.update(
            first.id, make_draft(scope=MemoryScope.PROJECT, project_id="project-x"), PROJECT_X)
    assert (await store.get(first.id, USER_A)).status is MemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_version_history_of_another_identity_raises_key_error(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(), USER_A)
    with pytest.raises(KeyError):
        await store.list_versions(first.root_id, USER_B)


# --------------------------------------------------------------------------------------
# 切片 4：invalidation 与 active 检索面（AC5 / §5.4.2）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_removes_from_active_but_keeps_history(
    store: SqliteMemoryV2Store,
) -> None:
    created = await store.create(make_draft(content="后来不再成立"), USER_A)

    invalidated = await store.invalidate(created.id, USER_A)

    assert invalidated.status is MemoryStatus.INVALIDATED
    assert invalidated.invalidated_at is not None
    assert await store.list_active(USER_A, limit=50) == []
    assert (await store.get(created.id, USER_A)).content == "后来不再成立"


@pytest.mark.asyncio
async def test_invalidate_is_rejected_for_non_active_record(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(content="v1"), USER_A)
    second = await store.update(first.id, make_draft(content="v2"), USER_A)
    with pytest.raises(ValueError):
        await store.invalidate(first.id, USER_A)
    assert (await store.get(second.id, USER_A)).status is MemoryStatus.ACTIVE


@pytest.mark.asyncio
async def test_invalidate_rejects_record_owned_by_another_identity(
    store: SqliteMemoryV2Store,
) -> None:
    created = await store.create(make_draft(), USER_A)
    with pytest.raises(PermissionError):
        await store.invalidate(created.id, USER_B)


@pytest.mark.asyncio
async def test_list_active_filters_by_scope_and_identity(
    store: SqliteMemoryV2Store,
) -> None:
    await store.create(make_draft(content="全局偏好"), USER_A)
    await store.create(
        make_draft(scope=MemoryScope.PROJECT, project_id="project-x", content="项目事实"), PROJECT_X)
    await store.create(make_draft(content="他人的偏好"), USER_B)

    global_only = await store.list_active(USER_A, scope=MemoryScope.USER_GLOBAL, limit=50)
    project_only = await store.list_active(PROJECT_X, scope=MemoryScope.PROJECT, limit=50)

    assert [record.content for record in global_only] == ["全局偏好"]
    assert [record.content for record in project_only] == ["项目事实"]


@pytest.mark.asyncio
async def test_list_active_project_scope_is_empty_without_project_context(
    store: SqliteMemoryV2Store,
) -> None:
    await store.create(
        make_draft(scope=MemoryScope.PROJECT, project_id="project-x"), PROJECT_X)
    assert await store.list_active(USER_A, scope=MemoryScope.PROJECT, limit=50) == []


# --------------------------------------------------------------------------------------
# 切片 5：派生索引的 outbox 入队（AC6 前半 / §6.6）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_enqueues_one_upsert(store: SqliteMemoryV2Store) -> None:
    created = await store.create(make_draft(), USER_A)
    pending = await store.pending()
    assert [(change.operation, change.memory_id) for change in pending] == [("upsert", created.id)]
    assert pending[0].record == created


@pytest.mark.asyncio
async def test_update_enqueues_upsert_for_new_version_and_delete_for_old(
    store: SqliteMemoryV2Store,
) -> None:
    first = await store.create(make_draft(content="v1"), USER_A)
    await store.acknowledge((await store.pending())[0])  # 清掉 create 的那条

    second = await store.update(first.id, make_draft(content="v2"), USER_A)

    pending = await store.pending()
    assert {(change.operation, change.memory_id) for change in pending} == {
        ("upsert", second.id), ("delete", first.id),
    }


@pytest.mark.asyncio
async def test_invalidate_enqueues_delete(store: SqliteMemoryV2Store) -> None:
    created = await store.create(make_draft(), USER_A)
    await store.acknowledge((await store.pending())[0])

    await store.invalidate(created.id, USER_A)

    pending = await store.pending()
    assert [(change.operation, change.memory_id) for change in pending] == [("delete", created.id)]
    assert pending[0].record is None


@pytest.mark.asyncio
async def test_acknowledge_matches_revision_and_is_idempotent(
    store: SqliteMemoryV2Store,
) -> None:
    await store.create(make_draft(), USER_A)
    change = (await store.pending())[0]

    assert await store.acknowledge(change) is True
    assert await store.acknowledge(change) is False  # 第二次：已经 ack 过，不重复计数
    assert await store.pending() == []


@pytest.mark.asyncio
async def test_pending_carries_the_routing_facts_the_relay_needs(
    store: SqliteMemoryV2Store,
) -> None:
    """relay 只有 outbox 行可用（记录行可能已不在），路由事实必须自足。"""
    created = await store.create(
        make_draft(scope=MemoryScope.PROJECT, project_id="project-x"), PROJECT_X)
    change = (await store.pending())[0]

    assert change.tenant_id == "tenant-a"
    assert change.user_id == "user-a"
    assert change.scope is MemoryScope.PROJECT
    assert change.project_id == "project-x"
    assert change.memory_id == created.id
    assert change.revision


# --------------------------------------------------------------------------------------
# 切片 6：并发写入下的 active 不变量（R3 / AC4）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_updates_leave_exactly_one_active_version(
    store: SqliteMemoryV2Store,
) -> None:
    """R3 的保证在数据库层，不在调用方的纪律：两个写者同时基于 v1 派生下一版。

    胜者产出 v2。败者必然失败——要么读到的 v1 已被置 superseded（`_require_active`
    抛 ValueError），要么自己那条 INSERT 撞上 `memory_v2_one_active` 部分唯一索引
    （转成 ValueError）。两条路径都是 ValueError，所以断言不依赖调度顺序；
    败者事务整体回滚，因此胜者写下的 `superseded_by` 不会被改脏。
    """
    first = await store.create(make_draft(content="v1"), USER_A)

    results = await asyncio.gather(
        store.update(first.id, make_draft(content="写者 A 的修订"), USER_A),
        store.update(first.id, make_draft(content="写者 B 的修订"), USER_A),
        return_exceptions=True,
    )

    winners = [result for result in results if isinstance(result, MemoryRecordV2)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(winners) == 1, results
    assert [type(failure) for failure in failures] == [ValueError]

    winner = winners[0]
    assert winner.version == 2
    assert [record.id for record in await store.list_active(USER_A, limit=50)] == [winner.id]

    history = await store.list_versions(first.root_id, USER_A)
    assert [(record.version, record.status) for record in history] == [
        (2, MemoryStatus.ACTIVE), (1, MemoryStatus.SUPERSEDED),
    ]
    assert history[1].superseded_by == winner.id
