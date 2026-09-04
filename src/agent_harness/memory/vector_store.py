"""向量索引只返回权威记录 ID 与查询分数。"""

from typing import Protocol

from agent_harness.identity import IdentityContext
from agent_harness.memory.types import MemoryScope


class VectorIndexStore(Protocol):
    async def upsert(self, memory_id: str, content: str, metadata: dict, identity: IdentityContext) -> None: ...
    async def search(self, query: str, identity: IdentityContext, scope: MemoryScope, limit: int) -> list[tuple[str, float]]: ...


class VectorStoreError(RuntimeError):
    """不携带 SDK 原始异常文本或凭证的分类错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Memory vector store: {code}")
