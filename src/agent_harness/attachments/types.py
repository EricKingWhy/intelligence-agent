"""附件领域的持久化契约（来源: DeepSeek Harness `5badb150`
`packages/attachment/attachment/src/types.ts:1-164`，MIT）。

只移植 MM-01 需要的三个契约：`ImageAttachmentRef` / `ImageAttachmentLimits` /
`AttachmentId`（本仓用 `str` + 别名表达 opacity，落库形状见 `storage/artifact.py`
的 `BYTE_ARTIFACT_ID_PATTERN`）。**配置面解析规则**（`parse_allowed_media_types` 与
词汇表）住在 `attachments.validation`，本模块原样再导出——`config` 只 import 前者，
方向因此单向（#939 M-15）。`PromptContentPart` 一族（浏览器提交 → 落库引用的
admission 流程）属 MM-02 的投影层，本票不移植。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from agent_harness.attachments.validation import (
    EXTENSION_MEDIA_TYPES,
    SUPPORTED_IMAGE_MEDIA_TYPES,
    ImageMediaType,
    parse_allowed_media_types,
)

if TYPE_CHECKING:
    from agent_harness.config import Settings

#: 内容寻址 id（`sha256:<64 hex>`）。**永不**是路径 / 裸 URL / bearer 句柄。
#: 来源: DSH `attachment-local/src/file-store.ts:45-56` `FILE_ID_PATTERN`。
AttachmentId = str


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
    #: 展示名（去本地路径）。**可空**，不是"恒缺省"（#939 M-27 起的实际口径）：
    #: CLI `--image <path>` 手上有文件名 ⇒ **真的带上**（`cli.py::_admit_cli_images` 传
    #: `name=str(path)`，经 `admission.image_reference` 的 `file_leaf_name` 消毒）；
    #: Web 上传回执不持久化 name（`web/attachments.py` 只存字节+mime），发送端点只收
    #: id 列表 ⇒ 那条**无回传信道**、缺省。`parse_image_refs` 会解析它（兼容未来写入方）。
    #: 前端 `web/src/lib/attachmentRefs.ts` 的 `name` JSDoc 与本字段互为权威。
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
