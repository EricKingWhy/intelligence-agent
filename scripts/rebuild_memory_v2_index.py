"""Rebuild active Memory V2 records from ``<workspace>/memory-v2.db`` into Milvus.

This command does not rebuild legacy V1 data in ``memory.db``. Stop every writer that can target
the configured Memory collection, including processes using other workspace roots or clones.
The local maintenance fence can coordinate only this workspace root. The command targets only
the configured Memory collection and never drops or clears a collection.
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
import tempfile
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from agent_harness.capability.factories import build_memory_vector_client
from agent_harness.config import Settings
from agent_harness.instance_lock import (
    ALLOW_SHARED_ROOT_ENV,
    MEMORY_INDEX_REBUILD_FENCE_FILENAME,
    InstanceLock,
)
from agent_harness.memory.v2.assembly import MEMORY_V2_DATABASE_NAME
from agent_harness.memory.v2.index import MemoryV2IndexRelay
from agent_harness.memory.v2.milvus_index import MilvusMemoryV2Index
from agent_harness.memory.v2.store import SqliteMemoryV2Store

_FENCE_VERSION = 1
_FENCE_OPERATION = "memory-v2-index-rebuild"
_MAX_FENCE_BYTES = 4096


def memory_collection_name(settings: Settings) -> str:
    """Select the application Memory collection and refuse an aliased Knowledge target."""
    collection = settings.milvus_collection
    knowledge_collection = settings.knowledge_collection
    if not collection.strip():
        raise ValueError("Memory collection is not configured")
    if collection != collection.strip():
        raise ValueError("Memory collection must not contain surrounding whitespace")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", collection):
        raise ValueError("Memory collection name is invalid")
    if knowledge_collection and collection.casefold() == knowledge_collection.casefold():
        raise ValueError("Memory collection must differ from the Knowledge collection")
    return collection


def validate_existing_memory_v2_database(database: Path) -> None:
    """Refuse to create or initialize a database when the operator chose the wrong path."""
    if not database.is_file():
        raise ValueError("existing Memory V2 database was not found")
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('memory_v2_records', 'memory_v2_outbox')"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        raise ValueError("existing Memory V2 database could not be read") from None
    if {row[0] for row in rows} != {"memory_v2_records", "memory_v2_outbox"}:
        raise ValueError("existing Memory V2 schema was not found")


def reject_shared_root_mode() -> None:
    truthy = {"1", "true", "yes", "on"}
    if os.environ.get(ALLOW_SHARED_ROOT_ENV, "").strip().lower() in truthy:
        raise RuntimeError(
            "rebuild refuses ALLOW_SHARED_ROOT because it requires an exclusive writer lock"
        )


def _rebuild_fence_identity(workspace: Path, settings: Settings) -> dict[str, object]:
    """Identify the workspace and target without persisting endpoint/credential values."""
    canonical_workspace = os.path.normcase(os.path.realpath(workspace))
    target = json.dumps(
        [settings.milvus_uri, memory_collection_name(settings)],
        ensure_ascii=True,
        separators=(",", ":"),
    )

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    return {
        "version": _FENCE_VERSION,
        "operation": _FENCE_OPERATION,
        "workspace_sha256": digest(canonical_workspace),
        "target_sha256": digest(target),
    }


def _read_rebuild_fence(
    path: Path, *, allow_temporary_hard_link: bool = False,
) -> dict[str, object] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    valid_link_count = info.st_nlink == 1 or (
        allow_temporary_hard_link and info.st_nlink == 2
    )
    if not path.is_file() or path.is_symlink() or not valid_link_count:
        raise RuntimeError("Memory V2 index rebuild fence is not a regular private file")
    if info.st_size > _MAX_FENCE_BYTES:
        raise RuntimeError("Memory V2 index rebuild fence is invalid")
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = stream.read(_MAX_FENCE_BYTES + 1)
        if len(raw) > _MAX_FENCE_BYTES:
            raise ValueError
        value = json.loads(raw)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("Memory V2 index rebuild fence cannot be validated") from None
    if not isinstance(value, dict):
        raise TypeError("Memory V2 index rebuild fence cannot be validated")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_and_recover_published_fence(path: Path) -> dict[str, object] | None:
    """Remove a same-directory temp hard link left after atomic publication."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if info.st_nlink == 1:
        return _read_rebuild_fence(path)

    _read_rebuild_fence(path, allow_temporary_hard_link=True)
    temporary_links = []
    for candidate in path.parent.glob(f"{path.name}.*.tmp"):
        try:
            candidate_info = candidate.lstat()
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate_info.st_nlink == 2
                and os.path.samefile(path, candidate)
            ):
                temporary_links.append(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            raise RuntimeError(
                "Memory V2 index rebuild fence cannot be validated"
            ) from None
    if len(temporary_links) != 1:
        raise RuntimeError("Memory V2 index rebuild fence cannot be validated")
    temporary_links[0].unlink()
    _fsync_directory(path.parent)
    return _read_rebuild_fence(path)


def _publish_or_validate_rebuild_fence(
    workspace: Path, settings: Settings,
) -> dict[str, object]:
    path = workspace / MEMORY_INDEX_REBUILD_FENCE_FILENAME
    expected = _rebuild_fence_identity(workspace, settings)
    current = _read_and_recover_published_fence(path)
    if current is not None:
        if current != expected:
            raise RuntimeError("an incompatible Memory V2 index rebuild fence already exists")
        return expected

    payload = (json.dumps(expected, sort_keys=True) + "\n").encode("utf-8")
    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=workspace,
        )
        temporary_path = Path(temporary_name)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("fence write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        os.link(temporary_path, path)
        temporary_path.unlink()
        _fsync_directory(workspace)
        return expected
    except FileExistsError:
        if _read_and_recover_published_fence(path) == expected:
            return expected
        raise RuntimeError("Memory V2 index rebuild fence changed unexpectedly") from None
    except OSError:
        raise RuntimeError("Memory V2 index rebuild fence could not be persisted") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _clear_rebuild_fence(workspace: Path, expected: dict[str, object]) -> None:
    path = workspace / MEMORY_INDEX_REBUILD_FENCE_FILENAME
    if _read_rebuild_fence(path) != expected:
        raise RuntimeError("Memory V2 index rebuild fence changed before completion")
    path.unlink()
    _fsync_directory(workspace)


async def _rebuild(database: Path, settings: Settings) -> int:
    store = SqliteMemoryV2Store(database)
    vectors = build_memory_vector_client(settings)
    try:
        await vectors.initialize()
        relay = MemoryV2IndexRelay(store, MilvusMemoryV2Index(vectors))
        return await relay.rebuild_from_authority()
    finally:
        await vectors.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-dir",
        help=(
            "workspace containing the existing memory-v2.db (defaults to Settings; "
            "relative paths resolve from the current working directory)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings()
        if args.workspace_dir:
            settings.workspace_dir = args.workspace_dir
        # Match Settings consumers in assembly/web: relative workspace paths are CWD-relative.
        workspace = Path(settings.workspace_dir).expanduser().resolve()
        database = workspace / MEMORY_V2_DATABASE_NAME
        validate_existing_memory_v2_database(database)
        memory_collection_name(settings)
        reject_shared_root_mode()
        lock = InstanceLock(
            workspace, allow_memory_index_rebuild=True,
        ).acquire()
    except Exception as error:  # noqa: BLE001 -- sanitize provider and configuration failures.
        print(
            f"Memory V2 index rebuild refused ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 1

    try:
        try:
            fence = _publish_or_validate_rebuild_fence(workspace, settings)
            # Publish first: this scan then refuses existing bypass writers, while their
            # post-registration fence check prevents a new bypass writer from starting.
            lock.assert_no_shared_root_writers()
        except Exception as error:  # noqa: BLE001 -- sanitize local state failures.
            print(
                f"Memory V2 index rebuild refused ({type(error).__name__}).",
                file=sys.stderr,
            )
            return 1

        try:
            count = asyncio.run(_rebuild(database, settings))
        except Exception as error:  # noqa: BLE001 -- sanitize provider and configuration failures.
            print(
                f"Memory V2 index rebuild failed ({type(error).__name__}); fence retained for retry.",
                file=sys.stderr,
            )
            return 1

        try:
            _clear_rebuild_fence(workspace, fence)
        except Exception as error:  # noqa: BLE001 -- keep startup blocked on uncertain state.
            print(
                f"Memory V2 index rebuild failed ({type(error).__name__}); inspect fence before retry.",
                file=sys.stderr,
            )
            return 1
    finally:
        lock.release()

    print(
        f"Memory V2 index rebuild complete: {count} active records queued; outbox drained."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
