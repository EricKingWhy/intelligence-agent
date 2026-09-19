"""Docker-backed Sandbox with an optional, lazily loaded SDK dependency."""

from __future__ import annotations

import fnmatch
import importlib
import io
import logging
import posixpath
import queue
import shlex
import tarfile
import threading
import weakref
from pathlib import Path, PurePosixPath
from time import perf_counter, sleep
from typing import ClassVar
from uuid import uuid4

from agent_harness.sandbox.base import (
    ExecResult,
    Sandbox,
    ShellEnvironment,
    ShellFamily,
)
from agent_harness.sandbox.decoding import StreamDecoder
from agent_harness.sandbox.local import DEFAULT_EXEC_TIMEOUT, _CappedCapture

logger = logging.getLogger("agent_harness.sandbox.docker")


class _ContainerExecState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cleanup_failed = False


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

    _container_exec_states = weakref.WeakValueDictionary()
    _container_exec_states_lock = threading.Lock()
    _poisoned_container_exec_states: ClassVar[dict[str, _ContainerExecState]] = {}

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
        self._exec_state = self._state_for_container(self._container_name)
        self._exec_lock = self._exec_state.lock

    @classmethod
    def _state_for_container(cls, container_name: str) -> _ContainerExecState:
        with cls._container_exec_states_lock:
            state = cls._poisoned_container_exec_states.get(container_name)
            if state is not None:
                return state
            state = cls._container_exec_states.get(container_name)
            if state is None:
                state = _ContainerExecState()
                cls._container_exec_states[container_name] = state
            return state

    @property
    def workspace_root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    @property
    def shell_environment(self) -> ShellEnvironment:
        """容器内固定用 `/bin/sh -lc`（见 exec）——OBS-012：是 sh，不是 bash。"""
        return ShellEnvironment(name="/bin/sh", family=ShellFamily.POSIX_SH)

    def ensure_started(self) -> None:
        if self._container is not None:
            self._container.reload()
            if self._container.status == "running":
                return
            self._container.start()
            return

        # 跨进程恢复：按确定性 container_name 查找已存在的容器。
        # 进程重启后 self._container 为 None，但容器可能还在（停止状态）。
        # 如果找到，重启它；找不到才创建新容器。
        docker = importlib.import_module("docker")
        try:
            existing = self._client.containers.get(self._container_name)
        except docker.errors.NotFound:
            existing = None
        if existing is not None:
            if existing.status != "running":
                existing.start()
            self._container = existing
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

    def exec(self, command: str, *, timeout: float | None = None,
             cancel_event=None, on_output=None) -> ExecResult:
        """在容器内执行命令，超时/取消终止对应 exec 的进程组。"""
        started = perf_counter()
        effective_timeout = timeout if timeout is not None else DEFAULT_EXEC_TIMEOUT
        deadline = started + effective_timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return self._interrupted_result(started, effective_timeout, cancelled=True)
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return self._interrupted_result(started, effective_timeout, cancelled=False)
            if self._exec_lock.acquire(timeout=min(0.05, remaining)):
                break
        try:
            self._assert_exec_cleanup_healthy()
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return self._interrupted_result(started, effective_timeout, cancelled=False)
            self.ensure_started()
            return self._exec_locked(
                command,
                effective_timeout=effective_timeout,
                started=started,
                deadline=perf_counter() + remaining,
                cancel_event=cancel_event,
                on_output=on_output,
            )
        finally:
            self._exec_lock.release()

    @staticmethod
    def _interrupted_result(
        started: float,
        effective_timeout: float,
        *,
        cancelled: bool,
        stdout: str = "",
        stderr: str = "",
    ) -> ExecResult:
        detail = "\n命令被取消，进程树已终止" if cancelled else (
            f"\n命令超时（上限 {effective_timeout} 秒）"
        )
        return ExecResult(
            exit_code=-1,
            stdout=stdout,
            stderr=f"{stderr}{detail}",
            duration_ms=round((perf_counter() - started) * 1000, 1),
            cancelled=cancelled,
        )

    def _assert_exec_cleanup_healthy(self) -> None:
        state = getattr(self, "_exec_state", None)
        if getattr(self, "_exec_cleanup_failed", False) or (
            state is not None and state.cleanup_failed
        ):
            raise RuntimeError(
                "DockerSandbox cannot execute after a previous cleanup was unconfirmed"
            )

    def _mark_exec_cleanup_failed(self) -> None:
        self._exec_cleanup_failed = True
        state = getattr(self, "_exec_state", None)
        if state is not None:
            state.cleanup_failed = True
            container_name = getattr(self, "_container_name", None)
            if container_name is not None:
                cls = type(self)
                with cls._container_exec_states_lock:
                    cls._poisoned_container_exec_states[container_name] = state

    def _exec_locked(
        self,
        command: str,
        *,
        effective_timeout: float,
        started: float,
        deadline: float,
        cancel_event,
        on_output,
    ) -> ExecResult:
        api = self._client.api
        if not hasattr(api, "exec_create"):
            raise RuntimeError("DockerSandbox 需要支持 low-level exec API 的 Docker SDK")

        wrapped_command = (
            "printf '\\036AH_PID:%s\\037\\n' \"$$\" >&2; "
            "trap 'exit 143' TERM INT HUP; "
            f"/bin/sh -lc {shlex.quote(command)}; status=$?; exit $status"
        )
        if cancel_event is not None and cancel_event.is_set():
            return self._interrupted_result(started, effective_timeout, cancelled=True)
        complete, created = self._bounded_call(
            lambda: api.exec_create(
                self._container.id,
                ["setsid", "-w", "/bin/sh", "-lc", wrapped_command],
                workdir=str(self.workspace_root),
            ),
            max(0.0, deadline - perf_counter()),
            cancel_event=cancel_event,
        )
        if not complete:
            return self._interrupted_result(
                started,
                effective_timeout,
                cancelled=bool(cancel_event is not None and cancel_event.is_set()),
            )
        if not isinstance(created, dict) or not created.get("Id"):
            raise RuntimeError("Docker exec_create 返回了无效的 exec ID")
        if cancel_event is not None and cancel_event.is_set():
            return self._interrupted_result(started, effective_timeout, cancelled=True)
        if perf_counter() >= deadline:
            return self._interrupted_result(started, effective_timeout, cancelled=False)
        exec_id = created["Id"]
        output_queue: queue.Queue[tuple[bytes | None, bytes | None] | None] = queue.Queue(
            maxsize=128,
        )
        stop_stream = threading.Event()
        holder: dict[str, object] = {}
        control_pid: int | None = None
        control_pending = bytearray()
        control_prefix = b"\x1eAH_PID:"
        control_suffix = b"\x1f\n"

        def _put(chunk: tuple[bytes | None, bytes | None] | None) -> None:
            while not stop_stream.is_set():
                try:
                    output_queue.put(chunk, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def _run() -> None:
            try:
                stream = api.exec_start(exec_id, stream=True, demux=True)
                holder["stream"] = stream
                for chunk in stream:
                    _put(chunk)
            except Exception as error:  # noqa: BLE001
                holder["error"] = error
            finally:
                _put(None)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        stdout_capture = _CappedCapture(2_000_000)
        stderr_capture = _CappedCapture(2_000_000)
        stdout_decoder = StreamDecoder("utf-8")
        stderr_decoder = StreamDecoder("utf-8")

        def consume(chunk: tuple[bytes | None, bytes | None]) -> None:
            nonlocal control_pid
            stdout_payload, stderr_payload = chunk
            if stderr_payload and control_pid is None:
                control_pending.extend(stderr_payload)
                start = control_pending.find(control_prefix)
                if start < 0:
                    keep = 0
                    for size in range(1, len(control_prefix)):
                        if control_pending.endswith(control_prefix[:size]):
                            keep = size
                    if keep:
                        stderr_payload = bytes(control_pending[:-keep])
                        del control_pending[:-keep]
                    else:
                        stderr_payload = bytes(control_pending)
                        control_pending.clear()
                else:
                    end = control_pending.find(
                        control_suffix, start + len(control_prefix),
                    )
                    if end < 0:
                        stderr_payload = bytes(control_pending[:start])
                        del control_pending[:start]
                    else:
                        candidate = bytes(control_pending[start + len(control_prefix):end])
                        try:
                            decoded_pid = candidate.decode("ascii")
                        except UnicodeDecodeError:
                            decoded_pid = ""
                        if decoded_pid.isdecimal():
                            control_pid = int(decoded_pid)
                            stderr_payload = bytes(
                                control_pending[:start]
                                + control_pending[end + len(control_suffix):]
                            )
                            control_pending.clear()
                        else:
                            stderr_payload = bytes(control_pending)
                            control_pending.clear()
                chunk = (stdout_payload, stderr_payload)
            for channel, payload, decoder, capture in (
                ("stdout", chunk[0], stdout_decoder, stdout_capture),
                ("stderr", chunk[1], stderr_decoder, stderr_capture),
            ):
                if not payload:
                    continue
                text = decoder.feed(payload)
                capture.append(text)
                if on_output is not None and text:
                    try:
                        on_output(channel, text)
                    except Exception:
                        logger.debug("Docker output callback failed", exc_info=True)

        cancelled = False
        timed_out = False
        finished = False
        while not finished:
            remaining = max(0.0, deadline - perf_counter())
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if remaining <= 0:
                timed_out = True
                break
            try:
                chunk = output_queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                finished = True
            else:
                consume(chunk)

        if cancelled or timed_out:
            startup_deadline = perf_counter() + 0.25
            while control_pid is None and perf_counter() < startup_deadline:
                try:
                    chunk = output_queue.get(timeout=0.02)
                except queue.Empty:
                    continue
                if chunk is not None:
                    consume(chunk)
            while True:
                try:
                    chunk = output_queue.get_nowait()
                except queue.Empty:
                    break
                if chunk is not None:
                    consume(chunk)
            stop_stream.set()
            self._terminate_exec(api, exec_id, control_pid)
            stream = holder.get("stream")
            if stream is not None and hasattr(stream, "close"):
                self._best_effort_bounded_call(stream.close, 0.2)
            self._stop_container()
            worker.join(1.0)
            if worker.is_alive():
                self._mark_exec_cleanup_failed()
                raise RuntimeError(
                    "Docker exec cleanup could not be confirmed: output stream worker did not stop"
                )
            while True:
                try:
                    chunk = output_queue.get_nowait()
                except queue.Empty:
                    break
                if chunk is not None:
                    consume(chunk)
        else:
            if "error" in holder:
                self._terminate_exec(api, exec_id, control_pid)
                stream = holder.get("stream")
                if stream is not None and hasattr(stream, "close"):
                    self._best_effort_bounded_call(stream.close, 0.2)
                try:
                    self._stop_container()
                except RuntimeError as cleanup_error:
                    raise cleanup_error from holder["error"]
                worker.join(1.0)
                if worker.is_alive():
                    self._mark_exec_cleanup_failed()
                    raise RuntimeError(
                        "Docker exec cleanup could not be confirmed: output stream worker did not stop"
                    ) from holder["error"]
                raise holder["error"]

        for decoder, capture, channel in (
            (stdout_decoder, stdout_capture, "stdout"),
            (stderr_decoder, stderr_capture, "stderr"),
        ):
            text = decoder.flush()
            capture.append(text)
            if on_output is not None and text:
                try:
                    on_output(channel, text)
                except Exception:
                    logger.debug("Docker output callback failed", exc_info=True)

        if cancelled or timed_out:
            stdout, stderr = stdout_capture.value(), stderr_capture.value()
            if stdout_capture.truncated:
                stdout += "\n[stdout 超过捕获上限 2000000 字符，已截断]"
            if stderr_capture.truncated:
                stderr += "\n[stderr 超过捕获上限 2000000 字符，已截断]"
            stderr += "\n命令被取消，进程树已终止" if cancelled else (
                f"\n命令超时（上限 {effective_timeout} 秒）"
            )
            return ExecResult(
                exit_code=-1, stdout=stdout, stderr=stderr,
                duration_ms=round((perf_counter() - started) * 1000, 1),
                cancelled=cancelled,
            )

        inspected = api.exec_inspect(exec_id)
        stdout, stderr = stdout_capture.value(), stderr_capture.value()
        if stdout_capture.truncated:
            stdout += "\n[stdout 超过捕获上限 2000000 字符，已截断]"
        if stderr_capture.truncated:
            stderr += "\n[stderr 超过捕获上限 2000000 字符，已截断]"
        return ExecResult(
            exit_code=inspected.get("ExitCode", 0), stdout=stdout, stderr=stderr,
            duration_ms=round((perf_counter() - started) * 1000, 1),
        )

    @staticmethod
    def _best_effort_bounded_call(call, timeout: float) -> tuple[bool, object | None]:
        try:
            return DockerSandbox._bounded_call(call, timeout)
        except Exception as error:  # noqa: BLE001
            logger.debug("Docker cleanup call failed: %s", type(error).__name__)
            return False, None

    def _stop_container(self) -> None:
        kill_error: Exception | None = None
        try:
            complete, _ = self._bounded_call(self._container.kill, 0.5)
            if not complete:
                raise TimeoutError("Docker container kill timed out")
        except Exception as error:  # noqa: BLE001
            kill_error = error

        try:
            complete, _ = self._bounded_call(self._container.wait, 5.0)
            if not complete:
                raise TimeoutError("Docker container stop wait timed out")
        except Exception as error:
            self._mark_exec_cleanup_failed()
            logger.debug(
                "Docker container stop could not be confirmed (kill error: %s)",
                type(kill_error).__name__ if kill_error is not None else "none",
            )
            raise RuntimeError(
                "Docker exec cleanup could not be confirmed: container did not stop"
            ) from error

    @staticmethod
    def _bounded_call(
        call,
        timeout: float,
        *,
        cancel_event=None,
    ) -> tuple[bool, object | None]:
        holder: dict[str, object] = {}

        def run() -> None:
            try:
                holder["result"] = call()
            except Exception as error:  # noqa: BLE001
                holder["error"] = error

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = perf_counter() + timeout
        while worker.is_alive():
            if cancel_event is not None and cancel_event.is_set():
                return False, None
            remaining = deadline - perf_counter()
            if remaining <= 0:
                return False, None
            worker.join(min(0.05, remaining))
        if "error" in holder:
            raise holder["error"]
        return True, holder.get("result")

    def _terminate_exec(self, api, exec_id: str, pid: int | None) -> None:
        if pid is None or pid <= 1:
            logger.warning("Docker exec %s did not publish a kill PID", exec_id)
            return
        workdir = str(self.workspace_root)

        def kill(signal_name: str):
            command = (
                "signal=$2; "
                "kill_tree() { "
                "for child in $(cat /proc/$1/task/$1/children 2>/dev/null); do "
                "kill_tree $child; "
                "done; "
                "kill -$signal -$1 2>/dev/null; "
                "}; "
                "kill_tree $1"
            )
            return self._container.exec_run(
                ["/bin/sh", "-lc", command, "--", str(pid), signal_name],
                workdir=workdir,
            )

        self._best_effort_bounded_call(lambda: kill("TERM"), 0.2)
        sleep(0.2)
        self._best_effort_bounded_call(lambda: kill("KILL"), 0.2)

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
        target = self.resolve_within_workspace(path)
        self.ensure_started()
        docker = importlib.import_module("docker")
        try:
            stream, _ = self._container.get_archive(str(target))
        except docker.errors.NotFound as exc:
            # Docker 的 get_archive 对不存在的文件抛 NotFound（OSError 子类，
            # 不是 FileNotFoundError）。统一映射成 FileNotFoundError，
            # 让调用方（如 WriteTool 读 before）能按预期捕获。
            raise FileNotFoundError(path) from exc
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream)), mode="r:") as archive:
            member = next((item for item in archive.getmembers() if item.isfile()), None)
            if member is None:
                raise FileNotFoundError(path)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise FileNotFoundError(path)
            return extracted.read().decode("utf-8")

    def write_text(self, path: str, content: str) -> None:
        target = self.resolve_within_workspace(path)
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
        target = self.resolve_within_workspace(workspace_path)
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

    def resolve_within_workspace(self, path: str) -> PurePosixPath:
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
        """停容器（保留 Volume 以便 resume）。幂等。"""
        if self._container is None:
            return
        docker = importlib.import_module("docker")
        try:
            self._container.reload()
            if self._container.status == "running":
                self._container.stop()
        except docker.errors.NotFound:
            pass
        finally:
            self._container = None

    def delete(self) -> None:
        """彻底清理：移除容器 + 删除 Volume。幂等。"""
        docker = importlib.import_module("docker")
        if self._container is not None:
            try:
                self._container.remove(force=True)
            except docker.errors.NotFound:
                pass
            finally:
                self._container = None
        else:
            # 跨进程：容器可能还在，按确定性名字查回再删。
            try:
                existing = self._client.containers.get(self._container_name)
            except docker.errors.NotFound:
                existing = None
            if existing is not None:
                existing.remove(force=True)
        try:
            self._client.volumes.get(self._volume_name).remove(force=True)
        except docker.errors.NotFound:
            pass
        state = getattr(self, "_exec_state", None)
        if state is not None:
            cls = type(self)
            with cls._container_exec_states_lock:
                if cls._poisoned_container_exec_states.get(self._container_name) is state:
                    cls._poisoned_container_exec_states.pop(self._container_name, None)
