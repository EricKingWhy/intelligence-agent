"""请求装配 adapter：标准内容块 → provider 载荷（#823 / MM-02）。

`session/derive.py` 的投影把附件引用物化成**标准**图片内容块
（`attachments.projection.image_content_block`：`{"type":"image","file_id","mime_type"}`）。
本模块在消息真正发给模型前，把这层 provider 无关的标准块翻译成 **OpenAI 风格**
载荷（`{"type":"image_url","image_url":{"url":"data:<mime>;base64,…","detail":…}}`）。

**为什么由本仓 adapter 承担**（MM-02 AC5 / PRD D4）：langchain-openai 的 `ChatOpenAI`
能自己把标准块翻成 `image_url`，但那样"请求方向翻译"就归了 LangChain，与本仓
"只借它做 Provider compatibility、协议细节自持"的边界不符（spec 13 §4）。这里显式
翻译，`detail` 也由本仓控制（默认 `auto`，OpenAI 文档取值之一）。

字节从**字节存储**按 `file_id` 取回、归一化后 base64 内联——事件流与 JSONL 里只有
引用，没有 base64（不变量 #15 + AC3）。取不回（引用悬空/存储不可用）的那张，降级为
占位文本块而不是静默丢弃（`derive` 的非视觉分支同款语义）。
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.messages import AnyMessage

from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER

#: 默认 `detail`（PRD D4 / OpenAI Chat Completions 取值：auto|low|high|original）。
DEFAULT_IMAGE_DETAIL = "auto"

#: 标准图片块的 `type` 判别值。
_STANDARD_IMAGE_TYPE = "image"
_PROVIDER_IMAGE_TYPE = "image_url"

#: 解析器：`file_id`（内容寻址 `attachment_id`）→ `(media_type, base64_data)`，取不回返回 None。
ImageResolver = Callable[[str], "tuple[str, str] | None"]


def image_data_url(media_type: str, base64_data: str) -> str:
    """`data:` URL（OpenAI `image_url.url` 接受 http(s) 或 data URL）。"""
    return f"data:{media_type};base64,{base64_data}"


def to_provider_messages(
    messages: list[AnyMessage],
    *,
    resolve_image: ImageResolver,
    detail: str = DEFAULT_IMAGE_DETAIL,
) -> list[AnyMessage]:
    """把消息链里的标准图片块翻译成 provider 载荷；其它消息原样返回。

    只改**内容为块列表**的消息（当前只有带图的 user 消息）；字符串内容的消息一律
    原样返回（无图消息逐字不变，AC8）。返回新消息对象，不原地改输入。
    """
    result: list[AnyMessage] = []
    for message in messages:
        content = message.content
        if not isinstance(content, list):
            result.append(message)
            continue
        translated = [
            _translate_block(block, resolve_image=resolve_image, detail=detail)
            for block in content
        ]
        result.append(message.model_copy(update={"content": translated}))
    return result


def _translate_block(
    block: object, *, resolve_image: ImageResolver, detail: str
) -> object:
    if not isinstance(block, dict) or block.get("type") != _STANDARD_IMAGE_TYPE:
        return block
    file_id = block.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return block
    resolved = resolve_image(file_id)
    if resolved is None:
        # 取不回字节：不静默丢弃，降级为占位文本块（与 derive 的非视觉分支同语义）。
        return {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER}
    media_type, base64_data = resolved
    return {
        "type": _PROVIDER_IMAGE_TYPE,
        "image_url": {"url": image_data_url(media_type, base64_data), "detail": detail},
    }


__all__ = [
    "DEFAULT_IMAGE_DETAIL",
    "ImageResolver",
    "image_data_url",
    "to_provider_messages",
]
