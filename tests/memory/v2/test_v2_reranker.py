import json

import httpx
import pytest

from agent_harness.config import Settings
from agent_harness.memory.v2.reranker import (
    MemoryV2RerankError,
    SiliconFlowMemoryV2Reranker,
    create_memory_v2_reranker,
)


@pytest.mark.asyncio
async def test_siliconflow_reranker_sends_only_query_and_documents() -> None:
    seen: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.2},
        ]})

    reranker = SiliconFlowMemoryV2Reranker(
        model="BAAI/bge-reranker-v2-m3",
        url="https://rerank.example/v1/rerank",
        api_key="test-only",
        transport=httpx.MockTransport(respond),
    )

    ranked = await reranker.rerank("project migration", ["memory one", "memory two"])

    assert ranked == [(1, 0.9), (0, 0.2)]
    assert seen["authorization"] == "Bearer test-only"
    assert seen["body"] == {
        "model": "BAAI/bge-reranker-v2-m3",
        "query": "project migration",
        "documents": ["memory one", "memory two"],
        "top_n": 2,
        "return_documents": False,
    }


@pytest.mark.asyncio
async def test_siliconflow_reranker_rejects_invalid_results_without_echoing_content() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.8},
        ]})

    reranker = SiliconFlowMemoryV2Reranker(
        model="model", url="https://rerank.example/v1/rerank", api_key="test-only",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(MemoryV2RerankError) as raised:
        await reranker.rerank("private query", ["private memory one", "private memory two"])

    assert "private" not in str(raised.value)


def test_reranker_is_opt_in_and_api_key_is_secret() -> None:
    assert create_memory_v2_reranker(Settings(_env_file=None)) is None

    settings = Settings(
        _env_file=None,
        rerank_model="BAAI/bge-reranker-v2-m3",
        rerank_url="https://rerank.example/v1/rerank",
        rerank_api_key="test-only",
    )
    reranker = create_memory_v2_reranker(settings)

    assert isinstance(reranker, SiliconFlowMemoryV2Reranker)
    assert "test-only" not in repr(settings.rerank_api_key)


def test_incomplete_reranker_configuration_degrades_to_disabled(caplog) -> None:
    settings = Settings(_env_file=None, rerank_model="BAAI/bge-reranker-v2-m3")

    assert create_memory_v2_reranker(settings) is None
    assert "configuration is incomplete" in caplog.text


@pytest.mark.asyncio
async def test_reranker_http_error_does_not_expose_provider_body() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="credential private response body")

    reranker = SiliconFlowMemoryV2Reranker(
        model="model", url="https://rerank.example/v1/rerank", api_key="test-only",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(MemoryV2RerankError) as raised:
        await reranker.rerank("private query", ["private memory"])

    assert "private" not in str(raised.value)
    assert "credential" not in str(raised.value)
