"""Web Search 域（Phase 12，ADR-0014）。

RetrievalHit 是 KB/Web 共享的数据形状；各域返回自己的类型（WebSearchProvider
→ list[WebHit]，KB → KnowledgeHit）。Tavily 默认 provider 见 tavily.py；
Fake 见 fake.py（CI 零依赖）。
"""

from agent_harness.websearch.protocol import (
    RetrievalHit,
    WebHit,
    WebSearchError,
    WebSearchProvider,
)

__all__ = [
    "RetrievalHit",
    "WebHit",
    "WebSearchError",
    "WebSearchProvider",
]
