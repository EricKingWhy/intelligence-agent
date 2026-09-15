"""ADR-0030（#196）T4：`message/superseded` 的投影剔除契约。

被取代的 user/message **连同它的整轮**（答、tool_call、tool_result）都必须从
模型可见投影里消失——这是"编辑问句 = 重写那一轮"的语义落点。它只影响投影
（不变量 #7：完整保存 ≠ 完整注入），从不改写历史事件（不变量 #3）。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent_harness.session import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    DANGLING_TOOL_CONTENT,
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
    derive_messages,
)
from agent_harness.session.event import MESSAGE_SUPERSEDED


def _user(seq: int, sid: str, content: str) -> SessionEvent:
    return SessionEvent(seq=seq, type=USER_MESSAGE, session_id=sid, data={"content": content})


def _model(seq: int, sid: str, content: str, tool_calls=None) -> SessionEvent:
    data: dict = {"content": content}
    if tool_calls:
        data["tool_calls"] = tool_calls
    return SessionEvent(seq=seq, type=MODEL_COMPLETED, session_id=sid, data=data)


def _tool_call(seq: int, sid: str, tc_id: str, name: str, args: dict) -> SessionEvent:
    return SessionEvent(
        seq=seq,
        type=TOOL_CALL,
        session_id=sid,
        data={"tool_call_id": tc_id, "tool_name": name, "args": args},
    )


def _tool_result(seq: int, sid: str, tc_id: str, content: str) -> SessionEvent:
    return SessionEvent(
        seq=seq,
        type=TOOL_RESULT,
        session_id=sid,
        data={"tool_call_id": tc_id, "content": content},
    )


def _superseded(seq: int, sid: str, superseded_seq: object, carrier: str = "edit") -> SessionEvent:
    return SessionEvent(
        seq=seq,
        type=MESSAGE_SUPERSEDED,
        session_id=sid,
        data={"superseded_seq": superseded_seq, "carrier": carrier},
    )


def _texts(messages) -> list[str]:
    return [str(m.content) for m in messages]


class TestSupersedeShadow:
    def test_drops_whole_round_keeps_new_question(self):
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A"),
            _user(2, "s1", "B"),
            _superseded(3, "s1", 0),
        ]
        messages = derive_messages(events)
        assert _texts(messages) == ["B"]
        assert isinstance(messages[0], HumanMessage)

    def test_round_with_tools_leaves_no_trace_and_no_synthetic_result(self, caplog):
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A", tool_calls=[{"id": "tc1", "name": "read", "args": {}}]),
            _tool_call(2, "s1", "tc1", "read", {}),
            _tool_result(3, "s1", "tc1", "文件内容"),
            _user(4, "s1", "B"),
            _superseded(5, "s1", 0),
        ]
        with caplog.at_level(logging.WARNING, logger="agent_harness.session.derive"):
            messages = derive_messages(events)
        assert _texts(messages) == ["B"]
        # 被取代轮里的 tool_call 不产生 dangling 合成 ToolMessage（顺序：先 shadow 后合成）
        assert not any(isinstance(m, AIMessage) for m in messages)
        assert not any(isinstance(m, ToolMessage) for m in messages)
        assert DANGLING_TOOL_CONTENT not in _texts(messages)

    def test_superseding_latest_turn_shadows_to_end(self):
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A"),
            _user(2, "s1", "B"),
            _model(3, "s1", "答B"),
            _superseded(4, "s1", 2),
        ]
        messages = derive_messages(events)
        assert _texts(messages) == ["A", "答A"]

    def test_two_superseded_questions_collapse_to_empty(self):
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A"),
            _user(2, "s1", "B"),
            _model(3, "s1", "答B"),
            _superseded(4, "s1", 0),
            _superseded(5, "s1", 2),
        ]
        assert derive_messages(events) == []

    def test_duplicate_supersede_event_is_idempotent(self):
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A"),
            _user(2, "s1", "B"),
            _superseded(3, "s1", 0),
            _superseded(4, "s1", 0, carrier="resend"),
        ]
        assert _texts(derive_messages(events)) == ["B"]

    def test_middle_turn_dropped_neighbours_kept(self):
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A"),
            _user(2, "s1", "B"),
            _model(3, "s1", "答B"),
            _user(4, "s1", "C"),
            _superseded(5, "s1", 2),
        ]
        assert _texts(derive_messages(events)) == ["A", "答A", "C"]


class TestSupersedeDegradation:
    def test_invalid_superseded_seq_ignores_event_with_warning(self, caplog):
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A"),
            _superseded(2, "s1", "0"),
        ]
        with caplog.at_level(logging.WARNING, logger="agent_harness.session.derive"):
            messages = derive_messages(events)
        assert _texts(messages) == ["A", "答A"]
        assert any("superseded_seq" in r.message for r in caplog.records)

    def test_unknown_anchor_does_not_shadow_anything(self):
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A"),
            _superseded(2, "s1", 99),
        ]
        assert _texts(derive_messages(events)) == ["A", "答A"]

    def test_no_supersede_event_leaves_projection_untouched(self):
        events = [_user(0, "s1", "A"), _model(1, "s1", "答A")]
        assert _texts(derive_messages(events)) == ["A", "答A"]


class TestSupersedeWithCompaction:
    def test_compaction_summary_still_emitted_alongside_supersede(self):
        """supersede 区间与 compaction 区间并存时 summary 不能被挤掉。

        回归锁：supersede 区间若与 `shadowed_ranges` 合并成一个列表，下面吐
        summary 的循环会用错位的下标索引 `bracket_summaries`。
        """
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A"),
            SessionEvent(
                seq=2, type=COMPACTION_START, session_id="s1",
                data={"source_seq_start": 0, "source_seq_end": 1},
            ),
            SessionEvent(
                seq=3, type=CONTEXT_COMPACTED, session_id="s1",
                data={"summary": "SUM", "compacted_turn_count": 1},
            ),
            SessionEvent(seq=4, type=COMPACTION_END, session_id="s1", data={}),
            _user(5, "s1", "B"),
            _model(6, "s1", "答B"),
            _superseded(7, "s1", 0),
        ]
        messages = derive_messages(events)
        assert isinstance(messages[0], SystemMessage)
        assert messages[0].content == "SUM"
        assert _texts(messages) == ["SUM", "B", "答B"]

    def test_second_bracket_summary_not_misplaced_by_supersede(self):
        """两个 bracket + 一个 supersede：两条 summary 各归其位、不丢不串。"""
        events = [
            _user(0, "s1", "A"),
            _model(1, "s1", "答A"),
            SessionEvent(
                seq=2, type=COMPACTION_START, session_id="s1",
                data={"source_seq_start": 0, "source_seq_end": 1},
            ),
            SessionEvent(
                seq=3, type=CONTEXT_COMPACTED, session_id="s1",
                data={"summary": "SUM1"},
            ),
            SessionEvent(seq=4, type=COMPACTION_END, session_id="s1", data={}),
            _user(5, "s1", "B"),
            _model(6, "s1", "答B"),
            SessionEvent(
                seq=7, type=COMPACTION_START, session_id="s1",
                data={"source_seq_start": 5, "source_seq_end": 6},
            ),
            SessionEvent(
                seq=8, type=CONTEXT_COMPACTED, session_id="s1",
                data={"summary": "SUM2"},
            ),
            SessionEvent(seq=9, type=COMPACTION_END, session_id="s1", data={}),
            _user(10, "s1", "C"),
            SessionEvent(
                seq=11, type=MESSAGE_SUPERSEDED, session_id="s1",
                data={"superseded_seq": 10, "carrier": "edit"},
            ),
        ]
        assert _texts(derive_messages(events)) == ["SUM1", "SUM2"]
