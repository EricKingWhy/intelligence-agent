"""Frozen JSONL reader compatibility contract for #259."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_harness.session import (
    SESSION_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)
from agent_harness.session.header import StartedHeader
from agent_harness.session.store import SessionSummaryStats

_STORE_LOGGER = "agent_harness.session.store"


@pytest.fixture
def store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path)


def _event_line(
    session_id: str,
    seq: int,
    event_type: str,
    *,
    timestamp: str,
    data: dict | None = None,
    agent_id: str | None = None,
) -> bytes:
    event = SessionEvent(
        event_id=f"evt-{seq}",
        seq=seq,
        type=event_type,
        session_id=session_id,
        time=timestamp,
        data=data or {},
        agent_id=agent_id,
    )
    return json.dumps(event.to_dict(), ensure_ascii=False).encode("utf-8") + b"\n"


def _write_events(store: JsonlSessionStore, session_id: str, payload: bytes) -> Path:
    path = store._events_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_read_events_bad_line_and_diagnostic_golden(
    store: JsonlSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = "reader-diagnostics"
    payload = b"".join(
        [
            _event_line(session_id, 0, SESSION_STARTED, timestamp="t0"),
            b"null\n",
            b'{"type":"user/message","session_id":"reader-diagnostics"}\n',
            b'{"seq":true,"type":"user/message","session_id":"reader-diagnostics"}\n',
            b'{"seq":"1","type":"user/message","session_id":"reader-diagnostics"}\n',
            b'{"seq":-1,"type":"user/message","session_id":"reader-diagnostics"}\n',
            b"\xff\n",
            _event_line(
                session_id, 1, USER_MESSAGE, timestamp="t1", data={"content": "kept"}
            ),
            b'{"seq":2,"type":"user/message"',  # incomplete final line; no newline
        ]
    )
    _write_events(store, session_id, payload)

    caplog.set_level(logging.WARNING, logger=_STORE_LOGGER)
    events = store.read_events(session_id)

    assert type(events) is list
    assert events == [
        SessionEvent(
            event_id="evt-0", seq=0, type=SESSION_STARTED,
            session_id=session_id, time="t0",
        ),
        SessionEvent(
            event_id="evt-1", seq=1, type=USER_MESSAGE,
            session_id=session_id, time="t1", data={"content": "kept"},
        ),
    ]
    assert [
        (record.name, record.levelno, record.getMessage())
        for record in caplog.records
    ] == [
        (_STORE_LOGGER, logging.WARNING, "跳过损坏行 events.jsonl:2（合法 JSON 但非事件字典）"),
        (_STORE_LOGGER, logging.WARNING, "跳过损坏行 events.jsonl:3（seq 缺失、类型非法或为负）"),
        (_STORE_LOGGER, logging.WARNING, "跳过损坏行 events.jsonl:4（seq 缺失、类型非法或为负）"),
        (_STORE_LOGGER, logging.WARNING, "跳过损坏行 events.jsonl:5（seq 缺失、类型非法或为负）"),
        (_STORE_LOGGER, logging.WARNING, "跳过损坏行 events.jsonl:6（seq 缺失、类型非法或为负）"),
        (_STORE_LOGGER, logging.WARNING, "跳过损坏行 events.jsonl:7（半行或写入中断）"),
        (_STORE_LOGGER, logging.WARNING, "跳过损坏行 events.jsonl:9（半行或写入中断）"),
    ]


def test_missing_and_empty_jsonl_return_shapes(store: JsonlSessionStore) -> None:
    missing_id = "missing-reader-log"
    assert type(store.read_events(missing_id)) is list
    assert store.read_events(missing_id) == []
    assert store.read_session_summary(missing_id) is None
    assert store.read_started_header(missing_id) is None

    empty_id = "empty-reader-log"
    empty_path = _write_events(store, empty_id, b"")
    assert empty_path.stat().st_size == 0
    assert store.read_events(empty_id) == []
    assert store.read_started_header(empty_id) is None

    summary = store.read_session_summary(empty_id)
    assert type(summary) is SessionSummaryStats
    assert asdict(summary) == {
        "session_id": empty_id,
        "event_count": 0,
        "first_event_time": None,
        "last_event_time": None,
        "first_user_message": None,
        "trace_id": None,
        "trace_url": None,
        "workspace": None,
        "archived": False,
    }


def test_started_header_shape_skips_bad_prefix_and_stops_at_header(
    store: JsonlSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = "header-golden"
    header_time = "2026-09-19T00:00:00+00:00"
    header = _event_line(
        session_id,
        0,
        SESSION_STARTED,
        timestamp=header_time,
        data={"cwd": "D:/workspace"},
        agent_id="agent-golden",
    )
    _write_events(store, session_id, b"\n{bad-json\n" + header + b"\xff\n")

    caplog.set_level(logging.WARNING, logger=_STORE_LOGGER)
    result = store.read_started_header(session_id)

    assert type(result) is StartedHeader
    assert result == StartedHeader(
        session_id=session_id,
        cwd="D:/workspace",
        created_at=header_time,
        agent_id="agent-golden",
    )
    assert [
        (record.levelno, record.getMessage()) for record in caplog.records
    ] == [(logging.WARNING, "跳过损坏行 events.jsonl:2（半行或写入中断）")]


def test_started_header_does_not_search_past_first_valid_non_started_event(
    store: JsonlSessionStore,
) -> None:
    session_id = "header-not-started-first"
    _write_events(
        store,
        session_id,
        _event_line(
            session_id, 0, USER_MESSAGE, timestamp="t0", data={"content": "first"}
        )
        + _event_line(
            session_id, 1, SESSION_STARTED, timestamp="t1", data={"cwd": "D:/late"}
        ),
    )

    assert store.read_started_header(session_id) is None


@pytest.mark.parametrize(
    ("user_position", "expected_message"),
    [(200, "at-limit"), (201, None)],
)
def test_summary_head_parse_limit_boundary_is_200(
    store: JsonlSessionStore, user_position: int, expected_message: str | None
) -> None:
    session_id = f"summary-limit-{user_position}"
    lines: list[bytes] = []
    for position in range(1, user_position + 1):
        seq = position - 1
        if position == 1:
            event_type, data = SESSION_STARTED, None
        elif position == user_position:
            event_type, data = USER_MESSAGE, {"content": expected_message or "after-limit"}
        else:
            event_type, data = "tool/result", {"tool_call_id": f"call-{seq}"}
        lines.append(
            _event_line(
                session_id, seq, event_type, timestamp=f"t{seq}", data=data
            )
        )
    _write_events(store, session_id, b"".join(lines))

    summary = store.read_session_summary(session_id)

    assert type(summary) is SessionSummaryStats
    assert summary.event_count == user_position
    assert summary.first_user_message == expected_message


def test_summary_corrupt_tail_fallback_shape_golden(
    store: JsonlSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = "summary-fallback-golden"
    payload = b"".join(
        [
            _event_line(session_id, 0, SESSION_STARTED, timestamp="t0"),
            _event_line(
                session_id, 1, USER_MESSAGE, timestamp="t1", data={"content": "  hello  "}
            ),
            _event_line(
                session_id,
                2,
                "run/completed",
                timestamp="t2",
                data={
                    "final_text": "done",
                    "trace_id": "trace-golden",
                    "trace_url": "https://trace.example/golden",
                },
            ),
            b'{"seq":3,"type":"user/message"',  # incomplete final line; no newline
        ]
    )
    _write_events(store, session_id, payload)

    caplog.set_level(logging.WARNING, logger=_STORE_LOGGER)
    summary = store.read_session_summary(session_id)

    assert type(summary) is SessionSummaryStats
    assert asdict(summary) == {
        "session_id": session_id,
        "event_count": 3,
        "first_event_time": "t0",
        "last_event_time": "t2",
        "first_user_message": "hello",
        "trace_id": "trace-golden",
        "trace_url": "https://trace.example/golden",
        "workspace": None,
        "archived": False,
    }
    assert caplog.records
    assert all(record.name == _STORE_LOGGER for record in caplog.records)
    assert all(record.levelno == logging.WARNING for record in caplog.records)
    assert {
        record.getMessage() for record in caplog.records
    } == {"跳过损坏行 events.jsonl:4（半行或写入中断）"}
