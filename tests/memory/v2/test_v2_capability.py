"""MEM-V2-1（#297）provider-neutral 记忆边界的端到端契约测试。

Seam：`MemoryV2Capability` 的七个方法。测试只通过这个 Protocol 调用，
不碰 SQLite 表、不碰索引内部——这正是"边界存在"的可判定证据：
下面每一个断言都只用 Protocol 上的公开操作表达。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from agent_harness.memory.v2 import (
    MemoryScope,
    MemoryStatus,
    MemoryV2Capability,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.index import InMemoryMemoryV2Index, MemoryV2IndexRelay
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from tests.memory.v2._records import make_draft

USER_A = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a")
PROJECT_X = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a", project_id="project-x")
USER_B = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-b")


@pytest_asyncio.fixture
async def service(tmp_path):
    store = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await store.initialize()
    index = InMemoryMemoryV2Index()
    return MemoryV2Service(store, index), store, index


# --------------------------------------------------------------------------------------
# 切片 8：通过 Protocol 的完整生命周期
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_created_memory_becomes_searchable_after_index_converges(service) -> None:
    capability, store, index = service

    created = await capability.create(make_draft(content="用户偏好深色主题"), USER_A)
    await MemoryV2IndexRelay(store, index).flush()

    hits = await capability.search("深色", USER_A, scope=MemoryScope.USER_GLOBAL, limit=10)

    assert [record.id for record in hits] == [created.id]
    assert (await capability.read(created.id, USER_A)) == created


@pytest.mark.asyncio
async def test_searched_memory_never_exposes_superseded_or_invalidated(service) -> None:
    """AC5 经 Protocol：两种"不再有效"的版本都不得出现在检索结果里。"""
    capability, store, index = service
    relay = MemoryV2IndexRelay(store, index)

    superseded = await capability.create(make_draft(content="偏好浅色主题"), USER_A)
    current = await capability.update(superseded.id, make_draft(content="偏好深色主题"), USER_A)
    invalidated = await capability.create(make_draft(content="偏好中等主题"), USER_A)
    await capability.invalidate(invalidated.id, USER_A)
    await relay.flush()

    hits = await capability.search("主题", USER_A, scope=MemoryScope.USER_GLOBAL, limit=10)

    assert [record.id for record in hits] == [current.id]
    assert await capability.versions(current.root_id, USER_A) != []


@pytest.mark.asyncio
async def test_project_memory_is_searchable_only_inside_its_project(service) -> None:
    capability, store, index = service
    created = await capability.create(
        make_draft(scope=MemoryScope.PROJECT, project_id="project-x", content="项目构建命令"),
        PROJECT_X,
    )
    await MemoryV2IndexRelay(store, index).flush()

    inside = await capability.search("构建", PROJECT_X, scope=MemoryScope.PROJECT, limit=10)
    outside = await capability.search("构建", USER_A, scope=MemoryScope.PROJECT, limit=10)

    assert [record.id for record in inside] == [created.id]
    assert outside == []


@pytest.mark.asyncio
async def test_other_identity_cannot_read_or_search_a_memory(service) -> None:
    capability, store, index = service
    created = await capability.create(make_draft(content="用户偏好深色主题"), USER_A)
    await MemoryV2IndexRelay(store, index).flush()

    with pytest.raises(KeyError):
        await capability.read(created.id, USER_B)
    assert await capability.search("深色", USER_B, scope=MemoryScope.USER_GLOBAL, limit=10) == []


@pytest.mark.asyncio
async def test_rejected_write_leaves_no_record_and_no_index_intent(service) -> None:
    """AC2：拒绝必须**什么都不留下**——没有记录行，也没有留给 relay 的索引意图。"""
    capability, store, _index = service

    with pytest.raises(PermissionError):
        await capability.create(make_draft(scope=MemoryScope.PROJECT, project_id="project-z"), PROJECT_X)

    assert await capability.list_active(PROJECT_X, scope=MemoryScope.PROJECT, limit=10) == []
    assert await capability.list_active(USER_A, scope=MemoryScope.USER_GLOBAL, limit=10) == []
    assert await store.pending() == []


@pytest.mark.asyncio
async def test_invalid_draft_cannot_be_constructed_at_all() -> None:
    """AC2 的另一半：类型层就拒绝的输入根本进不到存储，谈不上"部分写入"。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_draft(content="x" * 501)


@pytest.mark.asyncio
async def test_lifecycle_is_versioned_and_status_is_observable_via_protocol(service) -> None:
    capability, _store, _index = service
    first = await capability.create(make_draft(content="v1"), USER_A)
    second = await capability.update(first.id, make_draft(content="v2"), USER_A)

    history = await capability.versions(first.root_id, USER_A)

    assert [record.version for record in history] == [2, 1]
    assert history[0].status is MemoryStatus.ACTIVE and history[0].id == second.id
    assert history[1].status is MemoryStatus.SUPERSEDED


def test_service_implements_every_method_the_capability_protocol_declares() -> None:
    """`MemoryV2Service` 落地 `MemoryV2Capability` 的全部方法——"边界是 Protocol"的落地检查。

    方法名**从 Protocol 自己派生**（不在测试里再抄一份）：抄一份等于造第二份契约，
    Protocol 加了方法而实现漏了时这条不会红，恰好丢掉它要守的东西。
    第一行是正控：真的读到了方法名，否则下面的断言对空列表恒真。
    """
    declared = [
        name for name, value in vars(MemoryV2Capability).items()
        if callable(value) and not name.startswith("_")
    ]
    assert declared
    missing = [name for name in declared if not callable(getattr(MemoryV2Service, name, None))]
    assert missing == []
