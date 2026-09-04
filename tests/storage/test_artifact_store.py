"""FakeArtifactStore + compute_artifact_id + _slice_lines 单元测试。

验证 ArtifactStore 契约：
- save → load 往返（content-hash 寻址正确）
- inspect 按行范围
- inspect 按关键词
- max_lines 截断
- 去重（同内容同 ID）
"""

from __future__ import annotations

import pytest

from agent_harness.storage.artifact import (
    ArtifactSlice,
    ArtifactStore,
    FakeArtifactStore,
    compute_artifact_id,
)


class TestComputeArtifactId:
    def test_deterministic(self) -> None:
        assert compute_artifact_id("hello") == compute_artifact_id("hello")

    def test_different_content_different_id(self) -> None:
        assert compute_artifact_id("hello") != compute_artifact_id("world")

    def test_id_length(self) -> None:
        assert len(compute_artifact_id("anything")) == 16


class TestFakeArtifactStoreSave:
    @pytest.mark.asyncio
    async def test_save_returns_artifact_with_correct_id(self) -> None:
        store = FakeArtifactStore()
        content = "line1\nline2\nline3"
        artifact = await store.save(
            "session-1", content,
            mime_type="text/plain",
            source_tool="bash",
            tool_call_id="call-1",
        )
        assert artifact.artifact_id == compute_artifact_id(content)
        assert artifact.session_id == "session-1"
        assert artifact.size == len(content.encode("utf-8"))
        assert artifact.mime_type == "text/plain"
        assert artifact.source_tool == "bash"
        assert artifact.tool_call_id == "call-1"
        assert artifact.created_at  # non-empty ISO string
        assert artifact.content is None  # save 不填充 content

    @pytest.mark.asyncio
    async def test_save_dedup_same_content_same_id(self) -> None:
        store = FakeArtifactStore()
        a1 = await store.save("s1", "same", mime_type="text/plain", source_tool="bash", tool_call_id="c1")
        a2 = await store.save("s2", "same", mime_type="text/plain", source_tool="read", tool_call_id="c2")
        assert a1.artifact_id == a2.artifact_id


class TestFakeArtifactStoreLoad:
    @pytest.mark.asyncio
    async def test_load_returns_content(self) -> None:
        store = FakeArtifactStore()
        content = "hello\nworld"
        artifact = await store.save("s1", content, mime_type="text/plain", source_tool="bash", tool_call_id="c1")
        loaded = await store.load(artifact.artifact_id)
        assert loaded.content == content
        assert loaded.artifact_id == artifact.artifact_id

    @pytest.mark.asyncio
    async def test_load_nonexistent_raises(self) -> None:
        store = FakeArtifactStore()
        with pytest.raises(KeyError):
            await store.load("nonexistent")


class TestFakeArtifactStoreInspect:
    @staticmethod
    async def _make_store() -> tuple[FakeArtifactStore, str]:
        store = FakeArtifactStore()
        lines = [f"line {i}" for i in range(1, 101)]  # 100 行
        content = "\n".join(lines)
        artifact = await store.save("s1", content, mime_type="text/plain", source_tool="bash", tool_call_id="c1")
        return store, artifact.artifact_id

    @pytest.mark.asyncio
    async def test_inspect_default_returns_first_200_lines(self) -> None:
        store, artifact_id = await self._make_store()
        result = await store.inspect(artifact_id)
        assert isinstance(result, ArtifactSlice)
        assert result.total_lines == 100
        assert result.returned_lines == 100  # 100 < 200，不截断
        assert result.truncated is False
        assert result.lines[0] == {"line_number": 1, "text": "line 1"}
        assert result.lines[99] == {"line_number": 100, "text": "line 100"}

    @pytest.mark.asyncio
    async def test_inspect_line_range(self) -> None:
        store, artifact_id = await self._make_store()
        result = await store.inspect(artifact_id, start_line=10, end_line=20)
        assert result.returned_lines == 11
        assert result.lines[0] == {"line_number": 10, "text": "line 10"}
        assert result.lines[-1] == {"line_number": 20, "text": "line 20"}

    @pytest.mark.asyncio
    async def test_inspect_keyword_filter(self) -> None:
        store, artifact_id = await self._make_store()
        result = await store.inspect(artifact_id, keyword="5")
        # line 5, 15, 25, ..., 95 → 含 "5" 的行
        assert all("5" in entry["text"] for entry in result.lines)
        assert result.returned_lines == len(result.lines)

    @pytest.mark.asyncio
    async def test_inspect_max_lines_truncation(self) -> None:
        store, artifact_id = await self._make_store()
        result = await store.inspect(artifact_id, max_lines=10)
        assert result.returned_lines == 10
        assert result.truncated is True

    @pytest.mark.asyncio
    async def test_inspect_nonexistent_raises(self) -> None:
        store, _ = await self._make_store()
        with pytest.raises(KeyError):
            await store.inspect("nonexistent")


def test_artifact_store_is_abstract() -> None:
    """ArtifactStore 是 ABC，不能直接实例化。"""
    with pytest.raises(TypeError):
        ArtifactStore()  # type: ignore[abstract]
