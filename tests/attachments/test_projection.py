"""#823 / MM-02：附件引用 → 投影的纯函数层。"""

from __future__ import annotations

from agent_harness.attachments.projection import (
    IMAGE_OMITTED_PLACEHOLDER,
    ImageRef,
    content_block_with_text,
    image_content_block,
    parse_image_refs,
    text_with_omitted_images,
)


def _ref_dict(**overrides):
    base = {
        "kind": "image",
        "attachment_id": "sha256:" + "a" * 64,
        "media_type": "image/png",
        "bytes": 123,
        "width": 640,
        "height": 480,
    }
    base.update(overrides)
    return base


def test_parse_reads_valid_refs_and_drops_bad_ones() -> None:
    raw = [
        _ref_dict(),
        _ref_dict(attachment_id="sha256:" + "b" * 64, name="cat.png"),
        # kind 不是 image → 跳过
        {**_ref_dict(), "kind": "file"},
        # 缺 id → 跳过
        {"kind": "image", "media_type": "image/png", "bytes": 1, "width": 1, "height": 1},
        # 负尺寸 → 跳过
        _ref_dict(width=-1),
        # bool 不是 int（True 是 int 的子类）→ 跳过
        _ref_dict(height=True),
        "not-a-dict",
    ]
    refs = parse_image_refs(raw)
    assert [r.attachment_id for r in refs] == [
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    ]
    assert refs[1].name == "cat.png"


def test_parse_non_list_is_empty() -> None:
    assert parse_image_refs(None) == []
    assert parse_image_refs({"a": 1}) == []
    assert parse_image_refs("") == []


def test_image_content_block_is_provider_neutral_standard_block() -> None:
    ref = ImageRef(
        attachment_id="sha256:" + "c" * 64,
        media_type="image/webp",
        bytes=10,
        width=2,
        height=3,
    )
    block = image_content_block(ref)
    # 标准块：带 file_id + mime_type + 尺寸元数据（#935 / M-03），绝不带 base64（AC3）。
    assert block == {
        "type": "image",
        "file_id": "sha256:" + "c" * 64,
        "mime_type": "image/webp",
        "width": 2,
        "height": 3,
    }
    assert "base64" not in block
    assert "data:" not in str(block)


def test_content_block_with_text_preserves_ref_order() -> None:
    refs = [
        ImageRef(attachment_id="sha256:" + "1" * 64, media_type="image/png", bytes=1, width=1, height=1),
        ImageRef(attachment_id="sha256:" + "2" * 64, media_type="image/jpeg", bytes=1, width=1, height=1),
    ]
    blocks = content_block_with_text("看这两张", refs)
    assert blocks[0] == {"type": "text", "text": "看这两张"}
    assert [b["file_id"] for b in blocks[1:]] == [
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
    ]


def test_text_with_omitted_images_marks_but_keeps_text() -> None:
    assert text_with_omitted_images("hi") == f"hi\n{IMAGE_OMITTED_PLACEHOLDER}"
    assert text_with_omitted_images("") == IMAGE_OMITTED_PLACEHOLDER
