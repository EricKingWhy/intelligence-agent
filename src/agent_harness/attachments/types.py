"""附件领域的持久化契约（来源: DeepSeek Harness `5badb150`
`packages/attachment/attachment/src/types.ts:1-164`，MIT）。

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
#: 来源: DSH `attachment-local/src/file-store.ts:45-56` `FILE_ID_PATTERN`。
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
    """一张不可变图片的持久化引用的**基类**（来源: DSH `ImageAttachmentRef`）。

    与 DSH 的差异：MM-01 不做归一化，故 `original_dimensions` 缺省不出现（那要
    归一化缩放过才填）。`name` 是**去掉本地路径信息**的展示名（见 `file_leaf_name`）。

    **消费方**：MM-02 的投影层领域模型 `attachments.projection.ImageRef` 直接**继承本类**
    并加一个 `kind` 判别字段（#823 / MM-02 B1：消除双份领域模型）。二者不再是重复
    定义——`kind` 是唯一差异，事件里 `user/message.data["attachments"]` 的形状即
    `ImageRef`。
    """

    attachment_id: AttachmentId
    media_type: str
    bytes: int
    width: int
    height: int
    #: 展示名（去本地路径）。#823 / MM-02（B6）：写入路径**恒缺省**——上传回执的 name
    #: 未持久化（`web/attachments.py` 只存字节+mime），发送端点只收 id 列表、无回传信道，
    #: 属**结构性缺省**；`parse_image_refs` 会解析它（兼容未来写入方）。展示名接线留待
    #: 前端票 / MM-03，不留无声死字段。
    name: str | None = None


class ImageAttachmentLimits(BaseModel):
    """上传接纳与请求缓冲使用的部署上限（来源: DSH `ImageAttachmentLimits`）。

    默认值取 DSH 一组（20 MiB / 20 张 / 200 MiB / 64M 像素 / 8192px / 四种类型）。
    """

    max_image_bytes: int
    #: #823 / MM-02（B6）：单消息图片**数量**上限。`resolve_image_limits` 已读出，但
    #: 发送路径的聚合校验（超数量 → 413/422）归 MM-03；当前仅 pydantic 层有静态兜底
    #: （`web.app.SendMessageRequest.attachments` 的 `max_length`）。不做无声死字段。
    max_images_per_message: int
    #: #823 / MM-02（B6）：单消息图片**总字节**上限，同样归 MM-03 消费（见上）。
    max_message_image_bytes: int
    max_image_pixels: int
    max_image_dimension: int
    media_types: tuple[str, ...]


def parse_allowed_media_types(raw: str) -> tuple[str, ...]:
    """解析逗号分隔的允许图片类型；未知类型 / 空 → `ValueError`（响亮失败）。

    **单一解析点**：`Settings` 的字段校验（**启动期**，见 `config.Settings`）与
    `resolve_image_limits`（请求期）共用它，避免两处规则漂移。未知类型静默忽略会让
    部署者以为某格式已放行、实际被悄悄拒绝（fail-closed 的反面）。
    """
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
    return tuple(media_types)


def resolve_image_limits(settings: Settings) -> ImageAttachmentLimits:
    """把 `Settings` 里的附件上限键解析成领域契约（单一解析点）。

    `attachment_allowed_media_types` 是逗号分隔的字符串（沿用本仓"复杂配置 = env
    里字符串"的既有形制）；解析规则单点在 `parse_allowed_media_types`，且**已由
    `Settings` 在构造期（= 启动期）预校验**——请求路径不会再撞上配置错误（配错在
    服务起来时即响亮失败）。
    """
    return ImageAttachmentLimits(
        max_image_bytes=settings.attachment_max_image_bytes,
        max_images_per_message=settings.attachment_max_images_per_message,
        max_message_image_bytes=settings.attachment_max_message_image_bytes,
        max_image_pixels=settings.attachment_max_image_pixels,
        max_image_dimension=settings.attachment_max_image_dimension,
        media_types=parse_allowed_media_types(settings.attachment_allowed_media_types),
    )


__all__ = [
    "EXTENSION_MEDIA_TYPES",
    "SUPPORTED_IMAGE_MEDIA_TYPES",
    "AttachmentId",
    "ImageAttachmentLimits",
    "ImageAttachmentRef",
    "ImageMediaType",
    "parse_allowed_media_types",
    "resolve_image_limits",
]
