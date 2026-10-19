"""#1353 AC1：`memory` add / replace / remove / batch 的存储行为。

Oracle：Hermes `tests/tools/test_memory_tool.py`（commit b324b1f7）的对应
用例；本票有意偏差只有“写入面换成 markdown 权威”，条目语义不变。
票面额外要求：``old_text`` 0 命中 / ≥2 命中时必须回 ``current_entries``。
"""

from __future__ import annotations

import inspect

import pytest
import pytest_asyncio

from agent_harness.memory.v2.memory_tool import _MemoryArgs
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
    return await store.apply_batch(target, operations)


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


async def test_ambiguous_match_returns_every_competing_entry(store) -> None:
    await _add(store, "memory", "server A runs nginx")
    await _add(store, "memory", "server B runs nginx")

    result = await _replace(store, "memory", "nginx", "apache")

    assert result["success"] is False
    assert "Multiple" in result["error"]

    # 判据只锁「模型能自纠」这件事：≥2 命中必须回**全部候选条目**，而不是只说“有歧义”。
    # ⚠ 票面 §6 AC1 / §3 M1 的散文写「≥2 命中返回 `current_entries`」，而上游 oracle
    # （`memory_tool_store.py:329-331`，票面同时要求逐行搬）用的是 `matches=[...]`（截断 80 字符）；
    # 两处口径不一致已上报，本条不替任一方拍板：两种形状都接受，但候选集必须完整，
    # 且**互斥**——实现必须二选一并自洽，不允许两种键同时出现（Stds-P3）。
    has_current = "current_entries" in result
    has_matches = "matches" in result
    assert has_current != has_matches, f"current_entries 与 matches 必须互斥，实际：{result!r}"
    candidates = result.get("current_entries")
    if candidates is None:
        matches = result.get("matches")
        assert matches, f"≥2 命中必须回候选集（current_entries 或 matches）：{result!r}"
        candidates = [text.removesuffix("...") for text in matches]
    assert candidates == ["server A runs nginx", "server B runs nginx"]

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


def _enum_sets(node, found: list[frozenset]) -> list[frozenset]:
    """递归收集 JSON Schema 里所有 `enum`（pydantic 会把它们放进 `$defs` / `allOf`）。"""
    if isinstance(node, dict):
        enum = node.get("enum")
        if isinstance(enum, list):
            found.append(frozenset(enum))
        for value in node.values():
            _enum_sets(value, found)
    elif isinstance(node, list):
        for value in node:
            _enum_sets(value, found)
    return found


def _memory_schema() -> dict:
    """`memory` 工具参数形状的真实落点：`MemoryTool.args_schema` 返回 `_MemoryArgs`。

    M1 要求"照搬 `MEMORY_SCHEMA` 的 description + 参数形状"，同时把 dispatch 换成
    Owlet `Tool` + pydantic `args_schema`——形状唯一钉死在 `_MemoryArgs`，直连。
    """
    return _MemoryArgs.model_json_schema()


def _enum_sets(node, found: list[frozenset]) -> list[frozenset]:
    """递归收集 JSON Schema 里所有 `enum`（pydantic 会把它们放进 `$defs` / `allOf`）。"""
    if isinstance(node, dict):
        enum = node.get("enum")
        if isinstance(enum, list):
            found.append(frozenset(enum))
        for value in node.values():
            _enum_sets(value, found)
    elif isinstance(node, list):
        for value in node:
            _enum_sets(value, found)
    return found


async def test_tool_schema_exposes_memory_action_and_target_enums(store) -> None:
    """M1：`memory` 工具的参数形状必须覆盖 add/replace/remove/batch × memory/user。"""
    schema = _memory_schema()
    parameters = schema.get("parameters", schema)
    enums = _enum_sets(parameters, [])

    assert frozenset({"add", "replace", "remove", "batch"}) in enums, (
        f"AC1：schema 里没有 action 的四个取值；实际 enum 集合：{enums}"
    )
    assert frozenset({"memory", "user"}) in enums, (
        f"AC1：schema 里没有 target 的两个取值；实际 enum 集合：{enums}"
    )
