"""#823 / MM-02：ContextBuilder 的附件物化 → provider 载荷（build 出口）。"""

from __future__ import annotations

import io
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from PIL import Image

from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER
from agent_harness.context.builder import ContextBuilder
from agent_harness.model.multimodal import DEFAULT_IMAGE_DETAIL
from agent_harness.session import (
    MODEL_COMPLETED,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.storage.artifact import FakeArtifactStore, compute_byte_artifact_id
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

#: 合法四节摘要剧本（与 tests/context/test_647_bracket_identity.py 同源）。
_MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


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


def _first_image_url_block(messages):
    for message in messages:
        if isinstance(message, HumanMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return block
    raise AssertionError("请求里没有 image_url 块")


@pytest.mark.asyncio
async def test_resume_rebuilds_identical_image_block(tmp_path):
    """AC9：从**持久化事件前缀**重建（resume）出的请求与首次一致（图片块形状相同）。

    与"同一进程内续聊"不同：这里丢弃内存 Session，从磁盘 JSONL 重新 `Session.load`，
    用独立 builder 重新投影 + 重新取字节归一化——证明重建只依赖事件前缀（含引用）
    与字节存储，不依赖任何进程内状态。
    """
    session, store, _aid = await _session_with_image(tmp_path)
    first = await _builder(store, vision=True).build(session)
    first_block = _first_image_url_block(first)

    reloaded = Session.load(JsonlSessionStore(root=tmp_path), session.session_id)
    rebuilt = await _builder(store, vision=True).build(reloaded)

    assert _first_image_url_block(rebuilt) == first_block
    # 事件流里永远只有引用、没有 base64（AC3）。
    persisted = JsonlSessionStore(root=tmp_path).read_events(session.session_id)
    assert "base64" not in json.dumps(
        [event.data for event in persisted], ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_vision_compaction_reprojection_is_same_projection(tmp_path):
    """视觉投影下压缩的「重投影确认」必须与压缩输入**同口径**（#823）。

    `compact_now` 按 `model_supports_vision` 物化图片块，其尾部 `_reproject` 复核
    必须用**同一** `supports_vision`——否则带图会话一触发压缩，重投影（占位符文本）
    与压缩产物（图片块）不一致 ⇒ `CompactionPostWriteError`，run 直接 fail-closed。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "读取旧记录后继续。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
    data = _png()
    attachment_id = compute_byte_artifact_id(data)
    session.append(USER_MESSAGE, {
        "content": "看看这张图",
        "attachments": [{
            "kind": "image",
            "attachment_id": attachment_id,
            "media_type": "image/png",
            "bytes": len(data),
            "width": 8,
            "height": 6,
        }],
    })
    store = FakeArtifactStore()
    await store.save_bytes(session.session_id, data, mime_type="image/png")

    builder = ContextBuilder(
        ScriptedModel([AIMessage(content=_MODEL_SECTIONS)]),
        artifact_store=store,
        artifact_read_tool_name="read_artifact",
        model_supports_vision=True,
        max_context_tokens=10_000,
        auto_compact_threshold=0.3,
    )
    result = await builder.compact_now(session)

    assert result is not None
    assert result.compacted_turn_count == 1


@pytest.mark.asyncio
async def test_vision_compaction_without_pruner_is_not_a_noop(tmp_path):
    """手动压缩路径（builder **不传** artifact_store）的带图压缩不得静默 no-op（#823）。

    手动压缩 `service._build_context_builder` 不传 artifact_store ⇒ `_pruner is None`
    ⇒ `compact_now` 的 `source_ranges=None` ⇒ 压缩器**内部**在 `_early_source_ranges`
    从 events 重推导对齐。该重推导必须与压缩输入同一视觉口径——否则带图会话下
    重推导（占位符文本 `str`）与压缩产物（图片块列表 `list`）不一致 ⇒ 来源区间不可用
    ⇒ `compacted_turn_count=0`（用户按"压缩"却被告知无早期轮）。

    变异验证：把 `_early_source_ranges` 的 `supports_vision` 传参去掉，本用例必须红。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "读取旧记录后继续。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
    data = _png()
    attachment_id = compute_byte_artifact_id(data)
    session.append(USER_MESSAGE, {
        "content": "看看这张图",
        "attachments": [{
            "kind": "image",
            "attachment_id": attachment_id,
            "media_type": "image/png",
            "bytes": len(data),
            "width": 8,
            "height": 6,
        }],
    })

    # 对齐 `service._build_context_builder` 手动路径：**不传** store / read_tool_name。
    builder = ContextBuilder(
        ScriptedModel([AIMessage(content=_MODEL_SECTIONS)]),
        model_supports_vision=True,
        max_context_tokens=10_000,
        auto_compact_threshold=0.3,
    )
    result = await builder.compact_now(session)

    assert result is not None
    assert result.compacted_turn_count == 1


class _CountingStore(FakeArtifactStore):
    """记录 `load_bytes` 调用次数的假 store（B3 缓存验证用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.load_calls = 0

    async def load_bytes(self, artifact_id: str):
        self.load_calls += 1
        return await super().load_bytes(artifact_id)


def _decode_data_url_image(block: dict) -> Image.Image:
    import base64

    b64 = block["image_url"]["url"].split(";base64,", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(b64)))


@pytest.mark.asyncio
async def test_custom_image_detail_is_applied(tmp_path):
    """B2：`image_detail` 构造参数（配置接线）真的传到了 provider 载荷。"""
    session, store, _aid = await _session_with_image(tmp_path)
    messages = await ContextBuilder(
        ScriptedModel([]),
        artifact_store=store,
        artifact_read_tool_name="read_artifact",
        model_supports_vision=True,
        image_detail="low",
    ).build(session)
    assert _first_image_url_block(messages)["image_url"]["detail"] == "low"


@pytest.mark.asyncio
async def test_configured_normalize_dimension_is_applied(tmp_path):
    """B4：归一化长边目标从构造参数（Settings 接线）生效。"""
    session = make_session(tmp_path)
    data = _png(40, 20)
    attachment_id = compute_byte_artifact_id(data)
    session.append(USER_MESSAGE, {
        "content": "图",
        "attachments": [{
            "kind": "image", "attachment_id": attachment_id,
            "media_type": "image/png", "bytes": len(data), "width": 40, "height": 20,
        }],
    })
    store = FakeArtifactStore()
    await store.save_bytes(session.session_id, data, mime_type="image/png")
    messages = await ContextBuilder(
        ScriptedModel([]),
        artifact_store=store,
        artifact_read_tool_name="read_artifact",
        model_supports_vision=True,
        image_normalize_max_dimension=10,
    ).build(session)
    image = _decode_data_url_image(_first_image_url_block(messages))
    assert max(image.size) <= 10


@pytest.mark.asyncio
async def test_image_payload_cache_avoids_repeat_load(tmp_path):
    """B3：同一 builder 实例重复 build 不重复 `load_bytes`（内容寻址不可变）。"""
    session = make_session(tmp_path)
    data = _png()
    attachment_id = compute_byte_artifact_id(data)
    session.append(USER_MESSAGE, {
        "content": "图",
        "attachments": [{
            "kind": "image", "attachment_id": attachment_id,
            "media_type": "image/png", "bytes": len(data), "width": 8, "height": 6,
        }],
    })
    store = _CountingStore()
    await store.save_bytes(session.session_id, data, mime_type="image/png")
    builder = ContextBuilder(
        ScriptedModel([]),
        artifact_store=store,
        artifact_read_tool_name="read_artifact",
        model_supports_vision=True,
    )
    await builder.build(session)
    await builder.build(session)
    assert store.load_calls == 1


@pytest.mark.asyncio
async def test_usage_snapshot_messages_bucket_uses_vision_projection(tmp_path):
    """A6：`usage_snapshot` 的 messages 桶按**同一视觉口径**投影估算，不漂移。"""
    from agent_harness.context.tokens import estimate_message_tokens
    from agent_harness.session.derive import derive_messages

    session, store, _aid = await _session_with_image(tmp_path)
    vision_builder = _builder(store, vision=True)
    nonvision_builder = ContextBuilder(
        ScriptedModel([]),
        artifact_store=store,
        artifact_read_tool_name="read_artifact",
        model_supports_vision=False,
    )
    assert vision_builder.usage_snapshot(session)["messages"] == estimate_message_tokens(
        derive_messages(session.events, supports_vision=True)
    )
    assert nonvision_builder.usage_snapshot(session)["messages"] == estimate_message_tokens(
        derive_messages(session.events, supports_vision=False)
    )
