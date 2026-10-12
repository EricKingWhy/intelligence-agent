"""#1024 —— 事件序号双写者竞争：活聚合 resync 契约。

**症状**（真机混沌实测，`main @ 21593a87`）：run 存活期间后台记忆作业
（`memory/v2/runner.SessionEventSink`，每次 `read_events` 重建 Session 再写
formation 事件）抢占了活聚合下一步要用的号 ⇒ 活聚合下一次 append 撞
`SeqConflict` ⇒ Agent Loop 异常终止 ⇒ **失败终态走同一枚过期计数器必然再撞**、
被运行时兜底吞掉 ⇒ run 永远没有 `run/completed` / `run/failed`，UI 永久
「思考中」（9 个会话实锤、非首轮 run 约 30% 挂死）。

**不变量**：seq 是「单 Session 内单调递增」的单一真相（spec 03 §2）；分配权可以
被多个写者争，但**撞号必须可重同步**，而终态写入不得因一枚过期缓存而静默丢失
（不变量 #3 append-only 事实源 + 规格 03 §5「Run 状态由 SessionEvent 派生」）。

**协议形状**（方案依据，`AGENTS.md` §6.1）：存储层 fail-closed 拒写 = EventStoreDB
的 `WrongCurrentVersionError`（KurrentDB Python client 官方文档 "Appending events"：
一致性和 idempotency 检查失败即抛该错，客户端重读再投）＝ Marten 的
`ConcurrencyException`（martendb.io `events/appending.md` / `documents/concurrency.md`：
`FetchForWriting` 取流当前版本、`SaveChanges` 撞版本即抛错，由写者侧重取重试）。
两边都把「冲突」当协议的一部分，而不是终局。

本文件**只钉读写两侧的可观察行为**：日志里的 seq 序列、事件条数、终态存在性。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.errors import SeqConflict
from agent_harness.session.event import (
    MEMORY_DEGRADED,
    MODEL_REQUEST_STARTED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_TERMINAL_TYPES,
    USER_MESSAGE,
)


@pytest.fixture
def store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path)


def _bypass_write(store: JsonlSessionStore, session_id: str, count: int = 1) -> None:
    """复刻旁路写者的真实形状（`SessionEventSink.emit` / `Session.append_event`）：
    每次从**盘上**重建一枚独立聚合再写——它自己的计数器看得见活聚合的推进，
    活聚合的计数器却看不见它。"""
    bypass = Session(session_id, store, events=store.read_events(session_id))
    for index in range(count):
        bypass.append(MEMORY_DEGRADED, {"operation": "formation", "n": index}, run_id="bg")


def _seqs(store: JsonlSessionStore, session_id: str) -> list[int]:
    return [event.seq for event in store.read_events(session_id)]


def _live_run(store: JsonlSessionStore, session_id: str) -> tuple[Session, str]:
    """一枚活聚合 + 一个在途 run（`session/started` + `user/message` + `run/started`）。"""
    live = Session.start(store, session_id=session_id)
    live.append(USER_MESSAGE, {"content": "hi"})
    run_id, _ = live.begin_run()
    return live, run_id


class TestLiveAggregateResync:
    def test_append_survives_bypass_writer_stealing_next_seq(self, store):
        """AC1：旁路写者抢占 seq 后，活聚合下一次 append 仍成功，且 seq 连续无重复。"""
        live, run_id = _live_run(store, "s-ac1")
        stolen = live.next_seq
        _bypass_write(store, "s-ac1")

        event = live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)

        assert event.seq > stolen, "resync 后必须落到被占号之后，而不是原地重投"
        seqs = _seqs(store, "s-ac1")
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs), "seq 不得重复"

    def test_append_after_resync_does_not_reconflict(self, store):
        """AC1（续）：resync 成功后计数器已跟上盘面，后续 append 不再进冲突分支。"""
        live, run_id = _live_run(store, "s-ac1b")
        _bypass_write(store, "s-ac1b", count=3)
        live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)
        live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)
        seqs = _seqs(store, "s-ac1b")
        assert seqs == list(range(len(seqs)))

    @pytest.mark.parametrize(
        ("status", "terminal"),
        [("completed", RUN_COMPLETED), ("failed", RUN_FAILED)],
    )
    def test_stale_counter_still_lands_terminal(self, store, status, terminal):
        """AC2：计数器过期后，end_run 的 run/completed **与** run/failed 两条均能落盘。"""
        live, run_id = _live_run(store, f"s-ac2-{status}")
        _bypass_write(store, f"s-ac2-{status}")

        event = live.end_run(run_id, status=status, reason="boom", message="boom")

        assert event.type == terminal
        types = [e.type for e in store.read_events(f"s-ac2-{status}")]
        assert types.count(terminal) == 1


class TestNoTerminalLossUnderInterleaving:
    def test_terminal_always_present_when_background_writer_interleaves(self, store):
        """AC3：后台记忆作业与 run 并发（最坏情形：每轮都写 degraded）时终态缺失率 = 0，
        且事件流里恰好一条终态。

        后台写者用真线程 + 真盘竞争（不是 mock 时序）：活聚合与旁路写者各自持一枚
        计数器，撞号在 store 写锁内 fail-closed，resync 在活聚合一侧闭合。
        """
        rounds = 12
        missing: list[str] = []
        for index in range(rounds):
            session_id = f"s-ac3-{index}"
            live, run_id = _live_run(store, session_id)
            stop = threading.Event()

            def background(sid: str = session_id, halt: threading.Event = stop) -> None:
                while not halt.is_set():
                    try:
                        _bypass_write(store, sid)
                    except SeqConflict:
                        # 旁路写者自己撞号是正常的（它也有自己的重读重试），
                        # 这里只关心它有没有把活聚合的终态挤掉。
                        pass
                    halt.wait(0.0005)

            writer = threading.Thread(target=background, daemon=True)
            writer.start()
            try:
                for _ in range(4):
                    live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)
            finally:
                stop.set()
                writer.join(timeout=5)
            live.end_run(run_id, status="completed", final_text="done")

            events = store.read_events(session_id)
            terminals = [e for e in events if e.type in RUN_TERMINAL_TYPES]
            if len(terminals) != 1:
                missing.append(session_id)
            seqs = [e.seq for e in events]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

        assert missing == [], f"终态缺失/重复的会话：{missing}"


class TestConflictIsNotSwallowedForOtherFailures:
    def test_non_conflict_store_failure_still_propagates(self, store):
        """resync 只认 `SeqConflict`：其它存储故障仍按原契约向上抛（防误吞）。"""

        class Boom(JsonlSessionStore):
            """第 2 次写起抛非冲突故障（第 1 次让 `Session.start` 正常建会话）。"""

            def __init__(self, root) -> None:
                super().__init__(root=root)
                self.calls = 0

            def append_event(self, session_id, event):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("磁盘故障（注入）")
                super().append_event(session_id, event)

        boom = Boom(root=store._root)
        live = Session.start(boom, session_id="s-boom")
        with pytest.raises(RuntimeError, match="磁盘故障"):
            live.append(MODEL_REQUEST_STARTED, {"model": "m"})
