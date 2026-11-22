"""#1702：看门狗 abort 前终止**已登记**的子进程。

被测机制住在 `tests/conftest.py`（`_SubprocessTable` / `_abort` / `track_subprocess` 夹具）。
本文件**不 import** `tests.test_per_test_timeout`——那目录没有 `__init__.py`；这里自带一份
同款 `_run_probe`：临时目录 + 复制 conftest + 子进程 pytest。凡是「超时之后进程还在不在」
的断言都必须在**父进程**做：触发超时的那个进程已经被 `os._exit(1)` 打死，进程内断言没有
意义（票面 AC2 的 harness 取证要求）。

登记制边界：注册表只碰显式登记的 `Popen`；未登记进程一律不碰（用例 4 取证）。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFTEST = Path(__file__).resolve().parent / "conftest.py"

#: 探针里的长睡子进程：不杀就一直活到超时上限。
_SLEEP_FOREVER = 'import time; time.sleep(3600)'

#: 探针里的「组长」：自己再开一个同组孙进程，然后挂着——用来验证按组收后代。
_GROUP_LEADER_CODE = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'])\n"
    "open('grandchild.pid', 'w').write(str(child.pid))\n"
    "time.sleep(3600)"
)

#: 孙进程：**忽略 SIGTERM**（先装处理器、落盘 ready 标记，再长睡）。组长自己对 TERM
#: 是敏感的——组长先死、组内后代免疫 TERM 时，SIGKILL 升级若只看 leader 就会漏掉它。
_TERM_IMMUNE_CHILD_CODE = (
    "import os, pathlib, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "pathlib.Path('grandchild.pid').write_text(str(os.getpid()))\n"
    "pathlib.Path('grandchild.ready').write_text('ok')\n"
    "time.sleep(3600)"
)

#: 探针里的「TERM 敏感组长 + TERM 免疫孙进程」，组长只负责 fork 与长睡。
_TERM_IMMUNE_GROUP_CODE = (
    "import subprocess, sys, time\n"
    f"child = subprocess.Popen([sys.executable, '-c', {_TERM_IMMUNE_CHILD_CODE!r}])\n"
    "time.sleep(3600)"
)


def _run_probe(
    tmp_path: Path, body: str, *args: str, timeout: float = 90.0,
) -> subprocess.CompletedProcess[str]:
    """在仓外临时目录跑探针文件（conftest 复制过去，机制随目录生效）。"""
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text(textwrap.dedent(body), encoding="utf-8")
    env = dict(os.environ)
    # 工作树优先：venv 的 intelligence_agent.pth 指向主仓 src，不盖住会读到别的树。
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "test_probe.py", "-p", "no:cacheprovider",
         *args],
        check=False, cwd=tmp_path, capture_output=True, text=True,
        timeout=timeout, env=env,
    )


def _pid_alive(pid: int) -> bool:
    """父进程侧核查：Linux/Windows 各自查进程表（AC2 要求，不是只看退出码）。"""
    if sys.platform == "win32":  # pragma: no cover - 本机 Linux，Windows 未取证
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, check=False,
        )
        # CSV 第一列是镜像名、第二列才是 PID；子串匹配会把 1234 认成 12345。
        parsed = False
        for line in result.stdout.splitlines():
            columns = [part.strip().strip('"') for part in line.split(",")]
            if len(columns) > 1 and columns[1].isdigit():
                parsed = True
                if int(columns[1]) == pid:
                    return True
        if parsed:
            return False
        # 输出形态认不出来（本地化 / 报错文案）时退化为子串判定，保持不脆。
        return result.returncode == 0 and str(pid) in result.stdout
    result = subprocess.run(
        ["ps", "-o", "pid=,ppid=,pgid=", "-p", str(pid)],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() != ""


def _wait_pid_gone(pid: int, timeout: float = 10.0) -> bool:
    """等进程消失：被杀的子进程先变孤儿、再由 init 回收，留一条有界轮询窗口。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _kill_verified_process(pid: int) -> None:
    """父进程收尾：把「应当存活」的探针子进程杀掉，防泄漏。"""
    if not _pid_alive(pid):
        return
    if sys.platform == "win32":  # pragma: no cover - 同上
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _read_pids(tmp_path: Path) -> list[int]:
    payload = (tmp_path / "pids.txt").read_text(encoding="utf-8")
    return [int(part) for part in payload.split()]


# ---------------------------------------------------------------------------
# 1. 孤儿终止：登记后超时，子进程必须死（AC1 动作 + AC2 父进程核查）
# ---------------------------------------------------------------------------
def test_registered_orphan_is_killed_at_abort(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        f"""
        import pathlib
        import subprocess
        import sys
        import time

        def test_registers_child_then_hangs(track_subprocess):
            proc = subprocess.Popen(
                [sys.executable, "-c", { _SLEEP_FOREVER!r }],
                start_new_session=True,
            )
            pathlib.Path("pids.txt").write_text(str(proc.pid))
            track_subprocess.register(proc)
            time.sleep(3600)
        """,
        "-o", "test_timeout=1",
        timeout=60,
    )

    (pid,) = _read_pids(tmp_path)
    assert result.returncode != 0, "超时必须让退出码非零"
    assert "[test-timeout]" in result.stdout + result.stderr, (
        f"超时归因缺失：stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _wait_pid_gone(pid), (
        f"已登记子进程 pid={pid} 在 abort 后仍存活——注册表终止路径未生效：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


# ---------------------------------------------------------------------------
# 1b. 同一进程组内的后代一并终止（AC1 的「同组后代」承诺）
# ---------------------------------------------------------------------------
def test_registered_process_group_descendants_are_killed(
    tmp_path: Path,
) -> None:
    """登记的是组长；孙进程与其同组，killpg 必须连它一起收掉。

    只杀直接子进程的实现（`os.kill(pid, …)`）会在这里留活口——这正是本用例要钉的。
    """
    if sys.platform == "win32":  # pragma: no cover - Windows 走 taskkill /T，本机取证不了
        return
    result = _run_probe(
        tmp_path,
        f"""
        import pathlib
        import subprocess
        import sys
        import time

        def test_registers_group_then_hangs(track_subprocess):
            proc = subprocess.Popen(
                [sys.executable, "-c", {_GROUP_LEADER_CODE!r}],
                start_new_session=True,
            )
            pathlib.Path("pids.txt").write_text(str(proc.pid))
            track_subprocess.register(proc)
            time.sleep(3600)
        """,
        "-o", "test_timeout=1",
        timeout=60,
    )

    leader = _read_pids(tmp_path)[0]
    # 孙进程 pid 由组长自己落盘（组内成员可以读同一目录）。
    pid_file = tmp_path / "grandchild.pid"
    grandchildren = (
        {int(pid_file.read_text(encoding="utf-8").strip())} if pid_file.exists() else set()
    )
    try:
        # 前置：探针已 fork 出孙进程；拿不到就只能判定用例本身无效，不能当通过。
        assert grandchildren, (
            "前置失败：没拿到孙进程 pid（组长没来得及 fork，或进程组已空）"
        )
        assert result.returncode != 0
        assert "[test-timeout]" in result.stdout + result.stderr
        assert _wait_pid_gone(leader), f"组长 pid={leader} 未被终止"
        for child in grandchildren:
            assert _wait_pid_gone(child), (
                f"同组后代 pid={child} 仍存活——终止只杀了直接子进程，没有按进程组收"
            )
    finally:
        _kill_verified_process(leader)
        for child in grandchildren:
            _kill_verified_process(child)


# ---------------------------------------------------------------------------
# 1c. SIGKILL 升级不以 leader 存活为准：忽略 TERM 的后代也必须被收掉
# ---------------------------------------------------------------------------
def test_term_immune_descendant_is_sigkilled_after_leader_exits(
    tmp_path: Path,
) -> None:
    """组长对 TERM 敏感、孙进程忽略 TERM：宽限期后必须按组补 SIGKILL。

    旧实现只在 leader `poll()` 仍存活时才发 SIGKILL ⇒ 这个孙进程逃逸。用例靠
    `grandchild.ready` 确认孙进程已装好 SIG_IGN 再进 abort，断言不能靠时序碰运气。
    """
    if sys.platform == "win32":  # pragma: no cover - Windows 无进程组语义
        return
    result = _run_probe(
        tmp_path,
        f"""
        import pathlib
        import subprocess
        import sys
        import time

        def test_registers_term_immune_group_then_hangs(track_subprocess):
            proc = subprocess.Popen(
                [sys.executable, "-c", {_TERM_IMMUNE_GROUP_CODE!r}],
                start_new_session=True,
            )
            pathlib.Path("pids.txt").write_text(str(proc.pid))
            track_subprocess.register(proc)
            time.sleep(3600)
        """,
        "-o", "test_timeout=1",
        timeout=60,
    )

    (leader,) = _read_pids(tmp_path)
    pid_file = tmp_path / "grandchild.pid"
    immune = int(pid_file.read_text(encoding="utf-8").strip()) if pid_file.exists() else None
    try:
        assert immune is not None, "前置失败：没拿到孙进程 pid（组长没来得及 fork）"
        assert (tmp_path / "grandchild.ready").exists(), (
            "前置失败：孙进程还没装好 SIGTERM 忽略处理器，用例会测不到升级路径"
        )
        assert result.returncode != 0
        assert "[test-timeout]" in result.stdout + result.stderr
        assert _wait_pid_gone(leader), f"组长 pid={leader} 未被终止"
        assert _wait_pid_gone(immune), (
            f"忽略 SIGTERM 的同组后代 pid={immune} 在 abort 后仍存活——"
            "SIGKILL 升级以 leader 存活为条件，漏掉了 TERM-免疫的后代"
        )
    finally:
        _kill_verified_process(leader)
        if immune is not None:
            _kill_verified_process(immune)


# ---------------------------------------------------------------------------
# 2. 注册/注销：正常结束只清表、不杀进程（登记制只管 abort 路径）
# ---------------------------------------------------------------------------
def test_unregistered_on_normal_exit_survives(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        f"""
        import pathlib
        import subprocess
        import sys

        def test_registers_then_unregisters(track_subprocess):
            proc = subprocess.Popen(
                [sys.executable, "-c", { _SLEEP_FOREVER!r }],
                start_new_session=True,
            )
            pathlib.Path("pids.txt").write_text(str(proc.pid))
            track_subprocess.register(proc)
            track_subprocess.unregister(proc)
            assert proc.poll() is None
        """,
        "-o", "test_timeout=30",
        timeout=60,
    )

    try:
        (pid,) = _read_pids(tmp_path)
        assert result.returncode == 0, (
            f"正常结束的探针不应失败：stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "[test-timeout]" not in result.stdout + result.stderr
        assert _pid_alive(pid), (
            f"注销后的子进程 pid={pid} 被误杀——正常结束路径不得终止进程"
        )
    finally:
        if (tmp_path / "pids.txt").exists():
            for pid in _read_pids(tmp_path):
                _kill_verified_process(pid)


# ---------------------------------------------------------------------------
# 3. 重复登记 / 重复终止：幂等，不抛异常
# ---------------------------------------------------------------------------
def test_repeated_register_and_terminate_is_idempotent(track_subprocess) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _SLEEP_FOREVER], start_new_session=True,
    )
    try:
        track_subprocess.register(proc)
        track_subprocess.register(proc)  # 去重
        assert proc.poll() is None
        track_subprocess.terminate_all("probe::repeat-1")
        track_subprocess.terminate_all("probe::repeat-2")  # 已退出条目 = 成功
        assert proc.poll() is not None, "终止后子进程必须退出"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
    # 未登记 / 重复注销静默返回
    track_subprocess.unregister(proc)
    track_subprocess.unregister(proc)


# ---------------------------------------------------------------------------
# 4. 未登记子进程不受影响
# ---------------------------------------------------------------------------
def test_unregistered_child_is_not_touched(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        f"""
        import pathlib
        import subprocess
        import sys
        import time

        def test_only_registered_child_is_killed(track_subprocess):
            tracked = subprocess.Popen(
                [sys.executable, "-c", { _SLEEP_FOREVER!r }],
                start_new_session=True,
            )
            # 未登记：自带 60s 自杀兜底，即便本用例的清理逻辑全坏也不会永久泄漏。
            untracked = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                start_new_session=True,
            )
            pathlib.Path("pids.txt").write_text(f"{{tracked.pid}} {{untracked.pid}}")
            track_subprocess.register(tracked)
            time.sleep(3600)
        """,
        "-o", "test_timeout=1",
        timeout=60,
    )

    tracked, untracked = _read_pids(tmp_path)
    try:
        assert result.returncode != 0
        assert "[test-timeout]" in result.stdout + result.stderr
        assert _wait_pid_gone(tracked), f"已登记 pid={tracked} 未被终止"
        assert _pid_alive(untracked), (
            f"未登记 pid={untracked} 被误杀——注册表不得追踪未登记进程"
        )
    finally:
        _kill_verified_process(tracked)
        _kill_verified_process(untracked)


# ---------------------------------------------------------------------------
# 5. 跨用例不串扰：前一用例的登记项不进入下一用例的 abort
# ---------------------------------------------------------------------------
def test_previous_test_registration_does_not_leak_into_next_abort(
    tmp_path: Path,
) -> None:
    """A 登记后**不注销**就正常结束 → B 超时 abort。

    两个断言缺一不可：
    - A 的进程必须活着 ⇒ 正常结束路径只清表、不终止；
    - B 的超时归因必须点名 B，且 A 的 pid 仍活 ⇒ A 遗留的登记项没被 B 的 abort 拿去终止。

    旧版探针在 A 里 `unregister(proc)`，条目在 B 之前就没了，表不隔离也不红。
    本用例钉的是端到端保证（前一用例的登记项进不了后一用例的 abort）；换表与
    `table.clear()` 是两条实现路径，后者另由
    `test_clear_drops_entries_without_killing` 单独钉住。"""
    result = _run_probe(
        tmp_path,
        f"""
        import pathlib
        import subprocess
        import sys
        import time

        def test_a_finishes_normally(track_subprocess):
            proc = subprocess.Popen(
                [sys.executable, "-c", { _SLEEP_FOREVER!r }],
                start_new_session=True,
            )
            pathlib.Path("pids.txt").write_text(str(proc.pid))
            track_subprocess.register(proc)

        def test_b_times_out(track_subprocess):
            time.sleep(3600)
        """,
        "-o", "test_timeout=1",
        timeout=60,
    )

    try:
        (pid,) = _read_pids(tmp_path)
        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "test_b_times_out" in output, f"超时归因必须点名后一个用例：{output}"
        assert "[test-timeout]" in output
        assert _pid_alive(pid), (
            f"前一用例注册的 pid={pid} 被后一用例的 abort 误杀——表未按用例隔离"
        )
    finally:
        if (tmp_path / "pids.txt").exists():
            for pid in _read_pids(tmp_path):
                _kill_verified_process(pid)


# ---------------------------------------------------------------------------
# 5b. 正常结束路径只清表、不终止：直接把「上一用例那张表」拿去 terminate_all 也不能杀
# ---------------------------------------------------------------------------
def test_clear_drops_entries_without_killing(tmp_path: Path) -> None:
    """专钉 `pytest_runtest_protocol` 的 `table.clear()`（Standards P4-2）。

    test_a 把本用例的表对象留在模块全局；test_b 对**那张旧表**调 `terminate_all`
    （公开 API，不碰私有字段）。`clear()` 生效 ⇒ 旧表已空 ⇒ 进程活着；
    删掉 `clear()` ⇒ 旧表仍带登记项 ⇒ 这次终止会把它杀掉，父进程断言变红。
    """
    result = _run_probe(
        tmp_path,
        f"""
        import pathlib
        import subprocess
        import sys

        stale_table = None

        def test_a_finishes_normally(track_subprocess):
            global stale_table
            proc = subprocess.Popen(
                [sys.executable, "-c", { _SLEEP_FOREVER!r }],
                start_new_session=True,
            )
            pathlib.Path("pids.txt").write_text(str(proc.pid))
            track_subprocess.register(proc)
            stale_table = track_subprocess

        def test_b_reuses_previous_table():
            # 前一用例正常结束后，它的表必须已经清空；terminate_all 空表 = 无操作。
            stale_table.terminate_all("probe::stale-table")
        """,
        "-o", "test_timeout=30",
        timeout=60,
    )

    try:
        (pid,) = _read_pids(tmp_path)
        assert result.returncode == 0, (
            f"两个用例都应正常结束：stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert _pid_alive(pid), (
            f"前一用例登记过的 pid={pid} 被旧表的 terminate_all 杀掉——"
            "正常结束路径没有清表（table.clear() 缺失或失效）"
        )
    finally:
        if (tmp_path / "pids.txt").exists():
            for pid in _read_pids(tmp_path):
                _kill_verified_process(pid)
def test_concurrent_register_and_unregister_are_thread_safe(
    track_subprocess,
) -> None:
    shared = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        for _ in range(6)
    ]
    errors: list[BaseException] = []
    start = threading.Barrier(len(procs) + 1)

    def worker(process: subprocess.Popen[object]) -> None:
        try:
            start.wait(timeout=10)
            for _ in range(100):
                track_subprocess.register(process)
                track_subprocess.register(shared)
                track_subprocess.unregister(process)
                track_subprocess.unregister(shared)
        except BaseException as error:  # noqa: BLE001 — 测试收集，不吞
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(proc,)) for proc in procs]
    threads.append(threading.Thread(target=worker, args=(shared,)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    try:
        assert not any(thread.is_alive() for thread in threads), "并发登记/注销发生死锁"
        assert errors == [], f"并发登记/注销抛异常：{errors!r}"
    finally:
        for proc in [shared, *procs]:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# 7. fail-closed：同进程组（未 start_new_session）的子进程拒绝登记、绝不被 killpg
# ---------------------------------------------------------------------------
def test_child_in_pytest_own_group_is_refused(track_subprocess) -> None:
    """不满足新会话契约时拒绝登记：对 pytest 自身组做 killpg 会连 runner 一起杀。"""
    if sys.platform == "win32":  # pragma: no cover - Windows 无进程组语义
        return
    # 故意**不**带 start_new_session：pgid == pytest 自身组。
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert os.getpgid(proc.pid) == os.getpgid(0), "前置：该子进程确实与 pytest 同组"
        track_subprocess.register(proc)
        track_subprocess.terminate_all("probe::same-group")
        assert proc.poll() is None, (
            "同组子进程必须被拒绝登记，terminate_all 不得对它 killpg"
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)
