"""#1353 AC3：外部手改后的漂移处理（有意反转 Hermes 的拒绝语义）。

票面已批准偏差：Hermes 外部手改 → 拒绝覆盖；本票手改是合法操作 →
``.bak.<ts>`` 备份 → **以文件内容为准继续** → 通知用户。
"""

from __future__ import annotations

import inspect
import logging
import re

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

_BAK = re.compile(r"\.bak\.\d+$")


@pytest_asyncio.fixture()
async def store(tmp_path):
    instance = build_store(tmp_path, memory_char_limit=400, user_char_limit=250)
    pending = load(instance)
    if inspect.isawaitable(pending):
        await pending
    return instance


async def test_hand_edit_is_backed_up_and_file_wins(store, caplog) -> None:
    assert (await call(store.add, "memory", "tool entry"))["success"] is True
    path = path_of(store, "memory")
    hand_edited = read_raw(path) + "\n§\nhand-written fact"
    path.write_text(hand_edited, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agent_harness"):
        result = await call(store.add, "memory", "after hand edit")

    assert result["success"] is True
    entries = read_entries(path)
    # 以文件为准继续：手改内容与本次新增都在，且没有丢工具旧条目。
    assert entries == ["tool entry", "hand-written fact", "after hand edit"]

    backups = sorted(path.parent.glob(path.name + ".bak.*"))
    assert len(backups) == 1, f"AC3 要求恰好一份 .bak.<ts>，实际: {backups}"
    assert _BAK.search(str(backups[0]))
    assert read_raw(backups[0]) == hand_edited

    # 通知用户：备份路径必须回给调用方，且有一条可见告警。
    assert result.get("drift_backup") == str(backups[0]) or str(backups[0]) in str(result)
    assert any(
        "drift" in record.getMessage().lower() or ".bak" in record.getMessage()
        for record in caplog.records
    ), "AC3：漂移必须产生用户可见通知/告警"


async def test_non_roundtripping_hand_edit_is_not_discarded(store) -> None:
    """无法 round-trip 的自由文本也不能被覆盖：先备份，再以文件内容继续。"""
    assert (await call(store.add, "memory", "tool entry"))["success"] is True
    path = path_of(store, "memory")
    freeform = "\n\n## Vendor Master\n" + "x" * 120 + "\n\n## Pin Board\n" + "y" * 120
    path.write_text(read_raw(path) + freeform, encoding="utf-8")

    result = await call(store.add, "memory", "new fact")

    assert result["success"] is True
    raw_after = read_raw(path)
    assert "## Vendor Master" in raw_after
    assert "new fact" in raw_after
    backups = list(path.parent.glob(path.name + ".bak.*"))
    assert len(backups) == 1
    assert "## Vendor Master" in read_raw(backups[0])


async def test_replace_after_hand_edit_uses_disk_entry(store) -> None:
    assert (await call(store.add, "memory", "tool entry"))["success"] is True
    path = path_of(store, "memory")
    path.write_text("tool entry\n§\nhand fact\n§\nsecond hand fact", encoding="utf-8")

    result = await call(store.replace, "memory", "second hand fact", "second hand fact, updated")

    assert result["success"] is True
    assert read_entries(path) == ["tool entry", "hand fact", "second hand fact, updated"]
    assert len(list(path.parent.glob(path.name + ".bak.*"))) == 1


async def test_clean_file_does_not_create_backup(store) -> None:
    assert (await call(store.add, "memory", "entry one"))["success"] is True
    assert (await call(store.add, "memory", "entry two"))["success"] is True
    path = path_of(store, "memory")
    assert list(path.parent.glob(path.name + ".bak.*")) == []


async def test_user_target_gets_same_drift_guarantee(store) -> None:
    assert (await call(store.add, "user", "Some preference."))["success"] is True
    path = path_of(store, "user")
    path.write_text(read_raw(path) + "\n§\nhand-written preference", encoding="utf-8")

    result = await call(store.replace, "user", "Some preference", "New preference.")

    assert result["success"] is True
    assert read_entries(path) == ["New preference", "hand-written preference"]
    assert len(list(path.parent.glob(path.name + ".bak.*"))) == 1
