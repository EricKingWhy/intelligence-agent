"""#823 / MM-02：derive_messages 的附件物化 / 占位分支。"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER
from agent_harness.session import USER_MESSAGE, SessionEvent
from agent_harness.session.derive import (
    derive_messages,
    derive_messages_with_source_ranges,
    referenced_attachment_ids,
)

_REF = {
    "kind": "image",
    "attachment_id": "sha256:" + "a" * 64,
    "media_type": "image/png",
    "bytes": 1024,
    "width": 640,
    "height": 480,
}


def _user(seq: int, data: dict) -> SessionEvent:
    return SessionEvent(seq=seq, type=USER_MESSAGE, session_id="s1", data=data)


def test_text_only_message_is_byte_identical() -> None:
    events = [_user(0, {"content": "纯文本"})]
    (message, _range), = derive_messages_with_source_ranges(events)
    assert isinstance(message, HumanMessage)
    assert message.content == "纯文本"  # 逐字不变（AC8）
    assert isinstance(message.content, str)


def test_vision_materializes_image_block_without_base64() -> None:
    events = [_user(0, {"content": "看图", "attachments": [_REF]})]
    (message, _range), = derive_messages_with_source_ranges(events, supports_vision=True)
    assert message.content == [
        {"type": "text", "text": "看图"},
        # #935 / M-03：标准块随块携带尺寸（token 估算按尺寸相关公式计费用）。
        {
            "type": "image",
            "file_id": _REF["attachment_id"],
            "mime_type": "image/png",
            "width": 640,
            "height": 480,
        },
    ]
    # 事件流 / 投影里绝不出现 base64（AC3）。
    assert "base64" not in str(message.content)


def test_non_vision_appends_placeholder() -> None:
    events = [_user(0, {"content": "看图", "attachments": [_REF]})]
    (message, _range), = derive_messages_with_source_ranges(events, supports_vision=False)
    assert message.content == f"看图\n{IMAGE_OMITTED_PLACEHOLDER}"


def test_default_derive_is_non_vision() -> None:
    events = [_user(0, {"content": "看图", "attachments": [_REF]})]
    (message,) = derive_messages(events)
    assert message.content == f"看图\n{IMAGE_OMITTED_PLACEHOLDER}"


def test_bad_attachment_entries_are_ignored() -> None:
    events = [_user(0, {"content": "x", "attachments": ["nope", {"kind": "file"}]})]
    (message,) = derive_messages(events)
    assert message.content == "x"  # 无可解析引用 ⇒ 文本逐字不变


def test_referenced_attachment_ids_scans_user_messages() -> None:
    other = {**_REF, "attachment_id": "sha256:" + "b" * 64}
    events = [
        _user(0, {"content": "a", "attachments": [_REF]}),
        SessionEvent(seq=1, type="model/completed", session_id="s1", data={"content": "ok"}),
        _user(2, {"content": "b", "attachments": [other]}),
    ]
    assert referenced_attachment_ids(events) == {
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    }


def test_referenced_attachment_ids_empty_for_plain_text() -> None:
    assert referenced_attachment_ids([_user(0, {"content": "hi"})]) == set()


def test_undelivered_inputs_carries_attachments() -> None:
    """排队/steer 的附件引用不静默丢失（投递时随新 user/message 带出）。"""
    from agent_harness.session.derive import undelivered_inputs
    from agent_harness.session.event import MESSAGE_QUEUED

    event = SessionEvent(
        seq=0,
        type=MESSAGE_QUEUED,
        session_id="s1",
        data={"queue_id": "q1", "content": "看图", "attachments": [_REF]},
    )
    (item,) = undelivered_inputs([event])
    assert item.attachments is not None
    assert item.attachments[0]["attachment_id"] == _REF["attachment_id"]
    assert item.attachments[0]["width"] == 640


def _image_session() -> list[SessionEvent]:
    return [
        _user(0, {"content": "旧消息"}),
        SessionEvent(seq=1, type="model/completed", session_id="s1", data={"content": "答"}),
        _user(2, {"content": "看图", "attachments": [_REF]}),
    ]


def test_is_direct_user_input_event_accepts_image_message() -> None:
    """A7：带图 user 消息仍是"活跃直接用户输入"（视觉/非视觉投影都要选中）。"""
    from agent_harness.session.derive import is_direct_user_input_event

    events = _image_session()
    target = next(e for e in events if e.seq == 2)
    assert is_direct_user_input_event(events, target.event_id) is True


def test_latest_direct_user_input_selects_image_message_vision() -> None:
    """A7：最新一条带图用户消息（视觉投影为块列表）必须被选中，不回落到旧文本。"""
    from agent_harness.session.derive import latest_direct_user_input_event

    events = _image_session()
    model_messages = derive_messages(events, supports_vision=True)
    selected = latest_direct_user_input_event(events, model_messages)
    assert selected is not None
    assert selected.seq == 2


def test_latest_direct_user_input_selects_image_message_non_vision() -> None:
    """A7：非视觉投影（原文 + 占位符后缀）同样必须选中带图消息。"""
    from agent_harness.session.derive import latest_direct_user_input_event

    events = _image_session()
    model_messages = derive_messages(events, supports_vision=False)
    selected = latest_direct_user_input_event(events, model_messages)
    assert selected is not None
    assert selected.seq == 2


def test_latest_direct_user_input_plain_text_still_selected() -> None:
    """A7 回归：纯文本最新用户消息的选择行为不变。"""
    from agent_harness.session.derive import latest_direct_user_input_event

    events = [
        SessionEvent(seq=0, type=USER_MESSAGE, session_id="s1", data={"content": "旧"}),
        SessionEvent(seq=1, type="model/completed", session_id="s1", data={"content": "答"}),
        SessionEvent(seq=2, type=USER_MESSAGE, session_id="s1", data={"content": "新问题"}),
    ]
    selected = latest_direct_user_input_event(events, derive_messages(events))
    assert selected is not None and selected.seq == 2


def test_projected_user_text_mis_strips_trailing_placeholder_from_plain_text() -> None:
    """P4 负例：`_projected_user_text` 的逆映射是启发式（#823 / MM-02 重审 P4）。

    非视觉下带图 user 消息的投影是 `原文 + "\\n" + 占位符`；还原事件原文只能靠
    **后缀剥离**。这带来一个已知局限：一条**纯文本**消息若恰好以该占位符文案结尾，
    也会被误剥离——两种形态在 `HumanMessage.content` 层面不可区分（附件标记只存在于
    事件里，投影后已丢失），无法在不改投影契约的前提下消除。

    影响面极小（概率极低，且仅影响"来源约束选择"），故**如实登记为已知局限**并钉住
    当前行为：本用例红 = 有人改了后缀启发式，需重新评估该局限是否仍成立。
    """
    from agent_harness.session.derive import _projected_user_text

    plain = f"人类在讨论这个占位符文案\n{IMAGE_OMITTED_PLACEHOLDER}"
    assert _projected_user_text(HumanMessage(content=plain)) == "人类在讨论这个占位符文案"
    # 恰好等于占位符本身（无前缀文本）亦被剥成空串——同源边界。
    assert _projected_user_text(HumanMessage(content=IMAGE_OMITTED_PLACEHOLDER)) == ""
