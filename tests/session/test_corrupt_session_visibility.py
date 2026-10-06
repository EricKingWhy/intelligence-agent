"""#752：events.jsonl 零可解析事件时，会话不应从列表静默消失。

复现 issue #752 的三个症状：
1. `list_sessions` 直接丢弃 event_count == 0 的行 → 损坏会话在列表中不可见；
2. `get_events` 对零事件抛 `SessionNotFound`（误导性 404）；
3. `has_session` 对零事件返回 False。

期望（与 `recover` 的 409 诊断对齐）：
1. `list_sessions` 包含损坏会话，标记 `corrupted=True`；
2. `get_events` 对损坏日志抛 `EventLogCorruptError`（409），真不存在才 404；
3. `has_session` 对磁盘上存在（即使全坏）的会话返回 True。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_harness.session import JsonlSessionStore, SessionEvent
from agent_harness.session.errors import EventLogCorruptError, SessionNotFound


def _write_corrupt_log(tmp_path: Path, session_id: str) -> None:
    """写一个零可解析事件的 events.jsonl（全坏行）。"""
    path = tmp_path / session_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 两行坏 JSON（有换行结尾 = 完整坏行，非末尾撕裂片段）
    path.write_bytes(b"{not json}\n[also bad\n")


def _write_healthy_log(tmp_path: Path, session_id: str) -> None:
    event = SessionEvent(
        event_id="evt-0",
        seq=0,
        type="session/started",
        session_id=session_id,
        time="t0",
        data={},
    )
    path = tmp_path / session_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(event.to_dict()).encode("utf-8") + b"\n")


@pytest.mark.asyncio
async def test_list_includes_corrupt_session(tmp_path: Path, make_session_service) -> None:
    """损坏会话应出现在列表中并标记，而不是静默消失。"""
    _meta = AsyncMock()
    _meta.list_all.return_value = []
    service = make_session_service(
        store=JsonlSessionStore(root=tmp_path),
        session_meta_store=_meta,
    )
    _write_healthy_log(tmp_path, "healthy-1")
    _write_corrupt_log(tmp_path, "corrupt-1")

    summaries = await service.list_sessions()
    ids = {s.session_id for s in summaries}
    assert "healthy-1" in ids
    assert "corrupt-1" in ids, "损坏会话不应从列表静默消失"
    corrupt_row = next(s for s in summaries if s.session_id == "corrupt-1")
    assert corrupt_row.corrupted is True


@pytest.mark.asyncio
async def test_get_events_raises_corrupt_not_notfound(tmp_path: Path, make_session_service) -> None:
    """损坏日志的 get_events 应抛 409 损坏错误，而非误导性 404。"""
    _meta = AsyncMock()
    _meta.list_all.return_value = []
    service = make_session_service(
        store=JsonlSessionStore(root=tmp_path),
        session_meta_store=_meta,
    )
    _write_corrupt_log(tmp_path, "corrupt-1")

    with pytest.raises(EventLogCorruptError):
        await service.get_events("corrupt-1")


@pytest.mark.asyncio
async def test_get_events_still_404_for_truly_missing(tmp_path: Path, make_session_service) -> None:
    """真不存在的会话仍应 404（不改变既有契约）。"""
    _meta = AsyncMock()
    _meta.list_all.return_value = []
    service = make_session_service(
        store=JsonlSessionStore(root=tmp_path),
        session_meta_store=_meta,
    )

    with pytest.raises(SessionNotFound):
        await service.get_events("no-such-session")


@pytest.mark.asyncio
async def test_has_session_true_for_corrupt(tmp_path: Path, make_session_service) -> None:
    """磁盘上存在（即使全坏）的会话，has_session 应为 True。"""
    _meta = AsyncMock()
    _meta.list_all.return_value = []
    service = make_session_service(
        store=JsonlSessionStore(root=tmp_path),
        session_meta_store=_meta,
    )
    _write_corrupt_log(tmp_path, "corrupt-1")

    assert await service.has_session("corrupt-1") is True
    assert await service.has_session("no-such-session") is False
