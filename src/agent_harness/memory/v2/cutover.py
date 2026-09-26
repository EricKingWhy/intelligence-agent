"""One-time, content-free clean-slate reset for the Memory V2 cutover.

Dry-run is read-only. Apply is bound to its dry-run plan, fenced by the workspace
instance lock, and only touches the two memory SQLite files and configured Memory
collection. A persistent marker keeps normal app startup blocked after an interrupted
reset until the operator successfully resumes it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_harness.capability.factories import build_memory_vector_client
from agent_harness.config import Settings
from agent_harness.instance_lock import (
    ALLOW_SHARED_ROOT_ENV,
    CUTOVER_FENCE_FILENAME,
    InstanceLock,
    InstanceLockError,
)
from agent_harness.memory.v2.assembly import MEMORY_V2_DATABASE_NAME

_V1_DATABASE_NAME = "memory.db"
_DATABASE_TABLES = {
    _V1_DATABASE_NAME: frozenset({"memory_records", "memory_outbox"}),
    MEMORY_V2_DATABASE_NAME: frozenset({
        "memory_v2_records", "memory_v2_outbox", "memory_v2_tombstones",
        "memory_v2_settings", "memory_v2_jobs",
    }),
}
_COLLECTION_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class CutoverRefused(RuntimeError):
    """A fail-closed precondition rejected the reset."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _path_fingerprint(path: Path) -> str:
    return _sha256(os.fsencode(os.path.normcase(str(path.resolve(strict=False)))))


def _workspace_root(settings: Settings) -> Path:
    raw = str(settings.workspace_dir).strip()
    if not raw:
        raise CutoverRefused("workspace_root_unconfigured")
    root = Path(raw).expanduser().resolve(strict=False)
    if root == Path(root.anchor) or not root.anchor:
        raise CutoverRefused("workspace_root_too_broad")
    return root


def _sqlite_target(root: Path, name: str) -> Path:
    path = root / name
    if path.is_symlink() or path.resolve(strict=False).parent != root:
        raise CutoverRefused("sqlite_target_not_direct_workspace_child")
    if path.exists():
        try:
            info = path.stat()
            if not path.is_file() or info.st_nlink > 1:
                raise CutoverRefused("sqlite_target_is_not_an_unlinked_regular_file")
        except OSError as error:
            raise CutoverRefused("sqlite_target_stat_failed") from error
    _validate_sqlite_sidecars(path)
    return path


def _validate_sqlite_sidecars(path: Path) -> dict[str, dict[str, int | bool]]:
    sidecars: dict[str, dict[str, int | bool]] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.is_symlink() or sidecar.resolve(strict=False).parent != path.parent:
            raise CutoverRefused("sqlite_sidecar_not_direct_workspace_child")
        try:
            exists = sidecar.exists()
            info = sidecar.stat() if exists else None
        except OSError as error:
            raise CutoverRefused("sqlite_sidecar_stat_failed") from error
        if info is not None and (not sidecar.is_file() or info.st_nlink > 1):
            raise CutoverRefused("sqlite_sidecar_is_not_an_unlinked_regular_file")
        sidecars[suffix] = {
            "exists": exists,
            "size_bytes": 0 if info is None else info.st_size,
        }
    if not path.exists() and any(item["exists"] for item in sidecars.values()):
        raise CutoverRefused("sqlite_sidecar_without_database")
    return sidecars


def _validate_database_schema(connection: sqlite3.Connection, name: str) -> set[str]:
    rows = connection.execute(
        "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables = {object_name for object_type, object_name in rows if object_type == "table"}
    unknown_tables = tables - _DATABASE_TABLES[name]
    if unknown_tables or any(object_type in {"trigger", "view"} for object_type, _ in rows):
        raise CutoverRefused("sqlite_schema_contains_unapproved_objects")
    return tables


def _read_only_sqlite_inventory(path: Path) -> dict[str, Any]:
    sidecars = _validate_sqlite_sidecars(path)
    if any(sidecars[suffix]["size_bytes"] for suffix in ("-wal", "-journal")):
        raise CutoverRefused("sqlite_has_pending_journal; stop writers and retry")
    if not path.exists():
        return {
            "file": path.name,
            "identity_sha256": _path_fingerprint(path),
            "exists": False,
            "tables": [],
            "row_counts": {},
            "sidecars": sidecars,
        }
    uri = f"{path.as_uri()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            tables = _validate_database_schema(connection, path.name)
            counts = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in sorted(tables)
            }
    except CutoverRefused:
        raise
    except sqlite3.Error as error:
        raise CutoverRefused("sqlite_read_only_inventory_failed") from error
    return {
        "file": path.name,
        "identity_sha256": _path_fingerprint(path),
        "exists": True,
        "tables": sorted(tables),
        "row_counts": counts,
        "sidecars": sidecars,
    }


def _tree_fingerprint(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0

    def visit(directory: Path, prefix: str = "") -> None:
        nonlocal file_count, total_bytes
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except FileNotFoundError:
            return
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            digest.update(relative.encode("utf-8", errors="surrogatepass"))
            try:
                if entry.is_symlink():
                    digest.update(b"symlink\0")
                    digest.update(os.fsencode(os.readlink(entry.path)))
                    file_count += 1
                elif entry.is_dir(follow_symlinks=False):
                    digest.update(b"directory\0")
                    visit(Path(entry.path), relative)
                elif entry.is_file(follow_symlinks=False):
                    before = entry.stat(follow_symlinks=False)
                    file_hash = hashlib.sha256()
                    with open(entry.path, "rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            file_hash.update(chunk)
                    after = os.stat(entry.path, follow_symlinks=False)
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise CutoverRefused("preserved_file_changed_during_inventory")
                    digest.update(b"file\0")
                    digest.update(file_hash.digest())
                    digest.update(str(after.st_size).encode())
                    file_count += 1
                    total_bytes += after.st_size
            except OSError as error:
                raise CutoverRefused("preserved_domain_inventory_failed") from error

    exists = root.exists()
    if exists:
        if root.is_symlink():
            digest.update(b"symlink-root\0")
            digest.update(os.fsencode(os.readlink(root)))
            file_count = 1
        elif root.is_dir():
            visit(root)
        elif root.is_file():
            stat = root.stat()
            file_hash = hashlib.sha256(root.read_bytes()).digest()
            digest.update(file_hash)
            total_bytes = stat.st_size
            file_count = 1
    return {
        "exists": exists,
        "root_identity_sha256": _path_fingerprint(root),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "sha256": digest.hexdigest(),
    }


def _file_fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    files = [path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")]
    present = [item for item in files if item.exists()]
    total_bytes = 0
    for item in present:
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                total_bytes += len(chunk)
    return {
        "exists": path.exists(),
        "file_count": len(present),
        "total_bytes": total_bytes,
        "sha256": digest.hexdigest(),
    }


def _preserved_local_domains(settings: Settings, root: Path) -> dict[str, Any]:
    artifact_root = Path(settings.artifact_dir).expanduser().resolve(strict=False)
    if artifact_root == root or artifact_root in root.parents or root in artifact_root.parents:
        raise CutoverRefused("artifact_and_memory_roots_overlap")
    repo_root = Path(__file__).resolve().parents[4]
    return {
        "session_events_and_chats": _tree_fingerprint(root / "sessions"),
        "workspace_metadata_and_checkpoints": _file_fingerprint(root / "harness.db"),
        "workspace_files": _tree_fingerprint(root / "workspaces"),
        "local_artifacts": _tree_fingerprint(artifact_root),
        "evaluation_data": _tree_fingerprint(repo_root / "evaluation"),
        "external_stores": {
            "artifact_object_store_configured": bool(
                settings.artifact_store_endpoint or settings.artifact_store_bucket
            ),
            "minio_configured": bool(settings.minio_endpoint or settings.minio_bucket),
            "accessed": False,
            "mutations": 0,
        },
        "credentials_and_langfuse": {
            "milvus_authentication_used": True,
            "embedding_key_configuration_checked": True,
            "credential_values_emitted": False,
            "credential_sources_modified": False,
            "langfuse_accessed": False,
            "mutations": 0,
        },
    }


async def _milvus_collection_inventory(vectors: Any, settings: Settings, name: str) -> dict[str, Any]:
    collections = await vectors.connect()
    present = name in collections
    result: dict[str, Any] = {
        "identity_sha256": _sha256(name.encode("utf-8")),
        "exists": present,
        "row_count": 0,
        "schema_sha256": None,
    }
    if not present:
        return result
    description = await vectors._call("describe_collection", collection_name=name)
    if name == settings.milvus_collection:
        fields = {item.get("name"): item for item in description.get("fields", [])}
        expected_fields = {
            "id", "memory_id", "tenant_id", "user_id", "scope", "session_id",
            "content", "metadata", "vector",
        }
        vector_params = fields.get("vector", {}).get("params", {})
        if (
            set(fields) != expected_fields
            or bool(description.get("auto_id", False))
            or not fields.get("id", {}).get("is_primary", False)
            or not fields.get("tenant_id", {}).get("is_partition_key", False)
            or int(vector_params.get("dim", 0)) <= 0
        ):
            raise CutoverRefused("configured_collection_schema_not_memory")
    rows = await vectors._call(
        "query", collection_name=name, filter="", output_fields=["count(*)"],
        consistency_level="Strong",
    )
    result["row_count"] = int(rows[0].get("count(*)", 0)) if rows else 0
    schema = {
        "fields": [
            {
                "name": item.get("name"),
                "type": str(item.get("type")),
                "params": item.get("params", {}),
                "is_partition_key": bool(item.get("is_partition_key", False)),
            }
            for item in description.get("fields", [])
        ],
        "auto_id": bool(description.get("auto_id", False)),
    }
    result["schema_sha256"] = _sha256(json.dumps(
        schema, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8"))
    return result


def _validate_memory_target(settings: Settings) -> None:
    name = settings.milvus_collection
    if not _COLLECTION_PATTERN.fullmatch(name or ""):
        raise CutoverRefused("memory_collection_target_unresolved_or_broad")
    if settings.knowledge_collection and name.casefold() == settings.knowledge_collection.casefold():
        raise CutoverRefused("memory_collection_conflicts_with_knowledge")
    if not settings.milvus_uri or not settings.milvus_token.get_secret_value():
        raise CutoverRefused("milvus_connection_unconfigured")
    if not (
        settings.embedding_model
        and settings.embedding_base_url
        and settings.embedding_api_key.get_secret_value()
    ):
        raise CutoverRefused("embedding_configuration_unavailable")


async def build_plan(settings: Settings) -> dict[str, Any]:
    """Build a content-free read-only plan. It never creates files or collections."""
    _validate_memory_target(settings)
    root = _workspace_root(settings)
    databases = [
        _read_only_sqlite_inventory(_sqlite_target(root, name))
        for name in _DATABASE_TABLES
    ]
    vectors = build_memory_vector_client(settings)
    try:
        memory_collection = await _milvus_collection_inventory(
            vectors, settings, settings.milvus_collection,
        )
        knowledge_collection = None
        if settings.knowledge_collection:
            knowledge_collection = await _milvus_collection_inventory(
                vectors, settings, settings.knowledge_collection,
            )
    finally:
        await vectors.close()
    plan = {
        "workspace_identity_sha256": _path_fingerprint(root),
        "sqlite_targets": databases,
        "milvus_memory_collection": memory_collection,
        "knowledge_collection": knowledge_collection,
        "preserved_domains_before": _preserved_local_domains(settings, root),
        "excluded_domains": [
            (
                "session events, chats, sessions, checkpoints, artifacts, workspaces, Knowledge, "
                "evaluation datasets, credentials, Langfuse"
            ),
        ],
        "backup_created": False,
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), default=str).encode()
    plan["plan_sha256"] = _sha256(canonical)
    return plan


def _clear_sqlite_database(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"file": path.name, "exists": False, "before": {}, "after": {}}
    tables = _DATABASE_TABLES[path.name]
    uri = f"{path.as_uri()}?mode=rw"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0)
    except sqlite3.Error as error:
        raise CutoverRefused("sqlite_open_existing_database_failed") from error
    try:
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        actual = _validate_database_schema(connection, path.name)
        before = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(actual)
        }
        for table in sorted(tables & actual):
            connection.execute(f'DELETE FROM "{table}"')
        connection.commit()
        connection.execute("VACUUM")
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
        if journal_mode == "wal":
            busy, log_pages, checkpointed = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if busy or log_pages != checkpointed:
                raise CutoverRefused("sqlite_wal_checkpoint_incomplete")
        connection.commit()
        after = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(actual)
        }
        if any(after.values()):
            raise CutoverRefused("sqlite_memory_rows_remain")
        freelist_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        if freelist_pages:
            raise CutoverRefused("sqlite_freelist_not_empty_after_vacuum")
    except (sqlite3.Error, CutoverRefused) as error:
        connection.rollback()
        if isinstance(error, CutoverRefused):
            raise
        raise CutoverRefused("sqlite_clean_slate_failed") from error
    finally:
        connection.close()
    sidecars = _validate_sqlite_sidecars(path)
    if any(sidecars[suffix]["size_bytes"] for suffix in ("-wal", "-journal")):
        raise CutoverRefused("sqlite_journal_remains_after_reset")
    return {
        "file": path.name,
        "identity_sha256": _path_fingerprint(path),
        "exists": True,
        "before": before,
        "after": after,
        "freelist_pages": 0,
        "sidecars_after": sidecars,
        "content_backup_created": False,
    }


def _canonical_plan_sha256(plan: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), default=str).encode()
    return _sha256(canonical)


def _read_fence(root: Path) -> dict[str, Any] | None:
    path = root / CUTOVER_FENCE_FILENAME
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        baseline = record["baseline_plan"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise CutoverRefused("cutover_fence_invalid") from error
    if (
        record.get("version") != 1
        or baseline.get("plan_sha256") != record.get("plan_sha256")
        or _canonical_plan_sha256(baseline) != record.get("plan_sha256")
    ):
        raise CutoverRefused("cutover_fence_plan_invalid")
    return record


def _write_fence(root: Path, plan: dict[str, Any]) -> Path:
    path = root / CUTOVER_FENCE_FILENAME
    existing = _read_fence(root)
    if existing is not None:
        if existing.get("plan_sha256") != plan.get("plan_sha256"):
            raise CutoverRefused("cutover_fence_targets_different_plan")
        return path
    record = {
        "version": 1,
        "plan_sha256": plan["plan_sha256"],
        "collection_identity_sha256": plan["milvus_memory_collection"]["identity_sha256"],
        "workspace_identity_sha256": plan["workspace_identity_sha256"],
        "started_at": datetime.now(UTC).isoformat(),
        "baseline_plan": plan,
    }
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise CutoverRefused("cutover_fence_create_failed") from error
    return path


def _resume_matches(current: dict[str, Any], fence: dict[str, Any]) -> bool:
    baseline = fence["baseline_plan"]
    if (
        current["workspace_identity_sha256"] != baseline["workspace_identity_sha256"]
        or current["preserved_domains_before"] != baseline["preserved_domains_before"]
        or current["knowledge_collection"] != baseline["knowledge_collection"]
    ):
        return False

    baseline_databases = {item["file"]: item for item in baseline["sqlite_targets"]}
    current_databases = {item["file"]: item for item in current["sqlite_targets"]}
    if baseline_databases.keys() != current_databases.keys():
        return False
    for name, original in baseline_databases.items():
        now = current_databases[name]
        if (
            now.get("identity_sha256") != original.get("identity_sha256")
            or now.get("exists") != original.get("exists")
            or now.get("tables") != original.get("tables")
            or any(now["row_counts"].get(table, 0) > count
                   for table, count in original["row_counts"].items())
        ):
            return False

    original_memory = baseline["milvus_memory_collection"]
    now_memory = current["milvus_memory_collection"]
    if now_memory["identity_sha256"] != original_memory["identity_sha256"]:
        return False
    if now_memory["exists"]:
        if original_memory["exists"] and now_memory["schema_sha256"] != original_memory["schema_sha256"]:
            return False
        if now_memory["row_count"] > original_memory["row_count"]:
            return False
    return True


def _report_target(path: Path, workspace_root: Path) -> Path:
    target = path.expanduser().resolve(strict=False)
    if target == workspace_root or workspace_root in target.parents:
        raise CutoverRefused("report_must_be_outside_workspace_root")
    if target.exists():
        raise CutoverRefused("report_path_already_exists")
    if not target.parent.exists() or target.is_symlink():
        raise CutoverRefused("report_parent_unavailable")
    return target


async def apply_plan(
    settings: Settings, *, expected_plan_sha256: str, report_path: Path,
) -> dict[str, Any]:
    root = _workspace_root(settings)
    fence_record = _read_fence(root)
    current_plan = await build_plan(settings)
    if fence_record is None:
        if current_plan["plan_sha256"] != expected_plan_sha256:
            raise CutoverRefused("plan_changed_since_dry_run")
        plan = current_plan
    else:
        if fence_record["plan_sha256"] != expected_plan_sha256 or not _resume_matches(
            current_plan, fence_record,
        ):
            raise CutoverRefused("interrupted_cutover_target_or_preserved_state_changed")
        plan = fence_record["baseline_plan"]
    report_file = _report_target(report_path, root)
    if os.environ.get(ALLOW_SHARED_ROOT_ENV, "").strip().lower() in _TRUTHY:
        raise CutoverRefused("shared_workspace_escape_hatch_must_be_disabled")

    try:
        lock = InstanceLock(root, allow_cutover=True).acquire()
    except InstanceLockError as error:
        raise CutoverRefused("workspace_writer_is_active") from error
    fence = None
    try:
        # Recompute under the process lock; no writer may change the target after this point.
        locked_plan = await build_plan(settings)
        if fence_record is None:
            if locked_plan["plan_sha256"] != expected_plan_sha256:
                raise CutoverRefused("plan_changed_while_acquiring_writer_fence")
        elif not _resume_matches(locked_plan, fence_record):
            raise CutoverRefused("interrupted_cutover_target_or_preserved_state_changed")
        fence = _write_fence(root, plan)
        results = [
            _clear_sqlite_database(_sqlite_target(root, name))
            for name in _DATABASE_TABLES
        ]

        vectors = build_memory_vector_client(settings)
        try:
            collections = await vectors.connect()
            if settings.milvus_collection in collections:
                await vectors._call(
                    "drop_collection", collection_name=settings.milvus_collection,
                )
            await vectors.initialize()
            memory_after = await _milvus_collection_inventory(
                vectors, settings, settings.milvus_collection,
            )
            if not memory_after["exists"] or memory_after["row_count"] != 0:
                raise CutoverRefused("milvus_memory_collection_not_empty_after_reset")
            knowledge_after = None
            if settings.knowledge_collection:
                knowledge_after = await _milvus_collection_inventory(
                    vectors, settings, settings.knowledge_collection,
                )
        finally:
            await vectors.close()

        preserved_after = _preserved_local_domains(settings, root)
        preserved_unchanged = preserved_after == plan["preserved_domains_before"]
        knowledge_unchanged = knowledge_after == plan["knowledge_collection"]
        if not preserved_unchanged or not knowledge_unchanged:
            raise CutoverRefused("preserved_domain_proof_mismatch")
        if any(any(item["after"].values()) for item in results if item["exists"]):
            raise CutoverRefused("sqlite_memory_rows_remain")

        report = {
            "ticket": 303,
            "status": "completed",
            "completed_at": datetime.now(UTC).isoformat(),
            "plan_sha256": expected_plan_sha256,
            "sqlite_results": [
                {
                    **result,
                    "baseline_row_counts": next(
                        item["row_counts"] for item in plan["sqlite_targets"]
                        if item["file"] == result["file"]
                    ),
                }
                for result in results
            ],
            "milvus_memory_collection": {
                **memory_after,
                "drop_requested": True,
                "recreated": True,
            },
            "knowledge_collection_before": plan["knowledge_collection"],
            "knowledge_collection_after": knowledge_after,
            "preserved_domains_before": plan["preserved_domains_before"],
            "preserved_domains_after": preserved_after,
            "preserved_domains_unchanged": preserved_unchanged,
            "knowledge_unchanged": knowledge_unchanged,
            "content_backup_created": False,
        }
        with report_file.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        fence.unlink()
        fence = None
        return report
    finally:
        lock.release()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Memory V2 clean-slate cutover")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--dry-run", action="store_true")
    actions.add_argument("--apply", action="store_true")
    actions.add_argument("--resume", action="store_true")
    parser.add_argument("--confirm-plan-sha256")
    parser.add_argument("--report", type=Path)
    return parser


async def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings()
    except Exception:  # noqa: BLE001 - keep configuration failure details and secrets out of CLI output.
        print(json.dumps({"status": "refused", "reason": "settings_unavailable"}))
        return 2
    try:
        if args.dry_run:
            if args.confirm_plan_sha256 or args.report:
                raise CutoverRefused("dry_run_does_not_accept_apply_options")
            result = await build_plan(settings)
            result["status"] = "dry_run"
            result["generated_at"] = datetime.now(UTC).isoformat()
        elif args.resume:
            if args.confirm_plan_sha256 or not args.report:
                raise CutoverRefused("resume_requires_report_and_no_plan_override")
            fence = _read_fence(_workspace_root(settings))
            if fence is None:
                raise CutoverRefused("no_interrupted_cutover_to_resume")
            result = await apply_plan(
                settings, expected_plan_sha256=fence["plan_sha256"], report_path=args.report,
            )
        else:
            if not args.confirm_plan_sha256 or not args.report:
                raise CutoverRefused("apply_requires_plan_hash_and_report_path")
            result = await apply_plan(
                settings, expected_plan_sha256=args.confirm_plan_sha256,
                report_path=args.report,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except CutoverRefused as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    except Exception as error:  # noqa: BLE001 - failures are reported by type only, never by message.
        print(json.dumps({"status": "failed", "reason": type(error).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
