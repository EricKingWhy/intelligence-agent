"""#616 存储层契约：`session_budgets.tool_call_limits` 陈旧名的清除通道。

待清对象是 **ceiling 表** `tool_call_limits`（"对某工具名的调用上限"），不是消耗事实表
`tool_calls_by_tool` / `tool_attempts_by_tool`（历史事实，必须原样保留）。`session_budgets`
本就可 UPDATE（merge-only 指的是 `ensure_session_budget` 并入策略，不是表不可改）。

**本文件钉的接口（#616 第一轮，用户已按推荐拍板，按此固定）**：

    SqliteDelegationTreeLedger.purge_session_tool_limits(
        budget_key: str, names: Iterable[str], source: str = "unknown",
    ) -> SessionToolLimitsPurge

字段：`.purged`（被清名 → 原 ceiling）、`.remaining`（清后仍未限名的工具）、`.rows`
（受影响行数，无变更 = 0）、`.version`（清后的账行版本）。

语义要点（TDD 契约，实现缺失时本文件整批红）：

- **只清点名且在账的键**：`names` 是调用方（服务层，掌握根 registry）判定为陈旧的候选名；
  存储层只删这些名字里**确实在 `tool_call_limits` 中**的，其余名字（正常注册名）一律不动。
- **单事务、append-only 审计、version bump**：删键 + 追加 `session_budget_events`
  (kind=`tool_limits_purged`) + `version + 1` 在同一事务里提交；审计 detail 带被清名与剩余表。
- **幂等**：重跑（名字已不在）返回空、`rows == 0`、**不 bump version、不落新事件**。
- **不动消耗事实**：`tool_calls_by_tool` / `tool_attempts_by_tool` 一个字节不改。
- **未知 budget_key**：抛 `KeyError`（与 `update_session_limits` 同一口径）。

为什么单测只跑 Sqlite 实现：本票点名的存储层契约就是 `SqliteDelegationTreeLedger`——
`session_budget_events` 审计表本身是 SQLite 专属机制（InMemory 的
`session_event_kinds` 恒返回空列表）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_harness.agent.run_budget import SessionLimits
from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger


def _ledger(tmp_path) -> SqliteDelegationTreeLedger:
    return SqliteDelegationTreeLedger(tmp_path / "harness.db")


def _audit_rows(tmp_path, budget_key: str) -> list[tuple[str, int, str | None]]:
    """append-only 审计行的 (kind, version, detail) 序（按写入顺序）。"""
    with sqlite3.connect(tmp_path / "harness.db") as connection:
        return connection.execute(
            "SELECT kind, version, detail FROM session_budget_events "
            "WHERE budget_key = ? ORDER BY rowid",
            (budget_key,),
        ).fetchall()


# ── 核心：只清点名陈旧键，正常名与其余 ceiling 一个不动 ──────────────────


@pytest.mark.asyncio
async def test_purge_removes_only_named_stale_keys_and_keeps_the_rest(tmp_path):
    """点名陈旧名 `ghost` 从表里摘掉；同表的正常名 `glob` 原样保留。"""
    ledger = _ledger(tmp_path)
    await ledger.initialize()
    key = "sess-purge"
    await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(tool_call_limits={"ghost": 5, "glob": 9}),
    )

    result = await ledger.purge_session_tool_limits(key, {"ghost"}, source="web")

    assert result.purged == {"ghost": 5}
    assert result.remaining == {"glob": 9}
    assert result.rows == 1, "清了键 ⇒ 账行被写了一次"
    assert result.version == 2

    # 落盘读数：陈旧名真的没了，正常名还在
    snapshot = await ledger.get_session_budget(key)
    assert snapshot.limits.tool_call_limits == {"glob": 9}
    assert snapshot.version == 2


@pytest.mark.asyncio
async def test_purge_leaves_non_tool_ceilings_untouched(tmp_path):
    """非工具维的 ceiling（turns / delegations 等）不属于清除面，一个都不动。"""
    ledger = _ledger(tmp_path)
    await ledger.initialize()
    key = "sess-purge-ceilings"
    await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(
            max_agent_turns_total=10,
            max_delegations=4,
            tool_call_limits={"ghost": 5},
        ),
    )

    await ledger.purge_session_tool_limits(key, {"ghost"})

    snapshot = await ledger.get_session_budget(key)
    assert snapshot.limits.max_agent_turns_total == 10
    assert snapshot.limits.max_delegations == 4
    assert snapshot.limits.tool_call_limits == {}


@pytest.mark.asyncio
async def test_purge_leaves_consumption_facts_untouched(tmp_path):
    """`tool_calls_by_tool` / `tool_attempts_by_tool` 是历史消耗事实，清除不得触碰。"""
    ledger = _ledger(tmp_path)
    await ledger.initialize()
    key = "sess-purge-consumed"
    await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(tool_call_limits={"ghost": 5, "glob": 9}),
    )
    await ledger.record_session_tools(
        key, calls={"ghost": 2, "glob": 1}, attempts={"ghost": 3, "glob": 1},
    )

    await ledger.purge_session_tool_limits(key, {"ghost"})

    snapshot = await ledger.get_session_budget(key)
    assert snapshot.consumed.tool_calls_by_tool == {"ghost": 2, "glob": 1}
    assert snapshot.consumed.tool_attempts_by_tool == {"ghost": 3, "glob": 1}


# ── 幂等：第二遍空、零行、无新事件、版本不动 ─────────────────────────────


@pytest.mark.asyncio
async def test_purge_is_idempotent_second_run_is_empty_and_silent(tmp_path):
    """名字已不在 ⇒ 第二遍 `purged == {}`、`rows == 0`、不 bump version、不落事件。"""
    ledger = _ledger(tmp_path)
    await ledger.initialize()
    key = "sess-purge-twice"
    await ledger.ensure_session_budget(
        key, root_session_id=key, limits=SessionLimits(tool_call_limits={"ghost": 5}),
    )

    first = await ledger.purge_session_tool_limits(key, {"ghost"})
    assert first.purged == {"ghost": 5} and first.version == 2
    kinds_after_first = await ledger.session_event_kinds(key)

    second = await ledger.purge_session_tool_limits(key, {"ghost"})
    assert second.purged == {}
    assert second.remaining == {}
    assert second.rows == 0
    assert second.version == 2, "无变更不许 bump version（否则 CAS/投影看到假变更）"

    snapshot = await ledger.get_session_budget(key)
    assert snapshot.version == 2
    assert await ledger.session_event_kinds(key) == kinds_after_first, "无清洗不许落新事件"


# ── 审计：kind / version bump / detail 形状 ────────────────────────────


@pytest.mark.asyncio
async def test_purge_appends_audit_event_with_detail_and_bumps_version(tmp_path):
    """删除 + 审计 + version bump 在同一事务里；审计 kind = `tool_limits_purged`。"""
    ledger = _ledger(tmp_path)
    await ledger.initialize()
    key = "sess-purge-audit"
    await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(tool_call_limits={"ghost": 5, "glob": 9}),
    )

    result = await ledger.purge_session_tool_limits(key, {"ghost"}, source="cli")
    assert result.version == 2

    rows = _audit_rows(tmp_path, key)
    kind, version, detail_raw = rows[-1]
    assert kind == "tool_limits_purged"
    assert version == 2, "审计 version 必须与账行新版本一致"

    detail = json.loads(detail_raw)
    assert detail["purged"] == {"ghost": 5}
    assert detail["remaining"] == {"glob": 9}
    assert detail["source"] == "cli"


# ── 未知 budget_key：与 update_session_limits 同一口径 ──────────────────


@pytest.mark.asyncio
async def test_purge_unknown_budget_key_raises_keyerror(tmp_path):
    """行不存在 ⇒ `KeyError`，不静默造行、不假装清成。"""
    ledger = _ledger(tmp_path)
    await ledger.initialize()

    with pytest.raises(KeyError):
        await ledger.purge_session_tool_limits("no-such-budget", {"ghost"})


# ── 结果类型：字段齐全（#616 用户确认的固定接口）────────────────────────


@pytest.mark.asyncio
async def test_purge_returns_the_fixed_result_type(tmp_path):
    """返回值是 `SessionToolLimitsPurge`，四字段 `.purged/.remaining/.rows/.version` 全在。"""
    ledger = _ledger(tmp_path)
    await ledger.initialize()
    key = "sess-purge-shape"
    await ledger.ensure_session_budget(
        key, root_session_id=key, limits=SessionLimits(tool_call_limits={"ghost": 5}),
    )

    result = await ledger.purge_session_tool_limits(key, {"ghost"})

    assert type(result).__name__ == "SessionToolLimitsPurge"
    assert result.purged == {"ghost": 5}
    assert result.remaining == {}
    assert result.rows == 1
    assert result.version == 2
