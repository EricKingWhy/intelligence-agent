"""#1278：fork 继承父会话显式选择的 sandbox 后端。

语义（issue #1278）：child 必须能启动，所以缺省沿用父**显式选择**的 sandbox
后端（环境兼容性，不是安全边界 / 权限面）。父的显式选择记在父
`session/started.data.sandbox_backend`（#363 / W-19），而 fork seed **按设计
排除父 `session/started`** ⇒ child 必须显式读父事件并显式传给 `Session.start`，
不能靠 seed 继承。

优先级：显式参数 > 继承父 > 部署默认（None）。父未显式选择时 child 仍传
None，行为与修前逐字一致（部署默认路径不变）。
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

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
from tests.session.registry_doubles import RecordingRegistry


@pytest.fixture
def fork_env(tmp_path, make_session_service):
    """真 store / 真 meta store + 记录型 registry；service 走 conftest 约定。

    `stores.delegation_tree_ledger.get_session_budget` 按真实契约答 None
    （#318：父没有账行）——裸 MagicMock 在这里是**不可 await** 的，会让
    fork 谱系读账时以难懂的方式炸掉。
    """
    store = JsonlSessionStore(root=tmp_path / "sessions")
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    asyncio.run(meta.initialize())
    registry = RecordingRegistry()
    stores = MagicMock()
    stores.delegation_tree_ledger.get_session_budget = AsyncMock(return_value=None)
    service = make_session_service(
        store=store,
        session_meta_store=meta,
        workspace_registry=registry,
        stores=stores,
    )
    return store, meta, registry, service


def _build_parent(
    store: JsonlSessionStore,
    *,
    sandbox_backend: str | None = None,
    started_data: dict | None = None,
) -> Session:
    """两轮对话的父会话；返回时最后一条事件是第二轮的 fork 锚点（seq=4）。

    `started_data` 用于复现**历史数据**里的任意 `sandbox_backend` 值（空串 /
    已退役后端名）——`sandbox_backend` 参数只承载真正的显式选择，这些非法值
    是旧版本落盘的，只能从事件数据构造。
    """
    parent = Session.start(
        store,
        session_id="parent",
        sandbox_backend=sandbox_backend,
        started_data=started_data,
    )
    parent.append(USER_MESSAGE, {"content": "第一条"})
    parent.append(RUN_STARTED, {})
    parent.append(RUN_COMPLETED, {})
    parent.append(USER_MESSAGE, {"content": "第二条"})
    return parent


@pytest.mark.asyncio
async def test_fork_inherits_parent_sandbox_backend(fork_env) -> None:
    """父显式选 "local"、fork 不传参 ⇒ child 记 local 且 registry 收到 local。"""
    store, meta, registry, _ = fork_env
    _build_parent(store, sandbox_backend="local")

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
async def test_explicit_backend_overrides_parent(fork_env) -> None:
    """显式传值优先于父的显式选择（显式 > 继承）。"""
    store, meta, registry, _ = fork_env
    _build_parent(store, sandbox_backend="local")

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
async def test_parent_without_declaration_passes_none(fork_env) -> None:
    """父未显式选择 ⇒ child 也走 None（部署默认），不写 started 键。"""
    store, meta, registry, _ = fork_env
    _build_parent(store)

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=4,
        child_session_id="child",
        workspace_registry=registry,
    )

    assert registry.created == [("child", None)]
    assert "sandbox_backend" not in child.events[0].data


@pytest.mark.asyncio
async def test_empty_string_backend_is_not_inherited(fork_env, caplog) -> None:
    """历史 `session/started.data.sandbox_backend == ""` 不算显式选择。

    「空白」与「已退役的未知值」是两件事：前者等价于**没声明**（静默忽略），
    后者要留一条 warning。守卫（`isinstance(str) and truthy`）的鉴别力用例：
    把它改成裸 `.get()` 后，空串会落进未知分支 ⇒ 多出一条 warning；若再没有
    `SUPPORTED_BACKENDS` 归一，空串还会被透传给 registry 并写进 child 的
    `session/started`。
    """
    store, meta, registry, _ = fork_env
    _build_parent(store, started_data={"sandbox_backend": ""})

    with caplog.at_level(logging.WARNING, logger="agent_harness.session.fork"):
        child = await fork_session(
            store, meta, "parent",
            boundary_user_message_seq=4,
            child_session_id="child",
            workspace_registry=registry,
        )

    assert registry.created == [("child", None)]
    assert "sandbox_backend" not in child.events[0].data
    assert "不在支持列表" not in caplog.text


@pytest.mark.asyncio
async def test_unknown_inherited_backend_falls_back_to_default(
    fork_env, caplog
) -> None:
    """父记录的已退役后端名（#1048 的 windows_psec）降级为 None + warning。

    继承值直接透传会让 registry 抛 ValueError → 500，且所有记着旧值的父会话
    永久不可 fork；这里必须降级、不得 500。
    """
    store, meta, registry, _ = fork_env
    _build_parent(store, started_data={"sandbox_backend": "windows_psec"})

    with caplog.at_level(logging.WARNING, logger="agent_harness.session.fork"):
        child = await fork_session(
            store, meta, "parent",
            boundary_user_message_seq=4,
            child_session_id="child",
            workspace_registry=registry,
        )

    assert registry.created == [("child", None)]
    assert "sandbox_backend" not in child.events[0].data
    assert "windows_psec" in caplog.text


def test_service_fork_inherits_parent_backend(fork_env) -> None:
    """service 层缺省同样继承（web / 内部调用共用一条路径）。"""
    store, _, registry, service = fork_env
    _build_parent(store, sandbox_backend="local")

    child_id = asyncio.run(service.fork(session_id="parent", from_seq=4))

    assert registry.created == [(child_id, "local")]
    assert store.read_events(child_id)[0].data["sandbox_backend"] == "local"


def test_service_fork_passes_explicit_backend(fork_env) -> None:
    """`SessionService.fork` 把显式 backend 原样透传给 `fork_session`。"""
    store, _, registry, service = fork_env
    _build_parent(store, sandbox_backend="local")

    child_id = asyncio.run(
        service.fork(
            session_id="parent", from_seq=4, sandbox_backend="docker"
        )
    )

    assert registry.created == [(child_id, "docker")]
    assert store.read_events(child_id)[0].data["sandbox_backend"] == "docker"
