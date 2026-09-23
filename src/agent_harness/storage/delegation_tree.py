"""Durable, process-safe accounting for a single multi-agent delegation tree."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import aiosqlite

from agent_harness.agent.guards import GuardLevel, GuardSignal


@dataclass(frozen=True)
class DelegationReservation:
    accepted: bool
    used: int
    limit: int


@dataclass(frozen=True)
class DelegationTreeState:
    tree_id: str
    root_session_id: str
    max_delegations: int
    used_delegations: int
    max_depth: int
    fingerprint: str | None
    consecutive_failures: int
    soft_triggered: bool


class DelegationTreeLedger(Protocol):
    async def reserve(
        self, tree_id: str, *, root_session_id: str,
        max_delegations: int, max_depth: int,
    ) -> DelegationReservation: ...

    async def observe_result(
        self, tree_id: str, fingerprint: str, *, ok: bool,
        soft_threshold: int = 3, hard_threshold: int = 3,
    ) -> GuardSignal: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegation_trees (
    tree_id TEXT PRIMARY KEY,
    root_session_id TEXT NOT NULL,
    max_delegations INTEGER NOT NULL CHECK (max_delegations >= 0),
    used_delegations INTEGER NOT NULL DEFAULT 0 CHECK (used_delegations >= 0),
    max_depth INTEGER NOT NULL CHECK (max_depth >= 0),
    fingerprint TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    soft_triggered INTEGER NOT NULL DEFAULT 0 CHECK (soft_triggered IN (0, 1))
);
CREATE TABLE IF NOT EXISTS delegation_tree_events (
    event_id TEXT PRIMARY KEY,
    tree_id TEXT NOT NULL REFERENCES delegation_trees(tree_id),
    kind TEXT NOT NULL,
    fingerprint TEXT,
    used_delegations INTEGER NOT NULL,
    max_delegations INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_delegation_tree_events_tree
    ON delegation_tree_events(tree_id, created_at);
CREATE TRIGGER IF NOT EXISTS delegation_tree_events_no_update
BEFORE UPDATE ON delegation_tree_events
BEGIN
    SELECT RAISE(ABORT, 'delegation tree audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS delegation_tree_events_no_delete
BEFORE DELETE ON delegation_tree_events
BEGIN
    SELECT RAISE(ABORT, 'delegation tree audit events are append-only');
END;
"""


class SqliteDelegationTreeLedger:
    """SQLite ledger with serialized budget reservations and append-only audit rows.

    A tree's configured limits are pinned on first use. A later activation can only
    tighten those limits; recreating a Runtime or Store cannot replenish the budget.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.database_path, timeout=10)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA busy_timeout = 10000")
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> None:
        async with self._connect() as connection:
            await connection.executescript(_SCHEMA)
            await connection.commit()

    async def reserve(
        self, tree_id: str, *, root_session_id: str,
        max_delegations: int, max_depth: int,
    ) -> DelegationReservation:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._ensure_tree(
                    connection, tree_id, root_session_id,
                    max_delegations=max_delegations, max_depth=max_depth,
                )
                used, limit = int(row["used_delegations"]), int(row["max_delegations"])
                accepted = used < limit
                if accepted:
                    used += 1
                    await connection.execute(
                        "UPDATE delegation_trees SET used_delegations = ? WHERE tree_id = ?",
                        (used, tree_id),
                    )
                await self._append_event(
                    connection, tree_id, "attempt_reserved" if accepted else "attempt_rejected",
                    fingerprint=None, used=used, limit=limit,
                )
                await connection.commit()
                return DelegationReservation(accepted, used, limit)
            except BaseException:
                await connection.rollback()
                raise

    async def observe_result(
        self, tree_id: str, fingerprint: str, *, ok: bool,
        soft_threshold: int = 3, hard_threshold: int = 3,
    ) -> GuardSignal:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await connection.execute_fetchall(
                    "SELECT * FROM delegation_trees WHERE tree_id = ?", (tree_id,),
                )
                if not row:
                    raise KeyError(f"unknown delegation tree: {tree_id}")
                state = row[0]
                previous = state["fingerprint"]
                failures = int(state["consecutive_failures"])
                soft_triggered = bool(state["soft_triggered"])
                if fingerprint != previous:
                    failures, soft_triggered = 0, False
                level = GuardLevel.NONE
                if not ok:
                    failures += 1
                    if not soft_triggered and failures >= soft_threshold:
                        soft_triggered = True
                        level = GuardLevel.SOFT
                    elif soft_triggered and failures >= soft_threshold + hard_threshold:
                        level = GuardLevel.HARD
                await connection.execute(
                    """UPDATE delegation_trees
                       SET fingerprint = ?, consecutive_failures = ?, soft_triggered = ?
                       WHERE tree_id = ?""",
                    (fingerprint, failures, int(soft_triggered), tree_id),
                )
                used, limit = int(state["used_delegations"]), int(state["max_delegations"])
                await self._append_event(
                    connection, tree_id, "result_ok" if ok else "result_failed",
                    fingerprint=fingerprint, used=used, limit=limit,
                )
                if level != GuardLevel.NONE:
                    await self._append_event(
                        connection, tree_id,
                        "guard_soft" if level == GuardLevel.SOFT else "guard_hard",
                        fingerprint=fingerprint, used=used, limit=limit,
                    )
                await connection.commit()
                return GuardSignal(level, "delegate", fingerprint, failures)
            except BaseException:
                await connection.rollback()
                raise

    async def get_state(self, tree_id: str) -> DelegationTreeState:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM delegation_trees WHERE tree_id = ?", (tree_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"unknown delegation tree: {tree_id}")
        return DelegationTreeState(
            tree_id=row["tree_id"], root_session_id=row["root_session_id"],
            max_delegations=row["max_delegations"],
            used_delegations=row["used_delegations"], max_depth=row["max_depth"],
            fingerprint=row["fingerprint"],
            consecutive_failures=row["consecutive_failures"],
            soft_triggered=bool(row["soft_triggered"]),
        )

    async def event_kinds(self, tree_id: str) -> list[str]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT kind FROM delegation_tree_events WHERE tree_id = "
                "? ORDER BY rowid", (tree_id,),
            )
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def _ensure_tree(
        self, connection: aiosqlite.Connection, tree_id: str, root_session_id: str,
        *, max_delegations: int, max_depth: int,
    ):
        await connection.execute(
            """INSERT OR IGNORE INTO delegation_trees
               (tree_id, root_session_id, max_delegations, max_depth)
               VALUES (?, ?, ?, ?)""",
            (tree_id, root_session_id, max_delegations, max_depth),
        )
        cursor = await connection.execute(
            "SELECT * FROM delegation_trees WHERE tree_id = ?", (tree_id,),
        )
        row = await cursor.fetchone()
        if row["root_session_id"] != root_session_id:
            raise ValueError("delegation tree root identity mismatch")
        # Configuration drift during resume is fail-closed: an existing tree may
        # tighten its ceiling, never raise it or change its root identity.
        limit = min(int(row["max_delegations"]), max_delegations)
        depth = min(int(row["max_depth"]), max_depth)
        if limit != row["max_delegations"] or depth != row["max_depth"]:
            await connection.execute(
                "UPDATE delegation_trees SET max_delegations = ?, max_depth = ? "
                "WHERE tree_id = ?", (limit, depth, tree_id),
            )
            row = dict(row)
            row["max_delegations"] = limit
            row["max_depth"] = depth
        return row

    @staticmethod
    async def _append_event(
        connection: aiosqlite.Connection, tree_id: str, kind: str,
        *, fingerprint: str | None, used: int, limit: int,
    ) -> None:
        await connection.execute(
            """INSERT INTO delegation_tree_events
               (event_id, tree_id, kind, fingerprint, used_delegations, max_delegations)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), tree_id, kind, fingerprint, used, limit),
        )


class InMemoryDelegationTreeLedger:
    """Process-local fallback for isolated Runtime constructions without app stores."""

    def __init__(self) -> None:
        self._trees: dict[str, dict[str, object]] = {}
        self._events: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def reserve(
        self, tree_id: str, *, root_session_id: str,
        max_delegations: int, max_depth: int,
    ) -> DelegationReservation:
        async with self._lock:
            state = self._ensure(tree_id, root_session_id, max_delegations, max_depth)
            state["max_delegations"] = min(int(state["max_delegations"]), max_delegations)
            state["max_depth"] = min(int(state["max_depth"]), max_depth)
            used, limit = int(state["used_delegations"]), int(state["max_delegations"])
            accepted = used < limit
            if accepted:
                used += 1
                state["used_delegations"] = used
            self._events[tree_id].append("attempt_reserved" if accepted else "attempt_rejected")
            return DelegationReservation(accepted, used, limit)

    async def observe_result(
        self, tree_id: str, fingerprint: str, *, ok: bool,
        soft_threshold: int = 3, hard_threshold: int = 3,
    ) -> GuardSignal:
        async with self._lock:
            state = self._trees[tree_id]
            if fingerprint != state["fingerprint"]:
                state["fingerprint"] = fingerprint
                state["consecutive_failures"] = 0
                state["soft_triggered"] = False
            failures = int(state["consecutive_failures"])
            soft = bool(state["soft_triggered"])
            level = GuardLevel.NONE
            if not ok:
                failures += 1
                if not soft and failures >= soft_threshold:
                    soft, level = True, GuardLevel.SOFT
                elif soft and failures >= soft_threshold + hard_threshold:
                    level = GuardLevel.HARD
            state["consecutive_failures"] = failures
            state["soft_triggered"] = soft
            self._events[tree_id].append("result_ok" if ok else "result_failed")
            if level != GuardLevel.NONE:
                self._events[tree_id].append(
                    "guard_soft" if level == GuardLevel.SOFT else "guard_hard",
                )
            return GuardSignal(level, "delegate", fingerprint, failures)

    async def get_state(self, tree_id: str) -> DelegationTreeState:
        async with self._lock:
            state = self._trees[tree_id]
            return DelegationTreeState(
                tree_id=tree_id,
                root_session_id=str(state["root_session_id"]),
                max_delegations=int(state["max_delegations"]),
                used_delegations=int(state["used_delegations"]),
                max_depth=int(state["max_depth"]),
                fingerprint=state["fingerprint"],
                consecutive_failures=int(state["consecutive_failures"]),
                soft_triggered=bool(state["soft_triggered"]),
            )

    async def event_kinds(self, tree_id: str) -> list[str]:
        async with self._lock:
            return list(self._events[tree_id])

    def _ensure(
        self, tree_id: str, root_session_id: str,
        max_delegations: int, max_depth: int,
    ) -> dict[str, object]:
        if tree_id not in self._trees:
            self._trees[tree_id] = {
                "root_session_id": root_session_id,
                "max_delegations": max_delegations,
                "used_delegations": 0,
                "max_depth": max_depth,
                "fingerprint": None,
                "consecutive_failures": 0,
                "soft_triggered": False,
            }
            self._events[tree_id] = []
        elif self._trees[tree_id]["root_session_id"] != root_session_id:
            raise ValueError("delegation tree root identity mismatch")
        return self._trees[tree_id]
