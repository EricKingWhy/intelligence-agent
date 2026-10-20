"""SQLite persistence for Watchtower rules, execution intents, and audit events."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from agent_harness.storage.sqlite import retry_on_busy
from agent_harness.watchtower.models import WatchtowerExecution, WatchtowerRule

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchtower_rules (
    rule_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    trigger_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watchtower_rules_schedule
    ON watchtower_rules(state, trigger_at);
CREATE TABLE IF NOT EXISTS watchtower_executions (
    fire_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL REFERENCES watchtower_rules(rule_id),
    session_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    fire_at TEXT NOT NULL,
    deadline_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watchtower_executions_rule
    ON watchtower_executions(rule_id, created_at);
CREATE TABLE IF NOT EXISTS watchtower_events (
    event_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL REFERENCES watchtower_rules(rule_id),
    fire_id TEXT,
    kind TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watchtower_events_rule
    ON watchtower_events(rule_id, created_at);
CREATE TRIGGER IF NOT EXISTS watchtower_events_no_update
BEFORE UPDATE ON watchtower_events
BEGIN
    SELECT RAISE(ABORT, 'watchtower audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS watchtower_events_no_delete
BEFORE DELETE ON watchtower_events
BEGIN
    SELECT RAISE(ABORT, 'watchtower audit events are append-only');
END;
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class SqliteWatchtowerStore:
    """Mutable rule projections backed by append-only audit and execution rows."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.database_path, timeout=10)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA busy_timeout=10000")
            await connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            await connection.close()

    @retry_on_busy
    async def initialize(self) -> None:
        async with self._connect() as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.executescript(_SCHEMA)
            await connection.commit()

    @retry_on_busy
    async def create_rule(self, rule: WatchtowerRule) -> WatchtowerRule:
        data = rule.model_dump(mode="json")
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """INSERT INTO watchtower_rules
                    (rule_id, state, trigger_at, version, data_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rule.rule_id, rule.state, rule.trigger_at.isoformat(),
                        rule.version, _json(data), rule.created_at.isoformat(),
                        rule.updated_at.isoformat(),
                    ),
                )
                await self._append_event(
                    connection, rule.rule_id, "created", data,
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return rule

    async def get_rule(self, rule_id: str) -> WatchtowerRule | None:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT data_json FROM watchtower_rules WHERE rule_id = ?",
                (rule_id,),
            )
            row = await cursor.fetchone()
        return WatchtowerRule.model_validate_json(row["data_json"]) if row else None

    async def list_rules(self) -> list[WatchtowerRule]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT data_json FROM watchtower_rules ORDER BY trigger_at, rule_id"
            )
            rows = await cursor.fetchall()
        return [WatchtowerRule.model_validate_json(row["data_json"]) for row in rows]

    @retry_on_busy
    async def transition(
        self,
        rule_id: str,
        *,
        expected_version: int,
        state: str,
        event_kind: str,
        patch: Mapping[str, Any] | None = None,
        event_data: Mapping[str, Any] | None = None,
    ) -> WatchtowerRule | None:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT data_json, version FROM watchtower_rules WHERE rule_id = ?",
                    (rule_id,),
                )
                row = await cursor.fetchone()
                if row is None or row["version"] != expected_version:
                    await connection.rollback()
                    return None
                data = json.loads(row["data_json"])
                data.update(patch or {})
                now = _now().isoformat()
                data.update(state=state, version=expected_version + 1, updated_at=now)
                await connection.execute(
                    """UPDATE watchtower_rules SET state=?, trigger_at=?, version=?,
                    data_json=?, updated_at=? WHERE rule_id=? AND version=?""",
                    (
                        state, data["trigger_at"], expected_version + 1, _json(data),
                        now, rule_id, expected_version,
                    ),
                )
                await self._append_event(
                    connection, rule_id, event_kind,
                    {**(event_data or {}), **(patch or {}), "state": state},
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return WatchtowerRule.model_validate(data)

    @retry_on_busy
    async def claim_fire(
        self,
        rule_id: str,
        *,
        expected_version: int,
        fire_id: str,
        session_id: str,
        fire_at: datetime,
    ) -> WatchtowerExecution | None:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT data_json, version, state FROM watchtower_rules WHERE rule_id=?",
                    (rule_id,),
                )
                row = await cursor.fetchone()
                if (
                    row is None
                    or row["version"] != expected_version
                    or row["state"] not in {"active", "armed"}
                ):
                    await connection.rollback()
                    return None
                data = json.loads(row["data_json"])
                now = _now().isoformat()
                fire_text = fire_at.astimezone(UTC).isoformat()
                data.update(
                    state="dispatching", version=expected_version + 1,
                    current_fire_id=fire_id, planned_session_id=session_id,
                    fired_session_id=None, fire_at=fire_text, last_error=None,
                    updated_at=now,
                )
                await connection.execute(
                    """UPDATE watchtower_rules SET state='dispatching', trigger_at=?,
                    version=?, data_json=?, updated_at=? WHERE rule_id=? AND version=?""",
                    (data["trigger_at"], expected_version + 1, _json(data), now,
                     rule_id, expected_version),
                )
                await connection.execute(
                    """INSERT INTO watchtower_executions
                    (fire_id, rule_id, session_id, state, fire_at, created_at, updated_at)
                    VALUES (?, ?, ?, 'dispatching', ?, ?, ?)""",
                    (fire_id, rule_id, session_id, fire_text, now, now),
                )
                await self._append_event(
                    connection, rule_id, "dispatch_intent",
                    {"fire_id": fire_id, "session_id": session_id, "fire_at": fire_text},
                    fire_id=fire_id,
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return WatchtowerExecution(
            fire_id=fire_id, rule_id=rule_id, session_id=session_id,
            state="dispatching", fire_at=fire_at, created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    async def list_dispatching(self) -> list[tuple[WatchtowerRule, WatchtowerExecution]]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """SELECT r.data_json, e.* FROM watchtower_executions e
                JOIN watchtower_rules r ON r.rule_id=e.rule_id
                WHERE e.state='dispatching' ORDER BY e.created_at, e.fire_id"""
            )
            rows = await cursor.fetchall()
        return [
            (
                WatchtowerRule.model_validate_json(row["data_json"]),
                WatchtowerExecution(
                    fire_id=row["fire_id"], rule_id=row["rule_id"],
                    session_id=row["session_id"], state=row["state"],
                    fire_at=datetime.fromisoformat(row["fire_at"]),
                    deadline_at=(datetime.fromisoformat(row["deadline_at"])
                                 if row["deadline_at"] else None),
                    error=row["error"], created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                ),
            )
            for row in rows
        ]

    async def list_fired(self) -> list[tuple[WatchtowerRule, WatchtowerExecution]]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                """SELECT r.data_json, e.* FROM watchtower_executions e
                JOIN watchtower_rules r ON r.rule_id=e.rule_id
                WHERE e.state='fired' ORDER BY e.created_at, e.fire_id"""
            )
            rows = await cursor.fetchall()
        return [
            (
                WatchtowerRule.model_validate_json(row["data_json"]),
                WatchtowerExecution(
                    fire_id=row["fire_id"], rule_id=row["rule_id"],
                    session_id=row["session_id"], state=row["state"],
                    fire_at=datetime.fromisoformat(row["fire_at"]),
                    deadline_at=(datetime.fromisoformat(row["deadline_at"])
                                 if row["deadline_at"] else None),
                    error=row["error"], created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                ),
            )
            for row in rows
        ]

    @retry_on_busy
    async def set_deadline(
        self, fire_id: str, deadline_at: datetime | None = None, *,
        duration_seconds: int | None = None,
    ) -> WatchtowerExecution:
        if (deadline_at is None) == (duration_seconds is None):
            raise ValueError("provide exactly one of deadline_at or duration_seconds")
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT * FROM watchtower_executions WHERE fire_id=?",
                    (fire_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown fire_id: {fire_id}")
                if row["deadline_at"] is None:
                    if duration_seconds is not None:
                        deadline_at = _now() + timedelta(seconds=duration_seconds)
                    assert deadline_at is not None
                    deadline_text = deadline_at.astimezone(UTC).isoformat()
                    await connection.execute(
                        "UPDATE watchtower_executions SET deadline_at=?, updated_at=? WHERE fire_id=?",
                        (deadline_text, _now().isoformat(), fire_id),
                    )
                    await self._append_event(
                        connection, row["rule_id"], "deadline_set",
                        {"deadline_at": deadline_text}, fire_id=fire_id,
                    )
                    row = dict(row)
                    row["deadline_at"] = deadline_text
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return WatchtowerExecution(
            fire_id=row["fire_id"], rule_id=row["rule_id"],
            session_id=row["session_id"], state=row["state"],
            fire_at=datetime.fromisoformat(row["fire_at"]),
            deadline_at=datetime.fromisoformat(row["deadline_at"]),
            error=row["error"], created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def get_execution(self, fire_id: str) -> WatchtowerExecution | None:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM watchtower_executions WHERE fire_id=?", (fire_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return WatchtowerExecution(
            fire_id=row["fire_id"], rule_id=row["rule_id"],
            session_id=row["session_id"], state=row["state"],
            fire_at=datetime.fromisoformat(row["fire_at"]),
            deadline_at=(datetime.fromisoformat(row["deadline_at"])
                         if row["deadline_at"] else None),
            error=row["error"], created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @retry_on_busy
    async def finish_fire(
        self, fire_id: str, *, state: str, error: str | None = None,
    ) -> WatchtowerExecution:
        if state not in {"fired", "launch_failed"}:
            raise ValueError("fire completion state is invalid")
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """SELECT e.*, r.data_json FROM watchtower_executions e
                    JOIN watchtower_rules r ON r.rule_id=e.rule_id WHERE e.fire_id=?""",
                    (fire_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown fire_id: {fire_id}")
                can_finish = row["state"] == "dispatching" and state in {
                    "fired", "launch_failed",
                }
                can_fail_launch = row["state"] == "fired" and state == "launch_failed"
                if can_finish or can_fail_launch:
                    now = _now().isoformat()
                    await connection.execute(
                        "UPDATE watchtower_executions SET state=?, error=?, updated_at=? WHERE fire_id=?",
                        (state, error, now, fire_id),
                    )
                    data = json.loads(row["data_json"])
                    if data.get("current_fire_id") == fire_id:
                        data.update(
                            state=state,
                            fired_session_id=(row["session_id"] if state == "fired" else None),
                            planned_session_id=None,
                            last_error=error,
                            version=data["version"] + 1,
                            updated_at=now,
                        )
                        await connection.execute(
                            "UPDATE watchtower_rules SET state=?, version=?, data_json=?, updated_at=? WHERE rule_id=?",
                            (state, data["version"], _json(data), now, row["rule_id"]),
                        )
                    await self._append_event(
                        connection, row["rule_id"], state,
                        {"session_id": row["session_id"], "error": error},
                        fire_id=fire_id,
                    )
                    row = dict(row)
                    row.update(state=state, error=error, updated_at=now)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return WatchtowerExecution(
            fire_id=row["fire_id"], rule_id=row["rule_id"],
            session_id=row["session_id"], state=row["state"],
            fire_at=datetime.fromisoformat(row["fire_at"]),
            deadline_at=(datetime.fromisoformat(row["deadline_at"])
                         if row["deadline_at"] else None),
            error=row["error"], created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @retry_on_busy
    async def set_notice(
        self, rule_id: str, *, fire_id: str, request_id: str, notice: str,
    ) -> WatchtowerRule | None:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT data_json FROM watchtower_rules WHERE rule_id=?",
                    (rule_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.rollback()
                    return None
                data = json.loads(row["data_json"])
                if data.get("current_fire_id") != fire_id or data.get("state") != "fired":
                    await connection.rollback()
                    return None
                now = _now().isoformat()
                data.update(
                    last_notice=notice,
                    last_timed_out_request_id=request_id,
                    version=data["version"] + 1,
                    updated_at=now,
                )
                await connection.execute(
                    "UPDATE watchtower_rules SET version=?, data_json=?, updated_at=? WHERE rule_id=?",
                    (data["version"], _json(data), now, rule_id),
                )
                await self._append_event(
                    connection, rule_id, "notice", {"message": notice}, fire_id=fire_id,
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return WatchtowerRule.model_validate(data)

    async def list_executions(self, rule_id: str) -> list[WatchtowerExecution]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM watchtower_executions WHERE rule_id=? ORDER BY created_at, fire_id",
                (rule_id,),
            )
            rows = await cursor.fetchall()
        return [
            WatchtowerExecution(
                fire_id=row["fire_id"], rule_id=row["rule_id"],
                session_id=row["session_id"], state=row["state"],
                fire_at=datetime.fromisoformat(row["fire_at"]),
                deadline_at=(datetime.fromisoformat(row["deadline_at"])
                             if row["deadline_at"] else None),
                error=row["error"], created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    async def _append_event(
        self,
        connection: aiosqlite.Connection,
        rule_id: str,
        kind: str,
        data: Mapping[str, Any],
        *,
        fire_id: str | None = None,
    ) -> None:
        await connection.execute(
            """INSERT INTO watchtower_events
            (event_id, rule_id, fire_id, kind, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), rule_id, fire_id, kind, _json(data), _now().isoformat()),
        )
