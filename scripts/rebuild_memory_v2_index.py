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

_RECORD_COLUMN_CONTRACT = {
    "memory_id": ("TEXT", False),
    "schema_version": ("INTEGER", True),
    "root_id": ("TEXT", True),
    "version": ("INTEGER", True),
    "kind": ("TEXT", True),
    "tier": ("TEXT", True),
    "scope": ("TEXT", True),
    "tenant_id": ("TEXT", True),
    "user_id": ("TEXT", True),
    "project_id": ("TEXT", False),
    "content": ("TEXT", True),
    "payload": ("TEXT", True),
    "status": ("TEXT", True),
    "importance": ("REAL", True),
    "strength": ("REAL", True),
    "source_type": ("TEXT", True),
    "source_session_id": ("TEXT", False),
    "source_event_ids": ("TEXT", True),
    "evidence": ("TEXT", True),
    "valid_at": ("TEXT", False),
    "invalidated_at": ("TEXT", False),
    "superseded_by": ("TEXT", False),
    "created_at": ("TEXT", True),
    "updated_at": ("TEXT", True),
}
_OUTBOX_COLUMN_CONTRACT = {
    "memory_id": ("TEXT", False),
    "revision": ("TEXT", True),
    "operation": ("TEXT", True),
    "tenant_id": ("TEXT", True),
    "user_id": ("TEXT", True),
    "scope": ("TEXT", True),
    "project_id": ("TEXT", False),
}


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
    """Refuse missing or incompatible authority schema before taking the workspace lock."""
    if not database.is_file():
        raise ValueError("existing Memory V2 database was not found")
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('memory_v2_records', 'memory_v2_outbox')"
            ).fetchall()
            if {row[0] for row in tables} != {
                "memory_v2_records", "memory_v2_outbox",
            }:
                raise ValueError("existing Memory V2 schema was not found")

            _validate_table_columns(
                connection, "memory_v2_records", _RECORD_COLUMN_CONTRACT,
            )
            _validate_table_columns(
                connection, "memory_v2_outbox", _OUTBOX_COLUMN_CONTRACT,
            )
            _validate_primary_key(connection, "memory_v2_records")
            _validate_primary_key(connection, "memory_v2_outbox")
            _validate_unique_index(
                connection, "memory_v2_records", ("root_id", "version"),
            )
            _validate_active_index(connection)
            _validate_outbox_operation_check(connection)
        finally:
            connection.close()
    except sqlite3.Error:
        raise ValueError("existing Memory V2 schema could not be validated") from None


def _validate_table_columns(
    connection: sqlite3.Connection,
    table: str,
    expected: dict[str, tuple[str, bool]],
) -> None:
    quoted_table = table.replace('"', '""')
    rows = connection.execute(f'PRAGMA table_info("{quoted_table}")').fetchall()
    columns = {row[1]: row for row in rows}
    if not expected.keys() <= columns.keys():
        raise ValueError("existing Memory V2 schema is incomplete or incompatible")
    for name, (expected_type, expected_notnull) in expected.items():
        column = columns[name]
        if column[2].upper() != expected_type or bool(column[3]) != expected_notnull:
            raise ValueError("existing Memory V2 schema is incomplete or incompatible")
    if any(
        name not in expected and bool(column[3])
        and _is_null_or_unproven_default(column[4])
        for name, column in columns.items()
    ):
        raise ValueError("existing Memory V2 schema is incomplete or incompatible")


def _validate_primary_key(connection: sqlite3.Connection, table: str) -> None:
    quoted_table = table.replace('"', '""')
    rows = connection.execute(f'PRAGMA table_info("{quoted_table}")').fetchall()
    primary_key = [row[1] for row in sorted(rows, key=lambda item: item[5]) if row[5]]
    if primary_key != ["memory_id"]:
        raise ValueError("existing Memory V2 schema is incomplete or incompatible")


def _index_columns(connection: sqlite3.Connection, index: str) -> list[str | None]:
    quoted_index = index.replace('"', '""')
    rows = connection.execute(f'PRAGMA index_info("{quoted_index}")').fetchall()
    return [row[2] for row in sorted(rows, key=lambda item: item[0])]


def _validate_unique_index(
    connection: sqlite3.Connection,
    table: str,
    expected_columns: tuple[str, ...],
) -> None:
    quoted_table = table.replace('"', '""')
    rows = connection.execute(f'PRAGMA index_list("{quoted_table}")').fetchall()
    if not any(
        bool(row[2])
        and not bool(row[4])
        and _index_columns(connection, row[1]) == list(expected_columns)
        for row in rows
    ):
        raise ValueError("existing Memory V2 schema is incomplete or incompatible")


def _normalize_schema_sql(sql: str | None) -> str:
    if sql is None:
        return ""
    parts = re.split(r"('(?:''|[^'])*')", _strip_sql_comments(sql))
    normalized_parts = []
    for index, part in enumerate(parts):
        if index % 2:
            normalized_parts.append(part)
            continue
        normalized_parts.append(_normalize_sql_code(part))
    normalized = "".join(normalized_parts)
    return normalized.rstrip(";")


def _normalize_sql_code(sql: str) -> str:
    normalized = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char in '"`[':
            end = _skip_quoted_sql(sql, index)
            raw = sql[index:end]
            closing = "]" if char == "[" else char
            identifier = raw[1:-1].replace(closing * 2, closing)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
                normalized.append(identifier.casefold())
            else:
                normalized.append(raw.casefold())
            index = end
        elif char.isspace():
            index += 1
        else:
            normalized.append(char.casefold())
            index += 1
    return "".join(normalized)


def _skip_quoted_sql(sql: str, start: int) -> int:
    opening = sql[start]
    closing = "]" if opening == "[" else opening
    index = start + 1
    while index < len(sql):
        if sql[index] == closing:
            if closing != "]" and index + 1 < len(sql) and sql[index + 1] == closing:
                index += 2
                continue
            return index + 1
        index += 1
    return len(sql)


def _strip_sql_comments(sql: str) -> str:
    result = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char in "'\"`[":
            end = _skip_quoted_sql(sql, index)
            result.append(sql[index:end])
            index = end
        elif sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            if newline < 0:
                break
            result.append("\n")
            index = newline + 1
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                break
            result.append(" ")
            index = end + 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _is_null_or_unproven_default(value: object) -> bool:
    if value is None:
        return True
    normalized = _strip_redundant_parentheses(_normalize_schema_sql(str(value)))
    if normalized.casefold() == "null":
        return True
    known_non_null = (
        r"'(?:''|[^'])*'"
        r"|x'(?:[0-9a-f]{2})*'"
        r"|[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?"
        r"|0x[0-9a-f]+"
        r"|current_(?:time|date|timestamp)"
        r"|true|false"
    )
    return re.fullmatch(known_non_null, normalized, flags=re.IGNORECASE) is None


def _strip_redundant_parentheses(expression: str) -> str:
    """Remove only parentheses that enclose the complete expression."""
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        quoted = False
        wraps_expression = True
        index = 0
        while index < len(expression):
            char = expression[index]
            if char == "'":
                if quoted and index + 1 < len(expression) and expression[index + 1] == "'":
                    index += 2
                    continue
                quoted = not quoted
            elif not quoted and char == "(":
                depth += 1
            elif not quoted and char == ")":
                depth -= 1
                if depth == 0 and index != len(expression) - 1:
                    wraps_expression = False
                    break
            index += 1
        if not wraps_expression or depth != 0 or quoted:
            return expression
        expression = expression[1:-1]
    return expression


def _validate_active_index(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "PRAGMA index_list(\"memory_v2_records\")"
    ).fetchall()
    index = next((row for row in rows if row[1] == "memory_v2_one_active"), None)
    if index is None or not bool(index[2]) or not bool(index[4]):
        raise ValueError("existing Memory V2 schema is incomplete or incompatible")
    if _index_columns(connection, index[1]) != ["root_id"]:
        raise ValueError("existing Memory V2 schema is incomplete or incompatible")
    definition = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (index[1],),
    ).fetchone()
    sql = definition[0] if definition else None
    sql_without_comments = _strip_sql_comments(sql or "")
    where = re.search(r"\bwhere\b", sql_without_comments, flags=re.IGNORECASE)
    predicate = (
        _normalize_schema_sql(sql_without_comments[where.end():]) if where
        else ""
    )
    if _strip_redundant_parentheses(predicate) != "status='active'":
        raise ValueError("existing Memory V2 schema is incomplete or incompatible")


def _check_expressions(sql: str) -> list[str]:
    expressions = []
    sql = _strip_sql_comments(sql)
    index = 0
    while index < len(sql):
        char = sql[index]
        if char in "'\"`[":
            index = _skip_quoted_sql(sql, index)
        elif char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            if sql[index:end].casefold() == "check":
                opening = end
                while opening < len(sql) and sql[opening].isspace():
                    opening += 1
                if opening < len(sql) and sql[opening] == "(":
                    parsed = _extract_parenthesized_sql(sql, opening)
                    if parsed is None:
                        return []
                    expression, index = parsed
                    expressions.append(expression)
                    continue
            index = end
        else:
            index += 1
    return expressions


def _extract_parenthesized_sql(sql: str, opening: int) -> tuple[str, int] | None:
    depth = 0
    index = opening
    while index < len(sql):
        char = sql[index]
        if char in "'\"`[":
            index = _skip_quoted_sql(sql, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql[opening + 1:index], index + 1
        index += 1
    return None


def _validate_outbox_operation_check(connection: sqlite3.Connection) -> None:
    definition = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_v2_outbox'"
    ).fetchone()
    sql = definition[0] if definition else None
    expressions = _check_expressions(sql or "")
    normalized_expression = _strip_redundant_parentheses(
        _normalize_schema_sql(expressions[0]),
    ) if len(expressions) == 1 else ""
    if (
        len(expressions) != 1
        or normalized_expression != "operationin('upsert','delete')"
    ):
        raise ValueError("existing Memory V2 schema is incomplete or incompatible")


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
