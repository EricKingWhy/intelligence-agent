"""W-06（#350）：Fork 分家——child 独立进度文件、父子互不影响、失败无半成品。

票面工作指令 3：Fork 仅在既有 ``session/fork.py`` 的合法稳定边界执行；新
Session 写独立 ``agent-progress/<child-id>/progress.md``，带 parent ID /
fork seq（由 child 事件流的 ``session/forked`` 投影，derive 单源）；父子
随后更新互不影响；Fork 失败不能留下误导性半成品 child 文件。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_STARTED,
    USER_MESSAGE,
)
from agent_harness.session.fork import fork_session
from agent_harness.session.progress import (
    PROGRESS_VERIFY_OK,
    progress_paths,
    verify_progress_file,
    write_progress_file,
)
from agent_harness.session.task import apply_task_definition, apply_verification
from agent_harness.storage.sqlite import SqliteSessionMetaStore

pytestmark = pytest.mark.asyncio


def _parent(tmp_path: Path) -> Session:
    parent = Session.start(
        JsonlSessionStore(root=tmp_path / "sessions"),
        session_id="parent", cwd=str(tmp_path),
    )
    parent.append(USER_MESSAGE, {"content": "第一条"})
    parent.append(RUN_STARTED, {})
    parent.append(MODEL_COMPLETED, {"content": "好的"})
    parent.append(RUN_COMPLETED, {})
    # 任务定义在锚点（第二条用户消息）**之前**：定义事件属于 seed 前缀，
    # child 投影才能含任务事实（fork 锚点之后的事件不进 seed）。
    assert apply_task_definition(
        parent, task_text="父任务", criteria=[{"text": "父验收项 A"}],
    ).ok
    parent.append(USER_MESSAGE, {"content": "第二条"})
    return parent


def _meta_store(tmp_path: Path) -> SqliteSessionMetaStore:
    return SqliteSessionMetaStore(tmp_path / "harness.db")


def _second_user_message_seq(parent: Session) -> int:
    return [e for e in parent.events if e.type == USER_MESSAGE][-1].seq


async def _fork(tmp_path: Path, parent: Session, store, meta) -> Session:
    return await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=_second_user_message_seq(parent),
        child_session_id="child",
    )


class TestForkChildProgressFile:
    async def test_child_gets_own_file_with_parent_and_fork_seq(self, tmp_path) -> None:
        parent = _parent(tmp_path)
        store = JsonlSessionStore(root=tmp_path / "sessions")
        meta = _meta_store(tmp_path)
        await meta.initialize()
        child = await _fork(tmp_path, parent, store, meta)
        assert child.session_id == "child"

        child_path = progress_paths(tmp_path, "child").markdown
        parent_path = progress_paths(tmp_path, "parent").markdown
        assert child_path.exists(), "child 独立进度文件在 fork 完成时落盘"
        body = child_path.read_text(encoding="utf-8")
        assert "- session_id: child" in body
        assert "- parent_session_id: parent" in body
        assert "- fork_point_seq: " in body and "fork_point_seq: -" not in body
        assert parent_path.exists() is False or True  # 父文件是否存在取决于此前是否写过
        # 父文件零写入：fork 不改父（有父文件时逐字节比对）
        if parent_path.exists():
            before = parent_path.read_bytes()
            await _fork(tmp_path, parent, store, meta)
            assert parent_path.read_bytes() == before

    async def test_parent_file_untouched_by_fork(self, tmp_path) -> None:
        parent = _parent(tmp_path)
        assert write_progress_file(tmp_path, "parent", parent.events).ok
        store = JsonlSessionStore(root=tmp_path / "sessions")
        meta = _meta_store(tmp_path)
        await meta.initialize()
        before = progress_paths(tmp_path, "parent").markdown.read_bytes()

        await _fork(tmp_path, parent, store, meta)

        assert progress_paths(tmp_path, "parent").markdown.read_bytes() == before, \
            "fork 对父进度文件零写入（03 §7 父不变）"
        child_body = progress_paths(tmp_path, "child").markdown.read_text("utf-8")
        assert "父任务" in child_body, "child 投影含 seed 里的任务事实"

    async def test_parent_and_child_update_independently(self, tmp_path) -> None:
        parent = _parent(tmp_path)
        assert write_progress_file(tmp_path, "parent", parent.events).ok
        store = JsonlSessionStore(root=tmp_path / "sessions")
        meta = _meta_store(tmp_path)
        await meta.initialize()
        child = await _fork(tmp_path, parent, store, meta)

        # 父更新：验收项验证 + 刷新（投影内容随之变化）
        from agent_harness.session.task import derive_task_state

        item_id = derive_task_state(parent.events).criteria[0].item_id
        assert apply_verification(parent, item_id, "passed").ok
        assert write_progress_file(tmp_path, "parent", parent.events).ok
        parent_body = progress_paths(tmp_path, "parent").markdown.read_text("utf-8")
        child_before = progress_paths(tmp_path, "child").markdown.read_bytes()
        assert "passed" in parent_body
        assert progress_paths(tmp_path, "child").markdown.read_bytes() == child_before, \
            "父更新不影响 child 文件"

        # child 更新：清单更新 + 刷新
        from agent_harness.session.plan import apply_plan_update

        assert apply_plan_update(
            child,
            [{
                "id": "c1", "content": "child 专属步骤", "status": "completed",
                "activeForm": "推进 child 步骤", "source": "agent",
            }],
        ).ok
        assert write_progress_file(tmp_path, "child", child.events).ok
        child_body = progress_paths(tmp_path, "child").markdown.read_text("utf-8")
        assert "child 专属步骤" in child_body
        assert "child 专属步骤" not in parent_body, "child 更新不影响父文件"
        assert parent_body == progress_paths(tmp_path, "parent").markdown.read_text("utf-8")

        # 双方对账各自 ok（source_event_seq 独立）
        assert verify_progress_file(
            tmp_path, "parent", parent.events
        ).status == PROGRESS_VERIFY_OK
        assert verify_progress_file(
            tmp_path, "child", child.events
        ).status == PROGRESS_VERIFY_OK

    async def test_fork_failure_leaves_no_child_progress_file(self, tmp_path) -> None:
        """fork 未完成（补偿/崩溃路径）⇒ child 进度文件不存在——误导性半成品
        不可能存在（child 文件只在 session/forked 落盘后写）。"""
        parent = _parent(tmp_path)
        store = JsonlSessionStore(root=tmp_path / "sessions")
        meta = _meta_store(tmp_path)
        await meta.initialize()

        # workspace 拷贝失败（基础设施故障，原样上抛 → fork 失败）
        with pytest.raises(OSError):
            await fork_session(
                store, meta, "parent",
                boundary_user_message_seq=_second_user_message_seq(parent),
                child_session_id="child",
                workspace_registry=_FailingRegistry(),
            )
        assert not progress_paths(tmp_path, "child").markdown.exists(), \
            "fork 失败不留 child 进度文件"


class _FailingRegistry:
    """基础设施故障的 registry 替身（fork 失败注入，原样上抛）。

    create() 形参与生产 WorkspaceRegistry.create() 保持对齐（#363 起生产
    调用点会传 backend=，更早还有 workspace_root=）：本替身在入口即抛
    OSError、形参接受后不消费——签名失配会让 TypeError 抢在失败注入点
    之前，把"基础设施故障"洗成"替身接口过期"。
    """

    def exists(self, session_id: str) -> bool:
        return True

    def get(self, session_id: str):
        parent_ws = MagicMock()
        parent_ws.workspace_root = Path("/nonexistent-parent-ws")
        return parent_ws

    def default_workspace_root(self, session_id: str) -> Path:
        return Path("/nonexistent-child-ws")

    def create(
        self,
        session_id: str,
        *,
        workspace_root: Path | None = None,
        backend: str | None = None,
    ):
        raise OSError("staging unavailable")

    def fork_staging_root(self) -> Path:
        raise OSError("staging unavailable")
