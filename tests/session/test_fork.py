"""fork 核心域（Phase 14 T2, #108, ADR-0017 决策 1/2/3/8）。

file-per-lineage：fork = 新 session 文件 + seed 前缀逐字复制（重编 seq、
保留原 event_id）+ session/forked provenance 事件 + meta 索引行。
父文件零改动（spec §7）。boundary = run 完整边界，UX 按用户消息表达。
"""

from __future__ import annotations

import pytest

from agent_harness.session import Session
from agent_harness.session.event import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    SESSION_FORKED,
    SESSION_STARTED,
    USER_MESSAGE,
)
from agent_harness.session.fork import (
    ForkBoundaryError,
    find_fork_boundaries,
    fork_session,
)
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage.sqlite import SqliteSessionMetaStore

pytestmark = pytest.mark.asyncio


def _store(tmp_path) -> JsonlSessionStore:
    return JsonlSessionStore(tmp_path / "sessions")


def _build_parent(store: JsonlSessionStore) -> Session:
    """两轮对话的父会话：seq1=started, 2=user1, 3=run/started, 4=model,
    5=run/completed, 6=user2。"""
    s = Session.start(store, session_id="parent")
    s.append(USER_MESSAGE, {"content": "第一条"})
    s.append(RUN_STARTED, {})
    s.append(MODEL_COMPLETED, {"content": "好的"})
    s.append(RUN_COMPLETED, {})
    s.append(USER_MESSAGE, {"content": "第二条"})
    return s


async def test_fork_seeds_prefix_and_records_provenance(tmp_path) -> None:
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)
    anchor = parent.events[-1].seq  # 第二条用户消息

    child = await fork_session(
        store, meta, "parent", boundary_user_message_seq=anchor,
        child_session_id="child",
    )

    types = [e.type for e in child.events]
    # child 自己的 started + seed（不含父的 started）+ forked 事件
    assert types == [
        SESSION_STARTED, USER_MESSAGE, RUN_STARTED, MODEL_COMPLETED,
        RUN_COMPLETED, SESSION_FORKED,
    ]
    # seed 逐字复制：event_id / time / data 保留，seq 重编为 child 局部单调
    parent_events = parent.events
    # seq 重编为 child 局部单调（0 起头，与 Session 既有约定一致）
    assert child.events[1].event_id == parent_events[1].event_id
    assert child.events[1].time == parent_events[1].time
    assert child.events[1].data == parent_events[1].data
    assert child.events[1].session_id == "child"
    assert [e.seq for e in child.events] == [0, 1, 2, 3, 4, 5]
    forked = child.events[-1]
    assert forked.data["parent_session_id"] == "parent"
    assert forked.data["boundary_user_message_seq"] == anchor
    assert forked.data["fork_point_seq"] == parent_events[4].seq


async def test_fork_child_resumable_and_parent_untouched(tmp_path) -> None:
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)
    anchor = parent.events[-1].seq
    parent_before = store.read_events("parent")

    child = await fork_session(
        store, meta, "parent", boundary_user_message_seq=anchor,
    )

    # 父文件字节级不变（§7）：事件数、类型序、seq 全等——尤其没有 session/resumed
    parent_after = store.read_events("parent")
    assert [e.to_dict() for e in parent_after] == [
        e.to_dict() for e in parent_before
    ]

    # child 可 resume、可投影：模型可见的只有 seed 里的第一条用户消息
    resumed = Session.resume(store, child.session_id)
    from agent_harness.session.derive import derive_messages
    human = [m.content for m in derive_messages(resumed.events)
             if type(m).__name__ == "HumanMessage"]
    assert human == ["第一条"]


async def test_fork_at_first_message_yields_empty_seed(tmp_path) -> None:
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)
    first_user_seq = parent.events[1].seq

    child = await fork_session(
        store, meta, "parent", boundary_user_message_seq=first_user_seq,
        child_session_id="fresh",
    )
    # seed 为空：child = 自己的 started + forked；fork_point_seq 无
    types = [e.type for e in child.events]
    assert types == [SESSION_STARTED, SESSION_FORKED]
    assert child.events[-1].data["fork_point_seq"] is None


async def test_fork_boundary_errors(tmp_path) -> None:
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)

    # 锚点不是用户消息（指向 run/completed）
    with pytest.raises(ForkBoundaryError, match="用户消息"):
        await fork_session(
            store, meta, "parent",
            boundary_user_message_seq=parent.events[4].seq,
        )
    # 锚点不存在：报错列出可用边界
    with pytest.raises(ForkBoundaryError, match=str(parent.events[1].seq)):
        await fork_session(store, meta, "parent", boundary_user_message_seq=999)

    # 前缀含未终态 run：user1 → run/started（未终止）→ user2
    broken = Session.start(store, session_id="broken")
    broken.append(USER_MESSAGE, {"content": "第一条"})
    broken.append(RUN_STARTED, {})
    broken.append(USER_MESSAGE, {"content": "第二条"})
    with pytest.raises(ForkBoundaryError, match="未终态"):
        await fork_session(
            store, meta, "broken",
            boundary_user_message_seq=broken.events[-1].seq,
        )


async def test_fork_writes_meta_index(tmp_path) -> None:
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)
    anchor = parent.events[-1].seq

    await fork_session(
        store, meta, "parent", boundary_user_message_seq=anchor,
        child_session_id="indexed",
    )
    row = await meta.get("indexed")
    assert row is not None
    assert row.parent_session_id == "parent"
    assert row.origin == "fork"
    assert row.fork_point_seq == parent.events[4].seq


async def test_failed_fork_leaves_no_child_artifacts(tmp_path) -> None:
    """fork 失败（boundary 非法）不得留下 child JSONL / meta 行（无孤儿）。"""
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)
    with pytest.raises(ForkBoundaryError):
        await fork_session(
            store, meta, "parent",
            boundary_user_message_seq=parent.events[4].seq,
            child_session_id="orphan",
        )
    with pytest.raises(ValueError, match="不存在"):
        Session.resume(store, "orphan")
    assert await meta.get("orphan") is None


def test_find_fork_boundaries_lists_user_message_seqs(tmp_path) -> None:
    store = _store(tmp_path)
    parent = _build_parent(store)
    boundaries = find_fork_boundaries(parent.events)
    assert boundaries == [parent.events[1].seq, parent.events[-1].seq]


async def test_fork_from_failed_run_prefix(tmp_path) -> None:
    """run/failed 同样是终态：失败轮之后的消息也是合法边界。"""
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    s = Session.start(store, session_id="failedrun")
    s.append(USER_MESSAGE, {"content": "第一条"})
    s.append(RUN_STARTED, {})
    s.append(RUN_FAILED, {"reason": "boom"})
    s.append(USER_MESSAGE, {"content": "第二条"})

    child = await fork_session(
        store, meta, "failedrun",
        boundary_user_message_seq=s.events[-1].seq,
        child_session_id="after-fail",
    )
    assert [e.type for e in child.events].count(RUN_FAILED) == 1
    assert [e.type for e in child.events][-1] == SESSION_FORKED
