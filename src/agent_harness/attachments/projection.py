"""附件引用 → 模型可见投影的纯函数层（#823 / MM-02）。

`user/message` 事件只带**引用**（`attachments` 数组，见 `ImageRef`）；把引用物化
成图片内容块、或在非视觉模型下替换为文本占位符，都由这里定义，供
`session/derive.py` 的投影层消费。**本模块不碰字节、不碰存储**——base64 载荷由
请求装配层的 adapter（`model/multimodal.py`）在最后一刻从字节存储取回、归一化后
嵌入，事件流里永远不出现 base64（不变量 #15 + MM-02 AC3）。

来源（AGENTS.md §6.1，判定见 `docs/research/2026-10-07-multimodal-image-input-research.md`
§7.3-A）：引用形状移植自 **DeepSeek Harness** `packages/attachment/attachment/src/types.ts`
（`ImageAttachmentRef`，MIT，commit `5badb150`）；非视觉占位符文案逐字移植自 **Pi**
`packages/ai/src/api/transform-messages.ts`（`"(image omitted: model does not support images)"`，
MIT，commit `1b347794`）——两个独立来源各自解过同一问题（引用进事件、模型侧降级），
本项目只对译结构、不复制其运行时。
"""

from __future__ import annotations

from typing import Any

from agent_harness.attachments.types import ImageAttachmentRef

#: 附件引用的 `kind` 词汇。v1 只有图片（通用文件走 `@path` 引用，不进 provider）。
KIND_IMAGE = "image"

#: 非视觉模型下的降级文案（逐字移植 Pi `getNonVisionImageNote` 同源措辞）。
#: 语义是"图被明确省略"，不是静默丢弃、也不是报错中断（PRD D3/D6）。
IMAGE_OMITTED_PLACEHOLDER = "(image omitted: model does not support images)"


class ImageRef(ImageAttachmentRef):
    """`user/message.data["attachments"]` 里一条图片引用的形状（#823 AC1）。

    加法式扩展：`content` 仍是 `str`，引用是平行字段。`name` 是去掉本地路径信息的
    展示名（可缺省）。这是**事件持久化**的形状；模型可见投影由 `image_content_block`
    给出。

    #823 / MM-02（B1）：**继承** MM-01 的持久化契约 `ImageAttachmentRef`，只加一个
    `kind` 判别字段——不再是与之重复的第二份领域模型。
    """

    kind: str = KIND_IMAGE


def parse_image_refs(raw: Any) -> list[ImageRef]:
    """从事件 `data["attachments"]` 解析图片引用；坏形状**逐条**跳过（不 brick 恢复）。

    与 `derive.collect_dangling` 同一条容错纪律：投影/恢复必经节点上，一行坏数据
    不能拖垮整个会话。判据只看形状（kind=image、id/类型为串、三数为非负 int），
    不查存在性（那是发送端点与读端点的责任）。
    """
    if not isinstance(raw, list):
        return []
    refs: list[ImageRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("kind") != KIND_IMAGE:
            continue
        attachment_id = item.get("attachment_id")
        media_type = item.get("media_type")
        if not isinstance(attachment_id, str) or not attachment_id:
            continue
        if not isinstance(media_type, str) or not media_type:
            continue
        width = item.get("width")
        height = item.get("height")
        size = item.get("bytes")
        if not _is_non_negative_int(width) or not _is_non_negative_int(height):
            continue
        if not _is_non_negative_int(size):
            continue
        name = item.get("name")
        refs.append(
            ImageRef(
                attachment_id=attachment_id,
                media_type=media_type,
                bytes=size,
                width=width,
                height=height,
                name=name if isinstance(name, str) and name else None,
            )
        )
    return refs


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def image_content_block(ref: ImageRef) -> dict[str, object]:
    """引用 → **标准**图片内容块（provider 无关的中间层）。

    形状即 LangChain v1 的 `ImageContentBlock`（`{"type":"image", ...}`，见
    `langchain_core/messages/content.py`）：这里用 `file_id` 承载内容寻址的
    `attachment_id`（该字段的语义正是"外部文件存储里的引用"），**不**在此嵌入
    base64——真正的字节由请求装配 adapter 在发送前一刻物化（见 `model/multimodal.py`）。

    `width`/`height`（**便宜的图像元数据**，与 `mime_type` 同类）随块一并带出：token 估算
    （`context.tokens.image_tokens_for_size`）需要尺寸才能按尺寸相关近似公式计费，而本层
    正是尺寸的已知点（`ImageRef` 已带）。刻意排除的是**载荷**（base64），不是元数据。该块
    是**服务端内部**的投影产物（`derive_messages` → `model.multimodal`），不是跨端契约
    ——跨端数据是事件（`user/message.data["attachments"]`，其形状不变）。provider 块
    （`image_url`）由 `model.multimodal._translate_block` 生成，**不带**这两个字段（其形状
    由 provider 协议决定），故估算对 provider 块走尺寸未知回退。
    """
    return {
        "type": "image",
        "file_id": ref.attachment_id,
        "mime_type": ref.media_type,
        "width": ref.width,
        "height": ref.height,
    }


def content_block_with_text(text: str, refs: list[ImageRef]) -> list[dict[str, object]]:
    """视觉模型路径下的 user 消息内容：文本块 + 每个引用一张图片块（保持顺序）。"""
    blocks: list[dict[str, object]] = [{"type": "text", "text": text}]
    blocks.extend(image_content_block(ref) for ref in refs)
    return blocks


def text_with_omitted_images(text: str) -> str:
    """非视觉模型路径：在原文本后追加固定占位符（图不被静默丢弃）。"""
    if not text:
        return IMAGE_OMITTED_PLACEHOLDER
    return f"{text}\n{IMAGE_OMITTED_PLACEHOLDER}"


__all__ = [
    "IMAGE_OMITTED_PLACEHOLDER",
    "KIND_IMAGE",
    "ImageRef",
    "content_block_with_text",
    "image_content_block",
    "parse_image_refs",
    "text_with_omitted_images",
]
