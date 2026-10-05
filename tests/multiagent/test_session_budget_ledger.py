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
    BudgetRejection,
    SessionConsumed,
    SessionLimits,
)
from agent_harness.storage.delegation_tree import (
    InMemoryDelegationTreeLedger,
    SessionBudgetHandle,
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
async def test_record_tool_call_is_idempotent_by_call_id(tmp_path, memory):
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-tool-call-idempotency"
    await ledger.ensure_session_budget(
        key,
        root_session_id=key,
        limits=SessionLimits(
            tool_call_limits={"request_constraint_resolution": 2},
        ),
    )
    values = {
        "tool_call_id": "ask-1",
        "tool_name": "request_constraint_resolution",
        "calls": 1,
        "attempts": 1,
    }

    assert await ledger.record_session_tool_call(key, **values) is True
    assert await ledger.record_session_tool_call(key, **values) is False
    snapshot = await ledger.get_session_budget(key)
    assert snapshot is not None
    assert snapshot.consumed.tool_calls_by_tool == {
        "request_constraint_resolution": 1,
    }
    assert snapshot.consumed.tool_attempts_by_tool == {
        "request_constraint_resolution": 1,
    }

    with pytest.raises(ValueError, match="different budget data"):
        await ledger.record_session_tool_call(key, **{**values, "calls": 0})


@pytest.mark.asyncio
async def test_cancelled_tool_call_budget_write_marks_session_for_replay(monkeypatch):
    ledger = InMemoryDelegationTreeLedger()
    key = "root-cancelled-tool-call"
    await ledger.ensure_session_budget(
        key,
        root_session_id=key,
        limits=SessionLimits(),
    )
    recovery_required: list[str] = []
    handle = SessionBudgetHandle(
        ledger,
        budget_key=key,
        root_session_id=key,
        on_tool_call_record_failure=lambda: recovery_required.append(key),
    )
    write_started = asyncio.Event()

    async def wait_for_cancellation(*_args, **_kwargs):
        write_started.set()
        await asyncio.Future()

    monkeypatch.setattr(ledger, "record_session_tool_call", wait_for_cancellation)
    write = asyncio.create_task(handle.record_tool_call(
        tool_call_id="ask-cancelled",
        tool_name="request_constraint_resolution",
        calls=1,
        attempts=1,
    ))
    await asyncio.wait_for(write_started.wait(), timeout=1.0)
    write.cancel()

    with pytest.raises(asyncio.CancelledError):
        await write

    assert recovery_required == [key]


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


# ── #552：整数 ceiling / usage 的 int64 值域（BUG-R4-02 / BUG-R4-03）──────────
# 存储层是 SQLite INTEGER（64 位有符号）。外部数值（provider usage / 请求 ceiling）
# 进账本前必须按此上限校验，而不是等绑定爆炸成未分类的 OverflowError。InMemory 是
# Sqlite 的孪生实现，两者必须给出**同一读数**（recon552 §A5/A8 的分歧要消除）。

INT64_MAX = 2**63 - 1


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_ensure_session_budget_accepts_int64_max_boundary(tmp_path, memory):
    """锚（C1）：`2**63-1` 是 SQLite INTEGER 上限，必须原样接受、原样读回。"""
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-int64-max"
    snap = await ledger.ensure_session_budget(
        key, root_session_id=key,
        limits=SessionLimits(max_total_tokens=INT64_MAX),
    )
    assert snap.limits.max_total_tokens == INT64_MAX
    reread = await ledger.get_session_budget(key)
    assert reread.limits.max_total_tokens == INT64_MAX


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
@pytest.mark.parametrize("oversize", [2**63, 10**30])
async def test_ensure_session_budget_rejects_ceiling_above_int64(
    tmp_path, memory, oversize,
):
    """C2：超过 int64 的 ceiling 在**绑定前**被拒，不得炸 OverflowError。

    兜底抛的是**域错误** `BudgetRejection`（越界 = 422 语义；`web/domain_errors.py`
    的单一映射据此落 422，不是一个裸 `ValueError` 的未分类 500，见 #552 C4）。
    拒绝必须零落盘（行不存在）。
    """
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-ceil-over"
    with pytest.raises(BudgetRejection) as excinfo:
        await ledger.ensure_session_budget(
            key, root_session_id=key,
            limits=SessionLimits(max_total_tokens=oversize),
        )
    assert not isinstance(excinfo.value, OverflowError)
    assert await ledger.get_session_budget(key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
@pytest.mark.parametrize("bad", [10**30, 2**63, -5, True, 1.5, "100"])
async def test_record_usage_out_of_contract_becomes_unknown_not_coerced(
    tmp_path, memory, bad,
):
    """C3/C5/C6/C7：越界 / 负数 / bool / float / 字符串一律**不入账**——该维转未知。

    绝不 `int()` 强转（`True→1`、`1.5→1`、`"100"→100` 都是伪造账目），也绝不 clamp
    到 int64 上限（audit 明令 MUST NOT clamp）、不记 0。Sqlite 与 InMemory 同判。
    当前 RED：Sqlite 对 10**30/2**63 抛 OverflowError，其余把 −5/1/1/100 直存。
    """
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-usage-bad"
    await ledger.ensure_session_budget(key, root_session_id=key, limits=SessionLimits())

    await ledger.record_session_model_requests(
        key, count=1, usage={"total_tokens": bad}, cost=None,
    )
    snap = await ledger.get_session_budget(key)
    assert snap.consumed.total_tokens is None
    assert snap.consumed.model_requests == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [False, True])
async def test_two_legal_usages_summing_over_int64_has_defined_outcome(
    tmp_path, memory,
):
    """C4：两个**各自合法**的 usage 相加溢出 int64 ⇒ 无半写、行为已定义（转未知）。

    当前 RED：Sqlite 第二次 record 抛 OverflowError、快照停在第一次的 2**62；
    InMemory 存下 2**63。修复后第二次整体落账、该维转未知（不可表达 ≠ 0）。
    """
    ledger = _ledger(tmp_path, memory=memory)
    await ledger.initialize()
    key = "sess-usage-sum"
    await ledger.ensure_session_budget(key, root_session_id=key, limits=SessionLimits())

    await ledger.record_session_model_requests(
        key, count=1, usage={"total_tokens": 2**62}, cost=None,
    )
    first = await ledger.get_session_budget(key)
    assert first.consumed.total_tokens == 2**62  # 第一次整体落账

    await ledger.record_session_model_requests(
        key, count=1, usage={"total_tokens": 2**62}, cost=None,
    )
    second = await ledger.get_session_budget(key)
    assert second.consumed.total_tokens is None
    assert second.consumed.model_requests == 2


@pytest.mark.asyncio
async def test_sqlite_and_inmemory_agree_on_oversize_usage(tmp_path):
    """C8：同一越界输入，两种实现给出**同一读数**（此前 Sqlite 炸、InMemory 存 10**30）。"""
    sqlite = SqliteDelegationTreeLedger(tmp_path / "harness.db")
    memory = InMemoryDelegationTreeLedger()
    await sqlite.initialize()
    await memory.initialize()

    readings = []
    for ledger in (sqlite, memory):
        await ledger.ensure_session_budget(
            "sess-parity", root_session_id="sess-parity", limits=SessionLimits(),
        )
        await ledger.record_session_model_requests(
            "sess-parity", count=1, usage={"total_tokens": 10**30}, cost=None,
        )
        snap = await ledger.get_session_budget("sess-parity")
        readings.append((snap.consumed.total_tokens, snap.consumed.model_requests))

    assert readings[0] == readings[1] == (None, 1)
