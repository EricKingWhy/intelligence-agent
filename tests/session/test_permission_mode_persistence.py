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
5. 续聊：未声明 → **v2 新默认** `workspace-write` + ask（交互式回调，#358 D2 翻转）；
6. 纯函数 `declared_permission_mode` 的边界（无 started / 坏值 → None）；
7. `auto_approve` 同病同修：显式声明才落键；续聊复原**审批路由**（交互式回调非
   None，#423）而不是退化成全自动批准；档位声明优先于它（与创建路径同一优先级）；
8. 纯函数 `declared_auto_approve` 只认 bool，坏值按未声明处理。

#358 / W-14（本文件新增，D2 默认翻转 + 旧 Session 迁移）：

9. 新会话 `session/started` **恒写** `permission_defaults_version=2`；
10. v2 新会话未声明档位 → 续聊 ask（interactive）；
11. 旧会话（无 v2 标记、无 `permission/changed`、无声明）续聊 → **沿用旧默认**
    （auto-approve）+ 一次性提示（`LaunchResult.warnings`）+ 落 `permission/changed`
    迁移事件；二次续聊幂等（不再提示、仍旧默认）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent_harness.config import Settings
from agent_harness.session import PERMISSION_CHANGED, SESSION_STARTED
from agent_harness.session.approval import (
    InteractiveCallbackHolder,
    declared_auto_approve,
    declared_permission_mode,
)
from agent_harness.session.service import SessionService
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.web.app import session_service
from tests.session.ledger_doubles import idle_operation_ledger


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
    state.operation_ledger = idle_operation_ledger()
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    # `#318`：fork 谱系会 await 账行读数；AsyncMock(None) = "父没有账行"。
    state.stores.delegation_tree_ledger.get_session_budget = AsyncMock(
        return_value=None
    )
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


def test_new_session_always_writes_defaults_version(tmp_path):
    """#358：新会话 `session/started` **恒写** `permission_defaults_version=2`。

    档位 / auto_approve 的 keys 仍只在显式时写（上面两条用例），版本键是新契约。
    """
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(session_service(state))

    started = state.store.read_events(session_id)[0]
    assert started.data["permission_defaults_version"] == 2


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


def _resume_result(model_state: MagicMock, session_id: str):
    """同 :func:`_resume`，但同时返回 LaunchResult（读 warnings，#358）。"""
    captured, patcher = _capture_build()
    with patcher:
        service = session_service(model_state)
        result = asyncio.run(
            service.resume_and_launch(
                session_id=session_id, task="再来一轮", amend=None
            )
        )
    return captured, result


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


def test_resume_without_declared_mode_defaults_to_ask(tmp_path):
    """#358（D2 默认翻转）：v2 新会话未声明档位 → 续聊 ask（交互式 holder）。

    旧契约（"未声明 → workspace-write + None，行为逐字不变"）已被 #358 收紧取代：
    新会话缺省是 workspace-write + ask。
    """
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(session_service(state))
    state.approval_queues.clear()

    captured = _resume(state, session_id)

    assert captured[0]["permission_mode"] is PermissionPolicy.WORKSPACE_WRITE
    assert isinstance(captured[0]["approval_callback"], InteractiveCallbackHolder), (
        "v2 新会话缺省应走 D2 新默认（ask），不再是 auto-approve"
    )


# ── 1b / 3b：deny 路由（auto_approve=false，未选档位）同病同修 ─────────


def test_explicit_auto_approve_false_is_persisted(tmp_path):
    """auto_approve 的显式声明也要落盘——它是续聊审批路由（#423 弹卡）的唯一依据。"""
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


def test_resume_restores_interactive_route_instead_of_auto_approving(tmp_path):
    """续聊复原审批路由（#423）：auto_approve=false 的会话续聊必须重新弹卡。

    创建期该组合走 interactive 路由（#423，不再 deny）；续聊必须同一待遇。
    回调退化成 None 才是 F15 的原始 bug——``build_runtime`` 对 ``None`` 的
    语义是全自动批准，等于把用户声明悄悄撤掉。
    """
    state = _state(tmp_path)
    _, patcher = _capture_build()
    with patcher:
        session_id = _create(
            session_service(state),
            auto_approve_explicit=True,
            auto_approve=False,
        )
    # 创建路径已登记过队列；清掉才能证明是续聊自己重新登记的。
    state.approval_queues.clear()

    captured = _resume(state, session_id)

    callback = captured[0]["approval_callback"]
    assert isinstance(callback, InteractiveCallbackHolder), (
        "续聊必须重建交互式回调——None 在 build_runtime 里的语义是全自动批准"
    )
    assert session_id in state.approval_queues  # 人工队列重新登记，等待有人接


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


# ── #358：旧 Session 迁移（mock 一次、幂等）─────────────────────────────


def _create_legacy_session(state: MagicMock, tmp_path) -> str:
    """构造一个"创建于 #358 默认权限收紧之前"的旧会话。

    生产路径 `create_and_launch` 现在恒写 v2 标记，造不出旧会话；历史会话的对盘
    形状就是"`session/started` 无版本键、无 `permission/changed`、无档位 / auto_approve
    声明"，直接 `Session.start`（不传 started_data）即该形状。
    """
    from uuid import uuid4

    from agent_harness.session.session import Session

    session = Session.start(state.store, session_id=str(uuid4()), cwd=tmp_path)
    return session.session_id


def _migration_events(state: MagicMock, session_id: str) -> list:
    return [
        event for event in state.store.read_events(session_id)
        if event.type == PERMISSION_CHANGED
        and event.data.get("permission_migration") == "legacy-v1-defaults"
    ]


def test_legacy_session_resume_keeps_old_default_with_one_time_warning(tmp_path):
    """#358：旧会话续聊 → 沿用旧默认（auto-approve）+ 一次性提示 + 迁移事件。"""
    state = _state(tmp_path)
    session_id = _create_legacy_session(state, tmp_path)

    captured, result = _resume_result(state, session_id)

    assert captured[0]["permission_mode"] is PermissionPolicy.WORKSPACE_WRITE
    assert captured[0]["approval_callback"] is None, "旧会话沿用旧默认（自动批准）"
    assert len(result.warnings) == 1, "一次性提示有且仅有一条"
    assert "沿用旧默认" in result.warnings[0]

    migrated = _migration_events(state, session_id)
    assert len(migrated) == 1
    assert migrated[0].data["permission_mode"] == "workspace-write"
    assert migrated[0].data["auto_approve"] is True


def test_legacy_session_second_resume_is_idempotent(tmp_path):
    """#358：迁移事件在位后二次续聊 → 不再提示、仍旧默认（幂等，只落一次）。"""
    state = _state(tmp_path)
    session_id = _create_legacy_session(state, tmp_path)
    _resume_result(state, session_id)  # 第一次：落迁移事件 + 一条警告

    captured, result = _resume_result(state, session_id)

    assert captured[0]["approval_callback"] is None
    assert result.warnings == [], "二次续聊不再提示"
    assert len(_migration_events(state, session_id)) == 1, "迁移事件只落一次"


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
