"""DockerSandbox contract tests."""

from __future__ import annotations

import importlib
import io
import os
import tarfile
import threading
import time
from collections.abc import Iterator
from uuid import uuid4

import pytest

from agent_harness.sandbox import ExecResult


def _docker_available() -> bool:
    try:
        docker = importlib.import_module("docker")
        client = docker.from_env(use_context=False)
        client.ping()
        client.close()
    except Exception:  # noqa: BLE001 — probe: any failure means Docker unavailable
        return False
    return True


docker_required = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker SDK or daemon is unavailable",
)


@pytest.mark.parametrize(
    ("member_name", "member_type", "linkname"),
    [
        ("workspace/../canary.txt", tarfile.REGTYPE, ""),
        ("/workspace/canary.txt", tarfile.REGTYPE, ""),
        ("workspace/external-link", tarfile.LNKTYPE, "../../canary.txt"),
        ("workspace/external-link", tarfile.SYMTYPE, "../../canary.txt"),
        ("workspace/fifo", tarfile.FIFOTYPE, ""),
    ],
)
def test_copy_out_rejects_unsafe_archive_entries_without_touching_canary(
    tmp_path, member_name: str, member_type: bytes, linkname: str,
) -> None:
    from agent_harness.sandbox.docker import DockerSandbox

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        root = tarfile.TarInfo("workspace")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        safe = tarfile.TarInfo("workspace/safe.txt")
        safe.size = 4
        archive.addfile(safe, io.BytesIO(b"safe"))
        unsafe = tarfile.TarInfo(member_name)
        unsafe.type = member_type
        unsafe.linkname = linkname
        archive.addfile(unsafe, io.BytesIO(b"bad") if member_type == tarfile.REGTYPE else None)

    class Container:
        def get_archive(self, path: str):
            assert path == "/workspace"
            return iter((archive_bytes.getvalue(),)), {}

    sandbox = object.__new__(DockerSandbox)
    sandbox._container = Container()
    sandbox.ensure_started = lambda: None
    destination = tmp_path / "export"
    canary = tmp_path / "canary.txt"
    canary.write_text("safe", encoding="utf-8")

    with pytest.raises(ValueError):
        sandbox.copy_out(destination)

    assert not destination.exists()
    assert canary.read_text(encoding="utf-8") == "safe"


@pytest.mark.skipif(os.name != "nt", reason="Windows host filename rules apply only on Windows")
@pytest.mark.parametrize("filename", ["config.", "NUL.txt", "COM¹.log"])
def test_copy_out_rejects_windows_alias_and_device_names(
    tmp_path, filename: str,
) -> None:
    from agent_harness.sandbox.docker import DockerSandbox

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        root = tarfile.TarInfo("workspace")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        member = tarfile.TarInfo(f"workspace/{filename}")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"data"))

    class Container:
        def get_archive(self, path: str):
            assert path == "/workspace"
            return iter((archive_bytes.getvalue(),)), {}

    sandbox = object.__new__(DockerSandbox)
    sandbox._container = Container()
    sandbox.ensure_started = lambda: None
    destination = tmp_path / "export"

    with pytest.raises(ValueError, match="Windows"):
        sandbox.copy_out(destination)

    assert not destination.exists()


def test_copy_out_removes_readonly_partial_export_after_extraction_failure(
    tmp_path,
) -> None:
    from agent_harness.sandbox.docker import DockerSandbox

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        root = tarfile.TarInfo("workspace")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        readonly = tarfile.TarInfo("workspace/blocked")
        readonly.mode = 0o444
        readonly.size = 4
        archive.addfile(readonly, io.BytesIO(b"data"))
        child = tarfile.TarInfo("workspace/blocked/child.txt")
        child.size = 4
        archive.addfile(child, io.BytesIO(b"data"))

    class Container:
        def get_archive(self, path: str):
            assert path == "/workspace"
            return iter((archive_bytes.getvalue(),)), {}

    sandbox = object.__new__(DockerSandbox)
    sandbox._container = Container()
    sandbox.ensure_started = lambda: None
    destination = tmp_path / "export"

    with pytest.raises(FileExistsError):
        sandbox.copy_out(destination)

    assert not destination.exists()


def test_copy_out_surfaces_partial_export_cleanup_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_harness.sandbox.docker as docker_module

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        root = tarfile.TarInfo("workspace")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        readonly = tarfile.TarInfo("workspace/blocked")
        readonly.size = 4
        archive.addfile(readonly, io.BytesIO(b"data"))
        child = tarfile.TarInfo("workspace/blocked/child.txt")
        child.size = 4
        archive.addfile(child, io.BytesIO(b"data"))

    class Container:
        def get_archive(self, path: str):
            assert path == "/workspace"
            return iter((archive_bytes.getvalue(),)), {}

    sandbox = object.__new__(docker_module.DockerSandbox)
    sandbox._container = Container()
    sandbox.ensure_started = lambda: None
    destination = tmp_path / "export"

    def fail_cleanup(*args, **kwargs):
        raise PermissionError("injected cleanup failure")

    monkeypatch.setattr(docker_module.shutil, "rmtree", fail_cleanup)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        sandbox.copy_out(destination)


@pytest.fixture
def docker_sandbox() -> Iterator[object]:
    from agent_harness.sandbox.docker import DockerSandbox

    suffix = uuid4().hex
    volume_name = f"agent-harness-test-{suffix}"
    sandbox = DockerSandbox(
        container_name=f"agent-harness-test-{suffix}",
        volume_name=volume_name,
    )
    try:
        yield sandbox
    finally:
        sandbox.delete()


def test_constructor_reports_missing_docker_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The optional Docker SDK is loaded only when the backend is constructed."""
    real_import_module = importlib.import_module

    def import_without_docker(name: str, package: str | None = None):
        if name == "docker":
            raise ModuleNotFoundError("No module named 'docker'")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", import_without_docker)

    from agent_harness.sandbox.docker import DockerSandbox

    with pytest.raises(RuntimeError, match="DockerSandbox 需要 pip install docker"):
        DockerSandbox()


@docker_required
def test_exec_runs_in_workspace_and_stop_is_idempotent(docker_sandbox: object) -> None:
    result = docker_sandbox.exec("printf hello")

    assert isinstance(result, ExecResult)
    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert result.stderr == ""
    assert result.duration_ms >= 0

    docker_sandbox.stop()
    docker_sandbox.stop()


@docker_required
def test_timeout_stops_late_workspace_mutation(docker_sandbox: object) -> None:
    marker = "/workspace/late-timeout-marker"
    # Keep Docker's cold container startup outside the command timeout being tested.
    assert docker_sandbox.exec("true", timeout=10).exit_code == 0
    # 预算 1.0s / 写入点 2.5s：预算必须够命令启动（printf before 有输出，断言才非平凡），
    # 又必须早于写入点——0.2s 会让容器冷启动吃掉全部预算，命令根本没跑起来。
    result = docker_sandbox.exec(
        f"rm -f {marker}; "
        f"(sleep 2.5; touch {marker}) & child=$!; "
        f"printf before; wait $child",
        timeout=1.0,
    )

    assert result.exit_code == -1
    assert "before" in result.stdout
    assert "超时" in result.stderr
    time.sleep(2.7)
    assert docker_sandbox.exec(f"test ! -e {marker}").exit_code == 0


@docker_required
def test_timeout_stops_detached_workspace_mutation(docker_sandbox: object) -> None:
    marker = "/workspace/detached-timeout-marker"
    # 预算/延迟同 test_timeout_stops_late_workspace_mutation：保证命令真的启动并跑满预算。
    result = docker_sandbox.exec(
        f"rm -f {marker}; "
        f"setsid /bin/sh -lc 'sleep 2.5; touch {marker}' "
        ">/dev/null 2>&1 & sleep 3.5",
        timeout=1.0,
    )

    assert result.exit_code == -1
    time.sleep(2.7)
    assert docker_sandbox.exec(f"test ! -e {marker}").exit_code == 0


@docker_required
def test_cancel_stops_late_workspace_mutation(docker_sandbox: object) -> None:
    marker = "/workspace/late-cancel-marker"
    cancel_event = threading.Event()
    result_holder: list[ExecResult] = []
    ready = threading.Event()

    def on_output(channel: str, text: str) -> None:
        if channel == "stdout" and "before" in text:
            ready.set()

    worker = threading.Thread(
        target=lambda: result_holder.append(
            docker_sandbox.exec(
                f"rm -f {marker}; "
                f"(sleep 1; touch {marker}) & child=$!; "
                f"printf before; wait $child",
                timeout=5,
                cancel_event=cancel_event,
                on_output=on_output,
            )
        )
    )
    worker.start()
    assert ready.wait(3)
    cancel_event.set()
    worker.join(8)

    assert not worker.is_alive()
    assert len(result_holder) == 1
    assert result_holder[0].exit_code == -1
    assert result_holder[0].cancelled is True
    assert "before" in result_holder[0].stdout
    assert "取消" in result_holder[0].stderr
    time.sleep(1.2)
    assert docker_sandbox.exec(f"test ! -e {marker}").exit_code == 0


@docker_required
def test_write_then_read_nested_file(docker_sandbox: object) -> None:
    docker_sandbox.write_text("nested/note.txt", "你好, Docker")

    assert docker_sandbox.read_text("nested/note.txt") == "你好, Docker"


@docker_required
@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "..\\..\\etc\\passwd"])
def test_workspace_path_escape_is_denied(docker_sandbox: object, path: str) -> None:
    with pytest.raises(PermissionError):
        docker_sandbox.read_text(path)


@docker_required
def test_copy_in_imports_host_file(
    docker_sandbox: object,
    tmp_path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("from Host", encoding="utf-8")

    docker_sandbox.copy_in(source, "imports/copied.txt")

    assert docker_sandbox.read_text("imports/copied.txt") == "from Host"
