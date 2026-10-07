"""附件领域的持久化契约（来源: DeepSeek Harness `5badb150` `packages/attachment/attachment/src/types.ts`，MIT）。

只移植 MM-01 需要的三个契约：`ImageAttachmentRef` / `ImageAttachmentLimits` /
`AttachmentId`（本仓用 `str` + 别名表达 opacity，落库形状见 `storage/artifact.py`
的 `BYTE_ARTIFACT_ID_PATTERN`）。`PromptContentPart` 一族（浏览器提交 → 落库引用的
admission 流程）属 MM-02 的投影层，本票不移植。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from agent_harness.config import Settings

#: 内容寻址 id（`sha256:<64 hex>`）。**永不**是路径 / 裸 URL / bearer 句柄。
#: 来源: DSH `file-store.ts` `FILE_ID_PATTERN`。
AttachmentId = str

#: v1 接受的栅格图片 media types（来源: DSH `types.ts` `ImageMediaType`）。
ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]

SUPPORTED_IMAGE_MEDIA_TYPES: tuple[ImageMediaType, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)

#: 扩展名 → media type（用于"声明文件名与字节不符"的判定；大小写不敏感）。
EXTENSION_MEDIA_TYPES: dict[str, ImageMediaType] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ImageAttachmentRef(BaseModel):
    """一张不可变图片的持久化引用（来源: DSH `ImageAttachmentRef`）。

    与 DSH 的差异：MM-01 不做归一化，故 `original_dimensions` 缺省不出现（那要
    归一化缩放过才填）。`name` 是**去掉本地路径信息**的展示名（见 `file_leaf_name`）。
    """

    attachment_id: AttachmentId
    media_type: str
    bytes: int
    width: int
    height: int
    name: str | None = None


class ImageAttachmentLimits(BaseModel):
    """上传接纳与请求缓冲使用的部署上限（来源: DSH `ImageAttachmentLimits`）。

    默认值取 DSH 一组（20 MiB / 20 张 / 200 MiB / 64M 像素 / 8192px / 四种类型）。
    """

    max_image_bytes: int
    max_images_per_message: int
    max_message_image_bytes: int
    max_image_pixels: int
    max_image_dimension: int
    media_types: tuple[str, ...]


def resolve_image_limits(settings: Settings) -> ImageAttachmentLimits:
    """把 `Settings` 里的附件上限键解析成领域契约（单一解析点）。

    `attachment_allowed_media_types` 是逗号分隔的字符串（沿用本仓"复杂配置 = env
    里字符串"的既有形制）；未知类型**响亮失败**——静默忽略会让部署者以为某格式已放行，
    实际被悄悄拒绝（fail-closed 的反面）。
    """
    raw = settings.attachment_allowed_media_types
    media_types: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        if value not in SUPPORTED_IMAGE_MEDIA_TYPES:
            raise ValueError(
                "attachment_allowed_media_types 含不支持的图片类型"
                f"（允许: {', '.join(SUPPORTED_IMAGE_MEDIA_TYPES)}）：{value!r}"
            )
        if value not in media_types:
            media_types.append(value)
    if not media_types:
        raise ValueError("attachment_allowed_media_types 不能为空")
    return ImageAttachmentLimits(
        max_image_bytes=settings.attachment_max_image_bytes,
        max_images_per_message=settings.attachment_max_images_per_message,
        max_message_image_bytes=settings.attachment_max_message_image_bytes,
        max_image_pixels=settings.attachment_max_image_pixels,
        max_image_dimension=settings.attachment_max_image_dimension,
        media_types=tuple(media_types),
    )


__all__ = [
    "EXTENSION_MEDIA_TYPES",
    "SUPPORTED_IMAGE_MEDIA_TYPES",
    "AttachmentId",
    "ImageAttachmentLimits",
    "ImageAttachmentRef",
    "ImageMediaType",
    "resolve_image_limits",
]
