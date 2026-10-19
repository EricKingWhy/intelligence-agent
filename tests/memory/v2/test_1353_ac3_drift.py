"""#1353 AC3：外部手改后的漂移处理（有意反转 Hermes 的拒绝语义）。

票面已批准偏差：Hermes 外部手改 → 拒绝覆盖；本票手改是合法操作 →
``.bak.<ts>`` 备份 → **以文件内容为准继续** → 通知用户。
"""

from __future__ import annotations

import inspect
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


async def test_well_formed_hand_edit_is_kept_and_file_wins(store) -> None:
    """格式合法的手改仍然以文件为准（不丢数据），但它不是漂移**信号**。

    Hermes 的信号是"文件与工具写出的形状不同"（round-trip 不一致 / 单条超过整文件上限，
    见 `_detect_external_drift`）；票面偏差只改**处置**（备份+继续，而非拒绝），不改信号。
    所以这里断言"内容不丢"，并把"不产生备份"也钉住——否则每次正常写入都会复制一份文件。
    """
    assert (await call(store.add, "memory", "tool entry"))["success"] is True
    path = path_of(store, "memory")
    hand_edited = read_raw(path) + "\n§\nhand-written fact"
    path.write_text(hand_edited, encoding="utf-8")

    result = await call(store.add, "memory", "after hand edit")

    assert result["success"] is True
    entries = read_entries(path)
    # 以文件为准继续：手改内容与本次新增都在，且没有丢工具旧条目。
    assert entries == ["tool entry", "hand-written fact", "after hand edit"]
    assert list(path.parent.glob(path.name + ".bak.*")) == []


async def test_non_roundtripping_hand_edit_is_backed_up_and_not_discarded(store) -> None:
    """无法 round-trip 的手改（这里用条目内尾随空格，人工编辑常见）必须先备份再继续。

    用 `replace` 触发：`add` 是 append-only（上游 `skip_drift=True`），本来就不会覆盖
    外来内容；会覆盖的只有 replace/remove，所以漂移备份只在这条路径上有意义。
    """
    assert (await call(store.add, "memory", "tool entry"))["success"] is True
    path = path_of(store, "memory")
    hand_edited = "tool entry   \n§\nhand-written fact"
    path.write_text(hand_edited, encoding="utf-8")

    result = await call(
        store.replace, "memory", "hand-written fact", "hand-written fact, updated"
    )

    assert result["success"] is True
    entries = read_entries(path)
    assert entries == ["tool entry", "hand-written fact, updated"]

    backups = sorted(path.parent.glob(path.name + ".bak.*"))
    assert len(backups) == 1, f"AC3 要求恰好一份 .bak.<ts>，实际: {backups}"
    assert _BAK.search(str(backups[0]))
    assert read_raw(backups[0]) == hand_edited

    # 通知用户：备份路径必须回给调用方（响应里可见）。
    assert result.get("drift_backup") == str(backups[0]), (
        f"AC3：漂移必须把备份路径回给调用方，实际响应：{result}"
    )


async def test_replace_after_hand_edit_uses_disk_entry(store) -> None:
    assert (await call(store.add, "memory", "tool entry"))["success"] is True
    path = path_of(store, "memory")
    path.write_text("tool entry\n§\nhand fact\n§\nsecond hand fact", encoding="utf-8")

    result = await call(store.replace, "memory", "second hand fact", "second hand fact, updated")

    assert result["success"] is True
    assert read_entries(path) == ["tool entry", "hand fact", "second hand fact, updated"]


async def test_clean_file_does_not_create_backup(store) -> None:
    assert (await call(store.add, "memory", "entry one"))["success"] is True
    assert (await call(store.add, "memory", "entry two"))["success"] is True
    path = path_of(store, "memory")
    assert list(path.parent.glob(path.name + ".bak.*")) == []


async def test_user_target_gets_same_drift_guarantee(store) -> None:
    assert (await call(store.add, "user", "Some preference."))["success"] is True
    path = path_of(store, "user")
    hand_edited = "Some preference.  \n§\nhand-written preference"
    path.write_text(hand_edited, encoding="utf-8")

    result = await call(
        store.replace, "user", "hand-written preference", "another preference"
    )

    assert result["success"] is True
    assert read_entries(path) == ["Some preference.", "another preference"]
    backups = list(path.parent.glob(path.name + ".bak.*"))
    assert len(backups) == 1
    assert read_raw(backups[0]) == hand_edited
    assert result.get("drift_backup") == str(backups[0])
