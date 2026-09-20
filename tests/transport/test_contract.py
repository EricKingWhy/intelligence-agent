from __future__ import annotations

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


def test_generated_git_summary_never_contains_position_pathspec():
    summary = redact_command_summary(
        'git_status -- "secret.py" C:/private/repo/secret.py',
        scope="workspace",
    )
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
async def test_sqlite_transport_ledger_rejects_backdated_append(tmp_path):
    ledger = SqliteTransportLedger(tmp_path / "harness.db")
    await ledger.initialize()
    await ledger.append(_entry())
    with pytest.raises(ValueError, match="append-only"):
        await ledger.append(_entry(
            operation_id="op-2",
            created_at="2026-09-19T23:59:59+00:00",
        ))


@pytest.mark.asyncio
async def test_transport_ledger_is_append_only_and_queryable():
    ledger = InMemoryTransportLedger()
    first = _entry()
    second = _entry(
        operation_id="op-2",
        status=TransportStatus.FAILED,
        created_at="2026-09-20T00:00:01+00:00",
    )

    await ledger.append(first)
    await ledger.append(second)

    assert await ledger.list_for_request("req-1") == [first, second]
    with pytest.raises(ValueError, match="append-only"):
        await ledger.append(_entry(
            operation_id="op-3",
            created_at="2026-09-19T23:59:59+00:00",
        ))
