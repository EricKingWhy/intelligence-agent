#!/usr/bin/env python3
"""Freeze and rehearse a plaintext, offline Memory workspace backup.

This is deliberately a standalone operator tool.  It does not import the Memory
runtime, initialize a store, purge tombstones, or register an archive as a live
retrieval source.  The only database write it performs is the destination side
of SQLite's Online Backup API.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

RETENTION_UNTIL = "2027-10-23"
ARCHIVE_FORMAT = "plain-tar-v1"
WRITER_CATEGORIES = (
    "memory_tool",
    "formation",
    "approval",
    "governance",
    "external_legacy_process",
    "human_writer",
)
WRITER_PROOF_METHODS = {
    "endpoint-disable",
    "fixture-no-writer",
    "manual-pause",
    "process-stop",
    "worker-quiesce",
}
WRITER_EVIDENCE_TYPES = {
    "control-plane",
    "fixture",
    "manual-confirmation",
    "process-list",
    "scheduler-state",
    "service-state",
}
DATABASE_NAMES = ("memory.db", "memory-v2.db")
DATABASE_SIDECARS = ("-wal", "-shm", "-journal")


class BackupError(RuntimeError):
    """An operator-facing, non-secret validation or backup error."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": repr(value)}
    return {"type": type(value).__name__, "value": str(value)}


def _value_digest(value: Any) -> str:
    return _sha256_bytes(_json_bytes(_canonical_value(value)))


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _column_summary(values: list[Any]) -> dict[str, Any]:
    digests = [_value_digest(value) for value in values if value is not None]
    counts = Counter(digests)
    return {
        "row_count": len(values),
        "non_null_count": len(digests),
        "distinct_value_count": len(counts),
        "value_counts": dict(sorted(counts.items())),
        "values_sha256": _sha256_bytes("\n".join(sorted(digests)).encode("ascii")),
    }


def _column_bucket(
    names: list[str], rows: list[tuple[Any, ...]], predicate,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, name in enumerate(names):
        if predicate(name.lower()):
            result[name] = _column_summary([row[index] for row in rows])
    return result


def _table_manifest(connection: sqlite3.Connection, table_name: str) -> dict[str, Any]:
    quoted = _quote_identifier(table_name)
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
    rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
    row_digests = [
        _sha256_bytes(_json_bytes([_canonical_value(value) for value in row])) for row in rows
    ]
    status_names = {
        "status",
        "state",
        "stage",
        "outcome",
        "operation",
        "protected_fact_extraction_state",
        "disposition",
    }
    key_names = {
        "memory_id",
        "root_id",
        "job_id",
        "idempotency_key",
        "revision",
        "run_id",
        "version",
        "id",
    }
    lease_names = {"lease_owner", "lease_expires_at", "claimed_at"}
    provenance_tokens = (
        "source",
        "evidence",
        "origin",
        "reason",
        "session",
        "event",
        "projection",
        "tenant",
        "user",
        "project",
        "namespace",
    )
    status = _column_bucket(
        columns,
        rows,
        lambda name: name in status_names or name.endswith(("_status", "_state")),
    )
    old_keys = _column_bucket(
        columns,
        rows,
        lambda name: name in key_names
        or name.endswith(("_id", "_key", "_revision", "_version")),
    )
    leases = _column_bucket(columns, rows, lambda name: name in lease_names)
    provenance = _column_bucket(
        columns,
        rows,
        lambda name: any(token in name for token in provenance_tokens),
    )
    return {
        "columns": columns,
        "row_count": len(rows),
        "row_hash": _sha256_bytes("\n".join(sorted(row_digests)).encode("ascii")),
        "status": status,
        "old_keys": old_keys,
        "leases": leases,
        "provenance": provenance,
    }


def _database_manifest_connection(connection: sqlite3.Connection, label: str) -> dict[str, Any]:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise BackupError(f"SQLite integrity_check failed for {label}: {integrity}")
    table_names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    tables = {name: _table_manifest(connection, name) for name in table_names}
    return {"tables": tables}


def _database_manifest(path: Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error as error:
        raise BackupError(f"cannot open SQLite snapshot {path.name}: {error}") from error
    try:
        return _database_manifest_connection(connection, path.name)
    finally:
        connection.close()


def _resolve_existing(path: Path, label: str) -> Path:
    _reject_reparse_components(path.expanduser(), label)
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise BackupError(f"{label} does not exist: {resolved}")
    return resolved


def _ensure_not_inside(path: Path, parent: Path, label: str) -> None:
    if path == parent or path.is_relative_to(parent):
        raise BackupError(f"{label} must be outside the workspace runtime directory")


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise BackupError(f"cannot inspect path for reparse containment: {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_attribute)


def _reject_reparse_components(path: Path, label: str) -> None:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    while True:
        if _is_reparse_point(candidate):
            raise BackupError(f"{label} cannot contain a symlink, junction, or reparse point: {candidate}")
        if candidate.parent == candidate:
            break
        candidate = candidate.parent


def _iter_regular_files(root: Path):
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise BackupError(f"cannot enumerate source directory {directory}: {error}") from error
        for entry in entries:
            path = Path(entry.path)
            if _is_reparse_point(path):
                raise BackupError(f"symlink, junction, or reparse point is not allowed: {path}")
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path
            except OSError as error:
                raise BackupError(f"cannot inspect source path {path}: {error}") from error


def _prepare_output(path: Path, workspace: Path) -> Path:
    _reject_reparse_components(path.expanduser(), "backup output")
    resolved = path.expanduser().resolve()
    _ensure_not_inside(resolved, workspace, "backup output")
    if resolved.exists():
        raise BackupError(f"refusing to overwrite existing backup output: {resolved}")
    if not resolved.parent.is_dir():
        raise BackupError(
            "backup output parent must already exist; operator must verify that it is "
            f"access-controlled: {resolved.parent}"
        )
    return resolved


def _open_new_file_mode_600(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        return os.fdopen(descriptor, "wb")
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _publish_no_clobber(temporary: Path, output: Path) -> None:
    try:
        if os.name == "nt":
            os.rename(temporary, output)
        else:
            os.link(temporary, output)
            with suppress(OSError):
                temporary.unlink()
    except FileExistsError as error:
        raise BackupError(f"refusing to overwrite existing backup output: {output}") from error
    except OSError as error:
        raise BackupError(f"cannot publish backup without overwrite: {error}") from error


def _cleanup_archive_temporary(
    temporary: Path, *, original_error: BaseException | None = None, published_output: Path | None = None
) -> None:
    try:
        temporary.unlink()
    except FileNotFoundError:
        return
    except OSError as cleanup_error:
        detail = f"could not remove temporary archive {temporary}: {cleanup_error}"
        if original_error is not None:
            detail += f"; original failure: {original_error}"
        if published_output is not None:
            detail += f"; backup may already be published at {published_output}"
        raise BackupError(detail) from (original_error or cleanup_error)


def _load_writer_proof(path: Path, *, allow_fixture: bool) -> dict[str, Any]:
    try:
        proof = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError(f"cannot read writer proof: {error}") from error
    return _validate_writer_proof(proof, require_fresh=True, allow_fixture=allow_fixture)


def _validate_writer_proof(
    proof: Any, *, require_fresh: bool, allow_fixture: bool
) -> dict[str, Any]:
    if not isinstance(proof, dict) or proof.get("schema_version") != 1:
        raise BackupError("writer proof must have schema_version=1")
    if proof.get("freeze_effective") is not True:
        raise BackupError("writer proof is not marked freeze_effective=true")
    checked_at = proof.get("checked_at")
    if not isinstance(checked_at, str):
        raise BackupError("writer proof must contain checked_at")
    try:
        checked_moment = datetime.fromisoformat(checked_at)
        if checked_moment.tzinfo is None:
            raise ValueError("timestamp has no timezone")
    except ValueError as error:
        raise BackupError("writer proof checked_at must be an ISO timestamp with timezone") from error
    now = datetime.now(UTC)
    if checked_moment > now + timedelta(minutes=5) or (
        require_fresh and now - checked_moment > timedelta(days=1)
    ):
        raise BackupError("writer proof checked_at is outside the one-day evidence window")
    writers = proof.get("writers")
    if not isinstance(writers, dict):
        raise BackupError("writer proof must contain a writers object")
    missing = sorted(set(WRITER_CATEGORIES) - set(writers))
    unknown = sorted(set(writers) - set(WRITER_CATEGORIES))
    if missing or unknown:
        detail = []
        if missing:
            detail.append(f"missing={','.join(missing)}")
        if unknown:
            detail.append(f"unknown={','.join(unknown)}")
        raise BackupError(f"writer proof categories are incomplete ({'; '.join(detail)})")
    for category in WRITER_CATEGORIES:
        entry = writers[category]
        if not isinstance(entry, dict):
            raise BackupError(f"writer proof entry {category!r} is not an object")
        if entry.get("category") != category:
            raise BackupError(f"writer proof entry {category!r} has the wrong category")
        if entry.get("status") not in {"absent", "stopped"}:
            raise BackupError(f"writer {category!r} is not absent or stopped")
        if entry.get("method") not in WRITER_PROOF_METHODS:
            raise BackupError(
                f"writer {category!r} must declare a supported stop method"
            )
        if entry["method"] == "fixture-no-writer" and not allow_fixture:
            raise BackupError(
                "synthetic fixture writer proof requires the explicit local rehearsal flag"
            )
        if not isinstance(entry.get("control_action"), str) or not entry["control_action"].strip():
            raise BackupError(f"writer {category!r} lacks a non-empty control_action")
        scope = entry.get("scope")
        if not isinstance(scope, dict):
            raise BackupError(f"writer {category!r} lacks structured scope")
        if not isinstance(scope.get("workspace"), str) or not scope["workspace"].strip():
            raise BackupError(f"writer {category!r} lacks scope workspace")
        for field in ("hosts", "clones"):
            values = scope.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or not value.strip() for value in values)
            ):
                raise BackupError(f"writer {category!r} lacks a non-empty scope {field} list")
        if scope.get("cross_clone_checked") is not True:
            raise BackupError(f"writer {category!r} scope was not checked across clones")
        proof_evidence = entry.get("proof")
        if not isinstance(proof_evidence, dict):
            raise BackupError(f"writer {category!r} lacks structured proof evidence")
        if proof_evidence.get("evidence_type") not in WRITER_EVIDENCE_TYPES:
            raise BackupError(f"writer {category!r} has an unsupported proof evidence type")
        if proof_evidence["evidence_type"] == "fixture" and not allow_fixture:
            raise BackupError(
                "synthetic fixture writer evidence requires the explicit local rehearsal flag"
            )
        if proof_evidence.get("observed_state") != entry["status"]:
            raise BackupError(f"writer {category!r} proof state does not match status")
        if not isinstance(proof_evidence.get("evidence_ref"), str) or not proof_evidence["evidence_ref"].strip():
            raise BackupError(f"writer {category!r} lacks a proof evidence reference")
        verification = entry.get("verification")
        if not isinstance(verification, dict):
            raise BackupError(f"writer {category!r} lacks structured verification")
        if verification.get("active_writers") != 0:
            raise BackupError(f"writer {category!r} active_writers must be zero")
        if verification.get("active_writer_processes") != []:
            raise BackupError(f"writer {category!r} active_writer_processes must be empty")
        if verification.get("write_activity") != "none":
            raise BackupError(f"writer {category!r} write_activity must be none")
        if verification.get("mtime_stable") is not True:
            raise BackupError(f"writer {category!r} mtime_stable must be true")
        for field in ("host", "evidence_ref"):
            if not isinstance(verification.get(field), str) or not verification[field].strip():
                raise BackupError(f"writer {category!r} lacks verification {field}")
        if verification["evidence_ref"] != proof_evidence["evidence_ref"]:
            raise BackupError(f"writer {category!r} proof references do not match")
        verified_at = verification.get("verified_at")
        last_write_at = verification.get("last_write_at")
        for field, value in (("verified_at", verified_at), ("last_write_at", last_write_at)):
            if not isinstance(value, str):
                raise BackupError(f"writer {category!r} lacks verification {field}")
            try:
                moment = datetime.fromisoformat(value)
                if moment.tzinfo is None:
                    raise ValueError("timestamp has no timezone")
            except ValueError as error:
                raise BackupError(
                    f"writer {category!r} verification {field} must be an ISO timestamp with timezone"
                ) from error
            if moment > now + timedelta(minutes=5):
                raise BackupError(f"writer {category!r} verification {field} is in the future")
            if field == "verified_at" and require_fresh and now - moment > timedelta(days=1):
                raise BackupError(f"writer {category!r} verification is outside the one-day evidence window")
        evidence = json.dumps(entry, ensure_ascii=False, sort_keys=True).lower()
        if any(
            term in evidence
            for term in (
                "file lock",
                "file-lock",
                "filesystem locking",
                "local lock",
                "local-only",
                "lock based",
                "lock-based",
                "lock-only",
                "locking only",
            )
        ):
            raise BackupError(f"writer {category!r} uses a local file lock as its freeze proof")
    return proof


def _validate_writer_proof_workspace(proof: dict[str, Any], workspace: Path) -> None:
    for category, entry in proof["writers"].items():
        scope_workspace = entry["scope"]["workspace"]
        scope_path = Path(scope_workspace)
        if not scope_path.is_absolute():
            raise BackupError(
                f"writer {category!r} scope workspace must be an absolute path"
            )
        if scope_workspace != str(workspace):
            raise BackupError(
                f"writer {category!r} scope workspace does not match canonical backup workspace path"
            )


def _file_role(relative: str) -> str:
    name = PurePosixPath(relative).name
    if name in {"MEMORY.md", "USER.md"}:
        return "markdown-main"
    if ".bak." in name or name.endswith(".bak"):
        return "markdown-backup"
    if name.startswith(".mem_") and name.endswith(".tmp"):
        return "markdown-temp"
    if name.endswith(".lock"):
        return "markdown-lock"
    return "markdown-other"


def _source_inventory(workspace: Path) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}

    def add(relative: str, role: str) -> None:
        relative_path = PurePosixPath(relative)
        source = workspace.joinpath(*relative_path.parts)
        if _is_reparse_point(source):
            raise BackupError(f"symlink, junction, or reparse point is not allowed: {relative}")
        present = source.is_file()
        item: dict[str, Any] = {"relative_path": str(relative_path), "role": role, "present": present}
        if present:
            item["size"] = source.stat().st_size
            item["sha256"] = _sha256_file(source)
        entries[str(relative_path)] = item

    for database in DATABASE_NAMES:
        add(database, "sqlite-database")
        for suffix in DATABASE_SIDECARS:
            add(database + suffix, "sqlite-sidecar")
    memory_dir = workspace / "memories"
    if _is_reparse_point(memory_dir):
        raise BackupError("memories directory symlink, junction, or reparse point is not allowed")
    add("memories/MEMORY.md", "markdown-main")
    add("memories/USER.md", "markdown-main")
    if memory_dir.exists():
        if not memory_dir.is_dir():
            raise BackupError("memories path is not a directory")
        for path in _iter_regular_files(memory_dir):
            relative = path.relative_to(workspace).as_posix()
            entries[relative] = {
                "relative_path": relative,
                "role": _file_role(relative),
                "present": True,
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
    return [entries[key] for key in sorted(entries)]


def _inventory_signature(inventory: list[dict[str, Any]]) -> bytes:
    return _json_bytes(inventory)


def _online_backup(source: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro&immutable=1"
        source_connection = sqlite3.connect(source_uri, timeout=5, uri=True)
        destination_connection = sqlite3.connect(":memory:")
        try:
            source_connection.backup(destination_connection, pages=128, sleep=0.05)
            destination_connection.commit()
            snapshot = destination_connection.serialize()
            manifest = _database_manifest_connection(destination_connection, source.name)
        finally:
            destination_connection.close()
            source_connection.close()
        return snapshot, {
            "present": True,
            "snapshot_sha256": _sha256_bytes(snapshot),
            **manifest,
        }
    except sqlite3.Error as error:
        raise BackupError(f"SQLite Online Backup API failed for {source.name}: {error}") from error


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_json_bytes(value) + b"\n")


def _tar_add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.mode = 0o600
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def _tar_add_source(archive: tarfile.TarFile, source: Path, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.mode = 0o600
    info.size = source.stat().st_size
    with source.open("rb") as handle:
        archive.addfile(info, handle)


def _make_archive(
    workspace: Path,
    inventory: list[dict[str, Any]],
    snapshots: dict[str, bytes],
    manifest: dict[str, Any],
    proof: dict[str, Any],
    output: Path,
) -> None:
    completed = False
    created = False
    try:
        destination = _open_new_file_mode_600(output)
        created = True
        with destination:
            with tarfile.open(fileobj=destination, mode="w|") as archive:
                _tar_add_bytes(archive, "manifest.json", _json_bytes(manifest) + b"\n")
                _tar_add_bytes(archive, "writer-proof.json", _json_bytes(proof) + b"\n")
                for name in DATABASE_NAMES:
                    if manifest["databases"][name]["present"]:
                        _tar_add_bytes(archive, f"snapshot/databases/{name}", snapshots[name])
                for item in inventory:
                    if item["present"]:
                        relative = PurePosixPath(item["relative_path"])
                        _tar_add_source(
                            archive,
                            workspace.joinpath(*relative.parts),
                            f"source/files/{relative}",
                        )
            destination.flush()
            os.fsync(destination.fileno())
        completed = True
    except BaseException as error:
        if created and not completed:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                raise BackupError(
                    f"could not remove incomplete archive {output}: {cleanup_error}; "
                    f"original failure: {error}"
                ) from error
        raise


def _allowed_archive_members(manifest: dict[str, Any]) -> set[str]:
    allowed = {"manifest.json", "writer-proof.json"}
    for name in DATABASE_NAMES:
        if manifest["databases"][name]["present"]:
            allowed.add(f"snapshot/databases/{name}")
    for item in manifest["source_files"]:
        if item["present"]:
            relative = _safe_relative_path(item["relative_path"], "source inventory")
            allowed.add(f"source/files/{relative}")
    return allowed


def _restore_stream(
    archive_path: Path, destination: Path, restore_root: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest: dict[str, Any] | None = None
    proof: dict[str, Any] | None = None
    seen: set[str] = set()
    with tarfile.open(archive_path, mode="r|") as archive:
        for member in archive:
            if not member.isfile():
                raise BackupError("backup contains an unsupported non-file archive member")
            relative = _safe_relative_path(member.name, "archive member")
            name = str(relative)
            if name in seen:
                raise BackupError("backup contains duplicate archive members")
            if name == "manifest.json":
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupError("backup manifest could not be read")
                try:
                    manifest_value = json.loads(extracted.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise BackupError("backup manifest is not valid JSON") from error
                _validate_manifest(manifest_value)
                _ensure_not_inside(
                    restore_root,
                    Path(manifest_value["workspace_root"]).expanduser().resolve(),
                    "restore root",
                )
                destination.mkdir(parents=True, exist_ok=True)
                manifest = manifest_value
                seen.add(name)
                continue
            if manifest is None:
                raise BackupError("backup manifest must be the first archive member")
            if name == "writer-proof.json":
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupError("backup writer proof could not be read")
                try:
                    proof_value = json.loads(extracted.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise BackupError("backup writer proof is not valid JSON") from error
                proof = _validate_writer_proof(
                    proof_value,
                    require_fresh=False,
                    allow_fixture=manifest.get("writer_proof_mode") == "fixture",
                )
                seen.add(name)
                continue
            allowed = _allowed_archive_members(manifest)
            if name not in allowed:
                raise BackupError("backup contains an unlisted archive member")
            target = destination.joinpath(*relative.parts).resolve()
            if target != destination and not target.is_relative_to(destination):
                raise BackupError("backup contains an archive path outside the restore root")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise BackupError("backup member could not be read")
            with _open_new_file_mode_600(target) as handle:
                shutil.copyfileobj(extracted, handle)
            seen.add(name)
    if manifest is None or proof is None:
        raise BackupError("backup is missing manifest or writer proof")
    if seen != _allowed_archive_members(manifest) | {"manifest.json", "writer-proof.json"}:
        raise BackupError("backup archive members do not match its manifest")
    _write_json(destination / "manifest.json", manifest)
    _write_json(destination / "writer-proof.json", proof)
    return manifest, proof


def _safe_relative_path(value: str, label: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise BackupError(f"{label} path is not text")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or str(relative) in {"", "."}
    ):
        raise BackupError(f"{label} path is unsafe")
    return relative


def _validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise BackupError("restored manifest schema is unsupported")
    if manifest.get("archive_format") != ARCHIVE_FORMAT or manifest.get("encrypted") is not False:
        raise BackupError("restored manifest does not identify the supported plaintext archive format")
    if manifest.get("retention_until") != RETENTION_UNTIL:
        raise BackupError("restored manifest has an unexpected retention date")
    if manifest.get("snapshot_method") != "sqlite-online-backup-api":
        raise BackupError("restored manifest does not identify SQLite Online Backup")
    if not isinstance(manifest.get("workspace_root"), str) or not manifest["workspace_root"].strip():
        raise BackupError("restored manifest has no workspace identity")
    if manifest.get("runtime_loaded") is not False or manifest.get("retrieval_registered") is not False:
        raise BackupError("restored manifest claims runtime or retrieval registration")
    if manifest.get("writer_proof_mode") not in {"fixture", "owner"}:
        raise BackupError("restored manifest has an invalid writer proof mode")
    if manifest.get("writer_categories") != list(WRITER_CATEGORIES):
        raise BackupError("restored manifest has an incomplete writer category list")
    inventory = manifest.get("source_files")
    if not isinstance(inventory, list):
        raise BackupError("restored manifest source_files is not a list")
    seen: set[str] = set()
    for item in inventory:
        if not isinstance(item, dict):
            raise BackupError("restored manifest contains an invalid source file entry")
        relative = _safe_relative_path(item.get("relative_path"), "source inventory")
        if str(relative) in seen:
            raise BackupError("restored manifest contains duplicate source paths")
        seen.add(str(relative))
        if not isinstance(item.get("present"), bool):
            raise BackupError("restored manifest source presence is invalid")
        if item["present"] and (
            not isinstance(item.get("size"), int)
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise BackupError("restored manifest source hash or size is invalid")
    expected_paths = {
        database + suffix
        for database in DATABASE_NAMES
        for suffix in ("", *DATABASE_SIDECARS)
    } | {"memories/MEMORY.md", "memories/USER.md"}
    if not expected_paths <= seen:
        raise BackupError("restored manifest omits a required database or Markdown path")
    databases = manifest.get("databases")
    if not isinstance(databases, dict) or set(databases) != set(DATABASE_NAMES):
        raise BackupError("restored manifest database inventory is incomplete")
    for name in DATABASE_NAMES:
        item = databases[name]
        if not isinstance(item, dict) or not isinstance(item.get("present"), bool):
            raise BackupError(f"restored manifest database entry {name!r} is invalid")
        if not isinstance(item.get("tables"), dict):
            raise BackupError(f"restored manifest table inventory {name!r} is invalid")
        if item["present"] and (
            not isinstance(item.get("snapshot_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["snapshot_sha256"])
        ):
            raise BackupError(f"restored manifest snapshot hash {name!r} is invalid")
    if not any(item["present"] for item in databases.values()):
        raise BackupError("restored manifest contains no Memory SQLite database")


def _restore_source_check(root: Path, inventory: list[dict[str, Any]]) -> bool:
    for item in inventory:
        relative = _safe_relative_path(item["relative_path"], "source inventory")
        target = root / "source" / "files" / Path(*relative.parts)
        if item["present"]:
            if not target.is_file():
                return False
            if target.stat().st_size != item["size"] or _sha256_file(target) != item["sha256"]:
                return False
        elif target.exists():
            return False
    return True


def _restore_database_check(
    root: Path, databases: dict[str, Any]
) -> tuple[bool, dict[str, int], dict[str, Any], dict[str, str]]:
    table_counts: dict[str, int] = {}
    actual_manifests: dict[str, Any] = {}
    integrity_checks: dict[str, str] = {}
    for name, expected in databases.items():
        path = root / "snapshot" / "databases" / name
        if not expected["present"]:
            if path.exists():
                return False, table_counts, actual_manifests, integrity_checks
            continue
        if not path.is_file() or _sha256_file(path) != expected["snapshot_sha256"]:
            return False, table_counts, actual_manifests, integrity_checks
        actual = _database_manifest(path)
        actual_manifests[name] = actual
        integrity_checks[name] = "ok"
        if actual["tables"] != expected["tables"]:
            return False, table_counts, actual_manifests, integrity_checks
        table_counts[name] = len(actual["tables"])
    return True, table_counts, actual_manifests, integrity_checks


def _seed_database(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        if version == 1:
            connection.executescript(
                """
                CREATE TABLE memory_records (
                    memory_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    scope TEXT NOT NULL, namespace TEXT NOT NULL, content TEXT NOT NULL,
                    metadata TEXT NOT NULL, created_at TEXT NOT NULL, indexed BOOLEAN NOT NULL
                );
                CREATE TABLE memory_outbox (
                    memory_id TEXT PRIMARY KEY, revision TEXT NOT NULL,
                    operation TEXT NOT NULL, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    scope TEXT NOT NULL, namespace TEXT NOT NULL
                );
                INSERT INTO memory_records VALUES
                    ('v1-old-1', 'tenant-a', 'user-a', 'global', '[]',
                     'legacy note', '{"source":"seed"}', '2026-10-23T00:00:00+00:00', 1);
                INSERT INTO memory_outbox VALUES
                    ('v1-old-1', 'rev-v1-1', 'upsert', 'tenant-a', 'user-a', 'global', '[]');
                """
            )
        else:
            connection.executescript(
                """
                CREATE TABLE memory_v2_records (
                    memory_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                    root_id TEXT NOT NULL, version INTEGER NOT NULL, kind TEXT NOT NULL,
                    tier TEXT NOT NULL, scope TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, project_id TEXT, content TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL, importance REAL NOT NULL,
                    strength REAL NOT NULL, source_type TEXT NOT NULL,
                    source_session_id TEXT, source_event_ids TEXT NOT NULL, evidence TEXT NOT NULL,
                    valid_at TEXT, invalidated_at TEXT, superseded_by TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    projection_source TEXT NOT NULL, projection_target TEXT
                );
                CREATE TABLE memory_v2_outbox (
                    memory_id TEXT PRIMARY KEY, revision TEXT NOT NULL, operation TEXT NOT NULL,
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, scope TEXT NOT NULL,
                    project_id TEXT
                );
                CREATE TABLE memory_v2_tombstones (
                    memory_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, scope TEXT NOT NULL, project_id TEXT, version INTEGER,
                    deleted_at TEXT NOT NULL, expires_at TEXT NOT NULL, deletion_reason TEXT NOT NULL,
                    content_hashes TEXT NOT NULL, source_hashes TEXT NOT NULL
                );
                CREATE TABLE memory_v2_settings (
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, extraction_enabled INTEGER NOT NULL,
                    recall_enabled INTEGER NOT NULL, fallback_enabled INTEGER NOT NULL,
                    write_approval INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id)
                );
                CREATE TABLE memory_v2_staged (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL, payload TEXT NOT NULL,
                    pinned_entry TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, claimed_at TEXT
                );
                CREATE TABLE memory_v2_projection_pending (
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, target TEXT NOT NULL,
                    content_digest TEXT NOT NULL, content TEXT NOT NULL, session_id TEXT,
                    event_id TEXT, attempts INTEGER NOT NULL, last_error TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, target, content_digest)
                );
                CREATE TABLE memory_v2_blocked (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, identity TEXT NOT NULL,
                    project_id TEXT, origin TEXT NOT NULL, reason TEXT NOT NULL,
                    payload TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL,
                    disposition_note TEXT, created_at TEXT NOT NULL, disposed_at TEXT
                );
                CREATE TABLE memory_v2_jobs (
                    job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, project_id TEXT,
                    session_id TEXT NOT NULL, run_id TEXT, protected_fact_token_budget INTEGER,
                    protected_fact_extraction_state TEXT NOT NULL, protected_fact_extraction_candidates TEXT,
                    stage TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT, reason TEXT,
                    lease_owner TEXT, lease_expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE memory_v2_fts USING fts5(content);
                INSERT INTO memory_v2_records VALUES
                    ('v2-active-1', 1, 'root-1', 1, 'fact', 'profile', 'global',
                     'tenant-a', 'user-a', NULL, 'likes tea', '{"confidence":0.9}', 'active',
                     0.8, 0.9, 'conversation', 'session-1', '["event-1"]',
                     '[{"kind":"message","ref":"event-1"}]', '2026-10-23T00:00:00+00:00',
                     NULL, NULL, '2026-10-23T00:00:00+00:00', '2026-10-23T00:00:00+00:00',
                     'service', 'user');
                INSERT INTO memory_v2_records VALUES
                    ('v2-old-1', 1, 'root-old', 1, 'fact', 'collection', 'global',
                     'tenant-a', 'user-a', 'project-a', 'old fact', '{}', 'superseded',
                     0.5, 0.5, 'tool', 'session-0', '["event-0"]',
                     '[{"kind":"tool","ref":"event-0"}]', '2026-10-20T00:00:00+00:00',
                     NULL, 'v2-active-1', '2026-10-20T00:00:00+00:00', '2026-10-23T00:00:00+00:00',
                     'tool', 'memory');
                INSERT INTO memory_v2_outbox VALUES
                    ('v2-active-1', 'rev-v2-1', 'upsert', 'tenant-a', 'user-a', 'global', NULL);
                INSERT INTO memory_v2_tombstones VALUES
                    ('v2-deleted-1', 'root-deleted', 'tenant-a', 'user-a', 'global', NULL, 2,
                     '2026-10-22T00:00:00+00:00', '2027-10-23T00:00:00+00:00',
                     'user-request', '["hash-content"]', '["hash-source"]');
                INSERT INTO memory_v2_settings VALUES
                    ('tenant-a', 'user-a', 1, 1, 1, 0, '2026-10-23T00:00:00+00:00');
                INSERT INTO memory_v2_staged VALUES
                    ('stage-1', '{"tenant_id":"tenant-a","user_id":"user-a"}',
                     '{"draft":"pending"}', 'likes tea', 'pending',
                     '2026-10-23T00:00:00+00:00', NULL);
                INSERT INTO memory_v2_projection_pending VALUES
                    ('tenant-a', 'user-a', 'memory', 'digest-1', 'likes tea', 'session-1',
                     'event-1', 1, NULL, '2026-10-23T00:00:00+00:00');
                INSERT INTO memory_v2_blocked VALUES
                    ('blocked-1', 'job-1', '{"tenant_id":"tenant-a","user_id":"user-a"}', NULL,
                     'formation', 'approval-required', '{"memory_id":"blocked-memory"}',
                     'content-hash-1', 'blocked', NULL, '2026-10-23T00:00:00+00:00', NULL);
                INSERT INTO memory_v2_jobs VALUES
                    ('job-1', 'idempotency-1', 'tenant-a', 'user-a', NULL, 'session-1', 'run-1',
                     100, 'started', '[{"candidate":"blocked-memory"}]', 'forming', '{"count":1}',
                     NULL, NULL, 'worker-a', '2026-10-23T00:05:00+00:00',
                     '2026-10-23T00:00:00+00:00', '2026-10-23T00:00:01+00:00');
                INSERT INTO memory_v2_jobs VALUES
                    ('job-2', 'idempotency-2', 'tenant-a', 'user-a', NULL, 'session-2', 'run-2',
                     100, 'completed', NULL, 'approved', 'completed', 'committed', NULL,
                     NULL, NULL, '2026-10-23T00:00:00+00:00',
                     '2026-10-23T00:10:00+00:00');
                INSERT INTO memory_v2_fts(rowid, content) VALUES (1, 'likes tea');
                INSERT INTO memory_v2_fts(rowid, content) VALUES (2, 'old fact');
                """
            )
        connection.commit()
    finally:
        connection.close()


def _seed_workspace(workspace: Path) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    if workspace.exists() and any(workspace.iterdir()):
        raise BackupError(f"seed workspace must be new or empty: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    _seed_database(workspace / "memory.db", 1)
    _seed_database(workspace / "memory-v2.db", 2)
    memories = workspace / "memories"
    memories.mkdir()
    (memories / "MEMORY.md").write_text("- likes tea\n", encoding="utf-8")
    (memories / "USER.md").write_text("- prefers concise answers\n", encoding="utf-8")
    (memories / "MEMORY.md.bak.20261023").write_text("- old note\n", encoding="utf-8")
    (memories / ".mem_seed.tmp").write_text("temporary write artifact\n", encoding="utf-8")
    (memories / "MEMORY.md.lock").write_text("seed lock evidence\n", encoding="utf-8")
    checked_at = datetime.now(UTC).isoformat()
    proof = {
        "schema_version": 1,
        "freeze_effective": True,
        "checked_at": checked_at,
        "writers": {
            category: {
                "category": category,
                "status": "stopped",
                "method": "fixture-no-writer",
                "control_action": f"seed fixture starts no {category} writer",
                "scope": {
                    "workspace": str(workspace),
                    "hosts": ["seed-fixture"],
                    "clones": ["seed-fixture"],
                    "cross_clone_checked": True,
                },
                "proof": {
                    "evidence_type": "fixture",
                    "observed_state": "stopped",
                    "evidence_ref": f"seed-fixture://writer/{category}",
                },
                "verification": {
                    "host": "seed-fixture",
                    "active_writers": 0,
                    "active_writer_processes": [],
                    "write_activity": "none",
                    "mtime_stable": True,
                    "last_write_at": checked_at,
                    "verified_at": checked_at,
                    "evidence_ref": f"seed-fixture://writer/{category}",
                },
            }
            for category in WRITER_CATEGORIES
        },
    }
    proof_path = workspace / "writer-proof.json"
    _write_json(proof_path, proof)
    return {"workspace": str(workspace), "writer_proof": str(proof_path)}


def _backup(
    workspace_arg: Path,
    output_arg: Path,
    writer_proof_arg: Path,
    allow_fixture: bool,
) -> dict[str, Any]:
    workspace = _resolve_existing(workspace_arg, "workspace")
    if not workspace.is_dir():
        raise BackupError(f"workspace is not a directory: {workspace}")
    output = _prepare_output(output_arg, workspace)
    writer_proof = _resolve_existing(writer_proof_arg, "writer proof")
    proof = _load_writer_proof(writer_proof, allow_fixture=allow_fixture)
    _validate_writer_proof_workspace(proof, workspace)
    proof_hash = _sha256_file(writer_proof)
    before = _source_inventory(workspace)
    snapshots: dict[str, bytes] = {}
    databases: dict[str, Any] = {}
    for name in DATABASE_NAMES:
        source = workspace / name
        if not source.is_file():
            databases[name] = {"present": False, "tables": {}}
            continue
        snapshot, database = _online_backup(source)
        snapshots[name] = snapshot
        databases[name] = database
    if not any(database["present"] for database in databases.values()):
        raise BackupError("workspace contains no Memory SQLite database to preserve")
    manifest = {
        "schema_version": 2,
        "archive_format": ARCHIVE_FORMAT,
        "encrypted": False,
        "workspace_root": str(workspace),
        "retention_until": RETENTION_UNTIL,
        "snapshot_method": "sqlite-online-backup-api",
        "runtime_loaded": False,
        "retrieval_registered": False,
        "writer_proof_mode": "fixture" if allow_fixture else "owner",
        "writer_categories": list(WRITER_CATEGORIES),
        "source_files": before,
        "databases": databases,
    }
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp")
    temporary_created = False
    try:
        _make_archive(workspace, before, snapshots, manifest, proof, temporary)
        temporary_created = True
        after = _source_inventory(workspace)
        if _inventory_signature(before) != _inventory_signature(after):
            raise BackupError("workspace changed during freeze; writer proof is not sufficient")
        if _sha256_file(writer_proof) != proof_hash:
            raise BackupError("writer proof changed during freeze; restart with fresh evidence")
        _publish_no_clobber(temporary, output)
    except BaseException as error:
        if temporary_created:
            _cleanup_archive_temporary(temporary, original_error=error)
        raise
    else:
        if temporary_created:
            _cleanup_archive_temporary(temporary, published_output=output)
    return {
        "result": "backup_created",
        "archive": str(output),
        "archive_sha256": _sha256_file(output),
        "archive_size": output.stat().st_size,
        "encrypted": False,
        "archive_format": ARCHIVE_FORMAT,
        "snapshot_method": "sqlite-online-backup-api",
        "runtime_loaded": False,
        "retrieval_registered": False,
        "writer_proof_mode": "fixture" if allow_fixture else "owner",
        "retention_until": RETENTION_UNTIL,
        "writer_categories": list(WRITER_CATEGORIES),
    }


def _restore(archive_arg: Path, restore_root_arg: Path) -> dict[str, Any]:
    archive = _resolve_existing(archive_arg, "backup archive")
    if not archive.is_file():
        raise BackupError(f"backup archive is not a file: {archive}")
    _reject_reparse_components(restore_root_arg.expanduser(), "restore root")
    restore_root = restore_root_arg.expanduser().resolve()
    if restore_root.exists():
        if not restore_root.is_dir():
            raise BackupError(f"restore root must be a directory: {restore_root}")
        if any(restore_root.iterdir()):
            raise BackupError(f"restore root must be new or empty: {restore_root}")
    if not restore_root.parent.is_dir():
        raise BackupError(
            "restore parent must already exist; operator must verify that it is "
            f"access-controlled: {restore_root.parent}"
        )
    try:
        staging_root = Path(
            tempfile.mkdtemp(prefix=f".{restore_root.name}.restore-", dir=restore_root.parent)
        )
    except OSError as error:
        raise BackupError(f"could not create restore staging directory: {error}") from error
    try:
        try:
            manifest, proof = _restore_stream(archive, staging_root, restore_root)
        except (tarfile.TarError, EOFError) as error:
            raise BackupError(f"backup archive is not a valid tar: {error}") from error
        source_match = _restore_source_check(staging_root, manifest["source_files"])
        databases_match, table_counts, database_manifests, integrity_checks = (
            _restore_database_check(staging_root, manifest["databases"])
        )
        if not source_match or not databases_match:
            raise BackupError("restored source files or database table manifests do not match")
        receipt = {
            "result": "restore_rehearsal_passed",
            "encrypted": False,
            "archive_sha256": _sha256_file(archive),
            "restore_root": str(restore_root),
            "source_files_match": source_match,
            "source_file_manifest": manifest["source_files"],
            "database_tables_match": databases_match,
            "database_table_counts": table_counts,
            "database_manifests": database_manifests,
            "integrity_checks": integrity_checks,
            "databases_readable": True,
            "manifest_match": True,
            "retention_until": manifest["retention_until"],
            "writer_categories": manifest["writer_categories"],
            "writer_proof_mode": manifest["writer_proof_mode"],
            "runtime_loaded": False,
            "retrieval_registered": False,
            "proof_schema_version": proof["schema_version"],
        }
        _write_json(staging_root / "restore-receipt.json", receipt)
        if restore_root.exists():
            try:
                restore_root.rmdir()
            except OSError as error:
                raise BackupError(f"restore root changed during restore: {restore_root}") from error
        try:
            os.rename(staging_root, restore_root)
        except FileExistsError as error:
            raise BackupError(f"restore root appeared during restore: {restore_root}") from error
        except OSError as error:
            raise BackupError(f"could not publish verified restore: {error}") from error
        return receipt
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed = subparsers.add_parser("seed", help="create a representative local rehearsal workspace")
    seed.add_argument("--workspace", type=Path, required=True)

    backup = subparsers.add_parser("backup", help="freeze a workspace backup")
    backup.add_argument("--workspace", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--writer-proof", type=Path, required=True)
    backup.add_argument(
        "--rehearsal-fixture",
        action="store_true",
        help="allow synthetic seed writer evidence; never use for production",
    )

    restore = subparsers.add_parser("restore", help="restore and verify an isolated backup")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--restore-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "seed":
            result = _seed_workspace(args.workspace)
        elif args.command == "backup":
            result = _backup(
                args.workspace,
                args.output,
                args.writer_proof,
                args.rehearsal_fixture,
            )
        else:
            result = _restore(args.archive, args.restore_root)
    except (BackupError, OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
