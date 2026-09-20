"""Transport-scoped audit and controlled artifact contracts.

This module freezes the contract needed before a transport adapter is wired into
ToolExecutor. It deliberately does not emit SessionEvents or execute commands.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_harness.storage.artifact import ARTIFACT_ID_PATTERN, SESSION_KEY_PATTERN

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9._:-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(--?(?:token|password|passwd|secret|api[-_]?key|authorization)|"
    r"(?:token|password|passwd|secret|api[-_]?key|authorization))\s*(?:=|:)\s*([^\s]+)"
)
_PATH_ASSIGNMENT = re.compile(r"(?i)(--?(?:path|file|cwd|workdir)|(?:path|file|cwd|workdir))\s*(?:=|:)\s*([^\s]+)")
_QUOTED_PATH = re.compile(r"(?:\"[^\"]+\"|'[^']+')")
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.-])(?:[A-Za-z]:[\\/]|/)[^\s]+")
_PATHSPEC_TAIL = re.compile(r"(\s--\s+).+$")


class TransportStatus(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactAccessPolicy(str, Enum):
    READ_ONLY = "read_only"


class ArtifactRetention(str, Enum):
    SESSION = "session"
    EXPLICIT = "explicit"


class TransportArtifactRef(BaseModel):
    """Immutable reference; it contains no output bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    session_id: str
    access: ArtifactAccessPolicy = ArtifactAccessPolicy.READ_ONLY
    retention: ArtifactRetention = ArtifactRetention.SESSION

    @field_validator("artifact_id")
    @classmethod
    def _valid_artifact_id(cls, value: str) -> str:
        if not ARTIFACT_ID_PATTERN.fullmatch(value):
            raise ValueError("artifact_id must be a content-addressed artifact id")
        return value

    @field_validator("session_id")
    @classmethod
    def _valid_session_id(cls, value: str) -> str:
        if not SESSION_KEY_PATTERN.fullmatch(value):
            raise ValueError("session_id must be one safe path segment")
        return value


class TransportLedgerEntry(BaseModel):
    """Small append-only audit record for one transport request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    operation_id: str
    command_summary: str = Field(min_length=1, max_length=500)
    scope: str = Field(min_length=1, max_length=500)
    status: TransportStatus
    duration_ms: int | None = Field(default=None, ge=0)
    artifact_ref: TransportArtifactRef | None = None
    created_at: str

    @field_validator("command_summary", mode="before")
    @classmethod
    def _redact_command_summary(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("command_summary must be text")
        return redact_command_summary(value, scope="ledger")

    @field_validator("request_id", "operation_id")
    @classmethod
    def _valid_ids(cls, value: str) -> str:
        if not _SAFE_TOKEN.fullmatch(value):
            raise ValueError("transport ids contain unsafe characters")
        return value

    @field_validator("created_at")
    @classmethod
    def _valid_timestamp(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value


class TransportLedger(Protocol):
    async def append(self, entry: TransportLedgerEntry) -> None: ...

    async def list_for_request(self, request_id: str) -> list[TransportLedgerEntry]: ...


_TRANSPORT_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS transport_ledger (
    request_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    command_summary TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed', 'cancelled')),
    duration_ms INTEGER,
    artifact_ref_json TEXT,
    created_at TEXT NOT NULL
)
"""


@asynccontextmanager
async def _connect(database_path: Path):
    connection = await aiosqlite.connect(database_path)
    try:
        await connection.execute("PRAGMA busy_timeout=10000")
        yield connection
    finally:
        await connection.close()


class SqliteTransportLedger:
    """Durable append-only transport audit in the application SQLite database."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with _connect(self.database_path) as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute(_TRANSPORT_LEDGER_DDL)
            await connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_transport_ledger_request "
                "ON transport_ledger(request_id, created_at)"
            )
            await connection.commit()

    async def append(self, entry: TransportLedgerEntry) -> None:
        async with _connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                "SELECT created_at FROM transport_ledger ORDER BY rowid DESC LIMIT 1"
            )
            latest = await cursor.fetchone()
            if latest is not None and entry.created_at < latest[0]:
                await connection.rollback()
                raise ValueError("transport ledger entries must be append-only")
            if entry.status in {
                TransportStatus.SUCCEEDED,
                TransportStatus.FAILED,
                TransportStatus.CANCELLED,
            }:
                cursor = await connection.execute(
                    """
                    SELECT 1 FROM transport_ledger
                    WHERE operation_id = ?
                      AND status IN ('succeeded', 'failed', 'cancelled')
                    LIMIT 1
                    """,
                    (entry.operation_id,),
                )
                if await cursor.fetchone() is not None:
                    await connection.commit()
                    return
            await connection.execute(
                """
                INSERT INTO transport_ledger (
                    request_id, operation_id, command_summary, scope, status,
                    duration_ms, artifact_ref_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.request_id,
                    entry.operation_id,
                    entry.command_summary,
                    entry.scope,
                    entry.status.value,
                    entry.duration_ms,
                    entry.artifact_ref.model_dump_json()
                    if entry.artifact_ref is not None else None,
                    entry.created_at,
                ),
            )
            await connection.commit()

    async def list_for_request(self, request_id: str) -> list[TransportLedgerEntry]:
        async with _connect(self.database_path) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(
                """
                SELECT request_id, operation_id, command_summary, scope, status,
                       duration_ms, artifact_ref_json, created_at
                FROM transport_ledger WHERE request_id = ? ORDER BY rowid
                """,
                (request_id,),
            )
            rows = await cursor.fetchall()
        return [
            TransportLedgerEntry(
                request_id=row["request_id"],
                operation_id=row["operation_id"],
                command_summary=row["command_summary"],
                scope=row["scope"],
                status=row["status"],
                duration_ms=row["duration_ms"],
                artifact_ref=(
                    TransportArtifactRef.model_validate_json(row["artifact_ref_json"])
                    if row["artifact_ref_json"] is not None else None
                ),
                created_at=row["created_at"],
            )
            for row in rows
        ]


class InMemoryTransportLedger:
    """Small fake for contract tests; production wiring uses a durable adapter."""

    def __init__(self) -> None:
        self._entries: list[TransportLedgerEntry] = []

    async def append(self, entry: TransportLedgerEntry) -> None:
        if self._entries and entry.created_at < self._entries[-1].created_at:
            raise ValueError("transport ledger entries must be append-only")
        self._entries.append(entry)

    async def list_for_request(self, request_id: str) -> list[TransportLedgerEntry]:
        return [entry for entry in self._entries if entry.request_id == request_id]


def redact_command_summary(command: str, *, scope: str) -> str:
    """Return bounded audit text without secrets or raw path arguments."""
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be non-empty")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope must be non-empty")
    redacted = _SECRET_ASSIGNMENT.sub(r"\1=<redacted>", command)
    redacted = _PATH_ASSIGNMENT.sub(r"\1=<scoped>", redacted)
    redacted = _QUOTED_PATH.sub("<scoped>", redacted)
    redacted = _ABSOLUTE_PATH.sub("<scoped>", redacted)
    redacted = _PATHSPEC_TAIL.sub(r"\1<scoped>", redacted)
    redacted = re.sub(r"(?i)(Bearer\s+)[^\s]+", r"\1<redacted>", redacted)
    return redacted[:500]


def new_transport_entry(
    *,
    request_id: str,
    operation_id: str,
    command: str,
    scope: str,
    status: TransportStatus,
    duration_ms: int | None = None,
    artifact_ref: TransportArtifactRef | None = None,
) -> TransportLedgerEntry:
    return TransportLedgerEntry(
        request_id=request_id,
        operation_id=operation_id,
        command_summary=redact_command_summary(command, scope=scope),
        scope=scope,
        status=status,
        duration_ms=duration_ms,
        artifact_ref=artifact_ref,
        created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
    )
