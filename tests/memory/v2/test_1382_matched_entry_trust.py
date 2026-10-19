"""#1382：`matched_entry` 是**系统 pin**，模型输入不得采信。

信任边界必须落在存储层（`_batch_sync` / `_apply_batch_op`）或工具出口
（`operations_payload()`）——只修 `resolve_pins` 不够：`write_approval` 默认关，
直写路径根本不走它。模型只要在 batch op 里填 `matched_entry` 成另一条条目，
就能绕过 `old_text` 那条被改掉。

正控同样重要：审批重放携带的 pin 是 stage 时从磁盘钉住的全文，必须继续生效
（见 `test_1353_ac7_pinning.py` / `test_1353_ac7_approval.py`）。
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from tests.memory.v2._markdown_memory_1353 import (
    build_store,
    call,
    load,
    path_of,
    read_entries,
)

pytestmark = pytest.mark.asyncio

_TOOL_MODULES = ("agent_harness.memory.v2.memory_tool",)
_TOOL_CLASSES = ("MemoryTool", "MemoryWriteTool")


def _tool(store):
    for module_name in _TOOL_MODULES:
        module = importlib.import_module(module_name)
        for class_name in _TOOL_CLASSES:
            candidate = getattr(module, class_name, None)
            if isinstance(candidate, type):
                return candidate(store)
    pytest.fail("找不到 memory Tool 实现", pytrace=False)


async def _store(tmp_path):
    store = build_store(tmp_path)
    pending = load(store)
    if inspect.isawaitable(pending):
        await pending
    return store


async def test_tool_batch_cannot_forge_matched_entry(tmp_path) -> None:
    """直写路径（write_approval 默认关）：模型伪造的 pin 不许改动别的条目。"""
    store = await _store(tmp_path)
    assert (await call(store.add, "memory", "entry A: cats are nice"))["success"] is True
    assert (await call(store.add, "memory", "entry B: dogs are nice"))["success"] is True
    tool = _tool(store)
    before = path_of(store, "memory").read_bytes()

    result = await tool.execute(tool.args_schema(
        target="memory",
        operations=[
            {
                "action": "replace",
                "old_text": "nonexistent text",
                "content": "REPLACED",
                "matched_entry": "entry B: dogs are nice",
            },
        ],
    ))

    assert result.ok is False, result.message
    assert path_of(store, "memory").read_bytes() == before
    assert read_entries(path_of(store, "memory")) == [
        "entry A: cats are nice", "entry B: dogs are nice",
    ]


async def test_store_batch_ignores_model_matched_entry(tmp_path) -> None:
    """存储层自己就是边界：绕过工具直调也一样剥掉模型给的 pin。"""
    store = await _store(tmp_path)
    assert (await call(store.add, "memory", "entry A: cats are nice"))["success"] is True
    assert (await call(store.add, "memory", "entry B: dogs are nice"))["success"] is True

    result = await call(store.apply_batch, "memory", [
        {
            "action": "replace",
            "old_text": "entry A",
            "content": "REPLACED",
            "matched_entry": "entry B: dogs are nice",
        },
    ])

    # 按 old_text 定位 A；伪造的 pin 不生效。
    assert result["success"] is True, result
    assert read_entries(path_of(store, "memory")) == [
        "REPLACED", "entry B: dogs are nice",
    ]


async def test_resolve_batch_entries_ignores_model_matched_entry(tmp_path) -> None:
    """stage 前的解析同理：伪造 pin 不能让解析返回别的条目。"""
    store = await _store(tmp_path)
    assert (await call(store.add, "memory", "entry A: cats are nice"))["success"] is True
    assert (await call(store.add, "memory", "entry B: dogs are nice"))["success"] is True

    resolved = await call(store.resolve_batch_entries, "memory", [
        {
            "action": "replace",
            "old_text": "entry A",
            "content": "x",
            "matched_entry": "entry B: dogs are nice",
        },
    ])

    assert resolved["success"] is True, resolved
    assert resolved["matched_entries"] == ["entry A: cats are nice"]


async def test_trusted_pins_channel_still_works(tmp_path) -> None:
    """正控：审批重放通道（`trusted_pins=True`）必须继续认 pin。"""
    store = await _store(tmp_path)
    assert (await call(store.add, "memory", "entry A: cats are nice"))["success"] is True
    path = path_of(store, "memory")
    path.write_text("entry A: cats are nice, edited", encoding="utf-8")
    before = path.read_bytes()

    result = await call(store.apply_batch, "memory", [
        {
            "action": "replace",
            "old_text": "entry A",
            "content": "REPLACED",
            "matched_entry": "entry A: cats are nice",
        },
    ], trusted_pins=True)

    assert result["success"] is False, result
    assert path.read_bytes() == before
