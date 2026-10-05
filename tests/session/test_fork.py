"""fork 核心域（Phase 14 T2, #108, ADR-0017 决策 1/2/3/8）。

file-per-lineage：fork = 新 session 文件 + seed 前缀逐字复制（重编 seq、
保留原 event_id）+ session/forked provenance 事件 + meta 索引行。
父文件零改动（spec §7）。boundary = run 完整边界，UX 按用户消息表达。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session import Session
from agent_harness.session.errors import SessionNotFound
from agent_harness.session.event import (
    FORK_IN_PROGRESS,
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_PAUSED,
    RUN_RESUMED,
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
from tests.scripted_model import ScriptedModel
from tests.session.store_fixtures import FailingFromStore


def _store(tmp_path) -> JsonlSessionStore:
    return JsonlSessionStore(tmp_path / "sessions")


def _pause_data() -> dict:
    """`run/paused.data` 的最小合法形状（`03 §3.4`；字段名取自 `build_pause_data`）。"""
    return {
        "reason": "budget_exhausted",
        "trigger_dimension": "run.max_agent_turns_total",
        "budget_version": 1,
        "consumed": {"agent_turns": 1},
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": 2},
        },
        "continuation": {
            "completed": [], "remaining": ["还有活要干"], "blockers": [],
            "next_safe_action": "抬高 ceiling 后同 run 恢复",
        },
        "closeout_source": "deterministic",
        "resume_requirements": [],
        "trace_id": None,
    }


def _resume_data() -> dict:
    """`run/resumed.data` 的最小合法形状（同 run 续跑：版本 +1、绝对 ceiling）。"""
    return {
        "from_pause_seq": 3,
        "previous_budget_version": 1,
        "budget_version": 2,
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": 8},
        },
        "consumed": {"agent_turns": 1},
        "resume_basis": "budget_increase",
    }


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


@pytest.mark.asyncio
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
    # child 自己的 started + 意图标记（#555，先于一切拷贝动作）+ seed（不含
    # 父的 started）+ forked 事件
    assert types == [
        SESSION_STARTED, FORK_IN_PROGRESS, USER_MESSAGE, RUN_STARTED,
        MODEL_COMPLETED, RUN_COMPLETED, SESSION_FORKED,
    ]
    intent = child.events[1]
    assert intent.data == {
        "parent_session_id": "parent",
        "boundary_user_message_seq": anchor,
    }
    # seed 逐字复制：event_id / time / data 保留，seq 重编为 child 局部单调
    parent_events = parent.events
    # seq 重编为 child 局部单调（0 起头，与 Session 既有约定一致）
    assert child.events[2].event_id == parent_events[1].event_id
    assert child.events[2].time == parent_events[1].time
    assert child.events[2].data == parent_events[1].data
    assert child.events[2].session_id == "child"
    assert [e.seq for e in child.events] == [0, 1, 2, 3, 4, 5, 6]
    forked = child.events[-1]
    assert forked.data["parent_session_id"] == "parent"
    assert forked.data["boundary_user_message_seq"] == anchor
    assert forked.data["fork_point_seq"] == parent_events[4].seq


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    # seed 为空：child = 自己的 started + 意图标记（#555）+ forked；fork_point_seq 无
    types = [e.type for e in child.events]
    assert types == [SESSION_STARTED, FORK_IN_PROGRESS, SESSION_FORKED]
    assert child.events[-1].data["fork_point_seq"] is None


@pytest.mark.asyncio
async def test_fork_boundary_errors(tmp_path) -> None:
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)

    # 锚点不是用户消息（指向 run/completed，seq=4）
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    with pytest.raises(SessionNotFound, match="不存在"):
        Session.resume(store, "orphan")
    assert await meta.get("orphan") is None


def test_find_fork_boundaries_lists_user_message_seqs(tmp_path) -> None:
    store = _store(tmp_path)
    parent = _build_parent(store)
    boundaries = find_fork_boundaries(parent.events)
    assert boundaries == [parent.events[1].seq, parent.events[-1].seq]


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_fork_from_interrupted_run_prefix(tmp_path) -> None:
    """T8 #138：run/interrupted 也是 run 终态——被中断轮之后的消息仍是合法边界。

    回归：终态集合若漏掉 run/interrupted，该 run 永远算「开着」，
    之后的用户消息不再被列为锚点，seed 校验也会以「悬空 run」拒绝。
    """
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    s = Session.start(store, session_id="interrupted")
    s.append(USER_MESSAGE, {"content": "第一条"})
    run_id, _ = s.begin_run()
    s.append(RUN_INTERRUPTED, {"interrupted_seq": 3, "reason": "process_restart"},
             run_id=run_id)
    s.append(USER_MESSAGE, {"content": "第二条"})

    assert find_fork_boundaries(s.events) == [
        s.events[1].seq, s.events[-1].seq
    ]
    child = await fork_session(
        store, meta, "interrupted",
        boundary_user_message_seq=s.events[-1].seq,
        child_session_id="after-interrupt",
    )
    assert [e.type for e in child.events].count(RUN_INTERRUPTED) == 1
    assert [e.type for e in child.events][-1] == SESSION_FORKED


@pytest.mark.asyncio
async def test_fork_from_paused_run_prefix(tmp_path) -> None:
    """`#312`：尾部 `run/paused` 的 run 也算「已收口」——它之后的消息是合法 fork 锚点。

    暂停的执行没有在途工具调用、也没有悬空 run（`03 §3.4`），所以 child 以它作前缀是
    完整的。回归：若把 pause 排除在收口集合之外，暂停过的会话里 fork 会突然不可用——
    而"暂停后想换个方向重跑"（fork 出 child 另起一条线）恰恰是这个状态下的常见动作。
    """
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    s = Session.start(store, session_id="paused")
    s.append(USER_MESSAGE, {"content": "第一条"})
    run_id, _ = s.begin_run()
    s.append(RUN_PAUSED, _pause_data(), run_id=run_id)
    s.append(USER_MESSAGE, {"content": "换个方向再来"})

    assert find_fork_boundaries(s.events) == [s.events[1].seq, s.events[-1].seq]
    child = await fork_session(
        store, meta, "paused",
        boundary_user_message_seq=s.events[-1].seq,
        child_session_id="after-pause",
    )

    assert [e.type for e in child.events].count(RUN_PAUSED) == 1
    assert [e.type for e in child.events][-1] == SESSION_FORKED


@pytest.mark.asyncio
async def test_fork_prefix_with_resumed_run_is_rejected(tmp_path) -> None:
    """`run/resumed` 把 run 重新计入未收口 ⇒ 悬空前缀仍被拒（暂停不是免检通道）。

    成对的负向面：如果"pause 计一次收口"被实现成"这个 run 从此永远算收口"，一个
    **正在恢复中**的 run 也会被当成完整前缀——child 就可能在一个执行未收口的截面上
    长出来（`ForkBoundaryError` 的存在意义正是禁止这个）。
    """
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    s = Session.start(store, session_id="resumed")
    s.append(USER_MESSAGE, {"content": "第一条"})
    run_id, _ = s.begin_run()
    s.append(RUN_PAUSED, _pause_data(), run_id=run_id)
    s.append(RUN_RESUMED, _resume_data(), run_id=run_id)
    s.append(USER_MESSAGE, {"content": "第二条"})

    # 恢复之后的那条用户消息不是锚点（此刻 run 又开着）
    assert find_fork_boundaries(s.events) == [s.events[1].seq]
    with pytest.raises(ForkBoundaryError, match="未终态|悬空|open"):
        await fork_session(
            store, meta, "resumed",
            boundary_user_message_seq=s.events[-1].seq,
            child_session_id="not-created",
        )


# ── T3 copy-on-fork（#109, ADR-0017 决策 5）─────────────────────────────────


@pytest.mark.asyncio
async def test_fork_copies_parent_workspace_to_child(tmp_path) -> None:
    """fork 点世界快照：父 workspace 全部文件复制给 child。"""
    from agent_harness.sandbox import WorkspaceRegistry

    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    registry = WorkspaceRegistry(root=tmp_path / "ws")
    parent = Session.start(
        store, session_id="wsparent", workspace_registry=registry
    )
    parent.sandbox.write_text("a.txt", "hello")
    parent.sandbox.write_text("sub/nested.txt", "nested")
    anchor = parent.events[-1].seq
    parent.append(USER_MESSAGE, {"content": "第二条"})
    anchor = parent.events[-1].seq

    child = await fork_session(
        store, meta, "wsparent", boundary_user_message_seq=anchor,
        child_session_id="wschild", workspace_registry=registry,
    )
    assert child.sandbox is not None
    assert child.sandbox.read_text("a.txt") == "hello"
    assert child.sandbox.read_text("sub/nested.txt") == "nested"


@pytest.mark.asyncio
async def test_fork_workspace_isolation_bidirectional(tmp_path) -> None:
    """双向隔离：child 写不伤父；fork 后父写不进 child。"""
    from agent_harness.sandbox import WorkspaceRegistry

    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    registry = WorkspaceRegistry(root=tmp_path / "ws")
    parent = Session.start(
        store, session_id="isoparent", workspace_registry=registry
    )
    parent.sandbox.write_text("a.txt", "v1")
    parent.append(USER_MESSAGE, {"content": "第二条"})
    anchor = parent.events[-1].seq

    child = await fork_session(
        store, meta, "isoparent", boundary_user_message_seq=anchor,
        child_session_id="isochild", workspace_registry=registry,
    )
    child.sandbox.write_text("child-only.txt", "x")
    assert "child-only.txt" not in parent.sandbox.list_files("*")
    parent.sandbox.write_text("parent-late.txt", "y")
    assert "parent-late.txt" not in child.sandbox.list_files("*")
    # 父的原文件仍是 v1（child 改它不影响父）
    child.sandbox.write_text("a.txt", "child-version")
    assert parent.sandbox.read_text("a.txt") == "v1"


@pytest.mark.asyncio
async def test_fork_without_parent_workspace_degrades(tmp_path) -> None:
    """父从未绑定 workspace：child 得到空 workspace，不崩溃。"""
    from agent_harness.sandbox import WorkspaceRegistry

    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    registry = WorkspaceRegistry(root=tmp_path / "ws")
    parent = _build_parent(store)  # 未传 registry
    anchor = parent.events[-1].seq

    child = await fork_session(
        store, meta, "parent", boundary_user_message_seq=anchor,
        child_session_id="freshws", workspace_registry=registry,
    )
    assert child.sandbox is not None
    assert child.sandbox.list_files("*") == []


# ── T4 tail summary（#110, ADR-0017 决策 9）─────────────────────────────────

from langchain_core.messages import AIMessage


class _FakeSummarizer:
    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls: list[str] = []

    async def summarize(self, text: str) -> str:
        self.calls.append(text)
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _parent_with_tail(store: JsonlSessionStore) -> Session:
    """user1 → 完整 run → user2(锚点) → 被放弃的 run（tail）。"""
    s = Session.start(store, session_id="withtail")
    s.append(USER_MESSAGE, {"content": "第一条"})
    s.append(RUN_STARTED, {})
    s.append(MODEL_COMPLETED, {"content": "好的"})
    s.append(RUN_COMPLETED, {})
    s.append(USER_MESSAGE, {"content": "第二条"})
    s.append(RUN_STARTED, {})
    s.append(MODEL_COMPLETED, {"content": "我打算用方案A实现"})
    s.append(RUN_COMPLETED, {})
    return s


@pytest.mark.asyncio
async def test_tail_summary_attached_when_summarizer_given(tmp_path) -> None:
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _parent_with_tail(store)
    anchor = parent.events[5].seq  # 第二条用户消息（seq 0 起）
    fake = _FakeSummarizer("被放弃的线尝试了方案A")

    child = await fork_session(
        store, meta, "withtail", boundary_user_message_seq=anchor,
        child_session_id="sumchild", summarizer=fake,
    )
    forked = child.events[-1]
    assert forked.type == SESSION_FORKED
    assert forked.data["tail_summary"] == "被放弃的线尝试了方案A"
    # 恰好一次调用，文本含被放弃路线的内容
    assert len(fake.calls) == 1
    assert "方案A" in fake.calls[0]


@pytest.mark.asyncio
async def test_tail_summary_can_be_disabled(tmp_path) -> None:
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _parent_with_tail(store)
    fake = _FakeSummarizer("不该被调用")

    child = await fork_session(
        store, meta, "withtail",
        boundary_user_message_seq=parent.events[5].seq,
        summarizer=fake, with_tail_summary=False,
    )
    assert fake.calls == []
    assert "tail_summary" not in child.events[-1].data


@pytest.mark.asyncio
async def test_tail_summary_degrades_on_failure(tmp_path) -> None:
    """摘要失败 = 降级不挂接：fork 照常完成，无字段，meta 照写。"""
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _parent_with_tail(store)
    fake = _FakeSummarizer(RuntimeError("模型故障"))

    child = await fork_session(
        store, meta, "withtail",
        boundary_user_message_seq=parent.events[5].seq,
        child_session_id="degrade", summarizer=fake,
    )
    assert "tail_summary" not in child.events[-1].data
    assert child.events[-1].type == SESSION_FORKED
    assert await meta.get("degrade") is not None
    Session.resume(store, "degrade")  # child 完整可用


@pytest.mark.asyncio
async def test_tail_summary_skipped_when_tail_empty(tmp_path) -> None:
    """锚点是最后一条事件：无 tail，不调用摘要器。"""
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)  # 锚点（user2）后无事件
    fake = _FakeSummarizer("不该被调用")

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=parent.events[-1].seq,
        summarizer=fake,
    )
    assert fake.calls == []
    assert "tail_summary" not in child.events[-1].data


@pytest.mark.asyncio
async def test_tail_summarizer_uses_scripted_model(tmp_path) -> None:
    """真实 TailSummarizer 类：任何 ainvoke 模型可用（ScriptedModel 实测）。"""
    from agent_harness.session.fork import TailSummarizer

    model = ScriptedModel([AIMessage(content="这是摘要")])
    summarizer = TailSummarizer(model)
    out = await summarizer.summarize("[user] 试试方案A\n[assistant] 失败了")
    assert out == "这是摘要"
    # 提示词带进了 tail 文本与摘要指令
    prompt = model.snapshots[0].messages[0].content
    assert "方案A" in prompt


# ── F15 #234：权限决策是会话属性，fork 必须显式继承 ─────────────────


@pytest.mark.asyncio
async def test_fork_inherits_parent_permission_decisions(tmp_path) -> None:
    """父会话显式声明的权限决策（档位 + auto_approve）必须进 child 的 session/started。

    不继承的后果不是"少个字段"：child 续聊会落到"未声明"分支 = workspace-write +
    全自动批准——它复制了父的 workspace 文件，却对写操作免审批（安全边界反向放宽）。
    """
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = Session.start(
        store,
        session_id="parent",
        started_data={"permission_mode": "read-only", "auto_approve": False},
    )
    parent.append(USER_MESSAGE, {"content": "第一条"})
    parent.append(RUN_STARTED, {})
    parent.append(MODEL_COMPLETED, {"content": "好的"})
    parent.append(RUN_COMPLETED, {})
    anchor = parent.append(USER_MESSAGE, {"content": "第二条"}).seq

    child = await fork_session(
        store, meta, "parent", boundary_user_message_seq=anchor,
        child_session_id="child",
    )

    started = child.events[0]
    assert started.type == SESSION_STARTED
    assert started.data["permission_mode"] == "read-only"
    assert started.data["auto_approve"] is False
    # 父的 started 不进 seed（原语义不变）
    assert all(e.type != SESSION_STARTED for e in child.events[1:])


@pytest.mark.asyncio
async def test_fork_of_undeclared_parent_writes_no_permission_keys(tmp_path) -> None:
    """父未声明权限决策 → child 也不写键（历史会话 fork 出的子树语义一致）。"""
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=parent.events[-1].seq,
        child_session_id="child",
    )

    started = child.events[0]
    assert "permission_mode" not in started.data
    assert "auto_approve" not in started.data


@pytest.mark.asyncio
async def test_fork_mid_write_failure_leaves_partial_child_log(tmp_path) -> None:
    """seed 写盘中途失败：父日志逐字节不变，child 只留下已落盘的前缀。

    这是**当前语义**（`#251` 冻结，父票 #241 的冻结决策②）：不在本阶段实现
    "临时文件 + 原子替换"，失败清理的责任在 fork 主流程。本用例把两侧的事实都
    钉住——将来若改成原子替换，这条必须红着改。
    """
    root = tmp_path / "sessions"
    base = JsonlSessionStore(root=root)
    parent = _build_parent(base)
    parent_path = base._events_path("parent")
    parent_bytes_before = parent_path.read_bytes()

    # 第 3 次写 = child 的 seed 首项（1=session/started, 2=fork/in-progress,
    # 3=seed[0]——#555 起意图标记先于拷贝/移植落盘）
    failing = FailingFromStore(root, fail_from=3)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()

    with pytest.raises(RuntimeError, match="磁盘故障"):
        await fork_session(
            failing, meta, "parent",
            boundary_user_message_seq=parent.events[-1].seq,
            child_session_id="partial_child",
        )

    # ① 父日志逐字节不变（§7 父不可改）
    assert parent_path.read_bytes() == parent_bytes_before
    # ② child 只留下已落盘的前缀（started + 意图标记；无 session/forked、
    # 无 meta 行——失败清理在 fork 主流程的补偿里做，见 #555 测试）
    durable = base.read_events("partial_child")
    assert [e.type for e in durable] == [SESSION_STARTED, FORK_IN_PROGRESS]
    assert await meta.get("partial_child") is None


@pytest.mark.asyncio
async def test_fork_does_not_touch_parent_log_bytes(tmp_path) -> None:
    """成功 fork 同样是只读父：父日志逐字节不变（不含 seq / mtime 之外的任何痕迹）。"""
    store = _store(tmp_path)
    parent = _build_parent(store)
    parent_path = store._events_path("parent")
    before = parent_path.read_bytes()

    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=parent.events[1].seq,
        child_session_id="child_ok",
    )

    assert parent_path.read_bytes() == before


# ── #555：分阶段可见性（意图标记 / 暂存发布 / 失败补偿） ──────────────────


def _build_parent_with_workspace(store, registry) -> Session:
    """两轮对话 + 带资产的父会话（workspace 写一个文件）。"""
    parent = Session.start(store, session_id="parent", workspace_registry=registry)
    parent.append(USER_MESSAGE, {"content": "第一条"})
    parent.append(RUN_STARTED, {})
    parent.append(MODEL_COMPLETED, {"content": "好的"})
    parent.append(RUN_COMPLETED, {})
    parent.append(USER_MESSAGE, {"content": "第二条"})
    (registry.default_workspace_root("parent") / "keep.txt").write_text(
        "父资产", encoding="utf-8"
    )
    return parent


@pytest.mark.asyncio
async def test_fork_publishes_workspace_via_staging(tmp_path) -> None:
    """默认形态工作区走暂存 + 同卷 rename 发布：成功后副本完整、暂存无残留。

    #555：观察者要么看到空 child workspace、要么看到完整副本——半份拷贝
    永远不出现在 child 的登记路径上。
    """
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    registry = WorkspaceRegistry(root=tmp_path / "sandbox", backend="local")
    parent = _build_parent_with_workspace(store, registry)

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=parent.events[-1].seq,
        child_session_id="child", workspace_registry=registry,
    )

    # 意图标记先于一切拷贝动作落盘，data 可溯源
    intent = child.events[1]
    assert intent.type == FORK_IN_PROGRESS
    assert intent.data == {
        "parent_session_id": "parent",
        "boundary_user_message_seq": parent.events[-1].seq,
    }
    # 发布结果：child workspace 是父的完整副本
    child_root = registry.default_workspace_root(child.session_id)
    assert (child_root / "keep.txt").read_text(encoding="utf-8") == "父资产"
    # 暂存无残留（整个暂存目录被 rename 搬走）
    staging = registry.fork_staging_root()
    assert not staging.exists() or not any(staging.iterdir())


@pytest.mark.asyncio
async def test_fork_grandchild_seed_excludes_intent_marker(tmp_path) -> None:
    """意图标记是会话级状态：孙代 seed 不携带（孙的 fork 流程写自己的标记）。"""
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    registry = WorkspaceRegistry(root=tmp_path / "sandbox", backend="local")
    parent = _build_parent_with_workspace(store, registry)
    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=parent.events[-1].seq,
        child_session_id="child", workspace_registry=registry,
    )

    grandchild = await fork_session(
        store, meta, "child",
        boundary_user_message_seq=child.events[2].seq,  # child 的首条用户消息
        child_session_id="grandchild", workspace_registry=registry,
    )

    types = [e.type for e in grandchild.events]
    assert types.count(FORK_IN_PROGRESS) == 1  # 只有它自己写的那条
    assert types[1] == FORK_IN_PROGRESS
    # 各代自证：孙代标记指向 child，不指向 parent
    assert grandchild.events[1].data["parent_session_id"] == "child"


@pytest.mark.asyncio
async def test_fork_copy_failure_compensates_and_leaves_marked_child(
    tmp_path, monkeypatch
) -> None:
    """workspace 拷贝中途失败：原错误上抛，child 有标记、无 forked、无残留。

    修复前（audit CHAOS-01）：child 是无标记僵尸——半份拷贝直接留在 child
    workspace、启动扫描无从判定、HTTP 500 之后现场静默存在。
    """
    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    registry = WorkspaceRegistry(root=tmp_path / "sandbox", backend="local")
    parent = _build_parent_with_workspace(store, registry)
    parent_bytes = store._events_path("parent").read_bytes()

    def _half_copy_then_explode(src, dst, **kwargs):
        # 模拟"拷到一半磁盘故障"：目标目录里有半份内容后抛错
        dst_path = Path(dst)
        dst_path.mkdir(parents=True, exist_ok=True)
        (dst_path / "half.txt").write_text("半份", encoding="utf-8")
        raise RuntimeError("copy boom")

    monkeypatch.setattr(shutil, "copytree", _half_copy_then_explode)

    with pytest.raises(RuntimeError, match="copy boom"):
        await fork_session(
            store, meta, "parent",
            boundary_user_message_seq=parent.events[-1].seq,
            child_session_id="child", workspace_registry=registry,
        )

    # child：意图标记在场、无 session/forked——「fork 未完成」可判定
    durable = store.read_events("child")
    assert [e.type for e in durable] == [SESSION_STARTED, FORK_IN_PROGRESS]
    assert durable[1].data["parent_session_id"] == "parent"
    # 残留回收：暂存与默认形态子工作区都被清掉
    staging = registry.fork_staging_root()
    assert not staging.exists() or not any(staging.iterdir())
    assert not registry.default_workspace_root("child").exists()
    # 映射保留（可见性事实）；meta 行没有（fork 未完成）
    assert registry.exists("child")
    assert await meta.get("child") is None
    # 父侧逐字节不变、资产完好
    assert store._events_path("parent").read_bytes() == parent_bytes
    assert (
        registry.default_workspace_root("parent") / "keep.txt"
    ).read_text(encoding="utf-8") == "父资产"


@pytest.mark.asyncio
async def test_fork_does_not_inherit_approval_grants_or_workflow_mode(tmp_path) -> None:
    """#526：fork 不继承会话级审批授权与工作流档。

    父会话有一条 approval grant + 一次 plan 档切换；fork 后 child 的 seed 里
    不得含有 permission/approval-granted|revoked 与 workflow/mode-changed，
    derive_approval_grants 为空、effective_workflow_mode 回落 NORMAL。
    （#358 W-14 / F26：高风险权限不因 Fork 静默扩大。）
    """
    import time

    from agent_harness.session.approval import (
        append_approval_grant,
        derive_approval_grants,
    )
    from agent_harness.session.event import (
        PERMISSION_GRANTED,
        PERMISSION_REVOKED,
        WORKFLOW_MODE_CHANGED,
    )
    from agent_harness.session.workflow import (
        WorkflowMode,
        append_workflow_mode_change,
        effective_workflow_mode,
    )
    from agent_harness.tooling.approval import (
        ApprovalGrant,
        ApprovalIdentity,
    )
    from agent_harness.tooling.contract import PermissionPolicy, ToolPermission

    store = _store(tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    parent = _build_parent(store)

    grant = ApprovalGrant(
        identity=ApprovalIdentity(
            tool_name="bash",
            kind="COMMAND",
            canonical="echo hi",
            args_hash="abc",
            permission=ToolPermission.DANGER,
            policy_at_approval=PermissionPolicy.READ_ONLY,
        ),
        expires_at=time.time() + 600,
    )
    append_approval_grant(parent, grant)
    append_workflow_mode_change(parent, WorkflowMode.PLAN)
    anchor = parent.events[-2].seq  # 第二条用户消息（grant/plan 事件在其后也无妨）
    # 锚点取"第二条"用户消息：grant 与 mode 事件在锚点之前，确保进 seed 候选
    anchor = next(
        e.seq for e in parent.events
        if e.type == USER_MESSAGE and e.data.get("content") == "第二条"
    )

    child = await fork_session(
        store, meta, "parent", boundary_user_message_seq=anchor,
        child_session_id="child",
    )

    child_types = [e.type for e in child.events]
    assert PERMISSION_GRANTED not in child_types
    assert PERMISSION_REVOKED not in child_types
    assert WORKFLOW_MODE_CHANGED not in child_types
    assert derive_approval_grants(child.events) == {}
    assert effective_workflow_mode(child.events) is WorkflowMode.NORMAL
    # 父不受影响
    assert derive_approval_grants(parent.events) != {}
    assert effective_workflow_mode(parent.events) is WorkflowMode.PLAN
