"""#1353 AC4：容量预算 2200 / 1375 与三段式超限错误。

Hermes 文案形状：``Memory at <当前用量>/<上限> chars ...`` + ``current_entries``
（全部条目）。**单条路径**（add / replace）必须是这三段；**batch 中止路径**按上游
`memory_tool_store.py:239-243` 的明文契约**故意不带** `current_entries`（"the store did not
change and the model already has it"），只报「终态用量 / 上限」+ 整批未应用。两种形状都由
票面 §3 M3 的「三段式超限错误逐字搬」覆盖（oracle = 上游）。
"""

from __future__ import annotations

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


async def _store(tmp_path, memory_limit: int, user_limit: int):
    instance = build_store(tmp_path, memory_char_limit=memory_limit, user_char_limit=user_limit)
    pending = load(instance)
    if inspect.isawaitable(pending):
        await pending
    return instance


def _assert_three_part(result: dict, *, current: int, limit: int, entries: list[str]) -> None:
    """单条路径的超限错误必须同时给出「用量 / 上限 / 全部条目」。

    「用量」是**这次尝试最终会达到的**用量（add 是加上新条目后，replace 是改完后的终态）
    ——只报旧用量无法解释"为什么被拒"。`usage` 字段本身是**当前**占用，不参与本条断言
    （上游同形：文案带终态数字，`usage` 仍是活用量）。
    """
    assert result["success"] is False
    message = result["error"]
    assert f"{current:,}" in message or str(current) in message, message
    assert f"{limit:,}" in message or str(limit) in message, message
    assert result.get("current_entries") == entries


def _assert_batch_abort(result: dict, *, current: int, limit: int) -> None:
    """batch 终态超限：报「终态用量 / 上限」+ 整批未应用，且**不带** `current_entries`。

    依据上游 `memory_tool_store.py:239-243` 的函数注释（batch-abort failure WITHOUT
    ``current_entries``）与实测返回值；文件不变性由调用方断言。
    """
    assert result["success"] is False
    message = result["error"]
    assert f"{current:,}" in message or str(current) in message, message
    assert f"{limit:,}" in message or str(limit) in message, message
    assert "No operations were applied" in message, message
    assert "current_entries" not in result, f"batch 中止不应回显 current_entries：{result!r}"


async def test_default_limits_are_2200_and_1375(tmp_path) -> None:
    store = build_store(tmp_path)  # 不传 limit，验默认值本身
    pending = load(store)
    if inspect.isawaitable(pending):
        await pending

    assert (await call(store.add, "memory", "m" * 2200))["success"] is True
    assert (await call(store.add, "memory", "n"))["success"] is False
    assert (await call(store.add, "user", "u" * 1375))["success"] is True
    assert (await call(store.add, "user", "v"))["success"] is False


async def test_add_overflow_is_three_part_error(tmp_path) -> None:
    store = await _store(tmp_path / "add", memory_limit=120, user_limit=80)
    assert (await call(store.add, "memory", "a" * 60))["success"] is True
    assert (await call(store.add, "memory", "b" * 30))["success"] is True

    result = await call(store.add, "memory", "c" * 40)

    _assert_three_part(
        result,
        current=len("a" * 60 + "\n§\n" + "b" * 30),
        limit=120,
        entries=["a" * 60, "b" * 30],
    )
    # 失败不落盘。
    assert read_entries(path_of(store, "memory")) == ["a" * 60, "b" * 30]


async def test_replace_overflow_is_three_part_error(tmp_path) -> None:
    store = await _store(tmp_path / "replace", memory_limit=120, user_limit=80)
    assert (await call(store.add, "memory", "a" * 60))["success"] is True
    assert (await call(store.add, "memory", "b" * 30))["success"] is True
    before = read_entries(path_of(store, "memory"))

    result = await call(store.replace, "memory", "a" * 60, "a" * 110)

    _assert_three_part(result, current=len("a" * 110 + "\n§\n" + "b" * 30), limit=120, entries=before)
    assert read_entries(path_of(store, "memory")) == before


async def test_batch_overflow_checks_final_state_only(tmp_path) -> None:
    store = await _store(tmp_path / "batch", memory_limit=120, user_limit=80)
    assert (await call(store.add, "memory", "a" * 60))["success"] is True
    assert (await call(store.add, "memory", "b" * 40))["success"] is True

    # 先加后删：中间态 176 > 120、最终态 113 ≤ 120 ⇒ 必须成功（预算只看最终态）。
    ok = await call(
        store.apply_batch,
        "memory",
        [
            {"action": "add", "content": "c" * 70},
            {"action": "remove", "old_text": "a" * 60},
        ],
    )
    assert ok["success"] is True
    assert read_entries(path_of(store, "memory")) == ["b" * 40, "c" * 70]

    # 最终态超限 ⇒ 整批拒绝；错误带终态用量 / 上限，且整批未应用。
    before = read_entries(path_of(store, "memory"))
    bad = await call(
        store.apply_batch,
        "memory",
        [{"action": "add", "content": "d" * 40}],
    )
    _assert_batch_abort(
        bad,
        current=len("b" * 40 + "\n§\n" + "c" * 70 + "\n§\n" + "d" * 40),
        limit=120,
    )
    assert read_entries(path_of(store, "memory")) == before


async def test_user_limit_is_independent(tmp_path) -> None:
    store = await _store(tmp_path / "user", memory_limit=400, user_limit=60)
    assert (await call(store.add, "user", "u" * 50))["success"] is True
    result = await call(store.add, "user", "v" * 20)
    _assert_three_part(result, current=50, limit=60, entries=["u" * 50])
