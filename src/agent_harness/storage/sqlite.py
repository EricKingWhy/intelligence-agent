"""SQLite persistence adapters.

三个 Store（OperationLedger / CheckpointStore / SessionMetaStore）物理上共享同一
SQLite 文件，逻辑上各自独立 contract（ADR-0004 Round 3 §三张表）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from agent_harness.storage.checkpoint import (
    Checkpoint,
    CheckpointStore,
)
from agent_harness.storage.operation import (
    Operation,
    OperationLedger,
    OperationState,
)
from agent_harness.storage.session_meta import SessionMeta, SessionMetaStore

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
            # 崩溃恢复语义（07 §4）：RUNNING 先进 UNKNOWN，不允许跳步直达
            # NEED_RECONCILE——两步链由状态机强制（#30）。
            OperationState.UNKNOWN,
        }
    ),
    # UNKNOWN 只能进入待裁决态；终态必须经 ReconcileCallback 裁决后从
    # NEED_RECONCILE 达成（#30：UNKNOWN 与 NEED_RECONCILE 始终询问用户）。
    OperationState.UNKNOWN: frozenset({OperationState.NEED_RECONCILE}),
    OperationState.NEED_RECONCILE: frozenset(
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


# ── Checkpoint / SessionMeta schema（共享同一 .db 文件，独立 contract）──

_CHECKPOINTS_DDL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    session_id    TEXT NOT NULL,
    boundary_type TEXT NOT NULL CHECK (boundary_type IN (
        'USER_ACCEPTED', 'MODEL_COMPLETED', 'TOOL_BATCH_COMPLETED', 'FINAL_COMPLETED'
    )),
    event_seq     INTEGER NOT NULL,
    payload_json  TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (session_id, boundary_type, event_seq)
)
"""

_SESSION_META_DDL = """
CREATE TABLE IF NOT EXISTS session_meta (
    session_id          TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    agent_id            TEXT,
    last_checkpoint_seq INTEGER,
    archived            BOOLEAN DEFAULT 0
)
"""


class SqliteCheckpointStore(CheckpointStore):
    """默认本地 Checkpoint Store，与 Operation Ledger 共享同一个 SQLite 文件。

    schema 与 #26 冻结一致：PRIMARY KEY (session_id, boundary_type, event_seq)。
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute(_CHECKPOINTS_DDL)
            await connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoints_session "
                "ON checkpoints(session_id, event_seq)"
            )
            await connection.commit()

    async def save(self, checkpoint: Checkpoint) -> None:
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                """
                INSERT INTO checkpoints (
                    session_id, boundary_type, event_seq, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.session_id,
                    checkpoint.boundary_type.value,
                    checkpoint.event_seq,
                    checkpoint.payload_json,
                    checkpoint.created_at,
                ),
            )
            await connection.commit()

    async def list_for_session(self, session_id: str) -> list[Checkpoint]:
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE session_id = ?
                ORDER BY event_seq
                """,
                (session_id,),
            )
            rows = await cursor.fetchall()
        return [self._to_checkpoint(row) for row in rows]

    async def latest(self, session_id: str) -> Checkpoint | None:
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE session_id = ?
                ORDER BY event_seq DESC
                LIMIT 1
                """,
                (session_id,),
            )
            row = await cursor.fetchone()
        return self._to_checkpoint(row) if row is not None else None

    @staticmethod
    def _to_checkpoint(row: aiosqlite.Row) -> Checkpoint:
        return Checkpoint.model_validate(
            {**dict(row), "boundary_type": row["boundary_type"]}
        )


class SqliteSessionMetaStore(SessionMetaStore):
    """默认本地 Session Metadata Store，与其它 Store 共享同一个 SQLite 文件。

    schema 与 #26 冻结一致；支持 archived 标记 + 显式 cleanup（不自动 TTL）。
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute(_SESSION_META_DDL)
            await connection.commit()

    async def upsert(self, meta: SessionMeta) -> SessionMeta:
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                """
                INSERT INTO session_meta (
                    session_id, created_at, agent_id, last_checkpoint_seq, archived
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    agent_id = excluded.agent_id,
                    last_checkpoint_seq = excluded.last_checkpoint_seq,
                    archived = excluded.archived
                """,
                (
                    meta.session_id,
                    meta.created_at,
                    meta.agent_id,
                    meta.last_checkpoint_seq,
                    1 if meta.archived else 0,
                ),
            )
            await connection.commit()
        loaded = await self.get(meta.session_id)
        assert loaded is not None
        return loaded

    async def get(self, session_id: str) -> SessionMeta | None:
        async with aiosqlite.connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                "SELECT * FROM session_meta WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
        return self._to_meta(row) if row is not None else None

    async def set_archived(self, session_id: str, archived: bool = True) -> SessionMeta:
        async with aiosqlite.connect(self.database_path) as connection:
            cursor = await connection.execute(
                "UPDATE session_meta SET archived = ? WHERE session_id = ?",
                (1 if archived else 0, session_id),
            )
            await connection.commit()
            if cursor.rowcount != 1:
                raise KeyError(f"SessionMeta '{session_id}' does not exist")
        loaded = await self.get(session_id)
        assert loaded is not None
        return loaded

    async def update_last_checkpoint_seq(
        self, session_id: str, event_seq: int
    ) -> SessionMeta:
        async with aiosqlite.connect(self.database_path) as connection:
            cursor = await connection.execute(
                "UPDATE session_meta SET last_checkpoint_seq = ? WHERE session_id = ?",
                (event_seq, session_id),
            )
            await connection.commit()
            if cursor.rowcount != 1:
                raise KeyError(f"SessionMeta '{session_id}' does not exist")
        loaded = await self.get(session_id)
        assert loaded is not None
        return loaded

    async def cleanup(self, session_id: str) -> None:
        async with aiosqlite.connect(self.database_path) as connection:
            await connection.execute(
                "DELETE FROM session_meta WHERE session_id = ?",
                (session_id,),
            )
            await connection.commit()

    @staticmethod
    def _to_meta(row: aiosqlite.Row) -> SessionMeta:
        return SessionMeta.model_validate(
            {**dict(row), "archived": bool(row["archived"])}
        )
