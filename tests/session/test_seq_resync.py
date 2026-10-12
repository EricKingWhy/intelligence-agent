"""#1024 / #1055 —— 事件序号双写者竞争：**交付给 runtime 的聚合**的 resync 契约。

**症状**（真机混沌实测，`main @ 21593a87`）：run 存活期间后台记忆作业
（`memory/v2/runner.SessionEventSink`，每次 `read_events` 重建 Session 再写
formation 事件）抢占了活聚合下一步要用的号 ⇒ 活聚合下一次 append 撞
`SeqConflict` ⇒ Agent Loop 异常终止 ⇒ **失败终态走同一枚过期计数器必然再撞**、
被运行时兜底吞掉 ⇒ run 永远没有 `run/completed` / `run/failed`，UI 永久
「思考中」（9 个会话实锤、非首轮 run 约 30% 挂死）。

**不变量**：seq 是「单 Session 内单调递增」的单一真相（spec 03 §2）；分配权可以
被多个写者争，但**撞号必须可重同步**，而终态写入不得因一枚过期缓存而静默丢失
（不变量 #3 append-only 事实源 + 规格 03 §5「Run 状态由 SessionEvent 派生」）。

**判据是「交付点」而不是「构造方式」**（#1055）：只有被显式交给在途 runtime 的
聚合（`Session.deliver_to_runtime()`，交付点见类 docstring）才吃 resync。本文件
用**三个构造入口**（`start` / `load` / `resume`）分别验证——真机事故全发生在
`load`/`resume` 交付的聚合上，只测 `start` 会漏掉整条续聊路径。

**协议形状**（方案依据，`AGENTS.md` §6.1）：存储层 fail-closed 拒写 = EventStoreDB
的 `WrongCurrentVersionError`（KurrentDB Python client 官方文档 "Appending events"：
一致性和 idempotency 检查失败即抛该错，客户端重读再投）＝ Marten 的
`ConcurrencyException`（martendb.io `events/appending.md` / `documents/concurrency.md`：
`FetchForWriting` 取流当前版本、`SaveChanges` 撞版本即抛错，由写者侧重取重试）。
Marten 由 **API 入口显式**决定聚合是否「被追踪」（fetch-for-writing vs 普通 load），
不靠构造参数猜——`deliver_to_runtime()` 即这条 PORT DESIGN 的落点。

本文件**只钉读写两侧的可观察行为**：日志里的 seq 序列、事件条数、终态存在性，
以及「resync 真的发生过」。确定性注入（`_SeqThiefStore`）优先于线程赛跑。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

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

#: 三个构造入口。#1055 的 P0 正是「只测 start」漏掉的：服务层交给 runtime 的那一枚
#: 来自 load（`resume_and_launch` 无恢复腿 / `_commit_paused_resume`）与 resume
#: （`resume_and_launch` 的 deferred 腿）。
ENTRIES = ["start", "load", "resume"]


@pytest.fixture
def store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path)


def _bypass_write(store: JsonlSessionStore, session_id: str, count: int = 1) -> None:
    """复刻旁路写者的真实形状（`SessionEventSink.emit` / `Session.append_event`）：
    每次从**盘上**重建一枚独立聚合再写——它自己的计数器看得见活聚合的推进，
    活聚合的计数器却看不见它。刻意**不**调用 `deliver_to_runtime()`（#1055：
    它没有交付给在途 runtime，收口归它自己的 `_SINK_ATTEMPTS` 循环）。"""
    bypass = Session(session_id, store, events=store.read_events(session_id))
    for index in range(count):
        bypass.append(MEMORY_DEGRADED, {"operation": "formation", "n": index}, run_id="bg")


def _seqs(store: JsonlSessionStore, session_id: str) -> list[int]:
    return [event.seq for event in store.read_events(session_id)]


def _delivered_run(
    store: JsonlSessionStore, session_id: str, entry: str
) -> tuple[Session, str]:
    """按 ``entry`` 构造一枚**已交付 runtime** 的聚合 + 一个在途 run。

    三个入口与真机一致：`start` = 首轮 run（`create_and_launch`）；`load` / `resume`
    = 续聊 run（`resume_and_launch` 两条腿）——后两者先造出历史再取回聚合。
    交付点本身（`RunManager.launch` / CLI / 子 runtime）在所有入口上都是
    `deliver_to_runtime()`，故这里统一显式调用。
    """
    if entry == "start":
        live = Session.start(store, session_id=session_id)
        live.append(USER_MESSAGE, {"content": "hi"})
        live.deliver_to_runtime()
        run_id, _ = live.begin_run()
        return live, run_id

    # 先有一段已完成的历史（首轮 run），再走续聊入口取回「交给 runtime 的那一枚」。
    seed = Session.start(store, session_id=session_id)
    seed.append(USER_MESSAGE, {"content": "hi"})
    seed.end_run(seed.begin_run()[0], status="completed", final_text="first")
    live = (
        Session.load(store, session_id)
        if entry == "load"
        else Session.resume(store, session_id)
    )
    live.deliver_to_runtime()
    run_id, _ = live.begin_run()
    return live, run_id


class _SeqThiefStore(JsonlSessionStore):
    """旁路写者的**确定性**模型：在活聚合落盘前，把它这次要用的号先占掉（写一条
    别的类型的事件），连抢 ``steals`` 次后收手。用它替代线程竞争，是为了让用例
    可判定——线程版的命中率由调度决定（#1024 实测 30 次 append 时 3/8、200 次时
    0/6，不随规模单调），无法当守门用例。

    ``stolen`` 记录真实抢号次数，供用例断言「resync 真的发生过」（#1056）。
    """

    def __init__(self, *, root: Path, steals: int) -> None:
        super().__init__(root=root)
        self.steals = steals
        self.stolen = 0

    def append_event(self, session_id: str, event) -> None:
        if self.steals > 0:
            self.steals -= 1
            self.stolen += 1
            super().append_event(
                session_id,
                replace(
                    event,
                    event_id=str(uuid4()),
                    type=MEMORY_DEGRADED,
                    data={"operation": "formation"},
                ),
            )
        super().append_event(session_id, event)


class TestDeliveredAggregateResync:
    @pytest.mark.parametrize("entry", ENTRIES)
    def test_append_survives_bypass_writer_stealing_next_seq(self, store, entry):
        """AC1：旁路写者抢占 seq 后，已交付 runtime 的聚合下一次 append 仍成功，
        且 seq 连续无重复——**三个构造入口**各验一遍（#1055 的 P0 就在这条）。"""
        live, run_id = _delivered_run(store, "s-ac1", entry)
        stolen = live.next_seq
        _bypass_write(store, "s-ac1")

        event = live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)

        assert event.seq > stolen, "resync 后必须落到被占号之后，而不是原地重投"
        seqs = _seqs(store, "s-ac1")
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs), "seq 不得重复"

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_append_after_resync_does_not_reconflict(self, store, entry):
        """AC1（续）：resync 成功后计数器已跟上盘面，后续 append 不再进冲突分支。"""
        live, run_id = _delivered_run(store, "s-ac1b", entry)
        _bypass_write(store, "s-ac1b", count=3)
        live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)
        live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)
        seqs = _seqs(store, "s-ac1b")
        assert seqs == list(range(len(seqs)))

    @pytest.mark.parametrize("entry", ENTRIES)
    @pytest.mark.parametrize(
        ("status", "terminal"),
        [("completed", RUN_COMPLETED), ("failed", RUN_FAILED)],
    )
    def test_stale_counter_still_lands_terminal(self, store, entry, status, terminal):
        """AC2：计数器过期后，end_run 的 run/completed **与** run/failed 两条均能落盘。"""
        live, run_id = _delivered_run(store, f"s-ac2-{status}", entry)
        _bypass_write(store, f"s-ac2-{status}")

        event = live.end_run(run_id, status=status, reason="boom", message="boom")

        assert event.type == terminal
        # 只看**本次 run** 的终态：load/resume 入口的 seed 历史里已有一条上一轮 run 的
        # 终态，按类型全量计数会把它算进来（seed 是 fixture，不是被测对象）。
        mine = [
            e for e in store.read_events(f"s-ac2-{status}") if e.run_id == run_id
        ]
        assert [e.type for e in mine].count(terminal) == 1

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_conflict_raises_when_not_delivered(self, store, entry):
        """#1055 反向边界：**没有**交付给 runtime 的聚合撞号必须原样抛出。

        收口归调用方 owner（`change_model` 一族的 `_WRITE_CONFLICT_ATTEMPTS`、
        sink 的 `_SINK_ATTEMPTS`）；本层替它们吞掉会让那些上界与失败归因不可观测。
        """
        if entry == "start":
            live = Session.start(store, session_id="s-nodeliver")
        else:
            seed = Session.start(store, session_id="s-nodeliver")
            seed.append(USER_MESSAGE, {"content": "hi"})
            seed.end_run(seed.begin_run()[0], status="completed", final_text="first")
            live = (
                Session.load(store, "s-nodeliver")
                if entry == "load"
                else Session.resume(store, "s-nodeliver")
            )
        # 刻意不调用 deliver_to_runtime()
        _bypass_write(store, "s-nodeliver")
        with pytest.raises(SeqConflict):
            live.append(MEMORY_DEGRADED, {"operation": "formation"})


class TestNoTerminalLossUnderInterleaving:
    @pytest.mark.parametrize("entry", ENTRIES)
    def test_terminal_always_present_when_background_writer_interleaves(self, store, entry):
        """AC3：后台记忆作业与 run 并发（最坏情形：每轮都写 degraded）时终态缺失率 = 0，
        且事件流里恰好一条终态。三个构造入口各跑一轮（#1055）。

        后台写者用真线程 + 真盘竞争（不是 mock 时序）：活聚合与旁路写者各自持一枚
        计数器，撞号在 store 写锁内 fail-closed，resync 在交付聚合一侧闭合。
        """
        rounds = 12
        missing: list[str] = []
        for index in range(rounds):
            session_id = f"s-ac3-{entry}-{index}"
            live, run_id = _delivered_run(store, session_id, entry)
            stop = threading.Event()

            def background(sid: str = session_id, halt: threading.Event = stop) -> None:
                while not halt.is_set():
                    try:
                        _bypass_write(store, sid)
                    except SeqConflict:
                        # 旁路写者自己撞号是正常的（它也有自己的重读重试），
                        # 这里只关心它有没有把活聚合的终态挤掉。
                        pass
                    # 不等待：把竞争者开到**最大强度**（AC 要求的「最坏情形」）。
                    halt.wait(0)

            writer = threading.Thread(target=background, daemon=True)
            writer.start()
            try:
                for _ in range(4):
                    live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)
            finally:
                stop.set()
                writer.join(timeout=5)
                # #1056：join 超时必须让用例红，不许把 CI 挂死——本仓无
                # pytest-timeout，用例自身兜底（守护线程会随进程退出，但「没退出」
                # 意味着给的竞争窗口没关掉，结论不可信）。
                assert not writer.is_alive(), "后台写者必须在 join 超时前退出"
            live.end_run(run_id, status="completed", final_text="done")

            events = store.read_events(session_id)
            # 同样只看本次 run 的终态（load/resume 的 seed 历史自带上一轮的终态）。
            terminals = [
                e for e in events
                if e.type in RUN_TERMINAL_TYPES and e.run_id == run_id
            ]
            if len(terminals) != 1:
                missing.append(session_id)
            seqs = [e.seq for e in events]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

        assert missing == [], f"终态缺失/重复的会话：{missing}"


class TestAppendProgressGuarantee:
    def test_append_survives_more_steals_than_the_old_retry_bound(self, store):
        """AC3（推进保证）：旁路写者**连续**抢号超过旧上界时，交付聚合仍必须落地。

        线程序列化抓这个缺陷是碰运气（实测第二轮代码下命中率由调度决定），不能当
        守门用例。这里改成**确定性注入**：thief store 在聚合每次落盘前先把它选中的号
        占掉（写一条 memory/degraded），连抢 10 次（远超旧上界 3）。次数不能太大：
        thief 每抢一次都要真落盘 fsync，抢到 50 次时它的 fsync 开销本身就把 1s 墙钟
        预算耗光了，反而测不准。

        第二轮实现（撞号即无脑「重读 → 换号」，上界 3 次尝试）在第 3 次就抛
        `SeqConflict` ⇒ 红；第三轮（撞号后核对盘面、只在真被占时换号、自旋重投 +
        墙钟预算）会一直重投到 thief 收手，随即落地 ⇒ 绿。
        """
        # 被测聚合**就是** thief 本身（同一棵树的同一实例，不是跨树旁路）。
        thief = _SeqThiefStore(root=store._root, steals=0)
        live = Session.start(thief, session_id="s-steal")
        live.append(USER_MESSAGE, {"content": "hi"})
        live.deliver_to_runtime()
        run_id, _ = live.begin_run()

        # 开闸：接下来这**一次** append 会连续撞号 10 次（旧的 3 次上界必抛）。
        thief.steals = 10
        live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)

        # #1056：断言 resync **真实发生**（撞号被注入且被消化），而不是「一路没冲突
        # 直接成功」也能绿。
        assert thief.stolen == 10, f"必须真的撞过 10 次号，实际 {thief.stolen}"
        assert thief.steals == 0, "thief 必须被抢空（否则没压到旧上界就过了）"
        events = thief.read_events("s-steal")
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq 不得重复/回退"
        # 10 次抢号各留一条 degraded，加聚合自己的事件 ⇒ 总数可判
        assert sum(1 for e in events if e.type == MEMORY_DEGRADED) == 10


class TestResyncReadsTheDiskInsteadOfWalkingUp:
    def test_retry_jumps_to_disk_max_when_bypass_writer_advanced_far(self, store):
        """撞号后必须**按盘面读数**一次跳到空档，不能只把号 +1 一格一格爬。

        #1056：`TestAppendProgressGuarantee` 抓不到这个——那个 thief 是**连续**占号，
        逐格 +1 恰好也能追上，所以「重投不读盘面、只把号 +1」的变体在它面前全绿（实测
        m7）。真实形状是旁路写者**一次推进一大段**（`SessionEventSink` 落盘、或日志被
        撕裂尾中立化截断），此时逐格爬要 O(距离) 次重投，距离一大会撞满墙钟预算
        fail-loud，而读盘面只要一次。

        判据用**重投次数**（确定性）而不是耗时（机器相关，见 #1057）：正确实现撞号后
        只需 1 次重投，逐格爬需要 K 次。
        """
        advance = 50

        class CountingStore(JsonlSessionStore):
            """统计活聚合自己发起的物理写次数（thief 不参与——这里只测重投形态）。"""

            def __init__(self, *, root: Path) -> None:
                super().__init__(root=root)
                self.appends = 0

            def append_event(self, session_id: str, event) -> None:
                self.appends += 1
                super().append_event(session_id, event)

        counting = CountingStore(root=store._root)
        live = Session.start(counting, session_id="s-advance")
        live.append(USER_MESSAGE, {"content": "hi"})
        live.deliver_to_runtime()
        run_id, _ = live.begin_run()

        # 旁路写者一次推进 advance 段：盘面越过活聚合的计数器 50 个号。
        bypass = Session("s-advance", counting, events=counting.read_events("s-advance"))
        for index in range(advance):
            bypass.append(MEMORY_DEGRADED, {"operation": "formation", "n": index}, run_id="bg")
        ahead = counting.last_event_seq("s-advance")
        assert ahead >= live.next_seq + advance - 1, "盘面必须真的越到前面去"

        before = counting.appends
        event = live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)

        attempts = counting.appends - before
        assert event.seq == ahead + 1, "必须落到盘面最大值之后的第一格"
        assert attempts <= 2, (
            f"撞号后应一次跳到空档（1 次重投），实际写了 {attempts} 次"
            "——逐格 +1 的实现在这里要爬 50 次"
        )


class TestConflictBoundIsFailLoud:
    def test_never_yielding_competitor_raises_instead_of_claiming_success(self, store):
        """撞满墙钟预算必须 **fail-loud**：抛 `SeqConflict`，绝不把没落盘的事件当成写成。

        `_persist_event` 成功路径后面紧跟着 `self._events.append(event)` 与
        `_next_seq = event.seq + 1`。所以「预算耗尽时 `break` 而不是 `raise`」会让
        内存投影声称一条盘上并不存在的事件、计数器也前进——正是 #1024 事故里真正的
        静默失真源（比抛错危险得多）。本用例让 thief **永不停手**：聚合必须在预算内
        抛错，且内存投影不得多出那条事件。
        """
        thief = _SeqThiefStore(root=store._root, steals=0)
        live = Session.start(thief, session_id="s-never")
        live.append(USER_MESSAGE, {"content": "hi"})
        live.deliver_to_runtime()
        run_id, _ = live.begin_run()
        before = len(live._events)
        stolen_before = thief.stolen

        thief.steals = 10**9  # 永不停手（每次撞号它都真落盘，聚合永远抢不到）
        with pytest.raises(SeqConflict):
            live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)

        # #1056：确认真的走了「撞号 → 重投」路径（不是别的失败形状冒充）。
        assert thief.stolen > stolen_before, "必须真的撞过号才谈得上预算耗尽"
        # 内存投影不得多出那条事件——这是「假成功」的判据。
        # （`next_seq` 在重投期被按盘面纠正过，它会前进到「盘上最大 + 1」，这是缓存
        # 语义、不是写入成功的信号，故不对它下断言。）
        assert len(live._events) == before, "没落盘的事件不得进内存投影（不许假成功）"
        assert all(e.type != "model/request-started" for e in live._events)


class TestConflictIsNotSwallowedForOtherFailures:
    def test_non_conflict_store_failure_still_propagates(self, store):
        """resync 只认 `SeqConflict`：其它存储故障仍按原契约向上抛（防误吞）。"""

        class Boom(JsonlSessionStore):
            """第 2 次写起抛非冲突故障。

            注入点挂在 store 的物理写入口上：`Session.start` 的 `session/started` 是
            第 1 次（seq 守卫与真实写盘都在 `append_event` 之下，故必然计数），
            第 2 次即抛。"""

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
        live.deliver_to_runtime()
        with pytest.raises(RuntimeError, match="磁盘故障"):
            live.append(MODEL_REQUEST_STARTED, {"model": "m"})
        assert boom.calls == 2, "注入的故障必须真的发生在第 2 次写上"
