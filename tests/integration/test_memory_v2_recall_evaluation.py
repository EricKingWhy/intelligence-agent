"""Frozen Recall@6 against a unique, real Milvus collection and configured embeddings."""

from __future__ import annotations

import hashlib
import json
import subprocess
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
from scripts.gate0 import worktree_divergence
from tests.memory.v2._records import make_draft

pytestmark = [pytest.mark.integration, pytest.mark.live_services, pytest.mark.asyncio]


def _git_output(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


class _CachedEmbeddings:
    """Reuse real provider vectors so each Milvus operation avoids another model request."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[text] for text in texts]

    async def aembed_query(self, text: str) -> list[float]:
        return self._vectors[text]


async def test_live_project_cross_session_recall_at_six(tmp_path) -> None:
    divergence = worktree_divergence()
    assert not any(divergence[key] for key in ("tracked", "hidden", "risky")), (
        "live Recall@6 evidence must run without tracked, hidden-index, or risky-input divergence"
    )
    untracked_ignore_files = _git_output(
        "ls-files", "--others", "--exclude-per-directory=CON",
        "--", ":(glob)**/.gitignore",
    ).splitlines()
    ignored_runtime_roots = (".venv/", ".pytest_cache/", ".ruff_cache/")
    untrusted_ignore_files = [
        path for path in untracked_ignore_files
        if not any(path.startswith(root) for root in ignored_runtime_roots)
    ]
    assert not untrusted_ignore_files, "untracked .gitignore files must not shape evidence checks"
    code_commit = _git_output("rev-parse", "HEAD")
    code_tree = _git_output("rev-parse", "HEAD^{tree}")
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
    collection_absent_before_run = False
    evidence: dict | None = None

    try:
        existing_collections = await vectors.connect()
        assert collection_name not in existing_collections, "generated collection name must be unused"
        collection_absent_before_run = True
        await vectors.initialize()
        collection_created = vectors.created_collection
        assert collection_created, "evaluation must own a new isolated Milvus collection"

        store = SqliteMemoryV2Store(tmp_path / "memory-v2-recall.db")
        await store.initialize()
        index = MilvusMemoryV2Index(vectors)
        relay = MemoryV2IndexRelay(store, index)
        service = MemoryV2Service(store, index, relay=relay)

        sessions = JsonlSessionStore(root=tmp_path / "sessions")
        source_session = Session.start(sessions, session_id=source_session_id)
        memory_ids: dict[str, str] = {}
        for item in dataset["memories"]:
            content = item["content"]
            source_event = source_session.append(USER_MESSAGE, {"content": content})
            draft = make_draft(
                content=content,
                scope=MemoryScope.PROJECT,
                project_id=identity.project_id,
                source_session_id=source_session_id,
                source_event_ids=[source_event.event_id],
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
            "code_commit": code_commit,
            "code_tree": code_tree,
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
        try:
            if service is not None:
                await service.aclose()
        finally:
            try:
                if collection_absent_before_run:
                    try:
                        remaining = await vectors.connect()
                    except Exception:  # noqa: BLE001 — reconnect after uncertain create response.
                        await vectors.close()
                        vectors = MilvusVectorStore(vector_settings)
                        remaining = await vectors.connect()
                    if collection_name in remaining:
                        try:
                            if vectors.created_collection:
                                await vectors.drop_created_collection()
                            else:
                                # The unique name was absent before this run; create may have
                                # committed remotely even when its response was lost.
                                await vectors._call("drop_collection", collection_name=collection_name)
                        except Exception:  # noqa: BLE001 — drop may commit before its response is lost.
                            await vectors.close()
                            vectors = MilvusVectorStore(vector_settings)
                        remaining = await vectors.connect()
                        if collection_name in remaining:
                            await vectors._call("drop_collection", collection_name=collection_name)
                            remaining = await vectors.connect()
                    assert collection_name not in remaining, "temporary Milvus collection cleanup failed"
                if evidence is not None:
                    evidence["cleanup"] = "confirmed_absent"
                    print(json.dumps(evidence, sort_keys=True))
            finally:
                await vectors.close()
