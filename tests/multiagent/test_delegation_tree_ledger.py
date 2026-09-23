"""Durable accounting contracts for one delegation tree (#287)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from agent_harness.agent.guards import GuardLevel
from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger


@pytest.mark.asyncio
async def test_budget_survives_store_recreation_and_never_expands(tmp_path):
    database = tmp_path / "recovery.db"
    first = SqliteDelegationTreeLedger(database)
    await first.initialize()

    one = await first.reserve(
        "tree-1", root_session_id="root-1", max_delegations=2, max_depth=4,
    )
    assert one.accepted and one.used == 1 and one.limit == 2

    recovered = SqliteDelegationTreeLedger(database)
    await recovered.initialize()
    two = await recovered.reserve(
        "tree-1", root_session_id="root-1", max_delegations=8, max_depth=9,
    )
    rejected = await recovered.reserve(
        "tree-1", root_session_id="root-1", max_delegations=8, max_depth=9,
    )

    state = await recovered.get_state("tree-1")
    assert two.accepted and two.used == 2 and two.limit == 2
    assert not rejected.accepted and rejected.used == 2 and rejected.limit == 2
    assert state.max_depth == 4
    assert state.used_delegations == 2


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_overdraw_last_slots(tmp_path):
    ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await ledger.initialize()

    reservations = await asyncio.gather(*[
        ledger.reserve(
            "tree-race", root_session_id="root-race",
            max_delegations=4, max_depth=3,
        )
        for _ in range(16)
    ])

    assert sum(item.accepted for item in reservations) == 4
    assert sorted(item.used for item in reservations if item.accepted) == [1, 2, 3, 4]
    assert all(item.used == 4 for item in reservations if not item.accepted)


@pytest.mark.asyncio
async def test_failure_fingerprint_and_guard_level_survive_store_recreation(tmp_path):
    database = tmp_path / "recovery.db"
    ledger = SqliteDelegationTreeLedger(database)
    await ledger.initialize()
    await ledger.reserve(
        "tree-guard", root_session_id="root-guard",
        max_delegations=10, max_depth=3,
    )

    signals = [
        await ledger.observe_result("tree-guard", "profile:task-hash", ok=False)
        for _ in range(3)
    ]
    assert [signal.level for signal in signals] == [
        GuardLevel.NONE, GuardLevel.NONE, GuardLevel.SOFT,
    ]

    recovered = SqliteDelegationTreeLedger(database)
    await recovered.initialize()
    signal = None
    for _ in range(3):
        signal = await recovered.observe_result("tree-guard", "profile:task-hash", ok=False)
    assert signal is not None and signal.level == GuardLevel.HARD
    assert signal.consecutive_failures == 6

    changed = await recovered.observe_result("tree-guard", "profile:new-task", ok=False)
    assert changed.level == GuardLevel.NONE
    assert changed.consecutive_failures == 1


@pytest.mark.asyncio
async def test_tree_audit_events_are_append_only(tmp_path):
    database = tmp_path / "recovery.db"
    ledger = SqliteDelegationTreeLedger(database)
    await ledger.initialize()
    await ledger.reserve(
        "tree-audit", root_session_id="root-audit",
        max_delegations=1, max_depth=2,
    )

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE delegation_tree_events SET kind = 'rewritten' WHERE tree_id = ?",
                ("tree-audit",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM delegation_tree_events WHERE tree_id = ?",
                ("tree-audit",),
            )


@pytest.mark.asyncio
async def test_unrelated_root_trees_do_not_share_budget_or_failure_state(tmp_path):
    ledger = SqliteDelegationTreeLedger(tmp_path / "recovery.db")
    await ledger.initialize()
    for tree_id in ("root-run-a", "root-run-b"):
        await ledger.reserve(
            tree_id, root_session_id="same-session",
            max_delegations=3, max_depth=2,
        )

    await ledger.observe_result("root-run-a", "same-fingerprint", ok=False)
    await ledger.observe_result("root-run-a", "same-fingerprint", ok=False)
    other_root = await ledger.observe_result(
        "root-run-b", "same-fingerprint", ok=False,
    )
    third_failure = await ledger.observe_result(
        "root-run-a", "same-fingerprint", ok=False,
    )

    assert other_root.level == GuardLevel.NONE
    assert other_root.consecutive_failures == 1
    assert third_failure.level == GuardLevel.SOFT
    assert third_failure.consecutive_failures == 3
    assert (await ledger.get_state("root-run-a")).used_delegations == 1
    assert (await ledger.get_state("root-run-b")).used_delegations == 1
    with pytest.raises(ValueError, match="root identity"):
        await ledger.reserve(
            "root-run-a", root_session_id="another-session",
            max_delegations=3, max_depth=2,
        )
