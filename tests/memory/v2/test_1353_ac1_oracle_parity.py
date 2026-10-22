"""#1353 AC1 的 oracle 补全：上游 `tests/tools/test_memory_tool.py` 里还没迁过来的行为。

AC1 的口径是「按行为契约对齐」，不是机械逐条搬迁：上游 `test_memory_tool.py` 有 57 个
test 方法，本仓 AC1 两份文件共 24 条（本文件 11 + `test_1353_ac1_memory_tool.py` 13）。
条数差来自合并同判据用例与不适用项（上游 agent-fork 编排相关），行为覆盖已由审查确认
在位；口径修订留痕见 #1353 的「偏差清单追认」记录（#1441 §6 / F-16）。
`test_1353_ac1_memory_tool.py` 覆盖 add/replace/remove/batch 的主契约；
本文件补上游同一 oracle 中**判据不同**的那几类：

- 文本边界：BOM、去重、超限文件只告警不截断、读不出来时拒写；
- 失败预算：零命中的自纠提示按 per-turn cap 退化为终止态，成功与轮次边界重置预算；
- 工具层参数：`new_text` 是 `content` 的别名、两者同时出现时 `content` 优先、
  关闭的 target 返回 PERMISSION_DENIED。

这些都不改实现语义，只把"上游测过、本票仍要求一致"的条目搬成可执行判据。
"""

from __future__ import annotations

import inspect

import pytest

from agent_harness.memory.v2.markdown_store import MAX_CONSOLIDATION_FAILURES_PER_TURN
from agent_harness.tooling import ErrorCode
from tests.memory.v2._markdown_memory_1353 import (
    build_store,
    build_tool,
    call,
    load,
    path_of,
    read_entries,
)

pytestmark = pytest.mark.asyncio

async def _load(store) -> None:
    pending = load(store)
    if inspect.isawaitable(pending):
        await pending


# --------------------------------------------------------------------------------------
# 文本边界（上游 TestMemoryStorePersistence / load 相关用例）
# --------------------------------------------------------------------------------------


async def test_bom_is_stripped_from_first_entry(tmp_path) -> None:
    """上游 #10878：记事本写的 BOM 不能永久粘在第一条上。"""
    store = build_store(tmp_path)
    path = path_of(store, "memory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("\ufefffirst fact\n§\nsecond fact".encode())

    await _load(store)

    entries = store.entries("memory")
    assert entries == ["first fact", "second fact"], entries
    assert "\ufeff" not in entries[0]


async def test_bom_file_add_keeps_existing_entry_intact(tmp_path) -> None:
    store = build_store(tmp_path)
    path = path_of(store, "memory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("\ufefffirst fact".encode())

    result = await call(store.add, "memory", "second fact")

    assert result["success"] is True
    assert read_entries(path) == ["first fact", "second fact"]


async def test_duplicate_entries_are_deduplicated_on_load(tmp_path) -> None:
    store = build_store(tmp_path)
    path = path_of(store, "memory")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("same fact\n§\nsame fact\n§\nother fact", encoding="utf-8")

    await _load(store)

    assert store.entries("memory") == ["same fact", "other fact"]


async def test_over_limit_file_loads_without_truncation(tmp_path) -> None:
    """上游 #10877：外部写入超限只在写得进的时候被拒，加载时绝不截断用户记忆。"""
    store = build_store(tmp_path)
    path = path_of(store, "memory")
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = "x" * 2500
    path.write_text(entry, encoding="utf-8")

    await _load(store)

    assert store.entries("memory") == [entry]


async def test_unreadable_file_refuses_write_and_keeps_bytes(tmp_path) -> None:
    """读不出来时不许“当作空文件”整体重写（那等于静默覆盖真实字节）。"""
    store = build_store(tmp_path)
    path = path_of(store, "memory")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b"\xff\xfe\x00broken \x80 bytes"
    path.write_bytes(original)

    result = await call(store.add, "memory", "new fact")

    assert result["success"] is False
    assert path.read_bytes() == original


# --------------------------------------------------------------------------------------
# 失败预算（上游 TestMemoryConsolidationGracefulDegrade）
# --------------------------------------------------------------------------------------


async def test_zero_match_failures_degrade_after_cap(tmp_path) -> None:
    store = build_store(tmp_path)
    assert (await call(store.add, "memory", "fact A"))["success"] is True
    cap = MAX_CONSOLIDATION_FAILURES_PER_TURN

    for _ in range(cap):
        result = await call(store.replace, "memory", "nonexistent", "new")
        assert result["success"] is False
        assert "current_entries" in result, "cap 之内必须还能自纠"

    degraded = await call(store.replace, "memory", "nonexistent", "new")
    assert degraded["success"] is False
    assert degraded.get("done") is True
    assert "current_entries" not in degraded


async def test_batch_failures_count_toward_the_same_budget(tmp_path) -> None:
    store = build_store(tmp_path)
    assert (await call(store.add, "memory", "fact A"))["success"] is True
    cap = MAX_CONSOLIDATION_FAILURES_PER_TURN
    bad_batch = [{"action": "replace", "old_text": "nope", "content": "x"}]

    for _ in range(cap):
        assert (await call(store.apply_batch, "memory", bad_batch))["success"] is False

    degraded = await call(store.apply_batch, "memory", bad_batch)
    assert degraded["success"] is False
    assert degraded.get("done") is True


async def test_success_resets_failure_budget(tmp_path) -> None:
    store = build_store(tmp_path)
    assert (await call(store.add, "memory", "real entry"))["success"] is True
    cap = MAX_CONSOLIDATION_FAILURES_PER_TURN
    for _ in range(cap):
        await call(store.replace, "memory", "nonexistent", "new")

    assert (await call(store.replace, "memory", "real entry", "updated"))["success"] is True

    fresh = await call(store.replace, "memory", "nonexistent", "new")
    assert "current_entries" in fresh
    assert fresh.get("done") is not True


# --------------------------------------------------------------------------------------
# 工具层参数（上游 TestMemoryToolArguments）
# --------------------------------------------------------------------------------------


async def test_new_text_alias_works_for_add_and_replace(tmp_path) -> None:
    store = build_store(tmp_path)
    tool = build_tool(store)
    shape = tool.args_schema

    added = await tool.execute(shape(target="memory", action="add", new_text="alias fact"))
    assert added.ok is True, added.message
    assert "alias fact" in read_entries(path_of(store, "memory"))

    replaced = await tool.execute(
        shape(target="memory", action="replace", old_text="alias fact", new_text="renamed fact")
    )
    assert replaced.ok is True, replaced.message
    assert read_entries(path_of(store, "memory")) == ["renamed fact"]


async def test_content_wins_when_both_content_and_new_text_are_set(tmp_path) -> None:
    store = build_store(tmp_path)
    tool = build_tool(store)
    shape = tool.args_schema

    result = await tool.execute(
        shape(target="memory", action="add", content="primary content", new_text="alias content")
    )

    assert result.ok is True, result.message
    assert read_entries(path_of(store, "memory")) == ["primary content"]


async def test_disabled_target_returns_permission_denied(tmp_path) -> None:
    store = build_store(tmp_path)
    store.user_profile_enabled = False
    tool = build_tool(store)

    result = await tool.execute(
        tool.args_schema(target="user", action="add", content="profile fact")
    )

    assert result.ok is False
    assert result.error_code == ErrorCode.PERMISSION_DENIED
    assert not path_of(store, "user").exists()
