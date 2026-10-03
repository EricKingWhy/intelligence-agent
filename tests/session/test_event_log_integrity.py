"""#565：events.jsonl 完整性闸门与脱敏诊断契约。

四条验收线：

1. **未写完整的末尾片段**（无换行结尾 + 解析不出）——写入中断的预期形状，
   容错跳过（DEBUG），不算损坏，不阻断恢复；
2. **完整坏行**（含换行结尾的坏 JSON 尾行）——写入已完成，内容坏是磁盘/编辑
   问题，记 WARNING + 脱敏定位记录，恢复拒绝；
3. **中间损坏**——同上，且通常留下 seq 断层；
4. **seq 重复 / 缺号**——持久化 seq 连续是写入侧不变量（`Session.append` max+1
   + store 守卫拒重号），文件里的断层/重号即事实缺失，恢复拒绝。

另钉两条边界：诊断记录**脱敏**（只有定位元数据，不含行内容），字节偏移 / sha256
与磁盘原字节**逐位一致**（二进制遍历，不是文本替换后的失真值）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import pytest

from agent_harness.session import (
    SESSION_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)
from agent_harness.session.errors import EventLogCorruptError
from agent_harness.session.service import SessionService
from agent_harness.session.store import CORRUPT_LINE_REASONS, EventLogIntegrity

_STORE_LOGGER = "agent_harness.session.store"

#: 恢复入口的拒绝文案锚点（`_raise_if_event_log_corrupt` 产出）。
_REFUSAL_MARKER = "事件日志损坏，恢复已拒绝"


@pytest.fixture
def store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path)


def _line(
    session_id: str, seq: int, event_type: str, data: dict | None = None
) -> bytes:
    event = SessionEvent(
        event_id=f"evt-{seq}",
        seq=seq,
        type=event_type,
        session_id=session_id,
        time=f"t{seq}",
        data=data or {},
    )
    return json.dumps(event.to_dict(), ensure_ascii=False).encode("utf-8") + b"\n"


def _write(store: JsonlSessionStore, session_id: str, payload: bytes) -> Path:
    path = store._events_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# ── 健康基线（防误报）───────────────────────────────────────────────


def test_clean_log_reports_healthy(store: JsonlSessionStore) -> None:
    session_id = "clean-integrity"
    _write(
        store,
        session_id,
        _line(session_id, 0, SESSION_STARTED)
        + _line(session_id, 1, USER_MESSAGE, {"content": "hi"})
        + _line(session_id, 2, "run/completed", {"final_text": "done"}),
    )

    events, integrity = store.read_events_report(session_id)

    assert [event.seq for event in events] == [0, 1, 2]
    assert integrity == EventLogIntegrity()
    assert integrity.healthy


def test_missing_log_is_healthy_and_empty(store: JsonlSessionStore) -> None:
    events, integrity = store.read_events_report("no-such-session")

    assert events == []
    assert integrity.healthy


# ── 验收 1：未写完整的末尾片段（容错，不算损坏）──────────────────────


@pytest.mark.parametrize(
    "tail",
    [
        b'{"seq":2,"type":"user/message"',  # JSON 未闭合，无换行
        b'{"seq":2,"type":"user/mess',  # 截断更早
        b"\xe4\xb8\xad\xe6",  # 多字节 UTF-8 断在字符中间，无换行
    ],
)
def test_incomplete_tail_is_tolerated_not_corruption(
    store: JsonlSessionStore, tail: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = "partial-tail-tolerated"
    _write(
        store,
        session_id,
        _line(session_id, 0, SESSION_STARTED)
        + _line(session_id, 1, USER_MESSAGE, {"content": "kept"})
        + tail,
    )

    caplog.set_level(logging.WARNING, logger=_STORE_LOGGER)
    events, integrity = store.read_events_report(session_id)

    assert [event.seq for event in events] == [0, 1]
    assert integrity.healthy, integrity
    assert integrity.corrupt_lines == ()
    assert integrity.seq_gaps == ()
    assert integrity.seq_duplicates == ()
    # 未写完整的末尾片段不进 WARNING——它是崩溃恢复的预期形状，不是损坏。
    assert [record.getMessage() for record in caplog.records] == []


# ── 验收 2：完整坏尾行（有换行）→ 损坏 ──────────────────────────────


def test_complete_bad_tail_line_is_corruption(store: JsonlSessionStore) -> None:
    session_id = "bad-tail-line"
    bad_tail = b'{"seq":1,"type":"user/message"\n'  # 坏 JSON，但带换行结尾
    _write(store, session_id, _line(session_id, 0, SESSION_STARTED) + bad_tail)

    events, integrity = store.read_events_report(session_id)

    assert [event.seq for event in events] == [0]
    assert not integrity.healthy
    assert [record.reason for record in integrity.corrupt_lines] == ["bad_json"]
    assert integrity.corrupt_lines[0].lineno == 2
    # 尾行坏 → 后面没有可解析事件，gap 检测看不到断层；损坏本身已由
    # corrupt_lines 点名（seq 缺号只会发生在「坏行之后还有事件」的场景）。
    assert integrity.seq_gaps == ()


def test_complete_line_with_invalid_utf8_is_corruption(
    store: JsonlSessionStore,
) -> None:
    session_id = "bad-utf8-line"
    _write(
        store,
        session_id,
        _line(session_id, 0, SESSION_STARTED) + b"\xff\n",
    )

    _events, integrity = store.read_events_report(session_id)

    assert [record.reason for record in integrity.corrupt_lines] == ["invalid_utf8"]
    assert integrity.corrupt_lines[0].lineno == 2


# ── 验收 3：中间损坏（通常伴随 seq 断层）────────────────────────────


def test_mid_file_corruption_recorded_with_seq_gap(
    store: JsonlSessionStore,
) -> None:
    session_id = "mid-file-corrupt"
    _write(
        store,
        session_id,
        _line(session_id, 0, SESSION_STARTED)
        + _line(session_id, 1, USER_MESSAGE, {"content": "kept"})
        + b"garbage-not-json\n"
        + _line(session_id, 3, "run/completed", {"final_text": "done"}),
    )

    events, integrity = store.read_events_report(session_id)

    # 容错读路径仍在（显示不被 brick），但报告点名了事实缺口。
    assert [event.seq for event in events] == [0, 1, 3]
    assert not integrity.healthy
    assert [record.reason for record in integrity.corrupt_lines] == ["bad_json"]
    assert integrity.corrupt_lines[0].lineno == 3
    assert integrity.seq_gaps == ((2, 2),)


# ── 验收 4：seq 重复 / 缺号（无坏行也要报告）────────────────────────


def test_seq_duplicate_is_reported(store: JsonlSessionStore) -> None:
    session_id = "seq-duplicate"
    _write(
        store,
        session_id,
        _line(session_id, 0, SESSION_STARTED)
        + _line(session_id, 1, USER_MESSAGE, {"content": "once"})
        + _line(session_id, 1, USER_MESSAGE, {"content": "again"}),
    )

    events, integrity = store.read_events_report(session_id)

    assert [event.seq for event in events] == [0, 1, 1]
    assert integrity.corrupt_lines == ()
    assert integrity.seq_duplicates == (1,)
    assert integrity.seq_gaps == ()
    assert not integrity.healthy


def test_seq_gap_without_corrupt_line_is_reported(
    store: JsonlSessionStore,
) -> None:
    session_id = "seq-gap-no-corrupt-line"
    # 整行丢失（手工编辑 / 磁盘异常）：没有坏行，但 seq 不连续。
    _write(
        store,
        session_id,
        _line(session_id, 0, SESSION_STARTED)
        + _line(session_id, 2, USER_MESSAGE, {"content": "skipped 1"}),
    )

    _events, integrity = store.read_events_report(session_id)

    assert integrity.corrupt_lines == ()
    assert integrity.seq_gaps == ((1, 1),)
    assert not integrity.healthy


# ── 字节保真 + 脱敏 ────────────────────────────────────────────────


def test_byte_offset_and_sha256_match_disk_bytes(
    store: JsonlSessionStore,
) -> None:
    session_id = "byte-fidelity"
    bad = b'{"seq":1,BROKEN payload\n'
    path = _write(
        store,
        session_id,
        _line(session_id, 0, SESSION_STARTED) + bad + _line(session_id, 2, USER_MESSAGE),
    )

    on_disk = path.read_bytes()
    _events, integrity = store.read_events_report(session_id)

    record = integrity.corrupt_lines[0]
    assert on_disk[record.byte_offset : record.byte_offset + record.byte_length] == bad
    assert record.line_sha256 == hashlib.sha256(bad).hexdigest()
    assert record.byte_length == len(bad)


def test_diagnostic_log_is_sanitized(
    store: JsonlSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    session_id = "sanitized-diagnostic"
    sentinel = "SENTINEL-SECRET-VALUE"
    secret_line = (
        f'{{"seq":1,"type":"user/message","session_id":"{sentinel}"'.encode()
    )
    _write(
        store,
        session_id,
        _line(session_id, 0, SESSION_STARTED) + secret_line + b"\n",
    )

    caplog.set_level(logging.WARNING, logger=_STORE_LOGGER)
    _events, integrity = store.read_events_report(session_id)

    assert [record.reason for record in integrity.corrupt_lines] == ["bad_json"]
    messages = [record.getMessage() for record in caplog.records]
    assert messages, "完整坏行必须有 WARNING 信号"
    joined = "\n".join(messages)
    assert sentinel not in joined, "诊断不得复制行内容"
    assert "offset=" in joined
    assert "sha256=" in joined
    assert "reason=bad_json" in joined


def test_corrupt_line_reasons_vocabulary_is_stable() -> None:
    # 词表只增不改义（docstring 契约）：消费方是恢复拒绝文案与测试锚点。
    assert CORRUPT_LINE_REASONS == frozenset(
        {"invalid_utf8", "bad_json", "not_event_dict", "bad_seq", "bad_event_fields"}
    )


def test_every_produced_reason_is_in_vocabulary(store: JsonlSessionStore) -> None:
    session_id = "reason-vocabulary"
    payload = b"".join(
        [
            _line(session_id, 0, SESSION_STARTED),
            b"null\n",  # not_event_dict
            b'{"seq":true,"type":"user/message"}\n',  # bad_seq
            b'{"seq":1,"type":"user/message"\n',  # bad_json
            b"\xff\n",  # invalid_utf8
        ]
    )
    _write(store, session_id, payload)

    _events, integrity = store.read_events_report(session_id)

    assert {record.reason for record in integrity.corrupt_lines} <= CORRUPT_LINE_REASONS


# ── 恢复入口拒绝（service 层闸门）──────────────────────────────────


def _corrupt_session(store: JsonlSessionStore, session_id: str) -> None:
    _write(
        store,
        session_id,
        _line(session_id, 0, SESSION_STARTED)
        + _line(session_id, 1, USER_MESSAGE, {"content": "kept"})
        + b"garbage-not-json\n"
        + _line(session_id, 3, "run/completed", {"final_text": "done"}),
    )


def test_gate_refuses_gap_and_duplicate_without_touching_log(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(root=tmp_path)
    session_id = "gate-refuses"
    _corrupt_session(store, session_id)
    before = store._events_path(session_id).read_bytes()

    with pytest.raises(EventLogCorruptError) as excinfo:
        SessionService._raise_if_event_log_corrupt(
            session_id,
            store.read_events_report(session_id)[1],
        )

    message = str(excinfo.value)
    assert _REFUSAL_MARKER in message
    assert "reason=bad_json" in message
    assert "seq 缺号 2" in message
    assert "不可重试" in message
    # 拒绝是**只读**的：原字节一字未改（人工修复是唯一出路）。
    assert store._events_path(session_id).read_bytes() == before


def test_gate_is_noop_on_healthy_integrity() -> None:
    assert SessionService._raise_if_event_log_corrupt(
        "healthy", EventLogIntegrity()
    ) is None


def test_recover_refuses_corrupt_log(tmp_path: Path, make_session_service) -> None:
    store = JsonlSessionStore(root=tmp_path)
    session_id = "recover-refuses-corrupt"
    _corrupt_session(store, session_id)
    service = make_session_service(store=store)

    with pytest.raises(EventLogCorruptError):
        asyncio.run(service.recover(session_id))


def test_recover_missing_session_still_not_found(
    tmp_path: Path, make_session_service
) -> None:
    """闸门只在有事件时生效——不存在的会话仍是 404（SessionNotFound），不误报损坏。"""
    from agent_harness.session.errors import SessionNotFound

    service = make_session_service(store=JsonlSessionStore(root=tmp_path))

    with pytest.raises(SessionNotFound):
        asyncio.run(service.recover("truly-missing-session"))