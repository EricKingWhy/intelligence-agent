"""#1535：退役无运行消费者的 MILVUS_COLLECTION 配置承诺。

活的检索目标只有一个来源：`knowledge_collection`（`KNOWLEDGE_COLLECTION`）。
本文件同时钉住反控——部署 `.env` 里残留旧键，也不得把检索目标改写到别处。
"""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.embeddings import Embeddings

from agent_harness.config import Settings
from agent_harness.identity import IdentityContext
from agent_harness.knowledge.milvus_store import MilvusKnowledgeVectorStore

ALICE = IdentityContext("acme", "alice", ["user"])
_ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


class KnownEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[0.0, 1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


def _settings(**overrides) -> Settings:
    values = {
        "milvus_uri": "https://example.test",
        "milvus_token": "test-only",
        "knowledge_collection": "knowledge_live",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_settings_no_longer_exposes_milvus_collection():
    assert "milvus_collection" not in Settings.model_fields
    assert not hasattr(_settings(), "milvus_collection")


def test_stale_env_key_is_ignored_and_keeps_live_collection(monkeypatch):
    monkeypatch.setenv("MILVUS_COLLECTION", "hijacked_collection")

    settings = _settings()

    assert settings.knowledge_collection == "knowledge_live"
    assert "milvus_collection" not in settings.model_dump()
    assert "hijacked_collection" not in settings.model_dump().values()


def test_current_example_no_longer_promises_the_retired_key():
    assert "MILVUS_COLLECTION" not in _ENV_EXAMPLE.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_search_targets_knowledge_collection_despite_stale_key(monkeypatch):
    pytest.importorskip("pymilvus")
    monkeypatch.setenv("MILVUS_COLLECTION", "hijacked_collection")
    store = MilvusKnowledgeVectorStore(_settings(), KnownEmbeddings())
    client = Mock()
    client.search = AsyncMock(return_value=[[]])
    store._client = client

    await store.search("query", ALICE, limit=5)

    assert client.search.call_args.kwargs["collection_name"] == "knowledge_live"
