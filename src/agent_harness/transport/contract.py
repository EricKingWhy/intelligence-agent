"""Transport-scoped audit and controlled artifact contracts.

This module freezes the contract needed before a transport adapter is wired into
ToolExecutor. It deliberately does not emit SessionEvents or execute commands.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_harness.storage.artifact import ARTIFACT_ID_PATTERN, SESSION_KEY_PATTERN

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9._:-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(--?(?:token|password|passwd|secret|api[-_]?key|authorization)|"
    r"(?:token|password|passwd|secret|api[-_]?key|authorization))\s*(?:=|:)\s*([^\s]+)"
)
_PATH_ASSIGNMENT = re.compile(r"(?i)(--?(?:path|file|cwd|workdir)|(?:path|file|cwd|workdir))\s*(?:=|:)\s*([^\s]+)")


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


class InMemoryTransportLedger:
    """Small fake for contract tests; production wiring chooses its adapter later."""

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
