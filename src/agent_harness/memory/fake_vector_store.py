"""按字面匹配的测试 adapter，不声称提供语义 embedding。"""

from agent_harness.identity import IdentityContext
from agent_harness.memory.types import MemoryScope, scope_to_namespace


class FakeVectorStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[tuple[str, ...], str], tuple[str, dict]] = {}

    async def upsert(self, memory_id: str, content: str, metadata: dict, identity: IdentityContext) -> None:
        namespace = scope_to_namespace(MemoryScope(metadata["scope"]), identity)
        self._rows[namespace, memory_id] = (content, dict(metadata))

    async def search(self, query: str, identity: IdentityContext, scope: MemoryScope, limit: int) -> list[tuple[str, float]]:
        namespace = scope_to_namespace(scope, identity)
        if not query:
            return []
        return [(key, 1.0) for (ns, key), (content, _) in self._rows.items()
                if ns == namespace and query.casefold() in content.casefold()][:max(0, limit)]
