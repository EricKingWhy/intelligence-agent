"""SQLite 权威记录到向量索引的可重试同步。"""

import asyncio

import pytest

from agent_harness.identity import IdentityContext
from agent_harness.memory.fake_vector_store import FakeVectorStore
from agent_harness.memory.outbox_relay import OutboxRelay
from agent_harness.memory.sqlite_record_store import SqliteMemoryRecordStore
from agent_harness.memory.types import MemoryEntry, MemoryScope, memory_session_var


@pytest.mark.asyncio
async def test_poison_entry_is_dead_lettered_after_consecutive_failures(tmp_path):
    """持续失败的条目在连续 MAX 失败后被本进程死信跳过：不再尝试 upsert，
    outbox 行保留（indexed=FALSE 可观察，SQLite 仍是权威），新条目不受影响。"""
    store = SqliteMemoryRecordStore(tmp_path / "memory.db")
    await store.initialize()
    alice = IdentityContext("acme", "alice", ["user"])
    for entry_id in ("bad", "good"):
        await store.store(MemoryEntry(id=entry_id, content="c", scope=MemoryScope.USER,
                                      created_at="2026-09-04"), alice)

    class Poisoned(FakeVectorStore):
        def __init__(self):
            super().__init__()
            self.bad_attempts = 0

        async def upsert(self, memory_id, *args):
            if memory_id == "bad":
                self.bad_attempts += 1
                raise ValueError("permanent schema mismatch")
            await super().upsert(memory_id, *args)

    vector = Poisoned()
    relay = OutboxRelay(store, vector)
    for _ in range(OutboxRelay.MAX_CONSECUTIVE_FAILURES):
        await relay.flush()
    assert vector.bad_attempts == OutboxRelay.MAX_CONSECUTIVE_FAILURES
    # 死信后不再重试毒丸（flush 返回 0 且 attempts 不再增长）；good 首轮已照常同步。
    assert await relay.flush() == 0
    assert vector.bad_attempts == OutboxRelay.MAX_CONSECUTIVE_FAILURES
    assert (await store.get("good", alice)).indexed
    bad = await store.get("bad", alice)
    assert not bad.indexed and await store.pending()


@pytest.mark.asyncio
async def test_failure_counter_resets_after_success(tmp_path):
    """间歇性失败不触发死信：一次成功把连续失败计数清零，重新计满才死信。"""
    store = SqliteMemoryRecordStore(tmp_path / "memory.db")
    await store.initialize()
    alice = IdentityContext("acme", "alice", ["user"])
    await store.store(MemoryEntry(id="flaky", content="c", scope=MemoryScope.USER,
                                  created_at="2026-09-04"), alice)

    class SometimesFailing(FakeVectorStore):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def upsert(self, *args):
            if self.fail:
                self.fail = False
                raise ConnectionError("offline")
            await super().upsert(*args)

    vector = SometimesFailing()
    relay = OutboxRelay(store, vector)
    for _ in range(OutboxRelay.MAX_CONSECUTIVE_FAILURES - 1):
        await relay.flush()
        vector.fail = True  # 恢复失败态，模拟每次都差一点的间歇故障
    assert await store.pending()  # 仍未同步，但计数未达死信阈值
    vector.fail = False
    assert await relay.flush() == 1
    assert (await store.get("flaky", alice)).indexed


@pytest.mark.asyncio
async def test_outbox_failure_retries_and_restarts_without_losing_record(tmp_path):
    class Flaky(FakeVectorStore):
        fail = True

        async def upsert(self, *args):
            if self.fail:
                self.fail = False
                raise ConnectionError("offline")
            await super().upsert(*args)

    path = tmp_path / "memory.db"
    store = SqliteMemoryRecordStore(path)
    await store.initialize()
    alice = IdentityContext("acme", "alice", ["user"])
    entry = MemoryEntry(id="one", content="TypeScript", scope=MemoryScope.USER, created_at="2026-09-04")
    await store.store(entry, alice)
    vector = Flaky()
    relay = OutboxRelay(store, vector)
    assert await relay.flush() == 0
    assert not (await store.get("one", alice)).indexed
    restarted = OutboxRelay(SqliteMemoryRecordStore(path), vector)
    assert await restarted.flush() == 1
    assert (await store.get("one", alice)).indexed
    assert await restarted.flush() == 0
    assert await vector.search("TypeScript", alice, MemoryScope.USER, 5) == [("one", 1.0)]


@pytest.mark.asyncio
async def test_relay_retains_concurrent_update_and_restores_session_identity(tmp_path):
    store = SqliteMemoryRecordStore(tmp_path / "memory.db")
    await store.initialize()
    alice = IdentityContext("acme", "alice", ["session"])
    entry = MemoryEntry(id="one", content="old", scope=MemoryScope.SESSION, created_at="2026-09-04")
    token = memory_session_var.set("one-session")
    try:
        await store.store(entry, alice)
    finally:
        memory_session_var.reset(token)

    class Updating(FakeVectorStore):
        once = True

        async def upsert(self, *args):
            await super().upsert(*args)
            if self.once:
                self.once = False
                await store.store(entry.model_copy(update={"content": "new"}), alice)

    vector = Updating()
    relay = OutboxRelay(store, vector, poll_interval_seconds=0.01)
    assert await relay.flush() == 0
    assert memory_session_var.get() is None
    relay.start()
    try:
        async with asyncio.timeout(2):
            while await store.pending():
                await asyncio.sleep(0.01)
    finally:
        await relay.stop()
    token = memory_session_var.set("one-session")
    try:
        assert await vector.search("new", alice, MemoryScope.SESSION, 1) == [("one", 1.0)]
        assert (await store.get("one", alice)).indexed
    finally:
        memory_session_var.reset(token)


@pytest.mark.asyncio
async def test_failed_page_does_not_starve_later_records(tmp_path):
    store = SqliteMemoryRecordStore(tmp_path / "memory.db")
    await store.initialize()
    owner = IdentityContext("acme", "alice", ["user"])
    for number in range(101):
        await store.store(MemoryEntry(id=f"{number:03}", content="record", scope=MemoryScope.USER,
                                      created_at="2026-09-04"), owner)

    class Poisoned(FakeVectorStore):
        async def upsert(self, memory_id, *args):
            if memory_id != "100":
                raise ValueError("invalid record")
            await super().upsert(memory_id, *args)

    relay = OutboxRelay(store, Poisoned())
    assert await relay.flush() == 1
    assert (await store.get("100", owner)).indexed

