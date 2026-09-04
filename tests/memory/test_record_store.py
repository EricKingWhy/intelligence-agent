"""Memory 权威记录契约，SQLite 与 Fake 使用同一组验收。"""

import pytest
import pytest_asyncio

from agent_harness.identity import IdentityContext
from agent_harness.memory.fake_record_store import FakeMemoryRecordStore
from agent_harness.memory.sqlite_record_store import SqliteMemoryRecordStore
from agent_harness.memory.types import (
    MemoryEntry,
    MemoryScope,
    memory_session_var,
    scope_to_namespace,
)


@pytest_asyncio.fixture(params=["sqlite", "fake"])
async def store(request, tmp_path):
    store = (SqliteMemoryRecordStore(tmp_path / "memory.db")
             if request.param == "sqlite" else FakeMemoryRecordStore())
    await store.initialize()
    return store


def entry(memory_id="m1"):
    return MemoryEntry(id=memory_id, content="I prefer TypeScript", metadata={"importance": 0.8},
                       created_at="2026-09-04T00:00:00+00:00", scope=MemoryScope.USER)


@pytest.mark.asyncio
async def test_store_and_get_roundtrip(store):
    identity = IdentityContext("acme", "alice", ["user"])
    assert await store.store(entry(), identity) == "m1"
    assert await store.get("m1", identity) == entry()
    assert await store.list_by_scope(MemoryScope.USER, identity, 10) == [entry()]
    assert scope_to_namespace(MemoryScope.USER, identity) == ("memories", "acme", "alice", "user")


@pytest.mark.asyncio
@pytest.mark.parametrize("other", [IdentityContext("other", "alice", ["user"]),
                                  IdentityContext("acme", "bob", ["user"])])
async def test_identity_cannot_read_or_overwrite_another_owner(store, other):
    owner = IdentityContext("acme", "alice", ["user"])
    await store.store(entry(), owner)
    with pytest.raises(KeyError):
        await store.get("m1", other)
    assert await store.list_by_scope(MemoryScope.USER, other, 10) == []
    with pytest.raises(PermissionError):
        await store.store(entry(), other)
    assert await store.get("m1", owner) == entry()


@pytest.mark.asyncio
async def test_scope_permission_is_enforced(store):
    with pytest.raises(PermissionError):
        await store.store(entry(), IdentityContext("acme", "alice", []))
    with pytest.raises(NotImplementedError):
        await store.list_by_scope(MemoryScope.GLOBAL, IdentityContext("acme", "alice", ["global"]), 10)


@pytest.mark.asyncio
async def test_store_does_not_persist_query_score_or_trust_indexed_flag(store):
    identity = IdentityContext("acme", "alice", ["user"])
    await store.store(entry().model_copy(update={"score": 0.9, "indexed": True}), identity)
    stored = await store.get("m1", identity)
    assert stored.score is None
    assert stored.indexed is False


@pytest.mark.asyncio
async def test_sqlite_record_and_outbox_commit_atomically(tmp_path):
    import aiosqlite

    path = tmp_path / "memory.db"
    store = SqliteMemoryRecordStore(path)
    await store.initialize()
    identity = IdentityContext("acme", "alice", ["user"])
    # 数据库故障注入，验证权威记录不会在 outbox 写失败时单独提交。
    async with aiosqlite.connect(path) as db:
        await db.execute("""CREATE TRIGGER fail_outbox BEFORE INSERT ON memory_outbox
                         BEGIN SELECT RAISE(ABORT, 'outbox unavailable'); END""")
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await store.store(entry(), identity)
    with pytest.raises(KeyError):
        await store.get("m1", identity)
    async with aiosqlite.connect(path) as db:
        await db.execute("DROP TRIGGER fail_outbox")
        await db.commit()
    await store.store(entry(), identity)
    restarted = SqliteMemoryRecordStore(path)
    assert await restarted.get("m1", identity) == entry()
    async with (aiosqlite.connect(path) as db,
                db.execute("SELECT memory_id FROM memory_outbox") as cursor):
        assert await cursor.fetchall() == [("m1",)]


@pytest.mark.asyncio
async def test_updates_preserve_created_time_and_do_not_share_metadata(store):
    identity = IdentityContext("acme", "alice", ["user"])
    original = entry()
    await store.store(original, identity)
    original.metadata["importance"] = 0
    assert (await store.get("m1", identity)).metadata["importance"] == 0.8
    changed = entry().model_copy(update={"created_at": "2026-09-05T00:00:00+00:00", "content": "updated"})
    await store.store(changed, identity)
    stored = await store.get("m1", identity)
    assert stored.content == "updated"
    assert stored.created_at == "2026-09-04T00:00:00+00:00"


@pytest.mark.asyncio
async def test_session_scope_does_not_cross_sessions(store):
    owner = IdentityContext("acme", "alice", ["user", "session"])
    scoped = entry().model_copy(update={"scope": MemoryScope.SESSION})
    token = memory_session_var.set("session-a")
    try:
        await store.store(scoped, owner)
        assert await store.get("m1", owner) == scoped
        assert scope_to_namespace(MemoryScope.SESSION, owner) == (
            "memories", "acme", "alice", "session", "session-a",
        )
        other_token = memory_session_var.set("session-b")
        try:
            assert await store.list_by_scope(MemoryScope.SESSION, owner, 10) == []
            with pytest.raises(KeyError):
                await store.get("m1", owner)
            with pytest.raises(PermissionError):
                await store.store(scoped, owner)
        finally:
            memory_session_var.reset(other_token)
    finally:
        memory_session_var.reset(token)
    with pytest.raises(ValueError):
        await store.store(scoped, owner)
