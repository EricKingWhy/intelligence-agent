"""Core 依赖的 Memory 原语；身份由可信上下文提供。"""

from typing import Protocol

from agent_harness.memory.types import MemoryEntry, MemoryScope


class MemoryCapability(Protocol):
    async def store(self, scope: MemoryScope, content: str, metadata: dict) -> str: ...
    async def recall(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]: ...
    async def search(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryEntry]: ...
