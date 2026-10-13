"""#1063 —— **跨进程**并发写同一 session：store 的检查-写入必须跨进程互斥。

**症状**（Correctness 轴 P4-1，实测）：两个进程各写同一 session，`store.py` 的
「读已落盘最大 seq → 判定 → 写盘」只由**进程内** `threading.Lock`（`_lock_for`）
串行化。两个进程能各自读到同一个 `last`、各自通过 `seq <= last` 检查、各自把同一
个号写下去 ⇒ 盘上**重复 seq** ⇒ `read_events_report` 报空洞 / 判损坏 ⇒ 会话不可
resume。实测两进程各写 120 条 → seq 2..120 **全重复**（241 行、唯一 seq 121 个）。

**修法**：在 store 的临界区外包一层 **per-session 跨进程 advisory 文件锁**
（`instance_lock.session_append_lock`，复用 #150 的 POSIX `flock` / Windows
`msvcrt.locking` 平台分支），并把文件戳缓存更新也**移进**锁内——原实现把
`stat → 缓存写` 放在文件锁外，跨进程下会在「放锁 → stat」之间被另一进程追加，
把自己偏小的 seq 配上别人刚写后的文件戳入缓存 ⇒ 后续命中过期 last ⇒ 重复落盘
（实测 15 条重复全来自这一处，非锁未生效）。

**方案依据**（AGENTS §6.1）：POSIX `flock(2)`（man7.org，advisory、进程退出自动
释放）；SQLite 单写者模型（写与取号同一临界区，ADR-0004 已采用）；本仓
`instance_lock.py`（#150 跨平台 advisory 锁先例，`_WIN_LOCK_OFFSET` 的 mandatory
锁偏移陷阱只此一处）。`filelock`（tox-dev/py-filelock）是 Python 事实标准但**未装**，
不为此新增依赖——复用仓内先例。

**验收（票面）**：双进程并发写用例绿（seq 无重复无空洞）；进程内全量门禁无新增红。
本文件用**真实子进程**（不是线程、不是 mock）：启动期实例锁挡的是「两进程启同一
root」，但逃生门（`ALLOW_SHARED_ROOT`）或未取 instance 锁的直接 library 用法下，
两个进程仍会写同一 session——那正是本票要挡的形状。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from agent_harness.session import JsonlSessionStore, Session

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 每个子进程写入的条数。够大以覆盖跨进程交错窗口；够小以在 CI 上秒级完成。
_WRITES_PER_PROC = 120

#: 子进程 runner：两进程各自 `Session.load` 同一快照、`deliver_to_runtime()`（撞号走
#: resync 而非抛出）、再用一个 multiprocessing.Barrier 同时起跑，最大化交错。
_RUNNER = textwrap.dedent(
    """
    import multiprocessing as mp
    import sys
    from pathlib import Path

    root, sid, n = sys.argv[1], sys.argv[2], int(sys.argv[3])

    def worker(pid, barrier, q):
        from agent_harness.session import JsonlSessionStore, Session
        from agent_harness.session.event import MODEL_REQUEST_STARTED
        store = JsonlSessionStore(root=Path(root) / "sessions")
        live = Session.load(store, session_id=sid)
        live.deliver_to_runtime()
        barrier.wait()
        ok = 0
        for i in range(n):
            try:
                live.append(MODEL_REQUEST_STARTED, {"model": "m", "p": pid, "i": i})
                ok += 1
            except Exception:  # noqa: BLE001
                pass
        q.put((pid, ok))

    if __name__ == "__main__":
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(2)
        q = ctx.Queue()
        ps = [ctx.Process(target=worker, args=(i, barrier, q)) for i in range(2)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(300)
        sys.exit(0 if all(p.exitcode == 0 for p in ps) else 1)
    """
)


def _read_seqs(root: Path, sid: str) -> list[int]:
    path = root / "sessions" / sid / "events.jsonl"
    return [
        json.loads(line)["seq"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_two_processes_never_write_duplicate_or_gapped_seq(tmp_path):
    """双进程各写 N 条 ⇒ 盘上 seq 无重复、无空洞（#1063 票面验收）。

    确定性断言：不是「跑很多次碰运气」，而是断言**唯一性 + 连续性**这两个不变量。
    修复前实测：241 行、重复 seq 118 个（seq 2..120）；修复后：无重复无空洞。
    """
    sid = "xproc-sess"
    root = tmp_path
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    Session.start(JsonlSessionStore(root=root / "sessions"), session_id=sid)

    runner = tmp_path / "_xproc_runner.py"
    runner.write_text(_RUNNER, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(runner), str(root), sid, str(_WRITES_PER_PROC)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, f"runner 失败\nstdout={proc.stdout}\nstderr={proc.stderr}"

    seqs = _read_seqs(root, sid)
    # 每个进程都真写进去了（否则「无重复」是空会话的假绿）— session/started = 1 条。
    assert len(seqs) >= 2 * _WRITES_PER_PROC * 0.9, f"落盘条数异常少：{len(seqs)}"
    dups = sorted({s for s in seqs if seqs.count(s) > 1})
    assert not dups, f"跨进程写出现重复 seq：{dups[:20]}"
    assert seqs == list(range(len(seqs))), (
        f"seq 不是从 0 起的无空洞连续序列：max={max(seqs)} 条数={len(seqs)}"
    )


def test_events_load_clean_after_cross_process_writes(tmp_path):
    """跨进程写后会话仍可 load 且完整性闸门不报损坏（#1063 的后果面）。

    重复 seq 会让 `read_events_report` 报空洞/重复；本用例钉住「可恢复」。
    """
    sid = "xproc-load"
    root = tmp_path
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    store = JsonlSessionStore(root=root / "sessions")
    Session.start(store, session_id=sid)

    runner = tmp_path / "_xproc_runner.py"
    runner.write_text(_RUNNER, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(runner), str(root), sid, str(_WRITES_PER_PROC)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, f"runner 失败\nstderr={proc.stderr}"

    events, report = JsonlSessionStore(root=root / "sessions").read_events_report(sid)
    assert report.seq_duplicates == (), f"完整性闸门报重复 seq：{report.seq_duplicates}"
    assert report.seq_gaps == (), f"完整性闸门报空洞：{report.seq_gaps}"
    assert report.healthy, "完整性闸门判不健康"
    # 能重建聚合 = 可 resume（重复 seq 会让 Session.load 直接抛错）。
    reloaded = Session.load(JsonlSessionStore(root=root / "sessions"), session_id=sid)
    assert reloaded.next_seq == max(e.seq for e in events) + 1
