"""#823 / MM-02：图片归一化（Pillow 移植 DSH normalization 语义）。"""

from __future__ import annotations

import io

import pytest
from PIL import Image, UnidentifiedImageError

from agent_harness.attachments.normalize import (
    TARGET_MAX_BYTES,
    TARGET_MAX_DIMENSION,
    normalize_image,
)


def _encode(image: Image.Image, fmt: str = "PNG", **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def test_opaque_image_becomes_jpeg() -> None:
    result = normalize_image(_encode(Image.new("RGB", (10, 20), "red")))
    assert result.media_type == "image/jpeg"
    assert Image.open(io.BytesIO(result.data)).format == "JPEG"


def test_alpha_image_becomes_webp() -> None:
    result = normalize_image(_encode(Image.new("RGBA", (10, 20), (255, 0, 0, 128))))
    assert result.media_type == "image/webp"
    assert Image.open(io.BytesIO(result.data)).format == "WEBP"


def test_palette_transparency_counts_as_alpha() -> None:
    image = Image.new("P", (10, 10))
    image.info["transparency"] = 0
    result = normalize_image(_encode(image, fmt="PNG"))
    assert result.media_type == "image/webp"


def test_exif_orientation_is_applied() -> None:
    """竖拍（orientation=6）不被躺倒：存储 10x20 经 EXIF 转置后是 20x10。"""
    image = Image.new("RGB", (10, 20), "blue")
    exif = Image.Exif()
    exif[274] = 6  # Orientation: rotate 90° CW on display
    payload = _encode(image, fmt="JPEG", exif=exif)

    result = normalize_image(payload)
    with Image.open(io.BytesIO(result.data)) as normalized:
        assert (normalized.width, normalized.height) == (20, 10)


def test_downscales_to_target_dimension() -> None:
    result = normalize_image(_encode(Image.new("RGB", (4000, 1000), "green")))
    with Image.open(io.BytesIO(result.data)) as normalized:
        assert max(normalized.width, normalized.height) == TARGET_MAX_DIMENSION


def test_quality_ladder_respects_byte_budget() -> None:
    # 以 q90 编码长度作为预算：归一化必须给出 ≤ 该预算的字节（阶梯会降质）。
    reference = _encode(Image.new("RGB", (1024, 1024), "white"), fmt="JPEG", quality=90)
    result = normalize_image(
        _encode(Image.new("RGB", (1024, 1024), "white")),
        max_bytes=len(reference),
    )
    assert len(result.data) <= len(reference)


def test_generous_budget_stays_under_default_target() -> None:
    result = normalize_image(_encode(Image.new("RGB", (512, 512), "orange")))
    assert len(result.data) <= TARGET_MAX_BYTES


def test_is_deterministic() -> None:
    payload = _encode(Image.new("RGB", (200, 100), "purple"))
    first = normalize_image(payload)
    second = normalize_image(payload)
    assert first == second


def test_animated_gif_uses_first_frame() -> None:
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
    result = normalize_image(buffer.getvalue())
    assert result.media_type == "image/jpeg"
    assert Image.open(io.BytesIO(result.data)).format == "JPEG"


def test_undecodable_bytes_raise() -> None:
    with pytest.raises(UnidentifiedImageError):
        normalize_image(b"not an image at all")
