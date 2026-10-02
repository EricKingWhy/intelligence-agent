"""#516：列表页摘要缓存（文件戳失效，TDD 红→绿）。

`read_session_summary` 的单趟流式扫描把单会话摘要从秒级压到几十 ms，但 4000 会话
的列表页每刷一次仍要把全部文件重扫一遍。本文件钉住缓存契约：

- 文件戳 `(size, mtime_ns)` 未变 → 直接复用上次结果（同一对象，不再扫盘）；
- 戳变化（外部写者追加，size 必变）→ 以磁盘为准重扫；
- 与扫描并发的外部追加不得被误标为已缓存（写缓存前再取一次戳，前后一致才入）。

与 `_last_seq` 的既有缓存同款纪律：缓存只服务本实例的重复读，跨实例/跨进程的
一致性以戳为准，不夸大为跨进程安全。
"""

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


def _event_line(
    session_id: str, seq: int, event_type: str, data: dict | None = None
) -> str:
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


def test_summary_is_cached_while_file_unchanged(
    store: JsonlSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件没变 → 第二次读命中缓存（同一对象，扫盘迭代器只跑一趟）。"""
    session_id = "summary-cache-hit"
    _write_log(
        store,
        session_id,
        [
            _event_line(session_id, 0, SESSION_STARTED),
            _event_line(session_id, 1, USER_MESSAGE, {"content": "hi"}),
        ],
    )

    calls: list[Path] = []
    original = store._iter_event_lines

    def counting_iterator(
        event_path: Path, limit: int | None = None
    ) -> Iterator[tuple[int, str]]:
        calls.append(event_path)
        yield from original(event_path, limit)

    monkeypatch.setattr(store, "_iter_event_lines", counting_iterator)

    first = store.read_session_summary(session_id)
    second = store.read_session_summary(session_id)

    assert first is not None
    assert first is second, "文件戳未变时必须返回缓存对象"
    assert len(calls) == 1, "第二次读不许再扫盘"


def test_summary_cache_invalidates_when_file_grows(store: JsonlSessionStore) -> None:
    """外部写者追加（size 变化）→ 戳失效 → 重扫出新事件数。"""
    session_id = "summary-cache-invalidate"
    path = _write_log(
        store,
        session_id,
        [
            _event_line(session_id, 0, SESSION_STARTED),
            _event_line(session_id, 1, USER_MESSAGE, {"content": "hi"}),
        ],
    )
    first = store.read_session_summary(session_id)
    assert first is not None and first.event_count == 2

    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            _event_line(session_id, 2, "tool/result", {"tool_call_id": "c1"}) + "\n"
        )

    second = store.read_session_summary(session_id)
    assert second is not None and second.event_count == 3


def test_summary_cache_does_not_pin_concurrent_append(
    store: JsonlSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扫描与外部追加并发时，不得把「旧内容 + 新戳」错误入缓存。

    在扫描进行中（迭代器挂起时）追加一行：扫描读到的是 2 条，追加后的戳是
    「3 条版」。若实现只取一次戳就入缓存，下次读会命中「2 条的旧结果 + 新戳」，
    直到文件再次变化都拿不到那条新事件——这是缓存最危险的静默漂移。
    """
    session_id = "summary-cache-race"
    path = _write_log(
        store,
        session_id,
        [
            _event_line(session_id, 0, SESSION_STARTED),
            _event_line(session_id, 1, USER_MESSAGE, {"content": "hi"}),
        ],
    )

    original = store._iter_event_lines
    appended = False

    def racing_iterator(
        event_path: Path, limit: int | None = None
    ) -> Iterator[tuple[int, str]]:
        nonlocal appended
        yield from original(event_path, limit)
        if not appended:
            appended = True
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    _event_line(session_id, 2, "tool/result", {"tool_call_id": "c1"})
                    + "\n"
                )

    monkeypatch.setattr(store, "_iter_event_lines", racing_iterator)

    raced = store.read_session_summary(session_id)
    assert raced is not None
    # 扫描读到 2 条；这次结果要么不入缓存，要么以与新内容一致的戳入缓存——
    # 无论如何，下一次读必须看到 3 条（不许静默钉住旧结果）。
    after = store.read_session_summary(session_id)
    assert after is not None and after.event_count == 3
