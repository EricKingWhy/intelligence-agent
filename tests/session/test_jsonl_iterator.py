"""Internal JSONL scan contract for #260."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_harness.session import (
    SESSION_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)


@pytest.fixture
def store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path)


def _event_line(session_id: str, seq: int, event_type: str, data: dict | None = None) -> str:
    return json.dumps(
        SessionEvent(
            event_id=f"evt-{seq}",
            seq=seq,
            type=event_type,
            session_id=session_id,
            time=f"t{seq}",
            data=data or {},
        ).to_dict(),
        ensure_ascii=False,
    )


def _write_log(store: JsonlSessionStore, session_id: str, lines: list[str]) -> Path:
    path = store._events_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_store_scanners_share_one_line_iterator(
    store: JsonlSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "shared-jsonl-iterator"
    path = _write_log(
        store,
        session_id,
        [
            _event_line(session_id, 0, SESSION_STARTED, {"cwd": "D:/workspace"}),
            _event_line(session_id, 1, USER_MESSAGE, {"content": "hello"}),
            _event_line(session_id, 2, "run/completed", {"final_text": "done"}),
        ],
    )

    original = store._iter_event_lines
    calls: list[tuple[Path, int | None]] = []

    def recording_iterator(
        event_path: Path, limit: int | None = None
    ) -> Iterator[tuple[int, str]]:
        calls.append((event_path, limit))
        yield from original(event_path, limit)

    monkeypatch.setattr(store, "_iter_event_lines", recording_iterator)

    assert len(store.read_events(session_id)) == 3
    assert store.read_session_summary(session_id).event_count == 3
    assert store._summary_fallback(session_id).event_count == 3
    assert store.read_started_header(session_id).cwd == "D:/workspace"

    assert calls == [(path, None)] * 4


def test_line_iterator_limit_counts_physical_lines(
    store: JsonlSessionStore,
) -> None:
    session_id = "limited-jsonl-iterator"
    first = _event_line(session_id, 0, SESSION_STARTED)
    _write_log(store, session_id, [first, "", _event_line(session_id, 1, USER_MESSAGE)])

    iterator = store._iter_event_lines(store._events_path(session_id), limit=2)

    assert next(iterator) == (1, first + "\n")
    assert next(iterator) == (2, "\n")
    with pytest.raises(StopIteration):
        next(iterator)


def test_large_summary_parses_only_head_and_tail(
    store: JsonlSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "large-summary-streaming"
    line_count = 1200
    lines = [_event_line(session_id, 0, SESSION_STARTED)]
    lines.append(_event_line(session_id, 1, USER_MESSAGE, {"content": "hello"}))
    lines.extend(
        _event_line(session_id, seq, "tool/result", {"tool_call_id": f"call-{seq}"})
        for seq in range(2, line_count)
    )
    _write_log(store, session_id, lines)

    parsed_lines: list[int] = []
    original = JsonlSessionStore._parse_event_line

    def counting_parser(raw_line: str, path_name: str, lineno: int) -> SessionEvent | None:
        parsed_lines.append(lineno)
        return original(raw_line, path_name, lineno)

    monkeypatch.setattr(store, "_parse_event_line", counting_parser)
    monkeypatch.setattr(
        store,
        "read_events",
        lambda _session_id: pytest.fail("clean summary must not materialize full history"),
    )

    summary = store.read_session_summary(session_id)

    assert summary is not None
    assert summary.event_count == line_count
    assert summary.first_user_message == "hello"
    assert parsed_lines == [1, 2, line_count]
