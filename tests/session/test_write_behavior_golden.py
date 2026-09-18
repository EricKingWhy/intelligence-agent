"""#251：Session durable 写行为 golden——先冻结现状，再谈单一漏斗。

父票 #241 的第一阶段子票（`#252` 抽漏斗前的行为基线）。本模块**只钉行为、
不改行为**：每条用例都对应"将来抽漏斗时最容易被改错的那一处"，一旦 `#252`
的迁移改动了其中任何一条，这里必须红。

六条被冻结的语义：

1. `append` 失败（store 拒写）时**内存事件与 next_seq 都不推进**，且失败后
   仍可继续正常 append（seq 不留空洞、复用同一号）；
2. `append` 词表校验在**写盘之前**：未知类型 / 流式专属类型抛 ValueError，
   磁盘与内存都零影响；
3. `append` 在持久化成功**之后**同步通知 listener；listener 抛异常不破坏
   append 契约（事件已落盘、返回值照给）；
4. `adopt_history` **不通知** listener（离线 seed 初始化，ADR-0017 决策 3
   冻结）；
5. `adopt_history` 重编 seq + 改写 session_id，**逐字保留** event_id / time /
   data / run_id / agent_id / step_id / source_event_ids；
6. `adopt_history` **逐条** durable append，不是先整批校验——列表中间出现非法
   事件时，**已经落盘的前缀保留**（部分写入是当前语义，`#252` 不得暗改；
   若要改成先整批校验，必须先改本用例并披露）。
"""

from __future__ import annotations

import pytest

from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_STARTED,
    SESSION_FORKED,
    SESSION_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.event import MODEL_DELTA, SessionEvent
from agent_harness.session.store import SeqConflict

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path / "sessions")


class _RejectingStore(JsonlSessionStore):
    """append_event 一律拒写：模拟 seq 冲突 / 存储故障这一类失败。"""

    def __init__(self, root, error_type: type[Exception] = SeqConflict) -> None:
        super().__init__(root=root)
        self._error_type = error_type

    def append_event(self, session_id: str, event: SessionEvent) -> None:
        raise self._error_type("拒写（注入）")


class _FailingFrom(JsonlSessionStore):
    """第 n 次 append_event 起拒写：注入"写盘中途失败"。"""

    def __init__(self, root, *, fail_from: int) -> None:
        super().__init__(root=root)
        self._calls = 0
        self._fail_from = fail_from

    def append_event(self, session_id: str, event: SessionEvent) -> None:
        self._calls += 1
        if self._calls >= self._fail_from:
            raise RuntimeError(f"磁盘故障（注入，第 {self._calls} 次写）")
        super().append_event(session_id, event)


# ---- 1. append 失败：内存与 next_seq 不越过失控事件 ----


async def test_append_failure_leaves_memory_and_next_seq_untouched(tmp_path) -> None:
    """store 拒写时：不追加内存事件、不消耗 seq，且失败后可继续正常 append。

    "失败不消耗 seq"是 durable 日志无空洞的保证——`Session.load/resume` 要求
    事件 seq 连续，一次失败的 append 若推了计数器，下一次成功的 append 就会
    在磁盘上留下一个永久空洞。
    """
    good = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(good, session_id="s1")
    before_events = len(session.events)
    before_seq = session.next_seq

    rejecting = _RejectingStore(tmp_path / "sessions")
    session._store = rejecting  # 只换物理写口，聚合状态不变
    with pytest.raises(SeqConflict):
        session.append(USER_MESSAGE, {"content": "写不进去"})

    assert len(session.events) == before_events, "失败的事件不得进内存"
    assert session.next_seq == before_seq, "失败不得消耗 seq"
    assert len(good.read_events("s1")) == before_events, "磁盘也不得变"

    # 恢复写口后下一条 append 复用同一个号（没有空洞）
    session._store = good
    event = session.append(USER_MESSAGE, {"content": "这条能进去"})
    assert event.seq == before_seq
    assert [e.seq for e in good.read_events("s1")] == list(range(before_seq + 1))


# ---- 2. 词表校验先于写盘 ----


@pytest.mark.parametrize(
    ("event_type", "message"),
    [
        ("no/such/type", "不在 EVENT_TYPES 词汇表中"),
        (MODEL_DELTA, "流式专属事件"),
    ],
)
async def test_append_rejects_before_touching_disk(store, event_type, message) -> None:
    """未知类型与流式专属类型都在写盘前拒绝：磁盘与内存零影响。"""
    session = Session.start(store, session_id="s1")
    before = list(session.events)

    with pytest.raises(ValueError, match=message):
        session.append(event_type, {})

    assert session.events == before
    assert store.read_events("s1") == before


# ---- 3. listener：落盘后通知，异常不破坏 append 契约 ----


async def test_append_notifies_listener_after_persist(store) -> None:
    """listener 在持久化之后被同步调用，且拿到的是已落盘的那条事件。"""
    session = Session.start(store, session_id="s1")
    seen: list[SessionEvent] = []
    session.add_listener(seen.append)

    event = session.append(USER_MESSAGE, {"content": "hi"})

    assert seen == [event]
    assert store.read_events("s1")[-1] == event, "通知时事件已在磁盘上"


async def test_listener_exception_does_not_break_append(store) -> None:
    """listener 是观察者：抛异常被吞（落日志），append 照常返回、事件照常落盘。"""
    session = Session.start(store, session_id="s1")

    def boom(_event: SessionEvent) -> None:
        raise RuntimeError("listener 挂了")

    session.add_listener(boom)
    event = session.append(USER_MESSAGE, {"content": "hi"})

    assert event.type == USER_MESSAGE
    assert store.read_events("s1")[-1] == event


async def test_append_failure_does_not_notify_listener(tmp_path) -> None:
    """持久化失败 = 什么都没发生：listener 不得收到那条没落盘的事件。"""
    good = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(good, session_id="s1")
    seen: list[SessionEvent] = []
    session.add_listener(seen.append)

    session._store = _RejectingStore(tmp_path / "sessions")
    with pytest.raises(SeqConflict):
        session.append(USER_MESSAGE, {"content": "写不进去"})

    assert seen == []


# ---- 4/5/6. adopt_history：不通知、逐字保留、逐条落盘 ----


def _parent_event(seq: int, event_type: str, data: dict) -> SessionEvent:
    return SessionEvent(
        event_id=f"evt-{seq}",
        seq=seq,
        time="2026-01-02T03:04:05.000+00:00",
        type=event_type,
        session_id="parent",
        run_id="run-parent",
        agent_id="researcher",
        step_id=7,
        data=data,
        source_event_ids=["src-1"],
    )


async def test_adopt_history_rewrites_seq_and_session_id_only(store) -> None:
    """AC3：仅重编 seq / 改写 session_id；其余身份字段逐字保留。"""
    session = Session.start(store, session_id="child")
    seed = [
        _parent_event(0, USER_MESSAGE, {"content": "第一条"}),
        _parent_event(1, RUN_STARTED, {}),
        _parent_event(2, MODEL_COMPLETED, {"content": "答"}),
        _parent_event(3, RUN_COMPLETED, {}),
    ]

    adopted = session.adopt_history(seed)

    assert [e.seq for e in adopted] == [1, 2, 3, 4], "seq 从 child 当前位置重编"
    for original, moved in zip(seed, adopted, strict=True):
        assert moved.session_id == "child"
        assert moved.event_id == original.event_id
        assert moved.time == original.time
        assert moved.type == original.type
        assert moved.data == original.data
        assert moved.run_id == original.run_id
        assert moved.agent_id == original.agent_id
        assert moved.step_id == original.step_id
        assert moved.source_event_ids == original.source_event_ids
    assert [e.seq for e in store.read_events("child")][1:] == [1, 2, 3, 4]


async def test_adopt_history_does_not_notify_listeners(store) -> None:
    """AC4：`adopt_history` 是离线 seed 初始化，**不**通知实时 listener。

    listener 的存在意义是"广播正在发生的 run"（web 层据此推 SSE）；fork 的
    seed 是历史事实的移植，广播它等于把父会话的过去当成 child 的现场。
    """
    session = Session.start(store, session_id="child")
    seen: list[SessionEvent] = []
    session.add_listener(seen.append)

    session.adopt_history([_parent_event(0, USER_MESSAGE, {"content": "旧事"})])

    assert seen == [], "adopt_history 不得触发 listener"

    # 对照：同一 listener 对 append 仍然生效（证明它确实注册成功了）
    session.append(SESSION_FORKED, {})
    assert len(seen) == 1 and seen[0].type == SESSION_FORKED


@pytest.mark.parametrize(
    ("event_type", "message"),
    [
        ("no/such/type", "不在 EVENT_TYPES 词汇表中"),
        (MODEL_DELTA, "流式专属事件"),
    ],
)
async def test_adopt_history_keeps_already_written_prefix(store, event_type, message) -> None:
    """AC5（部分写入语义冻结）：逐条落盘 ⇒ 中间出现非法事件时前缀**保留**。

    这是当前语义，不是理想语义；`#252` 若改成"先整批校验再落盘"，必须先改本
    用例并披露——暗改会让 fork 的失败清理边界悄悄变形。
    """
    session = Session.start(store, session_id="child")
    seed = [
        _parent_event(0, USER_MESSAGE, {"content": "合法前缀"}),
        _parent_event(1, event_type, {}),
        _parent_event(2, USER_MESSAGE, {"content": "永不落盘"}),
    ]

    with pytest.raises(ValueError, match=message):
        session.adopt_history(seed)

    assert [e.type for e in session.events] == [SESSION_STARTED, USER_MESSAGE]
    assert [e.type for e in store.read_events("child")] == [SESSION_STARTED, USER_MESSAGE]
    assert session.next_seq == 2


async def test_adopt_history_mid_write_failure_keeps_prefix(tmp_path) -> None:
    """AC5（存储故障版）：写盘中途失败时，已落盘的前缀保留、异常原样上抛。

    调用方（fork 主流程）负责决定前缀的去留——见
    `tests/session/test_fork.py` 的 fork 失败孤儿用例。
    """
    root = tmp_path / "sessions"
    base = JsonlSessionStore(root=root)
    session = Session.start(base, session_id="child")
    failing = _FailingFrom(root, fail_from=2)  # 第 2 次写 = seed 第二项

    session._store = failing
    seed = [
        _parent_event(0, USER_MESSAGE, {"content": "第一条"}),
        _parent_event(1, USER_MESSAGE, {"content": "第二条"}),
        _parent_event(2, USER_MESSAGE, {"content": "第三条"}),
    ]

    with pytest.raises(RuntimeError, match="磁盘故障"):
        session.adopt_history(seed)

    durable = base.read_events("child")
    assert [e.data["content"] for e in durable[1:]] == ["第一条"], "只落了第一条"
    assert session.next_seq == 2, "seq 恰好推进到已落盘的前缀之后"
