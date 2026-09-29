from __future__ import annotations

import asyncio
import json
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
