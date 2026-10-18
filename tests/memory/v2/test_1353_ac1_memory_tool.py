"""#1353 AC1：`memory` add / replace / remove / batch 的存储行为。

Oracle：Hermes `tests/tools/test_memory_tool.py`（commit b324b1f7）的对应
用例；本票有意偏差只有“写入面换成 markdown 权威”，条目语义不变。
票面额外要求：``old_text`` 0 命中 / ≥2 命中时必须回 ``current_entries``。
"""

from __future__ import annotations

import inspect

import pytest
import pytest_asyncio

from tests.memory.v2._markdown_memory_1353 import (
    build_store,
    call,
    load,
    path_of,
    read_entries,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def store(tmp_path):
    instance = build_store(tmp_path)
    pending = load(instance)
    if inspect.isawaitable(pending):
        await pending
    return instance


async def _add(store, target: str, content: str):
    return await call(store.add, target, content)


async def _replace(store, target: str, old_text: str, content: str, **kwargs):
    return await call(store.replace, target, old_text, content, **kwargs)


async def _remove(store, target: str, old_text: str, **kwargs):
    return await call(store.remove, target, old_text, **kwargs)


async def _batch(store, target: str, operations: list[dict]):
    return await call(store.apply_batch, target, operations)


async def test_add_writes_entry_to_markdown(store) -> None:
    result = await _add(store, "memory", "Python 3.12 project")
    assert result["success"] is True
    assert result["target"] == "memory"
    assert "Python 3.12 project" in read_entries(path_of(store, "memory"))

    result = await _add(store, "user", "Name: Alice")
    assert result["success"] is True
    assert result["target"] == "user"
    assert "Name: Alice" in read_entries(path_of(store, "user"))


async def test_add_duplicate_is_noop_not_duplicate(store) -> None:
    assert (await _add(store, "memory", "fact A"))["success"] is True
    result = await _add(store, "memory", "fact A")
    assert result["success"] is True
    assert read_entries(path_of(store, "memory")).count("fact A") == 1


async def test_replace_replaces_whole_entry_and_reports_overwritten_text(store) -> None:
    original = "RULE A: gate merges. RULE B: ci per HEAD. RULE C: never squash."
    await _add(store, "memory", original)

    result = await _replace(store, "memory", "RULE B: ci per HEAD.", "RULE B: CI is per-head.")

    assert result["success"] is True
    assert read_entries(path_of(store, "memory")) == ["RULE B: CI is per-head."]
    assert result["replaced_entry"] == original


async def test_remove_reports_removed_entry(store) -> None:
    await _add(store, "memory", "temporary note")
    result = await _remove(store, "memory", "temporary")
    assert result["success"] is True
    assert result["removed_entry"] == "temporary note"
    assert read_entries(path_of(store, "memory")) == []


async def test_zero_match_returns_current_entries(store) -> None:
    await _add(store, "memory", "fact A")
    result = await _remove(store, "memory", "nonexistent")

    assert result["success"] is False
    assert "No entry matched" in result["error"]
    assert result["current_entries"] == ["fact A"]


async def test_ambiguous_match_returns_current_entries(store) -> None:
    await _add(store, "memory", "server A runs nginx")
    await _add(store, "memory", "server B runs nginx")

    result = await _replace(store, "memory", "nginx", "apache")

    assert result["success"] is False
    assert "Multiple" in result["error"]
    # 票面 AC1 明确要求 ≥2 命中时也回 current_entries（让模型自纠）。
    assert result["current_entries"] == ["server A runs nginx", "server B runs nginx"]
    assert read_entries(path_of(store, "memory")) == [
        "server A runs nginx",
        "server B runs nginx",
    ]


async def test_exact_whole_entry_match_beats_substring_collision(store) -> None:
    await _add(store, "memory", "test")
    await _add(store, "memory", "echo-reply tests pass via local twins and are false positives")

    result = await _remove(store, "memory", "test")

    assert result["success"] is True
    assert read_entries(path_of(store, "memory")) == [
        "echo-reply tests pass via local twins and are false positives"
    ]


async def test_batch_is_atomic_and_uses_final_state_budget(store) -> None:
    await _add(store, "memory", "stale one")
    await _add(store, "memory", "stale two")

    result = await _batch(
        store,
        "memory",
        [
            {"action": "remove", "old_text": "stale one"},
            {"action": "remove", "old_text": "stale two"},
            {"action": "add", "content": "fresh durable fact"},
        ],
    )

    assert result["success"] is True
    assert read_entries(path_of(store, "memory")) == ["fresh durable fact"]


async def test_batch_rejects_whole_batch_when_one_op_is_invalid(store) -> None:
    await _add(store, "memory", "fact A")
    before = read_entries(path_of(store, "memory"))

    result = await _batch(
        store,
        "memory",
        [
            {"action": "add", "content": "legit fact"},
            {"action": "remove", "old_text": "this substring is absent"},
        ],
    )

    assert result["success"] is False
    assert read_entries(path_of(store, "memory")) == before


async def test_batch_duplicate_add_is_noop(store) -> None:
    await _add(store, "memory", "already here")
    result = await _batch(
        store,
        "memory",
        [
            {"action": "add", "content": "already here"},
            {"action": "add", "content": "brand new"},
        ],
    )
    assert result["success"] is True
    entries = read_entries(path_of(store, "memory"))
    assert entries.count("already here") == 1
    assert "brand new" in entries


async def test_batch_refuses_to_empty_a_non_empty_store(store) -> None:
    await _add(store, "memory", "fact A")
    before = read_entries(path_of(store, "memory"))

    result = await _batch(store, "memory", [{"action": "remove", "old_text": "fact A"}])

    assert result["success"] is False
    assert read_entries(path_of(store, "memory")) == before


async def test_user_and_memory_targets_stay_separate(store) -> None:
    await _add(store, "memory", "environment fact")
    await _add(store, "user", "user preference")

    assert read_entries(path_of(store, "memory")) == ["environment fact"]
    assert read_entries(path_of(store, "user")) == ["user preference"]
    assert path_of(store, "memory").name == "MEMORY.md"
    assert path_of(store, "user").name == "USER.md"


async def test_tool_schema_exposes_memory_action_and_target_enums(store) -> None:
    """M1：`memory` 工具的参数形状必须覆盖 add/replace/remove/batch × memory/user。"""
    module = inspect.getmodule(type(store))
    assert module is not None
    schema = getattr(module, "MEMORY_SCHEMA", None) or getattr(module, "MEMORY_TOOL_SCHEMA", None)
    assert schema is not None, (
        "AC1：实现未暴露 MEMORY_SCHEMA（Hermes 照搬面要求参数形状可对账）"
    )
    params = schema.get("parameters", schema)
    assert "allOf" not in params and "anyOf" not in params and "oneOf" not in params
    properties = params["properties"]
    assert set(properties["action"]["enum"]) == {"add", "replace", "remove", "batch"}
    assert set(properties["target"]["enum"]) == {"memory", "user"}
