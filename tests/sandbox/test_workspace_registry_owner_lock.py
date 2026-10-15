"""Concurrency contract for Docker workspace restore and hard deletion."""

from __future__ import annotations

import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from threading import Event
from types import SimpleNamespace

import pytest

from agent_harness.sandbox.capabilities import SandboxCapabilities
from agent_harness.sandbox.registry import WorkspaceBindingError, WorkspaceRegistry


class _FakeDockerSandbox:
    def __init__(
        self,
        *,
        container_name: str | None = None,
        volume_name: str | None = None,
    ) -> None:
        self._container_name = container_name
        self._volume_name = volume_name

    @property
    def workspace_root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    def ensure_started(self) -> None:
        pass

    def workspace_volume_exists(self) -> bool:
        return True

    def copy_in(self, _source: Path, _destination: str) -> None:
        pass

    def write_text(self, _path: str, _content: str) -> None:
        pass

    def read_text(self, _path: str) -> str:
        return ""

    def delete(self) -> None:
        pass


class _ObservedLock:
    def __init__(
        self,
        lock,
        *,
        contended: Event | None = None,
        acquired: Event | None = None,
    ) -> None:
        self._lock = lock
        self._contended = contended
        self._acquired = acquired

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            if self._contended is not None:
                self._contended.set()
            self._lock.acquire()
        elif self._acquired is not None:
            self._acquired.set()
        return self._lock

    def __exit__(self, *_exc_info) -> None:
        self._lock.release()


def _docker_registries(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *session_ids: str,
) -> tuple[WorkspaceRegistry, WorkspaceRegistry]:
    registry_module = importlib.import_module("agent_harness.sandbox.registry")
    docker_module = importlib.import_module("agent_harness.sandbox.docker")
    monkeypatch.setattr(
        registry_module,
        "probe_docker_capabilities",
        lambda: SandboxCapabilities(backend="docker", available=True),
    )
    monkeypatch.setattr(docker_module, "DockerSandbox", _FakeDockerSandbox)

    owner_registry = WorkspaceRegistry(root=root, backend="docker")
    for session_id in session_ids:
        owner_registry.create(session_id, backend="docker")
    return owner_registry, WorkspaceRegistry(root=root, backend="docker")


def _observe_registry_lock(
    registry: WorkspaceRegistry,
    monkeypatch: pytest.MonkeyPatch,
    *,
    contended: Event | None = None,
    acquired: Event | None = None,
) -> None:
    original_workspace_lock = registry._workspace_lock

    def observe(owner_session_id: str) -> _ObservedLock:
        return _ObservedLock(
            original_workspace_lock(owner_session_id),
            contended=contended,
            acquired=acquired,
        )

    monkeypatch.setattr(registry, "_workspace_lock", observe)


def test_hard_delete_without_mapping_blocks_direct_owner_create_until_discard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_module = importlib.import_module("agent_harness.sandbox.registry")
    docker_module = importlib.import_module("agent_harness.sandbox.docker")
    monkeypatch.setattr(
        registry_module,
        "probe_docker_capabilities",
        lambda: SandboxCapabilities(backend="docker", available=True),
    )
    monkeypatch.setattr(docker_module, "DockerSandbox", _FakeDockerSandbox)
    registry = WorkspaceRegistry(root=tmp_path, backend="docker")
    session_id = "direct-create-during-delete"

    registry.cleanup_docker_workspace_resources(session_id)

    mapping = registry._read_mapping(session_id)
    assert mapping is not None
    assert mapping["workspace_delete_in_progress"] is True
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        registry.get(session_id)
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        registry.create(session_id, backend="docker")

    registry.discard_session_artifacts(session_id)
    assert registry.create(session_id, backend="docker") is not None


def test_direct_owner_create_finishes_before_hard_delete_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_registry, delete_registry = _docker_registries(tmp_path, monkeypatch)
    ensure_started = Event()
    allow_startup = Event()
    delete_lock_contended = Event()

    def pause_startup(_self: _FakeDockerSandbox) -> None:
        ensure_started.set()
        assert allow_startup.wait(timeout=5)

    monkeypatch.setattr(_FakeDockerSandbox, "ensure_started", pause_startup)
    _observe_registry_lock(
        delete_registry, monkeypatch, contended=delete_lock_contended
    )
    session_id = "direct-create-before-delete"

    with ThreadPoolExecutor(max_workers=2) as pool:
        create = pool.submit(owner_registry.create, session_id, backend="docker")
        assert ensure_started.wait(timeout=5)
        delete = pool.submit(
            delete_registry.cleanup_docker_workspace_resources, session_id
        )
        try:
            assert delete_lock_contended.wait(timeout=5)
            assert not delete.done()
        finally:
            allow_startup.set()

        created_sandbox = create.result(timeout=5)
        delete.result(timeout=5)

    mapping = owner_registry._read_mapping(session_id)
    assert mapping is not None
    assert mapping["workspace_delete_in_progress"] is True
    assert created_sandbox._container_name == f"agent-harness-{session_id}"
    owner_registry.discard_session_artifacts(session_id)


def test_local_direct_owner_create_is_rejected_during_hard_delete_window(
    tmp_path: Path,
) -> None:
    session_id = "local-create-during-delete"
    owner_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    delete_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    owner_registry.create(session_id)

    delete_registry.cleanup_docker_workspace_resources(session_id)

    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        owner_registry.create(session_id)
    assert owner_registry._read_mapping(session_id)["workspace_delete_in_progress"] is True

    delete_registry.discard_session_artifacts(session_id)


def test_legacy_local_mapping_without_backend_is_fenced_during_hard_delete(
    tmp_path: Path,
) -> None:
    session_id = "legacy-local-create-during-delete"
    registry = WorkspaceRegistry(root=tmp_path, backend="local")
    workspace_root = tmp_path / "user-workspace"
    workspace_root.mkdir()
    registry._write_mapping(session_id, {
        "session_id": session_id,
        "workspace_root": str(workspace_root),
    })

    registry.cleanup_docker_workspace_resources(session_id)

    mapping = registry._read_mapping(session_id)
    assert mapping is not None
    assert mapping["workspace_delete_in_progress"] is True
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        registry.get(session_id)
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        registry.create(session_id)
    assert workspace_root.is_dir()

    registry.discard_session_artifacts(session_id)


def test_unknown_owner_backend_is_marked_and_evicted_during_hard_delete(
    tmp_path: Path,
) -> None:
    session_id = "unknown-backend-owner"
    registry = WorkspaceRegistry(root=tmp_path, backend="local")
    workspace_root = tmp_path / "unknown-backend-workspace"
    workspace_root.mkdir()
    registry._write_mapping(session_id, {
        "session_id": session_id,
        "backend": "future_backend",
        "workspace_root": str(workspace_root),
    })
    cached_sandbox = _FakeDockerSandbox()
    registry._cache[session_id] = cached_sandbox

    registry.cleanup_docker_workspace_resources(session_id)

    mapping = registry._read_mapping(session_id)
    assert mapping is not None
    assert mapping["workspace_delete_in_progress"] is True
    assert session_id not in registry._cache
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        WorkspaceRegistry(root=tmp_path, backend="local").get(session_id)
    assert workspace_root.is_dir()

    registry.discard_session_artifacts(session_id)


def test_local_restore_waits_for_hard_delete_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "local-restore-during-delete"
    owner_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    restore_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    owner_registry.create(session_id)
    restore_entered = Event()
    allow_restore = Event()
    delete_lock_contended = Event()

    class _PausedSandbox:
        def ensure_started(self) -> None:
            restore_entered.set()
            assert allow_restore.wait(timeout=5)

    monkeypatch.setattr(
        restore_registry,
        "_instantiate_sandbox",
        lambda _mapping: _PausedSandbox(),
    )
    delete_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    _observe_registry_lock(
        delete_registry,
        monkeypatch,
        contended=delete_lock_contended,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        restore = pool.submit(restore_registry.get, session_id)
        assert restore_entered.wait(timeout=5)
        delete = pool.submit(
            delete_registry.cleanup_docker_workspace_resources, session_id
        )
        try:
            assert delete_lock_contended.wait(timeout=5)
            assert not delete.done()
        finally:
            allow_restore.set()

        assert restore.result(timeout=5) is not None
        delete.result(timeout=5)

    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        restore_registry.get(session_id)
    assert owner_registry._read_mapping(session_id)["workspace_delete_in_progress"] is True
    delete_registry.discard_session_artifacts(session_id)


def test_restore_waits_for_hard_delete_for_the_same_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "same-owner"
    owner_registry, restore_registry = _docker_registries(
        tmp_path, monkeypatch, session_id
    )
    alias_id = "same-owner-alias"
    owner_registry.bind_alias(alias_id, session_id)
    restore_entered = Event()
    allow_restore = Event()
    delete_entered = Event()

    def block_restore(self: _FakeDockerSandbox) -> None:
        restore_entered.set()
        assert allow_restore.wait(timeout=5)

    def record_delete(self: _FakeDockerSandbox) -> None:
        delete_entered.set()

    monkeypatch.setattr(_FakeDockerSandbox, "ensure_started", block_restore)
    monkeypatch.setattr(_FakeDockerSandbox, "delete", record_delete)
    delete_lock_contended = Event()
    _observe_registry_lock(
        owner_registry, monkeypatch, contended=delete_lock_contended
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        restore = pool.submit(restore_registry.get, alias_id)
        assert restore_entered.wait(timeout=5)

        def delete_workspace() -> None:
            owner_registry.cleanup_docker_workspace_resources(session_id)

        delete = pool.submit(delete_workspace)
        try:
            assert delete_lock_contended.wait(timeout=5)
            assert not delete_entered.is_set()
        finally:
            allow_restore.set()

        assert restore.result(timeout=5) is not None
        delete.result(timeout=5)

    assert delete_entered.is_set()


def test_hard_delete_waits_for_restore_for_the_same_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "same-owner"
    owner_registry, restore_registry = _docker_registries(
        tmp_path, monkeypatch, session_id
    )
    delete_entered = Event()
    allow_delete = Event()
    restore_entered = Event()

    def block_delete(self: _FakeDockerSandbox) -> None:
        delete_entered.set()
        assert allow_delete.wait(timeout=5)

    def record_restore(self: _FakeDockerSandbox) -> None:
        restore_entered.set()

    monkeypatch.setattr(_FakeDockerSandbox, "delete", block_delete)
    monkeypatch.setattr(_FakeDockerSandbox, "ensure_started", record_restore)
    restore_lock_contended = Event()
    _observe_registry_lock(
        restore_registry, monkeypatch, contended=restore_lock_contended
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        delete = pool.submit(
            owner_registry.cleanup_docker_workspace_resources, session_id
        )
        assert delete_entered.wait(timeout=5)
        restore = pool.submit(restore_registry.get, session_id)
        try:
            assert restore_lock_contended.wait(timeout=5)
            assert not restore_entered.is_set()
        finally:
            allow_delete.set()

        delete.result(timeout=5)
        with pytest.raises(WorkspaceBindingError, match="hard-delete"):
            restore.result(timeout=5)
        assert not restore_entered.is_set()


def test_different_owners_do_not_share_the_workspace_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_a = "owner-a"
    owner_b = "owner-b"
    owner_registry, restore_registry = _docker_registries(
        tmp_path, monkeypatch, owner_a, owner_b
    )
    restore_entered = Event()
    allow_restore = Event()
    delete_b_entered = Event()

    def block_restore(self: _FakeDockerSandbox) -> None:
        if self._container_name == f"agent-harness-{owner_a}":
            restore_entered.set()
            assert allow_restore.wait(timeout=5)

    def record_delete(self: _FakeDockerSandbox) -> None:
        if self._container_name == f"agent-harness-{owner_b}":
            delete_b_entered.set()

    monkeypatch.setattr(_FakeDockerSandbox, "ensure_started", block_restore)
    monkeypatch.setattr(_FakeDockerSandbox, "delete", record_delete)
    unrelated_lock_acquired = Event()
    _observe_registry_lock(
        owner_registry, monkeypatch, acquired=unrelated_lock_acquired
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        restore = pool.submit(restore_registry.get, owner_a)
        assert restore_entered.wait(timeout=5)
        delete = pool.submit(
            owner_registry.cleanup_docker_workspace_resources, owner_b
        )
        try:
            assert unrelated_lock_acquired.wait(timeout=5)
            assert delete_b_entered.wait(timeout=5)
        finally:
            allow_restore.set()

        assert restore.result(timeout=5) is not None
        delete.result(timeout=5)


def test_hard_delete_evicts_owner_and_alias_cache_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "same-owner"
    owner_registry, restore_registry = _docker_registries(
        tmp_path, monkeypatch, session_id
    )
    deleted_sandbox = owner_registry.create(session_id, backend="docker")
    owner_registry.bind_alias("child-session", session_id)
    restore_registry.get("child-session")

    owner_registry.cleanup_docker_workspace_resources(session_id)

    assert all(
        cached_sandbox is not deleted_sandbox
        for cached_sandbox in owner_registry._cache.values()
    )
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        restore_registry.get("child-session")
    assert all(
        cached_sandbox is not deleted_sandbox
        for cached_sandbox in restore_registry._cache.values()
    )

    owner_registry.discard_session_artifacts(session_id)
    with pytest.raises(WorkspaceBindingError, match="missing owner"):
        owner_registry.get("child-session")


def test_delete_marker_survives_cleanup_failure_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "retry-owner"
    owner_registry, restore_registry = _docker_registries(
        tmp_path, monkeypatch, session_id
    )
    delete_calls = 0

    def fail_once_then_delete(self: _FakeDockerSandbox) -> None:
        nonlocal delete_calls
        delete_calls += 1
        if delete_calls == 1:
            raise RuntimeError("docker temporarily unavailable")

    monkeypatch.setattr(_FakeDockerSandbox, "delete", fail_once_then_delete)

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        owner_registry.cleanup_docker_workspace_resources(session_id)

    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        restore_registry.get(session_id)
    fresh_registry = WorkspaceRegistry(root=tmp_path, backend="docker")
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        fresh_registry.get(session_id)

    owner_registry.cleanup_docker_workspace_resources(session_id)
    assert delete_calls == 2
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        restore_registry.get(session_id)

    owner_registry.discard_session_artifacts(session_id)
    with pytest.raises(KeyError):
        restore_registry.get(session_id)


def test_hard_delete_blocks_legacy_psec_migration_without_deleting_host_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "legacy-owner"
    registry = WorkspaceRegistry(root=tmp_path, backend="docker")
    host_workspace = tmp_path / "legacy-workspace"
    host_workspace.mkdir()
    canary = host_workspace / "user-data.txt"
    canary.write_text("preserve", encoding="utf-8")
    registry._write_mapping(session_id, {
        "session_id": session_id,
        "backend": "windows_psec",
        "workspace_root": str(host_workspace),
    })
    migration_calls = 0

    def record_migration(_self, _session_id: str, _mapping: dict) -> _FakeDockerSandbox:
        nonlocal migration_calls
        migration_calls += 1
        return _FakeDockerSandbox(
            container_name=f"agent-harness-{session_id}",
            volume_name=f"agent-harness-{session_id}",
        )

    monkeypatch.setattr(WorkspaceRegistry, "_migrate_legacy_mapping", record_migration)

    registry.cleanup_docker_workspace_resources(session_id)

    persisted_mapping = registry._read_mapping(session_id)
    assert persisted_mapping is not None
    assert persisted_mapping["workspace_delete_in_progress"] is True
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        WorkspaceRegistry(root=tmp_path, backend="docker").get(session_id)
    assert migration_calls == 0
    assert canary.read_text(encoding="utf-8") == "preserve"


def test_hard_delete_waits_for_legacy_psec_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_registry, restore_registry = _docker_registries(tmp_path, monkeypatch)
    session_id = "legacy-owner"
    host_workspace = tmp_path / "legacy-workspace"
    host_workspace.mkdir()
    canary = host_workspace / "user-data.txt"
    canary.write_text("preserve", encoding="utf-8")
    owner_registry._write_mapping(session_id, {
        "session_id": session_id,
        "backend": "windows_psec",
        "workspace_root": str(host_workspace),
    })
    migration_entered = Event()
    allow_migration = Event()
    delete_entered = Event()

    def migrate(self, _session_id: str, mapping: dict) -> _FakeDockerSandbox:
        migration_entered.set()
        assert allow_migration.wait(timeout=5)
        migrated = {
            **mapping,
            "backend": "docker",
            "container_name": f"agent-harness-{session_id}",
            "volume_name": f"agent-harness-{session_id}",
        }
        self._write_mapping(session_id, migrated)
        return _FakeDockerSandbox(
            container_name=migrated["container_name"],
            volume_name=migrated["volume_name"],
        )

    monkeypatch.setattr(WorkspaceRegistry, "_migrate_legacy_mapping", migrate)
    monkeypatch.setattr(
        _FakeDockerSandbox,
        "delete",
        lambda _self: delete_entered.set(),
    )
    delete_lock_contended = Event()
    _observe_registry_lock(
        owner_registry, monkeypatch, contended=delete_lock_contended
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        restore = pool.submit(restore_registry.get, session_id)
        assert migration_entered.wait(timeout=5)
        delete = pool.submit(
            owner_registry.cleanup_docker_workspace_resources, session_id
        )
        try:
            assert delete_lock_contended.wait(timeout=5)
            assert not delete_entered.is_set()
        finally:
            allow_migration.set()

        assert restore.result(timeout=5) is not None
        delete.result(timeout=5)

    assert delete_entered.is_set()
    persisted_mapping = owner_registry._read_mapping(session_id)
    assert persisted_mapping is not None
    assert persisted_mapping["workspace_delete_in_progress"] is True
    assert canary.read_text(encoding="utf-8") == "preserve"


def test_stale_docker_sandbox_cannot_recreate_volume_after_delete_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_harness.sandbox.docker import DockerSandbox

    class NotFound(Exception):
        pass

    class Containers:
        run_calls = 0

        def get(self, _name: str) -> None:
            raise NotFound

        def run(self, *_args, **_kwargs) -> object:
            self.run_calls += 1
            return object()

    containers = Containers()
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(errors=SimpleNamespace(NotFound=NotFound)),
    )
    registry = WorkspaceRegistry(root=tmp_path, backend="docker")
    session_id = "stale-sandbox-owner"
    mapping = {
        "session_id": session_id,
        "backend": "docker",
        "workspace_root": str(tmp_path / "workspace"),
        "container_name": f"agent-harness-{session_id}",
        "volume_name": f"agent-harness-{session_id}",
    }
    registry._write_mapping(session_id, mapping)
    sandbox = object.__new__(DockerSandbox)
    sandbox._container = None
    sandbox._client = SimpleNamespace(containers=containers)
    sandbox._container_name = mapping["container_name"]
    sandbox._volume_name = mapping["volume_name"]
    sandbox._image = "test-image"
    registry._attach_workspace_restore_guard(sandbox, session_id)

    sandbox.ensure_started()
    assert containers.run_calls == 1

    mapping["workspace_delete_in_progress"] = True
    registry._write_mapping(session_id, mapping)
    with pytest.raises(WorkspaceBindingError, match="hard-delete"):
        sandbox.ensure_started()
    assert containers.run_calls == 1
