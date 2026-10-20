from datetime import UTC, datetime, timedelta

import pytest

from agent_harness.watchtower.schedule import (
    ScheduledRule,
    evaluate_schedule,
)


def test_empty_schedule_has_no_due_or_expired_rules() -> None:
    decision = evaluate_schedule([], datetime(2026, 10, 17, tzinfo=UTC))

    assert decision.due == ()
    assert decision.expired == ()


def test_due_rules_are_ordered_and_grace_is_anchored_to_planned_time() -> None:
    at = datetime(2026, 10, 17, 9, tzinfo=UTC)
    rules = [
        ScheduledRule("later", at + timedelta(seconds=1), "armed"),
        ScheduledRule("same-b", at, "active"),
        ScheduledRule("same-a", at, "armed"),
    ]

    decision = evaluate_schedule(rules, at + timedelta(minutes=5))

    assert [rule.rule_id for rule in decision.due] == ["same-a", "same-b", "later"]
    assert decision.expired == ()


def test_active_rules_expire_after_grace_and_paused_rules_at_the_scheduled_time() -> None:
    at = datetime(2026, 10, 17, 9, tzinfo=UTC)
    late = evaluate_schedule(
        [ScheduledRule("late", at, "active")],
        at + timedelta(minutes=5, microseconds=1),
    )
    paused = evaluate_schedule([ScheduledRule("paused", at, "paused")], at)

    assert late.due == ()
    assert [rule.rule_id for rule in late.expired] == ["late"]
    assert paused.due == ()
    assert [rule.rule_id for rule in paused.expired] == ["paused"]


def test_schedule_requires_aware_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_schedule([], datetime.fromisoformat("2026-10-17T09:00:00"))
