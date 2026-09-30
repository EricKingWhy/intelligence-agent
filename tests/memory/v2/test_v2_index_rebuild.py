from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agent_harness.instance_lock import (
    MEMORY_INDEX_REBUILD_FENCE_FILENAME,
    InstanceLock,
)
from agent_harness.memory.v2.index import InMemoryMemoryV2Index, MemoryV2IndexRelay
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import MemoryScope, TrustedMemoryIdentity
from tests.memory.v2._records import make_draft

USER_A = TrustedMemoryIdentity("tenant-a", "user-a")
USER_B = TrustedMemoryIdentity("tenant-a", "user-b")
PROJECT_A = TrustedMemoryIdentity("tenant-a", "user-a", "project-a")


@pytest_asyncio.fixture
async def store(tmp_path) -> SqliteMemoryV2Store:
    value = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await value.initialize()
    return value


@pytest_asyncio.fixture
async def index() -> InMemoryMemoryV2Index:
    return InMemoryMemoryV2Index()


@pytest.mark.asyncio
async def test_rebuild_restores_only_current_active_records_and_is_idempotent(
    store, index
) -> None:
    user_record = await store.create(
        make_draft(content="alpha user preference"), USER_A
    )
    project_record = await store.create(
        make_draft(
            content="project-only workflow",
            scope=MemoryScope.PROJECT,
            project_id="project-a",
        ),
        PROJECT_A,
    )
    other_user_record = await store.create(
        make_draft(content="bravo private preference"),
        USER_B,
    )
    old_version = await store.create(make_draft(content="old version"), USER_A)
    invalidated = await store.create(make_draft(content="revoked preference"), USER_A)

    relay = MemoryV2IndexRelay(store, index)
    await relay.flush()
    successor = await store.update(
        old_version.id,
        make_draft(content="current replacement"),
        USER_A,
    )
    await store.invalidate(invalidated.id, USER_A)
    await relay.flush()

    # Simulate an operator losing the complete derived collection.
    index._rows.clear()
    assert await store.pending() == []

    assert await relay.rebuild_from_authority() == 4
    assert len(index._rows) == 4
    assert await store.pending() == []
    assert await index.contains(user_record.id, USER_A, MemoryScope.USER_GLOBAL)
    assert await index.contains(project_record.id, PROJECT_A, MemoryScope.PROJECT)
    assert await index.contains(other_user_record.id, USER_B, MemoryScope.USER_GLOBAL)
    assert await index.contains(successor.id, USER_A, MemoryScope.USER_GLOBAL)
    assert not await index.contains(old_version.id, USER_A, MemoryScope.USER_GLOBAL)
    assert not await index.contains(invalidated.id, USER_A, MemoryScope.USER_GLOBAL)

    assert await index.search("alpha", USER_A, MemoryScope.USER_GLOBAL, 10) == [
        (user_record.id, 1.0),
    ]
    assert await index.search("alpha", USER_B, MemoryScope.USER_GLOBAL, 10) == []
    assert await index.search("workflow", PROJECT_A, MemoryScope.PROJECT, 10) == [
        (project_record.id, 1.0),
    ]
    assert await index.search("workflow", USER_B, MemoryScope.PROJECT, 10) == []

    assert await relay.rebuild_from_authority() == 4
    assert len(index._rows) == 4
    assert await store.pending() == []
    assert len(set(index._rows)) == 4


@pytest.mark.asyncio
async def test_rebuild_failure_keeps_outbox_and_returns_failure(store, index) -> None:
    await store.create(make_draft(content="durable preference"), USER_A)
    index.fail_upsert = RuntimeError("opaque provider failure")
    relay = MemoryV2IndexRelay(store, index)

    with pytest.raises(RuntimeError, match="rebuild did not converge"):
        await relay.rebuild_from_authority()

    assert len(await store.pending()) == 1
    index.fail_upsert = None
    assert await relay.rebuild_from_authority() == 1
    assert await store.pending() == []


@pytest.mark.asyncio
async def test_rebuild_preserves_pending_delete_for_inactive_record(store, index) -> None:
    record = await store.create(make_draft(content="revoked preference"), USER_A)
    await store.invalidate(record.id, USER_A)
    relay = MemoryV2IndexRelay(store, index)

    assert await relay.rebuild_from_authority() == 0

    assert index.upsert_calls == []
    assert index.delete_calls == [record.id]
    assert await store.pending() == []


def test_rebuild_uses_memory_collection_and_rejects_knowledge_target() -> None:
    from scripts.rebuild_memory_v2_index import memory_collection_name

    settings = SimpleNamespace(
        milvus_collection="long_term_memory",
        knowledge_collection="knowledge_documents",
    )
    assert memory_collection_name(settings) == "long_term_memory"

    settings.milvus_collection = "KNOWLEDGE_DOCUMENTS"
    with pytest.raises(ValueError, match="must differ"):
        memory_collection_name(settings)

    settings.milvus_collection = ""
    with pytest.raises(ValueError, match="not configured"):
        memory_collection_name(settings)

    settings.milvus_collection = " long_term_memory "
    with pytest.raises(ValueError, match="surrounding whitespace"):
        memory_collection_name(settings)

    settings.milvus_collection = "memory-v2"
    with pytest.raises(ValueError, match="name is invalid"):
        memory_collection_name(settings)


@pytest.mark.asyncio
async def test_command_preflight_requires_an_existing_v2_database(tmp_path) -> None:
    from scripts.rebuild_memory_v2_index import validate_existing_memory_v2_database

    database = tmp_path / "memory-v2.db"
    with pytest.raises(ValueError, match="not found"):
        validate_existing_memory_v2_database(database)
    assert not database.exists()

    initialized = SqliteMemoryV2Store(database)
    await initialized.initialize()
    validate_existing_memory_v2_database(database)


def _replace_table_definition(
    connection: sqlite3.Connection,
    table: str,
    old: str,
    new: str,
) -> None:
    definition = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone()[0]
    indexes = [
        row[0]
        for row in connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,),
        )
    ]
    replacement = f"{table}_replacement"
    replacement_definition = definition.replace(
        f"CREATE TABLE {table}", f"CREATE TABLE {replacement}", 1,
    )
    assert old in replacement_definition
    connection.execute(replacement_definition.replace(old, new, 1))
    connection.execute(f'DROP TABLE "{table}"')
    connection.execute(f'ALTER TABLE "{replacement}" RENAME TO "{table}"')
    for index in indexes:
        connection.execute(index)


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        ("memory_v2_records", "content TEXT NOT NULL", "content BLOB NOT NULL"),
        ("memory_v2_records", "status TEXT NOT NULL", "status TEXT"),
        ("memory_v2_records", "project_id TEXT,", "project_id TEXT NOT NULL,"),
        ("memory_v2_records", "memory_id TEXT PRIMARY KEY", "memory_id TEXT"),
        ("memory_v2_records", ",\n    UNIQUE(root_id, version)", ""),
        ("memory_v2_outbox", "revision TEXT NOT NULL", "revision BLOB NOT NULL"),
        ("memory_v2_outbox", "scope TEXT NOT NULL", "scope TEXT"),
        ("memory_v2_outbox", "project_id TEXT", "project_id TEXT NOT NULL"),
        ("memory_v2_outbox", "revision TEXT NOT NULL,", ""),
        ("memory_v2_outbox", "memory_id TEXT PRIMARY KEY", "memory_id TEXT"),
        (
            "memory_v2_outbox",
            " CHECK (operation IN ('upsert', 'delete'))",
            "",
        ),
        (
            "memory_v2_outbox",
            " CHECK (operation IN ('upsert', 'delete'))",
            " /* CHECK (operation IN ('upsert', 'delete')) */",
        ),
        (
            "memory_v2_outbox",
            " CHECK (operation IN ('upsert', 'delete'))",
            ", marker TEXT DEFAULT \"CHECK(operation IN ('upsert', 'delete'))\"",
        ),
        (
            "memory_v2_outbox",
            "CHECK (operation IN ('upsert', 'delete'))",
            "CHECK (operation IN ('upsert', 'delete')) CHECK (operation = 'delete')",
        ),
        (
            "memory_v2_outbox",
            "CHECK (operation IN ('upsert', 'delete'))",
            "CHECK (operation IN ('upsert', 'delete') AND 0)",
        ),
        (
            "memory_v2_outbox",
            "CHECK (operation IN ('upsert', 'delete'))",
            "CHECK (operation IN ('UPSERT', 'DELETE'))",
        ),
        (
            "memory_v2_records",
            "updated_at TEXT NOT NULL",
            "updated_at TEXT NOT NULL,\n    extension_required TEXT NOT NULL",
        ),
        (
            "memory_v2_records",
            "updated_at TEXT NOT NULL",
            "updated_at TEXT NOT NULL,\n    extension_required TEXT NOT NULL DEFAULT NULL",
        ),
        (
            "memory_v2_records",
            "updated_at TEXT NOT NULL",
            "updated_at TEXT NOT NULL,\n    extension_required TEXT NOT NULL DEFAULT (NULL)",
        ),
        (
            "memory_v2_records",
            "updated_at TEXT NOT NULL",
            "updated_at TEXT NOT NULL,\n    extension_required TEXT NOT NULL DEFAULT (NULL /* comment */)",
        ),
        (
            "memory_v2_records",
            "updated_at TEXT NOT NULL",
            "updated_at TEXT NOT NULL,\n    extension_required TEXT NOT NULL DEFAULT (NULLIF('x', 'x'))",
        ),
        (
            "memory_v2_outbox",
            "project_id TEXT",
            "project_id TEXT, extension_required TEXT NOT NULL",
        ),
        (
            "memory_v2_outbox",
            "project_id TEXT",
            "project_id TEXT, extension_required TEXT NOT NULL DEFAULT NULL",
        ),
        (
            "memory_v2_outbox",
            "project_id TEXT",
            "project_id TEXT, extension_required TEXT NOT NULL DEFAULT (NULL)",
        ),
        (
            "memory_v2_outbox",
            "project_id TEXT",
            "project_id TEXT, extension_required TEXT NOT NULL DEFAULT (NULL /* comment */)",
        ),
        (
            "memory_v2_outbox",
            "project_id TEXT",
            "project_id TEXT, extension_required TEXT NOT NULL DEFAULT (NULLIF('x', 'x'))",
        ),
    ],
    ids=[
        "wrong-column-type",
        "nullable-required-column",
        "non-null-optional-column",
        "missing-records-primary-key",
        "missing-root-version-uniqueness",
        "wrong-outbox-column-type",
        "nullable-outbox-column",
        "non-null-optional-outbox-column",
        "missing-outbox-column",
        "missing-outbox-primary-key",
        "missing-operation-check",
        "comment-only-operation-check",
        "string-only-operation-check",
        "conflicting-operation-check",
        "overconstrained-operation-check",
        "case-sensitive-operation-literals",
        "extra-required-record-column",
        "extra-required-record-column-default-null",
        "extra-required-record-column-default-parenthesized-null",
        "extra-required-record-column-default-commented-null",
        "extra-required-record-column-default-nullif",
        "extra-required-outbox-column",
        "extra-required-outbox-column-default-null",
        "extra-required-outbox-column-default-parenthesized-null",
        "extra-required-outbox-column-default-commented-null",
        "extra-required-outbox-column-default-nullif",
    ],
)
def test_preflight_rejects_incompatible_v2_schema(tmp_path, table, old, new) -> None:
    from scripts.rebuild_memory_v2_index import validate_existing_memory_v2_database

    database = tmp_path / "memory-v2.db"
    asyncio.run(SqliteMemoryV2Store(database).initialize())
    with sqlite3.connect(database) as connection:
        _replace_table_definition(connection, table, old, new)

    with pytest.raises(ValueError, match="incomplete or incompatible"):
        validate_existing_memory_v2_database(database)


def test_preflight_accepts_equivalent_parenthesized_schema(tmp_path) -> None:
    from scripts.rebuild_memory_v2_index import validate_existing_memory_v2_database

    database = tmp_path / "memory-v2.db"
    asyncio.run(SqliteMemoryV2Store(database).initialize())
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX memory_v2_one_active")
        connection.execute(
            "CREATE UNIQUE INDEX memory_v2_one_active "
            "ON memory_v2_records(root_id) WHERE (status='active')"
        )
        _replace_table_definition(
            connection,
            "memory_v2_outbox",
            "CHECK (operation IN ('upsert', 'delete'))",
            "CHECK ((operation IN ('upsert', 'delete')))",
        )

    validate_existing_memory_v2_database(database)


def test_preflight_rejects_quoted_string_active_index_predicate(tmp_path) -> None:
    from scripts.rebuild_memory_v2_index import validate_existing_memory_v2_database

    database = tmp_path / "memory-v2.db"
    asyncio.run(SqliteMemoryV2Store(database).initialize())
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX memory_v2_one_active")
        connection.execute(
            'CREATE UNIQUE INDEX memory_v2_one_active ON memory_v2_records(root_id) '
            'WHERE "status=\'active\'"'
        )

    with pytest.raises(ValueError, match="incomplete or incompatible"):
        validate_existing_memory_v2_database(database)


def test_preflight_accepts_quoted_active_index_column(tmp_path) -> None:
    from scripts.rebuild_memory_v2_index import validate_existing_memory_v2_database

    database = tmp_path / "memory-v2.db"
    asyncio.run(SqliteMemoryV2Store(database).initialize())
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX memory_v2_one_active")
        connection.execute(
            'CREATE UNIQUE INDEX memory_v2_one_active ON memory_v2_records(root_id) '
            'WHERE "status" = \'active\''
        )

    validate_existing_memory_v2_database(database)


def test_preflight_ignores_where_inside_index_comment(tmp_path) -> None:
    from scripts.rebuild_memory_v2_index import validate_existing_memory_v2_database

    database = tmp_path / "memory-v2.db"
    asyncio.run(SqliteMemoryV2Store(database).initialize())
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX memory_v2_one_active")
        connection.execute(
            "CREATE UNIQUE INDEX memory_v2_one_active ON memory_v2_records(root_id) "
            "/* WHERE status='inactive' */ WHERE status='active'"
        )

    validate_existing_memory_v2_database(database)


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        (
            "memory_v2_records",
            "updated_at TEXT NOT NULL",
            "updated_at TEXT NOT NULL, extension_required TEXT NOT NULL DEFAULT 'present'",
        ),
        (
            "memory_v2_outbox",
            "project_id TEXT",
            "project_id TEXT, extension_required TEXT NOT NULL DEFAULT 'present'",
        ),
    ],
    ids=["records-default", "outbox-default"],
)
def test_preflight_accepts_extra_required_column_with_default(
    tmp_path, table, old, new,
) -> None:
    from scripts.rebuild_memory_v2_index import validate_existing_memory_v2_database

    database = tmp_path / "memory-v2.db"
    asyncio.run(SqliteMemoryV2Store(database).initialize())
    with sqlite3.connect(database) as connection:
        _replace_table_definition(connection, table, old, new)

    validate_existing_memory_v2_database(database)


@pytest.mark.parametrize(
    "index_ddl",
    [
        (
            "CREATE INDEX memory_v2_one_active "
            "ON memory_v2_records(root_id) WHERE status='active'"
        ),
        (
            "CREATE UNIQUE INDEX memory_v2_one_active "
            "ON memory_v2_records(root_id) WHERE status='inactive'"
        ),
        (
            "CREATE UNIQUE INDEX memory_v2_one_active "
            "ON memory_v2_records(root_id) WHERE status='ACTIVE'"
        ),
        (
            "CREATE UNIQUE INDEX memory_v2_one_active "
            "ON memory_v2_records(root_id) WHERE kind='semantic' AND status='active'"
        ),
    ],
    ids=["not-unique", "wrong-predicate", "case-sensitive-literal", "extra-predicate"],
)
def test_preflight_rejects_incompatible_active_index(tmp_path, index_ddl) -> None:
    from scripts.rebuild_memory_v2_index import validate_existing_memory_v2_database

    database = tmp_path / "memory-v2.db"
    asyncio.run(SqliteMemoryV2Store(database).initialize())
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX memory_v2_one_active")
        connection.execute(index_ddl)

    with pytest.raises(ValueError, match="incomplete or incompatible"):
        validate_existing_memory_v2_database(database)


def test_preflight_rejects_partial_root_version_uniqueness(tmp_path) -> None:
    from scripts.rebuild_memory_v2_index import validate_existing_memory_v2_database

    database = tmp_path / "memory-v2.db"
    asyncio.run(SqliteMemoryV2Store(database).initialize())
    with sqlite3.connect(database) as connection:
        _replace_table_definition(
            connection, "memory_v2_records", ",\n    UNIQUE(root_id, version)", "",
        )
        connection.execute(
            "CREATE UNIQUE INDEX memory_v2_root_version_active "
            "ON memory_v2_records(root_id, version) WHERE status='active'"
        )

    with pytest.raises(ValueError, match="incomplete or incompatible"):
        validate_existing_memory_v2_database(database)


def test_command_rejects_incomplete_schema_before_lock_or_fence(tmp_path, monkeypatch, capsys) -> None:
    from scripts import rebuild_memory_v2_index as command

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = workspace / "memory-v2.db"
    asyncio.run(SqliteMemoryV2Store(database).initialize())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "ALTER TABLE memory_v2_records RENAME TO memory_v2_records_old"
        )
        connection.execute(
            "CREATE TABLE memory_v2_records (memory_id TEXT PRIMARY KEY)"
        )
        connection.execute("DROP TABLE memory_v2_records_old")

    class FakeSettings:
        workspace_dir = str(workspace)
        milvus_collection = "memory"
        knowledge_collection = "knowledge"
        milvus_uri = "https://milvus.example"

    lock_attempts = []

    class FakeLock:
        def __init__(self, root, *, allow_memory_index_rebuild=False):
            lock_attempts.append((root, allow_memory_index_rebuild))

        def acquire(self):
            return self

        def assert_no_shared_root_writers(self):
            pytest.fail("schema preflight must happen before writer scan")

        def release(self):
            pass

    monkeypatch.setattr(command, "Settings", FakeSettings)
    monkeypatch.setattr(command, "InstanceLock", FakeLock)
    monkeypatch.setattr(command, "reject_shared_root_mode", lambda: None)
    monkeypatch.setattr(
        command, "_rebuild", lambda *_args: pytest.fail("invalid schema must not rebuild"),
    )
    monkeypatch.delenv("ALLOW_SHARED_ROOT", raising=False)

    assert command.main([]) == 1
    assert lock_attempts == []
    assert not (workspace / MEMORY_INDEX_REBUILD_FENCE_FILENAME).exists()
    assert "ValueError" in capsys.readouterr().err


def test_command_returns_nonzero_without_echoing_provider_error(
    tmp_path, monkeypatch, capsys
) -> None:
    from scripts import rebuild_memory_v2_index as command

    monkeypatch.chdir(tmp_path)
    configured_workspace = tmp_path / "configured-workspace"
    configured_workspace.mkdir()
    database = configured_workspace / "memory-v2.db"
    initialized = SqliteMemoryV2Store(database)
    asyncio.run(initialized.initialize())

    class FakeSettings:
        workspace_dir = "configured-workspace"
        milvus_collection = "memory"
        knowledge_collection = "knowledge"
        milvus_uri = "https://milvus.example"

    class FakeLock:
        def __init__(self, workspace, *, allow_memory_index_rebuild=False) -> None:
            self.workspace = workspace
            self.allow_memory_index_rebuild = allow_memory_index_rebuild
            self.released = False
            self.checked_writers = False

        def acquire(self):
            return self

        def assert_no_shared_root_writers(self) -> None:
            self.checked_writers = True
            assert (self.workspace / MEMORY_INDEX_REBUILD_FENCE_FILENAME).is_file()

        def release(self) -> None:
            self.released = True

    locks = []

    def make_lock(workspace, *, allow_memory_index_rebuild=False):
        lock = FakeLock(
            workspace, allow_memory_index_rebuild=allow_memory_index_rebuild,
        )
        locks.append(lock)
        return lock

    monkeypatch.setattr(command, "Settings", FakeSettings)
    monkeypatch.setattr(command, "InstanceLock", make_lock)
    monkeypatch.setattr(command, "reject_shared_root_mode", lambda: None)
    monkeypatch.delenv("ALLOW_SHARED_ROOT", raising=False)

    observed = {}

    async def fail_rebuild(database_arg, _settings):
        observed["database"] = database_arg
        raise RuntimeError("private content and credentials")

    monkeypatch.setattr(command, "_rebuild", fail_rebuild)
    assert command.main([]) == 1

    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert "private content" not in captured.err
    assert "credentials" not in captured.err
    assert observed["database"] == database.resolve()
    assert locks[0].workspace == configured_workspace.resolve()
    assert locks[0].allow_memory_index_rebuild
    assert locks[0].checked_writers
    assert locks[0].released


def test_failed_rebuild_keeps_matching_fence_for_retry_and_success_clears_it(
    tmp_path, monkeypatch,
) -> None:
    from scripts import rebuild_memory_v2_index as command

    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    asyncio.run(SqliteMemoryV2Store(workspace / "memory-v2.db").initialize())

    class FakeSettings:
        workspace_dir = str(workspace)
        milvus_collection = "private_memory"
        knowledge_collection = "knowledge"
        milvus_uri = "https://milvus.example/private?credential=not-for-output"
        milvus_token = "secret-token-not-for-output"

    class FakeLock:
        def __init__(self, root, *, allow_memory_index_rebuild=False):
            assert allow_memory_index_rebuild is True
            self.root = root
            self.events = []

        def acquire(self):
            self.events.append("acquire")
            return self

        def assert_no_shared_root_writers(self):
            assert (self.root / MEMORY_INDEX_REBUILD_FENCE_FILENAME).is_file()
            self.events.append("scan")

        def release(self):
            self.events.append("release")

    locks = []

    def make_lock(root, *, allow_memory_index_rebuild=False):
        lock = FakeLock(root, allow_memory_index_rebuild=allow_memory_index_rebuild)
        locks.append(lock)
        return lock

    monkeypatch.setattr(command, "Settings", FakeSettings)
    monkeypatch.setattr(command, "InstanceLock", make_lock)
    monkeypatch.delenv("ALLOW_SHARED_ROOT", raising=False)
    attempts = 0

    async def rebuild(_database, _settings):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private provider failure")
        return 3

    monkeypatch.setattr(command, "_rebuild", rebuild)
    fence = workspace / MEMORY_INDEX_REBUILD_FENCE_FILENAME

    assert command.main([]) == 1
    marker = fence.read_text(encoding="utf-8")
    assert "milvus.example" not in marker
    assert "credential" not in marker
    assert "secret-token" not in marker
    assert locks[0].events == ["acquire", "scan", "release"]

    assert command.main([]) == 0
    assert not fence.exists()
    assert locks[1].events == ["acquire", "scan", "release"]
    assert attempts == 2


def test_interrupted_fence_write_does_not_block_workspace_startup(
    tmp_path, monkeypatch,
) -> None:
    from scripts import rebuild_memory_v2_index as command

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = SimpleNamespace(
        milvus_collection="private_memory",
        knowledge_collection="knowledge",
        milvus_uri="https://milvus.example",
    )
    fence = workspace / MEMORY_INDEX_REBUILD_FENCE_FILENAME
    original_write = command.os.write

    def interrupt_write(_fd, _content):
        raise SystemExit("simulated process interruption during fence write")

    monkeypatch.setattr(command.os, "write", interrupt_write)
    with pytest.raises(SystemExit, match="simulated process interruption"):
        command._publish_or_validate_rebuild_fence(workspace, settings)

    monkeypatch.setattr(command.os, "write", original_write)
    assert not fence.exists()
    lock = InstanceLock(workspace).acquire()
    lock.release()


def test_rebuild_recovers_fence_link_left_by_interrupted_publish(tmp_path) -> None:
    from scripts import rebuild_memory_v2_index as command

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = SimpleNamespace(
        milvus_collection="private_memory",
        knowledge_collection="knowledge",
        milvus_uri="https://milvus.example",
    )
    expected = command._rebuild_fence_identity(workspace, settings)
    fence = workspace / MEMORY_INDEX_REBUILD_FENCE_FILENAME
    temporary = workspace / f"{fence.name}.interrupted.tmp"
    temporary.write_text(json.dumps(expected, sort_keys=True) + "\n", encoding="utf-8")
    command.os.link(temporary, fence)

    assert command._publish_or_validate_rebuild_fence(workspace, settings) == expected
    assert not temporary.exists()
    assert command._read_rebuild_fence(fence) == expected


def test_rebuild_refuses_fence_for_another_target_without_overwriting_it(
    tmp_path, monkeypatch, capsys,
) -> None:
    from scripts import rebuild_memory_v2_index as command

    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    asyncio.run(SqliteMemoryV2Store(workspace / "memory-v2.db").initialize())

    class FakeSettings:
        workspace_dir = str(workspace)
        milvus_collection = "private_memory"
        knowledge_collection = "knowledge"
        milvus_uri = "https://milvus.example"

    class FakeLock:
        def __init__(self, _root, *, allow_memory_index_rebuild=False):
            assert allow_memory_index_rebuild
            self.root = _root

        def acquire(self):
            return self

        def assert_no_shared_root_writers(self):
            assert (self.root / MEMORY_INDEX_REBUILD_FENCE_FILENAME).is_file()

        def release(self):
            pass

    monkeypatch.setattr(command, "Settings", FakeSettings)
    monkeypatch.setattr(command, "InstanceLock", FakeLock)
    monkeypatch.delenv("ALLOW_SHARED_ROOT", raising=False)
    monkeypatch.setattr(
        command, "_rebuild", lambda *_args: pytest.fail("wrong target must not rebuild"),
    )

    fence = workspace / MEMORY_INDEX_REBUILD_FENCE_FILENAME
    expected = command._rebuild_fence_identity(workspace, FakeSettings)
    fence.write_text(json.dumps(expected, sort_keys=True) + "\n", encoding="utf-8")
    original = fence.read_text(encoding="utf-8")
    FakeSettings.milvus_uri = "https://different-milvus.example"
    assert command.main([]) == 1
    assert fence.read_text(encoding="utf-8") == original
    assert "Memory V2 index rebuild refused" in capsys.readouterr().err
