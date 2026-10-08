"""测试用图片字节构造器（只构造**头部足够**的字节，供 `attachments.probe` 解析）。

本票只解析图片头、不完整解码，所以测试构造的是合法的头部 + 任意负载，而不是
真实的编码图片（避免为测试引入编码器依赖）。
"""

from __future__ import annotations

import os
import struct
import zlib


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)


def png_bytes(width: int, height: int, *, animated: bool = False) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
    if animated:
        out += _png_chunk(b"acTL", struct.pack(">II", 1, 0))
    out += _png_chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00" * (width * 3)))
    out += _png_chunk(b"IEND", b"")
    return out


def large_png_bytes(width: int, height: int, payload_bytes: int) -> bytes:
    """一张**体积很大**但头部合法的 PNG：IHDR 之后跟一条巨大的 IDAT。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
    out += _png_chunk(b"IDAT", os.urandom(payload_bytes))
    out += _png_chunk(b"IEND", b"")
    return out


def gif_bytes(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def jpeg_bytes(width: int, height: int) -> bytes:
    app0 = (
        b"\xff\xe0"
        + struct.pack(">H", 16)
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp_vp8_bytes(width: int, height: int) -> bytes:
    frame = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    chunk = b"VP8 " + struct.pack("<I", len(frame)) + frame
    return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk


def webp_vp8l_bytes(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + struct.pack("<I", bits)
    chunk = b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk


def webp_vp8x_bytes(width: int, height: int) -> bytes:
    canvas = (
        b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + struct.pack("<I", len(canvas)) + canvas
    return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk


__all__ = [
    "gif_bytes",
    "jpeg_bytes",
    "large_png_bytes",
    "png_bytes",
    "webp_vp8_bytes",
    "webp_vp8l_bytes",
    "webp_vp8x_bytes",
]
