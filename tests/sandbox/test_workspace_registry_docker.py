"""WorkspaceRegistry Docker 后端集成测试。

确定性命名 + workspace 恢复（volume 持久性）验证。
默认 skip（需要 Docker daemon）。
"""

from __future__ import annotations

import importlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _docker_available() -> bool:
    try:
        docker = importlib.import_module("docker")
        client = docker.from_env(use_context=False)
        client.ping()
        client.close()
    except Exception:  # noqa: BLE001 — probe
        return False
    return True


docker_required = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker SDK or daemon is unavailable",
)

SESSION_ID = "test-registry-docker-session"
CHILD_SESSION_ID = "test-registry-docker-child"


@pytest.fixture
def registry(tmp_path: Path):
    from agent_harness.sandbox.registry import WorkspaceRegistry

    reg = WorkspaceRegistry(root=tmp_path, backend="docker")
    yield reg
    # 清理：删除测试容器和 volume
    try:
        reg.delete(SESSION_ID)
    except Exception:  # noqa: BLE001, S110 — best-effort cleanup
        pass
    try:
        import docker

        client = docker.from_env(use_context=False)
        for vol_suffix in [SESSION_ID]:
            try:
                client.volumes.get(f"agent-harness-{vol_suffix}").remove(force=True)
            except Exception:  # noqa: BLE001, S110
                pass
        client.close()
    except Exception:  # noqa: BLE001, S110
        pass


@docker_required
class TestDockerRegistryDeterministicNaming:
    def test_container_name_is_deterministic(self, registry, tmp_path: Path):
        """同一 session_id 产生的容器名确定性。"""
        import json

        _sandbox = registry.create(SESSION_ID)

        mapping_file = tmp_path / "workspaces" / f"{SESSION_ID}.json"
        mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
        assert mapping["container_name"] == f"agent-harness-{SESSION_ID}"
        assert mapping["volume_name"] == f"agent-harness-{SESSION_ID}"

    @pytest.mark.skipif(os.name != "nt", reason="Windows junction import is Windows-only")
    @pytest.mark.parametrize("legacy_mapping", [False, True], ids=["new", "psec-migration"])
    def test_private_import_does_not_follow_external_directory_junction(
        self, registry, tmp_path: Path, legacy_mapping: bool,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Neither new workspaces nor PSEC migration may import outside a junction."""
        import subprocess

        from agent_harness.sandbox.docker import DockerSandbox

        copy_in = DockerSandbox.copy_in
        volume_checks: list[tuple[int, str]] = []

        def check_volume_after_rejected_import(sandbox, host_path, workspace_path):
            try:
                return copy_in(sandbox, host_path, workspace_path)
            except PermissionError:
                result = sandbox.exec(
                    "python -c \"from pathlib import Path; "
                    "print(Path('/workspace/external/secret.txt').exists())\""
                )
                volume_checks.append((result.exit_code, result.stdout.strip()))
                raise

        monkeypatch.setattr(DockerSandbox, "copy_in", check_volume_after_rejected_import)

        source = tmp_path / "project"
        source.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("external secret", encoding="utf-8")
        junction = source / "external"
        result = subprocess.run(
            f'mklink /J "{junction}" "{outside}"',
            capture_output=True,
            text=True,
            check=False,
            shell=True,
        )
        if result.returncode != 0:
            pytest.skip(
                f"Windows junction creation unavailable: {result.stdout}{result.stderr}"
            )

        if legacy_mapping:
            from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

            identity = WindowsPsecSandbox.validate_workspace_identity(source)
            registry._write_mapping(SESSION_ID, {
                "session_id": SESSION_ID,
                "backend": "windows_psec",
                "workspace_root": str(source),
                "workspace_identity": identity,
                "container_name": None,
                "volume_name": None,
                "created_at": "legacy-created-at",
            })
            with pytest.raises(PermissionError, match="reparse"):
                registry.get(SESSION_ID)
            mapping = registry._read_mapping(SESSION_ID)
            assert mapping is not None
            assert mapping["backend"] == "windows_psec"
        else:
            with pytest.raises(PermissionError, match="reparse"):
                registry.create(SESSION_ID, workspace_root=source, backend="docker")
            assert registry._read_mapping(SESSION_ID) is None
        assert volume_checks == [(0, "False")]
        assert (outside / "secret.txt").read_text(encoding="utf-8") == "external secret"

    def test_create_imports_project_as_private_volume_copy(
        self, registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Importing a project must sever NTFS hard-link identity before execution."""
        from agent_harness.sandbox.docker import DockerSandbox

        source = tmp_path / "project"
        source.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("host-original", encoding="utf-8")
        try:
            os.link(outside, source / "outside-alias.txt")
        except OSError as exc:
            pytest.skip(f"host hard-link creation unavailable: {exc}")

        internal = source / "internal-a.txt"
        internal.write_text("internal-original", encoding="utf-8")
        os.link(internal, source / "internal-b.txt")

        entering_import = threading.Event()
        release_import = threading.Event()
        copy_in = DockerSandbox.copy_in

        def paused_copy_in(sandbox, host_path, workspace_path):
            if Path(host_path) == source:
                entering_import.set()
                if not release_import.wait(10):
                    raise TimeoutError("test did not release the workspace import")
            return copy_in(sandbox, host_path, workspace_path)

        monkeypatch.setattr(DockerSandbox, "copy_in", paused_copy_in)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                registry.create, SESSION_ID, workspace_root=source, backend="docker",
            )
            assert entering_import.wait(30), "workspace import did not reach the barrier"
            try:
                os.link(outside, source / "racing-alias.txt")
            except OSError as exc:
                release_import.set()
                future.result(timeout=30)
                pytest.skip(f"host hard-link creation unavailable: {exc}")
            release_import.set()
            sandbox = future.result(timeout=30)

        result = sandbox.exec(
            "python -c \"from pathlib import Path; "
            "Path('outside-alias.txt').write_text('guest-write'); "
            "Path('racing-alias.txt').write_text('race-write'); "
            "Path('internal-b.txt').write_text('internal-update'); "
            "print(Path('internal-a.txt').samefile(Path('internal-b.txt')))\""
        )

        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == "True"
        assert sandbox.read_text("outside-alias.txt") == "race-write"
        assert sandbox.read_text("racing-alias.txt") == "race-write"
        assert outside.read_text(encoding="utf-8") == "host-original"
        assert (source / "outside-alias.txt").read_text(encoding="utf-8") == "host-original"
        assert internal.read_text(encoding="utf-8") == "internal-original"

        workspace_mounts = [
            mount for mount in sandbox._container.attrs["Mounts"]
            if mount.get("Destination") == "/workspace"
        ]
        assert len(workspace_mounts) == 1
        assert workspace_mounts[0]["Type"] == "volume"
        assert workspace_mounts[0]["RW"] is True
        assert not any(
            mount.get("Type") == "bind" and mount.get("RW")
            for mount in sandbox._container.attrs["Mounts"]
        )

    def test_get_migrates_legacy_psec_mapping_after_private_import(
        self, registry, tmp_path: Path,
    ):
        from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

        source = tmp_path / "legacy-project"
        source.mkdir()
        outside = tmp_path / "legacy-outside.txt"
        outside.write_text("host-original", encoding="utf-8")
        try:
            os.link(outside, source / "outside-alias.txt")
        except OSError as exc:
            pytest.skip(f"host hard-link creation unavailable: {exc}")

        identity = WindowsPsecSandbox.validate_workspace_identity(source)
        legacy_mapping = {
            "session_id": SESSION_ID,
            "backend": "windows_psec",
            "workspace_root": str(source),
            "workspace_identity": identity,
            "container_name": None,
            "volume_name": None,
            "created_at": "legacy-created-at",
        }
        registry._write_mapping(SESSION_ID, legacy_mapping)

        sandbox = registry.get(SESSION_ID)

        mapping = registry._read_mapping(SESSION_ID)
        assert mapping is not None
        assert mapping["backend"] == "docker"
        assert mapping["workspace_root"] == str(source)
        assert mapping["container_name"] == f"agent-harness-{SESSION_ID}"
        assert mapping["volume_name"] == f"agent-harness-{SESSION_ID}"
        assert "workspace_identity" not in mapping
        result = sandbox.exec(
            "python -c \"from pathlib import Path; "
            "Path('outside-alias.txt').write_text('guest-write')\""
        )
        assert result.exit_code == 0, result.stderr
        assert sandbox.read_text("outside-alias.txt") == "guest-write"
        assert outside.read_text(encoding="utf-8") == "host-original"
        assert (source / "outside-alias.txt").read_text(encoding="utf-8") == "host-original"

    def test_failed_legacy_import_keeps_psec_mapping_and_source_retryable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import docker

        from agent_harness.sandbox.docker import DockerSandbox
        from agent_harness.sandbox.registry import WorkspaceRegistry
        from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

        source = tmp_path / "legacy-project"
        source.mkdir()
        (source / "project.txt").write_text("legacy source", encoding="utf-8")
        outside = tmp_path / "legacy-outside.txt"
        outside.write_text("host-original", encoding="utf-8")
        try:
            os.link(outside, source / "outside-alias.txt")
        except OSError as exc:
            pytest.skip(f"host hard-link creation unavailable: {exc}")

        docker_client = docker.from_env(use_context=False)
        try:
            docker_client.ping()
            docker_client.images.get("python:3-slim")
        except Exception as exc:  # noqa: BLE001 — environment-dependent integration test
            docker_client.close()
            pytest.skip(f"Docker daemon or python:3-slim image is unavailable: {exc}")

        registry = WorkspaceRegistry(root=tmp_path / "registry")
        identity = WindowsPsecSandbox.validate_workspace_identity(source)
        legacy_mapping = {
            "session_id": SESSION_ID,
            "backend": "windows_psec",
            "workspace_root": str(source),
            "workspace_identity": identity,
            "container_name": None,
            "volume_name": None,
            "created_at": "legacy-created-at",
        }
        registry._write_mapping(SESSION_ID, legacy_mapping)
        mapping_path = registry._mapping_path(SESSION_ID)
        original_mapping_bytes = mapping_path.read_bytes()
        original_copy_in = DockerSandbox.copy_in

        def copy_then_fail(sandbox, host_path, workspace_path):
            original_copy_in(sandbox, host_path, workspace_path)
            raise RuntimeError("injected legacy import failure")

        monkeypatch.setattr(DockerSandbox, "copy_in", copy_then_fail)
        try:
            with pytest.raises(RuntimeError, match="injected legacy import failure"):
                registry.get(SESSION_ID)

            assert mapping_path.read_bytes() == original_mapping_bytes
            assert registry._read_mapping(SESSION_ID)["backend"] == "windows_psec"
            assert (source / "project.txt").read_text(encoding="utf-8") == "legacy source"
            assert outside.read_text(encoding="utf-8") == "host-original"
            assert (source / "outside-alias.txt").read_text(encoding="utf-8") == "host-original"
            with pytest.raises(docker.errors.NotFound):
                docker_client.containers.get(f"agent-harness-{SESSION_ID}")
            with pytest.raises(docker.errors.NotFound):
                docker_client.volumes.get(f"agent-harness-{SESSION_ID}")

            monkeypatch.setattr(DockerSandbox, "copy_in", original_copy_in)
            sandbox = registry.get(SESSION_ID)
            mapping = registry._read_mapping(SESSION_ID)
            assert mapping is not None and mapping["backend"] == "docker"
            assert sandbox.read_text("project.txt") == "legacy source"
            assert outside.read_text(encoding="utf-8") == "host-original"
        finally:
            monkeypatch.setattr(DockerSandbox, "copy_in", original_copy_in)
            mapping = registry._read_mapping(SESSION_ID)
            if mapping is not None and mapping.get("backend") == "docker":
                registry.delete(SESSION_ID)
            else:
                mapping_path.unlink(missing_ok=True)
            docker_client.close()

    def test_export_materializes_internal_hardlinks_and_refuses_existing_target(
        self, registry, tmp_path: Path,
    ):
        source = tmp_path / "export-source"
        source.mkdir()
        first = source / "first.txt"
        first.write_text("initial", encoding="utf-8")
        try:
            os.link(first, source / "second.txt")
        except OSError as exc:
            pytest.skip(f"host hard-link creation unavailable: {exc}")
        sandbox = registry.create(SESSION_ID, workspace_root=source, backend="docker")

        destination = tmp_path / "exported"
        sandbox.copy_out(destination)

        exported_first = destination / "first.txt"
        exported_second = destination / "second.txt"
        assert exported_first.read_text(encoding="utf-8") == "initial"
        assert exported_second.read_text(encoding="utf-8") == "initial"
        assert not exported_first.samefile(exported_second)
        with pytest.raises(FileExistsError):
            sandbox.copy_out(destination)
        assert exported_first.read_text(encoding="utf-8") == "initial"

    def test_export_rejects_symlinks_and_removes_partial_destination(
        self, registry, tmp_path: Path,
    ):
        source = tmp_path / "symlink-source"
        source.mkdir()
        (source / "safe.txt").write_text("safe", encoding="utf-8")
        sandbox = registry.create(
            SESSION_ID, workspace_root=source, backend="docker",
        )
        result = sandbox.exec("ln -s /tmp/private-canary unsafe-link")
        assert result.exit_code == 0, result.stderr

        destination = tmp_path / "rejected-export"
        with pytest.raises(ValueError, match="unsafe entry"):
            sandbox.copy_out(destination)
        assert not destination.exists()


@docker_required
class TestDockerWorkspaceRecovery:
    def test_workspace_files_persist_across_restart(self, registry):
        """workspace 恢复：create → write → 新 Registry get → 文件还在。

        Docker volume 持久：容器停了但 volume 在，resume 时重启容器即可。
        """
        sandbox1 = registry.create(SESSION_ID)
        sandbox1.write_text("persistent.py", "print('survived restart')")

        # 模拟进程重启：stop 容器（不删 volume），新 Registry get 重启容器
        registry.stop(SESSION_ID)

        from agent_harness.sandbox.registry import WorkspaceRegistry

        new_registry = WorkspaceRegistry(
            root=registry._root, backend="docker"
        )
        sandbox2 = new_registry.get(SESSION_ID)

        content = sandbox2.read_text("persistent.py")
        assert content == "print('survived restart')"

    def test_child_alias_ownership_preserves_then_cleans_parent_workspace(self, registry):
        """Deleting a delegated alias is non-owning; deleting its owner removes it."""
        parent = registry.create(SESSION_ID)
        parent.write_text("owner-marker.txt", "parent-owned")
        registry.bind_alias(CHILD_SESSION_ID, SESSION_ID)

        registry.delete(CHILD_SESSION_ID)
        assert registry.exists(SESSION_ID)
        assert registry.get(SESSION_ID).read_text("owner-marker.txt") == "parent-owned"

        registry.bind_alias(CHILD_SESSION_ID, SESSION_ID)
        registry.delete(SESSION_ID)
        assert not registry.exists(SESSION_ID)
        assert not registry.exists(CHILD_SESSION_ID)
