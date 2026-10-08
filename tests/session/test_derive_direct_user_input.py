"""#823 MM-02 × #663 合并判据：`is_direct_user_input_event` 的组合语义回归。

pre-merge sync（#823 × #663）在 `is_direct_user_input_event` 尾部发生过语义冲突：
本线（#823 / MM-02 A7）的**投影一致比对**（图片感知 `_projected_user_text`）与 main
侧（#663 P2 / P3-1）的**作废标记负向闸门**（`live_supersede_markers`，含 #614① 空替换
槽规则）必须并存。二者**取并**而非取交——把投影比对当硬闸门，会让压缩后的原文事件被
误判成"非直接输入"（正是 #663 P2 要修的 bug）。

本文件钉住合并后的行为：带图消息（两侧各自的判别力）在视觉 / 非视觉投影下都是直接
输入；作废标记是唯一的负向闸门；压缩后原文事件仍判 True。带 "AND 会分叉" 注释的用例
是合并组合的判别性场景。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER
from agent_harness.session import USER_MESSAGE, SessionEvent
from agent_harness.session.derive import (
    derive_messages,
    derive_messages_with_source_ranges,
    is_direct_user_input_event,
)
from agent_harness.session.event import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    MESSAGE_QUEUED,
    MESSAGE_SUPERSEDED,
    QUEUE_CANCELLED,
)

_REF = {
    "kind": "image",
    "attachment_id": "sha256:" + "a" * 64,
    "media_type": "image/png",
    "bytes": 1024,
    "width": 640,
    "height": 480,
}


def _user(seq: int, content: str, **data) -> SessionEvent:
    return SessionEvent(
        seq=seq, type=USER_MESSAGE, session_id="s1",
        data={"content": content, **data},
    )


def _bracket(seq: int, start: int, end: int, summary: str = "compaction summary"):
    """一条完整落盘的 compaction bracket，覆盖 ``[start, end]``。"""
    return [
        SessionEvent(
            seq=seq, type=COMPACTION_START, session_id="s1",
            data={"bracket_id": "b1", "source_seq_start": start,
                  "source_seq_end": end},
        ),
        SessionEvent(
            seq=seq + 1, type=CONTEXT_COMPACTED, session_id="s1",
            data={"bracket_id": "b1", "source_seq_start": start,
                  "source_seq_end": end, "summary": summary},
        ),
        SessionEvent(
            seq=seq + 2, type=COMPACTION_END, session_id="s1",
            data={"bracket_id": "b1"},
        ),
    ]


def test_image_message_is_direct_input_vision_and_non_vision() -> None:
    """#823 / MM-02 A7：带图 user 消息在两种投影下都是活跃直接输入。

    视觉投影是块列表、非视觉投影是"原文 + 占位符后缀"，都不是事件原始 content；
    合并前本线靠 `_projected_user_text` 还原。main 侧的事件事实判据（无作废标记）
    同样判 True——两者在此不分叉。
    """
    events = [_user(2, "看图", attachments=[_REF])]
    msg_vision, = derive_messages(events, supports_vision=True)
    msg_non_vision, = derive_messages(events)
    assert msg_vision.content != "看图"  # 块列表
    assert msg_non_vision.content == f"看图\n{IMAGE_OMITTED_PLACEHOLDER}"

    assert is_direct_user_input_event(events, events[0].event_id) is True


def test_compacted_source_event_still_direct_input() -> None:
    """#663 P2：压缩后原文事件仍是直接输入——投影比对只作正向、不作拒绝依据。

    **AND 会分叉**：单事件 bracket 的 summary 投影来源范围恰好也是 `(seq, seq)`
    且内容对不上，把投影比对当硬闸门会在这里判 False（即 #663 P2 的 bug）。
    合并后取并 ⇒ True。
    """
    source = _user(1, "本次修改不能新增第三方依赖。")
    events = [source, *_bracket(2, 1, 1)]

    # 投影里只剩 summary（来源范围恰为 (1, 1)），原文消息不在。
    projected = derive_messages_with_source_ranges(events)
    assert [(m.content, r) for m, r in projected] == [("compaction summary", (1, 1))]

    assert is_direct_user_input_event(events, source.event_id) is True


def test_compacted_image_message_still_direct_input() -> None:
    """合并交叉场景：带图消息 **且** 被压缩 ⇒ 仍是直接输入。

    #823（带图）与 #663（压缩免疫）两个语义在同一条事件上叠加。AND 同样会在这里
    分叉（投影是摘要、对不上）；取并 ⇒ True。
    """
    source = _user(1, "看图", attachments=[_REF])
    events = [source, *_bracket(2, 1, 1)]

    assert is_direct_user_input_event(events, source.event_id) is True


def test_supersede_with_live_replacement_retires_the_target() -> None:
    """#663 P3-1：替换槽真被填上 ⇒ supersede 生效，目标不再是来源（负向闸门）。"""
    old = _user(1, "Use tabs.")
    replacement = _user(2, "Use spaces instead.")
    marker = SessionEvent(
        seq=3, type=MESSAGE_SUPERSEDED, session_id="s1",
        data={"superseded_seq": 1},
    )

    assert not is_direct_user_input_event([old, replacement, marker], old.event_id)
    assert is_direct_user_input_event([old, replacement, marker], replacement.event_id)


def test_supersede_with_empty_replacement_slot_keeps_source_active() -> None:
    """#614①：替换排队项被取消 ⇒ 替换槽空、supersede 未发生，目标仍是来源。

    **AND 会分叉**：目标被纯解析的 `superseded_event_seqs` shadow，投影比对判 False；
    合并后负向闸门只认 `live_supersede_markers`（空槽不算），取并 ⇒ True。
    """
    old = _user(1, "Use tabs.")
    queued = SessionEvent(
        seq=2, type=MESSAGE_QUEUED, session_id="s1",
        data={"content": "Use spaces", "queue_id": "q9"},
    )
    marker = SessionEvent(
        seq=3, type=MESSAGE_SUPERSEDED, session_id="s1",
        data={"superseded_seq": 1},
    )
    cancel = SessionEvent(
        seq=4, type=QUEUE_CANCELLED, session_id="s1",
        data={"queue_id": "q9"},
    )

    assert is_direct_user_input_event([old, queued, marker, cancel], old.event_id)


def test_superseded_image_message_is_not_direct_input() -> None:
    """带图 + 作废：被取代的带图消息两条路径同判 False（负向闸门优先）。"""
    old = _user(1, "看图", attachments=[_REF])
    replacement = _user(2, "换一张")
    marker = SessionEvent(
        seq=3, type=MESSAGE_SUPERSEDED, session_id="s1",
        data={"superseded_seq": 1},
    )

    assert not is_direct_user_input_event([old, replacement, marker], old.event_id)
    assert is_direct_user_input_event([old, replacement, marker], replacement.event_id)


def test_plain_text_message_selection_is_byte_identical() -> None:
    """AC8 回归：无附件消息的投影逐字不变，判据行为不变。"""
    events = [_user(1, "回答用中文")]
    (message, source_range), = derive_messages_with_source_ranges(events)
    assert isinstance(message, HumanMessage)
    assert message.content == "回答用中文"
    assert source_range == (1, 1)
    assert is_direct_user_input_event(events, events[0].event_id) is True
