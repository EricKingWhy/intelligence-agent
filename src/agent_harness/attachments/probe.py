"""图片头探测：按 magic bytes 判定 MIME、按头部解析像素尺寸（不完整解码）。

magic-bytes 判据对译自 Pi `packages/coding-agent/src/utils/mime.ts:1-116`
（MIT，commit `1b347794`）；尺寸解析用标准库实现（**零新依赖**）——本仓未依赖
Pillow，且本票只要求"解析图片头"、不做解码/归一化（那是 MM-02）。

为什么客户端声明不是权威：文件名 / Content-Type 由调用方提供、可任意伪造，
而"这段字节是不是图片、是哪种图片"只能由字节自身回答（#822 AC）。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_harness.attachments.errors import AttachmentError

#: PNG 签名（来源: Pi `PNG_SIGNATURE`）。
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")
_RIFF = b"RIFF"
_WEBP = b"WEBP"
_JPEG_SOI = b"\xff\xd8\xff"

#: JPEG SOFn（Start Of Frame）标记：这些载荷里的第 4/6 字节起是高度 / 宽度。
#: 排除 DHT(0xC4) / JPG(0xC8) / DAC(0xCC) 等非 SOF 段。
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


@dataclass(frozen=True)
class DetectedImage:
    """从字节头部探得的图片事实。"""

    media_type: str
    width: int
    height: int


def detect_image(
    data: bytes,
    *,
    max_pixels: int,
    max_dimension: int,
    allowed_media_types: tuple[str, ...],
) -> DetectedImage:
    """按字节判定图片类型与尺寸，并在接纳上限内。

    :raises AttachmentError: `UNSUPPORTED_IMAGE_TYPE`（不是受支持的图片格式，或不在
        允许 media types 内）/ `INVALID_IMAGE`（头部损坏或为空）/
        `IMAGE_TOO_MANY_PIXELS` / `IMAGE_DIMENSION_TOO_LARGE`。
    """
    if not data:
        raise AttachmentError("图片字节为空。", "INVALID_IMAGE")
    detected = _detect(data)
    if detected.media_type not in allowed_media_types:
        raise AttachmentError(
            f"图片类型 {detected.media_type!r} 不在允许列表内。", "UNSUPPORTED_IMAGE_TYPE"
        )
    if detected.width * detected.height > max_pixels:
        raise AttachmentError(
            "图片像素数超过上限。", "IMAGE_TOO_MANY_PIXELS"
        )
    if max(detected.width, detected.height) > max_dimension:
        raise AttachmentError("图片边长超过上限。", "IMAGE_DIMENSION_TOO_LARGE")
    return detected


def _detect(data: bytes) -> DetectedImage:
    if data.startswith(_PNG_SIGNATURE):
        return _png(data)
    if data[:6] in _GIF_SIGNATURES:
        return _gif(data)
    if data[:3] == _JPEG_SOI:
        return _jpeg(data)
    if data[:4] == _RIFF and data[8:12] == _WEBP:
        return _webp(data)
    raise AttachmentError("不支持或无法识别的图片字节。", "UNSUPPORTED_IMAGE_TYPE")


def _invalid() -> AttachmentError:
    return AttachmentError("图片头部损坏或无法解析。", "INVALID_IMAGE")


def _png(data: bytes) -> DetectedImage:
    if len(data) < 33:
        raise _invalid()
    chunk_length = int.from_bytes(data[8:12], "big")
    chunk_type = data[12:16]
    if chunk_length != 13 or chunk_type != b"IHDR":
        raise _invalid()
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width == 0 or height == 0:
        raise _invalid()
    if _png_is_animated(data):
        # 与 Pi / DSH 同口径：APNG 需要多帧语义，v1 图片路径不接受（来源: Pi
        # `isAnimatedPng`、DSH 只接纳单帧栅格）。拒绝而非误当成单帧 PNG。
        raise AttachmentError("不支持动图 PNG。", "UNSUPPORTED_IMAGE_TYPE")
    return DetectedImage("image/png", width, height)


def _png_is_animated(data: bytes) -> bool:
    """扫描 PNG chunk，判断是否含 `acTL`（动画控制）且出现在首个 `IDAT` 之前。

    来源: Pi `isAnimatedPng`（零依赖逐 chunk 扫描）。
    """
    offset = 8
    total = len(data)
    while offset + 8 <= total:
        chunk_length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        if chunk_type == b"acTL":
            return True
        if chunk_type == b"IDAT":
            return False
        next_offset = offset + 8 + chunk_length + 4
        if next_offset <= offset or next_offset > total:
            return False
        offset = next_offset
    return False


def _gif(data: bytes) -> DetectedImage:
    if len(data) < 10:
        raise _invalid()
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    if width == 0 or height == 0:
        raise _invalid()
    return DetectedImage("image/gif", width, height)


def _jpeg(data: bytes) -> DetectedImage:
    total = len(data)
    index = 2  # 跳过 SOI
    while index + 1 < total:
        while index < total and data[index] == 0xFF:  # 填充字节
            index += 1
        if index >= total:
            break
        marker = data[index]
        index += 1
        if marker == 0x00:  # 字节填充
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # 无长度段
            continue
        if marker in (0xD9, 0xDA):  # EOI / SOS：之前没找到 SOF
            break
        if index + 2 > total:
            raise _invalid()
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2:
            raise _invalid()
        if marker in _JPEG_SOF_MARKERS:
            if index + 7 > total:
                raise _invalid()
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            if width == 0 or height == 0:
                raise _invalid()
            return DetectedImage("image/jpeg", width, height)
        index += segment_length
    raise _invalid()


def _webp(data: bytes) -> DetectedImage:
    if len(data) < 16:
        raise _invalid()
    format_fourcc = data[12:16]
    if format_fourcc == b"VP8 ":
        if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            raise _invalid()
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
    elif format_fourcc == b"VP8L":
        if len(data) < 25 or data[20] != 0x2F:
            raise _invalid()
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    elif format_fourcc == b"VP8X":
        if len(data) < 30:
            raise _invalid()
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
    else:
        raise AttachmentError("不支持的 WebP 变体。", "UNSUPPORTED_IMAGE_TYPE")
    if width == 0 or height == 0:
        raise _invalid()
    return DetectedImage("image/webp", width, height)


__all__ = ["DetectedImage", "detect_image"]
