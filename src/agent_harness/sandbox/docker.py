"""Docker-backed Sandbox with an optional, lazily loaded SDK dependency."""

from __future__ import annotations

import fnmatch
import importlib
import io
import posixpath
import tarfile
from pathlib import Path, PurePosixPath
from time import perf_counter
from uuid import uuid4

from agent_harness.sandbox.base import ExecResult, Sandbox


def _glob_match_posix(rel_path: str, pattern: str) -> bool:
    """对容器内相对路径做 glob 匹配，支持 ** 递归（与 LocalSubprocessSandbox._glob_match 同语义）。"""
    if pattern in ("", "*"):
        return True
    if "**" in pattern:
        normalized = pattern.replace("**/", "").replace("**", "*")
        return fnmatch.fnmatch(rel_path, normalized) or fnmatch.fnmatch(
            PurePosixPath(rel_path).name, normalized
        )
    return fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(
        PurePosixPath(rel_path).name, pattern
    )


class DockerSandbox(Sandbox):
    """Run Coding Tools inside an isolated Docker container."""

    def __init__(
        self,
        *,
        image: str = "python:3-slim",
        container_name: str | None = None,
        volume_name: str | None = None,
    ) -> None:
        try:
            docker = importlib.import_module("docker")
        except ModuleNotFoundError as error:
            raise RuntimeError("DockerSandbox 需要 pip install docker") from error
        self._client = docker.from_env(use_context=False)
        suffix = uuid4().hex
        self._image = image
        self._container_name = container_name or f"agent-harness-{suffix}"
        self._volume_name = volume_name or f"agent-harness-{suffix}"
        self._container = None

    @property
    def workspace_root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    def ensure_started(self) -> None:
        if self._container is not None:
            self._container.reload()
            if self._container.status == "running":
                return
            self._container.start()
            return

        self._container = self._client.containers.run(
            self._image,
            ["sleep", "infinity"],
            name=self._container_name,
            detach=True,
            working_dir=str(self.workspace_root),
            volumes={
                self._volume_name: {
                    "bind": str(self.workspace_root),
                    "mode": "rw",
                }
            },
        )

    def exec(self, command: str, *, timeout: float | None = None) -> ExecResult:
        del timeout
        self.ensure_started()
        started = perf_counter()
        result = self._container.exec_run(
            ["/bin/sh", "-lc", command],
            demux=True,
            workdir=str(self.workspace_root),
        )
        stdout_bytes, stderr_bytes = result.output
        return ExecResult(
            exit_code=result.exit_code,
            stdout=(stdout_bytes or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_bytes or b"").decode("utf-8", errors="replace"),
            duration_ms=round((perf_counter() - started) * 1000, 1),
        )

    def list_files(self, pattern: str) -> list[str]:
        """枚举容器 workspace 内匹配 glob 模式的文件，返回相对 /workspace 路径（排序）。

        用 exec("find . -type f -printf '%P\n'") 拿文件列表后 Python 侧 fnmatch 过滤。
        """
        effective = pattern if pattern else "*"
        result = self.exec("find . -type f -printf '%P\n'")
        if result.exit_code != 0:
            return []
        candidates = [line for line in result.stdout.splitlines() if line.strip()]
        matched = [
            rel
            for rel in candidates
            if _glob_match_posix(rel, effective)
        ]
        matched.sort()
        return matched

    def read_text(self, path: str) -> str:
        target = self._resolve_within_workspace(path)
        self.ensure_started()
        stream, _ = self._container.get_archive(str(target))
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream)), mode="r:") as archive:
            member = next((item for item in archive.getmembers() if item.isfile()), None)
            if member is None:
                raise FileNotFoundError(path)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise FileNotFoundError(path)
            return extracted.read().decode("utf-8")

    def write_text(self, path: str, content: str) -> None:
        target = self._resolve_within_workspace(path)
        self.ensure_started()
        mkdir_result = self._container.exec_run(["mkdir", "-p", str(target.parent)])
        if mkdir_result.exit_code != 0:
            raise RuntimeError(f"无法创建 Workspace 目录 '{target.parent}'")

        payload = content.encode("utf-8")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            member = tarfile.TarInfo(name=target.name)
            member.size = len(payload)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(payload))
        if not self._container.put_archive(str(target.parent), buffer.getvalue()):
            raise RuntimeError(f"无法写入 Workspace 文件 '{target}'")

    def copy_in(self, host_path: Path, workspace_path: str) -> None:
        target = self._resolve_within_workspace(workspace_path)
        source = Path(host_path)
        if not source.exists():
            raise FileNotFoundError(source)

        self.ensure_started()
        mkdir_result = self._container.exec_run(["mkdir", "-p", str(target.parent)])
        if mkdir_result.exit_code != 0:
            raise RuntimeError(f"无法创建 Workspace 目录 '{target.parent}'")

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", dereference=False) as archive:
            archive.add(source, arcname=target.name, recursive=True)
        if not self._container.put_archive(str(target.parent), buffer.getvalue()):
            raise RuntimeError(f"无法导入 Host 路径 '{source}'")

    def _resolve_within_workspace(self, path: str) -> PurePosixPath:
        """Resolve a model-supplied path with container-native POSIX semantics."""
        root = self.workspace_root
        raw = PurePosixPath(path.replace("\\", "/"))
        candidate = raw if raw.is_absolute() else root / raw
        resolved = PurePosixPath(posixpath.normpath(str(candidate)))
        if resolved != root and root not in resolved.parents:
            raise PermissionError(
                f"路径 '{path}' 解析为 '{resolved}'，越出 workspace '{root}' 边界，拒绝访问"
            )
        return resolved

    def stop(self) -> None:
        if self._container is None:
            return
        try:
            self._container.remove(force=True)
        finally:
            self._container = None
