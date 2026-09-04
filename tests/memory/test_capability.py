"""MemoryCapability 可替换性，LangMem 真工具经过项目 BaseStore。"""

import pytest

from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.fake_capability import FakeMemoryCapability
from agent_harness.memory.fake_vector_store import FakeVectorStore
from agent_harness.memory.outbox_relay import OutboxRelay
from agent_harness.memory.sqlite_record_store import SqliteMemoryRecordStore
from agent_harness.memory.types import MemoryScope


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["fake", "langmem"])
async def test_capability_store_recall_and_identity_isolation(tmp_path, backend):
    relay = None
    if backend == "fake":
        capability = FakeMemoryCapability()
    else:
        pytest.importorskip("langmem")
        from agent_harness.memory.langmem_capability import LangMemMemoryCapability
        records = SqliteMemoryRecordStore(tmp_path / "memory.db")
        await records.initialize()
        vectors = FakeVectorStore()
        relay = OutboxRelay(records, vectors)
        capability = LangMemMemoryCapability(records, vectors)
    token = set_identity_context(IdentityContext("acme", "alice", ["user"]))
    try:
        memory_id = await capability.store(MemoryScope.USER, "I prefer TypeScript", {"importance": 0.8})
        if relay:
            await relay.flush()
        result = await capability.recall(MemoryScope.USER, "TypeScript", 2)
        assert len(result) == 1
        assert result[0].id == memory_id
        assert result[0].content == "I prefer TypeScript"
        assert result[0].metadata["importance"] == 0.8
        for other in (IdentityContext("acme", "bob", ["user"]), IdentityContext("other", "alice", ["user"])):
            other_token = set_identity_context(other)
            try:
                assert await capability.search(MemoryScope.USER, "TypeScript", 2) == []
            finally:
                identity_context_var.reset(other_token)
    finally:
        identity_context_var.reset(token)


@pytest.mark.asyncio
async def test_langmem_manager_forms_memory_through_owned_store(tmp_path):
    pytest.importorskip("langmem")
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    from agent_harness.memory.langmem_capability import LangMemMemoryCapability

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = Model(responses=[AIMessage(content="", tool_calls=[{
        "name": "MemoryPayload", "args": {"content": "TypeScript preference", "metadata": {"importance": 0.8}},
        "id": "extract-one",
    }]), AIMessage(content="Existing preference is unchanged")])
    records = SqliteMemoryRecordStore(tmp_path / "memory.db")
    await records.initialize()
    vectors = FakeVectorStore()
    capability = LangMemMemoryCapability(records, vectors, model)
    memory_id = await capability.store(MemoryScope.USER, "I prefer TypeScript", {"importance": 0.8})
    from agent_harness.identity import get_identity_context
    assert (await records.get(memory_id, get_identity_context())).content == "TypeScript preference"
    assert await OutboxRelay(records, vectors).flush() == 1
    assert (await capability.recall(MemoryScope.USER, "TypeScript", 1))[0].id == memory_id
    assert await capability.store(MemoryScope.USER, "TypeScript preference", {"importance": 0.8}) == memory_id
    assert len(await records.list_by_scope(MemoryScope.USER, get_identity_context(), 10)) == 1


@pytest.mark.asyncio
async def test_basestore_rejects_foreign_namespace_and_ignores_untrusted_index_ids(tmp_path):
    pytest.importorskip("langmem")
    from agent_harness.memory.base_store_adapter import SqliteMilvusBaseStore
    from agent_harness.memory.types import MemoryEntry
    records = SqliteMemoryRecordStore(tmp_path / "memory.db")
    await records.initialize()
    await records.store(MemoryEntry(id="foreign", content="private", scope=MemoryScope.USER,
                                    created_at="2026-09-04"), IdentityContext("other", "bob", ["user"]))
    class UntrustedIndex(FakeVectorStore):
        async def search(self, *args):
            return [("foreign", 1.0)]
    adapter = SqliteMilvusBaseStore(records, UntrustedIndex())
    with pytest.raises(PermissionError):
        await adapter.aget(("memories", "other", "bob", "user"), "foreign")
    assert await adapter.asearch(("memories", "local", "local", "user"), query="private") == []


@pytest.mark.asyncio
async def test_nearest_memory_does_not_replace_a_different_new_candidate(tmp_path):
    pytest.importorskip("langmem")
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    from agent_harness.memory.langmem_capability import LangMemMemoryCapability
    class NoChanges(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self
    class Nearest(FakeVectorStore):
        async def search(self, *args):
            return [(old_id, 0.1)]
    records = SqliteMemoryRecordStore(tmp_path / "memory.db")
    await records.initialize()
    vector = Nearest()
    capability = LangMemMemoryCapability(records, vector)
    old_id = await capability.store(MemoryScope.USER, "I prefer Python", {})
    capability = LangMemMemoryCapability(records, vector, NoChanges(responses=[AIMessage(content="No changes")]))
    new_id = await capability.store(MemoryScope.USER, "I prefer TypeScript", {})
    assert new_id != old_id
