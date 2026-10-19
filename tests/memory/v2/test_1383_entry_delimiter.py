"""#1383：含保留分隔符 ``\\n§\\n`` 的内容一律**拒绝**（fail closed，不转义）。

条目分隔符是存储格式的一部分：一段 content 若含它，落盘后重载就变成两条独立
记忆，而 pin / digest / 去重都按正文逐字相等判定——转义会让磁盘文本 ≠ 记忆正文，
所以唯一安全的选择是拒绝，让模型拆成多次 add 自纠。

四个写面全覆盖：单条 add、单条 replace、batch add、batch replace。

判定按**读侧口径**（评审 D1/D2、评审 B-F1）：写侧 ``newline=""`` 原样保留 ``\r``，
读侧 ``read_text`` 把 CRLF/CR 折成 LF；``normalize_entry`` 又 strip 掉首尾空白。
所以 ``"a\r\n§\r\nb"`` 与 ``"foo\n§\n"`` 落盘后同样会分裂——两者都必须拒。
"""

from __future__ import annotations

import inspect

import pytest

from agent_harness.memory.v2.markdown_store import ENTRY_DELIMITER
from agent_harness.memory.v2.migration import migrate_active_records
from tests.memory.v2._markdown_memory_1353 import (
    call,
    loaded_store,
    path_of,
    read_raw,
)
from tests.memory.v2._records import insert_rows, raw_row

pytestmark = pytest.mark.asyncio

_SPLIT = f"part1{ENTRY_DELIMITER}part2"


#: 按**读侧口径**会变成分隔符的同形变体（评审 D1/D2、评审 B-F1）。
_FORGED = (
    f"part1{ENTRY_DELIMITER}part2",
    "legit\r\n§\r\ninjected",
    "legit\r§\rinjected",
    "foo\n§\n",
)


def _assert_blocked(response: dict) -> None:
    assert response["success"] is False, response
    assert "reserved entry delimiter" in response["error"], response


@pytest.mark.parametrize("content", _FORGED)
async def test_add_rejects_delimiter_and_leaves_file_unchanged(tmp_path, content) -> None:
    store = await loaded_store(tmp_path)
    assert (await call(store.add, "memory", "seed"))["success"] is True
    before = path_of(store, "memory").read_bytes()

    _assert_blocked(await call(store.add, "memory", content))

    assert path_of(store, "memory").read_bytes() == before
    assert (await call(store.entries, "memory")) == ["seed"]


async def test_trailing_half_delimiter_does_not_swallow_next_entry(tmp_path) -> None:
    """评审 D2/B-F1 的实害：``"x\n§"`` 若被接受，下一条 add 会被并进本条。"""
    store = await loaded_store(tmp_path)
    assert (await call(store.add, "memory", "first"))["success"] is True
    _assert_blocked(await call(store.add, "memory", "x\n§"))

    assert (await call(store.add, "memory", "tail"))["success"] is True
    assert (await call(store.entries, "memory")) == ["first", "tail"]


@pytest.mark.parametrize("content", _FORGED)
async def test_replace_rejects_delimiter_and_leaves_file_unchanged(tmp_path, content) -> None:
    store = await loaded_store(tmp_path)
    assert (await call(store.add, "memory", "seed"))["success"] is True
    before = path_of(store, "memory").read_bytes()

    _assert_blocked(await call(store.replace, "memory", "seed", content))

    assert path_of(store, "memory").read_bytes() == before
    assert (await call(store.entries, "memory")) == ["seed"]


async def test_batch_add_rejects_delimiter(tmp_path) -> None:
    store = await loaded_store(tmp_path)
    assert (await call(store.add, "memory", "seed"))["success"] is True
    before = path_of(store, "memory").read_bytes()

    response = await call(
        store.apply_batch, "memory",
        [{"action": "add", "content": "fine"}, {"action": "add", "content": _SPLIT}],
    )

    _assert_blocked(response)
    assert path_of(store, "memory").read_bytes() == before


@pytest.mark.parametrize("content", _FORGED)
async def test_batch_replace_rejects_delimiter_via_new_text_alias(tmp_path, content) -> None:
    store = await loaded_store(tmp_path)
    assert (await call(store.add, "memory", "seed"))["success"] is True
    before = path_of(store, "memory").read_bytes()

    response = await call(
        store.apply_batch, "memory",
        [{"action": "replace", "old_text": "seed", "new_text": content}],
    )

    _assert_blocked(response)
    assert path_of(store, "memory").read_bytes() == before


async def test_batch_remove_with_delimiter_content_is_not_rejected(tmp_path) -> None:
    """评审 D3/B-F5：remove 的 content 不落盘，拿它拒绝一次合法删除是误伤。"""
    store = await loaded_store(tmp_path)
    assert (await call(store.add, "memory", "stale"))["success"] is True
    assert (await call(store.add, "memory", "kept"))["success"] is True

    response = await call(
        store.apply_batch, "memory",
        [{"action": "remove", "old_text": "stale", "content": _SPLIT}],
    )

    assert response["success"] is True, response
    assert (await call(store.entries, "memory")) == ["kept"]


async def test_batch_none_op_returns_error_not_exception(tmp_path) -> None:
    """评审 B-F2：基线对 ``None`` op 返回错误 dict；容错不能退化成 TypeError。"""
    store = await loaded_store(tmp_path)
    assert (await call(store.add, "memory", "seed"))["success"] is True
    before = path_of(store, "memory").read_bytes()

    response = await call(store.apply_batch, "memory", [None])

    assert response["success"] is False, response
    assert path_of(store, "memory").read_bytes() == before


async def test_migration_delimiter_row_is_skipped_not_split(tmp_path) -> None:
    """迁移侧沿用同一拒绝语义：分裂行进 `skipped`（CLI 据此非零退出），不进文件。"""
    from agent_harness.memory.v2.store import SqliteMemoryV2Store

    database = tmp_path / "memory-v2.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SqliteMemoryV2Store(database)
    await store.initialize()
    closer = getattr(store, "aclose", None) or getattr(store, "close", None)
    if closer is not None:
        result = closer()
        if inspect.isawaitable(result):
            await result

    insert_rows(database, [raw_row("m1", _SPLIT), raw_row("m2", "kept")])

    result = migrate_active_records(database, workspace)
    if inspect.isawaitable(result):
        result = await result

    assert [memory_id for memory_id, _ in result["skipped"]] == ["m1"], result
    assert result["exported"] == 1, result

    memory_file = next(workspace.rglob("MEMORY.md"))
    raw = read_raw(memory_file)
    assert [e for e in raw.split(ENTRY_DELIMITER) if e.strip()] == ["kept"]
