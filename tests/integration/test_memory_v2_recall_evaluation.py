"""Frozen Recall@6 against a unique, real Milvus collection and configured embeddings."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from agent_harness.config import Settings
from agent_harness.memory.embeddings import create_embeddings
from agent_harness.memory.milvus_vector_store import MilvusVectorStore
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.index import MemoryV2IndexRelay
from agent_harness.memory.v2.milvus_index import MilvusMemoryV2Index
from agent_harness.memory.v2.recall import RANKING_VERSION
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import (
    EvidenceItem,
    MemoryScope,
    SemanticCategory,
    SemanticPayload,
    TrustedMemoryIdentity,
)
from agent_harness.session import USER_MESSAGE, JsonlSessionStore, Session
from evaluation.memory_v2_recall import dataset_sha256, load_dataset, recall_at_k
from tests.memory.v2._records import make_draft

pytestmark = [pytest.mark.integration, pytest.mark.live_services, pytest.mark.asyncio]


class _CachedEmbeddings:
    """Reuse real provider vectors so each Milvus operation avoids another model request."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[text] for text in texts]

    async def aembed_query(self, text: str) -> list[float]:
        return self._vectors[text]


async def test_live_project_cross_session_recall_at_six(tmp_path) -> None:
    settings = Settings()
    if not settings.milvus_uri or not settings.milvus_token.get_secret_value():
        pytest.skip("live Milvus credentials are not configured")
    if not all((
        settings.embedding_model,
        settings.embedding_base_url,
        settings.embedding_api_key.get_secret_value(),
    )):
        pytest.skip("live embedding credentials are not configured")

    dataset = load_dataset()
    collection_name = f"memv2r_{uuid4().hex[:12]}"
    identity = TrustedMemoryIdentity(
        tenant_id=f"eval-tenant-{uuid4().hex}",
        user_id=f"eval-user-{uuid4().hex}",
        project_id=f"eval-project-{uuid4().hex}",
    )
    source_session_id = dataset["source_session_id"]
    query_session_prefix = dataset["query_session_prefix"]
    source_texts = [memory["content"] for memory in dataset["memories"]]
    query_texts = [query["text"] for query in dataset["queries"]]
    probe_text = "memory index dimension probe"
    embedding_texts = [probe_text, *source_texts, *query_texts]

    live_embeddings = create_embeddings(settings)
    embedded: list[list[float]] = []
    try:
        # The configured client has a 15-second request timeout; keep this frozen batch bounded.
        for offset in range(0, len(embedding_texts), 12):
            embedded.extend(await live_embeddings.aembed_documents(embedding_texts[offset:offset + 12]))
    except Exception as error:  # noqa: BLE001 — keep provider diagnostics and credentials out of output.
        pytest.fail(f"configured embedding request failed ({type(error).__name__})", pytrace=False)
    cached = _CachedEmbeddings(dict(zip(embedding_texts, embedded, strict=True)))
    vector_settings = settings.model_copy(update={"milvus_collection": collection_name})
    vectors = MilvusVectorStore(vector_settings, cached)
    service: MemoryV2Service | None = None
    collection_created = False
    evidence: dict | None = None

    try:
        await vectors.initialize()
        collection_created = vectors.created_collection
        assert collection_created, "evaluation must own a new isolated Milvus collection"

        store = SqliteMemoryV2Store(tmp_path / "memory-v2-recall.db")
        await store.initialize()
        index = MilvusMemoryV2Index(vectors)
        relay = MemoryV2IndexRelay(store, index)
        service = MemoryV2Service(store, index, relay=relay)

        memory_ids: dict[str, str] = {}
        for item in dataset["memories"]:
            content = item["content"]
            draft = make_draft(
                content=content,
                scope=MemoryScope.PROJECT,
                project_id=identity.project_id,
                source_session_id=source_session_id,
                source_event_ids=[f"gold-event-{item['id']}"],
                payload=SemanticPayload(
                    subject=item["id"], fact=content,
                    category=SemanticCategory.PROJECT_FACT,
                ),
                evidence=[EvidenceItem(
                    role="user", excerpt=item["source"],
                    hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                )],
            )
            record = await service.create(draft, identity)
            memory_ids[record.id] = item["id"]
        await relay.flush()

        sessions = JsonlSessionStore(root=tmp_path / "query-sessions")
        ranked_ids: dict[str, list[str]] = {}
        target_factors: dict[str, dict[str, float | int | str]] = {}
        for query in dataset["queries"]:
            query_session_id = f"{query_session_prefix}-{query['id']}"
            assert query_session_id != source_session_id
            session = Session.start(sessions, session_id=query_session_id)
            session.append(USER_MESSAGE, {"content": query["text"]})
            request = "\n".join(str(message.content) for message in session.derive_messages())
            hits = await service.hybrid_search(
                request, identity, scopes=(MemoryScope.PROJECT,), limit=dataset["k"],
            )
            ranked_ids[query["id"]] = [memory_ids[hit.record.id] for hit in hits]
            expected_id = query["relevant_ids"][0]
            expected_hit = next(
                (hit for hit in hits if memory_ids[hit.record.id] == expected_id), None,
            )
            if expected_hit is not None:
                target_factors[query["id"]] = expected_hit.explanation

        result = recall_at_k(dataset, ranked_ids)
        dense_target_count = sum(float(factors["dense"]) > 0 for factors in target_factors.values())
        lexical_target_count = sum(float(factors["keyword"]) > 0 for factors in target_factors.values())
        evidence = {
            "dataset": dataset["dataset_id"],
            "dataset_sha256": dataset_sha256(),
            "ranking_version": RANKING_VERSION,
            "collection": collection_name,
            "collection_created_for_run": collection_created,
            "query_count": len(dataset["queries"]),
            "k": result["k"],
            "hits": result["hits"],
            "relevant": result["relevant"],
            "recall": result["recall"],
            "minimum_recall": dataset["minimum_recall"],
            "dense_target_hits": dense_target_count,
            "lexical_target_hits": lexical_target_count,
            "target_ranks": result["ranks"],
        }
        assert result["recall"] >= dataset["minimum_recall"], json.dumps(evidence, sort_keys=True)
    finally:
        if service is not None:
            await service.aclose()
        if vectors.created_collection:
            try:
                await vectors.drop_created_collection()
            except Exception:  # noqa: BLE001 — drop may commit remotely before its connection fails.
                await vectors.close()
                vectors = MilvusVectorStore(vector_settings)
            remaining = await vectors.connect()
            if collection_name in remaining:
                await vectors._call("drop_collection", collection_name=collection_name)
                remaining = await vectors.connect()
            assert collection_name not in remaining, "temporary Milvus collection cleanup failed"
        if evidence is not None:
            evidence["cleanup"] = "confirmed"
            print(json.dumps(evidence, sort_keys=True))
        await vectors.close()
