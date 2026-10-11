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

**"取字节 + 归一化 + base64"也在本模块**（`load_image_payloads`，#939 M-12）。此前这段
住在 `ContextBuilder._load_image_payloads`，而接缝（`ImageResolver` / `to_provider_messages`）
在这里——三处 docstring（本模块、`attachments/projection.py`、`session/derive.py`）都声称
字节在本模块取回，实现却在别处，属"声称与实现错位"。装配的**两个半边**现在同处一室：
半张是同步纯函数（块 → 载荷），半张是异步有 I/O 的 loader；`ContextBuilder` 只调一次接缝、
不再自己解析。loader 不 import `context.*` / `session.*`（`referenced` 是入参、不是
`Session`），因此依赖仍是单向的 `context.builder → model.multimodal`。
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from langchain_core.messages import AnyMessage

from agent_harness.attachments.normalize import normalize_image
from agent_harness.attachments.projection import (
    IMAGE_OMITTED_PLACEHOLDER,
    text_with_omitted_images,
)

if TYPE_CHECKING:
    from agent_harness.storage.artifact import ArtifactStore

logger = logging.getLogger("agent_harness.model.multimodal")

#: 默认 `detail`（PRD D4 / OpenAI Chat Completions 取值：auto|low|high|original）。
DEFAULT_IMAGE_DETAIL = "auto"

#: 标准图片块的 `type` 判别值。
_STANDARD_IMAGE_TYPE = "image"
_PROVIDER_IMAGE_TYPE = "image_url"

#: 解析器：`file_id`（内容寻址 `attachment_id`）→ `(media_type, base64_data)`，取不回返回 None。
ImageResolver = Callable[[str], "tuple[str, str] | None"]


async def load_image_payloads(
    store: ArtifactStore | None,
    *,
    referenced: Iterable[str],
    max_dimension: int,
    max_bytes: int,
) -> dict[str, tuple[str, str]] | None:
    """把事件引用过的附件物化成 `attachment_id → (media_type, base64)` 载荷表。

    `None` 表示**接缝不存在**（没有字节存储——本部署没配附件存储，或本次请求模型不支持
    视觉）：调用方据此整条跳过装配，而不是拿到一张空表去逐块查。空表 = "有存储但一个都
    取不回"，两者对 `resolve_image` 的行为相同，但对调用方是两回事。

    **per-image 降级**：单张字节缺失 / 解码失败 / 归一化失败都只让**那一个 id 缺席**，
    其余照常返回——该引用在 `_translate_block` 里降级为占位文本块（不静默丢弃、不 brick
    run）。这正是本 loader 与 `to_provider_messages` 同处一个模块的理由：降级的两个半边
    在同一个文件里读得到。

    `referenced` 由调用方给（不传 `Session`）：授权判据是
    `session.derive.referenced_attachment_ids(session.events)`，#939 M-12 刻意不把它搬进来
    ——那会让 provider 装配层懂 Session 事件语义，内聚账是负的。调用方负责**只传被引用的
    id**（未引用的 id 不得读回，PRD D5）。
    """
    if store is None:
        return None
    payloads: dict[str, tuple[str, str]] = {}
    for attachment_id in sorted(set(referenced)):
        try:
            blob = await store.load_bytes(attachment_id)
        except Exception:  # noqa: BLE001 —— 存储故障降级为占位符，不让一张图 brick run
            logger.warning("附件字节读取失败（%s）——投影降级为占位符", attachment_id)
            continue
        data = blob.content
        if not data:
            continue
        try:
            normalized = normalize_image(
                data, max_dimension=max_dimension, max_bytes=max_bytes
            )
            media_type, raw = normalized.media_type, normalized.data
        except Exception:  # noqa: BLE001 —— 解码失败回退原始字节（best-effort）
            logger.warning("附件归一化失败（%s）——按原始字节发送", attachment_id)
            media_type, raw = blob.mime_type, data
        payloads[attachment_id] = (media_type, base64.b64encode(raw).decode("ascii"))
    return payloads


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


def downgrade_to_non_vision(messages: list[AnyMessage]) -> list[AnyMessage]:
    """把已按视觉口径装配的消息降级为非视觉形态（#823 / MM-02 A2 残口）。

    PRD D6 字面场景：主模型支持视觉、**发送后** fallback 到非视觉模型——切换那一步
    的重试若复用切换前已投影/装配的 `messages`，非视觉 fallback 会直接收到 provider
    `image_url` 块。本函数只做**纯消息变换**（不读 session、不触发 build/压缩等副作
    用）：含图片内容块的消息整体降级为等价的非视觉投影。

    **含图片块的形态**下，降级产物与 `derive_messages(..., supports_vision=False)`
    逐字一致——取首个文本块的文本，追加 `IMAGE_OMITTED_PLACEHOLDER`（视觉下
    `content` 必首块为 `{"type":"text","text":原文}`，见
    `attachments.projection.content_block_with_text`）。

    已无图片块的消息原样返回（无图消息逐字不变，AC8）。这包含两类：纯文本 / 只有
    文本块的消息，以及**图片字节取不回**的形态——`_translate_block` 早已把那张图片块
    降级成文本占位块，此时 `content` 是 `[{text:原文},{text:占位符}]` 双文本块列表，
    `_has_image_block` 为假，本函数不再动它。该形态与
    `derive_messages(..., supports_vision=False)` 的单字符串**不逐字相等**；但它不含
    任何图片块，D6 语义不受影响（非视觉 fallback 不会收到 `image_url` 块）。返回新
    消息对象，不原地改输入。

    识别 `image`（标准块）与 `image_url`（provider 块）两种形态：前者用于投影后、
    装配前的消息，后者用于 coordinator 重试点（已是装配后形态）。
    """
    result: list[AnyMessage] = []
    for message in messages:
        content = message.content
        if not isinstance(content, list) or not _has_image_block(content):
            result.append(message)
            continue
        result.append(
            message.model_copy(
                update={"content": text_with_omitted_images(_first_text_block(content))}
            )
        )
    return result


def _has_image_block(content: list[object]) -> bool:
    return any(
        isinstance(block, dict)
        and block.get("type") in (_STANDARD_IMAGE_TYPE, _PROVIDER_IMAGE_TYPE)
        for block in content
    )


def _first_text_block(content: list[object]) -> str:
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                return text
    return ""


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
    "downgrade_to_non_vision",
    "image_data_url",
    "load_image_payloads",
    "to_provider_messages",
]
