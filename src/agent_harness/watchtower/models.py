"""Persisted Watchtower rule and execution records."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

RuleState = Literal[
    "active", "armed", "paused", "dispatching", "fired", "expired",
    "launch_failed", "archived",
]


class WatchtowerRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    prompt: str
    trigger_kind: Literal["relative", "absolute"]
    trigger_at: datetime
    timezone: str
    local_time: str | None = None
    delay_seconds: int | None = None
    model: str | None = None
    sandbox_backend: str | None = None
    cwd: str | None = None
    budget_turns: int | None = None
    budget_tokens: int | None = None
    budget_duration_seconds: int | None = None
    consent_version: str
    consented_at: datetime
    state: RuleState = "active"
    version: int = 1
    current_fire_id: str | None = None
    planned_session_id: str | None = None
    fired_session_id: str | None = None
    fire_at: datetime | None = None
    paused_at: datetime | None = None
    last_error: str | None = None
    last_notice: str | None = None
    last_timed_out_request_id: str | None = None
    created_at: datetime
    updated_at: datetime


class WatchtowerExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fire_id: str
    rule_id: str
    session_id: str
    state: Literal["dispatching", "fired", "launch_failed"]
    fire_at: datetime
    deadline_at: datetime | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
