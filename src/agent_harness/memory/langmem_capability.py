"""LangMem Formation/Consolidation 与工具读写，存储权威留在项目内。"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agent_harness.identity import get_identity_context
from agent_harness.memory.record_store import MemoryRecordStore
from agent_harness.memory.types import (
    MemoryEntry,
    MemoryNamespace,
    MemoryScope,
    public_metadata,
)
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

        self._records = records
        self._store = SqliteMilvusBaseStore(records, vectors)
        self._manage = create_manage_memory_tool
        self._search = create_search_memory_tool
        self._manager = create_memory_store_manager
        self._model = model

    async def store(self, scope: MemoryScope, content: str, metadata: dict) -> str:
        namespace = MemoryNamespace.of(scope, get_identity_context()).as_tuple()
        if self._model is not None:
            # #157：解禁上游本来就有的删除能力——models 判"这条过时了"时会发 RemoveDoc，
            # manager 转成 `store.adelete(ns, key)`，落进 adapter 的 `PutOp(value=None)` 分支。
            # 只作用于本 namespace：manager 只能删它自己检索回来的 id，adapter 再校验一次归属。
            manager = self._manager(self._model, schemas=[MemoryPayload], namespace=namespace,
                                    store=self._store, enable_deletes=True)
            async with asyncio.timeout(15):
                puts = await manager.ainvoke({"messages": [{"role": "user", "content": json.dumps({
                    "content": content, "metadata": metadata}, ensure_ascii=False)}], "max_steps": 1})
            if puts:
                return puts[0]["key"]
            # 没有变化时复用既有记忆；没有匹配时精确保留抽取候选。
            previous = await self.search(scope, content, 1)
            if previous and previous[0].content == content and previous[0].metadata == metadata:
                return previous[0].id
        # 与上游默认一致（#157）：本处调用只传 content、action 默认 create，所以放开 update/
        # delete 不会让这条直调变成破坏性动作；放开是为了不再对外声称一个被我们收窄的能力。
        tool = self._manage(namespace=namespace, schema=MemoryPayload,
                            actions_permitted=("create", "update", "delete"), store=self._store)
        result = await tool.ainvoke({"content": {"content": content, "metadata": metadata}})
        # SDK 返回形如 "created memory <uuid>"。校验后缀确为 UUID 形状；
        # 形状不符时降级为按 namespace 查最近一条同内容记录（不把任意文本当记录 ID）。
        suffix = result.removeprefix("created memory ").strip()
        try:
            return str(UUID(suffix))
        except (ValueError, TypeError):
            recent = await self.search(scope, content, 1)
            if recent and recent[0].content == content:
                return recent[0].id
            raise RuntimeError(f"memory tool returned unexpected shape: {result!r}") from None

    async def update(self, memory_id: str, scope: MemoryScope, content: str, metadata: dict) -> str:
        """按 id 覆盖写（#156 的**机制**）。

        写权威记录本身，索引由 outbox/relay 异步跟进。**刻意不**把 update 交给 LangMem
        的 manager/工具：上游的 update/delete 能力已在 #157 解禁，但那是"模型自己决定改哪条"
        的路径（走 manager → adapter）；本条是**调用方指定 id** 的确定性覆盖写，
        "记录主权在项目内"意味着它不需要 SDK 参与，也不该受模型决策影响。
        """
        entry = MemoryEntry(id=memory_id, content=content, metadata=metadata, scope=scope,
                            created_at=datetime.now(UTC).isoformat())
        return await self._records.store(entry, get_identity_context())

    async def forget(self, memory_id: str) -> bool:
        """硬删（记录行 + 异步传播到向量索引），契约见 `capability.py`。"""
        return await self._records.delete(memory_id, get_identity_context())

    async def list_entries(self, scope: MemoryScope, limit: int, offset: int = 0) -> list[MemoryEntry]:
        """按 namespace 分页列出（契约见 `capability.py`）。

        刻意**不走 embedding 检索**：用户管理界面要的是"我的记忆全都有哪些"，不是
        "哪几条最像某个 query"。读权威记录，先取 `limit + offset` 再切片（与 adapter 的
        SearchOp 同款做法）。
        """
        limit, offset = max(0, limit), max(0, offset)
        entries = await self._records.list_by_scope(scope, get_identity_context(), limit + offset)
        return entries[offset:offset + limit]

    async def search(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]:
        namespace = MemoryNamespace.of(scope, get_identity_context()).as_tuple()
        if not query or limit <= 0:
            return []
        tool = self._search(namespace=namespace, store=self._store)
        serialized = await tool.ainvoke({"query": query, "limit": limit})
        result = []
        for row in json.loads(serialized):
            try:
                entry = await self._store.records.get(row["key"], get_identity_context())
            except KeyError:
                # 索引里还挂着、记录行已被删掉：解禁 provider 删除后这是**可达**状态
                # （删除先落记录行，向量由 relay 异步收敛）。跳过这一条而不是让整次检索炸掉
                # ——adapter 的 SearchOp 分支一直是这么容忍的，能力层不该比它更脆。
                continue
            result.append(entry.model_copy(update={
                "score": row.get("score"), "metadata": public_metadata(entry.metadata)}))
        return result

    async def recall(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]:
        return await self.search(scope, query, limit)
