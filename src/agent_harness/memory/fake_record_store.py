"""测试用权威记录；复制值以模拟持久化的对象隔离。"""

from agent_harness.identity import IdentityContext
from agent_harness.memory.types import MemoryEntry, MemoryScope, scope_to_namespace


class FakeMemoryRecordStore:
    def __init__(self) -> None:
        self._records: dict[str, tuple[tuple[str, ...], MemoryEntry]] = {}

    async def initialize(self) -> None:
        pass

    async def store(self, entry: MemoryEntry, identity: IdentityContext) -> str:
        namespace = scope_to_namespace(entry.scope, identity)
        existing = self._records.get(entry.id)
        if existing and existing[0] != namespace:
            raise PermissionError("Memory belongs to a different namespace")
        self._records[entry.id] = (namespace, entry.model_copy(deep=True, update={
            "indexed": False, "score": None,
            "created_at": existing[1].created_at if existing else entry.created_at,
        }))
        return entry.id

    async def get(self, memory_id: str, identity: IdentityContext) -> MemoryEntry:
        row = self._records.get(memory_id)
        if row is None or row[0][1:3] != (identity.tenant_id, identity.user_id):
            raise KeyError(memory_id)
        if row[0] != scope_to_namespace(row[1].scope, identity):
            raise KeyError(memory_id)
        return row[1].model_copy(deep=True)

    async def list_by_scope(
        self, scope: MemoryScope, identity: IdentityContext, limit: int,
    ) -> list[MemoryEntry]:
        namespace = scope_to_namespace(scope, identity)
        entries = [entry for ns, entry in self._records.values() if ns == namespace]
        return [entry.model_copy(deep=True) for entry in
                sorted(entries, key=lambda entry: (entry.created_at, entry.id), reverse=True)[:max(0, limit)]]
