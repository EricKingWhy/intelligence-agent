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


async def test_real_forget_propagates_to_milvus_and_a_real_count_confirms_no_residue(gate_settings, tmp_path):
    """MEM-1（#156）AC4 的真实后端验收：`forget` 必须真的从向量索引里消失。

    两个容易骗过自己的点，所以断言按"不能骗自己"的方式写：
    1. `search` 检索不到 **不等于** 没有残留（命中会受相似度/limit 影响）——所以判定
       残留用 `query(output_fields=["count(*)"])` 的**真实 count**，不用
       `get_collection_stats`（它是惰性陈旧值）；
    2. 单独一个 0 不能证明 filter 写对了——所以留一条**对照组**记忆，它必须一直是 1。
    忘掉目标后目标变 0、对照仍 1，才是"删对了那一条"的证据。
    """
    from langchain_openai import OpenAIEmbeddings

    from agent_harness.identity import identity_context_var, set_identity_context
    from agent_harness.memory.langmem_capability import LangMemMemoryCapability
    from agent_harness.memory.outbox_relay import OutboxRelay
    from agent_harness.memory.sqlite_record_store import SqliteMemoryRecordStore

    if not gate_settings.embedding_api_key.get_secret_value() or not gate_settings.embedding_model:
        pytest.skip("Real embedding model is not configured")

    embeddings = OpenAIEmbeddings(
        model=gate_settings.embedding_model, base_url=gate_settings.embedding_base_url,
        api_key=gate_settings.embedding_api_key, check_embedding_ctx_length=False,
        dimensions=gate_settings.embedding_dimensions, request_timeout=30, max_retries=3,
    )
    vectors = MilvusVectorStore(gate_settings, embeddings)
    records = SqliteMemoryRecordStore(tmp_path / "memory.db")
    await records.initialize()
    capability = LangMemMemoryCapability(records, vectors)
    relay = OutboxRelay(records, vectors)
    alice = IdentityContext("gate_forget_" + uuid4().hex, "alice", ["user"])
    token = set_identity_context(alice)

    async def drain() -> int:
        """真实 embedding 服务有瞬态失败；outbox 的保证是"失败保留、下轮重试"，
        所以按该保证重试排空（同时也在验证这条保证本身）。"""
        acknowledged = 0
        async with asyncio.timeout(240):
            while True:
                acknowledged += await relay.flush()
                if not await records.pending():
                    return acknowledged
                await asyncio.sleep(2)

    async def real_count(memory_id: str) -> int:
        """查询一条记忆在当前 filter 下的**真实**行数（Strong 一致性，不用 stats）。"""
        expression, params = MilvusVectorStore._filter(alice, MemoryScope.USER)
        rows = await vectors._call(
            "query", collection_name=gate_settings.milvus_collection,
            filter=expression + " AND memory_id == {memory}",
            filter_params={**params, "memory": memory_id},
            output_fields=["count(*)"], consistency_level="Strong")
        return int(rows[0]["count(*)"])

    try:
        await vectors.initialize()
        doomed = await capability.store(MemoryScope.USER, "我喜欢用 Rust 写解析器。", {"importance": 0.7})
        control = await capability.store(MemoryScope.USER, "鲸鱼是生活在海洋中的哺乳动物。", {"importance": 0.1})
        await drain()
        assert await records.pending() == []
        assert await real_count(doomed) == 1
        assert await real_count(control) == 1
        # 真实向量检索返回的是 `limit` 个最近邻、**没有相似度下限**：两条记忆都在索引里，
        # 于是这一问会同时返回两条（目标在前、对照在后）。所以"命中"只能按 id 成员断言，
        # 不能假设"不相关的那条不会被返回"。
        hits = await capability.search(MemoryScope.USER, "Rust 解析器", 5)
        assert hits[0].id == doomed and {hit.id for hit in hits} == {doomed, control}

        assert await capability.forget(doomed) is True
        await drain()
        assert await records.pending() == []

        assert await vectors.get(doomed, alice, MemoryScope.USER) is None
        remaining = await capability.search(MemoryScope.USER, "Rust 解析器", 5)
        assert [hit.id for hit in remaining] == [control]  # 目标不再出现在检索结果里
        assert await real_count(doomed) == 0  # 真值：索引里这一条已不存在
        assert await real_count(control) == 1  # filter 与清理范围都正确（对照组还在）
        assert await capability.forget(doomed) is False  # 幂等
    finally:
        try:
            for entry in await records.list_by_scope(MemoryScope.USER, alice, 100):
                await capability.forget(entry.id)
            await drain()
            created = vectors.created_collection
            await vectors.drop_created_collection()
            if created:
                assert gate_settings.milvus_collection not in await vectors.connect()
        finally:
            await relay.stop()
            await vectors.close()
            identity_context_var.reset(token)
