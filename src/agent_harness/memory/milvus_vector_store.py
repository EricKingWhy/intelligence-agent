"""可选 pymilvus adapter；身份过滤由本层强制，SQLite 仍是事实源。"""

import hashlib
import json
import logging
import math

from langchain_core.embeddings import Embeddings

from agent_harness.config import Settings
from agent_harness.identity import IdentityContext
from agent_harness.memory.types import MemoryScope, scope_to_namespace
from agent_harness.memory.vector_store import VectorStoreError


class MilvusVectorStore:
    def __init__(self, settings: Settings, embeddings: Embeddings | None = None) -> None:
        if not settings.milvus_uri or not settings.milvus_token.get_secret_value():
            raise VectorStoreError("configuration")
        self._settings = settings
        self._embeddings = embeddings
        self._client = None
        self.dimension: int | None = None
        self.created_collection = False

    async def _call(self, operation: str, **kwargs):
        if self._client is None:
            raise VectorStoreError("not_connected")
        try:
            return await getattr(self._client, operation)(timeout=15, **kwargs)
        except Exception as error:  # noqa: BLE001 — SDK异常不得包含凭证进入日志/事件。
            code = getattr(error, "code", None)
            category = {1800: "authentication", 100: "collection_not_found",
                        1100: "invalid_request"}.get(code, "unavailable")
            raise VectorStoreError(category) from None

    async def connect(self) -> list[str]:
        """轻量连接检查，不创建 Collection、不调用 embedding。"""
        if self._client is None:
            try:
                from pymilvus import AsyncMilvusClient
            except ImportError:
                raise VectorStoreError("install_intelligence_agent_memory_extra") from None
            # SDK 的异常日志可能包含请求参数；仅暴露本 adapter 的分类错误。
            sdk_logger = logging.getLogger("pymilvus")
            sdk_logger.handlers = [logging.NullHandler()]
            sdk_logger.propagate = False
            try:
                self._client = AsyncMilvusClient(uri=self._settings.milvus_uri,
                                                token=self._settings.milvus_token.get_secret_value(), timeout=15)
            except Exception:  # noqa: BLE001
                raise VectorStoreError("connection") from None
        return await self._call("list_collections")

    async def _embed(self, text: str, *, document: bool = False) -> list[float]:
        if self._embeddings is None:
            raise VectorStoreError("embedding_not_configured")
        try:
            vector = ((await self._embeddings.aembed_documents([text]))[0] if document
                      else await self._embeddings.aembed_query(text))
        except Exception:  # noqa: BLE001
            raise VectorStoreError("embedding_unavailable") from None
        if (len(vector) < 2 or any(not math.isfinite(v) for v in vector)
                or not any(vector) or (self.dimension is not None and len(vector) != self.dimension)):
            raise VectorStoreError("embedding_dimension_or_value")
        return vector

    async def initialize(self) -> None:
        collections = await self.connect()
        if not self._settings.milvus_collection:
            raise VectorStoreError("configuration")
        vector = await self._embed("memory index dimension probe")
        self.dimension = len(vector)
        collection = self._settings.milvus_collection
        if collection in collections:
            description = await self._call("describe_collection", collection_name=collection)
            fields = {f["name"]: f for f in description["fields"]}
            if (not {"id", "memory_id", "tenant_id", "user_id", "scope", "session_id", "content", "metadata", "vector"} <= fields.keys()
                    or int(fields["vector"].get("params", {}).get("dim", 0)) != self.dimension
                    or not fields["tenant_id"].get("is_partition_key")):
                raise VectorStoreError("schema_mismatch")
            return
        from pymilvus import DataType

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        for field in ("memory_id", "tenant_id", "user_id", "scope", "session_id"):
            schema.add_field(field, DataType.VARCHAR, max_length=2048, is_partition_key=field == "tenant_id")
        schema.add_field("content", DataType.VARCHAR, max_length=65535)
        schema.add_field("metadata", DataType.JSON)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dimension)
        index = self._client.prepare_index_params()
        index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        await self._call("create_collection", collection_name=collection, schema=schema,
                         index_params=index, consistency_level="Strong")
        self.created_collection = True

    @staticmethod
    def _filter(identity: IdentityContext, scope: MemoryScope) -> tuple[str, dict]:
        namespace = scope_to_namespace(scope, identity)
        return ("tenant_id == {tenant} AND user_id == {user} AND scope == {scope} AND session_id == {session}",
                {"tenant": identity.tenant_id, "user": identity.user_id, "scope": scope.value,
                 "session": namespace[4] if len(namespace) == 5 else ""})

    async def upsert(self, memory_id: str, content: str, metadata: dict, identity: IdentityContext) -> None:
        scope = MemoryScope(metadata["scope"])
        namespace = scope_to_namespace(scope, identity)
        vector = await self._embed(content, document=True)
        key = hashlib.sha256(json.dumps([*namespace, memory_id], ensure_ascii=False).encode()).hexdigest()
        await self._call("upsert", collection_name=self._settings.milvus_collection, data=[{
            "id": key, "memory_id": memory_id, "tenant_id": identity.tenant_id, "user_id": identity.user_id,
            "scope": scope.value, "session_id": namespace[4] if len(namespace) == 5 else "",
            "content": content, "metadata": metadata, "vector": vector,
        }])

    async def search(self, query: str, identity: IdentityContext, scope: MemoryScope, limit: int) -> list[tuple[str, float]]:
        expression, params = self._filter(identity, scope)
        if not query or limit <= 0:
            return []
        vector = await self._embed(query)
        hits = await self._call("search", collection_name=self._settings.milvus_collection, data=[vector],
                                anns_field="vector", filter=expression, filter_params=params, limit=limit,
                                search_params={"metric_type": "COSINE"}, output_fields=["memory_id"],
                                consistency_level="Strong")
        return [(hit["entity"]["memory_id"], float(hit["distance"])) for hit in hits[0]]

    async def get(self, memory_id: str, identity: IdentityContext, scope: MemoryScope) -> dict | None:
        expression, params = self._filter(identity, scope)
        rows = await self._call("query", collection_name=self._settings.milvus_collection,
                                filter=expression + " AND memory_id == {memory}",
                                filter_params={**params, "memory": memory_id},
                                output_fields=["memory_id", "content", "metadata"], consistency_level="Strong")
        return rows[0] if rows else None

    async def delete(self, memory_id: str, identity: IdentityContext, scope: MemoryScope) -> None:
        expression, params = self._filter(identity, scope)
        await self._call("delete", collection_name=self._settings.milvus_collection,
                         filter=expression + " AND memory_id == {memory}", filter_params={**params, "memory": memory_id})

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
