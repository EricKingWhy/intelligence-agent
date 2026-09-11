"""抽取降级的可观测性（BUG-012）：回退必须落 ``memory/degraded``，成功不得有噪音。

真机现场：中文模型把 JSON 分隔符写成全角引号 → 严格校验失败 → 抽取**静默**退回
正则启发式（生产 prompt 连测 5/5），事件流里零痕迹、用户侧完全不可见。本文件钉住
「回退必留痕、成功零噪音、降级不丢候选」三条。
"""

import pytest

from agent_harness.memory.extractor import ExtractionOutcome
from agent_harness.memory.types import MemoryScope
from agent_harness.memory.writeback import MemoryWriteback
from tests.conftest import make_session


class RecordingCapability:
    def __init__(self) -> None:
        self.stored: list[tuple[MemoryScope, str, dict]] = []

    async def store(self, scope: MemoryScope, content: str, metadata: dict) -> str:
        self.stored.append((scope, content, metadata))
        return f"mem-{len(self.stored)}"

    async def search(self, scope, query, limit):  # pragma: no cover — 本文件不用
        return []

    async def recall(self, scope, query, limit):  # pragma: no cover — 本文件不用
        return []


@pytest.mark.asyncio
async def test_extraction_fallback_is_recorded_as_degraded_event(tmp_path):
    class FallbackExtractor:
        async def extract(self, events):
            return ExtractionOutcome(
                [(MemoryScope.USER, "Prefers Rust", {"importance": 0.7})],
                degraded_reason="heuristic_fallback: ValidationError",
            )

    session = make_session(tmp_path)
    writer = MemoryWriteback(RecordingCapability(), FallbackExtractor())
    writer.submit(session, session.events)
    await writer.drain()

    degraded = [e for e in session.events if e.type == "memory/degraded"]
    assert len(degraded) == 1
    assert degraded[0].data["operation"] == "extraction"
    assert degraded[0].data["reason"] == "heuristic_fallback: ValidationError"


@pytest.mark.asyncio
async def test_successful_extraction_produces_no_degraded_event(tmp_path):
    """对照组：LLM 路径成功时不得产生噪音——否则「降级」信号会贬值到无人看。"""
    class CleanExtractor:
        async def extract(self, events):
            return ExtractionOutcome([(MemoryScope.USER, "Prefers Rust", {"importance": 0.9})])

    session = make_session(tmp_path)
    writer = MemoryWriteback(RecordingCapability(), CleanExtractor())
    writer.submit(session, session.events)
    await writer.drain()

    assert not [e for e in session.events if e.type == "memory/degraded"]


@pytest.mark.asyncio
async def test_fallback_candidates_are_still_stored(tmp_path):
    """降级只在**质量**，不在可用性：正则抽到的候选照常落库。"""
    class FallbackExtractor:
        async def extract(self, events):
            return ExtractionOutcome(
                [(MemoryScope.SESSION, "decision-x", {"importance": 0.5})],
                degraded_reason="heuristic_fallback: TimeoutError",
            )

    capability = RecordingCapability()
    session = make_session(tmp_path)
    writer = MemoryWriteback(capability, FallbackExtractor())
    writer.submit(session, session.events)
    await writer.drain()

    assert [content for _, content, _ in capability.stored] == ["decision-x"]
