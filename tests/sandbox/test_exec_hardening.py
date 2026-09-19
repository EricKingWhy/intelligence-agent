"""Sandbox exec 硬化：捕获上限（D4）+ 超时契约（D10）+ event loop 卸载。

诊断依据（Directive B /diagnose）：
- D4：subprocess capture_output 无上限，大输出 OOM 进程——捕获到上限后继续
  排空管道让子进程自然结束，超出部分丢弃并附截断标记。
- D10：DockerSandbox.exec 丢弃 timeout 参数且同步调用阻塞 event loop——
  补上超时语义（线程级等待，超时返回 exit_code=-1），并把 tool 边界的
  sandbox.exec 调用卸载到工作线程。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import Mock

import pytest

import agent_harness.sandbox.local as local_module
from agent_harness.sandbox import ExecResult, LocalSubprocessSandbox
from agent_harness.tools import BashTool
from agent_harness.tools.bash import _BashArgs


def test_local_exec_caps_captured_output(tmp_path):
    """输出超过捕获上限：子进程正常结束，超限部分丢弃且带截断标记。"""
    (tmp_path / "gen.py").write_text("print('x' * 5000)", encoding="utf-8")
    sandbox = LocalSubprocessSandbox(tmp_path, max_capture_chars=1000)
    result = sandbox.exec(f'"{sys.executable}" gen.py')
    assert result.exit_code == 0
    assert 1000 <= len(result.stdout) < 5000
    assert "截断" in result.stderr or "截断" in result.stdout


def test_local_exec_under_cap_is_untouched(tmp_path):
    """未超上限时输出原样返回，无标记——既有行为不变。"""
    (tmp_path / "gen.py").write_text("print('hello')", encoding="utf-8")
    sandbox = LocalSubprocessSandbox(tmp_path, max_capture_chars=1000)
    result = sandbox.exec(f'"{sys.executable}" gen.py')
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert "截断" not in result.stdout and "截断" not in result.stderr


# ============================================================================
# 超时击杀范围：整棵进程树，而不只 shell 壳（[HAZARD] 超时杀树泄漏）
# ============================================================================


def _write_delayed_tree_writer(tmp_path):
    """Create a portable child/grandchild marker probe for Local process groups."""
    script = tmp_path / "tree_writer.py"
    script.write_text(
        textwrap.dedent(
            '''\
            import pathlib
            import subprocess
            import sys
            import time

            role, marker_name, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
            marker = pathlib.Path(marker_name)
            if role == "parent":
                started = marker.with_name(marker.stem + "-started" + marker.suffix)
                child = subprocess.Popen([sys.executable, __file__, "child", marker_name, str(delay)])
                print("stdout-before", flush=True)
                print("stderr-before", file=sys.stderr, flush=True)
                started.write_text(str(child.pid), encoding="utf-8")
                time.sleep(delay + 5)
            else:
                time.sleep(delay)
                marker.write_text("late", encoding="utf-8")
            '''
        ),
        encoding="utf-8",
    )
    return script


def _python_command(script, *args):
    parts = [sys.executable, str(script), *map(str, args)]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def _wait_for_path(path, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    assert path.exists(), f"marker 未在 {timeout}s 内出现: {path}"


def test_local_timeout_fallback_uses_job_when_taskkill_fails(monkeypatch):
    """taskkill failure must use the native Job seam, not shell-only kill."""
    calls = []

    class _Process:
        pid = 42

        def kill(self):
            calls.append("process.kill")

    monkeypatch.setattr(local_module.os, "name", "nt")
    monkeypatch.setattr(
        local_module.subprocess,
        "run",
        lambda *args, **kwargs: Mock(returncode=1),
    )
    monkeypatch.setattr(
        local_module.LocalSubprocessSandbox,
        "_terminate_windows_job",
        staticmethod(lambda process: calls.append("job") or True),
    )

    local_module.LocalSubprocessSandbox._kill_process_tree(_Process())

    assert calls == ["job"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object fallback")
def test_windows_native_fallback_kills_real_descendants(monkeypatch, tmp_path):
    """The real Job Object fallback prevents a late descendant marker."""
    script = _write_delayed_tree_writer(tmp_path)
    marker = tmp_path / "native-fallback-marker.txt"
    started = marker.with_name(marker.stem + "-started" + marker.suffix)
    sandbox = LocalSubprocessSandbox(tmp_path)
    monkeypatch.setattr(
        local_module.subprocess,
        "run",
        lambda *args, **kwargs: Mock(returncode=1),
    )
    result = sandbox.exec(
        _python_command(script, "parent", marker, 1.5),
        timeout=0.3,
    )
    _wait_for_path(started)
    assert result.exit_code == -1
    deadline = time.monotonic() + 2.0
    while marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not marker.exists()


def test_local_default_timeout_allows_commands_past_ten_seconds(tmp_path):
    """The Local default remains 60s: a 10s+ command must finish, not be killed early."""
    script = tmp_path / "finish_writer.py"
    marker = tmp_path / "finished.txt"
    script.write_text(
        "import pathlib, sys, time; time.sleep(10.2); "
        "pathlib.Path(sys.argv[1]).write_text('finished', encoding='utf-8')",
        encoding="utf-8",
    )

    result = LocalSubprocessSandbox(tmp_path).exec(
        _python_command(script, marker),
    )

    assert result.exit_code == 0
    assert marker.read_text(encoding="utf-8") == "finished"


def test_local_timeout_kills_child_tree_without_late_marker(tmp_path):
    """Timeout kills the child/grandchild tree and preserves output emitted before it."""
    script = _write_delayed_tree_writer(tmp_path)
    marker = tmp_path / "timeout-marker.txt"
    started = tmp_path / "timeout-marker-started.txt"
    output_script = tmp_path / "partial_output.py"
    output_script.write_text(
        "import sys, time; print('stdout-before', flush=True); "
        "print('stderr-before', file=sys.stderr, flush=True); time.sleep(5)",
        encoding="utf-8",
    )
    # The writer process is the parent of the delayed marker child; its shell is
    # the process owned by LocalSubprocessSandbox.
    command = _python_command(script, "parent", marker, 1.5)
    sandbox = LocalSubprocessSandbox(tmp_path)
    result = sandbox.exec(command, timeout=0.3)

    assert started.exists(), "child 未确认启动，marker 测试无法证明整树终止"
    assert result.exit_code == -1
    assert "超时" in result.stderr
    assert "stdout-before" in result.stdout
    assert "stderr-before" in result.stderr
    deadline = time.monotonic() + 2.0
    while marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not marker.exists(), "timeout 后 child/grandchild 仍写入 marker"

    partial = sandbox.exec(_python_command(output_script), timeout=0.3)
    assert partial.exit_code == -1
    assert "stdout-before" in partial.stdout
    assert "stderr-before" in partial.stderr


def test_local_cancel_kills_child_tree_without_late_marker(tmp_path):
    """Cooperative cancellation kills the same process tree as timeout."""
    script = _write_delayed_tree_writer(tmp_path)
    marker = tmp_path / "cancel-marker.txt"
    started = tmp_path / "cancel-marker-started.txt"
    cancel_event = threading.Event()
    sandbox = LocalSubprocessSandbox(tmp_path)
    result_holder = []
    thread = threading.Thread(
        target=lambda: result_holder.append(
            sandbox.exec(
                _python_command(script, "parent", marker, 1.5),
                timeout=5,
                cancel_event=cancel_event,
            )
        )
    )
    thread.start()
    _wait_for_path(started)
    cancel_event.set()
    thread.join(timeout=5)

    assert not thread.is_alive(), "取消后 Local exec 未在 5s 内返回"
    assert started.exists(), "child 未确认启动，marker 测试无法证明整树终止"
    assert len(result_holder) == 1
    assert result_holder[0].cancelled is True
    assert "stdout-before" in result_holder[0].stdout
    assert "stderr-before" in result_holder[0].stderr
    deadline = time.monotonic() + 2.0
    while marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not marker.exists(), "cancel 后 child/grandchild 仍写入 marker"


@pytest.mark.asyncio
async def test_executor_bash_local_timeout_stops_process_tree(tmp_path):
    """The real executor/tool/sandbox chain stops a timed-out Local command."""
    from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry

    script = _write_delayed_tree_writer(tmp_path)
    marker = tmp_path / "executor-timeout-marker.txt"
    started = marker.with_name(marker.stem + "-started" + marker.suffix)

    class _ShortDeadlineSandbox(LocalSubprocessSandbox):
        def __init__(self, workspace_root):
            super().__init__(workspace_root)
            self.seen_timeout = None

        def exec(self, command, *, timeout=None, cancel_event=None, on_output=None):
            self.seen_timeout = timeout
            return super().exec(
                command,
                timeout=0.3,
                cancel_event=cancel_event,
                on_output=on_output,
            )

    sandbox = _ShortDeadlineSandbox(tmp_path)

    class _TimedBash(BashTool):
        @property
        def timeout_seconds(self) -> float:
            return 0.8

    registry = ToolRegistry()
    registry.register(_TimedBash(sandbox))
    executor = ToolExecutor(registry, policy=PermissionPolicy.DANGER_FULL_ACCESS)

    execution = await executor.execute({
        "id": "executor-timeout",
        "name": "bash",
        "args": {"command": _python_command(script, "parent", marker, 1.5)},
    })

    _wait_for_path(started)
    assert sandbox.seen_timeout == 0.8
    assert execution.result.ok is True
    assert execution.result.data["exit_code"] == -1
    assert "stdout-before" in execution.result.data["stdout"]
    assert "stderr-before" in execution.result.data["stderr"]
    assert "cancelled" not in execution.result.data
    assert execution.result.metadata["attempt"] == 1
    deadline = time.monotonic() + 2.0
    while marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_bash_tool_offloads_exec_to_worker_thread(tmp_path):
    """tool 边界把同步 sandbox.exec 卸载到工作线程——event loop 不被长命令冻结。"""
    seen: dict = {}

    class ProbeSandbox(LocalSubprocessSandbox):
        def exec(self, command, *, timeout=None, cancel_event=None, on_output=None):
            seen["thread"] = threading.current_thread()
            seen["timeout"] = timeout
            return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=0.0)

    tool = BashTool(ProbeSandbox(tmp_path))
    await tool.execute(_BashArgs(command="anything"))
    assert seen["thread"] is not threading.main_thread()
    assert seen["timeout"] == 60.0


def _docker_sandbox_with_slow_exec(delay: float) -> object:
    """构造跳过 __init__ 的 DockerSandbox：不需要 Docker daemon 即可测 exec 契约。"""
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = object.__new__(DockerSandbox)
    container = Mock()
    container.reload = Mock()
    container.status = "running"

    def slow_exec_run(*args, **kwargs):
        time.sleep(delay)
        return Mock(exit_code=0, output=(b"done", b""))

    container.exec_run = slow_exec_run
    sandbox._container = container
    return sandbox


def test_docker_exec_honors_timeout():
    """D10：挂起的容器命令在 timeout 到点后返回超时 ExecResult，不再永久阻塞。"""
    sandbox = _docker_sandbox_with_slow_exec(delay=30)
    t0 = time.perf_counter()
    result = sandbox.exec("hang forever", timeout=0.2)
    elapsed = time.perf_counter() - t0
    assert result.exit_code == -1
    assert "超时" in result.stderr
    assert elapsed < 5, "exec 必须在 timeout 附近返回，而不是等容器命令结束"


def test_docker_exec_passes_through_normal_result():
    """正常路径契约不变：exit_code / stdout 解码 / demux。"""
    sandbox = _docker_sandbox_with_slow_exec(delay=0)
    result = sandbox.exec("echo done", timeout=5)
    assert result.exit_code == 0
    assert result.stdout == "done"
