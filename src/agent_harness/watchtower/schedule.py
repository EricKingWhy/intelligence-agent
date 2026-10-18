"""Pure one-shot schedule evaluation for Watchtower."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal


@dataclass(frozen=True)
class ScheduledRule:
    rule_id: str
    at: datetime
    state: Literal["active", "armed", "paused"]


@dataclass(frozen=True)
class ScheduleDecision:
    due: tuple[ScheduledRule, ...]
    expired: tuple[ScheduledRule, ...]


def evaluate_schedule(
    rules: tuple[ScheduledRule, ...] | list[ScheduledRule],
    now: datetime,
    *,
    grace: timedelta = timedelta(minutes=5),
) -> ScheduleDecision:
    """Return due and expired one-shot rules, ordered by scheduled time then ID.

    Paused rules expire when their scheduled time arrives. Active rules may
    catch up only inside the grace window, which is anchored to ``at``.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if grace < timedelta(0):
        raise ValueError("grace must not be negative")

    due: list[ScheduledRule] = []
    expired: list[ScheduledRule] = []
    for rule in rules:
        if rule.at.tzinfo is None or rule.at.utcoffset() is None:
            raise ValueError("scheduled rule timestamps must be timezone-aware")
        at = rule.at.astimezone(UTC)
        current = now.astimezone(UTC)
        if rule.state == "paused":
            if current >= at:
                expired.append(rule)
            continue
        if rule.state not in {"active", "armed"}:
            continue
        if at <= current <= at + grace:
            due.append(rule)
        elif current > at + grace:
            expired.append(rule)

    def order(rule: ScheduledRule) -> tuple[datetime, str]:
        return rule.at.astimezone(UTC), rule.rule_id

    return ScheduleDecision(
        due=tuple(sorted(due, key=order)),
        expired=tuple(sorted(expired, key=order)),
    )
