"""F18-A (#282)：同 session 内改权限档 + 派生「最后一次胜」（域 / service 层）。

本文件按 AC 逐条钉住行为（判据本身见 `docs/adr/0041-session-permission-mode-mutability.md`）：
AC2/AC3 派生优先级、AC5 两键同事件、AC6 resume 派生与 fork 继承、AC8 三概念分离、
AC4 的 422/404/409，外加 seq 冲突重试有界（BUG-011 同款）。

端点层（200 / 422 / 404 / 409）见 `tests/web/test_web_session_permission.py`。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_harness.config import Settings
from agent_harness.session.approval import (
    SESSION_AUTO_APPROVE_KEY,
    SESSION_PERMISSION_MODE_KEY,
    PermissionChange,
    append_permission_change,
    effective_auto_approve,
    effective_permission_mode,
)
from agent_harness.session.event import (
    PERMISSION_CHANGED,
    RUN_COMPLETED,
    RUN_STARTED,
    SESSION_FORKED,
    SESSION_STARTED,
    TOOL_APPROVAL_REQUESTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.session.service import (
    InvalidDecision,
    PendingApprovalConflict,
    SessionNotFound,
)
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage.sqlite import SqliteSessionMetaStore
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.web.app import session_service
from tests.session.ledger_doubles import idle_operation_ledger

_READ_ONLY = PermissionPolicy.READ_ONLY
_WRITE = PermissionPolicy.WORKSPACE_WRITE
_DANGER = PermissionPolicy.DANGER_FULL_ACCESS


# ── 测试基建（与 test_model_change.py 同构）─────────────────────────────


def _state(tmp_path) -> MagicMock:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_provider="deepseek",
        model_name="deepseek-chat",
    )
    state = MagicMock()
    state.settings = settings
    state.store = JsonlSessionStore(root=tmp_path / "sessions")
    state.workspaces_root = tmp_path
    state.workspace_registry = None
    state.workspace_index = None
    state.run_manager = MagicMock()
    state.run_manager.get_active = MagicMock(return_value=None)
    state.run_manager.launch = MagicMock(return_value=(MagicMock(), MagicMock()))
    state.get_wiring = AsyncMock(return_value=(MagicMock(), MagicMock()))
    state.operation_ledger = idle_operation_ledger()
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    # 审批队列字典必须是真 dict：pending 闸门会 .get(session_id)。
    state.approval_queues = {}
    return state


def _seed_session(state, session_id: str = "sid", *, started_data=None) -> Session:
    return Session.start(
        state.store, session_id=session_id, started_data=started_data or None
    )


def _event(event_type: str, data: dict) -> MagicMock:
    event = MagicMock()
    event.type = event_type
    event.data = data
    return event


def _resume_capture(state, session_id: str = "sid") -> dict:
    """驱动 resume_and_launch，返回 build_runtime 收到的关键字参数。"""
    with (
        patch(
            "agent_harness.session.service.build_runtime",
            new_callable=AsyncMock,
        ) as mock_build,
        patch("agent_harness.session.service.Session") as mock_session_cls,
    ):
        mock_session = MagicMock()
        mock_session.session_id = session_id
        mock_session_cls.resume = MagicMock(return_value=mock_session)
        asyncio.run(
            session_service(state).resume_and_launch(
                session_id=session_id, task="hi", amend=None
            )
        )
    return mock_build.await_args.kwargs


def _magic_store(events: list[SessionEvent]):
    store = MagicMock()
    store.read_events = MagicMock(return_value=events)
    return store


# ── 1 / 2：派生优先级（纯函数）──────────────────────────────────────────


class TestEffectivePermissionMode:
    def test_all_empty_is_none(self):
        assert effective_permission_mode([]) is None

    def test_declared_is_used_when_no_change(self):
        events = [_event(SESSION_STARTED, {SESSION_PERMISSION_MODE_KEY: "read-only"})]
        assert effective_permission_mode(events) is _READ_ONLY

    def test_change_wins_over_declared(self):
        events = [
            _event(SESSION_STARTED, {SESSION_PERMISSION_MODE_KEY: "read-only"}),
            _event(PERMISSION_CHANGED, {SESSION_PERMISSION_MODE_KEY: "danger-full-access"}),
        ]
        assert effective_permission_mode(events) is _DANGER

    def test_last_change_wins(self):
        events = [
            _event(SESSION_STARTED, {SESSION_PERMISSION_MODE_KEY: "read-only"}),
            _event(PERMISSION_CHANGED, {SESSION_PERMISSION_MODE_KEY: "workspace-write"}),
            _event(PERMISSION_CHANGED, {SESSION_PERMISSION_MODE_KEY: "danger-full-access"}),
        ]
        assert effective_permission_mode(events) is _DANGER

    def test_started_without_key_falls_through_to_change(self):
        """未声明档位的会话改档后，派生取 changed（不是回落到 None）。"""
        events = [
            _event(SESSION_STARTED, {}),
            _event(PERMISSION_CHANGED, {SESSION_PERMISSION_MODE_KEY: "read-only"}),
        ]
        assert effective_permission_mode(events) is _READ_ONLY

    def test_garbage_value_in_change_is_treated_as_undeclared(self):
        """日志被手改写出非法档 → 按未声明处理（**不回落**到 declared，也不猜更松的档）。"""
        events = [
            _event(SESSION_STARTED, {SESSION_PERMISSION_MODE_KEY: "read-only"}),
            _event(PERMISSION_CHANGED, {SESSION_PERMISSION_MODE_KEY: "delete-everything"}),
        ]
        assert effective_permission_mode(events) is None

    def test_change_wins_even_if_older_than_declared_seq(self):
        """判据是"最后一条 changed"，与 seq 顺序无关地只看事件流顺序（append-only）。"""
        events = [
            _event(PERMISSION_CHANGED, {SESSION_PERMISSION_MODE_KEY: "danger-full-access"}),
            _event(SESSION_STARTED, {SESSION_PERMISSION_MODE_KEY: "read-only"}),
        ]
        assert effective_permission_mode(events) is _DANGER


class TestEffectiveAutoApprove:
    def test_all_empty_is_none(self):
        assert effective_auto_approve([]) is None

    def test_declared_is_used_when_no_change(self):
        events = [_event(SESSION_STARTED, {SESSION_AUTO_APPROVE_KEY: False})]
        assert effective_auto_approve(events) is False

    def test_change_wins_over_declared(self):
        events = [
            _event(SESSION_STARTED, {SESSION_AUTO_APPROVE_KEY: False}),
            _event(PERMISSION_CHANGED, {SESSION_AUTO_APPROVE_KEY: True}),
        ]
        assert effective_auto_approve(events) is True

    def test_last_change_wins(self):
        events = [
            _event(PERMISSION_CHANGED, {SESSION_AUTO_APPROVE_KEY: True}),
            _event(PERMISSION_CHANGED, {SESSION_AUTO_APPROVE_KEY: False}),
        ]
        assert effective_auto_approve(events) is False

    def test_non_bool_in_change_is_treated_as_undeclared(self):
        """`0` / `"false"` 这类值不能当 False 用（会凭空造出 deny 路由）——按未声明处理。"""
        for raw in (0, 1, "false", "true", {}, None):
            events = [_event(PERMISSION_CHANGED, {SESSION_AUTO_APPROVE_KEY: raw})]
            assert effective_auto_approve(events) is None


# ── 3：唯一写入口 ──────────────────────────────────────────────────────


class TestAppendPermissionChange:
    def test_writes_both_keys_with_current_values(self, tmp_path):
        state = _state(tmp_path)
        session = _seed_session(state, started_data={SESSION_PERMISSION_MODE_KEY: "read-only"})

        append_permission_change(
            session, PermissionChange(permission_mode=_DANGER, auto_approve=False)
        )

        event = state.store.read_events("sid")[-1]
        assert event.type == PERMISSION_CHANGED
        assert event.data == {
            SESSION_PERMISSION_MODE_KEY: "danger-full-access",
            SESSION_AUTO_APPROVE_KEY: False,
        }

    def test_event_carries_no_policy_key(self, tmp_path):
        """AC8 守卫：事件只带 permission_mode / auto_approve——**不含** `policy`。

        `permission_policy` 是另一件事（`tool/approval-requested.data.policy` 折叠，前端
        projection.ts:782，**#236 已更正**）。把两者合并正是本票要防止的复辟。
        """
        state = _state(tmp_path)
        session = _seed_session(state)

        append_permission_change(
            session, PermissionChange(permission_mode=_WRITE, auto_approve=True)
        )

        assert set(state.store.read_events("sid")[-1].data) == {
            SESSION_PERMISSION_MODE_KEY,
            SESSION_AUTO_APPROVE_KEY,
        }

    def test_change_writes_no_policy_key_and_leaves_approval_policy_alone(self, tmp_path):
        """AC8 双向守卫：改档既不写 `policy`，也不动审批事件里已有的 `policy`。

        `permission_policy` 是**另一件事**（`tool/approval-requested.data.policy` 逐事件折叠，
        前端 projection.ts:782；#236 已更正）——两概念合并正是本票要防的复辟。
        """
        state = _state(tmp_path)
        session = _seed_session(state)
        session.append(TOOL_APPROVAL_REQUESTED, {"approval_id": "a1", "policy": "workspace-write"})

        append_permission_change(
            session, PermissionChange(permission_mode=_READ_ONLY, auto_approve=False)
        )

        events = state.store.read_events("sid")
        assert events[-1].type == PERMISSION_CHANGED
        assert "policy" not in events[-1].data
        approvals = [e for e in events if e.type == TOOL_APPROVAL_REQUESTED]
        assert [e.data["policy"] for e in approvals] == ["workspace-write"]

    def test_does_not_resume_or_inject_synthetic_events(self, tmp_path):
        """只 append（不变量 #7）：dangling tool_call 不被补合成 tool/result，也无 resumed。"""
        state = _state(tmp_path)
        session = _seed_session(state)
        session.append(TOOL_CALL, {"tool_call_id": "tc-1", "tool_name": "bash"})
        before = [e.type for e in state.store.read_events("sid")]

        append_permission_change(
            session, PermissionChange(permission_mode=_WRITE, auto_approve=True)
        )

        events = state.store.read_events("sid")
        assert [e.type for e in events][: len(before)] == before
        assert events[-1].type == PERMISSION_CHANGED
        assert all(e.type != TOOL_RESULT for e in events)
        assert all(e.type != "session/resumed" for e in events)


# ── 4：change_permission_mode ─────────────────────────────────────────


class TestChangePermissionMode:
    def test_appends_and_returns_effective_values(self, tmp_path):
        state = _state(tmp_path)
        _seed_session(state, started_data={SESSION_PERMISSION_MODE_KEY: "read-only"})

        change = asyncio.run(
            session_service(state).change_permission_mode(
                session_id="sid",
                permission_mode="danger-full-access",
                auto_approve=True,
            )
        )

        assert change == PermissionChange(permission_mode=_DANGER, auto_approve=True)
        events = state.store.read_events("sid")
        assert events[-1].type == PERMISSION_CHANGED
        assert effective_permission_mode(events) is _DANGER
        assert effective_auto_approve(events) is True

    def test_mode_and_auto_approve_change_together(self, tmp_path):
        """冻结决策 2：一个事件同时表达两件事、同一生效点。"""
        state = _state(tmp_path)
        _seed_session(
            state,
            started_data={
                SESSION_PERMISSION_MODE_KEY: "read-only",
                SESSION_AUTO_APPROVE_KEY: True,
            },
        )

        asyncio.run(
            session_service(state).change_permission_mode(
                session_id="sid", permission_mode="workspace-write", auto_approve=False
            )
        )

        events = state.store.read_events("sid")
        assert len([e for e in events if e.type == PERMISSION_CHANGED]) == 1
        assert effective_permission_mode(events) is _WRITE
        assert effective_auto_approve(events) is False

    def test_downgrade_is_allowed(self, tmp_path):
        """双向可改：降档不需要任何额外闸门（后端不加确认标志位）。"""
        state = _state(tmp_path)
        _seed_session(state, started_data={SESSION_PERMISSION_MODE_KEY: "danger-full-access"})

        change = asyncio.run(
            session_service(state).change_permission_mode(
                session_id="sid", permission_mode="read-only", auto_approve=False
            )
        )

        assert change.permission_mode is _READ_ONLY

    def test_unknown_mode_raises_invalid_decision(self, tmp_path):
        state = _state(tmp_path)
        _seed_session(state)

        with pytest.raises(InvalidDecision):
            asyncio.run(
                session_service(state).change_permission_mode(
                    session_id="sid",
                    permission_mode="delete-everything",
                    auto_approve=True,
                )
            )
        # 拒绝时不留半个事件
        assert all(
            e.type != PERMISSION_CHANGED for e in state.store.read_events("sid")
        )

    def test_missing_session_raises(self, tmp_path):
        state = _state(tmp_path)

        with pytest.raises(SessionNotFound):
            asyncio.run(
                session_service(state).change_permission_mode(
                    session_id="nope", permission_mode="read-only", auto_approve=True
                )
            )

    def test_invalid_session_id_raises(self, tmp_path):
        from agent_harness.session.service import InvalidSessionId

        state = _state(tmp_path)
        with pytest.raises(InvalidSessionId):
            asyncio.run(
                session_service(state).change_permission_mode(
                    session_id="../etc", permission_mode="read-only", auto_approve=True
                )
            )

    def test_pending_approval_conflicts(self, tmp_path):
        """冻结决策 5：有未裁决审批时拒绝改档（409），且**不落事件**。"""
        state = _state(tmp_path)
        _seed_session(state)
        queue = MagicMock()
        queue.pending_ids = MagicMock(return_value=["approval-1"])
        state.approval_queues["sid"] = queue

        with pytest.raises(PendingApprovalConflict):
            asyncio.run(
                session_service(state).change_permission_mode(
                    session_id="sid", permission_mode="read-only", auto_approve=True
                )
            )
        assert all(
            e.type != PERMISSION_CHANGED for e in state.store.read_events("sid")
        )

    def test_empty_queue_allows_change(self, tmp_path):
        """队列在场但无待裁决 → 允许（判据是 pending_ids，不是队列是否存在）。"""
        state = _state(tmp_path)
        _seed_session(state)
        queue = MagicMock()
        queue.pending_ids = MagicMock(return_value=[])
        state.approval_queues["sid"] = queue

        change = asyncio.run(
            session_service(state).change_permission_mode(
                session_id="sid", permission_mode="read-only", auto_approve=True
            )
        )
        assert change.permission_mode is _READ_ONLY

    def test_no_approval_gate_key_is_ignored(self, tmp_path):
        """会话不在审批队列表里 → 无 pending，改档照常（不能把"没有表项"当冲突）。"""
        state = _state(tmp_path)
        _seed_session(state)
        assert state.approval_queues == {}

        asyncio.run(
            session_service(state).change_permission_mode(
                session_id="sid", permission_mode="read-only", auto_approve=True
            )
        )
        assert state.store.read_events("sid")[-1].type == PERMISSION_CHANGED

    def test_mid_run_reuses_active_session_seq(self, tmp_path):
        """在途 run 持有 Session 时旁路追加走同一聚合——否则 seq 撞号，会话不可 resume。"""
        from types import SimpleNamespace

        state = _state(tmp_path)
        live = _seed_session(state)
        live.append(USER_MESSAGE, {"content": "hello"})  # seq 1
        state.run_manager.get_active = MagicMock(
            return_value=SimpleNamespace(session=live)
        )

        asyncio.run(
            session_service(state).change_permission_mode(
                session_id="sid", permission_mode="read-only", auto_approve=True
            )
        )
        live.append(USER_MESSAGE, {"content": "after"})

        seqs = [e.seq for e in state.store.read_events("sid")]
        assert seqs == [0, 1, 2, 3]
        assert Session.resume(state.store, "sid") is not None

    def test_mid_run_reaches_run_listener(self, tmp_path):
        from types import SimpleNamespace

        state = _state(tmp_path)
        live = _seed_session(state)
        seen: list = []
        live.add_listener(seen.append)
        state.run_manager.get_active = MagicMock(
            return_value=SimpleNamespace(session=live)
        )

        asyncio.run(
            session_service(state).change_permission_mode(
                session_id="sid", permission_mode="read-only", auto_approve=True
            )
        )

        assert seen[-1].type == PERMISSION_CHANGED


class TestConcurrentPermissionChange:
    """与 BUG-011 同款：两个并发改档不得把会话写死（同一 seq 重试循环）。"""

    def test_parallel_changes_keep_seq_strictly_increasing(self, tmp_path, monkeypatch):
        state = _state(tmp_path)
        _seed_session(state)
        real_read = state.store.read_events
        frozen = real_read("sid")
        reads: list[int] = []

        def read_with_frozen_first_snapshot(session_id: str):
            if len(reads) < 2:  # 两个并发写者各读到一次「变更前」快照
                reads.append(1)
                return list(frozen)
            return real_read(session_id)

        monkeypatch.setattr(state.store, "read_events", read_with_frozen_first_snapshot)

        async def run_both():
            return await asyncio.gather(
                session_service(state).change_permission_mode(
                    session_id="sid", permission_mode="read-only", auto_approve=True
                ),
                session_service(state).change_permission_mode(
                    session_id="sid", permission_mode="danger-full-access", auto_approve=False
                ),
            )

        changes = asyncio.run(run_both())

        events = real_read("sid")
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs)), f"seq 重复：{seqs}"
        changed = [e for e in events if e.type == PERMISSION_CHANGED]
        assert len(changed) == 2  # 两个改档都生效，不是「第二个被静默丢掉」
        assert {c.permission_mode for c in changes} == {_READ_ONLY, _DANGER}
        assert Session.resume(state.store, "sid") is not None

    def test_retry_is_bounded_and_surfaces_persistent_conflict(self, tmp_path, monkeypatch):
        from agent_harness.session.service import SeqConflict

        state = _state(tmp_path)
        _seed_session(state)
        attempts: list[int] = []

        def always_conflict(session_id: str, event):
            attempts.append(1)
            raise SeqConflict("注入：持续冲突")

        monkeypatch.setattr(state.store, "append_event", always_conflict)

        with pytest.raises(SeqConflict):
            asyncio.run(
                session_service(state).change_permission_mode(
                    session_id="sid", permission_mode="read-only", auto_approve=True
                )
            )

        assert len(attempts) == 3, f"重试次数应为 3（有界），实际 {len(attempts)}"


# ── 5：生效时机 = 下一轮 run（resume 派生）────────────────────────────


class TestResumeUsesEffectiveMode:
    def _events(self, *extra: SessionEvent) -> list[SessionEvent]:
        return [
            SessionEvent(
                seq=0,
                type=SESSION_STARTED,
                session_id="sid",
                data={SESSION_PERMISSION_MODE_KEY: "read-only"},
            ),
            *extra,
        ]

    def test_resume_uses_declared_when_no_change(self, tmp_path):
        state = _state(tmp_path)
        state.store = _magic_store(self._events())

        kwargs = _resume_capture(state)

        assert kwargs["permission_mode"] is _READ_ONLY

    def test_resume_uses_latest_permission_change(self, tmp_path):
        """改档后下一轮 run 生效（本票唯一要改的派生源）。"""
        state = _state(tmp_path)
        state.store = _magic_store(
            self._events(
                SessionEvent(
                    seq=1,
                    type=PERMISSION_CHANGED,
                    session_id="sid",
                    data={
                        SESSION_PERMISSION_MODE_KEY: "danger-full-access",
                        SESSION_AUTO_APPROVE_KEY: True,
                    },
                )
            )
        )

        kwargs = _resume_capture(state)

        assert kwargs["permission_mode"] is _DANGER

    def test_resume_after_mode_change_restores_interactive_callback(self, tmp_path):
        """改成交互式档位后，续聊必须重建交互式回调（None 在 build_runtime 里 = 全自动）。"""
        from agent_harness.session.approval import InteractiveCallbackHolder

        state = _state(tmp_path)
        state.store = _magic_store(
            [
                SessionEvent(seq=0, type=SESSION_STARTED, session_id="sid", data={}),
                SessionEvent(
                    seq=1,
                    type=PERMISSION_CHANGED,
                    session_id="sid",
                    data={
                        SESSION_PERMISSION_MODE_KEY: "read-only",
                        SESSION_AUTO_APPROVE_KEY: False,
                    },
                ),
            ]
        )

        kwargs = _resume_capture(state)

        assert kwargs["permission_mode"] is _READ_ONLY
        assert isinstance(kwargs["approval_callback"], InteractiveCallbackHolder)

    def test_resume_after_change_to_danger_is_non_interactive(self, tmp_path):
        state = _state(tmp_path)
        state.store = _magic_store(
            [
                SessionEvent(seq=0, type=SESSION_STARTED, session_id="sid", data={}),
                SessionEvent(
                    seq=1,
                    type=PERMISSION_CHANGED,
                    session_id="sid",
                    data={
                        SESSION_PERMISSION_MODE_KEY: "danger-full-access",
                        SESSION_AUTO_APPROVE_KEY: True,
                    },
                ),
            ]
        )

        kwargs = _resume_capture(state)

        assert kwargs["permission_mode"] is _DANGER
        assert kwargs["approval_callback"] is None

    def test_legacy_session_without_any_key_keeps_legacy_behavior(self, tmp_path):
        state = _state(tmp_path)
        state.store = _magic_store(
            [SessionEvent(seq=0, type=SESSION_STARTED, session_id="sid", data={})]
        )

        kwargs = _resume_capture(state)

        assert kwargs["permission_mode"] is _WRITE
        assert kwargs["approval_callback"] is None


# ── 6：fork 继承父 effective 档 ────────────────────────────────────────


class TestForkInheritsEffectiveMode:
    def _fork_state(self, tmp_path):
        state = _state(tmp_path)
        state.session_meta_store = SqliteSessionMetaStore(tmp_path / "harness.db")
        asyncio.run(state.session_meta_store.initialize())
        return state

    @staticmethod
    def _settle(parent) -> None:
        parent.append(USER_MESSAGE, {"content": "first"})
        parent.append(RUN_STARTED, {})
        parent.append(RUN_COMPLETED, {})

    def test_child_inherits_declared_mode(self, tmp_path):
        state = self._fork_state(tmp_path)
        parent = _seed_session(
            state, "parent", started_data={SESSION_PERMISSION_MODE_KEY: "read-only"}
        )
        self._settle(parent)

        child_id = asyncio.run(
            session_service(state).fork(session_id="parent", from_seq=1)
        )

        assert effective_permission_mode(state.store.read_events(child_id)) is _READ_ONLY

    def test_child_inherits_effective_mode_after_change(self, tmp_path):
        """父在会话内改过档 → child 继承**当下**档，而不是创建时的旧档。"""
        state = self._fork_state(tmp_path)
        parent = _seed_session(
            state, "parent", started_data={SESSION_PERMISSION_MODE_KEY: "read-only"}
        )
        self._settle(parent)
        append_permission_change(
            parent, PermissionChange(permission_mode=_DANGER, auto_approve=True)
        )

        child_id = asyncio.run(
            session_service(state).fork(session_id="parent", from_seq=1)
        )

        assert effective_permission_mode(state.store.read_events(child_id)) is _DANGER

    def test_change_after_the_anchor_is_still_inherited(self, tmp_path):
        """父 fork 点**之后**改的档也要被继承（seed 剔除 permission/changed，否则旧的会胜）。"""
        state = self._fork_state(tmp_path)
        parent = _seed_session(
            state, "parent", started_data={SESSION_PERMISSION_MODE_KEY: "read-only"}
        )
        parent.append(USER_MESSAGE, {"content": "first"})  # seq 1 = fork 锚点
        parent.append(RUN_STARTED, {})
        parent.append(RUN_COMPLETED, {})
        append_permission_change(  # 锚点之后
            parent, PermissionChange(permission_mode=_DANGER, auto_approve=False)
        )

        child_id = asyncio.run(
            session_service(state).fork(session_id="parent", from_seq=1)
        )

        child_events = state.store.read_events(child_id)
        assert effective_permission_mode(child_events) is _DANGER
        # seed 里不得残留父的 permission/changed（会话级状态由 child 自己声明）
        assert all(
            e.type != PERMISSION_CHANGED for e in child_events
        )

    def test_child_inherits_effective_auto_approve(self, tmp_path):
        state = self._fork_state(tmp_path)
        parent = _seed_session(
            state,
            "parent",
            started_data={
                SESSION_PERMISSION_MODE_KEY: "workspace-write",
                SESSION_AUTO_APPROVE_KEY: True,
            },
        )
        self._settle(parent)
        append_permission_change(
            parent, PermissionChange(permission_mode=_WRITE, auto_approve=False)
        )

        child_id = asyncio.run(
            session_service(state).fork(session_id="parent", from_seq=1)
        )

        assert effective_auto_approve(state.store.read_events(child_id)) is False

    def test_child_of_undeclared_parent_stays_undeclared(self, tmp_path):
        state = self._fork_state(tmp_path)
        parent = _seed_session(state, "parent")
        self._settle(parent)

        child_id = asyncio.run(
            session_service(state).fork(session_id="parent", from_seq=1)
        )

        child_events = state.store.read_events(child_id)
        assert effective_permission_mode(child_events) is None
        assert effective_auto_approve(child_events) is None
        assert child_events[-1].type == SESSION_FORKED


# ── 事件已进词汇表（守卫）──────────────────────────────────────────────


def test_permission_changed_is_a_durable_event_type():
    from agent_harness.session.event import EVENT_TYPES

    assert PERMISSION_CHANGED == "permission/changed"
    assert PERMISSION_CHANGED in EVENT_TYPES
