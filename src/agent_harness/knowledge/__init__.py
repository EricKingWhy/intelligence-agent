"""Knowledge 域（Phase 11，ADR-0013）：显式摄入的文档语料检索。

与 Memory 域平行且互不依赖（CONTEXT.md Knowledge 层）：Memory 是随对话
自动抽取的个性化事实（被动注入），Knowledge 是显式摄入的文档语料（只在
模型调用检索工具时被查询，Agentic RAG）。两域各有独立的向量协议与
Collection，仅共享 Milvus 部署与 embeddings 工厂。
"""

from agent_harness.knowledge.service import KnowledgeService
from agent_harness.knowledge.store import FakeKnowledgeVectorStore, KnowledgeVectorStore
from agent_harness.knowledge.types import (
    MAX_CHUNKS_PER_SOURCE,
    MAX_SOURCE_CHARS,
    KnowledgeChunk,
    KnowledgeError,
    KnowledgeHit,
    KnowledgeIngestResult,
    KnowledgeReadResult,
    KnowledgeSearchResult,
    KnowledgeSource,
)


def build_knowledge_vector_store(settings, embeddings=None):
    """建 Knowledge 域的真实向量库 adapter。

    存在的理由：装配层（`capability/wiring.py`）不该直接点名具体后端模块——
    记忆域已无向量腿（#1353 §5），装配层里再出现后端名字会让人以为记忆还在用它。
    具体实现仍留在 `knowledge/milvus_store.py`（RAG 侧本期不动）。
    """
    from agent_harness.knowledge.milvus_store import MilvusKnowledgeVectorStore

    return MilvusKnowledgeVectorStore(settings, embeddings)


__all__ = [
    "MAX_CHUNKS_PER_SOURCE",
    "MAX_SOURCE_CHARS",
    "FakeKnowledgeVectorStore",
    "KnowledgeChunk",
    "KnowledgeError",
    "KnowledgeHit",
    "KnowledgeIngestResult",
    "KnowledgeReadResult",
    "KnowledgeSearchResult",
    "KnowledgeService",
    "KnowledgeSource",
    "KnowledgeVectorStore",
    "build_knowledge_vector_store",
]
