"""#1278：fork 继承父会话显式选择的 sandbox 后端。

语义（issue #1278）：`fork` 是「同一会话回到更早时间点」，child 应延续父的
运行时配置。父的显式选择记在父 `session/started.data.sandbox_backend`
（#363 / W-19），而 fork seed **按设计排除父 `session/started`** ⇒ child 必须
显式读父事件并显式传给 `Session.start`，不能靠 seed 继承。

优先级：显式参数 > 继承父 > 部署默认（None）。父未显式选择时 child 仍传
None，行为与修前逐字一致（部署默认路径不变）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_harness.config import Settings
from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_STARTED,
    SESSION_STARTED,
    USER_MESSAGE,
)
from agent_harness.session.fork import fork_session
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage.sqlite import SqliteSessionMetaStore
from agent_harness.web.app import session_service
from tests.session.ledger_doubles import idle_operation_ledger

_CATALOG = (
    '[{"name": "gpt-4o", "provider": "deepseek", "model_name": "gpt-4o-mini"}]'
)


class _RecordingRegistry:
    """记录 `Session.start` 传给 `registry.create` 的 backend。

    刻意让缺省（backend=None）与 "local" 可区分：若实现没有继承父的显式选择，
    这里收到的就是 None —— 与 Windows 上「未显式选择 ⇒ 部署默认 docker」同形。
    父无 workspace（`exists=False`）⇒ `_copy_workspace` 早返回，不进入拷贝分支。
    """

    def __init__(self) -> None:
        self.created: list[tuple[str, str | None]] = []

    def create(self, session_id: str, backend: str | None = None, **_kwargs):
        self.created.append((session_id, backend))

    def exists(self, session_id: str) -> bool:
        return False


def _store(tmp_path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path / "sessions")


async def _meta_store(tmp_path) -> SqliteSessionMetaStore:
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    return meta


def _build_parent(store: JsonlSessionStore, *, sandbox_backend=None) -> Session:
    """两轮对话的父会话；返回时最后一条事件是第二轮的 fork 锚点（seq=4）。"""
    parent = Session.start(
        store, session_id="parent", sandbox_backend=sandbox_backend
    )
    parent.append(USER_MESSAGE, {"content": "第一条"})
    parent.append(RUN_STARTED, {})
    parent.append(RUN_COMPLETED, {})
    parent.append(USER_MESSAGE, {"content": "第二条"})
    return parent


def _state(tmp_path) -> MagicMock:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_provider="deepseek",
        model_name="deepseek-chat",
        agent_models=_CATALOG,
        provider_store_path=str(tmp_path / "model-providers.json"),
    )
    state = MagicMock()
    state.settings = settings
    state.store = _store(tmp_path)
    state.workspaces_root = tmp_path
    state.workspace_registry = _RecordingRegistry()
    state.workspace_index = None
    state.run_manager = MagicMock()
    state.run_manager.get_active = MagicMock(return_value=None)
    state.session_meta_store = SqliteSessionMetaStore(tmp_path / "harness.db")
    asyncio.run(state.session_meta_store.initialize())
    state.operation_ledger = idle_operation_ledger()
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    # `#318`：fork 谱系会 await 账行读数；None = "父没有账行"。
    state.stores.delegation_tree_ledger.get_session_budget = AsyncMock(
        return_value=None
    )
    return state


@pytest.mark.asyncio
async def test_fork_inherits_parent_sandbox_backend(tmp_path) -> None:
    """父显式选 "local"、fork 不传参 ⇒ child 记 local 且 registry 收到 local。"""
    store = _store(tmp_path)
    meta = await _meta_store(tmp_path)
    _build_parent(store, sandbox_backend="local")
    registry = _RecordingRegistry()

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=4,
        child_session_id="child",
        workspace_registry=registry,
    )

    assert registry.created == [("child", "local")]
    started = child.events[0]
    assert started.type == SESSION_STARTED
    assert started.data["sandbox_backend"] == "local"


@pytest.mark.asyncio
async def test_explicit_backend_overrides_parent(tmp_path) -> None:
    """显式传值优先于父的显式选择（显式 > 继承）。"""
    store = _store(tmp_path)
    meta = await _meta_store(tmp_path)
    _build_parent(store, sandbox_backend="local")
    registry = _RecordingRegistry()

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=4,
        child_session_id="child",
        workspace_registry=registry,
        sandbox_backend="docker",
    )

    assert registry.created == [("child", "docker")]
    assert child.events[0].data["sandbox_backend"] == "docker"


@pytest.mark.asyncio
async def test_parent_without_declaration_passes_none(tmp_path) -> None:
    """父未显式选择 ⇒ child 也走 None（部署默认），不写 started 键。"""
    store = _store(tmp_path)
    meta = await _meta_store(tmp_path)
    _build_parent(store)
    registry = _RecordingRegistry()

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=4,
        child_session_id="child",
        workspace_registry=registry,
    )

    assert registry.created == [("child", None)]
    assert "sandbox_backend" not in child.events[0].data


def test_service_fork_inherits_parent_backend(tmp_path) -> None:
    """service 层缺省同样继承（web / 内部调用共用一条路径）。"""
    state = _state(tmp_path)
    _build_parent(state.store, sandbox_backend="local")
    registry = state.workspace_registry

    child_id = asyncio.run(
        session_service(state).fork(session_id="parent", from_seq=4)
    )

    assert registry.created == [(child_id, "local")]
    assert state.store.read_events(child_id)[0].data["sandbox_backend"] == "local"


def test_service_fork_passes_explicit_backend(tmp_path) -> None:
    """`SessionService.fork` 把显式 backend 原样透传给 `fork_session`。"""
    state = _state(tmp_path)
    _build_parent(state.store, sandbox_backend="local")
    registry = state.workspace_registry

    child_id = asyncio.run(
        session_service(state).fork(
            session_id="parent", from_seq=4, sandbox_backend="docker"
        )
    )

    assert registry.created == [(child_id, "docker")]
    assert state.store.read_events(child_id)[0].data["sandbox_backend"] == "docker"
