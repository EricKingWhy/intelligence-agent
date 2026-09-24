from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_harness.memory.v2.milvus_index import MilvusMemoryV2Index
from agent_harness.memory.v2.types import MemoryScope, TrustedMemoryIdentity
from tests.memory.v2._records import make_record


class _InitializedVectorStore:
    def __init__(self) -> None:
        self._settings = SimpleNamespace(milvus_collection="memory")
        self.calls: list[tuple[str, dict]] = []
        self.search_result = [[{"entity": {"memory_id": "memory-1"}, "distance": 0.8}]]

    async def _embed(self, _text: str, *, document: bool = False) -> list[float]:
        return [1.0, 0.0] if document else [0.0, 1.0]

    async def _call(self, operation: str, **kwargs):
        self.calls.append((operation, kwargs))
        if operation == "search":
            return self.search_result
        return None


@pytest.mark.asyncio
async def test_project_search_routes_on_the_trusted_project_and_returns_ids_only() -> None:
    vectors = _InitializedVectorStore()
    index = MilvusMemoryV2Index(vectors)

    hits = await index.search(
        "query", TrustedMemoryIdentity("tenant-a", "user-a", "project-x"),
        MemoryScope.PROJECT, 10,
    )

    assert hits == [("memory-1", 0.8)]
    operation, call = vectors.calls[0]
    assert operation == "search"
    assert call["filter_params"] == {
        "tenant": "tenant-a", "user": "user-a", "scope": "project", "route": "project-x",
    }
    assert call["output_fields"] == ["memory_id"]
    assert call["limit"] == 10


@pytest.mark.asyncio
async def test_project_search_without_trusted_project_does_not_call_milvus() -> None:
    vectors = _InitializedVectorStore()
    index = MilvusMemoryV2Index(vectors)

    assert await index.search(
        "query", TrustedMemoryIdentity("tenant-a", "user-a"), MemoryScope.PROJECT, 10,
    ) == []
    assert vectors.calls == []


@pytest.mark.asyncio
async def test_project_upsert_stores_project_route_and_namespaced_primary_key() -> None:
    vectors = _InitializedVectorStore()
    index = MilvusMemoryV2Index(vectors)
    record = make_record(
        memory_id="memory-1", scope=MemoryScope.PROJECT, project_id="project-x",
    )

    await index.upsert(record)

    operation, call = vectors.calls[0]
    row = call["data"][0]
    assert operation == "upsert"
    assert row["session_id"] == "project-x"
    assert row["scope"] == "project"
    assert row["memory_id"] == "memory-1"
    assert row["id"] != "memory-1"
