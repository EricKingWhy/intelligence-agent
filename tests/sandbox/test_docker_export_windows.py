"""Windows host export regressions for Docker workspace archives."""

from __future__ import annotations

import importlib
import io
import os
import shutil
import stat
import tarfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from uuid import uuid4

import pytest

from agent_harness.sandbox.docker import DockerSandbox


def _archive(files: list[tuple[str, bytes, int]]) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        root = tarfile.TarInfo("workspace/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, contents, mode in files:
            member = tarfile.TarInfo(f"workspace/{name}")
            member.size = len(contents)
            member.mode = mode
            archive.addfile(member, io.BytesIO(contents))
    return payload.getvalue()


def _sandbox(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> DockerSandbox:
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox._container = SimpleNamespace(
        get_archive=lambda _path: (iter([payload]), {})
    )
    monkeypatch.setattr(sandbox, "ensure_started", lambda: None)
    return sandbox


def _docker_available() -> bool:
    try:
        client = importlib.import_module("docker").from_env(use_context=False)
        client.ping()
        client.close()
    except Exception:  # noqa: BLE001 — probe: unavailable daemon means skip
        return False
    return True


@pytest.fixture
def docker_sandbox() -> Iterator[DockerSandbox]:
    suffix = uuid4().hex
    sandbox = DockerSandbox(
        container_name=f"agent-harness-export-test-{suffix}",
        volume_name=f"agent-harness-export-test-{suffix}",
    )
    try:
        yield sandbox
    finally:
        sandbox.delete()


@pytest.mark.skipif(os.name != "nt", reason="Windows filename semantics")
@pytest.mark.parametrize(
    "member_name",
    [
        "workspace/config.",
        "workspace/NUL.txt",
        "workspace/CONIN$",
        "workspace/CONOUT$",
        "workspace/subdir./file.txt",
    ],
)
def test_export_rejects_windows_aliases_before_extracting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    member_name: str,
) -> None:
    import agent_harness.sandbox.docker as docker_module

    payload = b"fake archive"
    sandbox = _sandbox(monkeypatch, payload)
    extract_called = False

    class FakeArchive:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def getmembers(self) -> list[tarfile.TarInfo]:
            return [tarfile.TarInfo(member_name)]

        def extractall(self, **_kwargs: object) -> None:
            nonlocal extract_called
            extract_called = True

    monkeypatch.setattr(
        docker_module.tarfile,
        "open",
        lambda **_kwargs: FakeArchive(),
    )
    destination = tmp_path / "export"

    with pytest.raises(ValueError, match="Windows"):
        sandbox.export_to_host(destination)

    assert not extract_called
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows readonly file semantics")
@pytest.mark.parametrize("destination_exists", [False, True])
def test_export_failure_removes_partial_readonly_files_and_preserves_existing_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    destination_exists: bool,
) -> None:
    import agent_harness.sandbox.docker as docker_module

    sandbox = _sandbox(
        monkeypatch,
        _archive(
            [
                ("readonly.txt", b"partial", 0o444),
                ("later.txt", b"failure", 0o644),
            ]
        ),
    )
    destination = tmp_path / "export"
    if destination_exists:
        destination.mkdir()
        (destination / "user-data.txt").write_text("keep", encoding="utf-8")

    real_move = shutil.move

    def fail_after_first_move(source: str, target: str) -> str:
        if Path(source).name == "readonly.txt":
            os.chmod(source, stat.S_IREAD)
            assert Path(source).stat().st_mode & stat.S_IWRITE == 0
        result = real_move(source, target)
        if Path(target).name == "readonly.txt":
            assert Path(target).stat().st_mode & stat.S_IWRITE == 0
            raise PermissionError("simulated export interruption")
        return result

    monkeypatch.setattr(docker_module.shutil, "move", fail_after_first_move)

    with pytest.raises(PermissionError, match="simulated export interruption"):
        sandbox.export_to_host(destination)

    if destination_exists:
        assert (destination / "user-data.txt").read_text(encoding="utf-8") == "keep"
        assert not (destination / "readonly.txt").exists()
        assert not (destination / "later.txt").exists()
    else:
        assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows readonly file semantics")
def test_export_surfaces_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agent_harness.sandbox.docker as docker_module

    sandbox = _sandbox(
        monkeypatch,
        _archive([("readonly.txt", b"partial", 0o444)]),
    )
    destination = tmp_path / "export"
    real_move = shutil.move
    real_cleanup = docker_module._remove_export_path

    def readonly_then_fail(source: str, target: str) -> None:
        os.chmod(source, stat.S_IREAD)
        real_move(source, target)
        raise PermissionError("simulated export interruption")

    def fail_cleanup(_path: Path) -> None:
        raise PermissionError("simulated cleanup failure")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(docker_module.shutil, "move", readonly_then_fail)
        scoped_patch.setattr(docker_module, "_remove_export_path", fail_cleanup)

        with pytest.raises(PermissionError, match="simulated cleanup failure"):
            sandbox.export_to_host(destination)

    real_cleanup(destination)
    assert not destination.exists()


@pytest.mark.skipif(not _docker_available(), reason="Docker daemon is unavailable")
def test_export_to_host_copies_real_docker_workspace(
    docker_sandbox: DockerSandbox, tmp_path: Path
) -> None:
    docker_sandbox.write_text("export-check/note.txt", "from Docker")
    destination = tmp_path / f"docker-export-{uuid4().hex}"

    docker_sandbox.export_to_host(destination)

    assert (destination / "export-check" / "note.txt").read_text(
        encoding="utf-8"
    ) == "from Docker"
