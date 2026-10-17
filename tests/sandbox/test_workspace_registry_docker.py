"""WorkspaceRegistry Docker 后端集成测试。

确定性命名 + workspace 恢复（volume 持久性）验证。
默认 skip（需要 Docker daemon）。
"""

from __future__ import annotations

import importlib
import os
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
_WIN32_OPEN_EXISTING = 3
_WIN32_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WIN32_FSCTL_SET_REPARSE_POINT = 0x000900A4
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_REPARSE_DATA_BUFFER_HEADER_SIZE = 8


def _open_directory_write_attributes(path: Path):
    import ctypes
    from ctypes import wintypes

    from agent_harness.sandbox import windows_psec

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(
        str(path),
        windows_psec._FILE_WRITE_ATTRIBUTES,
        (windows_psec._FILE_SHARE_READ | windows_psec._FILE_SHARE_WRITE
         | windows_psec._FILE_SHARE_DELETE),
        None,
        _WIN32_OPEN_EXISTING,
        (_WIN32_FILE_FLAG_BACKUP_SEMANTICS
         | windows_psec._FILE_OPEN_REPARSE_POINT),
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, handle


def _set_directory_junction(kernel32, handle, target: Path) -> None:
    import ctypes
    import struct

    substitute = "\\??\\" + str(target)
    print_name = str(target)
    substitute_bytes = substitute.encode("utf-16-le")
    print_bytes = print_name.encode("utf-16-le")
    path_buffer = (
        substitute_bytes + b"\x00\x00" + print_bytes + b"\x00\x00"
    )
    data = struct.pack(
        "<IHHHHHH", _IO_REPARSE_TAG_MOUNT_POINT,
        _REPARSE_DATA_BUFFER_HEADER_SIZE + len(path_buffer), 0,
        0, len(substitute_bytes), len(substitute_bytes) + 2, len(print_bytes),
    ) + path_buffer
    payload = ctypes.create_string_buffer(data)
    returned = ctypes.c_ulong()
    if not kernel32.DeviceIoControl(
        handle, _WIN32_FSCTL_SET_REPARSE_POINT, payload, len(data), None, 0,
        ctypes.byref(returned), None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())



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

@docker_required
class TestDockerRegistryJunctionImport:
    @pytest.mark.skipif(os.name != "nt", reason="Windows handle import is Windows-only")
    @pytest.mark.parametrize("legacy_mapping", [False, True], ids=["new", "psec-migration"])
    def test_private_import_preserves_regular_files_and_directories(
        self, registry, tmp_path: Path, legacy_mapping: bool,
    ):
        from agent_harness.sandbox.docker import DockerSandbox
        from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

        source = tmp_path / "project"
        source.mkdir()
        (source / "root.txt").write_text("root content", encoding="utf-8")
        nested = source / "nested"
        nested.mkdir()
        (nested / "child.txt").write_text("nested content", encoding="utf-8")

        if legacy_mapping:
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
            sandbox = registry.get(SESSION_ID)
            mapping = registry._read_mapping(SESSION_ID)
            assert mapping is not None and mapping["backend"] == "docker"
        else:
            sandbox = registry.create(
                SESSION_ID, workspace_root=source, backend="docker",
            )

        assert isinstance(sandbox, DockerSandbox)
        assert sandbox.read_text("root.txt") == "root content"
        assert sandbox.read_text("nested/child.txt") == "nested content"

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

    @pytest.mark.skipif(os.name != "nt", reason="Windows junction import is Windows-only")
    @pytest.mark.parametrize("legacy_mapping", [False, True], ids=["new", "psec-migration"])
    def test_private_import_does_not_follow_junction_installed_after_validation(
        self, registry, tmp_path: Path, legacy_mapping: bool,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """An already-open directory handle cannot redirect archive reads outside."""
        import contextlib

        from agent_harness.sandbox import windows_psec
        from agent_harness.sandbox.docker import DockerSandbox
        from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

        source = tmp_path / "project"
        source.mkdir()
        slot = source / "slot"
        slot.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("external secret", encoding="utf-8")
        kernel32, junction_handle = _open_directory_write_attributes(slot)

        original_pin = windows_psec._pin_host_path_tree_for_archive

        @contextlib.contextmanager
        def install_junction_after_validation(host_path):
            with original_pin(host_path) as pinned:
                _set_directory_junction(kernel32, junction_handle, outside)
                yield pinned

        monkeypatch.setattr(
            windows_psec, "_pin_host_path_tree_for_archive",
            install_junction_after_validation,
        )
        try:
            if legacy_mapping:
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
                sandbox = registry.get(SESSION_ID)
            else:
                sandbox = registry.create(
                    SESSION_ID, workspace_root=source, backend="docker",
                )

            assert isinstance(sandbox, DockerSandbox)
            assert sandbox.path_kind("slot/secret.txt") is None
            assert (outside / "secret.txt").read_text(encoding="utf-8") == "external secret"
        finally:
            kernel32.CloseHandle(junction_handle)


@docker_required
class TestDockerRegistryPosixSymlinkSwapRace:
    """#1301：copy_in 非 Windows 分支按路径二次遍历的换链竞态回归。

    竞态是真实的：换链在**真实 tar 归档进行中**触发（addfile 钩子在归档到
    `workspace/nested` 目录成员那一刻，对真实文件系统执行 rename + symlink）。
    monkeypatch 只提供确定性交错点，不 mock 竞态本身——归档遍历、换链、泄漏
    观察全部走真实文件系统与真实容器。基线（按路径二次遍历）会把 `outside`
    的内容经换链后的 symlink 打进 tar（换链泄漏）；修后（fd 相对 + O_NOFOLLOW）
    读到的仍是 pin 住的原文件内容。
    """

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink swap is POSIX-only")
    @pytest.mark.parametrize("legacy_mapping", [False, True], ids=["new", "psec-migration"])
    def test_import_does_not_leak_content_through_mid_archive_symlink_swap(
        self, registry, tmp_path: Path, legacy_mapping: bool,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import tarfile as tarfile_module

        source = tmp_path / "project"
        nested = source / "nested"
        nested.mkdir(parents=True)
        (nested / "payload.txt").write_text("ORIGINAL-PAYLOAD", encoding="utf-8")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "payload.txt").write_text("EXTERNAL-SECRET", encoding="utf-8")

        swapped = {"done": False}
        original_addfile = tarfile_module.TarFile.addfile

        def addfile_with_real_swap(self, tarinfo, fileobj=None):
            if (
                not swapped["done"]
                and tarinfo.isdir()
                and tarinfo.name == "workspace/nested"
            ):
                swapped["done"] = True
                os.rename(nested, nested.with_name("nested.pre-swap"))
                os.symlink(outside, nested)
            return original_addfile(self, tarinfo, fileobj)

        monkeypatch.setattr(tarfile_module.TarFile, "addfile", addfile_with_real_swap)

        if legacy_mapping:
            registry._write_mapping(SESSION_ID, {
                "session_id": SESSION_ID,
                "backend": "windows_psec",
                "workspace_root": str(source),
                "container_name": None,
                "volume_name": None,
                "created_at": "legacy-created-at",
            })
            sandbox = registry.get(SESSION_ID)
        else:
            sandbox = registry.create(
                SESSION_ID, workspace_root=source, backend="docker",
            )

        try:
            assert sandbox.read_text("nested/payload.txt") == "ORIGINAL-PAYLOAD"
            assert (outside / "payload.txt").read_text(
                encoding="utf-8"
            ) == "EXTERNAL-SECRET"
        finally:
            if swapped["done"]:
                if nested.is_symlink():
                    nested.unlink()
                pre_swap = nested.with_name("nested.pre-swap")
                if pre_swap.is_dir():
                    os.rename(pre_swap, nested)
