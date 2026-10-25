"""#1526：`read_events_report` 文件戳缓存——分页重建的重复全量读去重。

契约（store.py `_events_cache` 注释的直译）：
* 戳 `(size, mtime_ns)` 未变 → 复用结果；
* 追加（size 必变）→ 以磁盘为准重扫，新事件可见；
* 归还**列表拷贝**——调用方（`Session.__init__` 直接持有传入列表）的追加
  不得写穿缓存；
* LRU 上限 4 个会话（全量事件是 MB 级，不能无界钉住）。
"""

from __future__ import annotations

from pathlib import Path

from agent_harness.session import USER_MESSAGE, JsonlSessionStore, Session, SessionEvent


def _sid(store: JsonlSessionStore, n: int = 3) -> str:
    session = Session.start(store)
    for i in range(1, n):
        store.append_event(
            session.session_id,
            SessionEvent(
                seq=i,
                type=USER_MESSAGE,
                session_id=session.session_id,
                data={"content": f"m{i}"},
            ),
        )
    return session.session_id


def test_cache_reuses_until_stamp_changes(tmp_path: Path) -> None:
    store = JsonlSessionStore(root=tmp_path)
    sid = _sid(store)

    first = store.read_events(sid)
    second = store.read_events(sid)
    assert [e.seq for e in first] == [e.seq for e in second] == [0, 1, 2]

    store.append_event(
        sid,
        SessionEvent(
            seq=3,
            type=USER_MESSAGE,
            session_id=sid,
            data={"content": "new"},
        ),
    )
    third = store.read_events(sid)
    assert [e.seq for e in third] == [0, 1, 2, 3], "追加后缓存必须失效（size 变化）"


def test_returned_list_is_a_copy(tmp_path: Path) -> None:
    """调用方对返回列表的任何修改不得影响缓存/后续读者（Session 持有列表追加）。"""
    store = JsonlSessionStore(root=tmp_path)
    sid = _sid(store)

    held = store.read_events(sid)
    held.pop()  # 模拟调用方（如 Session 内存投影）在自有列表上做手脚
    again = store.read_events(sid)
    assert len(again) == 3, "返回列表与缓存共享可变状态"


def test_sessions_share_no_list_instances(tmp_path: Path) -> None:
    """两个 Session 各自从 read_events 构造 ⇒ 互不写穿（列表级隔离）。"""
    store = JsonlSessionStore(root=tmp_path)
    sid = _sid(store)

    s1 = Session(sid, store, events=store.read_events(sid))
    s2 = Session(sid, store, events=store.read_events(sid))
    s1.append(USER_MESSAGE, {"content": "only-s1"})
    assert len(s2.events) == 3, "s1 的内存投影写进了 s2（列表别名泄漏）"
    assert len(store.read_events(sid)) == 4  # s1 的 append 已落盘


def test_lru_bounded_to_four_sessions(tmp_path: Path) -> None:
    store = JsonlSessionStore(root=tmp_path)
    for _ in range(5):
        _sid(store)
    assert len(store._events_cache) <= 4, "LRU 上限失效（全量事件钉内存无界）"


def test_hit_then_concurrent_eviction_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """命中路径的 LRU 竞态（C1）：`get` 命中后、`move_to_end` 前被并发逐出。

    读侧不加锁（#516 同款取舍）⇒ 另一线程插入第 5 个会话触发 `popitem` 时，
    可能逐掉恰在命中途中的老条目（`.get` 不改序，队首就是它）。该竞态必须
    表现为"本地 miss / 跳过提序"，**不能**让 `move_to_end` 的 KeyError 冒泡
    成端点 500。这里用戳回调注入确定性交错：真复现需要 5 会话并发读的
    微秒级窗口，测试里用「命中路径取戳的瞬间塞满并逐出」等价模拟。
    """
    store = JsonlSessionStore(root=tmp_path)
    sid = _sid(store)
    store.read_events(sid)  # 进缓存（此刻是队首也是队尾——唯一条目）
    path = store._events_path(sid)
    real_stamp = store._summary_stamp
    injected = False

    def racing_stamp(p):
        # 只在**下一次**对本会话取戳时注入（即命中路径的戳比较处），模拟
        # 另一线程在 get 与 move_to_end 之间完成了 4 次插入 + LRU 逐出。
        nonlocal injected
        if not injected and p == path:
            injected = True
            with store._state_guard:
                for i in range(4):
                    store._events_cache[f"other-{i}"] = ((0, 0), ([], None))
                    store._events_cache.move_to_end(f"other-{i}")
                while len(store._events_cache) > 4:
                    store._events_cache.popitem(last=False)  # 逐掉队首 = 本会话
        return real_stamp(p)

    monkeypatch.setattr(store, "_summary_stamp", racing_stamp)

    # 修复前：KeyError 冒泡；修复后：吞掉逐出，按已验证过的缓存条目返回。
    events, _integrity = store.read_events_report(sid)
    assert [e.seq for e in events] == [0, 1, 2]
