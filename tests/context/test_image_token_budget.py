"""#935 / M-03：图片按**尺寸相关近似公式**计入 token 估算与上下文压力。

口径见 `context/tokens.image_tokens_for_size`（OpenAI `gpt-4o` tile 制：`85 + 170×tiles`，
512px 方块，长边按 `IMAGE_MAX_DIMENSION` 等比缩小——与发送前 `normalize` 的
`frame.thumbnail` 同语义）。本文件钉住：

1. 标准图片块（`{"type":"image",...,"width","height"}`）按尺寸计费，随尺寸**单调**增长；
2. provider 块（`image_url`，无尺寸字段）走 `IMAGE_TOKENS_UNKNOWN_SIZE` 保守回退；
3. 无附件的纯文本消息口径逐字不变（AC9）；
4. ContextBuilder 的增量估算（`_estimate_tokens_cached`）在视觉口径下真的多算图片；
5. 消融：旧固定常量（1200）对小图高估、对 2048² 大图系统性低估；新公式两侧都更正。

断言一律用**硬编码的期望值**（如 8×6 → 255、2048² → 2805），不复用被测公式自身的
tile 算法——否则是"拿公式验公式"的同义反复。
"""

from __future__ import annotations

import io
import json

import pytest
from langchain_core.messages import HumanMessage
from PIL import Image

from agent_harness.attachments.projection import image_content_block
from agent_harness.attachments.types import ImageAttachmentRef
from agent_harness.context.builder import ContextBuilder
from agent_harness.context.tokens import (
    IMAGE_MAX_DIMENSION,
    IMAGE_TOKENS_BASE,
    IMAGE_TOKENS_PER_TILE,
    IMAGE_TOKENS_UNKNOWN_SIZE,
    estimate_message_tokens,
    estimate_tokens,
    image_tokens_for_size,
    image_tokens_in_message,
)
from agent_harness.session import USER_MESSAGE
from agent_harness.storage.artifact import FakeArtifactStore, compute_byte_artifact_id
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

_AID = "sha256:" + "a" * 64
#: 旧口径（#824 固定常量），仅用于消融对照。
_OLD_FIXED_TOKENS_PER_IMAGE = 1200


def _standard_block(width: int, height: int, *, file_id: str = _AID) -> dict:
    return {"type": "image", "file_id": file_id, "mime_type": "image/png",
            "width": width, "height": height}


# 无尺寸的 provider 块（装配后形态，形状由 provider 协议决定）。
_PROVIDER_BLOCK = {
    "type": "image_url",
    "image_url": {"url": "data:image/png;base64,AA", "detail": "auto"},
}


def test_formula_matches_openai_tile_structure() -> None:
    """公式 = base + per_tile × tiles（512px 方块）；tile 数与总值都是硬编码期望。"""
    # 8×6 → 1 tile；1024² → 4 tiles；2048² → 16 tiles。
    assert image_tokens_for_size(8, 6) == 255
    assert image_tokens_for_size(1024, 1024) == 765
    assert image_tokens_for_size(2048, 2048) == 2805
    # 常数本身也钉住（改常数会同时打破上面的硬编码值）。
    assert (IMAGE_TOKENS_BASE, IMAGE_TOKENS_PER_TILE) == (85, 170)


def test_formula_is_monotonic_in_size() -> None:
    """尺寸单调：更大的图不会算得更少（防"大图被低估"）。"""
    sizes = [(8, 6), (256, 256), (512, 512), (1024, 1024), (1536, 1536),
             (2048, 2048), (4096, 4096)]
    values = [image_tokens_for_size(w, h) for w, h in sizes]
    assert values == sorted(values)


def test_large_image_scaled_to_normalization_max_keeping_aspect() -> None:
    """长边超过归一化上限（2048）按**等比**缩放到上限——与发送前 `normalize` 同语义。"""
    # 正方形：4096² 缩到 2048²。
    assert image_tokens_for_size(4096, 4096) == image_tokens_for_size(
        IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION
    )
    # 2:1 长图：4000×2000 缩到 2048×1024（不是逐轴 clamp 到 2048×2048）；
    # 2048×1024 → 4×2 = 8 tiles。
    assert image_tokens_for_size(4000, 2000) == image_tokens_for_size(2048, 1024)
    assert image_tokens_for_size(2048, 1024) == 85 + 170 * 8
    # 极端长宽比：长边缩到 2048、短边保持比例（100 不放大）。
    assert image_tokens_for_size(8192, 100) == image_tokens_for_size(2048, 100)


def test_zero_dimension_only_base() -> None:
    assert image_tokens_for_size(0, 0) == IMAGE_TOKENS_BASE


def test_standard_block_counted_by_dimensions() -> None:
    """标准块带 width/height ⇒ 按该尺寸计费（不再固定）。"""
    msg = HumanMessage(content=[{"type": "text", "text": "看图"}, _standard_block(8, 6)])
    assert image_tokens_in_message(msg) == image_tokens_for_size(8, 6)
    big = HumanMessage(content=[{"type": "text", "text": "看图"}, _standard_block(2048, 2048)])
    assert image_tokens_in_message(big) == image_tokens_for_size(2048, 2048)


def test_projection_block_feeds_estimator() -> None:
    """投影产出的标准块（`image_content_block`）与估算口径对接：尺寸被真正读到。"""
    ref = ImageAttachmentRef(
        attachment_id=_AID, media_type="image/png", bytes=10, width=640, height=480,
    )
    block = image_content_block(ref)
    msg = HumanMessage(content=[{"type": "text", "text": "x"}, block])
    assert image_tokens_in_message(msg) == image_tokens_for_size(640, 480)


def test_provider_block_without_dimensions_falls_back_conservatively() -> None:
    """provider 块无尺寸字段 ⇒ 回退到 UNKNOWN，且该值对采样尺寸恒**保守**（≥ 真值）。"""
    msg = HumanMessage(content=[{"type": "text", "text": "x"}, _PROVIDER_BLOCK])
    assert image_tokens_in_message(msg) == IMAGE_TOKENS_UNKNOWN_SIZE
    # 真·保守判据（非同义反复）：回退值不低于一组尺寸各自算出的成本。
    for w, h in [(8, 6), (512, 512), (1024, 1024), (2048, 2048), (4000, 2000)]:
        assert IMAGE_TOKENS_UNKNOWN_SIZE >= image_tokens_for_size(w, h)


def test_standard_block_missing_dimensions_falls_back_conservatively() -> None:
    """标准块缺 width/height（如遗留 3 键块）⇒ 同走 UNKNOWN 回退，绝不少算。"""
    block = {"type": "image", "file_id": _AID, "mime_type": "image/png"}
    msg = HumanMessage(content=[{"type": "text", "text": "x"}, block])
    assert image_tokens_in_message(msg) == IMAGE_TOKENS_UNKNOWN_SIZE


@pytest.mark.parametrize("bad", [True, False, 3.5, -1, "512", None])
def test_non_negative_int_required_for_dimensions(bad) -> None:
    """非「非负 int」的 width（bool/float/负数/字符串/缺失）⇒ UNKNOWN 回退。"""
    block = _standard_block(512, 512)
    block["width"] = bad
    msg = HumanMessage(content=[{"type": "text", "text": "x"}, block])
    assert image_tokens_in_message(msg) == IMAGE_TOKENS_UNKNOWN_SIZE


def test_multiple_images_scale_with_each_size() -> None:
    blocks = [
        {"type": "text", "text": "x"},
        _standard_block(512, 512, file_id="sha256:" + "1" * 64),
        _standard_block(1024, 1024, file_id="sha256:" + "2" * 64),
    ]
    assert image_tokens_in_message(HumanMessage(content=blocks)) == (
        image_tokens_for_size(512, 512) + image_tokens_for_size(1024, 1024)
    )


def test_plain_text_message_count_unchanged() -> None:
    """AC9：纯文本（str / 只含文本块）的图片增量为 0，口径逐字不变。"""
    assert image_tokens_in_message(HumanMessage(content="只有文字")) == 0
    assert (
        image_tokens_in_message(HumanMessage(content=[{"type": "text", "text": "x"}])) == 0
    )


def test_message_estimate_adds_image_cost_on_top_of_structure() -> None:
    """整体估算 = 结构 token + 图片成本，残差不超过块自身 JSON 的大小。

    上下界各钉一侧：下界 `delta >= cost` 保证图片成本**全额**计入（hard guard 安全
    方向）；上界 `delta - cost <= 块 JSON 的 token 数` 保证残差只来自块序列化本身
    （含 71 字符 `file_id`/`mime_type`），公式没有额外放大或重复计费。
    """
    text_only = HumanMessage(content=[{"type": "text", "text": "看图"}])
    block = _standard_block(8, 6)
    with_image = HumanMessage(content=[{"type": "text", "text": "看图"}, block])
    cost = image_tokens_for_size(8, 6)
    delta = estimate_message_tokens([with_image]) - estimate_message_tokens([text_only])
    structural_upper = estimate_tokens(json.dumps(block, ensure_ascii=False))
    assert cost <= delta <= cost + structural_upper


def test_ablation_old_constant_misestimates_both_ends() -> None:
    """消融：旧固定 1200 对小图**高估**、对 2048² 大图**系统性低估**；新公式两侧都更正。"""
    small = image_tokens_for_size(8, 6)
    big = image_tokens_for_size(2048, 2048)
    # 小图：新公式低于旧常量（旧的高估）。
    assert small < _OLD_FIXED_TOKENS_PER_IMAGE
    # 大图：新公式高于旧常量（旧的低估），且低估倍数 > 2（旧常量对 2048² 明显不足）。
    assert big > _OLD_FIXED_TOKENS_PER_IMAGE
    assert big / _OLD_FIXED_TOKENS_PER_IMAGE > 2


def _png(width: int = 8, height: int = 6) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _session_with_image(tmp_path, *, width: int = 8, height: int = 6):
    session = make_session(tmp_path)
    data = _png(width, height)
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
                    "width": width,
                    "height": height,
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
    """视觉口径的 build 估算比非视觉口径多出（至少）这张图的**尺寸相关**近似成本。

    这是"图不计费导致 hard guard 失守"的直接回归：图片块的近似成本必须真的进
    `_estimate_tokens_cached` 的读数，且随尺寸变化。
    """
    session, store = await _session_with_image(tmp_path, width=8, height=6)

    vision = _builder(store, vision=True)
    non_vision = _builder(store, vision=False)
    await vision.build(session)
    await non_vision.build(session)

    assert (
        vision._token_estimate_total - non_vision._token_estimate_total
        >= image_tokens_for_size(8, 6)
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
    assert not any(
        isinstance(m.content, list)
        and any(isinstance(b, dict) and b.get("type") == "image" for b in m.content)
        for m in built
    )
    assert image_tokens_in_message(
        next(m for m in derive_messages(session.events) if isinstance(m.content, str))
    ) == 0
