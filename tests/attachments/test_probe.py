"""`attachments.probe` 单元测试：按 magic bytes 判 MIME + 头部解析尺寸 + 上限。

判别式：客户端声明的类型 / 扩展名一律不参与——只看字节。
"""

from __future__ import annotations

import pytest

from agent_harness.attachments.errors import AttachmentError
from agent_harness.attachments.probe import detect_image
from tests.attachments.helpers import (
    gif_bytes,
    jpeg_bytes,
    png_bytes,
    webp_vp8_bytes,
    webp_vp8l_bytes,
    webp_vp8x_bytes,
)

_ALLOWED = ("image/png", "image/jpeg", "image/webp", "image/gif")


def _detect(data: bytes):
    return detect_image(
        data, max_pixels=64_000_000, max_dimension=8192, allowed_media_types=_ALLOWED
    )


@pytest.mark.parametrize(
    ("data", "media_type", "width", "height"),
    [
        (png_bytes(640, 480), "image/png", 640, 480),
        (gif_bytes(120, 90), "image/gif", 120, 90),
        (jpeg_bytes(800, 600), "image/jpeg", 800, 600),
        (webp_vp8_bytes(321, 123), "image/webp", 321, 123),
        (webp_vp8l_bytes(321, 123), "image/webp", 321, 123),
        (webp_vp8x_bytes(321, 123), "image/webp", 321, 123),
    ],
)
def test_detects_supported_formats(
    data: bytes, media_type: str, width: int, height: int
) -> None:
    detected = _detect(data)
    assert detected.media_type == media_type
    assert detected.width == width
    assert detected.height == height


def test_empty_bytes_are_invalid() -> None:
    with pytest.raises(AttachmentError) as exc:
        _detect(b"")
    assert exc.value.code == "INVALID_IMAGE"


@pytest.mark.parametrize("data", [b"not an image at all", b"\x00\x01\x02\x03", b"<html>"])
def test_non_image_bytes_are_unsupported(data: bytes) -> None:
    with pytest.raises(AttachmentError) as exc:
        _detect(data)
    assert exc.value.code == "UNSUPPORTED_IMAGE_TYPE"


def test_truncated_png_header_is_invalid() -> None:
    with pytest.raises(AttachmentError) as exc:
        _detect(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
    assert exc.value.code == "INVALID_IMAGE"


def test_animated_png_is_unsupported() -> None:
    """APNG 需要多帧语义，v1 图片路径不接受（Pi / DSH 同口径）——拒绝而非误判单帧。"""
    with pytest.raises(AttachmentError) as exc:
        _detect(png_bytes(64, 64, animated=True))
    assert exc.value.code == "UNSUPPORTED_IMAGE_TYPE"


def test_pixel_limit_is_enforced() -> None:
    with pytest.raises(AttachmentError) as exc:
        detect_image(
            png_bytes(4000, 4000),
            max_pixels=1_000_000,
            max_dimension=8192,
            allowed_media_types=_ALLOWED,
        )
    assert exc.value.code == "IMAGE_TOO_MANY_PIXELS"


def test_dimension_limit_is_enforced() -> None:
    with pytest.raises(AttachmentError) as exc:
        detect_image(
            png_bytes(9000, 10),
            max_pixels=64_000_000,
            max_dimension=8192,
            allowed_media_types=_ALLOWED,
        )
    assert exc.value.code == "IMAGE_DIMENSION_TOO_LARGE"


def test_media_type_not_allowed_is_unsupported() -> None:
    with pytest.raises(AttachmentError) as exc:
        detect_image(
            gif_bytes(10, 10),
            max_pixels=64_000_000,
            max_dimension=8192,
            allowed_media_types=("image/png",),
        )
    assert exc.value.code == "UNSUPPORTED_IMAGE_TYPE"
