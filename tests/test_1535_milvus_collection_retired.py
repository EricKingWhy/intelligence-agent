"""#1535：退役无运行消费者的 MILVUS_COLLECTION 配置承诺。

活的检索目标只有一个来源：`knowledge_collection`（`KNOWLEDGE_COLLECTION`）。
本文件同时钉住反控——部署 `.env` / 进程环境里残留旧键，也不得把检索目标改写到
别处，且必须在运行时**告警**（AC2「按明确兼容策略报废弃」），但绝不拒绝启动。
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.embeddings import Embeddings

from agent_harness.config import Settings
from agent_harness.identity import IdentityContext
from agent_harness.knowledge.milvus_store import MilvusKnowledgeVectorStore

ALICE = IdentityContext("acme", "alice", ["user"])
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_CONFIG_LOGGER = "agent_harness.config"


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


def _retired_warnings(caplog) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records
        if record.name == _CONFIG_LOGGER and "MILVUS_COLLECTION" in record.getMessage()
    ]


def test_settings_no_longer_exposes_milvus_collection():
    assert "milvus_collection" not in Settings.model_fields
    assert not hasattr(_settings(), "milvus_collection")


def test_stale_env_var_warns_and_keeps_live_collection(monkeypatch, caplog):
    """进程环境残留路：告警可见、检索目标不变、构造不抛（extra="ignore" 不变）。"""
    monkeypatch.setenv("MILVUS_COLLECTION", "hijacked_collection")

    with caplog.at_level(logging.WARNING, logger=_CONFIG_LOGGER):
        settings = _settings()

    assert settings.knowledge_collection == "knowledge_live"
    assert "milvus_collection" not in settings.model_dump()
    assert _retired_warnings(caplog), "残留的退役键必须在运行时告警（AC2）"


def test_stale_env_file_key_warns_and_keeps_live_collection(monkeypatch, caplog, tmp_path):
    """部署 `.env` 文件残留路（env_file 值不进 os.environ，单独覆盖）。"""
    monkeypatch.delenv("MILVUS_COLLECTION", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MILVUS_COLLECTION=hijacked_collection\n"
        "KNOWLEDGE_COLLECTION=knowledge_live\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger=_CONFIG_LOGGER):
        settings = Settings(
            _env_file=str(env_file),
            milvus_uri="https://example.test",
            milvus_token="test-only",
        )

    assert settings.knowledge_collection == "knowledge_live"
    assert "milvus_collection" not in settings.model_dump()
    assert _retired_warnings(caplog), "`.env` 里残留的退役键同样必须告警（AC2）"


def test_clean_settings_do_not_warn(monkeypatch, caplog):
    """无残留时静默：不给每一次 Settings 构造刷屏。"""
    monkeypatch.delenv("MILVUS_COLLECTION", raising=False)
    caplog.clear()

    _settings()

    assert _retired_warnings(caplog) == []


def test_current_example_no_longer_promises_the_retired_key():
    text = _ENV_EXAMPLE.read_text(encoding="utf-8").lower()
    assert "milvus_collection" not in text


def test_restarted_process_keeps_knowledge_collection_despite_stale_key():
    """新进程（= 重启语义）里旧键残留，检索目标仍是 KNOWLEDGE_COLLECTION。"""
    script = """
import asyncio
from langchain_core.embeddings import Embeddings
from agent_harness.config import Settings
from agent_harness.identity import IdentityContext
from agent_harness.knowledge.milvus_store import MilvusKnowledgeVectorStore


class KnownEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[0.0, 1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


class StubClient:
    async def search(self, timeout, **kwargs):
        print("COLLECTION=" + kwargs["collection_name"])
        return [[]]


settings = Settings(_env_file=None)
print("SETTING=" + settings.knowledge_collection)
store = MilvusKnowledgeVectorStore(settings, KnownEmbeddings())
store._client = StubClient()
asyncio.run(store.search("q", IdentityContext("acme", "alice", ["user"]), limit=5))
"""
    env = {
        **os.environ,
        # 新进程读的是**本树**的源码，不是已安装到别处的副本。
        "PYTHONPATH": str(_REPO_ROOT / "src"),
        "MILVUS_COLLECTION": "hijacked_collection",
        "KNOWLEDGE_COLLECTION": "knowledge_live",
        "MILVUS_URI": "https://example.test",
        "MILVUS_TOKEN": "test-only",
    }

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "SETTING=knowledge_live" in completed.stdout
    assert "COLLECTION=knowledge_live" in completed.stdout
    assert "MILVUS_COLLECTION" in completed.stderr


@pytest.mark.asyncio
async def test_search_targets_knowledge_collection_despite_stale_key(monkeypatch):
    """本用例不需要真实 pymilvus（client 已被注入），因此不加 importorskip。

    如实记：本用例在 base 上也绿（base 的 store 本来就只读 `knowledge_collection`），
    属于**未来守卫**，不是本次修复的红绿证明。
    """
    monkeypatch.setenv("MILVUS_COLLECTION", "hijacked_collection")
    store = MilvusKnowledgeVectorStore(_settings(), KnownEmbeddings())
    client = Mock()
    client.search = AsyncMock(return_value=[[]])
    store._client = client

    await store.search("query", ALICE, limit=5)

    assert client.search.call_args.kwargs["collection_name"] == "knowledge_live"
