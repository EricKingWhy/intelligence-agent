"""单实例锁：同一 session root 的第二个**进程**必须启动期响亮失败。

这些用例大量依赖真实子进程——因为要证明的正是"跨进程"语义（OS 级 advisory
lock + 进程死亡由 OS 释放）。进程内幂等那一条相反，必须同进程验证。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import agent_harness.instance_lock as instance_lock_module
from agent_harness.instance_lock import (
    ALLOW_SHARED_ROOT_ENV,
    CUTOVER_FENCE_FILENAME,
    DEFAULT_LOCK_FILENAME,
    MEMORY_INDEX_REBUILD_FENCE_FILENAME,
    InstanceLock,
    InstanceLockError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 子进程脚本：拿锁 → 写 ready 文件（便于父进程确定它已持有）→ 打印 → 持有 hold 秒。
#: 拿到就 exit 0；拿不到打印 LOCKED:<msg> 并 exit 3（与 hold 区分开）。
_CHILD = """
import sys, time
from agent_harness.instance_lock import InstanceLock, InstanceLockError

root, ready, hold = {root!r}, {ready!r}, {hold!r}
try:
    lock = InstanceLock(root).acquire()
except InstanceLockError as error:
    print("LOCKED:" + str(error), flush=True)
    sys.exit(3)
if ready:
    open(ready, "w", encoding="utf-8").write("ok")
print("ACQUIRED", flush=True)
time.sleep(hold)
lock.release()
"""


def _child_script(root: Path, *, hold: float = 0.0, ready: Path | None = None) -> str:
    return _CHILD.format(root=str(root), hold=hold, ready=str(ready) if ready else None)


def _run_child(root: Path, *, hold: float = 0.0, env: dict | None = None) -> subprocess.CompletedProcess:
    # harness 拥有子进程 stdout 编码：强制 PYTHONIOENCODING=utf-8（覆盖 ambient，
    # Windows GA 下 ambient 是 cp1252，子进程打印中文锁错误会 UnicodeEncodeError）。
    # 父进程显式 encoding="utf-8" 解码，不依赖父进程 locale（Windows GA 是 cp1252，
    # 否则会把 UTF-8 字节按 cp1252 解出 mojibake）。调用方传入的其他 env key 保留。
    return subprocess.run(
        [sys.executable, "-c", _child_script(root, hold=hold)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        cwd=str(REPO_ROOT), check=False,
        env={**os.environ, **(env or {}), "PYTHONIOENCODING": "utf-8"},
    )


def _wait_for_ready(ready: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.exists():
            return
        time.sleep(0.05)
    pytest.fail(f"子进程未在 {timeout}s 内拿到锁（ready 文件未出现）")


def _run_cli(args: list[str], root: Path) -> subprocess.CompletedProcess:
    """跑真实 CLI 入口（WORKSPACE_DIR 指向 root，确保打的是同一把锁）。"""
    return subprocess.run(
        [sys.executable, "-m", "agent_harness.cli", *args],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT), check=False,
        env={**os.environ, "WORKSPACE_DIR": str(root)},
    )


def test_second_process_fails_loudly_and_names_the_path(tmp_path: Path) -> None:
    """核心 AC：第二个进程拿不到锁 → 响亮失败，且错误信息点名路径（可诊断）。"""
    holder = InstanceLock(tmp_path).acquire()
    try:
        child = _run_child(tmp_path)
        assert child.returncode == 3, f"子进程应响亮失败，实际 rc={child.returncode}\n{child.stdout}\n{child.stderr}"
        assert "LOCKED:" in child.stdout
        assert str(tmp_path) in child.stdout, "错误信息必须点名锁路径，便于用户定位"
    finally:
        holder.release()


def test_other_process_can_acquire_after_release(tmp_path: Path) -> None:
    """release 后必须真正可再次获取（否则就是"锁住不放"的死锁）。"""
    InstanceLock(tmp_path).acquire().release()
    child = _run_child(tmp_path)
    assert child.returncode == 0, f"{child.stdout}\n{child.stderr}"
    assert "ACQUIRED" in child.stdout


def test_lock_is_auto_released_when_holder_process_dies(tmp_path: Path) -> None:
    """OS 级 advisory lock 的意义：进程被杀后由 OS 释放，不留残留锁。

    这正是"不用裸 pidfile"的理由——残留锁会让服务永久起不来。
    """
    ready = tmp_path / "child-ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _child_script(tmp_path, hold=60, ready=ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        _wait_for_ready(ready)
        with pytest.raises(InstanceLockError):
            InstanceLock(tmp_path).acquire()
    finally:
        proc.kill()
        proc.wait(timeout=30)

    # 子进程已死 → 锁必须自动可用（父进程此刻应能拿到）
    lock = InstanceLock(tmp_path).acquire()
    lock.release()


def test_acquire_is_idempotent_within_one_process(tmp_path: Path) -> None:
    """同进程重复 acquire 返回同一把锁：一个进程只需一把，重复装配/测试不得自锁。"""
    first = InstanceLock(tmp_path).acquire()
    second = InstanceLock(tmp_path).acquire()
    assert first is second
    first.release()


def test_lock_survives_partial_release_until_last_holder_releases(tmp_path: Path) -> None:
    """引用计数：两个持有者各 release 一次，锁要到最后一次才真正释放。"""
    first = InstanceLock(tmp_path).acquire()
    second = InstanceLock(tmp_path).acquire()
    first.release()
    assert _run_child(tmp_path).returncode == 3, "还有持有者时不得放锁"
    second.release()
    assert _run_child(tmp_path).returncode == 0, "全部释放后必须可获取"


def test_lock_file_records_holder_for_diagnosis(tmp_path: Path) -> None:
    """锁文件写入持有者 pid：第二个进程的错误信息才可能指出"谁在占用"。"""
    lock = InstanceLock(tmp_path).acquire()
    try:
        content = (tmp_path / DEFAULT_LOCK_FILENAME).read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in content
    finally:
        lock.release()


def test_creates_missing_root_directory(tmp_path: Path) -> None:
    """root 可能尚未创建（首次启动）——取锁不能因为目录不存在而失败。"""
    nested = tmp_path / "a" / "b"
    lock = InstanceLock(nested).acquire()
    try:
        assert (nested / DEFAULT_LOCK_FILENAME).exists()
    finally:
        lock.release()


def test_cutover_fence_blocks_application_but_allows_maintenance_resume(tmp_path: Path) -> None:
    (tmp_path / CUTOVER_FENCE_FILENAME).write_text("pending", encoding="utf-8")

    with pytest.raises(InstanceLockError, match="reset is incomplete"):
        InstanceLock(tmp_path).acquire()

    maintenance = InstanceLock(tmp_path, allow_cutover=True).acquire()
    maintenance.release()


def test_memory_index_rebuild_fence_blocks_application_and_escape_hatch(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / MEMORY_INDEX_REBUILD_FENCE_FILENAME).write_text("pending", encoding="utf-8")

    with pytest.raises(InstanceLockError, match="index rebuild is incomplete"):
        InstanceLock(tmp_path).acquire()

    with pytest.raises(InstanceLockError, match="index rebuild is incomplete"):
        InstanceLock(tmp_path, allow_cutover=True).acquire()

    monkeypatch.setenv(ALLOW_SHARED_ROOT_ENV, "1")
    with pytest.raises(InstanceLockError, match="index rebuild is incomplete"):
        InstanceLock(tmp_path).acquire()
    assert list(tmp_path.glob(".instance-shared-root-*.lease")) == []

    maintenance = InstanceLock(tmp_path, allow_memory_index_rebuild=True).acquire()
    maintenance.release()


def test_rebuild_maintenance_and_application_cannot_reuse_same_process_lock(
    tmp_path: Path,
) -> None:
    application = InstanceLock(tmp_path).acquire()
    try:
        with pytest.raises(InstanceLockError, match="different operation"):
            InstanceLock(tmp_path, allow_memory_index_rebuild=True).acquire()
    finally:
        application.release()

    maintenance = InstanceLock(tmp_path, allow_memory_index_rebuild=True).acquire()
    try:
        with pytest.raises(InstanceLockError, match="different operation"):
            InstanceLock(tmp_path).acquire()
    finally:
        maintenance.release()


def test_memory_index_rebuild_maintenance_still_refuses_cutover_fence(
    tmp_path: Path,
) -> None:
    (tmp_path / MEMORY_INDEX_REBUILD_FENCE_FILENAME).write_text("pending", encoding="utf-8")
    (tmp_path / CUTOVER_FENCE_FILENAME).write_text("pending", encoding="utf-8")

    with pytest.raises(InstanceLockError, match="reset is incomplete"):
        InstanceLock(tmp_path, allow_memory_index_rebuild=True).acquire()


def test_cutover_and_index_rebuild_maintenance_modes_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    (tmp_path / CUTOVER_FENCE_FILENAME).write_text("pending", encoding="utf-8")
    (tmp_path / MEMORY_INDEX_REBUILD_FENCE_FILENAME).write_text(
        "pending", encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mutually exclusive"):
        InstanceLock(
            tmp_path,
            allow_cutover=True,
            allow_memory_index_rebuild=True,
        ).acquire()


def test_rebuild_maintenance_publishes_fence_then_detects_active_bypass_writer(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv(ALLOW_SHARED_ROOT_ENV, raising=False)
    holder = InstanceLock(tmp_path).acquire()
    ready = tmp_path / "shared-root-ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _child_script(tmp_path, hold=60, ready=ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env={**os.environ, ALLOW_SHARED_ROOT_ENV: "1", "PYTHONIOENCODING": "utf-8"},
    )
    maintenance = None
    try:
        _wait_for_ready(ready)
        assert len(list(tmp_path.glob(".instance-shared-root-*.lease"))) == 1
        holder.release()

        maintenance = InstanceLock(
            tmp_path, allow_memory_index_rebuild=True,
        ).acquire()
        (tmp_path / MEMORY_INDEX_REBUILD_FENCE_FILENAME).write_text(
            "pending", encoding="utf-8",
        )
        with pytest.raises(InstanceLockError, match="workspace writer is active") as error:
            maintenance.assert_no_shared_root_writers()
        assert "maintenance" not in str(error.value)
    finally:
        proc.kill()
        proc.wait(timeout=30)
        holder.release()
        if maintenance is not None:
            (tmp_path / MEMORY_INDEX_REBUILD_FENCE_FILENAME).unlink(missing_ok=True)
            maintenance.release()


def test_escape_hatch_rechecks_rebuild_fence_after_registering_lease(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv(ALLOW_SHARED_ROOT_ENV, "1")
    real_take_lock = instance_lock_module._take_os_lock
    first_attempt = True

    def simulate_contention_once(fd: int) -> None:
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            raise OSError("simulated existing lock holder")
        real_take_lock(fd)

    real_create_lease = InstanceLock._create_shared_root_lease

    def create_lease_then_publish_fence(lock: InstanceLock) -> tuple[Path, int]:
        lease = real_create_lease(lock)
        (tmp_path / MEMORY_INDEX_REBUILD_FENCE_FILENAME).write_text(
            "pending", encoding="utf-8",
        )
        return lease

    monkeypatch.setattr(instance_lock_module, "_take_os_lock", simulate_contention_once)
    monkeypatch.setattr(
        InstanceLock, "_create_shared_root_lease", create_lease_then_publish_fence,
    )

    with pytest.raises(InstanceLockError, match="index rebuild is in progress"):
        InstanceLock(tmp_path).acquire()

    assert list(tmp_path.glob(".instance-shared-root-*.lease")) == []


def test_cutover_and_application_cannot_reuse_each_others_same_process_lock(
    tmp_path: Path,
) -> None:
    application = InstanceLock(tmp_path).acquire()
    try:
        with pytest.raises(InstanceLockError, match="different operation"):
            InstanceLock(tmp_path, allow_cutover=True).acquire()
    finally:
        application.release()

    maintenance = InstanceLock(tmp_path, allow_cutover=True).acquire()
    try:
        with pytest.raises(InstanceLockError, match="different operation"):
            InstanceLock(tmp_path).acquire()
    finally:
        maintenance.release()


def test_escape_hatch_downgrades_to_warning(tmp_path: Path) -> None:
    """逃生门：显式设 ALLOW_SHARED_ROOT 时降级为警告（默认安全，必须显式开启）。"""
    holder = InstanceLock(tmp_path).acquire()
    try:
        child = _run_child(tmp_path, env={ALLOW_SHARED_ROOT_ENV: "1"})
        assert child.returncode == 0, f"设了逃生门就该放行，实际 rc={child.returncode}\n{child.stdout}"
        assert "WARNING" in child.stderr or "WARNING" in child.stdout
        assert list(tmp_path.glob(".instance-shared-root-*.lease")) == []
    finally:
        holder.release()


def test_escape_hatch_lease_blocks_cutover_until_writer_exits(tmp_path: Path) -> None:
    holder = InstanceLock(tmp_path).acquire()
    ready = tmp_path / "shared-root-ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _child_script(tmp_path, hold=60, ready=ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env={**os.environ, ALLOW_SHARED_ROOT_ENV: "1", "PYTHONIOENCODING": "utf-8"},
    )
    try:
        _wait_for_ready(ready)
        assert len(list(tmp_path.glob(".instance-shared-root-*.lease"))) == 1
        with pytest.raises(InstanceLockError, match="workspace writer is active"):
            InstanceLock(tmp_path).assert_no_shared_root_writers()
    finally:
        proc.kill()
        proc.wait(timeout=30)
        holder.release()

    InstanceLock(tmp_path).assert_no_shared_root_writers()
    assert list(tmp_path.glob(".instance-shared-root-*.lease")) == []


def test_normal_startup_refuses_active_shared_root_lease_after_primary_releases(
    tmp_path: Path,
) -> None:
    holder = InstanceLock(tmp_path).acquire()
    ready = tmp_path / "shared-root-ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _child_script(tmp_path, hold=60, ready=ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env={**os.environ, ALLOW_SHARED_ROOT_ENV: "1", "PYTHONIOENCODING": "utf-8"},
    )
    try:
        _wait_for_ready(ready)
        assert len(list(tmp_path.glob(".instance-shared-root-*.lease"))) == 1
        holder.release()

        normal_startup = _run_child(tmp_path, env={ALLOW_SHARED_ROOT_ENV: ""})
        assert normal_startup.returncode == 3
        assert "workspace writer is active" in normal_startup.stdout
    finally:
        proc.kill()
        proc.wait(timeout=30)
        holder.release()

    next_startup = _run_child(tmp_path, env={ALLOW_SHARED_ROOT_ENV: ""})
    assert next_startup.returncode == 0, f"{next_startup.stdout}\n{next_startup.stderr}"
    assert list(tmp_path.glob(".instance-shared-root-*.lease")) == []


def test_lease_scan_cannot_remove_a_writer_still_registering(tmp_path: Path) -> None:
    holder = InstanceLock(tmp_path).acquire()
    ready = tmp_path / "lease-created"
    proceed = tmp_path / "allow-lease-lock"
    registered = tmp_path / "lease-registered"
    finish = tmp_path / "finish-writer"
    script = f"""
import time
from pathlib import Path
import agent_harness.instance_lock as locks

root = Path({str(tmp_path)!r})
ready = Path({str(ready)!r})
proceed = Path({str(proceed)!r})
registered = Path({str(registered)!r})
finish = Path({str(finish)!r})
real_take_os_lock = locks._take_os_lock

def pause_before_lease_lock(fd):
    if list(root.glob(".instance-shared-root-*.lease")) and not ready.exists():
        ready.write_text("ready", encoding="utf-8")
        while not proceed.exists():
            time.sleep(0.01)
    real_take_os_lock(fd)

locks._take_os_lock = pause_before_lease_lock
holder = locks.InstanceLock(root).acquire()
registered.write_text("registered", encoding="utf-8")
while not finish.exists():
    time.sleep(0.01)
holder.release()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env={**os.environ, ALLOW_SHARED_ROOT_ENV: "1", "PYTHONIOENCODING": "utf-8"},
    )
    try:
        _wait_for_ready(ready)
        holder.release()

        during_registration = _run_child(tmp_path, env={ALLOW_SHARED_ROOT_ENV: ""})
        assert during_registration.returncode == 3
        assert "lease registry is busy" in during_registration.stdout
        assert len(list(tmp_path.glob(".instance-shared-root-*.lease"))) == 1

        proceed.touch()
        _wait_for_ready(registered)
        after_registration = _run_child(tmp_path, env={ALLOW_SHARED_ROOT_ENV: ""})
        assert after_registration.returncode == 3
        assert "workspace writer is active" in after_registration.stdout
    finally:
        proceed.touch()
        finish.touch()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
        holder.release()

    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""
    assert _run_child(tmp_path, env={ALLOW_SHARED_ROOT_ENV: ""}).returncode == 0


def test_escape_hatch_rechecks_fence_after_registering_lease(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(ALLOW_SHARED_ROOT_ENV, "1")
    real_take_lock = instance_lock_module._take_os_lock
    first_attempt = True

    def simulate_contention_once(fd: int) -> None:
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            raise OSError("simulated existing lock holder")
        real_take_lock(fd)

    real_create_lease = InstanceLock._create_shared_root_lease

    def create_lease_then_publish_fence(lock: InstanceLock) -> tuple[Path, int]:
        lease = real_create_lease(lock)
        (tmp_path / CUTOVER_FENCE_FILENAME).write_text("pending", encoding="utf-8")
        return lease

    monkeypatch.setattr(instance_lock_module, "_take_os_lock", simulate_contention_once)
    monkeypatch.setattr(
        InstanceLock, "_create_shared_root_lease", create_lease_then_publish_fence,
    )

    with pytest.raises(InstanceLockError, match="reset is in progress"):
        InstanceLock(tmp_path).acquire()

    assert list(tmp_path.glob(".instance-shared-root-*.lease")) == []


def test_release_is_idempotent(tmp_path: Path) -> None:
    """重复 release 不得把别人的锁放掉（幂等，不抛）。"""
    lock = InstanceLock(tmp_path).acquire()
    lock.release()
    lock.release()
    assert _run_child(tmp_path).returncode == 0


@pytest.mark.skipif(sys.platform != "win32", reason="大小写不敏感路径只在 Windows 上重现")
def test_lock_key_normalises_windows_path_spelling(tmp_path: Path) -> None:
    """同一目录的不同写法（大小写 / 8.3 短名）必须算同一把锁。

    只按 abspath 做 key 时，`C:\\WS` 与 `c:\\ws` 会算出两个 key → 同进程对同一
    文件开两个句柄抢锁 → 对自己报"已被占用"（自锁假阳性）。
    """
    first = InstanceLock(tmp_path).acquire()
    try:
        second = InstanceLock(Path(str(tmp_path).upper())).acquire()
        assert first is second
        second.release()
    finally:
        first.release()
    assert _run_child(tmp_path).returncode == 0, "计数归零后必须真正放锁"


def test_help_is_not_blocked_but_real_command_is(tmp_path: Path) -> None:
    """`--help` 不触碰 session root，不该被锁挡住；同环境下真实子命令必须被拒。

    后一条断言是前一条的对照：若 WORKSPACE_DIR 没生效（打到了别的根），
    `sessions` 会正常跑完，这条会红——防止"放行"被误判成"锁没生效"。
    """
    holder = InstanceLock(tmp_path).acquire()
    try:
        helped = _run_cli(["--help"], tmp_path)
        assert helped.returncode == 0, f"{helped.stdout}\n{helped.stderr}"
        assert "usage" in helped.stdout.lower()

        blocked = _run_cli(["sessions"], tmp_path)
        assert blocked.returncode == 2, (
            f"同环境下真实子命令必须被拒，实际 rc={blocked.returncode}\n{blocked.stdout}\n{blocked.stderr}"
        )
        assert "启动被拒绝" in blocked.stderr and str(tmp_path) in blocked.stderr
    finally:
        holder.release()


def test_help_prints_utf8_even_under_cp1252_console(tmp_path: Path) -> None:
    """Windows GA 回归：控制台/重定向流为 cp1252 时，`--help` 的中文帮助必须
    完整输出且不崩（rc=0）。

    产品侧在 CLI 入口把 stdout/stderr 归一到 UTF-8（见 `cli._ensure_utf8_console_streams`），
    所以这里显式给子进程 `PYTHONIOENCODING=cp1252`（复现 Windows 语义），再用**字节模式**
    抓 stdout 并按 UTF-8 **严格**解码——不指定 `text=True`，避免父进程 locale 干扰断言。
    严格解码成功即证明输出是合法 UTF-8（中文帮助完整、没被 mangling）。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "agent_harness.cli", "--help"],
        capture_output=True, timeout=60, cwd=str(REPO_ROOT), check=False,
        env={**os.environ, "WORKSPACE_DIR": str(tmp_path), "PYTHONIOENCODING": "cp1252"},
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    stdout = proc.stdout.decode("utf-8")  # 严格解码：非法字节会在这里抛，正是回归点
    assert "usage" in stdout.lower()
    assert "发送给 Agent 的任务" in stdout, "中文帮助文本必须完整输出"
