"""SessionBudget durable 账的存储契约（`#318`；`02 §5.1` / `10 §5.1` / `11 §6.1`）。

账的真源是 `session_budgets` 行（唯一 owner——事件流里没有第二份账），本文件钉的是
**这一层的机械性质**：

- **首用钉死 + 只收窄**（ensure）：重启后 runtime 拿同一份声明重新 ensure，
  ceiling 与账都从持久层读回，绝不把抬过的 ceiling 收紧回去；
- **原子准入**（admit）：turns / requests 的预留与判定在**同一个事务**里——
  并发抢占最后一格时至多一个赢家（`10 §13`），拒绝方账不变；
- **CAS 更新零副作用**（update）：版本过期 / ceiling 低于已消耗 / headroom 不足
  全部在单事务里判，409 时账行与 version 一个字节都不动；
- **审计 append-only**：`session_budget_events` 无 UPDATE / DELETE 触发器；
- **委派账**（consume）：行不存在按声明建行并钉死，之后收窄-only，原子计数。

两种实现（Sqlite / InMemory）必须满足同一批契约——InMemory 的状态形状与表行
一致（cost / deadline 是文本、map 是 JSON），这是刻意的：测试与装配替身都能
拿到同一条读数。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agent_harness.agent.run_budget import (
    TRIGGER_SESSION_REQUESTS,
    TRIGGER_SESSION_TURNS,
    SessionConsumed,
    SessionLimits,
)
from agent_harness.storage.delegation_tree import (
    InMemoryDelegationTreeLedger,
    SqliteDelegationTreeLedger,
)


def _ledger(tmp_path, *, memory: bool = False):
    if memory:
        return InMemoryDelegationTreeLedger()
    return SqliteDelegationTreeLedger(tmp_path / "harness.db")


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_ensure_is_idempotent_and_tighten_only(tmp_path, memory):
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-ensure"

    first = await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(max_agent_turns_total=10, max_delegations=4),
    )
    assert first.limits.max_agent_turns_total == 10
    assert first.limits.max_delegations == 4
    assert first.version == 1

    # 同一份声明再 ensure：幂等，version 不动（CAS 版本只属于恢复路径的显式更新）
    again = await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(max_agent_turns_total=10, max_delegations=4),
    )
    assert again.version == 1
    assert again.limits.max_agent_turns_total == 10

    # 更宽松的声明不抬回；更严的声明收窄；都 bump 不了 version
    looser = await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(max_agent_turns_total=99, max_model_requests=5),
    )
    assert looser.limits.max_agent_turns_total == 10
    assert looser.limits.max_model_requests == 5
    assert looser.version == 1

    # per-tool 与 cost / deadline 的收窄合并
    tightened = await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(
            max_cost_usd=Decimal("1.50"),
            deadline_at=datetime(2030, 1, 1, tzinfo=UTC),
            tool_call_limits={"bash": 3},
        ),
    )
    assert tightened.limits.tool_call_limits == {"bash": 3}
    assert tightened.version == 1

    merged = await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(
            max_cost_usd=Decimal("2.00"),
            deadline_at=datetime(2031, 1, 1, tzinfo=UTC),
            tool_call_limits={"read": 2, "bash": 7},
        ),
    )
    # cost / deadline 取更小者；per-tool 逐名收窄（未点名的沿用）
    assert merged.limits.max_cost_usd == Decimal("1.50")
    assert merged.limits.deadline_at == datetime(2030, 1, 1, tzinfo=UTC)
    assert merged.limits.tool_call_limits == {"bash": 3, "read": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_admission_reserves_and_rejects_atomically(tmp_path, memory):
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-admit"

    await ledger.ensure_session_budget(
        key, root_session_id=key, limits=SessionLimits(max_agent_turns_total=2),
    )

    first = await ledger.admit_session_step(key)
    assert first.accepted is True
    assert first.trigger_dimension is None

    # ceiling=2 的第二格：consumed(1) + 预留(1) >= 2 ⇒ 收口预留语义判到顶。
    # 注意这是**预留**语义（turns 的 closeout 预留），与 tokens 的到线即停不同。
    second = await ledger.admit_session_step(key)
    assert second.accepted is False
    assert second.trigger_dimension == TRIGGER_SESSION_TURNS

    # 拒绝方零变化：账还是 1 / 1
    after = await ledger.get_session_budget(key)
    assert after.consumed.agent_turns == 1
    assert after.consumed.model_requests == 1

    # 退回那一格 turns（决策未接纳）后，同一格能再被抢
    await ledger.refund_session_turn(key)
    retried = await ledger.admit_session_step(key)
    assert retried.accepted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_requests_dimension_also_reserves_closeout(tmp_path, memory):
    # requests 与 turns 同用**预留**语义（每步至少真实发出一次请求 + closeout 那次
    # 也要留位）：ceiling=2 ⇒ 第一格放行（0+1<2），第二格被预留挡下（1+1>=2）
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-requests"
    await ledger.ensure_session_budget(
        key, root_session_id=key, limits=SessionLimits(max_model_requests=2),
    )
    assert (await ledger.admit_session_step(key)).accepted
    second = await ledger.admit_session_step(key)
    assert second.accepted is False
    assert second.trigger_dimension == TRIGGER_SESSION_REQUESTS


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_record_model_requests_is_none_sticky(tmp_path, memory):
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-usage"
    await ledger.ensure_session_budget(key, root_session_id=key, limits=SessionLimits())

    await ledger.record_session_model_requests(
        key, count=2, usage={"total_tokens": 100}, cost=Decimal("0.20"),
    )
    snapshot = await ledger.get_session_budget(key)
    assert snapshot.consumed.model_requests == 2
    assert snapshot.consumed.total_tokens == 100
    assert snapshot.consumed.cost_usd == Decimal("0.20")

    # 任一请求没自报 ⇒ 该维度转**未知**并粘住（不可得 ≠ 0）
    await ledger.record_session_model_requests(
        key, count=1, usage=None, cost=None,
    )
    snapshot = await ledger.get_session_budget(key)
    assert snapshot.consumed.model_requests == 3
    assert snapshot.consumed.total_tokens is None
    assert snapshot.consumed.cost_usd is None


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_record_tools_merges_maps(tmp_path, memory):
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-tools"
    await ledger.ensure_session_budget(key, root_session_id=key, limits=SessionLimits())

    await ledger.record_session_tools(
        key, calls={"bash": 2, "read": 1}, attempts={"bash": 3, "read": 1},
    )
    await ledger.record_session_tools(key, calls={"bash": 1}, attempts={"bash": 1})
    snapshot = await ledger.get_session_budget(key)
    assert snapshot.consumed.tool_calls_by_tool == {"bash": 3, "read": 1}
    assert snapshot.consumed.tool_attempts_by_tool == {"bash": 4, "read": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_update_session_limits_cas_and_conflicts(tmp_path, memory):
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-cas"
    # 空 ceiling：三次准入都放行（turns=3, requests=3）
    await ledger.ensure_session_budget(key, root_session_id=key, limits=SessionLimits())
    for _ in range(3):
        assert (await ledger.admit_session_step(key)).accepted

    # 版本过期 ⇒ 409，账行零变化
    with pytest.raises(Exception, match="version"):
        await ledger.update_session_limits(
            key, expected_version=99,
            limits=SessionLimits(max_agent_turns_total=5),
        )
    stale = await ledger.get_session_budget(key)
    assert stale.version == 1

    # 生效 ceiling 低于已消耗（turns=3，点名 2）⇒ 409（版本对也不行）
    with pytest.raises(Exception, match="低于已消耗"):
        await ledger.update_session_limits(
            key, expected_version=1, limits=SessionLimits(max_agent_turns_total=2),
        )
    unchanged = await ledger.get_session_budget(key)
    assert unchanged.version == 1

    # 成功路径：抬到 5，version +1，未点名的 requests 沿用（None）
    updated = await ledger.update_session_limits(
        key, expected_version=1, limits=SessionLimits(max_agent_turns_total=5),
    )
    assert updated.limits.max_agent_turns_total == 5
    assert updated.version == 2
    assert updated.consumed.agent_turns == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_update_session_limits_rejects_no_headroom(tmp_path, memory):
    # "放得下一次新准入"也在单事务里判：turns=1 已消耗、点名 turns=2——
    # 高于已消耗但放不下"一次准入 + closeout 预留"（1 + 1 >= 2）⇒ 409 零改动
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-headroom"
    await ledger.ensure_session_budget(
        key, root_session_id=key, limits=SessionLimits(max_agent_turns_total=2),
    )
    assert (await ledger.admit_session_step(key)).accepted  # turns=1

    with pytest.raises(Exception, match="放不下"):
        await ledger.update_session_limits(
            key, expected_version=1, limits=SessionLimits(max_agent_turns_total=2),
        )
    unchanged = await ledger.get_session_budget(key)
    assert unchanged.version == 1
    assert unchanged.limits.max_agent_turns_total == 2

    # 抬到 3：放得下（1 + 1 < 3）⇒ 接受
    ok = await ledger.update_session_limits(
        key, expected_version=1, limits=SessionLimits(max_agent_turns_total=3),
    )
    assert ok.limits.max_agent_turns_total == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_update_session_limits_rejects_unknown_base(tmp_path, memory):
    # 账未知（有请求未自报 usage）而点名 token ceiling ⇒ 无法证明到线即停 ⇒ 409
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-unknown"
    await ledger.ensure_session_budget(key, root_session_id=key, limits=SessionLimits())
    await ledger.record_session_model_requests(
        key, count=1, usage=None, cost=None,
    )
    with pytest.raises(Exception, match="未知"):
        await ledger.update_session_limits(
            key, expected_version=1, limits=SessionLimits(max_total_tokens=1000),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_consume_session_delegation_pins_then_tightens(tmp_path, memory):
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-delegate"

    # 行不存在：按本次声明建行并钉死（provider 传域默认 8）
    first = await ledger.consume_session_delegation(
        key, root_session_id=key, max_delegations=8,
    )
    assert first.accepted and first.used == 1 and first.limit == 8

    # 之后声明更小 ⇒ 收窄-only（与 ensure 同一条纪律）
    second = await ledger.consume_session_delegation(
        key, root_session_id=key, max_delegations=2,
    )
    assert second.accepted and second.used == 2 and second.limit == 2

    # 最后一格已用满 ⇒ 原子拒绝
    third = await ledger.consume_session_delegation(
        key, root_session_id=key, max_delegations=8,
    )
    assert not third.accepted and third.used == 2 and third.limit == 2

    # 树 reserve 失败的回退：退一格
    await ledger.refund_session_delegation(key)
    state = await ledger.get_session_budget(key)
    assert state.consumed.delegations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_concurrent_session_admissions_cannot_overdraw(tmp_path, memory):
    # `10 §13`：并发抢最后一格 ⇒ 至多一个赢家；其余拒绝且账不变。
    # ceiling=2：预留语义下恰好有一格产出轮可抢（0+1<2 放行；1+1>=2 拒绝）。
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-race"
    await ledger.ensure_session_budget(
        key, root_session_id=key, limits=SessionLimits(max_model_requests=2),
    )

    admissions = await asyncio.gather(*[
        ledger.admit_session_step(key) for _ in range(8)
    ])
    assert sum(item.accepted for item in admissions) == 1
    state = await ledger.get_session_budget(key)
    assert state.consumed.model_requests == 1


@pytest.mark.asyncio
async def test_session_budget_survives_store_recreation(tmp_path):
    # Crash durability（`03 §5`）：账与 ceiling 都在 durable 行里，不靠进程内存
    database = tmp_path / "harness.db"
    first = SqliteDelegationTreeLedger(database)
    await first.initialize()
    key = "sess-restart"
    await first.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(max_agent_turns_total=9, max_delegations=8),
    )
    await first.record_session_model_requests(
        key, count=3, usage={"total_tokens": 42}, cost=Decimal("0.07"),
    )

    recovered = SqliteDelegationTreeLedger(database)
    await recovered.initialize()
    snapshot = await recovered.get_session_budget(key)
    assert snapshot.limits.max_agent_turns_total == 9
    assert snapshot.limits.max_delegations == 8
    assert snapshot.consumed.model_requests == 3
    assert snapshot.consumed.total_tokens == 42
    assert snapshot.consumed.cost_usd == Decimal("0.07")
    assert snapshot.version == 1


@pytest.mark.asyncio
async def test_session_budget_events_are_append_only(tmp_path):
    ledger = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    await ledger.initialize()
    key = "sess-audit"
    await ledger.ensure_session_budget(key, root_session_id=key, limits=SessionLimits())
    await ledger.admit_session_step(key)
    await ledger.refund_session_turn(key)
    await ledger.update_session_limits(
        key, expected_version=1, limits=SessionLimits(max_agent_turns_total=9),
    )

    kinds = await ledger.session_event_kinds(key)
    assert kinds == ["step_admitted", "turn_refunded", "limits_updated"]

    import sqlite3

    with sqlite3.connect(tmp_path / "harness.db") as connection:
        # 无 UPDATE / 无 DELETE 触发器：审计行只能插入，改写与抹除都在数据库层被拒
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE session_budget_events SET kind = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM session_budget_events")


@pytest.mark.asyncio
async def test_snapshot_absent_row_reads_none(tmp_path):
    ledger = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    await ledger.initialize()
    assert await ledger.get_session_budget("no-such-session") is None


def _consumed(**overrides) -> SessionConsumed:
    base = {
        "agent_turns": 0, "model_requests": 0, "total_tokens": 0, "cost_usd": None,
        "tool_calls_by_tool": {}, "tool_attempts_by_tool": {}, "delegations": 0,
    }
    base.update(overrides)
    return SessionConsumed(**base)


@pytest.mark.asyncio
async def test_deadline_headroom_is_strict_future(tmp_path):
    # 恢复点名的 session deadline 已在过去 ⇒ headroom 判"放不下" ⇒ 409
    ledger = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    await ledger.initialize()
    key = "sess-past-deadline"
    await ledger.ensure_session_budget(key, root_session_id=key, limits=SessionLimits())
    past = datetime.now(UTC) - timedelta(minutes=1)
    with pytest.raises(Exception, match="放不下"):
        await ledger.update_session_limits(
            key, expected_version=1,
            limits=SessionLimits(deadline_at=past),
        )
