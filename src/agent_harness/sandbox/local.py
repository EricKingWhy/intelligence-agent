"""LocalSubprocessSandbox：在本机子进程执行命令的 Sandbox 后端。

开发/测试默认后端，零外部依赖（不需要 Docker daemon）。workspace_root 是本机
一个真实目录，命令通过 subprocess.run 执行，文件读写用标准 pathlib。

生产环境隔离请用 DockerSandbox；本后端不做进程级隔离。
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from time import perf_counter

from agent_harness.sandbox.base import ExecResult, Sandbox

#: LocalSubprocess 的默认命令超时（秒）。None 表示不超时。
DEFAULT_EXEC_TIMEOUT: float = 60.0

logger = logging.getLogger("agent_harness.sandbox.local")

#: 捕获流读取块大小（字符）。
_DRAIN_CHUNK_CHARS = 65536


class _CappedCapture:
    """有上限的捕获缓冲：超限后继续排空管道（让子进程自然结束）但丢弃内容。

    没有它，`subprocess.run(capture_output=True)` 会把任意大的 stdout/stderr
    整体读进内存——一个 cat 大文件的命令就能 OOM 掉整个 agent 进程（D4）。
    """

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self._chunks: list[str] = []
        self._remaining = max_chars
        self.truncated = False
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        with self._lock:
            if self._remaining > 0:
                keep = text[: self._remaining]
                self._chunks.append(keep)
                self._remaining -= len(keep)
                if len(keep) < len(text):
                    self.truncated = True
            elif text:
                self.truncated = True

    def value(self) -> str:
        with self._lock:
            return "".join(self._chunks)


def _glob_match(rel_path: str, pattern: str) -> bool:
    """对 workspace 相对路径做 glob 匹配，支持 ** 递归。

    pathlib.PurePath.match 不支持顶级 ** 前缀跨多段目录匹配，
    所以这里把 ** 模式规范化后用 fnmatch 逐段处理。
    """
    if pattern in ("", "*"):
        return True
    if "**" in pattern:
        # 把 "**/" 收敛成 ""，让 fnmatch 对完整相对路径匹配剩余字面段。
        # 简化策略：如果模式含 **，剥掉 **/ 后对路径末尾段做匹配。
        normalized = pattern.replace("**/", "").replace("**", "*")
        return fnmatch.fnmatch(rel_path, normalized) or fnmatch.fnmatch(
            Path(rel_path).name, normalized
        )
    return fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(
        Path(rel_path).name, pattern
    )


class LocalSubprocessSandbox(Sandbox):
    """本机子进程 Sandbox：命令在本机 subprocess 里跑，文件读写落在 workspace 目录。

    workspace_root 在构造时确定；所有路径操作都经过 _resolve_within_workspace 校验。
    进程不存在"启动"概念，ensure_started 是 no-op；stop 也不需要清理（幂等空操作）。
    """

    def __init__(self, workspace_root: Path, *, max_capture_chars: int = 2_000_000) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._max_capture_chars = max_capture_chars

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def ensure_started(self) -> None:
        """no-op：本机进程总在，无需启动。幂等。"""

    def exec(self, command: str, *, timeout: float | None = None) -> ExecResult:
        """在本机 subprocess 执行命令，cwd 锁定在 workspace_root。

        timeout 默认 DEFAULT_EXEC_TIMEOUT 秒；到点杀掉子进程并返回
        ExecResult(exit_code=-1, stderr="命令超时…")，不抛异常。
        stdout/stderr 捕获到 max_capture_chars 上限，超限丢弃并附截断标记
        （D4：无上限捕获会被大输出 OOM）。管道由 reader 线程持续排空，
        子进程可自然结束，不会因为缓冲塞满而死锁。
        """
        self.ensure_started()
        effective_timeout = timeout if timeout is not None else DEFAULT_EXEC_TIMEOUT

        # encoding/errors：Windows 中文系统默认 GBK，模型跑的命令可能输出 UTF-8 或 GBK；
        # 用 errors="replace" 保证任何字节序列都不会让 subprocess 解码崩掉。
        t0 = perf_counter()
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=self._workspace_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout_cap = _CappedCapture(self._max_capture_chars)
        stderr_cap = _CappedCapture(self._max_capture_chars)
        readers = [
            threading.Thread(target=self._drain_stream, args=(process.stdout, stdout_cap), daemon=True),
            threading.Thread(target=self._drain_stream, args=(process.stderr, stderr_cap), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            exit_code = process.wait(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover — kill 后通常立即退出
                exit_code = -1
        for reader in readers:
            reader.join(5)

        stdout, stderr = stdout_cap.value(), stderr_cap.value()
        if stdout_cap.truncated:
            stdout += f"\n[stdout 超过捕获上限 {self._max_capture_chars} 字符，已截断]"
        if stderr_cap.truncated:
            stderr += f"\n[stderr 超过捕获上限 {self._max_capture_chars} 字符，已截断]"
        if timed_out:
            exit_code = -1
            stderr += f"\n命令超时（上限 {effective_timeout} 秒）"
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=round((perf_counter() - t0) * 1000, 1),
        )

    @staticmethod
    def _drain_stream(stream, cap: _CappedCapture) -> None:
        """后台排空一条捕获流；超限后只读不存，保证子进程不被管道背压卡死。"""
        try:
            while chunk := stream.read(_DRAIN_CHUNK_CHARS):
                cap.append(chunk)
        except Exception as error:  # noqa: BLE001 — 流被随 kill 关闭属正常路径
            logger.debug("capture stream closed during drain: %s", type(error).__name__)
        finally:
            try:
                stream.close()
            except Exception as error:  # noqa: BLE001 — 关闭失败不影响结果
                logger.debug("capture stream close failed: %s", type(error).__name__)

    def list_files(self, pattern: str) -> list[str]:
        """枚举 workspace 内匹配 glob 模式的文件，返回 POSIX 风格相对路径（排序）。

        用 os.walk 遍历 workspace_root，对每个文件的【相对路径】做 glob 匹配。
        pattern 为空或 "*" 时返回所有文件。仅返回文件，不返回目录。
        """
        effective = pattern if pattern else "*"
        results: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(self._workspace_root):
            for fname in filenames:
                full = Path(dirpath) / fname
                rel = full.relative_to(self._workspace_root)
                rel_posix = rel.as_posix()
                if _glob_match(rel_posix, effective):
                    results.append(rel_posix)
        results.sort()
        return results

    def read_text(self, path: str) -> str:
        """读 workspace 内文件。路径越界抛 PermissionError，文件不存在抛 FileNotFoundError。"""
        resolved = self._resolve_within_workspace(path)
        return resolved.read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> None:
        """覆盖写 workspace 内文件（父目录自动创建）。路径越界抛 PermissionError。"""
        resolved = self._resolve_within_workspace(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")

    def copy_in(self, host_path: Path, workspace_path: str) -> None:
        """把宿主文件/目录拷入 workspace 内指定位置。workspace_path 越界抛 PermissionError。"""
        resolved = self._resolve_within_workspace(workspace_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        host = Path(host_path)
        if host.is_dir():
            shutil.copytree(host, resolved)
        else:
            shutil.copy2(host, resolved)

    def stop(self) -> None:
        """no-op：本机进程不需要清理。幂等。"""

    def delete(self) -> None:
        """彻底删除 workspace 目录。幂等（目录不存在也不报错）。"""
        shutil.rmtree(self._workspace_root, ignore_errors=True)
