"""#823 / MM-02：请求装配 adapter（标准块 → OpenAI image_url 载荷）。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER
from agent_harness.model.multimodal import (
    DEFAULT_IMAGE_DETAIL,
    image_data_url,
    to_provider_messages,
)


def _split_messages(messages):
    return [m for m in messages if isinstance(m, HumanMessage)]


def test_string_content_messages_are_untouched() -> None:
    messages = [SystemMessage(content="sys"), HumanMessage(content="hello")]
    out = to_provider_messages(messages, resolve_image=lambda _fid: None)
    # 无块列表的消息必须原对象返回（无图行为逐字不变，AC8）。
    assert out[0] is messages[0]
    assert out[1] is messages[1]


def test_standard_image_block_becomes_openai_image_url() -> None:
    message = HumanMessage(
        content=[
            {"type": "text", "text": "看这张图"},
            {"type": "image", "file_id": "sha256:" + "a" * 64, "mime_type": "image/png"},
        ]
    )
    out = to_provider_messages(
        [message],
        resolve_image=lambda fid: ("image/png", "QUJD") if fid.endswith("a" * 64) else None,
    )
    blocks = out[0].content
    assert blocks[0] == {"type": "text", "text": "看这张图"}
    assert blocks[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,QUJD", "detail": DEFAULT_IMAGE_DETAIL},
    }


def test_detail_is_configurable() -> None:
    message = HumanMessage(
        content=[{"type": "image", "file_id": "sha256:" + "b" * 64, "mime_type": "image/webp"}]
    )
    out = to_provider_messages(
        [message],
        resolve_image=lambda _fid: ("image/webp", "QUJD"),
        detail="high",
    )
    assert out[0].content[0]["image_url"]["detail"] == "high"


def test_unresolvable_image_becomes_placeholder_not_dropped() -> None:
    message = HumanMessage(
        content=[{"type": "image", "file_id": "sha256:" + "c" * 64, "mime_type": "image/png"}]
    )
    out = to_provider_messages([message], resolve_image=lambda _fid: None)
    assert out[0].content == [{"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER}]


def test_non_image_blocks_pass_through() -> None:
    message = HumanMessage(content=[{"type": "text", "text": "keep me"}])
    out = to_provider_messages([message], resolve_image=lambda _fid: None)
    assert out[0].content == [{"type": "text", "text": "keep me"}]


def test_ai_message_with_string_content_untouched() -> None:
    ai = AIMessage(content="answer")
    out = to_provider_messages([ai], resolve_image=lambda _fid: None)
    assert out[0] is ai


def test_image_data_url_shape() -> None:
    assert image_data_url("image/jpeg", "ZZZ") == "data:image/jpeg;base64,ZZZ"
