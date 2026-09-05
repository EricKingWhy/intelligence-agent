"""Artifact overflow 的公开边界与完整输出恢复。"""

import json

import pytest

from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.tooling import ToolResult
from agent_harness.tooling.overflow import ArtifactOverflowHandler
from tests.conftest import make_session


@pytest.mark.asyncio
async def test_small_output_returns_original_result(tmp_path):
    session = make_session(tmp_path)
    before = session.events
    result = ToolResult.success("ok", data={"output": "x" * 2000})
    handler = ArtifactOverflowHandler(FakeArtifactStore())
    compact, deferred = await handler.maybe_overflow(session, "call", "read", result)
    assert compact is result and deferred == []
    assert session.events == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["output", "content", "stdout", "stderr", "message"])
async def test_large_output_saved_before_summary_and_event(tmp_path, field):
    session = make_session(tmp_path)
    store = FakeArtifactStore()
    raw = "\n".join(f"line {i}: 中文" for i in range(5000))
    result = (ToolResult.success(raw) if field == "message" else
              ToolResult.success("ok", data={field: raw, "exit_code": 1}))
    before = result.model_dump()
    compact, deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "bash", result,
    )
    artifact = await store.load(compact.artifact_ref)
    assert artifact.content == raw
    summary = compact.message if field == "message" else compact.data[field]
    assert len(summary) < 2000
    assert "line 0:" in summary and "line 4999:" in summary
    assert f"use inspect_artifact({artifact.artifact_id})" in summary
    assert result.model_dump() == before
    # R6-7 契约：handler 不再直接 append——事件以 (type, data) 形式返回，
    # 由 Runtime 在 tool/call 落盘之后追加（消除事件日志前向引用）。
    assert deferred == [(
        "artifact/created",
        {"artifact_id": artifact.artifact_id, "session_id": session.session_id,
         "source_tool": "bash", "tool_call_id": "call",
         "size": len(raw.encode("utf-8")), "mime_type": "text/plain"},
    )]
    assert not any(e.type == "artifact/created" for e in session.events)


@pytest.mark.asyncio
async def test_both_streams_and_duplicate_message_are_preserved_and_bounded(tmp_path):
    session = make_session(tmp_path)
    store = FakeArtifactStore()
    stdout, stderr = "a" * 10000, "b" * 10000
    result = ToolResult.success(stdout, data={"stdout": stdout, "stderr": stderr})
    compact, _deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "bash", result,
    )
    artifact = await store.load(compact.artifact_ref)
    assert json.loads(artifact.content) == {
        "stdout": stdout, "stderr": stderr, "message": stdout,
    }
    assert artifact.mime_type == "application/json"
    assert len(compact.message) <= 2000
    assert all(len(value) <= 2000 for value in compact.data.values())


@pytest.mark.asyncio
async def test_upload_failure_does_not_create_reference_or_event(tmp_path):
    class UnavailableStore(FakeArtifactStore):
        async def save(self, *args, **kwargs):
            raise ConnectionError("unavailable")

    session = make_session(tmp_path)
    before = session.events
    result = ToolResult.success("x" * 5000)
    with pytest.raises(ConnectionError):
        await ArtifactOverflowHandler(UnavailableStore()).maybe_overflow(
            session, "call", "bash", result,
        )
    assert result.artifact_ref is None
    assert session.events == before


def test_overflow_chars_smaller_than_marker_is_rejected_at_construction():
    """overflow_chars 小于截断 marker 最小长度时构造期快速失败。

    marker（≈110+ 字符，含 artifact_id）本身不受 head/tail 预算约束：
    overflow_chars 太小时摘要必然超出预算、悄悄污染 Context——与其运行时
    静默违约，不如配置期直接拒绝。默认预算 2000 必须仍可正常构造。"""
    with pytest.raises(ValueError, match="overflow_chars"):
        ArtifactOverflowHandler(FakeArtifactStore(), overflow_chars=50)
    # 默认预算不受影响（既有行为回归锚点）。
    ArtifactOverflowHandler(FakeArtifactStore())
