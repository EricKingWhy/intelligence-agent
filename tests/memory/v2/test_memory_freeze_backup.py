from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path

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
        env=key_env(),
    )

    assert result.returncode != 0
    assert "writer" in result.stderr.lower()


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
    assert tables["memory_v2_jobs"]["status"]["stage"]["non_null_count"] == 1
    assert tables["memory_v2_blocked"]["provenance"]["origin"]["non_null_count"] == 1

    for database_name in ("memory.db", "memory-v2.db"):
        restored_database = restore_root / "snapshot" / "databases" / database_name
        with sqlite3.connect(restored_database) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0] > 0

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


def test_writer_proof_cannot_use_a_local_lock_as_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert run_tool("seed", "--workspace", str(workspace)).returncode == 0
    proof = json.loads((workspace / "writer-proof.json").read_text(encoding="utf-8"))
    proof["writers"]["human_writer"]["proof"] = "local file lock only"
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
        env=key_env(),
    )
    assert result.returncode != 0
    assert "file lock" in result.stderr.lower()
