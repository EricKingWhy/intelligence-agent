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
import gc
import os
import shlex
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import Mock
from uuid import uuid4

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
        _python_command(script, "parent", marker, 3.0),
        timeout=1.5,
    )
    _wait_for_path(started)
    assert result.exit_code == -1
    deadline = time.monotonic() + 4.0
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
    command = _python_command(script, "parent", marker, 3.0)
    sandbox = LocalSubprocessSandbox(tmp_path)
    result = sandbox.exec(command, timeout=1.5)

    assert started.exists(), "child 未确认启动，marker 测试无法证明整树终止"
    assert result.exit_code == -1
    assert "超时" in result.stderr
    assert "stdout-before" in result.stdout
    assert "stderr-before" in result.stderr
    deadline = time.monotonic() + 4.0
    while marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not marker.exists(), "timeout 后 child/grandchild 仍写入 marker"

    partial = sandbox.exec(_python_command(output_script), timeout=1.5)
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
async def test_executor_owns_the_absolute_bash_deadline(tmp_path):
    """Executor creates the one absolute deadline consumed by Bash/Sandbox."""
    from time import perf_counter

    from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry

    seen: dict[str, float | None] = {}

    class _DeadlineProbeSandbox(LocalSubprocessSandbox):
        def exec(
            self, command, *, timeout=None, deadline=None,
            cancel_event=None, on_output=None,
        ):
            seen["timeout"] = timeout
            seen["deadline"] = deadline
            return ExecResult(exit_code=0, stdout="done", stderr="", duration_ms=0.0)

    class _TimedBash(BashTool):
        @property
        def timeout_seconds(self) -> float:
            return 0.2

    registry = ToolRegistry()
    registry.register(_TimedBash(_DeadlineProbeSandbox(tmp_path)))
    executor = ToolExecutor(registry, policy=PermissionPolicy.DANGER_FULL_ACCESS)

    started = perf_counter()
    execution = await executor.execute({
        "id": "executor-deadline-owner",
        "name": "bash",
        "args": {"command": "anything"},
    })

    assert execution.result.ok is True
    assert seen["timeout"] == 0.2
    assert seen["deadline"] is not None
    assert started < seen["deadline"] <= perf_counter() + 0.2


@pytest.mark.asyncio
async def test_executor_timeout_waits_for_bash_cleanup(tmp_path):
    """Timeout is not observable until the Sandbox has finished cancellation cleanup."""
    from agent_harness.tooling import (
        PermissionPolicy,
        ToolExecutor,
        ToolRegistry,
    )
    from agent_harness.tooling.result import ErrorCode

    cleaned = threading.Event()

    class _CleanupProbeSandbox(LocalSubprocessSandbox):
        def exec(
            self, command, *, timeout=None, deadline=None,
            cancel_event=None, on_output=None,
        ):
            assert deadline is not None
            assert cancel_event is not None
            assert cancel_event.wait(1.0), "Executor timeout did not reach the Sandbox"
            time.sleep(0.05)
            cleaned.set()
            return ExecResult(
                exit_code=-1, stdout="partial", stderr="cancelled",
                duration_ms=50.0, cancelled=True,
            )

    class _TimedBash(BashTool):
        @property
        def timeout_seconds(self) -> float:
            return 0.05

    registry = ToolRegistry()
    registry.register(_TimedBash(_CleanupProbeSandbox(tmp_path)))
    executor = ToolExecutor(registry, policy=PermissionPolicy.DANGER_FULL_ACCESS)

    execution = await executor.execute({
        "id": "executor-cleanup-boundary",
        "name": "bash",
        "args": {"command": "anything"},
    })

    assert cleaned.is_set(), "ToolExecutor returned before process cleanup completed"
    assert execution.result.ok is False
    assert execution.result.error_code == ErrorCode.TIMEOUT
    assert execution.result.retryable is False
    assert execution.result.metadata["attempt"] == 1


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
            self.seen_deadline = None

        def exec(
            self, command, *, timeout=None, deadline=None,
            cancel_event=None, on_output=None,
        ):
            self.seen_timeout = timeout
            self.seen_deadline = deadline
            return super().exec(
                command,
                timeout=timeout,
                deadline=deadline,
                cancel_event=cancel_event,
                on_output=on_output,
            )

    sandbox = _ShortDeadlineSandbox(tmp_path)

    class _TimedBash(BashTool):
        @property
        def timeout_seconds(self) -> float:
            return 2.5

    registry = ToolRegistry()
    registry.register(_TimedBash(sandbox))
    executor = ToolExecutor(registry, policy=PermissionPolicy.DANGER_FULL_ACCESS)

    execution = await executor.execute({
        "id": "executor-timeout",
        "name": "bash",
        "args": {"command": _python_command(script, "parent", marker, 4.0)},
    })

    _wait_for_path(started)
    assert sandbox.seen_timeout == 2.5
    assert sandbox.seen_deadline is not None
    assert execution.result.ok is False
    assert execution.result.error_code == "TIMEOUT"
    assert execution.result.retryable is False
    assert execution.result.metadata["attempt"] == 1
    deadline = time.monotonic() + 5.0
    while marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_bash_tool_offloads_exec_to_worker_thread(tmp_path):
    """tool 边界把同步 sandbox.exec 卸载到工作线程——event loop 不被长命令冻结。"""
    seen: dict = {}

    class ProbeSandbox(LocalSubprocessSandbox):
        def exec(
            self, command, *, timeout=None, deadline=None,
            cancel_event=None, on_output=None,
        ):
            seen["thread"] = threading.current_thread()
            seen["timeout"] = timeout
            return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=0.0)

    tool = BashTool(ProbeSandbox(tmp_path))
    await tool.execute(_BashArgs(command="anything"))
    assert seen["thread"] is not threading.main_thread()
    assert seen["timeout"] == 60.0


class _SlowDockerExec:
    def __init__(self, delay: float):
        self.delay = delay
        self.killed = threading.Event()
        self.started = threading.Event()

    def exec_create(self, container_id, command, **kwargs):
        return {"Id": "exec-slow"}

    def exec_start(self, exec_id, **kwargs):
        self.started.set()
        yield (None, b"\x1eAH_PID:1234\x1f\n")
        if self.delay:
            self.killed.wait(self.delay)
        if not self.killed.is_set():
            yield (b"done", b"")

    def exec_inspect(self, exec_id):
        return {"Pid": 1234, "Running": not self.killed.is_set(), "ExitCode": 0}


def _docker_sandbox_with_slow_exec(delay: float) -> object:
    """构造统一 low-level Docker exec fake，不需要 Docker daemon。"""
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = object.__new__(DockerSandbox)
    api = _SlowDockerExec(delay)
    container = Mock(id="container-1", status="running")

    def kill_exec(command, **kwargs):
        api.killed.set()
        return Mock(exit_code=0)

    container.exec_run = kill_exec
    sandbox._container = container
    sandbox._client = Mock(api=api)
    sandbox._exec_lock = threading.Lock()
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


def test_docker_exec_create_exception_is_not_timeout_result():
    """Docker API failure must remain an exception, not a successful Bash result."""
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = object.__new__(DockerSandbox)
    container = Mock(id="container-1", status="running")
    container.kill = Mock()
    api = Mock()
    api.exec_create.side_effect = RuntimeError("daemon unavailable")
    sandbox._container = container
    sandbox._client = Mock(api=api)
    sandbox._exec_lock = threading.Lock()

    with pytest.raises(RuntimeError, match="daemon unavailable"):
        sandbox.exec("echo never", timeout=1)
    container.kill.assert_not_called()


class _BlockingDockerExecCreate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.started = False

    def exec_create(self, container_id, command, **kwargs):
        self.entered.set()
        self.release.wait()
        return {"Id": "exec-blocked-create"}

    def exec_start(self, exec_id, **kwargs):
        self.started = True
        yield (b"should not start", None)


def test_docker_exec_create_late_result_is_reaped_after_cancel():
    """A late exec_create result must trigger container cleanup after cancel."""
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = object.__new__(DockerSandbox)
    api = _BlockingDockerExecCreate()
    container = Mock(id="container-1", status="running")
    container.kill = Mock()
    container.wait = Mock()
    sandbox._container = container
    sandbox._client = Mock(api=api)
    sandbox._exec_lock = threading.Lock()
    cancel_event = threading.Event()
    results: list[ExecResult] = []
    thread = threading.Thread(
        target=lambda: results.append(
            sandbox.exec("touch /workspace/late", timeout=5, cancel_event=cancel_event)
        )
    )
    thread.start()
    assert api.entered.wait(1)
    cancel_event.set()
    try:
        thread.join(0.2)
        assert not thread.is_alive(), "exec_create cancellation must not wait for the Docker API call"
        assert len(results) == 1
        assert results[0].cancelled is True
        assert not api.started
    finally:
        api.release.set()
        thread.join(2)

    for _ in range(50):
        if container.kill.called:
            break
        time.sleep(0.02)
    container.kill.assert_called_once_with()
    container.wait.assert_called_once_with()


class _LateAbandonedDockerExecCreate:
    """`exec_create` 在调用方已经放弃之后才返回结果。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.started = False

    def exec_create(self, container_id, command, **kwargs):
        self.entered.set()
        self.release.wait()
        return {"Id": "exec-late-abandoned"}

    def exec_start(self, exec_id, **kwargs):
        self.started = True
        yield (b"should not start", None)


def _sandbox_with_production_exec_state(api, container):
    """构造与 `__init__` 同形的 sandbox（含 `_exec_state` / `_exec_lock`）。

    上一个「late result」用例用 bare `object.__new__` 只设了 `_container` /
    `_client` / `_exec_lock`，**漏了 `_exec_state`** ⇒ 它命中 `state is None` 的
    内联捷径，从未覆盖生产一定会走的「排队」路径。这正是 #258 P2 长期未被测到的原因。
    """
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = object.__new__(DockerSandbox)
    sandbox._container = container
    sandbox._client = Mock(api=api)
    # 唯一名字：避免命中类级 WeakValueDictionary / poisoned 状态
    sandbox._container_name = f"container-late-{uuid4().hex[:8]}"
    sandbox._exec_state = DockerSandbox._state_for_container(sandbox._container_name)
    sandbox._exec_lock = sandbox._exec_state.lock
    return sandbox


def test_abandoned_sandbox_reaps_late_exec_without_a_second_exec():
    """#258 P2：迟到的 exec_create 必须在**没有任何后续 exec()** 时也被回收。

    红证（修复前）：清理只排进 `cleanup_pending`，而唯一的出队点是下一次
    `exec()` 的开头；sandbox 被遗弃（会话结束、用户不再下命令）时，容器里那个
    迟到的进程永远不会被停 ⇒ 容器泄漏。本用例全程不发起第二次 exec()。
    """
    api = _LateAbandonedDockerExecCreate()
    container = Mock(id="container-1", status="running")
    container.kill = Mock()
    container.wait = Mock()
    sandbox = _sandbox_with_production_exec_state(api, container)
    state = sandbox._exec_state

    cancel_event = threading.Event()
    results: list[ExecResult] = []
    thread = threading.Thread(
        target=lambda: results.append(
            sandbox.exec("sleep 999", timeout=5, cancel_event=cancel_event)
        )
    )
    thread.start()
    assert api.entered.wait(2)
    cancel_event.set()
    thread.join(2)
    assert not thread.is_alive(), "取消不得等待 Docker API 调用返回"
    assert len(results) == 1 and results[0].cancelled is True
    assert not api.started, "迟到的 exec 不得被 start"
    # 结果还没到达 ⇒ 此刻什么都还没排队（排队发生在 on_late_result 里）
    assert state.cleanup_pending is False
    assert not container.kill.called, "结果到达前不得有任何清理"

    api.release.set()  # 迟到结果现在才到达
    try:
        # 关键：从这里到用例结束**不发起第二次 exec()**。
        # 修复前：清理只排进 `cleanup_pending`，而唯一的出队点是下一次 exec()
        # 的开头 ⇒ 下面这个循环会空转到超时，kill 永不被调用。
        for _ in range(150):
            if container.kill.called:
                break
            time.sleep(0.02)
        container.kill.assert_called_once_with()
        container.wait.assert_called_once_with()
        for _ in range(100):
            if not state.cleanup_pending:
                break
            time.sleep(0.02)
        assert state.cleanup_pending is False, "排队的清理没有被消费（永久 pending）"
        assert state.late_cleanup is None
    finally:
        api.release.set()
        thread.join(2)


def test_docker_exec_start_exception_kills_container():
    """A stream-start failure must stop a possibly running exec/container."""
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = object.__new__(DockerSandbox)
    container = Mock(id="container-1", status="running")
    api = Mock()
    api.exec_create.return_value = {"Id": "exec-error"}
    api.exec_start.side_effect = RuntimeError("stream unavailable")
    sandbox._container = container
    sandbox._client = Mock(api=api)
    sandbox._exec_lock = threading.Lock()

    with pytest.raises(RuntimeError, match="stream unavailable"):
        sandbox.exec("touch /workspace/should-not-run", timeout=1)
    container.kill.assert_called_once_with()


def test_docker_exec_passes_through_normal_result():
    """正常路径契约不变：exit_code / stdout 解码 / demux。"""
    sandbox = _docker_sandbox_with_slow_exec(delay=0)
    result = sandbox.exec("echo done", timeout=5)
    assert result.exit_code == 0
    assert result.stdout == "done"


class _StreamingDockerExec:
    def __init__(self):
        self.killed = threading.Event()
        self.started = threading.Event()
        self.kill_commands: list[list[str]] = []

    def exec_create(self, container_id, command, **kwargs):
        self.command = command
        self.create_kwargs = kwargs
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, **kwargs):
        assert kwargs == {"stream": True, "demux": True}
        self.started.set()
        yield (b"stdout-before\n", b"\x1eAH_PID:1234\x1f\nstderr-before\n")
        while not self.killed.wait(0.01):
            yield (b"late\n", None)

    def exec_inspect(self, exec_id):
        return {
            "Pid": 1234,
            "Running": not self.killed.is_set(),
            "ExitCode": -9 if self.killed.is_set() else 0,
        }


def _docker_sandbox_with_streaming_exec(*, ignore_term: bool = False):
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = object.__new__(DockerSandbox)
    api = _StreamingDockerExec()
    container = Mock(id="container-1", status="running")

    def kill_exec(command, **kwargs):
        api.kill_commands.append(command)
        shell_command = command[2]
        if "test -s" in shell_command:
            return Mock(exit_code=0, output=(b"", b""))
        if "signal=$2" in shell_command and (
            command[-1] == "KILL" or not ignore_term
        ):
            api.killed.set()
        return Mock(exit_code=0, output=(b"", b""))

    container.exec_run = kill_exec
    sandbox._container = container
    sandbox._client = Mock(api=api)
    sandbox._exec_lock = threading.Lock()
    return sandbox, api


class _ConcurrentDockerExec:
    def __init__(self):
        self.started_ids: list[str] = []
        self.first_stopped = threading.Event()
        self.next_id = 0

    def exec_create(self, container_id, command, **kwargs):
        self.next_id += 1
        return {"Id": f"exec-{self.next_id}"}

    def exec_start(self, exec_id, **kwargs):
        self.started_ids.append(exec_id)
        yield (None, b"\x1eAH_PID:1234\x1f\n")
        if exec_id == "exec-1":
            while not self.first_stopped.wait(0.01):
                yield (None, b"")
        else:
            yield (b"second done", None)

    def exec_inspect(self, exec_id):
        return {"ExitCode": 0}


def _docker_sandbox_with_concurrent_exec():
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = object.__new__(DockerSandbox)
    api = _ConcurrentDockerExec()
    container = Mock(id="container-1", status="running")
    sandbox._container_name = "container-exec-lock-test"
    sandbox._exec_state = DockerSandbox._state_for_container(sandbox._container_name)

    def kill_exec(command, **kwargs):
        api.first_stopped.set()
        return Mock(exit_code=0)

    container.exec_run = kill_exec
    container.kill = Mock(side_effect=api.first_stopped.set)
    container.wait = Mock(return_value={"StatusCode": 137})
    sandbox._container = container
    sandbox._client = Mock(api=api)
    sandbox._exec_lock = sandbox._exec_state.lock
    return sandbox, api


def test_docker_timeout_does_not_kill_a_concurrent_exec():
    """A timed-out command cannot kill another exec using the same container."""
    sandbox, api = _docker_sandbox_with_concurrent_exec()
    from agent_harness.sandbox.docker import DockerSandbox

    other_sandbox = object.__new__(DockerSandbox)
    other_sandbox._container_name = sandbox._container_name
    other_sandbox._exec_state = DockerSandbox._state_for_container(other_sandbox._container_name)
    other_sandbox._exec_lock = other_sandbox._exec_state.lock
    other_sandbox._container = sandbox._container
    other_sandbox._client = sandbox._client
    results: dict[str, ExecResult] = {}
    second_attempted = threading.Event()
    first_thread = threading.Thread(
        target=lambda: results.setdefault("first", sandbox.exec("first", timeout=0.6))
    )
    first_thread.start()
    deadline = time.monotonic() + 1
    while not api.started_ids and time.monotonic() < deadline:
        time.sleep(0.01)
    assert api.started_ids == ["exec-1"]

    def run_second():
        second_attempted.set()
        results["second"] = other_sandbox.exec("second", timeout=3)

    second_thread = threading.Thread(target=run_second)
    second_thread.start()
    try:
        assert second_attempted.wait(1)
        deadline = time.monotonic() + 0.2
        while len(api.started_ids) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert api.started_ids == ["exec-1"]
    finally:
        first_thread.join(3)
        second_thread.join(3)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert api.started_ids == ["exec-1", "exec-2"]
    assert results["first"].exit_code == -1
    assert results["second"].exit_code == 0
    assert results["second"].stdout == "second done"


class _LargeDockerExec:
    def exec_create(self, container_id, command, **kwargs):
        return {"Id": "exec-large"}

    def exec_start(self, exec_id, **kwargs):
        yield (b"x" * 2_000_100, b"y" * 2_000_100)

    def exec_inspect(self, exec_id):
        return {"Running": False, "ExitCode": 0}


def _docker_sandbox_with_large_output():
    from agent_harness.sandbox.docker import DockerSandbox

    sandbox = object.__new__(DockerSandbox)
    sandbox._container = Mock(id="container-1", status="running")
    sandbox._container.exec_run = Mock(return_value=Mock(exit_code=0, output=b""))
    sandbox._client = Mock(api=_LargeDockerExec())
    sandbox._exec_lock = threading.Lock()
    return sandbox


def test_docker_output_is_capped_per_channel():
    """Docker output follows Local's 2M-per-channel cap and truncation marker."""
    result = _docker_sandbox_with_large_output().exec("large", timeout=5)

    assert len(result.stdout) <= 2_000_100
    assert len(result.stderr) <= 2_000_100
    assert "stdout 超过捕获上限" in result.stdout
    assert "stderr 超过捕获上限" in result.stderr


def test_docker_incomplete_pid_marker_keeps_stderr():
    """Stderr before an incomplete PID marker remains visible at stream EOF."""
    from agent_harness.sandbox.docker import DockerSandbox

    class _MissingMarkerExec:
        def exec_create(self, container_id, command, **kwargs):
            return {"Id": "exec-missing-marker"}

        def exec_start(self, exec_id, **kwargs):
            yield (None, b"stderr-before\x1eAH_PID:1234")

        def exec_inspect(self, exec_id):
            return {"ExitCode": 0}

    sandbox = object.__new__(DockerSandbox)
    sandbox._container = Mock(id="container-1", status="running")
    sandbox._client = Mock(api=_MissingMarkerExec())
    sandbox._exec_lock = threading.Lock()

    result = sandbox.exec("printf done", timeout=1)

    assert result.exit_code == 0
    assert "stderr-before" in result.stderr
    assert "AH_PID:1234" in result.stderr


def test_docker_timeout_kills_exec_and_keeps_partial_output():
    """Timeout terminates the container exec and retains output emitted before it."""
    sandbox, api = _docker_sandbox_with_streaming_exec()
    result = sandbox.exec("sleep forever", timeout=0.1)

    assert api.started.is_set()
    assert api.command[:4] == ["setsid", "-w", "/bin/sh", "-lc"]
    assert api.create_kwargs["workdir"] == "/workspace"
    assert api.kill_commands
    assert any("kill -$signal -- -$1" in command[2] for command in api.kill_commands)
    assert result.exit_code == -1
    assert "stdout-before" in result.stdout
    assert "stderr-before" in result.stderr
    assert "超时" in result.stderr
    assert result.cancelled is False


def test_docker_timeout_fails_if_container_stop_cannot_be_confirmed():
    """Do not report timeout cleanup as complete when Docker cannot confirm container exit."""
    sandbox, _ = _docker_sandbox_with_streaming_exec()
    sandbox._container.kill = Mock(side_effect=RuntimeError("kill unavailable"))
    sandbox._container.wait = Mock(side_effect=RuntimeError("wait unavailable"))

    with pytest.raises(RuntimeError, match="cleanup.*confirmed"):
        sandbox.exec("sleep forever", timeout=0.1)

    sandbox._container.kill.assert_called_once_with()
    sandbox._container.wait.assert_called_once_with()
    with pytest.raises(RuntimeError, match="previous cleanup was unconfirmed"):
        sandbox.exec("must not run", timeout=1)
    sandbox._container.reload.assert_called_once_with()


def test_docker_cleanup_failure_guard_survives_sandbox_collection():
    """A failed cleanup remains fail-closed if its original sandbox is collected."""
    from agent_harness.sandbox.docker import DockerSandbox

    container_name = f"cleanup-failed-{time.monotonic_ns()}"
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = container_name
    sandbox._exec_state = DockerSandbox._state_for_container(container_name)
    sandbox._mark_exec_cleanup_failed()

    del sandbox
    gc.collect()

    recovered = object.__new__(DockerSandbox)
    recovered._container_name = container_name
    recovered._exec_state = DockerSandbox._state_for_container(container_name)
    recovered._exec_lock = recovered._exec_state.lock

    with pytest.raises(RuntimeError, match="previous cleanup was unconfirmed"):
        recovered._assert_exec_cleanup_healthy()


def test_docker_cancel_kills_exec_and_marks_cancelled():
    """cancel_event terminates the same exec path and marks the result cancelled."""
    sandbox, api = _docker_sandbox_with_streaming_exec()
    cancel_event = threading.Event()
    result_holder: list[ExecResult] = []
    thread = threading.Thread(
        target=lambda: result_holder.append(
            sandbox.exec("sleep forever", timeout=5, cancel_event=cancel_event)
        )
    )
    thread.start()
    assert api.started.wait(2)
    cancel_event.set()
    thread.join(5)

    assert not thread.is_alive()
    assert len(result_holder) == 1
    assert api.kill_commands
    assert result_holder[0].exit_code == -1
    assert result_holder[0].cancelled is True
    assert "stdout-before" in result_holder[0].stdout
    assert "stderr-before" in result_holder[0].stderr
    assert "取消" in result_holder[0].stderr


def test_docker_timeout_escalates_when_term_is_ignored():
    """TERM delivery alone is insufficient; cleanup must escalate to KILL."""
    sandbox, api = _docker_sandbox_with_streaming_exec(ignore_term=True)
    result = sandbox.exec("sleep forever", timeout=0.1)

    assert result.exit_code == -1
    signal_commands = [
        command for command in api.kill_commands
        if "signal=$2" in command[2]
    ]
    assert len(signal_commands) >= 2
    assert signal_commands[0][-1] == "TERM"
    assert signal_commands[-1][-1] == "KILL"
