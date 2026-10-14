"""#1082：`recover` 与 `pending_decisions` 共用只读恢复预检。

两个恢复入口的前导五步（校验 session ID、确保 store、`read_events_report`、
404/409 映射）必须由同一个私有方法提供；今后只改一个副本导致的漂移
（GET 把全坏日志报成 404、POST/GET 对同一日志给出不同错误）由本文件的
错误矩阵 parity 测试钉住。

边界（与既有行为逐字一致）：
- 无文件或空文件且无损坏行 → SessionNotFound（404）
- 零可解析事件但有损坏行 → EventLogCorruptError（409）
- 部分可解析且有损坏行 → EventLogCorruptError（409）
- 合法事件 → 返回已验证的事件列表
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.session import JsonlSessionStore, SessionEvent
from agent_harness.session.errors import (
    EventLogCorruptError,
    SessionNotFound,
)
from agent_harness.session.service import InvalidSessionId


def _write_log(tmp_path: Path, session_id: str, payload: bytes) -> None:
    path = tmp_path / session_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _healthy_line(session_id: str) -> bytes:
    event = SessionEvent(
        event_id="evt-0",
        seq=0,
        type="session/started",
        session_id=session_id,
        time="t0",
        data={},
    )
    return json.dumps(event.to_dict()).encode("utf-8") + b"\n"


@pytest.fixture
def precheck_service(tmp_path: Path, make_session_service):
    """带真实 JsonlSessionStore 的 SessionService（读真实文件）。"""
    return make_session_service(store=JsonlSessionStore(tmp_path))


class TestReadEventsPrecheck:
    @pytest.mark.asyncio
    async def test_valid_log_returns_verified_events(
        self, tmp_path: Path, precheck_service
    ):
        _write_log(tmp_path, "ok-1", _healthy_line("ok-1"))
        events = await precheck_service._read_events_precheck("ok-1")
        assert len(events) == 1
        assert events[0].type == "session/started"

    @pytest.mark.asyncio
    async def test_missing_session_raises_not_found(self, precheck_service):
        with pytest.raises(SessionNotFound):
            await precheck_service._read_events_precheck("no-such-session")

    @pytest.mark.asyncio
    async def test_empty_file_raises_not_found(
        self, tmp_path: Path, precheck_service
    ):
        _write_log(tmp_path, "empty-1", b"")
        with pytest.raises(SessionNotFound):
            await precheck_service._read_events_precheck("empty-1")

    @pytest.mark.asyncio
    async def test_fully_corrupt_log_raises_corrupt(
        self, tmp_path: Path, precheck_service
    ):
        _write_log(tmp_path, "bad-1", b"{not json}\n[also bad\n")
        with pytest.raises(EventLogCorruptError):
            await precheck_service._read_events_precheck("bad-1")

    @pytest.mark.asyncio
    async def test_partially_corrupt_log_raises_corrupt(
        self, tmp_path: Path, precheck_service
    ):
        _write_log(
            tmp_path, "half-1", _healthy_line("half-1") + b"{not json}\n"
        )
        with pytest.raises(EventLogCorruptError):
            await precheck_service._read_events_precheck("half-1")

    @pytest.mark.asyncio
    async def test_unsafe_session_id_rejected(self, precheck_service):
        with pytest.raises(InvalidSessionId):
            await precheck_service._read_events_precheck("../etc/passwd")


class TestPrecheckParity:
    """recover 与 pending_decisions 对同一日志必须给出相同的错误类别。

    这是 #1082 的核心防漂移测试：两个入口共用同一个预检方法，今后任何
    只改一个副本的行为分歧都会在这里变红。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "session_id,payload",
        [
            ("missing-1", None),
            ("empty-2", b""),
            ("bad-2", b"{not json}\n"),
            ("half-2", None),  # 下面单独写半坏内容
        ],
    )
    async def test_error_class_parity(
        self, tmp_path: Path, precheck_service, session_id, payload
    ):
        if payload is not None:
            _write_log(tmp_path, session_id, payload)
        elif session_id == "half-2":
            _write_log(
                tmp_path, session_id, _healthy_line(session_id) + b"bad\n"
            )

        recover_exc = None
        try:
            await precheck_service.recover(session_id)
        except Exception as exc:  # noqa: BLE001 — 记录类别用
            recover_exc = type(exc)

        pending_exc = None
        try:
            await precheck_service.pending_decisions(session_id)
        except Exception as exc:  # noqa: BLE001 — 记录类别用
            pending_exc = type(exc)

        assert recover_exc is not None, "recover 应抛出领域异常"
        assert pending_exc is not None, "pending_decisions 应抛出领域异常"
        assert recover_exc is pending_exc, (
            f"两入口错误类别漂移：recover={recover_exc}, "
            f"pending_decisions={pending_exc}"
        )

    @pytest.mark.asyncio
    async def test_unsafe_id_parity(self, precheck_service):
        for method in ("recover", "pending_decisions"):
            with pytest.raises(InvalidSessionId):
                await getattr(precheck_service, method)("../etc/passwd")
