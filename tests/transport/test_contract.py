from __future__ import annotations

import asyncio
import sqlite3

import pytest
from pydantic import ValidationError

from agent_harness.transport import (
    ArtifactAccessPolicy,
    ArtifactRetention,
    InMemoryTransportLedger,
    SqliteTransportLedger,
    TransportArtifactRef,
    TransportLedgerEntry,
    TransportStatus,
    redact_command_summary,
)


def _entry(**overrides):
    values = {
        "request_id": "req-1",
        "session_id": "session-1",
        "operation_id": "op-1",
        "command_summary": "git diff -- path=<scoped>",
        "scope": "workspace/subdir",
        "status": TransportStatus.SUCCEEDED,
        "duration_ms": 12,
        "created_at": "2026-09-20T00:00:00+00:00",
    }
    values.update(overrides)
    return TransportLedgerEntry(**values)


def test_entry_is_small_and_artifact_ref_is_immutable():
    ref = TransportArtifactRef(
        artifact_id="0123456789abcdef",
        session_id="session-1",
    )
    entry = _entry(artifact_ref=ref)

    assert entry.artifact_ref is not None
    assert entry.artifact_ref.access is ArtifactAccessPolicy.READ_ONLY
    assert entry.artifact_ref.retention is ArtifactRetention.SESSION
    assert "stdout" not in entry.model_dump_json()
    with pytest.raises(ValidationError):
        entry.artifact_ref.artifact_id = "fedcba9876543210"


def test_artifact_ref_rejects_wrong_namespace_or_id():
    with pytest.raises(ValidationError):
        TransportArtifactRef(artifact_id="../../secret", session_id="session-1")
    with pytest.raises(ValidationError):
        TransportArtifactRef(artifact_id="0123456789abcdef", session_id="../other")
    with pytest.raises(ValidationError):
        TransportArtifactRef(
            artifact_id="0123456789abcdef",
            session_id="session-1",
            retention="forever",
        )


def test_command_summary_redacts_secret_and_raw_paths():
    summary = redact_command_summary(
        "git diff --path=/private/repo --token=secret-value Authorization:Bearer-secret",
        scope="workspace",
    )

    assert "secret-value" not in summary
    assert "Bearer-secret" not in summary
    assert "/private/repo" not in summary
    assert "<redacted>" in summary
    assert "<scoped>" in summary
    assert "secret-do-not-log" not in redact_command_summary(
        "git -c credential.helper=secret-do-not-log diff", scope="workspace"
    )
    assert "Bearer super-secret" not in redact_command_summary(
        'git -c http.extraheader="Authorization: Bearer super-secret" diff',
        scope="workspace",
    )
    assert "super-secret" not in redact_command_summary(
        "git -c http.extraheader=Authorization: Bearer super-secret diff",
        scope="workspace",
    )
    assert "user:secret@host" not in redact_command_summary(
        'git -c url."https://user:secret@host/".insteadOf=https://host/ diff',
        scope="workspace",
    )


def test_generated_git_summary_never_contains_position_pathspec():
    for command in (
        'git_status -- "secret.py" C:/private/repo/secret.py',
        "git diff -- secret.py",
    ):
        summary = redact_command_summary(command, scope="workspace")
        assert "secret.py" not in summary
        assert "private/repo" not in summary


def test_entry_redacts_direct_construction_and_caps_summary():
    entry = _entry(command_summary="token=secret " + "x" * 1000)

    assert "secret" not in entry.command_summary
    assert len(entry.command_summary) <= 500


@pytest.mark.asyncio
async def test_sqlite_transport_ledger_survives_reopen(tmp_path):
    first = SqliteTransportLedger(tmp_path / "harness.db")
    await first.initialize()
    entry = _entry(artifact_ref=TransportArtifactRef(
        artifact_id="0123456789abcdef", session_id="session-1"
    ))
    await first.append(entry)

    reopened = SqliteTransportLedger(tmp_path / "harness.db")
    await reopened.initialize()
    assert await reopened.list_for_request("req-1") == [entry]


@pytest.mark.asyncio
async def test_sqlite_transport_ledger_migrates_existing_table_and_scopes_deletion(tmp_path):
    database = tmp_path / "harness.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE transport_ledger (
                request_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                command_summary TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER,
                artifact_ref_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.commit()

    ledger = SqliteTransportLedger(database)
    await ledger.initialize()
    await ledger.append(_entry(
        request_id="req-target",
        session_id="session-target",
        operation_id="op-target",
        status=TransportStatus.STARTED,
    ))
    await ledger.append(_entry(
        request_id="req-other",
        session_id="session-other",
        operation_id="op-other",
        status=TransportStatus.STARTED,
    ))

    assert await ledger.delete_for_session("session-target") == 1
    assert await ledger.list_for_request("req-target") == []
    assert len(await ledger.list_for_request("req-other")) == 1


@pytest.mark.asyncio
async def test_sqlite_transport_ledger_migration_uses_artifact_session_owner(tmp_path):
    database = tmp_path / "harness.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE transport_ledger (
                request_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                command_summary TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER,
                artifact_ref_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO transport_ledger VALUES
            ('legacy-request', 'legacy-operation', 'git_status', '.', 'succeeded',
             NULL, '{"artifact_id":"0123456789abcdef","session_id":"session-owned"}',
             '2026-09-20T00:00:00+00:00')
            """
        )
        connection.commit()

    ledger = SqliteTransportLedger(database)
    await ledger.initialize()
    assert await ledger.delete_for_session("session-owned") == 1
    assert await ledger.list_for_request("legacy-request") == []


@pytest.mark.asyncio
async def test_sqlite_transport_ledger_migration_ignores_malformed_or_invalid_owners(tmp_path):
    database = tmp_path / "harness.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE transport_ledger (
                request_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                command_summary TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER,
                artifact_ref_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO transport_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("bad-json", "op-bad", "git_status", ".", "started", None, "{", "2026-09-20T00:00:00+00:00"),
                ("bad-owner", "op-owner", "git_status", ".", "started", None, '{"session_id":"../escape"}', "2026-09-20T00:00:00+00:00"),
                ("bad-artifact", "op-artifact", "git_status", ".", "started", None, '{"artifact_id":"../../bad","session_id":"session-owned"}', "2026-09-20T00:00:00+00:00"),
                ("retired-policy", "op-policy", "git_status", ".", "started", None, '{"artifact_id":"0123456789abcdef","session_id":"session-owned","retention":"forever"}', "2026-09-20T00:00:00+00:00"),
            ],
        )
        connection.commit()

    ledger = SqliteTransportLedger(database)
    await ledger.initialize()
    for request_id in ("bad-json", "bad-owner", "bad-artifact", "retired-policy"):
        [entry] = await ledger.list_for_request(request_id)
        assert entry.session_id == "legacy-transport"
        assert entry.artifact_ref is None
    assert await ledger.delete_for_session("session-owned") == 0
    assert await ledger.delete_for_session("legacy-transport") == 4


@pytest.mark.asyncio
async def test_sqlite_transport_ledger_deduplicates_terminal_append(tmp_path):
    ledger = SqliteTransportLedger(tmp_path / "harness.db")
    await ledger.initialize()
    started = _entry(status=TransportStatus.STARTED)
    await ledger.append(started)
    terminal = _entry(
        status=TransportStatus.SUCCEEDED,
        created_at="2026-09-20T00:00:01+00:00",
    )
    await ledger.append(terminal)
    await ledger.append(terminal)
    assert await ledger.list_for_request("req-1") == [started, terminal]


@pytest.mark.asyncio
async def test_sqlite_transport_ledger_rejects_conflicting_terminal(tmp_path):
    ledger = SqliteTransportLedger(tmp_path / "harness.db")
    await ledger.initialize()
    await ledger.append(_entry(status=TransportStatus.STARTED))
    await ledger.append(_entry(
        status=TransportStatus.SUCCEEDED,
        created_at="2026-09-20T00:00:01+00:00",
    ))
    with pytest.raises(ValueError, match="terminal state conflicts"):
        await ledger.append(_entry(
            status=TransportStatus.FAILED,
            created_at="2026-09-20T00:00:02+00:00",
        ))


@pytest.mark.asyncio
async def test_sqlite_transport_ledger_concurrent_appends_keep_both_entries(tmp_path):
    database = tmp_path / "harness.db"
    first = SqliteTransportLedger(database)
    second = SqliteTransportLedger(database)
    await first.initialize()
    earlier = _entry(operation_id="op-1", created_at="2026-09-20T00:00:00+00:00")
    later = _entry(operation_id="op-2", created_at="2026-09-20T00:00:01+00:00")

    await asyncio.gather(first.append(later), second.append(earlier))

    entries = await first.list_for_request("req-1")
    assert {entry.operation_id for entry in entries} == {"op-1", "op-2"}


@pytest.mark.asyncio
async def test_transport_ledger_is_append_only_and_queryable():
    ledger = InMemoryTransportLedger()
    first = _entry()
    second = _entry(
        operation_id="op-2",
        status=TransportStatus.FAILED,
        created_at="2026-09-20T00:00:01+00:00",
    )
    backdated = _entry(
        operation_id="op-3",
        created_at="2026-09-19T23:59:59+00:00",
    )

    await ledger.append(first)
    await ledger.append(second)
    await ledger.append(backdated)

    assert await ledger.list_for_request("req-1") == [first, second, backdated]
