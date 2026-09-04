"""零 SDK 的测试 MemoryCapability。"""

from datetime import UTC, datetime
from uuid import uuid4

from agent_harness.identity import get_identity_context
from agent_harness.memory.fake_record_store import FakeMemoryRecordStore
from agent_harness.memory.types import MemoryEntry, MemoryScope


class FakeMemoryCapability:
    def __init__(self) -> None:
        self._records = FakeMemoryRecordStore()

    async def store(self, scope: MemoryScope, content: str, metadata: dict) -> str:
        entry = MemoryEntry(id=str(uuid4()), content=content, metadata=metadata, scope=scope,
                            created_at=datetime.now(UTC).isoformat())
        return await self._records.store(entry, get_identity_context())

    async def search(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]:
        entries = await self._records.list_by_scope(scope, get_identity_context(), 10000)
        return [entry.model_copy(update={"score": 1.0}) for entry in entries
                if query and query.casefold() in entry.content.casefold()][:max(0, limit)]

    async def recall(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]:
        return await self.search(scope, query, limit)
