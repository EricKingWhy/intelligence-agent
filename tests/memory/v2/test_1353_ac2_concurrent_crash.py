"""#1353 AC2：并发写安全（fcntl 锁 + tempfile/os.replace 原子写 + kill -9）。

判据不是“跑完没报错”，而是：
- 并发进程各写 N 条后，磁盘条目数恰好等于总量（无丢写）；
- SIGKILL 落在原子替换窗口时，旧文件逐字节不变、无半截条目；
- 随机 SIGKILL 压力下，任何时刻重新解析文件都必须成功且条目完整；
- 锁文件 0600，且拒绝符号链接（O_NOFOLLOW）。

Windows 上共跳过四条：0600 权限位那条（无 POSIX 权限位）、符号链接那条
（无 `os.O_NOFOLLOW`），以及两条 SIGKILL 用例（`signal.SIGKILL` 不存在、
`process.kill()` 走 TerminateProcess 拿不到负 returncode）。
跨进程不丢写那条（`test_concurrent_processes_do_not_lose_writes`）不依赖任何
POSIX 专有语义，是全平台等价用例（#1384）。
"""

from __future__ import annotations

import inspect
import os
import random
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.memory.v2._markdown_memory_1353 import (
    build_store,
    call,
    load,
    path_of,
    read_entries,
)

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKER = Path(__file__).resolve().parent / "_1353_store_worker.py"

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限位 / SIGKILL")


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    # 本仓 src 永远排第一（门禁用 `run_tests_clean.sh` 清空 PYTHONPATH，故不存在遮蔽）；
    # 保留父进程的条目只为「用另一棵 worktree 的实现在本树预览」时不至于把子进程打成
    # `#1353 未找到markdown 存储实现模块` 的假红。
    entries = [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    entries += [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env["PYTHONUTF8"] = "1"
    for name in ("NO_PROXY", "no_proxy"):
        env[name] = "localhost,127.0.0.1"
    return env


def _run_worker(root: Path, mode: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(WORKER), "--root", str(root), "--mode", mode, *extra],
        cwd=REPO_ROOT,
        env=_worker_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


async def _new_store(tmp_path, *, limit: int = 8_000):
    store = build_store(tmp_path, memory_char_limit=limit, user_char_limit=limit)
    pending = load(store)
    if inspect.isawaitable(pending):
        await pending
    return store


def _assert_complete(entries: list[str], expected_prefixes: tuple[str, ...]) -> None:
    for entry in entries:
        assert entry.startswith(expected_prefixes), f"发现半截/未知条目: {entry!r}"


@_POSIX_ONLY
async def test_lock_file_is_owner_only(tmp_path) -> None:
    store = await _new_store(tmp_path)
    assert (await call(store.add, "memory", "entry-000"))["success"] is True

    lock_path = path_of(store, "memory").with_suffix(".md.lock")
    assert lock_path.exists(), f"AC2 要求独立锁文件 MEMORY.md.lock，实际不存在: {lock_path}"
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW unavailable")
async def test_lock_symlink_is_refused(tmp_path) -> None:
    store = await _new_store(tmp_path)
    assert (await call(store.add, "memory", "entry-000"))["success"] is True
    memory_path = path_of(store, "memory")
    lock_path = memory_path.with_suffix(".md.lock")
    outside = tmp_path / "outside"
    outside.write_text("do not touch", encoding="utf-8")
    if lock_path.exists():
        lock_path.unlink()
    lock_path.symlink_to(outside)

    with pytest.raises((OSError, RuntimeError, ValueError)):
        await call(store.add, "memory", "entry-001")

    assert outside.read_text(encoding="utf-8") == "do not touch"


@_POSIX_ONLY
async def test_crash_before_atomic_replace_keeps_old_file_intact(tmp_path) -> None:
    store = await _new_store(tmp_path)
    for index in range(3):
        assert (await call(store.add, "memory", f"seed-{index:03d}"))["success"] is True
    path = path_of(store, "memory")
    before = path.read_bytes()

    completed = _run_worker(tmp_path, "crash-before-replace")

    assert completed.returncode == -signal.SIGKILL, (
        "未能在原子替换窗口注入 SIGKILL——实现可能没走 os.replace/Path.replace；"
        f"rc={completed.returncode}, stderr={completed.stderr[-800:]}"
    )
    assert path.read_bytes() == before
    assert read_entries(path) == ["seed-000", "seed-001", "seed-002"]


@_POSIX_ONLY
async def test_random_sigkill_never_leaves_partial_entry(tmp_path) -> None:
    store = await _new_store(tmp_path)
    assert (await call(store.add, "memory", "seed-000"))["success"] is True
    path = path_of(store, "memory")

    for round_index in range(6):
        process = subprocess.Popen(  # noqa: ASYNC220 — 真子进程是 kill -9 判据本身
            [
                sys.executable,
                str(WORKER),
                "--root",
                str(tmp_path),
                "--mode",
                "loop",
                "--prefix",
                f"r{round_index}",
            ],
            cwd=REPO_ROOT,
            env=_worker_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(random.uniform(0.02, 0.09))  # noqa: ASYNC251 — 给子进程进入写入循环的时间窗
        process.kill()
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == -signal.SIGKILL, (
            f"round {round_index}: worker 未处于运行态即退出 rc={process.returncode}, stderr={stderr[-500:]}"
        )

        # 任何时刻重开都必须可解析、无半截条目。
        entries = read_entries(path)
        assert entries, f"round {round_index}: 崩溃后文件不可解析/为空"
        _assert_complete(entries, ("seed-", "r0-", "r1-", "r2-", "r3-", "r4-", "r5-"))


async def test_concurrent_processes_do_not_lose_writes(tmp_path) -> None:
    store = await _new_store(tmp_path)
    assert (await call(store.add, "memory", "seed-000"))["success"] is True
    processes = 6
    per_process = 8
    children = [
        subprocess.Popen(  # noqa: ASYNC220 — 并发写必须跨进程，线程不共享 fcntl 语义
            [
                sys.executable,
                str(WORKER),
                "--root",
                str(tmp_path),
                "--mode",
                "add",
                "--prefix",
                f"p{index}",
                "--count",
                str(per_process),
            ],
            cwd=REPO_ROOT,
            env=_worker_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(processes)
    ]
    for index, child in enumerate(children):
        _stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, f"worker {index} 失败 rc={child.returncode}: {stderr[-1000:]}"

    entries = read_entries(path_of(store, "memory"))
    assert len(entries) == 1 + processes * per_process, (
        f"并发丢写：期望 {1 + processes * per_process} 条，实际 {len(entries)} 条"
    )
    assert len(set(entries)) == len(entries), "并发写产生重复条目"
    _assert_complete(entries, ("seed-", "p0-", "p1-", "p2-", "p3-", "p4-", "p5-"))
