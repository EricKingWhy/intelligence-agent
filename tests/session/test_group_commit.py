"""#1489：durable append 的组提交 —— 事件循环不被 fsync 阻塞，确认边界不变。

三条不变量（票面 Scope Lock 的直译）：
1. **确认边界不变**：`append`/`append_async` 返回 ⇒ 本条的字节已 fsync 落盘；
   永不把未耐久的写报告成成功。
2. **fsync 不去掉**：慢盘/故障注入下仍能看到 fsync，且失败原样传播。
3. **共享刷盘**：同时到达的写者共享一次刷盘（PostgreSQL `commit_delay` 形状），
   而不是各自刷一遍。

原始读数一律打印出来供回报引用，阈值只写"定性"的那一条（别的 coroutine 至少
推进一次），不编造毫秒级响应阈值——部署盘量级由 #1541 复测。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_harness.session import USER_MESSAGE, JsonlSessionStore, Session
from agent_harness.session.errors import SeqConflict

#: 夹具用的"慢盘"：一次 fsync 的墙钟。取 150ms 是因为它明显大于一次事件循环
#: 心跳间隔（5ms）与一次 append 的其它开销，使"被阻塞 ⇒ 零心跳推进"成为可观测
#: 事实，而不是抖动。不是响应阈值。
SLOW_FSYNC_SECONDS = 0.150
HEARTBEAT_INTERVAL_SECONDS = 0.005


class _SlowDisk:
    """把 `os.fsync` 换成"睡 SLOW_FSYNC_SECONDS 再做真 fsync"的夹具。

    记录每次调用的 (线程名, 墙钟)，供"共享刷盘"用例数 fsync 次数。
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, seconds: float) -> None:
        self.calls: list[tuple[str, float]] = []
        self._seconds = seconds
        self._real = os.fsync
        self._lock = threading.Lock()
        monkeypatch.setattr(os, "fsync", self)

    def __call__(self, fd: int) -> None:
        with self._lock:
            self.calls.append((threading.current_thread().name, time.perf_counter()))
        if self._seconds:
            time.sleep(self._seconds)
        self._real(fd)

    def __len__(self) -> int:
        return len(self.calls)


@pytest.mark.asyncio
async def test_append_async_keeps_event_loop_responsive_during_slow_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收 1：慢盘期间其它 coroutine 可推进，且 append 完成语义不变。

    断言是**夹具相对**的两条，不编造部署阈值：

    * 窗口内心跳至少推进一次（未修复时为零次）；
    * 窗口内最大心跳间隔 < 一次 fsync 时长的一半——即事件循环**从未**被一次
      磁盘同步整体扣住。只测"至少一次"是不够的：看板投影等其它 `to_thread`
      会在窗口里漏出一两次心跳，掩盖仍在循环线程上的那次 fsync（消融实测：
      阻塞 150ms 时心跳仍有 29-31 次）。
    """
    disk = _SlowDisk(monkeypatch, seconds=SLOW_FSYNC_SECONDS)
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)  # 同步入口，建会话（含 session/started）

    ticks: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        while not stop.is_set():
            ticks.append(time.perf_counter())
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)  # 让心跳先跑起来，拿一个稳定基线

    #: 一次磁盘同步的一半：窗口内超过它即说明循环被整段扣住过。
    blocked_limit = SLOW_FSYNC_SECONDS / 2
    measured: list[str] = []
    for index in range(3):
        before = len(ticks)
        started = time.perf_counter()
        await session.append_async(USER_MESSAGE, {"content": f"m{index}"})
        elapsed = time.perf_counter() - started
        during = len(ticks) - before
        window = ticks[before:]
        gap = max(
            (b - a for a, b in itertools.pairwise(window)), default=0.0,
        )
        measured.append(
            f"commit#{index}: 墙钟={elapsed * 1000:.1f}ms 期间心跳={during} "
            f"窗口最大间隔={gap * 1000:.1f}ms"
        )
        assert during >= 1, (
            "durable append 期间事件循环一次都没有推进——fsync 仍在循环线程上："
            f"{measured}"
        )
        assert gap < blocked_limit, (
            "事件循环被一次磁盘同步整段扣住（最大心跳间隔 ≥ 半个 fsync 时长）："
            f"{measured}"
        )
        assert elapsed >= SLOW_FSYNC_SECONDS * 0.9, (
            f"夹具未生效（append 没有真的等慢 fsync）：{measured}"
        )

    stop.set()
    await task

    gaps = [
        ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)
    ]
    raw = (
        f"slow_fsync={SLOW_FSYNC_SECONDS * 1000:.0f}ms "
        f"心跳间隔基线={HEARTBEAT_INTERVAL_SECONDS * 1000:.0f}ms "
        f"心跳次数={len(ticks)} 最大心跳间隔={max(gaps) * 1000 if gaps else 0:.1f}ms "
        f"fsync 次数={len(disk)}"
    )
    print(f"[#1489 原始读数] {raw} | " + "; ".join(measured))

    # 完成语义不变：返回即耐久（重开 store 从盘上读得到）。
    reloaded = JsonlSessionStore(root=tmp_path).read_events(session.session_id)
    assert [e.seq for e in reloaded] == list(range(len(reloaded))), "seq 必须保序无空洞"
    assert [e.data["content"] for e in reloaded[-3:]] == ["m0", "m1", "m2"]


@pytest.mark.asyncio
async def test_concurrent_writers_share_one_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收 1/3：同时到达的写者共享一次刷盘（组提交），且全部耐久、保序。

    形状：leader 已在 fsync 里睡住时，其余写者完成自己的 write 后**并入**这一次
    刷盘，不再各自刷一遍。断言只有"fsync 次数少于写者数"，绝对次数打印出来。
    """
    disk = _SlowDisk(monkeypatch, seconds=SLOW_FSYNC_SECONDS)
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)
    start_seq = session.next_seq
    writers = 4
    errors: list[BaseException] = []
    barrier = threading.Barrier(writers)
    returned: list[int] = []
    lock = threading.Lock()
    # 只统计并发窗口内的刷盘：`Session.start` 的那一次不属于本用例。
    disk.calls.clear()

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            event = store.append_event(
                session.session_id,
                # 各写者都按同一乐观号提交，交给 store 在锁内改号（#1062 的
                # `allocate` 合同）。本用例只考刷盘共享，不考取号。
                _event(session.session_id, start_seq),
                allocate=True,
            )
            with lock:
                returned.append(event.seq)
        except BaseException as error:  # noqa: BLE001 — 断言用
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "写者在共享刷盘里悬挂"

    assert not errors, f"并发写者报错：{errors}"
    assert len(returned) == writers
    print(
        f"[#1489 原始读数] 并发写者={writers} fsync 次数={len(disk)} "
        f"slow_fsync={SLOW_FSYNC_SECONDS * 1000:.0f}ms"
    )
    assert len(disk) < writers, (
        f"没有共享刷盘：{writers} 个写者各刷了 {len(disk)} 次"
    )
    assert len(disk) >= 1, "fsync 被去掉了——耐久性底线失守"

    persisted = store.read_events(session.session_id)
    assert [e.seq for e in persisted] == list(range(len(persisted))), "seq 保序无重复"


def test_fsync_failure_propagates_to_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验收 2/3：真实 fsync 失败原样传播，未提交不得当成功，也不盲目重投。

    失败之后本条字节**已经在文件里**（write+flush 早于 fsync），但刷盘结果未知。
    正确的处置不是"悄悄再写一遍"：同号重投会被既有 seq 守卫拒掉
    （``SeqConflict``）——盘上不会出现两条同 seq 的记录，调用方也拿到了明确错误。
    """
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)
    seq = session.next_seq

    calls = {"n": 0}
    real_fsync = os.fsync

    def failing_fsync(fd: int) -> None:
        calls["n"] += 1
        raise OSError(5, "Input/output error（注入）")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="Input/output error"):
        store.append_event(session.session_id, _event(session.session_id, seq))
    assert calls["n"] >= 1, "fsync 未调用——耐久性底线失守"

    monkeypatch.setattr(os, "fsync", real_fsync)
    # UNKNOWN 不盲重跑：同号重投被 seq 守卫拒绝，而不是静默产生重复 seq。
    with pytest.raises(SeqConflict):
        store.append_event(session.session_id, _event(session.session_id, seq))
    # 换号即可继续（盘上事实是权威），完整性闸门读得回来。
    recovered = store.append_event(
        session.session_id, _event(session.session_id, seq + 1),
    )
    assert recovered.seq == seq + 1
    persisted = [e.seq for e in store.read_events(session.session_id)]
    assert persisted == sorted(persisted) and len(set(persisted)) == len(persisted)


def _event(session_id: str, seq: int):
    from agent_harness.session import SessionEvent

    return SessionEvent(
        seq=seq, type=USER_MESSAGE, session_id=session_id, data={"content": f"c{seq}"},
    )


# ── 真实 kill/restart（验收 2）─────────────────────────────────────────────

_CHILD = Path(__file__).with_name("_durable_batch_child.py")


def test_confirmed_batch_survives_real_kill(tmp_path: Path) -> None:
    """成功返回的批次经真实 kill 后全部可读、seq 保序、无重复。

    子进程用真实 `append_async` 逐条提交，把**已确认**的 seq 打到 stdout（逐条
    flush），随后 `os._exit(137)`——不跑清理、不给缓冲区机会。父进程用全新 store
    读盘核对：每条已确认的事件必须可读，seq 严格递增且无重复。
    """
    completed = subprocess.run(
        [sys.executable, str(_CHILD), str(tmp_path)],
        capture_output=True,
        timeout=120,
        check=False,  # 子进程故意以 137 退出
        env={**os.environ, "PYTHONUTF8": "1"},
        cwd=str(Path(__file__).parents[2]),
        text=True,
    )
    assert completed.returncode == 137, (
        f"子进程未按预期被 kill：rc={completed.returncode} stderr={completed.stderr}"
    )
    lines = completed.stdout.strip().splitlines()
    assert lines and "#END" in lines, f"子进程输出异常：{lines[-3:]}"
    end_index = lines.index("#END")
    session_id = lines[0]
    confirmed: list[int] = json.loads(lines[end_index + 1])
    assert len(confirmed) >= 5, f"确认批次太小：{confirmed}"

    events = JsonlSessionStore(root=tmp_path / "sessions").read_events(session_id)
    seqs = [e.seq for e in events]
    assert seqs == list(range(len(seqs))), f"kill 后 seq 不保序/有重复：{seqs}"
    missing = [seq for seq in confirmed if seq not in seqs]
    assert not missing, f"已确认成功的事件在 kill 后丢失：{missing}"


# ── 取消/关闭下不悬挂（验收 3）─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelling_an_inflight_append_does_not_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在 fsync 途中取消等待方：不悬挂、不留死锁，后续写正常。"""
    _SlowDisk(monkeypatch, seconds=SLOW_FSYNC_SECONDS)
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)

    task = asyncio.create_task(session.append_async(USER_MESSAGE, {"content": "cancelled"}))
    await asyncio.sleep(0.02)  # 让它进到 fsync
    started = time.perf_counter()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.perf_counter() - started < SLOW_FSYNC_SECONDS, "取消被 fsync 阻塞"

    # 取消之后同一 store 仍可提交（刷盘协调器没有卡在 _syncing）。
    later = await asyncio.wait_for(
        session.append_async(USER_MESSAGE, {"content": "after-cancel"}), timeout=10,
    )
    assert later.seq > 0
