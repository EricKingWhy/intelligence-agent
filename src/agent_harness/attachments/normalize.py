"""图片归一化：EXIF 方向、8-bit sRGB、限内缩放、有 alpha→WebP 否则 JPEG（#823 / MM-02）。

语义移植自 **DeepSeek Harness** `packages/attachment/attachment-local/src/normalization.ts`
（MIT，commit `5badb150`）：客户端不压缩不缩放，归一化全在 host 侧做；方向由 EXIF
决定、带 alpha 走 WebP、否则 JPEG、超出像素/边长/字节上限则逐档缩放与降质。本仓
把上游的 `sharp` 换成 **Pillow**（标准库无 JPEG/WebP 编码器，物理上绕不过；依赖
由 #823 声明，见 `pyproject.toml`）。

**归一化发生在模型边界、不在入库时**：MM-01（#822）的存储契约是"原始字节保真"
（上传回执与读回逐字节相等），故本仓保留原始字节，只在**投影物化**时归一化——模型
看到的是"竖拍不躺倒"的图，用户的原始文件与受控读回一字未动。这是相对 DSH
（pre-persist 归一化）的一处**有意偏离**，理由见 PRD 与 `web/attachments.py` 的存储
契约。判据纯函数、确定性：同字节输入必得同输出（replay/resume 一致）。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps

#: 发送前的归一化目标（PRD D2 / DSH 一组默认）：长边 ≤ 2048px、编码后 ≤ 4 MiB。
#: 与上传接纳上限（`attachments.types` 的 20 MiB / 8192px）分开：接纳上限管"收不收"，
#: 这里管"发给模型前压到多大"。
TARGET_MAX_DIMENSION = 2048
TARGET_MAX_BYTES = 4 * 1024 * 1024

#: 质量阶梯（有损）：从高到低找第一档 ≤ 目标字节数；上限比 DSH 的 82 更保守，
#: 保证桌面截图（文字密集、PNG 转 JPEG 后偏大）也能落进 4 MiB。
_QUALITY_LADDER: tuple[int, ...] = (90, 85, 80, 75, 70, 60, 50)

#: 输出 media type（有 alpha → WebP，否则 JPEG）。
_WEBP = "image/webp"
_JPEG = "image/jpeg"


@dataclass(frozen=True)
class NormalizedImage:
    """归一化结果：编码后的字节 + 其 media type。"""

    media_type: str
    data: bytes


def normalize_image(
    data: bytes,
    *,
    max_dimension: int = TARGET_MAX_DIMENSION,
    max_bytes: int = TARGET_MAX_BYTES,
) -> NormalizedImage:
    """把一段图片字节归一化成适合发给视觉模型的字节。

    步骤（顺序即 DSH）：① 按 EXIF 应用方向；② 需要时长边缩到 `max_dimension`；
    ③ 有 alpha → WebP，否则 JPEG（转成对应 color mode）；④ 从质量阶梯里降档直到
    编码后 ≤ `max_bytes`（阶梯用尽仍超限则取最低档结果——不无限循环）。

    动图（APNG/GIF 多帧）只取第一帧：v1 图片路径按单帧栅格处理（探测层已拒绝 APNG，
    这里兜住 GIF）。解码失败抛 `OSError`/`PIL.UnidentifiedImageError`，由调用方决定
    如何降级（本仓在投影层回退到原始字节，不让一张坏图 brick 整个 run）。
    """
    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        # 动图取首帧（单帧栅格语义）。
        if getattr(opened, "is_animated", False):
            opened.seek(0)
        frame = ImageOps.exif_transpose(opened) or opened
        has_alpha = _has_alpha(frame)
        if max(frame.width, frame.height) > max_dimension:
            frame = frame.copy()
            frame.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
        if has_alpha:
            prepared = frame.convert("RGBA")
            media_type = _WEBP
        else:
            prepared = frame.convert("RGB")
            media_type = _JPEG
    return NormalizedImage(
        media_type=media_type,
        data=_encode_within(prepared, media_type, max_bytes),
    )


def _has_alpha(image: Image.Image) -> bool:
    """是否有 alpha 通道（含调色板图的 `transparency` 表）。"""
    if image.mode in ("RGBA", "LA", "PA"):
        return True
    return image.mode == "P" and "transparency" in image.info


def _encode_within(image: Image.Image, media_type: str, max_bytes: int) -> bytes:
    """按质量阶梯编码，返回第一份 ≤ `max_bytes` 的字节；都超限则返回最小的一份。"""
    best: bytes | None = None
    for quality in _QUALITY_LADDER:
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP" if media_type == _WEBP else "JPEG", quality=quality)
        encoded = buffer.getvalue()
        if best is None or len(encoded) < len(best):
            best = encoded
        if len(encoded) <= max_bytes:
            return encoded
    # 阶梯用尽仍超限：取最小的一份（不无限循环；调用方若要求硬上限可再缩放）。
    assert best is not None
    return best


__all__ = [
    "TARGET_MAX_BYTES",
    "TARGET_MAX_DIMENSION",
    "NormalizedImage",
    "normalize_image",
]
