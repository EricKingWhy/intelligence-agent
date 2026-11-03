"""#1547：并发 append_async 的有序投递 —— 内存投影与 listener 通知按 seq 单调。

缺陷机理（票面）：#1489 把 fsync 等待移出事件循环后，`Session.append_async` 的
收口在**各写者自己的完成时刻**执行；`_GroupFlush` 的 cohort 唤醒顺序 ≠ seq 顺序，
于是内存投影 `_events` 与 listener 通知乱序，`RunManager.enqueue_session_event`
的 seq 幂等门把迟到帧**永久丢弃**（live SSE 与重放双路都丢）。

本文件的夹具用「按写入完成顺序反比延迟进入组提交」把乱序做成**确定性**：seq 最小
的写者最晚进入 `_GroupFlush.commit`，从而最晚完成——完成顺序与 seq 顺序倒挂。

三条验收（票面 AC1/AC3 的 Session 层直译）：
1. 并发 append_async 下 listener 收到顺序严格递增（non-monotonic = 0）；
2. 无帧丢失（delivered 集合 == 写入集合）；
3. `Session.events()` 在并发写入后按 seq 有序。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from agent_harness.session import USER_MESSAGE, JsonlSessionStore, Session
from agent_harness.session.store import _GroupFlush

#: 每个 writing 的组提交入场错峰（写完成顺序 = seq 顺序，首号睡最长 → 完成倒挂）。
STAGGER_SECONDS = 0.03


def _install_staggered_commit(
    monkeypatch: pytest.MonkeyPatch,
    *,
    writers: int,
) -> list[int]:
    """把 `_GroupFlush.commit` 换成「按入场序反比延迟」的确定性乱序夹具。

    入场序 = 写完成序 = 批内 seq 序（写阶段被会话写锁串行化）。首号睡
    ``writers * STAGGER_SECONDS``，末号不睡 ⇒ 完成顺序严格倒挂于 seq 顺序。
    返回共享的入场计数列表（供断言夹具生效）。
    """
    real_commit = _GroupFlush.commit
    lock = threading.Lock()
    entries: list[int] = []

    def staggered_commit(self: _GroupFlush, path) -> None:
        with lock:
            entries.append(len(entries))
            index = len(entries) - 1
        delay = max(0, writers - 1 - index) * STAGGER_SECONDS
        if delay:
            time.sleep(delay)
        return real_commit(self, path)

    monkeypatch.setattr(_GroupFlush, "commit", staggered_commit)
    return entries


def _record_store_writes(
    monkeypatch: pytest.MonkeyPatch,
    store: JsonlSessionStore,
) -> list[str]:
    """记录每次 store 物理写的 data.content——测试据此等到**前一个写者已进
    store 写临界区**再起下一个写者，把「更小 seq」从调度巧合变成顺序事实
    （取代对 sleep(0) 次数的依赖，审查 P2）。
    """
    entered: list[str] = []
    real_append = store.append_event

    def recording_append(session_id, event, *, allocate: bool = False):
        entered.append(str(event.data.get("content")))
        return real_append(session_id, event, allocate=allocate)

    monkeypatch.setattr(store, "append_event", recording_append)
    return entered


async def _until(predicate, *, spins: int = 10_000) -> None:
    """自旋等待谓词为真（每轮让出事件循环；谓词失败最终由调用方断言）。"""
    for _ in range(spins):
        if predicate():
            return
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_concurrent_append_async_notifies_in_seq_order_without_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC3：listener 顺序严格递增、零丢失；内存投影按 seq 有序。"""
    writers = 6
    entries = _install_staggered_commit(monkeypatch, writers=writers)
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)
    session.deliver_to_runtime()

    delivered: list[int] = []
    session.add_listener(lambda event: delivered.append(event.seq))

    persisted = await asyncio.gather(
        *[
            session.append_async(USER_MESSAGE, {"content": f"m{i}"})
            for i in range(writers)
        ]
    )

    written = sorted(event.seq for event in persisted)
    assert written == list(range(written[0], written[0] + writers)), (
        "夹具前提失效：并发写者没有拿到连续 seq"
    )
    assert len(entries) >= writers, (
        f"夹具未生效（组提交入场数 {len(entries)} < 写者数 {writers}）"
    )
    assert delivered == sorted(delivered), (
        f"listener 收到顺序非单调（AC1）：delivered={delivered}"
    )
    assert sorted(delivered) == written, (
        f"listener 丢帧（AC1）：delivered={delivered} != written={written}"
    )
    memory_seq = [event.seq for event in session.events]
    assert memory_seq == sorted(memory_seq), f"内存投影乱序（AC3）：{memory_seq}"
    # 盘面事实不受影响（票面：durable 日志本身正确）。
    disk_seq = [event.seq for event in store.read_events(session.session_id)]
    assert disk_seq == sorted(disk_seq)


@pytest.mark.asyncio
async def test_sync_append_while_lower_async_in_flight_stays_monotonic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步 append 与更低 seq 的在途 append_async 交错时，listener 仍单调。

    生产可达面（票面 (a)）：exec 子调用走同步 emit，父级 sink drain 走
    append_async。夹具让异步写者（更小 seq）在组提交里睡住，同步写者先完成——
    未修复时同步帧先通知、异步帧迟到被幂等门丢弃。
    """
    writers = 2
    _install_staggered_commit(monkeypatch, writers=writers)
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)
    session.deliver_to_runtime()
    entered = _record_store_writes(monkeypatch, store)

    delivered: list[int] = []
    session.add_listener(lambda event: delivered.append(event.seq))

    async_writer = asyncio.create_task(
        session.append_async(USER_MESSAGE, {"content": "async"})
    )
    # 等异步写者**已进 store 写临界区**（更小 seq 由写锁序确定为事实）再发起
    # 同步写——固定次数的 sleep(0) 在调度负载下不构成顺序保证（审查 P2）。
    await _until(lambda: "async" in entered)
    assert "async" in entered, "异步写者未进 store（夹具前提失效）"
    sync_event = session.append(USER_MESSAGE, {"content": "sync"})
    async_event = await async_writer

    assert async_event.seq < sync_event.seq, "夹具前提失效：异步写者没拿到更小 seq"
    assert delivered == sorted(delivered), (
        f"同步/异步交错后 listener 非单调：delivered={delivered}"
    )
    assert sorted(delivered) == sorted([async_event.seq, sync_event.seq]), (
        f"同步/异步交错后丢帧：delivered={delivered}"
    )
    memory_seq = [event.seq for event in session.events]
    assert memory_seq == sorted(memory_seq), f"内存投影乱序：{memory_seq}"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_stall_successors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消在途 append_async 的等待后：计数器对齐、后继通知不被卡死。

    #1489 语义：取消只放弃等待，写仍在工作线程跑完；本票要求该 seq 对通知
    游标透明（跳过而非阻塞），后继事件照常按序通知。
    """
    writers = 3
    _install_staggered_commit(monkeypatch, writers=writers)
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)
    session.deliver_to_runtime()

    delivered: list[int] = []
    session.add_listener(lambda event: delivered.append(event.seq))
    entered = _record_store_writes(monkeypatch, store)

    cancelled_seq = session.next_seq
    cancelled_writer = asyncio.create_task(
        session.append_async(USER_MESSAGE, {"content": "cancelled"})
    )
    # 顺序用「进 store 临界区」的事实钉死（见 _record_store_writes），不靠
    # sleep(0) 计数：取消写者必须先于 follower 拿到更小 seq。
    await _until(lambda: "cancelled" in entered)
    assert "cancelled" in entered, "取消写者未进 store（夹具前提失效）"

    follower = asyncio.create_task(
        session.append_async(USER_MESSAGE, {"content": "follower"})
    )
    await _until(lambda: "follower" in entered)
    assert "follower" in entered, "后继写者未进 store（夹具前提失效）"
    cancelled_writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_writer

    follower_event = await asyncio.wait_for(follower, timeout=5)
    # 后继的通知必须等取消写者那行**真实在途的写**落定（它压着更小 seq，落定前
    # 放行会倒序——票面禁止定时窗口，等的就是写本身），随后照常按序放行。
    for _ in range(100):
        if follower_event.seq in delivered:
            break
        await asyncio.sleep(STAGGER_SECONDS)
    # 取消写者的通知按 #1489 语义不补发（等待者已放弃）；但后继必须照常通知，
    # 且游标不被那个 UNKNOWN seq 卡死。
    assert follower_event.seq in delivered, (
        f"后继事件未通知（游标被取消写者卡死）：delivered={delivered}"
    )
    assert cancelled_seq not in delivered, (
        f"取消的等待者不该补发通知：delivered={delivered}"
    )
    assert delivered == sorted(delivered)
    # 等工作线程里那次写落定后，内存投影与盘面一致（计数器对齐语义不变）。
    for _ in range(100):
        disk = store.read_events(session.session_id)
        if len(session.events) == len(disk):
            break
        await asyncio.sleep(STAGGER_SECONDS)
    disk = store.read_events(session.session_id)
    assert [e.seq for e in disk] == sorted(e.seq for e in disk)
    assert [e.seq for e in session.events] == [e.seq for e in disk], (
        "写完成后内存投影必须与盘面对齐（#1489 对齐语义不因本票回归）"
    )


def test_concurrent_sync_appends_notify_in_seq_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多个**同步**写者的完成序倒挂时，通知同样按 seq 序（闸门覆盖同步/同步）。

    `_GroupFlush` 的 cohort 唤醒序也会颠倒两个同步 `append` 的返回序（取号 5 的
    fsync 后完成），基线里「返回即 append 即通知」随之乱序。同步路径因此与异步
    走同一道闸（`_persist_event` 先占位后投递）。全部写者返回后，最后一个写者的
    commit 必然已把闸内全部前缀放行。
    """
    writers = 4
    entries = _install_staggered_commit(monkeypatch, writers=writers)
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)
    session.deliver_to_runtime()

    delivered: list[int] = []
    delivered_lock = threading.Lock()

    def record(event) -> None:
        with delivered_lock:
            delivered.append(event.seq)

    session.add_listener(record)

    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            session.append(USER_MESSAGE, {"content": f"s{index}"})
        except BaseException as error:  # noqa: BLE001 — 断言用
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "同步写者在闸门里悬挂"

    assert not errors, f"并发同步写者报错：{errors}"
    assert len(entries) >= writers, (
        f"夹具未生效（组提交入场数 {len(entries)} < 写者数 {writers}）"
    )
    written = [
        e.seq
        for e in store.read_events(session.session_id)
        if e.data.get("content", "").startswith("s")
    ]
    assert sorted(delivered) == sorted(written), (
        f"同步/同步交错丢帧：delivered={delivered} written={written}"
    )
    assert delivered == sorted(delivered), (
        f"同步/同步交错后 listener 非单调：delivered={delivered}"
    )
    memory_seq = [e.seq for e in session.events]
    assert memory_seq == sorted(memory_seq)


@pytest.mark.asyncio
async def test_async_event_listener_fires_on_writer_loop_when_worker_unblocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1547 审查 P0：异步写者的事件必须回到**它自己的事件循环**上通知。

    形状：更低 seq 的**同步**写者在 worker 线程（组提交里睡住）；loop 上的异步
    写者（更高 seq）先写完、在闸内 ready 等待。同步写者的 commit 在 worker 线程
    上触发 drain——它只能投递自己的同步事件，异步事件必须转发回归属 loop
    （#1489 冻结的线程亲和，`web.AskUserTimeoutDriver` 依赖之），不得在 worker
    线程上代为通知。
    """
    writers = 2
    _install_staggered_commit(monkeypatch, writers=writers)
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)
    session.deliver_to_runtime()
    entered = _record_store_writes(monkeypatch, store)

    loop_thread_id = threading.get_ident()  # 本测试协程就跑在目标 loop 上
    fired: list[tuple[int, int]] = []  # (seq, listener 所在线程 id)
    session.add_listener(lambda event: fired.append((event.seq, threading.get_ident())))

    sync_done = threading.Event()

    def sync_worker() -> None:
        try:
            session.append(USER_MESSAGE, {"content": "sync-lower"})
        finally:
            sync_done.set()

    worker = threading.Thread(target=sync_worker)
    worker.start()
    # 同步写者先进入 store（拿到更小 seq），并在组提交夹具里睡住。
    await _until(lambda: "sync-lower" in entered)
    assert "sync-lower" in entered, "同步写者未进 store（夹具前提失效）"

    async_event = await session.append_async(USER_MESSAGE, {"content": "async-upper"})
    assert sync_done.wait(timeout=10), "同步写者在闸门里悬挂"
    worker.join(timeout=30)
    assert not worker.is_alive()

    fires = dict(fired)
    assert async_event.seq in fires, f"异步事件未通知：fired={fired}"
    assert fires[async_event.seq] == loop_thread_id, (
        f"异步事件的 listener 跑在了别的线程（线程亲和被破坏）：fired={fired}，"
        f"loop 线程={loop_thread_id}"
    )
    seqs = [seq for seq, _thread in fired]
    assert seqs == sorted(seqs)
    memory_seq = [e.seq for e in session.events]
    assert memory_seq == sorted(memory_seq)


@pytest.mark.asyncio
async def test_cancelled_higher_seq_waiter_keeps_memory_ordered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消**更高** seq 的在途等待者：内存投影仍与盘面同序（审查 F1 回归）。

    取消写者的事件若在取消臂/完成回调**当场** insort（旧实现），会越过仍在途
    的更低 seq 先进内存；随后更低 seq 由 drain 追加到尾部——内存倒插
    [0,2,1]、与盘面 [0,1,2] 分叉，喂给 `derive_messages`/ContextBuilder 的
    投影顺序随之污染，直到重载才自愈。补插必须推迟到 drain 消费该 skipped
    seq 的时刻（前置 ①③ 保证没有更低的在途 seq）。
    """
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)  # seq 0
    session.deliver_to_runtime()
    entered = _record_store_writes(monkeypatch, store)
    entries = _install_staggered_commit(monkeypatch, writers=2)
    first_written = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    staggered_commit = _GroupFlush.commit

    def hold_first_commit(self: _GroupFlush, path) -> None:
        # Commit is entered only after seq allocation and the JSONL write.
        if not first_written.is_set():
            first_written.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("first writer was not released")
            staggered_commit(self, path)
        else:
            try:
                staggered_commit(self, path)
            finally:
                second_finished.set()

    monkeypatch.setattr(_GroupFlush, "commit", hold_first_commit)

    slow_writer = asyncio.create_task(
        session.append_async(USER_MESSAGE, {"content": "slow"})  # 更低 seq
    )
    try:
        assert await asyncio.to_thread(first_written.wait, 5), "first write did not reach commit"
        cancelled_writer = asyncio.create_task(
            session.append_async(USER_MESSAGE, {"content": "cancelled"})  # 更高 seq
        )
        await _until(lambda: "cancelled" in entered)
        assert "cancelled" in entered, "cancelled writer did not enter the store"
        cancelled_writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_writer
        assert await asyncio.to_thread(second_finished.wait, 5), "second write did not finish"
    finally:
        release_first.set()

    slow_event = await asyncio.wait_for(slow_writer, timeout=5)
    # 取消写者那行写仍在工作线程里跑完（shield 只弃等待）；等它真实落定。
    for _ in range(100):
        disk = store.read_events(session.session_id)
        if len(disk) == 3:
            break
        await asyncio.sleep(STAGGER_SECONDS)
    disk = store.read_events(session.session_id)
    assert len(entries) >= 2, f"夹具未生效：组提交入场数 {len(entries)} < 2"
    assert [e.seq for e in disk] == [0, slow_event.seq, slow_event.seq + 1], (
        f"盘面非预期：{[e.seq for e in disk]}"
    )
    memory_seq = [e.seq for e in session.events]
    assert memory_seq == [e.seq for e in disk], (
        f"取消更高 seq 写者后内存与盘面分叉（审查 F1 倒插形状）："
        f"memory={memory_seq}，disk={[e.seq for e in disk]}"
    )


@pytest.mark.asyncio
async def test_wait_gate_settled_wakes_on_idle_and_survives_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wait_gate_settled：闸门在途时不返回、清空即唤醒；等待中途取消不残留。"""
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)
    entered = _record_store_writes(monkeypatch, store)
    _install_staggered_commit(monkeypatch, writers=2)  # entry0（首个写者）睡 30ms

    writer = asyncio.create_task(session.append_async(USER_MESSAGE, {"content": "w"}))
    await _until(lambda: "w" in entered)

    settled = asyncio.create_task(session.wait_gate_settled())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not settled.done(), "闸门仍在途时 settle 等待不得提前返回"

    await asyncio.wait_for(writer, timeout=5)
    await asyncio.wait_for(settled, timeout=5)  # 闸门清空即唤醒

    # 取消路径：等待者中途取消后必须从登记表移除，后续 settle 调用仍可用。
    writer2 = asyncio.create_task(session.append_async(USER_MESSAGE, {"content": "w2"}))
    await _until(lambda: "w2" in entered)
    settled2 = asyncio.create_task(session.wait_gate_settled())
    await asyncio.sleep(0)
    settled2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await settled2
    assert session._gate_settled_waiters == [], "取消的 settle 等待者残留登记表"
    await asyncio.wait_for(writer2, timeout=5)
    await asyncio.wait_for(session.wait_gate_settled(), timeout=5)
