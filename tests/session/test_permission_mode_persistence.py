"""F15 #234：会话级权限档落进 `session/started`，并在续聊路径复原审批边界。

背景（真机复现，见 issue #234）：`resume_and_launch` 曾硬编码
``permission_mode=WORKSPACE_WRITE`` + ``approval_callback=None``，而
``build_runtime`` 对 ``None`` 的语义是**全自动批准** —— 用户显式选的
「只读」从第二条消息起静默失效，写工具不经审批直接执行。

契约（本文件锁死）：

1. 创建时**显式声明**权限档 → `session/started` 带 `permission_mode` 键；
2. 未显式声明 → **不写键**，与历史会话逐字不可区分（既有行为不变）；
3. 续聊：声明过 → `build_runtime` 收到该档 + 交互式审批回调（并登记队列）；
4. 续聊：`danger-full-access` → 非交互（回调 None）；
5. 续聊：未声明 → `workspace-write` + `None`（今天的行为逐字不变）；
6. 纯函数 `declared_permission_mode` 的边界（无 started / 坏值 → None）；
7. `auto_approve` 同病同修：显式声明才落键；续聊复原 **deny 路由**（回调非 None）
   而不是退化成全自动批准；档位声明优先于它（与创建路径同一优先级）；
8. 纯函数 `declared_auto_approve` 只认 bool，坏值按未声明处理。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent_harness.config import Settings
from agent_harness.session import SESSION_STARTED
from agent_harness.session.approval import (
    InteractiveCallbackHolder,
    declared_auto_approve,
    declared_permission_mode,
)
from agent_harness.session.service import SessionService
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.web.app import session_service


def _state(tmp_path) -> MagicMock:
    """与 test_launch_false.py::_state 同构的隔离 AppState 替身 + 审批队列。"""
    from agent_harness.session.store import JsonlSessionStore

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
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    # 审批队列字典必须是真 dict：交互路径会 __setitem__ 后断言成员关系。
    state.approval_queues = {}
    state.message_queues = MagicMock()
    return state


def _capture_build() -> tuple[list[dict], object]:
    """返回 (captured, patch)：patch ``service.build_runtime`` 并收集 kwargs。"""
    captured: list[dict] = []

    async def _fake_build(**kwargs):
        captured.append(kwargs)
        return MagicMock()

    return captured, patch(
        "agent_harness.session.service.build_runtime", _fake_build
    )


def _create(service: SessionService, **kwargs) -> str:
    """只建会话（launch=False），返回 session_id。"""
    result = asyncio.run(service.create_and_launch(task="hi", launch=False, **kwargs))
    return result.session.session_id


# ── 1 / 2：落盘 ──────────────────────────────────────────────────────


def test_explicit_permission_mode_is_persisted_in_session_started(tmp_path):
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(
            session_service(state),
            permission_mode=PermissionPolicy.READ_ONLY,
            permission_mode_explicit=True,
        )

    started = state.store.read_events(session_id)[0]
    assert started.type == SESSION_STARTED
    assert started.data["permission_mode"] == "read-only"


def test_undeclared_permission_mode_writes_no_key(tmp_path):
    """未显式声明 → 不写键（历史会话的日志形状，逐字不变）。"""
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(
            session_service(state),
            permission_mode=PermissionPolicy.WORKSPACE_WRITE,
            permission_mode_explicit=False,
        )

    started = state.store.read_events(session_id)[0]
    assert "permission_mode" not in started.data


# ── 3 / 4 / 5：续聊复原 ──────────────────────────────────────────────


def _resume(model_state: MagicMock, session_id: str) -> list[dict]:
    captured, patcher = _capture_build()
    with patcher:
        service = session_service(model_state)
        asyncio.run(
            service.resume_and_launch(
                session_id=session_id, task="再来一轮", amend=None
            )
        )
    return captured


def test_resume_restores_declared_mode_and_interactive_callback(tmp_path):
    """只读会话续聊：档位复原 + 交互式审批回调登记（F15 的核心回归锁）。"""
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(
            session_service(state),
            permission_mode=PermissionPolicy.READ_ONLY,
            permission_mode_explicit=True,
        )
    # 只建路径会把队列撤掉（无 run 无从 GC）；续聊重新登记才算复原。
    state.approval_queues.clear()

    captured = _resume(state, session_id)

    assert captured[0]["permission_mode"] is PermissionPolicy.READ_ONLY
    callback = captured[0]["approval_callback"]
    assert isinstance(callback, InteractiveCallbackHolder), (
        "续聊必须重建交互式回调——None 在 build_runtime 里的语义是全自动批准"
    )
    assert session_id in state.approval_queues


def test_resume_danger_full_access_stays_non_interactive(tmp_path):
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(
            session_service(state),
            permission_mode=PermissionPolicy.DANGER_FULL_ACCESS,
            permission_mode_explicit=True,
        )
    state.approval_queues.clear()

    captured = _resume(state, session_id)

    assert captured[0]["permission_mode"] is PermissionPolicy.DANGER_FULL_ACCESS
    assert captured[0]["approval_callback"] is None
    assert session_id not in state.approval_queues


def test_resume_without_declared_mode_keeps_legacy_behavior(tmp_path):
    """未声明档位的会话（含全部历史会话）续聊行为逐字不变。"""
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(session_service(state))

    captured = _resume(state, session_id)

    assert captured[0]["permission_mode"] is PermissionPolicy.WORKSPACE_WRITE
    assert captured[0]["approval_callback"] is None


# ── 1b / 3b：deny 路由（auto_approve=false，未选档位）同病同修 ─────────


def test_explicit_auto_approve_false_is_persisted(tmp_path):
    """auto_approve 的显式声明也要落盘——它是 deny 路由的唯一依据。"""
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(
            session_service(state),
            auto_approve_explicit=True,
            auto_approve=False,
        )

    started = state.store.read_events(session_id)[0]
    assert started.data["auto_approve"] is False
    assert "permission_mode" not in started.data


def test_undeclared_auto_approve_writes_no_key(tmp_path):
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(session_service(state))

    assert "auto_approve" not in state.store.read_events(session_id)[0].data


def test_resume_restores_deny_route_instead_of_auto_approving(tmp_path):
    """续聊复原 deny 路由：回调必须**存在**——None 在 build_runtime 里 = 全自动批准，
    等于把用户创建的"不自动批准"从第二条消息起悄悄撤掉。"""
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(
            session_service(state),
            auto_approve_explicit=True,
            auto_approve=False,
        )

    captured = _resume(state, session_id)

    callback = captured[0]["approval_callback"]
    assert callback is not None, "deny 路由续聊退化成全自动批准（F15 的同一根因）"
    assert not isinstance(callback, InteractiveCallbackHolder)  # 非交互 = 不经人工
    assert session_id not in state.approval_queues  # 没有人工队列，就不会有等待


def test_resume_declared_auto_approve_true_keeps_auto_approve(tmp_path):
    """显式声明 auto_approve=true（且未选档位）→ 续聊仍 None（自动批准），逐字不变。"""
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(
            session_service(state),
            auto_approve_explicit=True,
            auto_approve=True,
        )

    assert state.store.read_events(session_id)[0].data["auto_approve"] is True
    captured = _resume(state, session_id)
    assert captured[0]["approval_callback"] is None


def test_declared_permission_mode_takes_priority_over_auto_approve(tmp_path):
    """两者都声明时以档位为准（与创建路径的分支优先级一致：interactive 先判）。"""
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(
            session_service(state),
            permission_mode=PermissionPolicy.WORKSPACE_WRITE,
            permission_mode_explicit=True,
            auto_approve_explicit=True,
            auto_approve=False,
        )
    state.approval_queues.clear()

    captured = _resume(state, session_id)

    # 创建时走的是 interactive（第一支），续聊也必须回到同一条路。
    assert isinstance(captured[0]["approval_callback"], InteractiveCallbackHolder)


# ── 6：纯函数边界 ────────────────────────────────────────────────────


def test_declared_permission_mode_returns_none_without_started(tmp_path):
    assert declared_permission_mode([]) is None


def test_declared_permission_mode_tolerates_garbage_value(tmp_path):
    """手改日志写出非法档位 → 按"未声明"处理（记 warning，不静默改写策略）。"""
    event = MagicMock()
    event.type = SESSION_STARTED
    event.data = {"permission_mode": "delete-everything"}
    assert declared_permission_mode([event]) is None


class TestDeclaredAutoApprove:
    """``declared_auto_approve`` 的边界：只认 bool，坏值按未声明（不猜更松的值）。"""

    @staticmethod
    def _started(data: dict) -> MagicMock:
        event = MagicMock()
        event.type = SESSION_STARTED
        event.data = data
        return event

    def test_returns_none_without_started(self):
        assert declared_auto_approve([]) is None

    def test_returns_none_when_key_absent(self):
        assert declared_auto_approve([self._started({})]) is None

    def test_reads_both_bool_values(self):
        assert declared_auto_approve([self._started({"auto_approve": False})]) is False
        assert declared_auto_approve([self._started({"auto_approve": True})]) is True

    def test_non_bool_is_treated_as_undeclared(self):
        """`0` / `"false"` / `{}` 这类值不能当 False 用——那会凭空造出一个 deny 路由；
        也不能当 True——那会凭空撤掉。统一按未声明处理。"""
        for raw in (0, 1, "false", "true", {}, [], None):
            assert declared_auto_approve([self._started({"auto_approve": raw})]) is None
