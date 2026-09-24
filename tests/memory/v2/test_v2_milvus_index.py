"""V2's adapter reuses the initialized V1 Milvus client without changing its schema."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_harness.memory.v2.milvus_index import MilvusMemoryV2Index
from agent_harness.memory.v2.types import MemoryScope, TrustedMemoryIdentity
from tests.memory.v2._records import make_record


class _VectorClient:
    def __init__(self):
        self._settings = SimpleNamespace(milvus_collection="memory")
        self.calls = []

    async def _embed(self, text: str, *, document: bool = False) -> list[float]:
        return [0.25, 0.75]

    async def _call(self, operation: str, **kwargs):
        self.calls.append((operation, kwargs))
        if operation == "search":
            return [[{"entity": {"memory_id": "memory-1"}, "distance": 0.875}]]
        return None


@pytest.mark.asyncio
async def test_milvus_adapter_routes_project_records_and_deletes_by_trusted_fields():
    vectors = _VectorClient()
    index = MilvusMemoryV2Index(vectors)
    record = make_record(
        tenant_id="tenant-a", user_id="user-a", memory_id="memory-1",
        scope=MemoryScope.PROJECT, project_id="project-a", content="a durable preference",
    )
    trusted = TrustedMemoryIdentity("tenant-a", "user-a", "project-a")

    await index.upsert(record)
    operation, upsert = vectors.calls[-1]
    row = upsert["data"][0]
    assert operation == "upsert" and upsert["collection_name"] == "memory"
    assert row["id"] != record.id and len(row["id"]) == 64
    assert row["memory_id"] == record.id and row["session_id"] == "project-a"
    assert row["content"] == record.content

    hits = await index.search("preference", trusted, MemoryScope.PROJECT, 5)
    operation, search = vectors.calls[-1]
    assert operation == "search" and hits == [(record.id, 0.875)]
    assert search["filter_params"] == {
        "tenant": "tenant-a", "user": "user-a", "scope": "project",
        "route": "project-a",
    }
    assert "tenant_id == {tenant}" in search["filter"]

    await index.delete(
        record.id, tenant_id="tenant-a", user_id="user-a",
        scope=MemoryScope.PROJECT, project_id="project-a",
    )
    operation, deletion = vectors.calls[-1]
    assert operation == "delete"
    assert deletion["filter_params"] == {
        "tenant": "tenant-a", "user": "user-a", "scope": "project",
        "route": "project-a", "memory": record.id,
    }
