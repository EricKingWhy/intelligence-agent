"""#824 / MM-03（AC7）：图片按近似成本计入 token 估算与上下文压力。

口径见 `context/tokens.IMAGE_TOKENS_PER_IMAGE`（Pi `estimate.ts` 的
`ESTIMATED_IMAGE_CHARS=4800 → 1200 token/图`，Open WebUI 1000 为交叉参照）。
本文件钉住三件事：

1. 图片块（标准块 `image` 与 provider 块 `image_url`）各计 `IMAGE_TOKENS_PER_IMAGE`；
2. 无附件的纯文本消息口径逐字不变（AC9）；
3. ContextBuilder 的增量估算（`_estimate_tokens_cached`）在视觉口径下真的多算图片。
"""

from __future__ import annotations

import io

import pytest
from langchain_core.messages import HumanMessage
from PIL import Image

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.tokens import (
    IMAGE_TOKENS_PER_IMAGE,
    estimate_message_tokens,
    image_tokens_in_message,
)
from agent_harness.session import USER_MESSAGE
from agent_harness.storage.artifact import FakeArtifactStore, compute_byte_artifact_id
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

_AID = "sha256:" + "a" * 64
_STANDARD_BLOCK = {"type": "image", "file_id": _AID, "mime_type": "image/png"}
_PROVIDER_BLOCK = {
    "type": "image_url",
    "image_url": {"url": "data:image/png;base64,AA", "detail": "auto"},
}


def test_standard_image_block_adds_constant() -> None:
    text_only = HumanMessage(content=[{"type": "text", "text": "看图"}])
    with_image = HumanMessage(content=[{"type": "text", "text": "看图"}, _STANDARD_BLOCK])
    # 图片块自身的近似成本恰好是常量。
    assert image_tokens_in_message(with_image) == IMAGE_TOKENS_PER_IMAGE
    # 整体估算：常量 + 图片块 JSON 的少量结构 token（上界 +32 兜住 JSON 形状）。
    delta = estimate_message_tokens([with_image]) - estimate_message_tokens([text_only])
    assert IMAGE_TOKENS_PER_IMAGE <= delta < IMAGE_TOKENS_PER_IMAGE + 32


def test_provider_image_url_block_also_counted() -> None:
    assert image_tokens_in_message(
        HumanMessage(content=[{"type": "text", "text": "x"}, _PROVIDER_BLOCK])
    ) == IMAGE_TOKENS_PER_IMAGE


def test_multiple_images_scale_linearly() -> None:
    blocks = [
        {"type": "text", "text": "x"},
        _STANDARD_BLOCK,
        {**_STANDARD_BLOCK, "file_id": "sha256:" + "b" * 64},
    ]
    assert image_tokens_in_message(HumanMessage(content=blocks)) == 2 * IMAGE_TOKENS_PER_IMAGE


def test_plain_text_message_count_unchanged() -> None:
    """AC9：纯文本（str / 只含文本块）的图片增量为 0，口径逐字不变。"""
    assert image_tokens_in_message(HumanMessage(content="只有文字")) == 0
    assert (
        image_tokens_in_message(HumanMessage(content=[{"type": "text", "text": "x"}])) == 0
    )


def _png(width: int = 8, height: int = 6) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _session_with_image(tmp_path):
    session = make_session(tmp_path)
    data = _png()
    attachment_id = compute_byte_artifact_id(data)
    session.append(
        USER_MESSAGE,
        {
            "content": "看看这张图",
            "attachments": [
                {
                    "kind": "image",
                    "attachment_id": attachment_id,
                    "media_type": "image/png",
                    "bytes": len(data),
                    "width": 8,
                    "height": 6,
                }
            ],
        },
    )
    store = FakeArtifactStore()
    await store.save_bytes(session.session_id, data, mime_type="image/png")
    return session, store


def _builder(store, *, vision: bool) -> ContextBuilder:
    if store is None:
        return ContextBuilder(ScriptedModel([]), model_supports_vision=vision)
    return ContextBuilder(
        ScriptedModel([]),
        artifact_store=store,
        artifact_read_tool_name="read_artifact",
        model_supports_vision=vision,
    )


@pytest.mark.asyncio
async def test_builder_vision_estimate_exceeds_non_vision_by_image_cost(tmp_path):
    """视觉口径的 build 估算比非视觉口径多出（至少）一张图的近似成本。

    这是"图不计费导致 hard guard 失守"的直接回归：图片块的近似成本必须真的进
    `_estimate_tokens_cached` 的读数。
    """
    session, store = await _session_with_image(tmp_path)

    vision = _builder(store, vision=True)
    non_vision = _builder(store, vision=False)
    await vision.build(session)
    await non_vision.build(session)

    assert (
        vision._token_estimate_total - non_vision._token_estimate_total
        >= IMAGE_TOKENS_PER_IMAGE
    )


@pytest.mark.asyncio
async def test_builder_text_only_estimate_unchanged(tmp_path):
    """AC9 回归：无附件会话的估算在视觉/非视觉两口径下**逐字节相等**（无图片增量）。"""
    from agent_harness.session.derive import derive_messages

    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "只有文字"})

    vision = _builder(None, vision=True)
    non_vision = _builder(None, vision=False)
    built = await vision.build(session)
    await non_vision.build(session)

    assert vision._token_estimate_total == non_vision._token_estimate_total
    # 投影里没有任何图片块。
    assert not any(
        isinstance(m.content, list)
        and any(isinstance(b, dict) and b.get("type") == "image" for b in m.content)
        for m in built
    )
    assert image_tokens_in_message(
        next(m for m in derive_messages(session.events) if isinstance(m.content, str))
    ) == 0
