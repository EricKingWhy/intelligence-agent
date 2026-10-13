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

    ``stolen`` 记录真实抢号次数，供用例断言「撞号真的发生过」（#1056）；``calls``
    记录**物理写调用次数**，供 A（#1062）断言「已交付写一次闭合、无自旋重投回边」。
    """

    def __init__(self, *, root: Path, steals: int) -> None:
        super().__init__(root=root)
        self.steals = steals
        self.stolen = 0
        self.calls = 0

    def append_event(self, session_id: str, event, *, allocate: bool = False):
        # A（#1062）：thief 是**对手**的形状，不参与协商；`allocate` 收下只用于转发，
        # 抢号行为与真实写盘都保持原样（抢到的号仍走真实 store 的锁内判定）。
        self.calls += 1
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
        return super().append_event(session_id, event, allocate=allocate)


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
    def test_delivered_append_lands_in_one_store_call_under_sustained_steal(self, store):
        """A（#1062）：已交付聚合在**永不停手**的对手下仍一次写落地——推进由构造保证。

        旧实现（#1024）是会话侧**自旋重投**：撞号 → 读盘面 → 换号 → 再 append，直到
        thief 收手才落地；对手不收手就撞满墙钟预算抛出。A 把「取号+判定+写盘」收进
        store 的**同一临界区**，故撞号在锁内一次改号即闭合，**不需要重投**。

        判据换了形状但更硬：一次 `live.append` 只触发**一次** `store.append_event`
        调用（`thief.calls == 1`），且事件确实落盘、seq 连续。thief 抢得再多，也只是
        这一次写内部的对手——写锁在读 `last` 之前已把它挡在门外，故它抢到几次都不会
        变成会话侧的多次重投。
        """
        thief = _SeqThiefStore(root=store._root, steals=0)
        live = Session.start(thief, session_id="s-steal")
        live.append(USER_MESSAGE, {"content": "hi"})
        live.deliver_to_runtime()
        run_id, _ = live.begin_run()

        # 开闸：对手在这一**次**写里抢号。A 之后它只能抢到**一次**——因为「取号+判定+
        # 写盘」同在写锁内，锁在读 `last` 之前已把它挡在门外；它抢到的号当场被 skip 掉，
        # 不存在「抢 10 次 = 会话侧重投 10 轮」的形状（那正是旧自旋的产物）。
        calls_before = thief.calls
        stolen_before = thief.stolen
        thief.steals = 10
        event = live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)

        # A 的硬判据：只发生**一次**物理写调用（没有自旋重投的回边）。
        assert thief.calls - calls_before == 1, (
            f"锁内分配应一次写闭合，实际 store 调用 {thief.calls - calls_before} 次"
        )
        # 对手至多抢到一次（写锁在它之后就不再放它进来）——这正是 A 消灭自旋的形状。
        assert thief.stolen - stolen_before == 1, (
            f"锁内分配下对手至多抢一次，实际 {thief.stolen - stolen_before} 次"
        )
        assert thief.steals == 9, "对手还剩 9 次没抢（因为一次写里只轮到它一次）"
        events = thief.read_events("s-steal")
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq 不得重复/回退"
        # 落地的那枚就是返回的那枚（锁内改号后必须同步给调用方/内存投影）。
        assert any(e.seq == event.seq and e.type == event.type for e in events)
        # 对手那一次抢号留一条 degraded；加聚合自己的事件。
        assert sum(1 for e in events if e.type == MEMORY_DEGRADED) == 1


class TestResyncReadsTheDiskInsteadOfWalkingUp:
    def test_allocation_jumps_to_disk_max_when_bypass_writer_advanced_far(self, store):
        """锁内分配必须**按盘面读数**一次跳到空档，不能只把号 +1 一格一格爬。

        #1056 的原始动机（会话侧重投期）在 A（#1062）后移到 store 锁内，但**判据不变**：
        写者一次推进一大段（`SessionEventSink` 落盘、或日志被撕裂尾中立化截断）时，正确
        实现要一次跳到 `盘面最大 + 1`；逐格 +1 要 O(距离) 次——旧实现在此撞满墙钟预算
        fail-loud，A 实现则在锁内一次改号到位。

        判据仍用**物理写次数**（确定性）而不是耗时（机器相关，见 #1057）。
        """
        advance = 50

        class CountingStore(JsonlSessionStore):
            """统计活聚合自己发起的物理写次数（thief 不参与——这里只测重投形态）。"""

            def __init__(self, *, root: Path) -> None:
                super().__init__(root=root)
                self.appends = 0

            def append_event(self, session_id: str, event, *, allocate: bool = False):
                self.appends += 1
                return super().append_event(session_id, event, allocate=allocate)

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
    """A（#1062）改写了这条边界，用例语义随之改写（不是 waive）。

    旧契约（#1024/#1061）：已交付聚合的**普通**写撞满墙钟预算 ⇒ fail-loud 抛
    `SeqConflict`。A 把分配收进 store 写锁后，已交付的普通写改为**锁内改号成功**——
    它不再有「预算」这个概念（没有自旋，一次写内闭合）。**未交付**聚合的快照门原样
    保留（BUG-011）：撞号仍 fail-loud。
    """

    def test_delivered_write_lands_not_raises_under_never_stopping_competitor(self, store):
        """已交付普通写：对手永不停手也**落地**（锁内改号），绝不抛。

        这是 A 的核心效果面，也是旧用例的反面：旧实现要求此处抛 `SeqConflict`，A 之后
        必须落地。内存投影只多出**落盘的那一枚**（不是「假成功」——盘上确有该 seq）。
        """
        thief = _SeqThiefStore(root=store._root, steals=0)
        live = Session.start(thief, session_id="s-never")
        live.append(USER_MESSAGE, {"content": "hi"})
        live.deliver_to_runtime()
        run_id, _ = live.begin_run()
        before = len(live._events)

        thief.steals = 10**9  # 永不停手
        event = live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)

        assert len(live._events) == before + 1, "落地的那一枚应进内存投影"
        assert live._events[-1].seq == event.seq
        # 盘上确实有这枚（不是「内存声称、盘上没有」的假成功）。
        events = thief.read_events("s-never")
        assert any(e.seq == event.seq and e.type == event.type for e in events)

    def test_undelivered_write_still_fails_loud_under_never_stopping_competitor(self, store):
        """未交付聚合的快照门（BUG-011）**不因 A 改变**：撞号仍 fail-loud。

        收口归调用方 owner（`change_model` 一族的 `_WRITE_CONFLICT_ATTEMPTS`、sink 的
        `_SINK_ATTEMPTS`）；本层替它们吞掉会让那些上界与失败归因不可观测。内存投影
        不得多出没落盘的事件。
        """
        thief = _SeqThiefStore(root=store._root, steals=0)
        live = Session.start(thief, session_id="s-nodeliver")
        live.append(USER_MESSAGE, {"content": "hi"})
        # 刻意**不**调用 deliver_to_runtime()
        before = len(live._events)

        thief.steals = 10**9
        with pytest.raises(SeqConflict):
            live.append(MODEL_REQUEST_STARTED, {"model": "m"})
        assert len(live._events) == before, "没落盘的事件不得进内存投影（不许假成功）"


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

            def append_event(self, session_id, event, *, allocate: bool = False):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("磁盘故障（注入）")
                return super().append_event(session_id, event, allocate=allocate)

        boom = Boom(root=store._root)
        live = Session.start(boom, session_id="s-boom")
        live.deliver_to_runtime()
        with pytest.raises(RuntimeError, match="磁盘故障"):
            live.append(MODEL_REQUEST_STARTED, {"model": "m"})
        assert boom.calls == 2, "注入的故障必须真的发生在第 2 次写上"


class TestTerminalGuaranteeUnderSustainedSteal:
    """终态在**持续占位**下仍必须落进 durable 流（#1061；A 后是通用路径的子集）。

    #1024 的复现里，失败终态走同一枚过期计数器、撞满即抛，于是盘上永远停在悬空的
    `run/started`，UI 显示「进行中」永久。#1061 给终态开了「锁内改号」保底；A（#1062）
    把这条路径推广到**所有已交付写**——终态只是「即使未交付也要改号」的那一个特例。
    本用例回归验证：推广没有把终态语义改坏（三构造入口 + 两个终态类型）。

    确定性优先：thief 用**永不停手**的注入（不是线程赛跑），三构造入口各验一遍
    （#1055 的教训：只测 `start` 会漏掉整条续聊路径）。
    """

    @pytest.mark.parametrize("entry", ENTRIES)
    @pytest.mark.parametrize(
        ("status", "terminal_type"),
        [("completed", RUN_COMPLETED), ("failed", RUN_FAILED)],
    )
    def test_terminal_lands_while_thief_never_stops(self, store, entry, status, terminal_type):
        thief = _SeqThiefStore(root=store._root, steals=0)
        session_id = f"s-guard-{entry}-{status}"
        live, run_id = _delivered_run(thief, session_id, entry)

        thief.steals = 10**9  # 永不停手
        # 前置：已交付普通写在 A 下也落地（不再抛）——见 TestConflictBoundIsFailLoud。
        live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)
        stolen_before = thief.stolen

        event = live.end_run(run_id, status=status, reason="boom", message="boom")

        assert thief.stolen > stolen_before, "终态那次写必须真的撞过号（否则没压到保底通道）"
        assert event.type == terminal_type
        events = thief.read_events(session_id)
        mine = [e for e in events if e.run_id == run_id and e.type in RUN_TERMINAL_TYPES]
        assert len(mine) == 1, f"该 run 的 durable 终态必须恰好一条，实际 {len(mine)}"
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq 不得重复/回退"
        # 落盘的终态就是返回的那一枚（锁内改号后必须同步给调用方/流式镜像）。
        assert any(e.seq == event.seq and e.type == event.type for e in events)

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_delivered_normal_write_also_lands_while_thief_never_stops(self, store, entry):
        """A 的正面效果面：已交付的**普通**写（非终态）在持续占位下也落地。

        旧用例此处断言 fail-loud（`test_normal_write_still_fails_loud_...`）；A 之后该
        契约被用户裁决改写为「锁内改号成功」，故断言随之反转——但**未交付**的普通写
        仍 fail-loud，那条反面判据搬到了 `TestConflictBoundIsFailLoud`。
        """
        thief = _SeqThiefStore(root=store._root, steals=0)
        live, run_id = _delivered_run(thief, f"s-guard-normal-{entry}", entry)
        before = len(live._events)

        thief.steals = 10**9
        event = live.append(MODEL_REQUEST_STARTED, {"model": "m"}, run_id=run_id)

        assert len(live._events) == before + 1, "落地的普通事件应进内存投影"
        assert live._events[-1].seq == event.seq
        events = thief.read_events(f"s-guard-normal-{entry}")
        assert any(e.seq == event.seq and e.type == event.type for e in events)
