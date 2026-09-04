"""SQLite 权威记录到向量索引的可重试同步。"""

import asyncio

import pytest

from agent_harness.identity import IdentityContext
from agent_harness.memory.fake_vector_store import FakeVectorStore
from agent_harness.memory.outbox_relay import OutboxRelay
from agent_harness.memory.sqlite_record_store import SqliteMemoryRecordStore
from agent_harness.memory.types import MemoryEntry, MemoryScope, memory_session_var


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

