"""T-529-4：skill/registered | updated | removed 事件词汇（#529 §6.2）。

三条 durable 事件对标 memory/audit.py 的记忆留痕先例：载荷含 skill name、
来源 run id、lint 结果摘要、确认者——append-only，不改历史（§8 #3）。
"""

from __future__ import annotations

from agent_harness.session.event import (
    EVENT_TYPES,
    SKILL_REGISTERED,
    SKILL_REMOVED,
    SKILL_UPDATED,
    STREAM_ONLY_TYPES,
    SessionEvent,
)


def test_skill_events_are_durable_vocabulary():
    """三条 skill 事件进 EVENT_TYPES（durable），绝不落 STREAM_ONLY。"""
    for event_type in (SKILL_REGISTERED, SKILL_UPDATED, SKILL_REMOVED):
        assert event_type in EVENT_TYPES
        assert event_type not in STREAM_ONLY_TYPES


def test_skill_event_values():
    assert SKILL_REGISTERED == "skill/registered"
    assert SKILL_UPDATED == "skill/updated"
    assert SKILL_REMOVED == "skill/removed"


def test_skill_registered_event_roundtrip_with_lint_summary_and_confirmer():
    """事件落盘（to_dict）含 run id + lint 摘要 + 确认者；from_dict 往返保真。"""
    event = SessionEvent(
        seq=42,
        type=SKILL_REGISTERED,
        session_id="s1",
        run_id="run-7",  # 信封 run id：本次沉淀发生在哪个 run
        data={
            "name": "pdf-export",  # skill name
            "source_run_id": "run-7",  # 来源 run id（经验出处）
            "lint": {"errors": [], "warnings": ["body uses uppercase 'ALWAYS'"]},  # lint 摘要
            "confirmed_by": "user:wang",  # 确认者（人审门，§5.2）
        },
    )
    raw = event.to_dict()
    assert raw["run_id"] == "run-7"
    assert raw["data"]["lint"]["errors"] == []
    assert raw["data"]["confirmed_by"] == "user:wang"

    reconstructed = SessionEvent.from_dict(raw)
    assert reconstructed.type == SKILL_REGISTERED
    assert reconstructed.run_id == "run-7"
    assert reconstructed.data == event.data


def test_skill_removed_event_roundtrip():
    event = SessionEvent(
        seq=43,
        type=SKILL_REMOVED,
        session_id="s1",
        data={"name": "pdf-export", "confirmed_by": "user:wang"},
    )
    reconstructed = SessionEvent.from_dict(event.to_dict())
    assert reconstructed.type == "skill/removed"
    assert reconstructed.data["name"] == "pdf-export"
