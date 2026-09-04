"""LangMem Formation/Consolidation 与工具读写，存储权威留在项目内。"""

import asyncio
import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agent_harness.identity import get_identity_context
from agent_harness.memory.record_store import MemoryRecordStore
from agent_harness.memory.types import MemoryEntry, MemoryScope, scope_to_namespace
from agent_harness.memory.vector_store import VectorIndexStore


class MemoryPayload(BaseModel):
    content: str
    metadata: dict = Field(default_factory=dict)


class LangMemMemoryCapability:
    def __init__(self, records: MemoryRecordStore, vectors: VectorIndexStore, model: Any = None) -> None:
        from langmem import (
            create_manage_memory_tool,
            create_memory_store_manager,
            create_search_memory_tool,
        )

        from agent_harness.memory.base_store_adapter import SqliteMilvusBaseStore

        self._store = SqliteMilvusBaseStore(records, vectors)
        self._manage = create_manage_memory_tool
        self._search = create_search_memory_tool
        self._manager = create_memory_store_manager
        self._model = model

    async def store(self, scope: MemoryScope, content: str, metadata: dict) -> str:
        namespace = scope_to_namespace(scope, get_identity_context())
        if self._model is not None:
            manager = self._manager(self._model, schemas=[MemoryPayload], namespace=namespace,
                                    store=self._store, enable_deletes=False)
            async with asyncio.timeout(15):
                puts = await manager.ainvoke({"messages": [{"role": "user", "content": json.dumps({
                    "content": content, "metadata": metadata}, ensure_ascii=False)}], "max_steps": 1})
            if puts:
                return puts[0]["key"]
            # 没有变化时复用既有记忆；没有匹配时精确保留抽取候选。
            previous = await self.search(scope, content, 1)
            if previous and previous[0].content == content and previous[0].metadata == metadata:
                return previous[0].id
        tool = self._manage(namespace=namespace, schema=MemoryPayload, actions_permitted=("create",), store=self._store)
        result = await tool.ainvoke({"content": {"content": content, "metadata": metadata}})
        # SDK 明确返回 "created memory <uuid>"；验证形状，不把任意文本当记录 ID。
        return str(UUID(result.removeprefix("created memory ")))

    async def search(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]:
        namespace = scope_to_namespace(scope, get_identity_context())
        if not query or limit <= 0:
            return []
        tool = self._search(namespace=namespace, store=self._store)
        serialized = await tool.ainvoke({"query": query, "limit": limit})
        result = []
        for row in json.loads(serialized):
            entry = await self._store.records.get(row["key"], get_identity_context())
            result.append(entry.model_copy(update={"score": row.get("score"),
                                                   "metadata": {k: v for k, v in entry.metadata.items() if k != "_langmem_value"}}))
        return result

    async def recall(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]:
        return await self.search(scope, query, limit)
