"""Real Zilliz + embedding Gate; simulated conversation, real storage/provider path."""

import asyncio
from uuid import uuid4

import pytest
from pydantic import SecretStr

from agent_harness.config import Settings
from agent_harness.identity import IdentityContext
from agent_harness.memory.milvus_vector_store import MilvusVectorStore
from agent_harness.memory.types import MemoryScope
from agent_harness.memory.vector_store import VectorStoreError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def gate_settings():
    settings = Settings()
    if not settings.milvus_uri or not settings.milvus_token.get_secret_value():
        pytest.skip("Real Milvus connection is not configured")
    if settings.milvus_collection != "memory_gate_test":
        pytest.skip("Gate requires the dedicated memory_gate_test collection")
    return settings


async def test_real_connection_and_missing_collection(gate_settings):
    vectors = MilvusVectorStore(gate_settings)
    try:
        collections = await vectors.connect()
        assert isinstance(collections, list)
    finally:
        await vectors.close()

    # A unique missing name verifies query error mapping without mutating any collection.
    missing = gate_settings.model_copy(update={"milvus_collection": "memory_gate_absent_" + uuid4().hex})
    vectors = MilvusVectorStore(missing)
    try:
        await vectors.connect()
        with pytest.raises(VectorStoreError) as error:
            await vectors.get("gate-no-record", IdentityContext("gate", "alice", ["user"]), MemoryScope.USER)
        assert error.value.code == "collection_not_found"
    finally:
        await vectors.close()


async def test_real_invalid_token_is_mapped(gate_settings):
    # Never modify the actual token or put it into assertions / exception messages.
    invalid = gate_settings.model_copy(update={"milvus_token": SecretStr("gate-deliberately-invalid-token")})
    vectors = MilvusVectorStore(invalid)
    try:
        with pytest.raises(VectorStoreError) as error:
            await vectors.connect()
        assert error.value.code == "authentication"
    finally:
        await vectors.close()


async def test_real_memory_runtime_semantics_and_cleanup(gate_settings, tmp_path):
    from langchain_core.messages import AIMessage

    from agent_harness.agent import AgentRuntime
    from agent_harness.identity import identity_context_var, set_identity_context
    from agent_harness.memory.context_provider import MemoryContextProvider
    from agent_harness.memory.extractor import MemoryExtractor
    from agent_harness.memory.langmem_capability import LangMemMemoryCapability
    from agent_harness.memory.outbox_relay import OutboxRelay
    from agent_harness.memory.sqlite_record_store import SqliteMemoryRecordStore
    from agent_harness.memory.types import memory_session_var
    from agent_harness.memory.writeback import MemoryWriteback
    from agent_harness.session import USER_MESSAGE
    from agent_harness.tooling import ToolExecutor, ToolRegistry
    from tests.conftest import make_session
    from tests.scripted_model import ScriptedModel

    if not gate_settings.embedding_api_key.get_secret_value() or not gate_settings.embedding_model:
        pytest.skip("Real embedding model is not configured")

    def gate_embeddings(settings):
        """Gate 专用嵌入客户端：真实云厂商存在负载波动，本测试验证的是记忆语义
        而非嵌入重试策略（生产保持快失败 + 降级事件，见 embeddings.py）。"""
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(
            model=settings.embedding_model, base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key, check_embedding_ctx_length=False,
            dimensions=settings.embedding_dimensions, request_timeout=30, max_retries=3,
        )

    vectors = MilvusVectorStore(gate_settings, gate_embeddings(gate_settings))
    records = SqliteMemoryRecordStore(tmp_path / "memory.db")
    await records.initialize()
    capability = LangMemMemoryCapability(records, vectors)
    relay = OutboxRelay(records, vectors)
    session = make_session(tmp_path / "sessions")
    alice = IdentityContext("gate_" + uuid4().hex, "alice", ["user", "session"])
    identity_token = set_identity_context(alice)
    session_token = memory_session_var.set(session.session_id)
    # Control conversation outputs so this gate tests storage and semantic retrieval,
    # independently of nondeterministic chat-model wording. Extractor itself is real.
    extractor = MemoryExtractor(ScriptedModel([AIMessage(content=
        '[{"scope":"user","content":"我喜欢使用 TypeScript 开发应用。","importance":0.9}]')]))
    writer = MemoryWriteback(capability, extractor)
    try:
        await vectors.initialize()
        assert vectors.dimension == gate_settings.embedding_dimensions
        registry = ToolRegistry()
        runtime = AgentRuntime(ScriptedModel([AIMessage(content="已记录你的偏好。")]), registry,
                               ToolExecutor(registry), memory_writer=writer)
        await runtime.run(session, "我喜欢 TypeScript")
        await writer.drain()
        assert not any(e.type == "memory/degraded" for e in session.events)
        preference = (await records.list_by_scope(MemoryScope.USER, alice, 20))[0]
        other_id = await capability.store(MemoryScope.USER, "鲸鱼是生活在海洋中的哺乳动物。", {"importance": 0.2})
        # 真实 embedding 服务存在瞬态失败；outbox 语义保证失败条目被保留、
        # 下轮 flush 重新 upsert。这里按该保证重试排空——同时验证持久 outbox 本身。
        async with asyncio.timeout(240):
            acknowledged = 0
            while True:
                acknowledged += await relay.flush()
                if not await records.pending():
                    break
                await asyncio.sleep(2)
        assert acknowledged == 2
        assert await records.pending() == []
        stored = await vectors.get(preference.id, alice, MemoryScope.USER)
        assert stored["memory_id"] == preference.id and "TypeScript" in stored["content"]
        assert stored["metadata"]["importance"] == 0.9
        hits = await capability.search(MemoryScope.USER, "我偏好的编程语言是什么？", 2)
        assert len(hits) == 2
        assert hits[0].id == preference.id and hits[1].id == other_id
        assert hits[0].score > hits[1].score
        assert -1.001 <= hits[1].score <= hits[0].score <= 1.001
        assert len(await capability.search(MemoryScope.USER, "编程语言偏好", 1)) == 1

        second = make_session(tmp_path / "sessions")
        second.append(USER_MESSAGE, {"content": "我偏好的编程语言是什么？"})
        context = MemoryContextProvider(capability, timeout_seconds=60)
        messages = await context.select(second, 1000)
        assert len(messages) == 1 and "TypeScript" in messages[0].content
        for identity in (IdentityContext(alice.tenant_id, "bob", ["user"]),
                         IdentityContext(alice.tenant_id + "_other", "alice", ["user"])):
            token = set_identity_context(identity)
            try:
                assert await capability.search(MemoryScope.USER, "编程语言", 20) == []
                assert await vectors.get(preference.id, identity, MemoryScope.USER) is None
                assert await context.select(second, 1000) == []
            finally:
                identity_context_var.reset(token)

        # A genuine alternate supported output dimension must reject the existing schema.
        mismatched_embeddings = gate_embeddings(gate_settings)
        mismatched_embeddings.dimensions = 64 if vectors.dimension != 64 else 128
        mismatch = MilvusVectorStore(gate_settings, mismatched_embeddings)
        try:
            with pytest.raises(VectorStoreError, match="schema_mismatch"):
                await mismatch.initialize()
        finally:
            await mismatch.close()
    finally:
        await writer.close()
        await relay.stop()
        try:
            for entry in await records.list_by_scope(MemoryScope.USER, alice, 100):
                await vectors.delete(entry.id, alice, MemoryScope.USER)
                assert await vectors.get(entry.id, alice, MemoryScope.USER) is None
            created = vectors.created_collection
            await vectors.drop_created_collection()
            if created:
                assert gate_settings.milvus_collection not in await vectors.connect()
        finally:
            await vectors.close()
            memory_session_var.reset(session_token)
            identity_context_var.reset(identity_token)
