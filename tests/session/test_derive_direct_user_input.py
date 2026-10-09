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
    COMPACTION_SUMMARY_MESSAGE_NAME,
    derive_messages,
    derive_messages_with_source_ranges,
    is_direct_user_input_event,
    latest_direct_user_input_event,
)
from agent_harness.session.event import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    MESSAGE_QUEUED,
    MESSAGE_SUPERSEDED,
    QUEUE_CANCELLED,
)

#: 澄清答复的正文与载荷（取值与真生产点 `session/service.py::_constraint_input_answer_data`
#: 的 `current_task_only` 分支逐字一致）。
_ANSWER_TEXT = "用户选择仅在当前任务采用此约束：For this task, use TypeScript."
_ANSWER_DATA = {
    "input_request_id": "request-1",
    "input_request_answer": {"request_id": "request-1", "choice": "current_task_only"},
}

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


def test_superseded_event_with_matching_summary_is_not_direct_input() -> None:
    """C2（#823 窄审查）：OR 的投影项不得让已撤回的消息复活。

    构造：一条 live-supersede 的事件 s（替换槽被填满 ⇒ 目标真被撤回），同时落在一个
    **以 s 为起点**的单事件 compaction bracket 内，且该 bracket 的 summary 文本**恰好
    等于** s 的原始 content。此时投影在 `(s, s)` 处出现该 summary，
    `_projected_user_text(summary) == content` ⇒ 合并前的 OR 对一条已撤回消息返回 True，
    与 #663 单边（只用 `live_supersede_markers`，判 False）分叉。

    修法：投影项排除 compaction summary（`message.name ==
    COMPACTION_SUMMARY_MESSAGE_NAME`）。合并后返回 False，与 main 单边一致。
    """
    content = "本题只用标准库"
    old = _user(1, content)
    replacement = _user(2, "换一条")
    marker = SessionEvent(
        seq=3, type=MESSAGE_SUPERSEDED, session_id="s1",
        data={"superseded_seq": 1},
    )
    events = [old, replacement, marker, *_bracket(4, 1, 1, summary=content)]

    # 投影在 (1, 1) 处正是那条与原文逐字相同的 summary——这是巧合命中的构造。
    projected = derive_messages_with_source_ranges(events)
    assert (projected[0][0].content, projected[0][1]) == (content, (1, 1))

    # live-supersede 生效：目标已撤回，投影项不得把它拉回来。
    assert not is_direct_user_input_event(events, old.event_id)
    # 替换槽里的新消息仍是活跃来源。
    assert is_direct_user_input_event(events, replacement.event_id)


def test_superseded_event_with_matching_summary_is_not_latest_input() -> None:
    """C2 同源（#862）：`latest_direct_user_input_event` 也不得让已作废消息复活。

    构造与 C2 同源：一条 live-supersede 的事件 s（替换槽被填满 ⇒ 目标真被撤回），同时落在
    一个**以 s 为起点**的单事件 compaction bracket 内，且该 bracket 的 summary 文本**恰好
    等于** s 的原始 content。此时投影在 `(s, s)` 处正是这条 summary，`_projected_user_text`
    还原出的文本 == s 的 content ⇒ 未修复前 `latest_direct_user_input_event` 把一条**已撤回**
    的消息选为最新直接用户输入（它只从消息投影取源，`_projected_user_text` 又还原回原文）。

    替换槽用**排队项**（`message/queued`，`live_supersede_markers` 认可的可达形态）而非一条新的
    `user/message`：后者的 seq 更高、会先成为候选并胜出 `max(...)`，反而**掩盖**这条已作废消息
    被误选的事实。此构造下模型可见投影里唯一的 `(s, s)` 来源就是那条 summary。

    修法：`candidates` 推导排除 compaction summary（`message.name ==
    COMPACTION_SUMMARY_MESSAGE_NAME`）。修复后候选集为空 ⇒ 返回 None，与
    `is_direct_user_input_event`（C2 收紧后判 False）同口径。
    """
    content = "本题只用标准库"
    old = _user(1, content)
    queued = SessionEvent(
        seq=2, type=MESSAGE_QUEUED, session_id="s1",
        data={"content": "换一条", "queue_id": "q9"},
    )
    marker = SessionEvent(
        seq=3, type=MESSAGE_SUPERSEDED, session_id="s1",
        data={"superseded_seq": 1},
    )
    events = [old, queued, marker, *_bracket(4, 1, 1, summary=content)]

    # 模型可见投影在 (1, 1) 处正是那条与原文逐字相同的 summary——这是巧合命中的构造。
    model_messages = derive_messages(events)
    (message, source_range), = derive_messages_with_source_ranges(events)
    assert (message.content, source_range) == (content, (1, 1))
    assert message.name == COMPACTION_SUMMARY_MESSAGE_NAME

    # live-supersede 生效：目标已撤回。投影里那条 summary 不是用户原话，
    # 不得被还原成"最新直接用户输入"⇒ 无活跃直接输入。
    assert latest_direct_user_input_event(events, model_messages) is None


def test_plain_text_message_selection_is_byte_identical() -> None:
    """AC8 回归：无附件消息的投影逐字不变，判据行为不变。"""
    events = [_user(1, "回答用中文")]
    (message, source_range), = derive_messages_with_source_ranges(events)
    assert isinstance(message, HumanMessage)
    assert message.content == "回答用中文"
    assert source_range == (1, 1)
    assert is_direct_user_input_event(events, events[0].event_id) is True


def test_replace_summary_stand_in_within_bracket_is_not_latest_input() -> None:
    """P3-1（#911）：`user/message(replace)` 替身一旦落盘就会被投影，不得被选为最新输入。

    `session/event.py` 登记过 4-event bracket 形状（``COMPACTION_START → CONTEXT_COMPACTED
    → USER_MESSAGE(replace) → COMPACTION_END``），但**现行写入路径不落这条替身**
    （`context/builder.py` 只写 START / CONTEXT_COMPACTED / END），全仓 `src/` 与全部
    git 历史都无 `replace=True` 生产点。因此"不可达"成立，但**理由不是它被 shadow**：
    投影只 shadow `source_seq_start..source_seq_end` 区间，而替身写在 SUMMARY 之后、
    区间之外 ⇒ 只要有这条事件就会被投影成 `(seq, seq)`（`_bracket` 已钉住这一点）。

    本条用例证明的正是"**若**历史上出现过该形状（旧日志 / 外部导入），闸门会把它选为
    最新直接用户输入"——其 content 与自身逐字相等，天然满足候选条件；而
    `is_direct_user_input_event`（含 `event.data.get("replace")` 排除）判 False ⇒ 两侧分叉。

    修法：#862 同源——候选闸门补 `event.data.get("replace")` 排除，使两处口径一致。
    """
    summary = "摘要：用户要求不要新增依赖"
    real = _user(1, "本题只用标准库")
    # 替身事件：`replace=True`（compaction 摘要替身的登记形状）。投影只 shadow bracket 的
    # source 区间 [1, 1]，替身的 seq=2 在区间之外 ⇒ 它会被投影成 (2, 2)。
    stand_in = SessionEvent(
        seq=2, type=USER_MESSAGE, session_id="s1",
        data={"content": summary, "replace": True},
    )
    events = [real, stand_in, *_bracket(3, 1, 1, summary=summary)]

    messages = derive_messages(events)
    projected = derive_messages_with_source_ranges(events)
    # 替身确实被投影成 (2, 2)，不是 compaction summary 命名的消息。
    assert (stand_in.data["content"], (2, 2)) in [
        (message.content, source_range) for message, source_range in projected
    ]
    assert not any(
        getattr(message, "name", None) == COMPACTION_SUMMARY_MESSAGE_NAME
        and source_range == (2, 2)
        for message, source_range in projected
    )

    # 分叉证据：替换替身不是用户原话（事件口径判 False）。
    assert not is_direct_user_input_event(events, stand_in.event_id)
    # 修复后：替身被排除；原文的 (1,1) 投影项是 summary 命名消息（C2 排除）、替身在 (2,2) 被
    # 排除 ⇒ **无候选**（锚定态，非"或原文"——原文早已被 bracket 换成 summary 投影）。
    selected = latest_direct_user_input_event(events, messages)
    assert selected is None


def test_replace_stand_in_does_not_resurrect_superseded_target() -> None:
    """P3-1（#911）残口的最坏后果：替身会让一条**已被撤回**的消息"复活"。

    与 #862 C2 同源的污染形状，但命中路径不同：#862 修的是**投影项被 summary 命名**的
    分支（`message.name == COMPACTION_SUMMARY_MESSAGE_NAME`）；本条走的是
    `replace=True` 的普通 `USER_MESSAGE` 替身——它不是 summary 命名，C2 收紧拦不住它。

    构造：s=1 被 live-supersede（替换槽 seq=2 被填满），替身 seq=3 与 summary 命名无关、
    其 content 恰等于 s 的 content。未修复前闸门把它选为"最新直接用户输入"，等价于对一条
    已撤回的消息给出"当前有效约束来源"，与 `is_direct_user_input_event`（判 False）分叉。
    """
    content = "本题只用标准库"
    old = _user(1, content)
    replacement = _user(2, "换一条")
    marker = SessionEvent(
        seq=3, type=MESSAGE_SUPERSEDED, session_id="s1",
        data={"superseded_seq": 1},
    )
    stand_in = SessionEvent(
        seq=5, type=USER_MESSAGE, session_id="s1",
        data={"content": content, "replace": True},
    )
    events = [old, replacement, marker, *_bracket(6, 1, 1, summary=content), stand_in]

    messages = derive_messages(events)
    assert not is_direct_user_input_event(events, stand_in.event_id)
    # 修复后替身被排除：落回替换槽里那条**真实**的新消息，两函数同口径判 True。
    selected = latest_direct_user_input_event(events, messages)
    assert selected is not None and selected.event_id == replacement.event_id
    assert is_direct_user_input_event(events, selected.event_id)


def test_input_request_answer_with_later_message_edited_is_not_latest_input() -> None:
    """P3-1（#911）：澄清答复在"后续消息被编辑取代"后不得被选为最新直接输入。

    票面的推理（"候选闸门要求投影文本 == 事件原始 content，而澄清答复的投影即其原文 ⇒
    真实路径下两处口径一致"）只在**答复就是最后一条 user 事件**时成立——那种形状由函数
    开头的 `latest_direct_message` 早退守卫（返回 None）挡住。**一旦答复之后还有 user
    事件**，早退守卫不触发，而那条后续消息若被 `message/superseded` 取代（投影里整轮被
    shadow），投影中 seq 最高的可见 `HumanMessage` 就是答复本身，其文本与自身 content
    逐字相等 ⇒ 未修复前本闸门选中**澄清答复**，而 `is_direct_user_input_event` 恒判
    False ⇒ 两处口径分叉。

    形状全部来自真生产点：答复由 `session/service.py::_constraint_input_answer_data` 落盘；
    后续消息与 `message/queued` / `message/superseded` 由续聊/编辑路径落盘
    （`_queue_incoming_message` / `SendMessageRequest.supersedes_seq`）。本用例直接钉住
    "两函数同口径"：选中的事件必须判 True。
    """
    original = _user(1, "The previous rule may not fit: For this task, use TypeScript.")
    answer = _user(2, _ANSWER_TEXT, **_ANSWER_DATA)
    later = _user(3, "再来一件事")
    replacement_slot = SessionEvent(
        seq=4, type=MESSAGE_QUEUED, session_id="s1",
        data={"content": "改成别的事", "queue_id": "q1"},
    )
    marker = SessionEvent(
        seq=5, type=MESSAGE_SUPERSEDED, session_id="s1",
        data={"superseded_seq": 3},
    )
    events = [original, answer, later, replacement_slot, marker]

    messages = derive_messages(events)
    assert not is_direct_user_input_event(events, answer.event_id)
    selected = latest_direct_user_input_event(events, messages)
    assert selected is not None and selected.event_id == original.event_id
    assert is_direct_user_input_event(events, selected.event_id)


def test_input_request_answer_with_later_message_shadowed_is_not_latest_input() -> None:
    """同前，但替换槽为空（#614①）：后续消息仍被**纯解析**的投影 shadow。

    `message/superseded` 没有合法替换槽 ⇒ `live_supersede_markers` 不算这次取代发生
    （事件口径下 seq=3 仍判 True），可消息投影用的 `superseded_event_seqs` 是纯解析的
    ⇒ seq=3 照旧不进投影。于是投影里最新的可见 user 消息仍是澄清答复：未修复前本闸门
    选它、事件口径判 False ⇒ 分叉。修复后落回最新一条**可见**的直接输入（seq=1），
    与 `is_direct_user_input_event` 同口径。
    """
    original = _user(1, "The previous rule may not fit: For this task, use TypeScript.")
    answer = _user(2, _ANSWER_TEXT, **_ANSWER_DATA)
    later = _user(3, "再来一件事")
    marker = SessionEvent(
        seq=5, type=MESSAGE_SUPERSEDED, session_id="s1",
        data={"superseded_seq": 3},
    )
    events = [original, answer, later, marker]

    messages = derive_messages(events)
    assert not is_direct_user_input_event(events, answer.event_id)
    selected = latest_direct_user_input_event(events, messages)
    assert selected is not None and selected.event_id == original.event_id
    assert is_direct_user_input_event(events, selected.event_id)
