"""#1353 AC7：pinning 堵 TOCTOU（存储层半边）。

stage 前把 ``old_text`` 选中的条目**全文** pin 住；approve/replay 时只接受
全文一致的条目。文件在两步之间被外部改过 ⇒ 拒绝，且不写入任何字节。
审批记录的保留由审批层用例（合并态）覆盖。
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
    read_raw,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture()
async def store(tmp_path):
    instance = build_store(tmp_path, memory_char_limit=600, user_char_limit=400)
    pending = load(instance)
    if inspect.isawaitable(pending):
        await pending
    return instance


async def test_resolve_entry_pins_full_text(store) -> None:
    assert (await call(store.add, "memory", "rule one"))["success"] is True
    resolved = await call(store.resolve_entry, "memory", "rule", "replace")
    assert resolved["success"] is True
    assert resolved["matched_entry"] == "rule one"


async def test_replace_with_pinned_entry_is_rejected_after_external_edit(store) -> None:
    assert (await call(store.add, "memory", "rule one"))["success"] is True
    resolved = await call(store.resolve_entry, "memory", "rule", "replace")
    pinned = resolved["matched_entry"]

    path = path_of(store, "memory")
    path.write_text("rule one, hand edited", encoding="utf-8")
    before = path.read_bytes()

    result = await call(
        store.replace, "memory", "rule", "rule one updated", matched_entry=pinned
    )

    assert result["success"] is False
    assert "no longer in memory" in result["error"] or "changed since" in result["error"]
    assert path.read_bytes() == before
    assert read_entries(path) == ["rule one, hand edited"]


async def test_pinned_entry_still_applies_when_file_unchanged(store) -> None:
    assert (await call(store.add, "memory", "rule one"))["success"] is True
    pinned = (await call(store.resolve_entry, "memory", "rule", "replace"))["matched_entry"]

    result = await call(
        store.replace, "memory", "rule", "rule one updated", matched_entry=pinned
    )

    assert result["success"] is True
    assert read_entries(path_of(store, "memory")) == ["rule one updated"]


async def test_batch_pins_each_entry_and_rejects_whole_batch_on_drift(store) -> None:
    assert (await call(store.add, "memory", "rule one"))["success"] is True
    assert (await call(store.add, "memory", "rule two"))["success"] is True
    resolved = await call(
        store.resolve_batch_entries,
        "memory",
        [
            {"action": "replace", "old_text": "rule one", "content": "rule one v2"},
            {"action": "remove", "old_text": "rule two"},
        ],
    )
    assert resolved["success"] is True
    matched = resolved["matched_entries"]
    assert matched == ["rule one", "rule two"]

    path = path_of(store, "memory")
    path.write_text("rule one\n§\nrule two, hand edited", encoding="utf-8")
    before = path.read_bytes()

    result = await call(
        store.apply_batch,
        "memory",
        [
            {"action": "replace", "old_text": "rule one", "content": "rule one v2", "matched_entry": matched[0]},
            {"action": "remove", "old_text": "rule two", "matched_entry": matched[1]},
        ],
        # 这些 pin 是 `resolve_batch_entries` 从磁盘钉住的全文，属系统 pin；
        # 只有重放通道认它们（模型输入见 #1382）。
        trusted_pins=True,
    )

    assert result["success"] is False
    assert path.read_bytes() == before
    assert "rule one v2" not in read_raw(path)


async def test_missing_pinned_entry_is_rejected_not_researched(store) -> None:
    assert (await call(store.add, "memory", "rule one"))["success"] is True
    path = path_of(store, "memory")
    path.write_text("rule one, hand edited", encoding="utf-8")

    result = await call(
        store.replace, "memory", "rule one", "rewritten", matched_entry="rule one"
    )

    assert result["success"] is False
    # 不得按 old_text 重新搜索到新条目（否则 pin 形同虚设）。
    assert read_entries(path) == ["rule one, hand edited"]
