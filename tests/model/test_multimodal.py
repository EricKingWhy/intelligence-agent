"""#823 / MM-02：请求装配 adapter（标准块 → OpenAI image_url 载荷）。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_harness.attachments.projection import (
    IMAGE_OMITTED_PLACEHOLDER,
    content_block_with_text,
    text_with_omitted_images,
)
from agent_harness.attachments.types import ImageAttachmentRef
from agent_harness.model.multimodal import (
    DEFAULT_IMAGE_DETAIL,
    downgrade_to_non_vision,
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


class TestDowngradeToNonVision:
    """#823 / MM-02 A2 残口：切换当步重试的非视觉降级（纯消息变换）。"""

    def test_openai_image_url_block_is_reduced_to_placeholder_text(self) -> None:
        """已装配的 provider 载荷（`image_url`）→ 非视觉文本形态。"""
        message = HumanMessage(content=[
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ])
        out = downgrade_to_non_vision([message])
        assert out[0].content == f"看图\n{IMAGE_OMITTED_PLACEHOLDER}"

    def test_matches_non_vision_derive_projection(self) -> None:
        """降级产物与 `derive_messages(supports_vision=False)` 逐字一致。"""
        ref = ImageAttachmentRef(
            attachment_id="sha256:" + "a" * 64, media_type="image/png",
            bytes=1, width=1, height=1,
        )
        vision_msg = HumanMessage(content=content_block_with_text("看图", [ref]))
        out = downgrade_to_non_vision([vision_msg])
        assert out[0].content == text_with_omitted_images("看图")

    def test_matches_real_derive_non_vision_output_multi_image(self) -> None:
        """与**真实** `derive_messages` 的两口径产物对拍（含多图：都塌成单个占位符）。"""
        from agent_harness.session import USER_MESSAGE, SessionEvent
        from agent_harness.session.derive import derive_messages

        events = [SessionEvent(
            seq=0, type=USER_MESSAGE, session_id="s", data={
                "content": "看图",
                "attachments": [
                    {"kind": "image", "attachment_id": "sha256:" + "a" * 64,
                     "media_type": "image/png", "bytes": 1, "width": 1, "height": 1},
                    {"kind": "image", "attachment_id": "sha256:" + "b" * 64,
                     "media_type": "image/jpeg", "bytes": 2, "width": 2, "height": 2},
                ],
            },
        )]
        vision = derive_messages(events, supports_vision=True)
        non_vision = derive_messages(events, supports_vision=False)
        out = downgrade_to_non_vision(vision)
        assert out[0].content == non_vision[0].content
        assert out[0].content == f"看图\n{IMAGE_OMITTED_PLACEHOLDER}"

    def test_standard_image_block_is_also_handled(self) -> None:
        """投影后、装配前的标准块（`image`）同样降级（防御性，形态无关）。"""
        message = HumanMessage(content=[
            {"type": "text", "text": "看图"},
            {"type": "image", "file_id": "sha256:" + "a" * 64, "mime_type": "image/png"},
        ])
        out = downgrade_to_non_vision([message])
        assert out[0].content == f"看图\n{IMAGE_OMITTED_PLACEHOLDER}"

    def test_string_and_text_only_messages_are_untouched(self) -> None:
        """无图消息逐字不变（AC8）——对象也不重建。"""
        plain = HumanMessage(content="只有文字")
        text_only = HumanMessage(content=[{"type": "text", "text": "keep"}])
        ai = AIMessage(content="answer")
        out = downgrade_to_non_vision([plain, text_only, ai])
        assert out[0] is plain
        assert out[1] is text_only
        assert out[2] is ai

    def test_empty_text_with_image_yields_bare_placeholder(self) -> None:
        message = HumanMessage(content=[
            {"type": "text", "text": ""},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ])
        out = downgrade_to_non_vision([message])
        assert out[0].content == IMAGE_OMITTED_PLACEHOLDER
