"""KnowledgeService：ingest / retrieve / read_source 的领域服务（ADR-0013）。

store 与 registry 都是注入的协议实现；本类只承载领域语义——source 级增量
（hash 未变跳过、变更原子重建、registry hash 提交点最后写 = 崩溃自愈）、
sufficient 阈值判定、citation 编解码、规模防呆。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from agent_harness.identity import IdentityContext
from agent_harness.knowledge.registry import SqliteKnowledgeSourceRegistry
from agent_harness.knowledge.splitter import split_text
from agent_harness.knowledge.store import KnowledgeVectorStore
from agent_harness.knowledge.types import (
    CITATION_PREFIX,
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

_CITATION_RE = re.compile(r"^kb:(?P<name>.+)#(?P<index>\d+)$")
_MAX_K = 20


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class KnowledgeService:
    """store / registry 为注入的协议实现；领域语义在本类一处可读。"""

    def __init__(
        self,
        *,
        store: KnowledgeVectorStore,
        registry: SqliteKnowledgeSourceRegistry,
        min_score: float = 0.6,
        chunk_size: int = 800,
        overlap: int = 100,
        max_source_chars: int = MAX_SOURCE_CHARS,
        max_chunks: int = MAX_CHUNKS_PER_SOURCE,
    ) -> None:
        if not 0 <= min_score <= 1:
            raise KnowledgeError(f"min_score 必须在 [0, 1]：{min_score}")
        self._store = store
        self._registry = registry
        self._min_score = min_score
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._max_source_chars = max_source_chars
        self._max_chunks = max_chunks

    async def ingest(
        self, *, text: str, source_name: str, identity: IdentityContext,
    ) -> KnowledgeIngestResult:
        """摄入/更新一个 source；返回 created / rebuilt / skipped。

        增量语义（决策 7）：hash 未变整篇跳过；变更 = 删旧 chunk → 插新 chunk
        → registry hash 提交（最后一步，崩溃自愈：hash 未提交则下次重做）。
        """
        name = self._validate_source_name(source_name)
        if not text or not text.strip():
            raise KnowledgeError("ingest 的 text 不能为空")
        if len(text) > self._max_source_chars:
            raise KnowledgeError(
                f"文本超出单 source 上限：{len(text)} 字符（上限 {self._max_source_chars}，"
                f"≈2 MB）——请拆分后分 source 摄入"
            )

        content_hash = sha256(text.encode("utf-8")).hexdigest()
        existing = await self._registry.get_by_name(identity.tenant_id, name)
        if existing is not None and existing.content_hash == content_hash:
            return KnowledgeIngestResult(
                source_id=existing.source_id, source_name=name,
                chunk_count=existing.chunk_count, status="skipped",
            )

        pieces = split_text(text, chunk_size=self._chunk_size, overlap=self._overlap)
        if len(pieces) > self._max_chunks:
            raise KnowledgeError(
                f"切分出 {len(pieces)} 个 chunk，超出单 source 上限 {self._max_chunks}"
                f"——请拆分后分 source 摄入"
            )

        source_id = existing.source_id if existing is not None else str(uuid4())
        chunks = [
            KnowledgeChunk(
                source_id=source_id, source_name=name, chunk_index=index,
                content_hash=sha256(piece.encode("utf-8")).hexdigest(),
                content=piece,
            )
            for index, piece in enumerate(pieces)
        ]
        # 重建顺序：删旧 → 插新 → registry hash 提交（最后写，崩溃自愈）。
        if existing is not None:
            await self._store.delete_source(source_id, identity)
        await self._store.upsert_chunks(chunks, identity)
        now = _now()
        await self._registry.upsert(KnowledgeSource(
            source_id=source_id, tenant_id=identity.tenant_id, name=name,
            content_hash=content_hash, chunk_count=len(chunks),
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        ))
        return KnowledgeIngestResult(
            source_id=source_id, source_name=name, chunk_count=len(chunks),
            status="rebuilt" if existing is not None else "created",
        )

    async def retrieve(
        self,
        *,
        query: str,
        identity: IdentityContext,
        k: int = 5,
        source_id: str | None = None,
    ) -> KnowledgeSearchResult:
        """检索语料；hits 如实透传，is_sufficient 是诚实信号不是结果开关。"""
        if not query or not query.strip():
            raise KnowledgeError("query 不能为空")
        if not 1 <= k <= _MAX_K:
            raise KnowledgeError(f"k 必须在 [1, {_MAX_K}]：{k}")
        pairs = await self._store.search(
            query, identity, limit=k, source_id=source_id,
        )
        hits = [
            KnowledgeHit(
                citation=self._citation(chunk.source_name, chunk.chunk_index),
                content=chunk.content, score=score,
                source_id=chunk.source_id, chunk_index=chunk.chunk_index,
            )
            for chunk, score in pairs
        ]
        sufficient = bool(hits) and hits[0].score >= self._min_score
        return KnowledgeSearchResult(query=query, hits=hits, is_sufficient=sufficient)

    async def read_source(
        self,
        *,
        citation: str,
        identity: IdentityContext,
        with_context: bool = False,
    ) -> KnowledgeReadResult:
        """citation → 原文 chunk（Gate 3 可追溯链路）；with_context 附带前后邻块。"""
        match = _CITATION_RE.match(citation.strip())
        if match is None:
            raise KnowledgeError(
                f"无法解析 citation（期望 kb:<source_name>#<chunk_index>）：{citation!r}"
            )
        name, index_text = match.group("name"), int(match.group("index"))
        source = await self._registry.get_by_name(identity.tenant_id, name)
        if source is None:
            raise KnowledgeError(f"citation 指向未知 source：{citation!r}")
        chunk = await self._store.get_chunk(source.source_id, index_text, identity)
        if chunk is None:
            raise KnowledgeError(
                f"citation 指向不存在的 chunk：{citation!r}"
                f"（source 共 {source.chunk_count} 块）"
            )
        context: list[KnowledgeChunk] = []
        if with_context:
            for neighbor in (index_text - 1, index_text + 1):
                if 0 <= neighbor < source.chunk_count:
                    neighbor_chunk = await self._store.get_chunk(
                        source.source_id, neighbor, identity,
                    )
                    if neighbor_chunk is not None:
                        context.append(neighbor_chunk)
        return KnowledgeReadResult(source_name=name, match=chunk, context=context)

    @staticmethod
    def _validate_source_name(source_name: str) -> str:
        name = source_name.strip()
        if not name:
            raise KnowledgeError("source_name 不能为空")
        if "#" in name:
            raise KnowledgeError(
                f"source_name 不得包含 '#'（citation 以它分隔 chunk 索引）：{source_name!r}"
            )
        return name

    @staticmethod
    def _citation(source_name: str, chunk_index: int) -> str:
        return f"{CITATION_PREFIX}{source_name}#{chunk_index}"
