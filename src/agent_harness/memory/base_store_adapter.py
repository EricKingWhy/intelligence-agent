"""LangGraph BaseStore adapter；仅由可选 LangMem Provider 导入。"""

import asyncio
from datetime import UTC, datetime

from langgraph.store.base import BaseStore, GetOp, Item, PutOp, SearchItem, SearchOp

from agent_harness.identity import get_identity_context
from agent_harness.memory.record_store import MemoryRecordStore
from agent_harness.memory.types import MemoryEntry, MemoryScope, scope_to_namespace
from agent_harness.memory.vector_store import VectorIndexStore


class SqliteMilvusBaseStore(BaseStore):
    def __init__(self, records: MemoryRecordStore, vectors: VectorIndexStore) -> None:
        self.records = records
        self.vectors = vectors

    @staticmethod
    def _scope(namespace: tuple[str, ...]) -> MemoryScope:
        if len(namespace) not in (4, 5):
            raise PermissionError("Memory namespace is not authorized")
        scope = MemoryScope(namespace[3])
        if tuple(namespace) != scope_to_namespace(scope, get_identity_context()):
            raise PermissionError("Memory namespace is not authorized")
        return scope

    def batch(self, ops):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.abatch(ops))
        raise RuntimeError("Use async BaseStore methods inside an event loop")

    async def abatch(self, ops):
        results = []
        for op in ops:
            if not isinstance(op, (GetOp, PutOp, SearchOp)):
                raise NotImplementedError("Namespace enumeration is not exposed")
            namespace = op.namespace_prefix if isinstance(op, SearchOp) else op.namespace
            scope = self._scope(namespace)
            identity = get_identity_context()
            if isinstance(op, PutOp):
                if op.value is None or op.ttl is not None:
                    raise NotImplementedError("Memory delete/TTL is not enabled")
                payload = op.value.get("content")
                if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
                    raise TypeError("Expected structured Memory content")
                metadata = {**payload.get("metadata", {}), "_langmem_value": {
                    "kind": op.value.get("kind", "MemoryPayload"), "content": payload,
                }}
                await self.records.store(MemoryEntry(id=op.key, content=payload["content"], metadata=metadata,
                                                      scope=scope, created_at=datetime.now(UTC).isoformat()), identity)
                results.append(None)
            elif isinstance(op, GetOp):
                try:
                    entry = await self.records.get(op.key, identity)
                except KeyError:
                    results.append(None)
                    continue
                if entry.scope != scope:
                    results.append(None)
                    continue
                results.append(self._item(entry, namespace))
            else:
                if op.limit <= 0 or op.offset < 0:
                    results.append([])
                    continue
                if op.query:
                    hits = await self.vectors.search(op.query, identity, scope, op.limit + op.offset)
                    entries = []
                    for key, score in hits:
                        try:
                            entry = await self.records.get(key, identity)
                        except KeyError:
                            continue
                        if entry.scope == scope:
                            entries.append(entry.model_copy(update={"score": score}))
                else:
                    entries = await self.records.list_by_scope(scope, identity, op.limit + op.offset)
                items = [self._item(entry, namespace, search=True) for entry in entries]
                if op.filter:
                    items = [item for item in items if all(item.value.get(k) == v for k, v in op.filter.items())]
                results.append(items[op.offset:op.offset + op.limit])
        return results

    @staticmethod
    def _item(entry: MemoryEntry, namespace, search=False):
        value = entry.metadata.get("_langmem_value", {"kind": "MemoryPayload", "content": {
            "content": entry.content, "metadata": entry.metadata,
        }})
        timestamp = datetime.fromisoformat(entry.created_at)
        kwargs = {"value": value, "key": entry.id, "namespace": namespace,
                  "created_at": timestamp, "updated_at": timestamp}
        return SearchItem(**kwargs, score=entry.score) if search else Item(**kwargs)
