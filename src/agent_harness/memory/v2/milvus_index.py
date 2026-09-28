"""V2 Milvus adapter over the initialized V1 memory vector client.

V1 and V2 share the configured collection and its stable storage schema, but use disjoint
scope values and namespaced primary keys. SQLite remains authoritative for every hit.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_harness.memory.v2.index import MemoryV2VectorIndex
from agent_harness.memory.v2.types import (
    MemoryRecordV2,
    MemoryScope,
    TrustedMemoryIdentity,
)


class MilvusMemoryV2Index(MemoryV2VectorIndex):
    """Adapt the existing initialized embedding/Milvus client to V2 routing semantics."""

    def __init__(self, initialized_vector_store: Any) -> None:
        self._vectors = initialized_vector_store

    async def upsert(self, record: MemoryRecordV2) -> None:
        settings = self._vectors._settings
        vector = await self._vectors._embed(record.content, document=True)
        route = record.project_id if record.scope is MemoryScope.PROJECT else ""
        key = hashlib.sha256(json.dumps((
            "memory-v2", record.tenant_id, record.user_id, record.scope.value,
            route, record.id,
        ), ensure_ascii=False).encode()).hexdigest()
        await self._vectors._call(
            "upsert", collection_name=settings.milvus_collection, data=[{
                "id": key,
                "memory_id": record.id,
                "tenant_id": record.tenant_id,
                "user_id": record.user_id,
                "scope": record.scope.value,
                # The shared V1 schema has no project_id column. For V2 rows this routing
                # column carries project_id; V1 queries never select V2's disjoint scopes.
                "session_id": route,
                "content": record.content,
                "metadata": {"memory_v2": True, "version": record.version},
                "vector": vector,
            }],
        )

    async def delete(
        self, memory_id: str, *, tenant_id: str, user_id: str,
        scope: MemoryScope, project_id: str | None,
    ) -> None:
        settings = self._vectors._settings
        route = project_id if scope is MemoryScope.PROJECT else ""
        expression = (
            "tenant_id == {tenant} AND user_id == {user} AND scope == {scope} "
            "AND session_id == {route} AND memory_id == {memory}"
        )
        await self._vectors._call(
            "delete", collection_name=settings.milvus_collection,
            filter=expression,
            filter_params={
                "tenant": tenant_id, "user": user_id, "scope": scope.value,
                "route": route or "", "memory": memory_id,
            },
        )

    async def search(
        self, query: str, trusted: TrustedMemoryIdentity,
        scope: MemoryScope, limit: int,
    ) -> list[tuple[str, float]]:
        if not query or limit <= 0 or (scope is MemoryScope.PROJECT and trusted.project_id is None):
            return []
        settings = self._vectors._settings
        route = trusted.project_id if scope is MemoryScope.PROJECT else ""
        vector = await self._vectors._embed(query)
        expression = (
            "tenant_id == {tenant} AND user_id == {user} AND scope == {scope} "
            "AND session_id == {route}"
        )
        hits = await self._vectors._call(
            "search", collection_name=settings.milvus_collection, data=[vector],
            anns_field="vector", filter=expression,
            filter_params={
                "tenant": trusted.tenant_id, "user": trusted.user_id,
                "scope": scope.value, "route": route or "",
            },
            limit=limit, search_params={"metric_type": "COSINE"},
            output_fields=["memory_id"], consistency_level="Strong",
        )
        return [
            (hit["entity"]["memory_id"], float(hit["distance"]))
            for hit in (hits[0] if hits else [])
        ]
