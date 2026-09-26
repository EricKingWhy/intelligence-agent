"""Optional SiliconFlow cross-encoder adapter for authorized Memory V2 candidates."""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import Protocol

import httpx

from agent_harness.config import Settings

logger = logging.getLogger(__name__)

MAX_RERANK_CANDIDATES = 20
RERANKING_VERSION = "hybrid-rerank-v1"


class MemoryV2Reranker(Protocol):
    async def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]: ...


class MemoryV2RerankError(RuntimeError):
    """Provider or response failure without request or response content."""


class SiliconFlowMemoryV2Reranker:
    def __init__(
        self, *, model: str, url: str, api_key: str, timeout: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._url = url
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    async def rerank(self, query: str, documents: Sequence[str]) -> list[tuple[int, float]]:
        if not query.strip() or not documents:
            return []
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport,
            ) as client:
                response = await client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "query": query,
                        "documents": list(documents),
                        "top_n": len(documents),
                        "return_documents": False,
                    },
                )
        except httpx.TimeoutException:
            raise MemoryV2RerankError("timeout") from None
        except httpx.HTTPError:
            raise MemoryV2RerankError("request_failed") from None

        if response.status_code != 200:
            raise MemoryV2RerankError(f"http_{response.status_code}")
        try:
            results = response.json().get("results")
        except (ValueError, AttributeError):
            raise MemoryV2RerankError("invalid_response") from None
        if not isinstance(results, list) or len(results) != len(documents):
            raise MemoryV2RerankError("invalid_results")

        ranking: list[tuple[int, float]] = []
        for item in results:
            if not isinstance(item, dict):
                raise MemoryV2RerankError("invalid_result")
            index, score = item.get("index"), item.get("relevance_score")
            if (type(index) is not int or not 0 <= index < len(documents)
                    or type(score) not in (int, float) or not math.isfinite(score)):
                raise MemoryV2RerankError("invalid_result")
            ranking.append((index, float(score)))
        if {index for index, _score in ranking} != set(range(len(documents))):
            raise MemoryV2RerankError("invalid_indices")
        return sorted(ranking, key=lambda item: (-item[1], item[0]))


def create_memory_v2_reranker(settings: Settings) -> MemoryV2Reranker | None:
    model = settings.rerank_model.strip()
    url = settings.rerank_url.strip()
    api_key = settings.rerank_api_key.get_secret_value()
    if not any((model, url, api_key)):
        return None
    if not all((model, url, api_key)):
        logger.warning("Memory V2 reranker disabled: configuration is incomplete")
        return None
    return SiliconFlowMemoryV2Reranker(model=model, url=url, api_key=api_key)
