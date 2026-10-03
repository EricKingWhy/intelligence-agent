"""#555 启动扫描单元测试：未完成 fork 的判定、回收边界与失败隔离。

回收边界是本文件的核心（#172 同类教训）：只动 harness 自建工件——暂存根
（registry root 直下）与默认形态子工作区（构造规则白名单）；命名 workspace
（用户目录形态，哪怕名字看起来像内部目录）、完成态 fork child 的工作区、
child 的 JSONL/映射，全部不碰。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.recovery.scan import scan_unfinished_forks
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session import Session
from agent_harness.session.store import JsonlSessionStore

pytestmark = pytest.mark.asyncio

INTENT = "fork/in-progress"
FORKED = "session/forked"


def _setup(tmp_path: Path) -> tuple[JsonlSessionStore, WorkspaceRegistry]:
    store = JsonlSessionStore(root=tmp_path / "sessions")
    registry = WorkspaceRegistry(root=tmp_path / "sandbox", backend="local")
    return store, registry


def _mark_unfinished_fork(
    store: JsonlSessionStore, registry: WorkspaceRegistry, session_id: str
) -> None:
    """手工摆出 W1 之后、forked 之前的崩溃现场（不跑 fork 流程）。"""
    child = Session.start(store, session_id=session_id, workspace_registry=registry)
    child.append(INTENT, {"parent_session_id": "p", "boundary_user_message_seq": 1})


async def test_scan_reclaims_unfinished_fork_residue(tmp_path: Path) -> None:
    store, registry = _setup(tmp_path)
    _mark_unfinished_fork(store, registry, "child")
    staging = registry.fork_staging_root()
    (staging / "child-ab12cd34").mkdir(parents=True)
    (staging / "child-ab12cd34" / "half.txt").write_text("半份", encoding="utf-8")
    child_workspace = registry.default_workspace_root("child")
    (child_workspace / "half.txt").write_text("半份", encoding="utf-8")

    results = await scan_unfinished_forks(
        session_store=store, workspace_registry=registry
    )

    assert [r.session_id for r in results] == ["child"]
    assert results[0].reclaimed and results[0].detail is None
    assert not staging.exists()
    assert not child_workspace.exists()
    # 可见性事实保留：JSONL（含标记）与映射
    assert [e.type for e in store.read_events("child")] == [
        "session/started", INTENT,
    ]
    assert registry.exists("child")


async def test_scan_sweeps_staging_even_without_unfinished_child(
    tmp_path: Path,
) -> None:
    """启动时暂存根里的任何残留都是垃圾（补偿失败等遗留），无条件清空。"""
    store, registry = _setup(tmp_path)
    staging = registry.fork_staging_root()
    (staging / "ghost-0000").mkdir(parents=True)
    (staging / "ghost-0000" / "x.txt").write_text("x", encoding="utf-8")

    results = await scan_unfinished_forks(
        session_store=store, workspace_registry=registry
    )

    assert results == []
    assert not staging.exists()


async def test_scan_never_touches_named_workspaces_or_completed_children(
    tmp_path: Path,
) -> None:
    """回收白名单：命名 workspace（用户目录）与完成态 child 工作区不动。"""
    store, registry = _setup(tmp_path)
    named = tmp_path / "sandbox" / "workspaces" / "myproject"
    named.mkdir(parents=True, exist_ok=True)
    (named / "user.txt").write_text("用户资产", encoding="utf-8")
    # 完成态 fork child：有标记也有 forked——不是未完成
    done = Session.start(store, session_id="done", workspace_registry=registry)
    done.append(INTENT, {"parent_session_id": "p", "boundary_user_message_seq": 1})
    done.append(FORKED, {
        "parent_session_id": "p", "boundary_user_message_seq": 1,
        "fork_point_seq": None,
    })
    (registry.default_workspace_root("done") / "keep.txt").write_text(
        "完成态资产", encoding="utf-8"
    )

    results = await scan_unfinished_forks(
        session_store=store, workspace_registry=registry
    )

    assert results == []
    assert (named / "user.txt").exists(), "用户命名 workspace 绝不回收"
    assert (registry.default_workspace_root("done") / "keep.txt").exists()


class _ExplodingReadStore(JsonlSessionStore):
    """对指定 session 抛读失败（store 本身按设计容忍半行，注入更硬的故障）。"""

    def read_events(self, session_id: str):
        if session_id == "bad":
            raise OSError("磁盘读取失败（注入）")
        return super().read_events(session_id)


async def test_scan_isolates_unreadable_child(tmp_path: Path) -> None:
    """单个读失败的会话不阻断整轮扫描；失败如实上报 detail、不回收。"""
    _setup(tmp_path)
    store = _ExplodingReadStore(root=tmp_path / "sessions")
    registry = WorkspaceRegistry(root=tmp_path / "sandbox", backend="local")
    _mark_unfinished_fork(store, registry, "good")
    # "bad" 必须真实存在（list_session_ids 只列有 events.jsonl 的目录），
    # 读取时才轮到注入的失败。
    bad_path = store._events_path("bad")
    bad_path.parent.mkdir(parents=True)
    bad_path.write_text("{}\n", encoding="utf-8")

    results = await scan_unfinished_forks(
        session_store=store, workspace_registry=registry
    )

    by_id = {r.session_id: r for r in results}
    assert by_id["good"].reclaimed
    assert by_id["bad"].detail is not None
    assert not by_id["bad"].reclaimed


async def test_scan_without_registry_reports_visibility_only(
    tmp_path: Path,
) -> None:
    """无注册表（纯事件层部署）：只报告未完成 child，无工件可回收。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    child = Session.start(store, session_id="child")
    child.append(INTENT, {"parent_session_id": "p", "boundary_user_message_seq": 1})

    results = await scan_unfinished_forks(
        session_store=store, workspace_registry=None
    )

    assert [r.session_id for r in results] == ["child"]
    assert results[0].reclaimed is False
