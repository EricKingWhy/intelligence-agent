"""SQLite persistence adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from agent_harness.storage.operation import (
    Operation,
    OperationLedger,
    OperationState,
)

_OPERATIONS_DDL = """
CREATE TABLE IF NOT EXISTS operations (
    tool_call_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT,
    agent_id TEXT,
    tool_name TEXT NOT NULL,
    args_identity TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED',
        'UNKNOWN', 'NEED_RECONCILE'
    )),
    result_json TEXT,
    artifact_ref TEXT,
    started_at TEXT,
    finished_at TEXT,
    reconcile_meta TEXT
)
"""

_ALLOWED_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.PENDING: frozenset(
        {OperationState.RUNNING, OperationState.CANCELLED}
    ),
    OperationState.RUNNING: frozenset(
        {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }
    ),
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class SqliteOperationLedger(OperationLedger):
    """Default local Operation Ledger backed by one SQLite database file."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute(_OPERATIONS_DDL)
            await connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_operations_session "
                "ON operations(session_id, started_at)"
            )
            await connection.commit()

    async def create(self, operation: Operation) -> None:
        if operation.state is not OperationState.PENDING:
            raise ValueError("A new Operation must start in PENDING")
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                """
                INSERT INTO operations (
                    tool_call_id, session_id, run_id, agent_id, tool_name,
                    args_identity, state, result_json, artifact_ref, started_at,
                    finished_at, reconcile_meta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation.tool_call_id,
                    operation.session_id,
                    operation.run_id,
                    operation.agent_id,
                    operation.tool_name,
                    operation.args_identity,
                    operation.state.value,
                    operation.result_json,
                    operation.artifact_ref,
                    operation.started_at,
                    operation.finished_at,
                    operation.reconcile_meta,
                ),
            )
            await connection.commit()

    async def get(self, tool_call_id: str) -> Operation | None:
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM operations WHERE tool_call_id = ?", (tool_call_id,)
            )
            row = await cursor.fetchone()
        return self._to_operation(row) if row is not None else None

    async def update_state(
        self,
        tool_call_id: str,
        state: OperationState,
        *,
        result_json: str | None = None,
        artifact_ref: str | None = None,
        reconcile_meta: str | None = None,
    ) -> Operation:
        current = await self.get(tool_call_id)
        if current is None:
            raise KeyError(f"Operation '{tool_call_id}' does not exist")
        if state not in _ALLOWED_TRANSITIONS.get(current.state, frozenset()):
            raise ValueError(
                f"Invalid Operation transition: {current.state.value} -> {state.value}"
            )

        finished_at = (
            _utc_now_iso()
            if state
            in {
                OperationState.SUCCEEDED,
                OperationState.FAILED,
                OperationState.CANCELLED,
            }
            else None
        )
        async with aiosqlite.connect(self.database_path) as connection:
            cursor = await connection.execute(
                """
                UPDATE operations
                SET state = ?, result_json = COALESCE(?, result_json),
                    artifact_ref = COALESCE(?, artifact_ref),
                    finished_at = COALESCE(?, finished_at),
                    reconcile_meta = COALESCE(?, reconcile_meta)
                WHERE tool_call_id = ? AND state = ?
                """,
                (
                    state.value,
                    result_json,
                    artifact_ref,
                    finished_at,
                    reconcile_meta,
                    tool_call_id,
                    current.state.value,
                ),
            )
            await connection.commit()
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Operation '{tool_call_id}' changed concurrently"
                )
        updated = await self.get(tool_call_id)
        assert updated is not None
        return updated

    async def list_for_session(self, session_id: str) -> list[Operation]:
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """
                SELECT * FROM operations
                WHERE session_id = ?
                ORDER BY started_at, tool_call_id
                """,
                (session_id,),
            )
            rows = await cursor.fetchall()
        return [self._to_operation(row) for row in rows]

    @staticmethod
    def _to_operation(row: aiosqlite.Row) -> Operation:
        return Operation.model_validate(dict(row))
