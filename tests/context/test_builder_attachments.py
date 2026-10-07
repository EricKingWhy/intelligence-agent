"""#823 / MM-02：ContextBuilder 的附件物化 → provider 载荷（build 出口）。"""

from __future__ import annotations

import io

import pytest
from langchain_core.messages import HumanMessage
from PIL import Image

from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER
from agent_harness.context.builder import ContextBuilder
from agent_harness.model.multimodal import DEFAULT_IMAGE_DETAIL
from agent_harness.session import USER_MESSAGE
from agent_harness.storage.artifact import FakeArtifactStore, compute_byte_artifact_id
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


def _png(width: int = 8, height: int = 6, *, alpha: bool = False) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    buffer = io.BytesIO()
    Image.new(mode, (width, height), (10, 20, 30, 255) if alpha else (10, 20, 30)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


async def _session_with_image(tmp_path):
    session = make_session(tmp_path)
    data = _png()
    attachment_id = compute_byte_artifact_id(data)
    ref = {
        "kind": "image",
        "attachment_id": attachment_id,
        "media_type": "image/png",
        "bytes": len(data),
        "width": 8,
        "height": 6,
    }
    session.append(USER_MESSAGE, {"content": "看看这张图", "attachments": [ref]})
    store = FakeArtifactStore()
    await store.save_bytes(session.session_id, data, mime_type="image/png")
    return session, store, attachment_id


def _builder(store, *, vision: bool):
    return ContextBuilder(
        ScriptedModel([]),
        artifact_store=store,
        artifact_read_tool_name="read_artifact",
        model_supports_vision=vision,
    )


@pytest.mark.asyncio
async def test_vision_model_gets_openai_image_url_block(tmp_path):
    session, store, _aid = await _session_with_image(tmp_path)

    messages = await _builder(store, vision=True).build(session)

    user = next(m for m in messages if isinstance(m, HumanMessage) and isinstance(m.content, list))
    blocks = user.content
    assert blocks[0] == {"type": "text", "text": "看看这张图"}
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["detail"] == DEFAULT_IMAGE_DETAIL
    assert blocks[1]["image_url"]["url"].startswith("data:image/")
    assert ";base64," in blocks[1]["image_url"]["url"]


@pytest.mark.asyncio
async def test_non_vision_model_gets_text_placeholder(tmp_path):
    session, store, _aid = await _session_with_image(tmp_path)

    messages = await _builder(store, vision=False).build(session)

    user = next(
        m for m in messages
        if isinstance(m, HumanMessage) and m.content == f"看看这张图\n{IMAGE_OMITTED_PLACEHOLDER}"
    )
    assert isinstance(user.content, str)


@pytest.mark.asyncio
async def test_vision_without_store_degrades_to_placeholder(tmp_path):
    """store 缺席时引用取不回字节 → 降级为占位符，而不是把裸标准块泄漏给 provider。"""
    session, _store, _aid = await _session_with_image(tmp_path)

    messages = await ContextBuilder(
        ScriptedModel([]), model_supports_vision=True
    ).build(session)

    user = next(m for m in messages if isinstance(m, HumanMessage) and isinstance(m.content, list))
    assert user.content == [
        {"type": "text", "text": "看看这张图"},
        {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER},
    ]


@pytest.mark.asyncio
async def test_text_only_build_is_unchanged(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "只有文字"})

    messages = await ContextBuilder(
        ScriptedModel([]), model_supports_vision=True
    ).build(session)

    user = next(
        m for m in messages if isinstance(m, HumanMessage) and m.content == "只有文字"
    )
    assert user.content == "只有文字"  # 无附件 ⇒ 逐字不变（AC8）
