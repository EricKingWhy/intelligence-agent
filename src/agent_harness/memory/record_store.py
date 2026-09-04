"""Memory 权威记录存取契约。"""

from typing import Protocol

from agent_harness.identity import IdentityContext
from agent_harness.memory.types import MemoryEntry, MemoryScope


class MemoryRecordStore(Protocol):
    async def store(self, entry: MemoryEntry, identity: IdentityContext) -> str: ...
    async def get(self, memory_id: str, identity: IdentityContext) -> MemoryEntry: ...
    async def list_by_scope(
        self, scope: MemoryScope, identity: IdentityContext, limit: int,
    ) -> list[MemoryEntry]: ...
