from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import memory_freeze_backup

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "memory_freeze_backup.py"


def run_tool(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )


def key_env() -> dict[str, str]:
    return {"MEMORY_BACKUP_KEY": base64.b64encode(secrets.token_bytes(32)).decode("ascii")}


def test_online_backup_does_not_create_a_missing_source_database(tmp_path: Path) -> None:
    source = tmp_path / "vanished.db"

    with pytest.raises(memory_freeze_backup.BackupError, match="SQLite Online Backup API failed"):
        memory_freeze_backup._online_backup(source)

    assert not source.exists()


def test_backup_rejects_a_workspace_without_a_memory_database(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0
    for name in memory_freeze_backup.DATABASE_NAMES:
        for suffix in ("", *memory_freeze_backup.DATABASE_SIDECARS):
            (workspace / f"{name}{suffix}").unlink(missing_ok=True)

    output = tmp_path / "offline" / "memory-backup.iab"
    result = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(output),
        "--writer-proof",
        str(workspace / "writer-proof.json"),
        "--rehearsal-fixture",
        env=key_env(),
    )

    assert result.returncode != 0
    assert "no memory sqlite database" in result.stderr.lower()
    assert not output.exists()


@pytest.mark.parametrize("kept_database", memory_freeze_backup.DATABASE_NAMES)
def test_backup_and_restore_accept_one_memory_database(
    tmp_path: Path, kept_database: str
) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0
    for name in memory_freeze_backup.DATABASE_NAMES:
        if name == kept_database:
            continue
        for suffix in ("", *memory_freeze_backup.DATABASE_SIDECARS):
            (workspace / f"{name}{suffix}").unlink(missing_ok=True)

    key = key_env()
    archive = tmp_path / "offline" / "memory-backup.iab"
    backed_up = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(archive),
        "--writer-proof",
        str(workspace / "writer-proof.json"),
        "--rehearsal-fixture",
        env=key,
    )
    assert backed_up.returncode == 0, backed_up.stderr

    restored = run_tool(
        "restore",
        "--archive",
        str(archive),
        "--restore-root",
        str(tmp_path / "restore"),
        env=key,
    )
    assert restored.returncode == 0, restored.stderr
    assert set(json.loads(restored.stdout)["database_manifests"]) == {kept_database}


def test_backup_publish_does_not_clobber_a_racing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0
    output = tmp_path / "offline" / "memory-backup.iab"
    monkeypatch.setenv("MEMORY_BACKUP_KEY", key_env()["MEMORY_BACKUP_KEY"])

    def create_racing_destination(*args: object) -> None:
        temporary = args[5]
        assert isinstance(temporary, Path)
        temporary.write_bytes(b"new encrypted backup")
        output.write_bytes(b"existing destination")

    monkeypatch.setattr(memory_freeze_backup, "_make_archive", create_racing_destination)

    with pytest.raises(memory_freeze_backup.BackupError, match="refusing to overwrite"):
        memory_freeze_backup._backup(
            workspace,
            output,
            workspace / "writer-proof.json",
            "MEMORY_BACKUP_KEY",
            True,
        )

    assert output.read_bytes() == b"existing destination"


def test_archive_failure_preserves_a_preexisting_temporary_path(tmp_path: Path) -> None:
    temporary = tmp_path / ".memory-backup.iab.1234.tmp"
    temporary.write_bytes(b"preexisting file")

    with pytest.raises(FileExistsError):
        memory_freeze_backup._make_archive(tmp_path, [], {}, {}, {}, temporary, b"k" * 32)

    assert temporary.read_bytes() == b"preexisting file"


def test_archive_failure_reports_when_owned_temporary_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / ".memory-backup.iab.partial.tmp"
    original_unlink = Path.unlink

    def fail_archive(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated archive write failure")

    def fail_temporary_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if path == temporary:
            raise PermissionError("simulated cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(memory_freeze_backup, "_tar_add_bytes", fail_archive)
    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)

    with pytest.raises(memory_freeze_backup.BackupError) as error:
        memory_freeze_backup._make_archive(tmp_path, [], {}, {}, {}, temporary, b"k" * 32)

    assert str(temporary) in str(error.value)
    assert "simulated cleanup failure" in str(error.value)
    assert temporary.exists()


def test_backup_validation_failure_reports_owned_temporary_cleanup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0
    monkeypatch.setenv("MEMORY_BACKUP_KEY", key_env()["MEMORY_BACKUP_KEY"])
    output = tmp_path / "offline" / "memory-backup.iab"
    original_inventory = memory_freeze_backup._source_inventory
    inventory_calls = 0
    temporary_paths: list[Path] = []

    def write_temporary_archive(*args: object) -> None:
        temporary = args[5]
        assert isinstance(temporary, Path)
        temporary_paths.append(temporary)
        temporary.write_bytes(b"encrypted temporary")

    def change_inventory(root: Path) -> list[dict[str, object]]:
        nonlocal inventory_calls
        inventory_calls += 1
        return original_inventory(root) if inventory_calls == 1 else []

    original_unlink = Path.unlink

    def fail_temporary_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        if temporary_paths and path == temporary_paths[0]:
            raise PermissionError("simulated cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(memory_freeze_backup, "_make_archive", write_temporary_archive)
    monkeypatch.setattr(memory_freeze_backup, "_source_inventory", change_inventory)
    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)

    with pytest.raises(memory_freeze_backup.BackupError) as error:
        memory_freeze_backup._backup(
            workspace,
            output,
            workspace / "writer-proof.json",
            "MEMORY_BACKUP_KEY",
            True,
        )

    assert str(temporary_paths[0]) in str(error.value)
    assert "simulated cleanup failure" in str(error.value)
    assert "workspace changed during freeze" in str(error.value)
    assert temporary_paths[0].exists()


def test_backup_requires_a_complete_writer_proof(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    seeded = run_tool("seed", "--workspace", str(workspace))
    assert seeded.returncode == 0, seeded.stderr

    proof = json.loads((workspace / "writer-proof.json").read_text(encoding="utf-8"))
    proof["writers"].pop("human_writer")
    proof_path = tmp_path / "incomplete-proof.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")

    result = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(tmp_path / "memory-backup.iab"),
        "--writer-proof",
        str(proof_path),
        "--rehearsal-fixture",
        env=key_env(),
    )

    assert result.returncode != 0
    assert "writer" in result.stderr.lower()


def test_backup_rejects_synthetic_fixture_without_rehearsal_flag(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0

    result = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(tmp_path / "offline" / "memory-backup.iab"),
        "--writer-proof",
        str(workspace / "writer-proof.json"),
        env=key_env(),
    )
    assert result.returncode != 0
    assert "rehearsal flag" in result.stderr.lower()


def test_backup_and_isolated_restore_reconcile_the_seeded_lifecycle(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    seeded = run_tool("seed", "--workspace", str(workspace))
    assert seeded.returncode == 0, seeded.stderr

    archive = tmp_path / "offline" / "memory-backup.iab"
    encryption_env = key_env()
    backed_up = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(archive),
        "--writer-proof",
        str(workspace / "writer-proof.json"),
        "--rehearsal-fixture",
        env=encryption_env,
    )
    assert backed_up.returncode == 0, backed_up.stderr
    backup_receipt = json.loads(backed_up.stdout)
    assert backup_receipt["encrypted"] is True
    assert backup_receipt["retention_until"] == "2027-10-23"
    assert archive.is_file()
    assert b"manifest.json" not in archive.read_bytes()

    inside_restore = run_tool(
        "restore",
        "--archive",
        str(archive),
        "--restore-root",
        str(workspace / "isolated-restore"),
        env=encryption_env,
    )
    assert inside_restore.returncode != 0
    assert "outside" in inside_restore.stderr.lower()
    assert not (workspace / "isolated-restore").exists()

    restore_root = tmp_path / "isolated-restore"
    restored = run_tool(
        "restore",
        "--archive",
        str(archive),
        "--restore-root",
        str(restore_root),
        env=encryption_env,
    )
    assert restored.returncode == 0, restored.stderr
    restore_receipt = json.loads(restored.stdout)
    assert restore_receipt["result"] == "restore_rehearsal_passed"
    assert restore_receipt["source_files_match"] is True
    assert restore_receipt["database_tables_match"] is True
    assert restore_receipt["databases_readable"] is True
    assert restore_receipt["manifest_match"] is True
    assert restore_receipt["retention_until"] == "2027-10-23"
    assert restore_receipt["integrity_checks"] == {"memory.db": "ok", "memory-v2.db": "ok"}
    assert restore_receipt["database_manifests"]["memory-v2.db"]["tables"]

    manifest = json.loads((restore_root / "manifest.json").read_text(encoding="utf-8"))
    assert restore_receipt["source_file_manifest"] == manifest["source_files"]
    inventory = {item["relative_path"]: item for item in manifest["source_files"]}
    assert inventory["memory-v2.db-wal"]["role"] == "sqlite-sidecar"
    assert inventory["memories/MEMORY.md"]["role"] == "markdown-main"
    assert inventory["memories/MEMORY.md"]["present"] is True
    assert inventory["memories/MEMORY.md.bak.20261023"]["role"] == "markdown-backup"
    assert inventory["memories/MEMORY.md.bak.20261023"]["present"] is True
    assert inventory["memories/.mem_seed.tmp"]["role"] == "markdown-temp"
    assert inventory["memories/.mem_seed.tmp"]["present"] is True
    assert inventory["memories/MEMORY.md.lock"]["role"] == "markdown-lock"
    assert inventory["memories/MEMORY.md.lock"]["present"] is True
    tables = manifest["databases"]["memory-v2.db"]["tables"]
    for table in tables.values():
        assert {"status", "old_keys", "leases", "provenance"} <= set(table)
    assert {
        "memory_v2_records",
        "memory_v2_outbox",
        "memory_v2_tombstones",
        "memory_v2_settings",
        "memory_v2_staged",
        "memory_v2_jobs",
        "memory_v2_fts",
    } <= set(tables)
    assert tables["memory_v2_records"]["status"]["status"]["non_null_count"] == 2
    assert tables["memory_v2_records"]["old_keys"]["memory_id"]["non_null_count"] == 2
    assert tables["memory_v2_records"]["provenance"]["source_type"]["non_null_count"] == 2
    assert tables["memory_v2_jobs"]["leases"]["lease_owner"]["non_null_count"] == 1
    assert tables["memory_v2_jobs"]["status"]["stage"]["non_null_count"] == 2
    assert tables["memory_v2_jobs"]["status"]["state"]["value_counts"]
    assert tables["memory_v2_jobs"]["status"]["outcome"]["non_null_count"] == 1
    assert tables["memory_v2_jobs"]["old_keys"]["idempotency_key"]["non_null_count"] == 2
    assert tables["memory_v2_blocked"]["provenance"]["origin"]["non_null_count"] == 1

    for database_name in ("memory.db", "memory-v2.db"):
        restored_database = restore_root / "snapshot" / "databases" / database_name
        with sqlite3.connect(restored_database) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0] > 0
            if database_name == "memory-v2.db":
                assert connection.execute(
                    "SELECT rowid, content FROM memory_v2_fts ORDER BY rowid"
                ).fetchall() == connection.execute(
                    "SELECT rowid, content FROM memory_v2_records ORDER BY rowid"
                ).fetchall()

    assert not archive.is_relative_to(workspace)
    assert not restore_root.is_relative_to(workspace)


def test_backup_refuses_runtime_output_and_missing_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    seeded = run_tool("seed", "--workspace", str(workspace))
    assert seeded.returncode == 0, seeded.stderr

    inside = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(workspace / "memory-backup.iab"),
        "--writer-proof",
        str(workspace / "writer-proof.json"),
        "--rehearsal-fixture",
        env=key_env(),
    )
    assert inside.returncode != 0
    assert "outside" in inside.stderr.lower()
    assert not (workspace / "memory-backup.iab").exists()

    outside = tmp_path / "offline" / "memory-backup.iab"
    missing_key = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(outside),
        "--writer-proof",
        str(workspace / "writer-proof.json"),
        "--rehearsal-fixture",
        env={"MEMORY_BACKUP_KEY": ""},
    )
    assert missing_key.returncode != 0
    assert "environment variable" in missing_key.stderr
    assert not outside.exists()


def test_restore_rejects_tampered_ciphertext(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0
    encryption_env = key_env()
    archive = tmp_path / "offline" / "memory-backup.iab"
    backed_up = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(archive),
        "--writer-proof",
        str(workspace / "writer-proof.json"),
        "--rehearsal-fixture",
        env=encryption_env,
    )
    assert backed_up.returncode == 0, backed_up.stderr

    tampered = tmp_path / "offline" / "tampered.iab"
    payload = bytearray(archive.read_bytes())
    payload[-1] ^= 1
    tampered.write_bytes(payload)
    restored = run_tool(
        "restore",
        "--archive",
        str(tampered),
        "--restore-root",
        str(tmp_path / "tampered-restore"),
        env=encryption_env,
    )
    assert restored.returncode != 0
    assert "authentication" in restored.stderr.lower()
    assert not (tmp_path / "tampered-restore").exists()
    assert not list(tmp_path.glob(".tampered-restore.restore-*"))


def test_writer_proof_cannot_use_a_local_lock_as_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0
    proof = json.loads((workspace / "writer-proof.json").read_text(encoding="utf-8"))
    proof["writers"]["human_writer"]["proof"]["evidence_ref"] = "filesystem locking only"
    proof["writers"]["human_writer"]["verification"]["evidence_ref"] = "filesystem locking only"
    proof_path = tmp_path / "lock-only-proof.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")

    result = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(tmp_path / "offline" / "memory-backup.iab"),
        "--writer-proof",
        str(proof_path),
        "--rehearsal-fixture",
        env=key_env(),
    )
    assert result.returncode != 0
    assert "file lock" in result.stderr.lower()


def test_writer_proof_requires_structured_zero_write_verification(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0
    proof = json.loads((workspace / "writer-proof.json").read_text(encoding="utf-8"))
    proof["writers"]["formation"]["verification"]["active_writers"] = 1
    proof_path = tmp_path / "active-writer-proof.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")

    result = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(tmp_path / "offline" / "memory-backup.iab"),
        "--writer-proof",
        str(proof_path),
        "--rehearsal-fixture",
        env=key_env(),
    )
    assert result.returncode != 0
    assert "active_writers" in result.stderr


def test_writer_proof_requires_fresh_per_writer_verification(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0
    proof = json.loads((workspace / "writer-proof.json").read_text(encoding="utf-8"))
    proof["writers"]["governance"]["verification"]["verified_at"] = "2020-01-01T00:00:00+00:00"
    proof_path = tmp_path / "stale-writer-proof.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")

    result = run_tool(
        "backup",
        "--workspace",
        str(workspace),
        "--output",
        str(tmp_path / "offline" / "memory-backup.iab"),
        "--writer-proof",
        str(proof_path),
        "--rehearsal-fixture",
        env=key_env(),
    )
    assert result.returncode != 0
    assert "one-day" in result.stderr
