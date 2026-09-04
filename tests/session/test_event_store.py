"""Ticket A — SessionEvent DTO + JsonlSessionStore 契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.session import (
    EVENT_TYPES,
    MODEL_COMPLETED,
    SESSION_STARTED,
    TOOL_CALL,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)

# ── SessionEvent DTO ──


class TestSessionEventDTO:
    def test_default_fields_populate(self):
        event = SessionEvent(type=SESSION_STARTED, session_id="s1")
        assert event.event_id  # UUID 自动生成
        assert event.seq == 0
        assert event.time  # ISO 时间戳自动生成
        assert event.type == SESSION_STARTED
        assert event.session_id == "s1"
        assert event.run_id is None
        assert event.agent_id is None
        assert event.step_id is None
        assert event.data == {}
        assert event.source_event_ids is None

    def test_all_event_types_registered(self):
        # Phase 1：10 种基础事件；Phase 9：+ model/started + model/delta（流式信号）；
        # Phase 4：+ operation/reconcile-required（UNKNOWN Operation 人工裁决信号，#30）。
        # Phase 5：+ artifact/created（大 Tool 输出完整保存，#47）。
        expected = {
            "session/started",
            "session/resumed",
            "run/started",
            "run/completed",
            "run/failed",
            "user/message",
            "model/started",
            "model/delta",
            "model/completed",
            "model/failed",
            "tool/call",
            "tool/result",
            "operation/reconcile-required",
            "artifact/created",
            "context/compacted",
        }
        assert EVENT_TYPES == expected

    def test_to_dict_omits_none_fields(self):
        event = SessionEvent(seq=1, type=SESSION_STARTED, session_id="s1")
        d = event.to_dict()
        assert "run_id" not in d
        assert "agent_id" not in d
        assert "step_id" not in d
        assert "source_event_ids" not in d
        assert "data" not in d  # 空 dict 也省略
        assert d["seq"] == 1
        assert d["type"] == "session/started"

    def test_to_dict_includes_populated_optional_fields(self):
        event = SessionEvent(
            seq=2,
            type=TOOL_CALL,
            session_id="s1",
            run_id="r1",
            agent_id="default",
            step_id=1,
            data={"command": "ls"},
            source_event_ids=["evt-prev"],
        )
        d = event.to_dict()
        assert d["run_id"] == "r1"
        assert d["agent_id"] == "default"
        assert d["step_id"] == 1
        assert d["data"] == {"command": "ls"}
        assert d["source_event_ids"] == ["evt-prev"]

    def test_from_dict_roundtrip(self):
        original = SessionEvent(
            seq=3,
            type=MODEL_COMPLETED,
            session_id="s1",
            run_id="r1",
            data={"content": "hello"},
        )
        reconstructed = SessionEvent.from_dict(original.to_dict())
        assert reconstructed.seq == original.seq
        assert reconstructed.type == original.type
        assert reconstructed.session_id == original.session_id
        assert reconstructed.run_id == original.run_id
        assert reconstructed.data == original.data
        assert reconstructed.event_id == original.event_id

    def test_frozen_dataclass_is_immutable(self):
        event = SessionEvent(type=SESSION_STARTED, session_id="s1")
        with pytest.raises(AttributeError):
            event.seq = 99  # type: ignore[misc]


# ── JsonlSessionStore ──


class TestJsonlSessionStore:
    def test_append_and_read_roundtrip(self, tmp_path: Path):
        store = JsonlSessionStore(root=tmp_path)
        sid = "session-001"

        events = [
            SessionEvent(seq=0, type=SESSION_STARTED, session_id=sid),
            SessionEvent(seq=1, type=MODEL_COMPLETED, session_id=sid, data={"content": "hi"}),
        ]
        for e in events:
            store.append_event(sid, e)

        loaded = store.read_events(sid)
        assert len(loaded) == 2
        assert loaded[0].seq == 0
        assert loaded[0].type == "session/started"
        assert loaded[1].seq == 1
        assert loaded[1].type == "model/completed"
        assert loaded[1].data == {"content": "hi"}

    def test_read_nonexistent_session_returns_empty(self, tmp_path: Path):
        store = JsonlSessionStore(root=tmp_path)
        assert store.read_events("never-existed") == []

    def test_half_line_is_skipped(self, tmp_path: Path):
        """崩溃安全：半行 JSON（写入被中断）读取时跳过，不影响其它行。"""
        store = JsonlSessionStore(root=tmp_path)
        sid = "crash-test"
        path = store._events_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 手写：一行完整 + 一行半截 + 一行完整
        good1 = SessionEvent(seq=0, type=SESSION_STARTED, session_id=sid)
        good2 = SessionEvent(seq=2, type=MODEL_COMPLETED, session_id=sid)

        import json

        with path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(good1.to_dict(), ensure_ascii=False) + "\n")
            fh.write('{"seq": 1, "type": "model/completed", "session_i')  # 半行，无换行
            fh.write("\n")
            fh.write(json.dumps(good2.to_dict(), ensure_ascii=False) + "\n")

        loaded = store.read_events(sid)
        assert len(loaded) == 2  # 半行被跳过
        assert loaded[0].seq == 0
        assert loaded[1].seq == 2

    def test_blank_lines_are_skipped(self, tmp_path: Path):
        store = JsonlSessionStore(root=tmp_path)
        sid = "blank-test"
        path = store._events_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)

        event = SessionEvent(seq=0, type=SESSION_STARTED, session_id=sid)
        import json

        with path.open("w", encoding="utf-8") as fh:
            fh.write("\n")  # 前导空行
            fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            fh.write("   \n")  # 空白行
            fh.write("\n")  # 尾随空行

        loaded = store.read_events(sid)
        assert len(loaded) == 1
        assert loaded[0].seq == 0

    def test_seq_ordering_preserved_as_written(self, tmp_path: Path):
        store = JsonlSessionStore(root=tmp_path)
        sid = "order-test"

        for seq in range(5):
            store.append_event(sid, SessionEvent(seq=seq, type=USER_MESSAGE if seq % 2 else SESSION_STARTED, session_id=sid))

        loaded = store.read_events(sid)
        assert [e.seq for e in loaded] == [0, 1, 2, 3, 4]

    def test_multiple_sessions_isolated(self, tmp_path: Path):
        store = JsonlSessionStore(root=tmp_path)

        store.append_event("s1", SessionEvent(seq=0, type=SESSION_STARTED, session_id="s1"))
        store.append_event("s2", SessionEvent(seq=0, type=SESSION_STARTED, session_id="s2"))
        store.append_event("s1", SessionEvent(seq=1, type=USER_MESSAGE, session_id="s1"))

        assert len(store.read_events("s1")) == 2
        assert len(store.read_events("s2")) == 1
