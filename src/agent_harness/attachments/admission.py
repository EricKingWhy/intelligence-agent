"""附件**接纳期**的共享判定与文案（单一来源）。

三个入站入口——Web 上传端点（`web/attachments.py`）、发送端点
（`session/service.py::_resolve_attachment_refs`）、CLI `--image`
（`cli.py::_admit_cli_images`）——各自编排自己的流程，但重复的判定与文案只在这里定义：

- 声明（`Content-Type` / 文件名扩展名）与字节判定的一致性（`check_declared_image_matches`）；
- `user/message.data["attachments"]` 一条引用的形状（`image_reference`，下游消费者
  无需区分引用来自哪个入口）；
- 单张 / 单条消息数量 / 单条消息总字节三条上限的文案（`single_image_too_large_message` /
  `too_many_images_message` / `message_images_too_large_message`）；
- "存储不可用"的文案（`STORAGE_UNAVAILABLE_MESSAGE`）。

为什么抽到这里（#828 审查 B-P2-2 / A-F5）：原先同一句限值文案在 `cli.py` /
`session/service.py` / `web/attachments.py` 各写一遍（`session/service.py` 与 `cli.py`
除插值变量名外**逐字相同**），改一处限值语义要同时动三处——霰弹式修改的教科书形状。
判定留在调用层（各自的错误类型不同：`ImageInputRejected` / `TooManyAttachments` /
`AttachmentMessageTooLarge` / `AttachmentError`），文案与形状收在这里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_harness.attachments.errors import AttachmentError
from agent_harness.attachments.filenames import file_leaf_name
from agent_harness.attachments.projection import ImageRef
from agent_harness.attachments.types import EXTENSION_MEDIA_TYPES

#: 存储不可用（`artifact_dir` 置空且无对象存储）时的**唯一**文案。三个入口共用，
#: 机读码由各自的错误通道给（HTTP 走 `web/attachments.py::ATTACHMENT_STORAGE_UNAVAILABLE`）。
STORAGE_UNAVAILABLE_MESSAGE = (
    "本部署没有可用的附件存储（artifact_dir 为空，或对象存储只配了一半）"
)


def check_declared_image_matches(
    *,
    declared_media_type: str | None,
    name: str | None,
    detected_media_type: str,
) -> None:
    """声明（`Content-Type` / 文件名扩展名）与字节判定不符 → `IMAGE_TYPE_MISMATCH`。

    客户端声明不是权威（#822 AC）：不一致时以**字节**为准，且不是静默采用字节，而是明确
    拒绝（避免"我以为是 PNG"这类认知偏差被吞掉）。Web 上传端点传 `Content-Type` 解析出的
    声明，CLI 只传文件名（命令行没有 header）——两处同一份判定 ⇒ 同一张
    `shot.jpg`（装 PNG 字节）在两个入口得到**相同**的拒绝（#828 审查 A-F1）。
    """
    if declared_media_type is not None and declared_media_type != detected_media_type:
        raise AttachmentError(
            f"声明的类型 {declared_media_type!r} 与字节判定 {detected_media_type!r} 不符。",
            "IMAGE_TYPE_MISMATCH",
        )
    if name:
        leaf = file_leaf_name(name)
        dot = leaf.rfind(".")
        extension = leaf[dot:].lower() if dot >= 0 else ""
        extension_type = EXTENSION_MEDIA_TYPES.get(extension)
        if extension_type is not None and extension_type != detected_media_type:
            raise AttachmentError(
                f"文件名 {leaf!r} 的扩展名与字节判定 {detected_media_type!r} 不符。",
                "IMAGE_TYPE_MISMATCH",
            )


def image_reference(
    attachment_id: str,
    media_type: str,
    *,
    size: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    """构造 `user/message.data["attachments"]` 里一条图片引用的字典。

    形状的**唯一**定义点：`ImageRef`（`name` 恒缺省——见 `ImageAttachmentRef` 的 MM-02
    B6 说明）。CLI 与发送端点都经此构造，加字段时两处同时生效。
    """
    return ImageRef(
        attachment_id=attachment_id,
        media_type=media_type,
        bytes=size,
        width=width,
        height=height,
    ).model_dump(exclude_none=True)


def single_image_too_large_message(max_bytes: int) -> str:
    """单张图片字节超限的文案（Web 上传端点的流式读取与 CLI 的文件读取共用）。"""
    return f"图片超过单张字节上限（{max_bytes} 字节）。"


def read_image_file_bounded(path: Path | str, max_bytes: int) -> bytes:
    """按「上限 + 1 字节」读一个本地候选图片文件，越界抛 `IMAGE_TOO_LARGE`。

    #828 审查 A-F2 的落地：与 MM-01 `web/attachments.py::_read_body_bounded` 同一语义
    （按**实际读到的字节**判限，而不是信 `stat` 的 size——文件可能在判定与读取之间被
    换掉），只是 IO 形态不同（本地路径 vs 异步请求流）。多读 1 字节即可判出"超了"，
    所以峰值 = 上限 + 1，不会把超大文件整个缓冲进内存。

    同步 IO 收在这里（而不是让调用方在 `async def` 里直接 `open()`）——附件接纳是
    单发的短流程，同步读是刻意的（#828 审查 A-F7：事件循环线程上的同步 IO 判可接受）。
    """
    with Path(path).open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise AttachmentError(
            single_image_too_large_message(max_bytes), "IMAGE_TOO_LARGE"
        )
    return data


def too_many_images_message(limit: int, count: int) -> str:
    """单条消息图片**数量**超限的文案（CLI 与发送端点共用）。"""
    return f"单条消息最多 {limit} 张图片，本次引用了 {count} 张"


def message_images_too_large_message(limit_bytes: int, total_bytes: int) -> str:
    """单条消息图片**总字节**超限的文案（CLI 与发送端点共用）。"""
    return (
        "单条消息的图片总字节超过上限"
        f"（{limit_bytes} 字节）：本次合计 {total_bytes} 字节"
    )


__all__ = [
    "STORAGE_UNAVAILABLE_MESSAGE",
    "check_declared_image_matches",
    "image_reference",
    "message_images_too_large_message",
    "read_image_file_bounded",
    "single_image_too_large_message",
    "too_many_images_message",
]
