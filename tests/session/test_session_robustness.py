"""Session 健壮性契约测试——append 词汇表校验、损坏行容错、增量 seq 计数器。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.session import (
    MODEL_DELTA,
    MODEL_STARTED,
    SESSION_RESUMED,
    SESSION_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
    SessionEvent,
)


@pytest.fixture
def store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path)


# ── append 词汇表校验（invariant #4：Event ≠ Diagnostic Log）──


class TestAppendVocabularyValidation:
    def test_append_rejects_stream_only_types(self, store: JsonlSessionStore):
        """model/started、model/delta 是流式专属信号，Session.append 必须拒绝持久化。"""
        session = Session.start(store)
        with pytest.raises(ValueError, match=MODEL_STARTED):
            session.append(MODEL_STARTED, {"model": "test-model"})
        with pytest.raises(ValueError, match=MODEL_DELTA):
            session.append(MODEL_DELTA, {"delta": "x"})

    def test_append_rejects_unknown_type(self, store: JsonlSessionStore):
        """未知事件类型（不在 EVENT_TYPES 词汇表）必须拒绝，防止拼写错误悄悄落盘。"""
        session = Session.start(store)
        with pytest.raises(ValueError, match="bogus/type"):
            session.append("bogus/type", {"content": "oops"})

    def test_rejected_append_persists_nothing(self, store: JsonlSessionStore):
        """被拒绝的 append 不得写入任何内容（先校验后写盘）。"""
        session = Session.start(store)
        with pytest.raises(ValueError):
            session.append(MODEL_DELTA, {"delta": "x"})
        # 磁盘上只有 session/started 一条
        assert len(store.read_events(session.session_id)) == 1
        assert session.next_seq == 1

    def test_append_valid_type_still_works(self, store: JsonlSessionStore):
        """合法持久化类型不受校验影响。"""
        session = Session.start(store)
        event = session.append(USER_MESSAGE, {"content": "hello"})
        assert event.type == USER_MESSAGE
        assert event.seq == 1
        assert len(store.read_events(session.session_id)) == 2


# ── read_events 损坏行容错（一行坏数据不能拖垮整个 session）──


def _good_line(sid: str, seq: int, event_type: str = SESSION_STARTED) -> bytes:
    event = SessionEvent(seq=seq, type=event_type, session_id=sid)
    return json.dumps(event.to_dict(), ensure_ascii=False).encode("utf-8") + b"\n"


def _write_events_file(
    store: JsonlSessionStore, sid: str, chunks: list[bytes]
) -> Path:
    """手写 events.jsonl（字节级）——模拟半行崩溃、非法 UTF-8 等磁盘损坏。"""
    path = store._events_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        for chunk in chunks:
            fh.write(chunk)
    return path


class TestReadEventsToleratesCorruptLines:
    def test_mixed_corrupt_lines_skip_and_keep_good_events(
        self, store: JsonlSessionStore
    ):
        """合法 JSON 但非事件字典（null / [1]）、非法 UTF-8 字节行——都按损坏行跳过。"""
        sid = "corrupt-lines"
        _write_events_file(
            store,
            sid,
            [
                _good_line(sid, 0, SESSION_STARTED),
                b"null\n",
                b"[1]\n",
                b"\xff\xfe\n",
                _good_line(sid, 1, USER_MESSAGE),
            ],
        )
        events = store.read_events(sid)
        assert [e.seq for e in events] == [0, 1]
        assert [e.type for e in events] == [SESSION_STARTED, USER_MESSAGE]

    def test_non_int_seq_line_is_skipped(self, store: JsonlSessionStore):
        """{"seq":"x"} 合法 JSON 但 seq 类型非法——跳过，避免污染 next_seq。"""
        sid = "bad-seq"
        _write_events_file(
            store,
            sid,
            [
                _good_line(sid, 0, SESSION_STARTED),
                b'{"seq":"x","type":"user/message","session_id":"bad-seq"}\n',
                _good_line(sid, 1, USER_MESSAGE),
            ],
        )
        events = store.read_events(sid)
        assert [e.seq for e in events] == [0, 1]

    def test_resume_succeeds_after_corrupt_lines(self, store: JsonlSessionStore):
        """一行坏数据不能 brick resume：坏行跳过后 Session.resume 正常，seq 从 max+1 继续。"""
        sid = "brick-resume"
        _write_events_file(
            store,
            sid,
            [
                _good_line(sid, 0, SESSION_STARTED),
                b"null\n",
                b"[1]\n",
                b"\xff\xfe\n",
                _good_line(sid, 1, USER_MESSAGE),
            ],
        )
        session = Session.resume(store, sid)
        # 2 条好事件 + session/resumed
        assert len(session.events) == 3
        assert session.events[-1].type == SESSION_RESUMED
        # 新追加事件从 max+1 继续（started=0, message=1, resumed=2 → 下一条 3）
        event = session.append(USER_MESSAGE, {"content": "after"})
        assert event.seq == 3


# ── 增量 seq 计数器（append 不做 O(n) 全量扫描；resume 一次性校验重算）──


class TestIncrementalSeqCounter:
    def test_append_500_events_seq_monotonic(self, store: JsonlSessionStore):
        """500 次追加：seq 0..499 严格单调递增，磁盘与内存一致。

        行为代理断言：O(n²)→O(1) 的复杂度变化由代码检查证明（见报告），
        此测试守住正确性——seq 分配在任何实现下都必须单调且无空洞。
        """
        session = Session.start(store)
        for i in range(499):
            session.append(USER_MESSAGE, {"content": f"msg-{i}"})
        assert session.next_seq == 500
        disk_events = store.read_events(session.session_id)
        assert [e.seq for e in disk_events] == list(range(500))
        assert [e.seq for e in session.events] == list(range(500))

    def test_resume_recomputes_counter_from_loaded_events(
        self, store: JsonlSessionStore
    ):
        """resume 时从已加载事件一次性重算计数器：新事件 seq = max+1。"""
        s1 = Session.start(store)
        for _ in range(5):
            s1.append(USER_MESSAGE, {"content": "x"})
        s2 = Session.resume(store, s1.session_id)
        # 6 条原事件 + session/resumed → 下一条 seq 7
        assert s2.next_seq == 7
        event = s2.append(USER_MESSAGE, {"content": "after-resume"})
        assert event.seq == 7

    def test_resume_rejects_seq_regression(self, store: JsonlSessionStore):
        """resume 校验 seq 严格递增：回退的损坏历史直接拒绝，不容忍。"""
        sid = "seq-regression"
        _write_events_file(
            store,
            sid,
            [
                _good_line(sid, 0, SESSION_STARTED),
                _good_line(sid, 2, USER_MESSAGE),
                _good_line(sid, 1, USER_MESSAGE),
            ],
        )
        with pytest.raises(ValueError, match="回退"):
            Session.resume(store, sid)


def test_negative_seq_line_is_skipped(store):
    """seq 为负的行不进事件列表，也不污染计数器——好行照常读出。"""
    sid = "neg-seq"
    bad = json.loads(_good_line(sid, 1, USER_MESSAGE).decode("utf-8"))
    bad["seq"] = -5
    _write_events_file(
        store,
        sid,
        [
            _good_line(sid, 0, SESSION_STARTED),
            json.dumps(bad, ensure_ascii=False).encode("utf-8") + b"\n",
        ],
    )
    events = store.read_events(sid)
    assert [e.seq for e in events] == [0]


# ── read_session_summary：列表页快路径（perf-fix 4，损坏 fallback 保精确）──


def _write_lines(store: JsonlSessionStore, sid: str, lines: list[str]) -> None:
    path = store._events_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _event_line(store: JsonlSessionStore, sid: str, seq: int, etype: str,
                data: dict) -> str:
    return json.dumps({"seq": seq, "type": etype, "session_id": sid,
                       "time": f"2026-09-06T00:00:{seq:02d}", "data": data},
                      ensure_ascii=False, separators=(",", ":"))


class TestReadSessionSummary:
    def test_clean_session_matches_full_read(self, store: JsonlSessionStore):
        """无损坏行时：event_count/时间/首条 user 消息与全量解析严格一致。"""
        sid = "summary-clean"
        lines = [_event_line(store, sid, 0, SESSION_STARTED, {})]
        lines.append(_event_line(store, sid, 1, USER_MESSAGE,
                                 {"content": "  帮我分析项目结构  "}))
        lines.append(_event_line(store, sid, 2, "model/completed",
                                 {"content": "好", "tool_calls": []}))
        lines.append(_event_line(store, sid, 3, "tool/result",
                                 {"tool_call_id": "c1", "content": "输出"}))
        _write_lines(store, sid, lines)

        stats = store.read_session_summary(sid)
        full = store.read_events(sid)

        assert stats is not None
        assert stats.event_count == len(full) == 4
        assert stats.first_event_time == full[0].time
        assert stats.last_event_time == full[-1].time
        assert stats.first_user_message == "帮我分析项目结构"  # strip + 无截断触发

    def test_no_user_message_returns_none(self, store: JsonlSessionStore):
        sid = "summary-nouser"
        _write_lines(store, sid, [
            _event_line(store, sid, 0, SESSION_STARTED, {}),
            _event_line(store, sid, 1, "model/completed", {"content": "hi"}),
        ])
        stats = store.read_session_summary(sid)
        assert stats is not None
        assert stats.first_user_message is None
        assert stats.event_count == 2

    def test_first_user_message_after_head_cap_returns_none(self, store: JsonlSessionStore):
        """user/message 出现在头部扫描上限之后 → None（前端有 events 扫描降级）。"""
        sid = "summary-deep"
        lines = [_event_line(store, sid, 0, SESSION_STARTED, {})]
        for i in range(1, 250):
            lines.append(_event_line(store, sid, i, "tool/result",
                                     {"tool_call_id": f"c{i}", "content": "x"}))
        lines.append(_event_line(store, sid, 250, USER_MESSAGE, {"content": "很晚"}))
        _write_lines(store, sid, lines)
        stats = store.read_session_summary(sid)
        assert stats is not None
        assert stats.first_user_message is None
        assert stats.event_count == 251

    def test_corrupt_tail_line_falls_back_to_exact(self, store: JsonlSessionStore):
        """末行半写（崩溃现场）→ 整体回退全量解析，event_count 精确排除坏行。"""
        sid = "summary-tail-corrupt"
        good = [_event_line(store, sid, i, USER_MESSAGE, {"content": f"m{i}"})
                for i in range(3)]
        _write_lines(store, sid, good + ['{"seq": 3, "type": "user/mess'])  # 半行

        stats = store.read_session_summary(sid)
        full = store.read_events(sid)

        assert stats is not None
        assert stats.event_count == len(full) == 3
        assert stats.first_user_message == "m0"
        assert stats.last_event_time == full[-1].time

    def test_corrupt_head_line_falls_back_to_exact(self, store: JsonlSessionStore):
        sid = "summary-head-corrupt"
        good = [_event_line(store, sid, i, USER_MESSAGE, {"content": f"m{i}"})
                for i in range(3)]
        _write_lines(store, sid, ["not-json"] + good)

        stats = store.read_session_summary(sid)
        full = store.read_events(sid)

        assert stats is not None
        assert stats.event_count == len(full) == 3
        assert stats.first_event_time == full[0].time

    def test_missing_session_returns_none(self, store: JsonlSessionStore):
        assert store.read_session_summary("no-such-session") is None

    def test_empty_file_returns_zero_count(self, store: JsonlSessionStore):
        sid = "summary-empty"
        _write_lines(store, sid, [])
        stats = store.read_session_summary(sid)
        assert stats is not None
        assert stats.event_count == 0
