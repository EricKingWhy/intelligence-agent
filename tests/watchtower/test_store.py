import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from agent_harness.watchtower.models import WatchtowerRule
from agent_harness.watchtower.store import SqliteWatchtowerStore


def make_rule() -> WatchtowerRule:
    now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    return WatchtowerRule(
        rule_id="rule-1",
        prompt="run the maintenance task",
        trigger_kind="relative",
        trigger_at=now + timedelta(hours=1),
        timezone="UTC",
        delay_seconds=3600,
        consent_version="watchtower-v1",
        consented_at=now,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_dispatch_intent_and_session_link_survive_store_recreation(tmp_path) -> None:
    path = tmp_path / "harness.db"
    store = SqliteWatchtowerStore(path)
    await store.initialize()
    created = await store.create_rule(make_rule())

    armed = await store.transition(
        created.rule_id,
        expected_version=created.version,
        state="armed",
        event_kind="armed",
    )
    assert armed is not None
    assert await store.transition(
        created.rule_id,
        expected_version=created.version,
        state="paused",
        event_kind="stale-pause",
    ) is None

    fire_at = datetime(2026, 10, 17, 10, tzinfo=UTC)
    intent = await store.claim_fire(
        created.rule_id,
        expected_version=armed.version,
        fire_id="fire-1",
        session_id="session-1",
        fire_at=fire_at,
    )
    assert intent is not None

    recovered_store = SqliteWatchtowerStore(path)
    await recovered_store.initialize()
    pending = await recovered_store.list_dispatching()
    assert len(pending) == 1
    recovered_rule, recovered_intent = pending[0]
    assert recovered_rule.planned_session_id == "session-1"
    assert recovered_intent.fire_id == "fire-1"

    first_deadline = datetime(2026, 10, 17, 12, tzinfo=UTC)
    second_deadline = datetime(2026, 10, 17, 14, tzinfo=UTC)
    assert (await recovered_store.set_deadline("fire-1", first_deadline)).deadline_at == first_deadline
    assert (await recovered_store.set_deadline("fire-1", second_deadline)).deadline_at == first_deadline

    await recovered_store.finish_fire("fire-1", state="fired")
    final = await recovered_store.get_rule("rule-1")
    assert final is not None
    assert final.state == "fired"
    assert final.fired_session_id == "session-1"
    assert await recovered_store.list_dispatching() == []


@pytest.mark.asyncio
async def test_audit_events_are_append_only(tmp_path) -> None:
    path = tmp_path / "harness.db"
    store = SqliteWatchtowerStore(path)
    await store.initialize()
    await store.create_rule(make_rule())

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE watchtower_events SET kind='changed'")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM watchtower_events")
    finally:
        connection.close()
