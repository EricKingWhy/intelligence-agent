"""MessageQueueManager 测试（T2 / #132）。

测试续聊排队与 steer 注册的内存状态管理。
"""

from __future__ import annotations

import pytest

from agent_harness.session.queue import MessageQueueManager, SteerRequest


@pytest.fixture
def manager() -> MessageQueueManager:
    return MessageQueueManager()


class TestEnqueueAndTake:
    @pytest.mark.asyncio
    async def test_enqueue_and_take_by_id(self, manager):
        """按 id 摘除镜像项；不存在 / 已摘除 → None。"""
        msg1 = await manager.enqueue(
            session_id="s1", content="hello", created_at="2026-01-01T00:00:00",
        )
        msg2 = await manager.enqueue(
            session_id="s1", content="world", created_at="2026-01-01T00:00:01",
        )
        assert msg1.queue_id != msg2.queue_id

        taken = await manager.take_queue_item(session_id="s1", queue_id=msg1.queue_id)
        assert taken is not None and taken.content == "hello"
        # 幂等：同 id 再摘 → None（事实已由 queue/consumed 表达，镜像里不再有）
        assert await manager.take_queue_item(session_id="s1", queue_id=msg1.queue_id) is None

        remaining = await manager.list_pending("s1")
        assert [m.content for m in remaining] == ["world"]

        assert await manager.take_queue_item(session_id="s1", queue_id=msg2.queue_id) is not None
        assert await manager.list_pending("s1") == []

    @pytest.mark.asyncio
    async def test_take_unknown_session_or_id_returns_none(self, manager):
        """未知 session / 未知 id → None（不抛错）。"""
        assert await manager.take_queue_item(session_id="nope", queue_id="q") is None
        await manager.enqueue(session_id="s1", content="a", created_at="t")
        assert await manager.take_queue_item(session_id="s1", queue_id="fake") is None

    @pytest.mark.asyncio
    async def test_pending_count(self, manager):
        """pending_count 返回未取消的数量。"""
        assert await manager.pending_count("s1") == 0
        await manager.enqueue(session_id="s1", content="a", created_at="t")
        await manager.enqueue(session_id="s1", content="b", created_at="t")
        assert await manager.pending_count("s1") == 2


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_marks_message(self, manager):
        """cancel 标记消息为已取消（保留在列表里，事实由 queue/cancelled 事件表达）。"""
        msg1 = await manager.enqueue(session_id="s1", content="first", created_at="t")
        await manager.enqueue(session_id="s1", content="second", created_at="t")

        cancelled = await manager.cancel(session_id="s1", queue_id=msg1.queue_id)
        assert cancelled is True

        # 已取消项不再是待发送项；未取消项仍在
        pending = await manager.list_pending("s1")
        assert [m.content for m in pending] == ["second"]
        assert await manager.pending_count("s1") == 1

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_false(self, manager):
        """取消不存在的 queue_id → False。"""
        assert await manager.cancel(session_id="s1", queue_id="fake-id") is False

    @pytest.mark.asyncio
    async def test_cancel_already_cancelled_returns_false(self, manager):
        """重复取消 → 第二次返回 False。"""
        msg = await manager.enqueue(session_id="s1", content="x", created_at="t")
        assert await manager.cancel(session_id="s1", queue_id=msg.queue_id) is True
        assert await manager.cancel(session_id="s1", queue_id=msg.queue_id) is False


class TestListPending:
    @pytest.mark.asyncio
    async def test_list_pending_excludes_cancelled(self, manager):
        """list_pending 不包含已取消的。"""
        msg1 = await manager.enqueue(session_id="s1", content="a", created_at="t")
        await manager.enqueue(session_id="s1", content="b", created_at="t")
        await manager.cancel(session_id="s1", queue_id=msg1.queue_id)

        pending = await manager.list_pending("s1")
        assert len(pending) == 1
        assert pending[0].content == "b"


class TestSteer:
    @pytest.mark.asyncio
    async def test_register_and_drain_steer(self, manager):
        """steer 注册后 drain 取出并清除。"""
        req = await manager.register_steer(
            session_id="s1", content="steer this", run_id="run-1", created_at="t",
        )
        assert isinstance(req, SteerRequest)
        assert req.content == "steer this"

        steers = await manager.drain_steers("s1")
        assert len(steers) == 1
        assert steers[0].steer_id == req.steer_id

        # drain 后清空
        assert await manager.drain_steers("s1") == []

    @pytest.mark.asyncio
    async def test_drain_steers_empty(self, manager):
        """无 steer 请求时 drain 返回空列表。"""
        assert await manager.drain_steers("s1") == []

    @pytest.mark.asyncio
    async def test_take_steer_by_id(self, manager):
        """按 id 摘除单条 steer（终态驱动把它降级成新 run 后同步镜像）。"""
        req = await manager.register_steer(
            session_id="s1", content="stale", run_id=None, created_at="t",
        )
        taken = await manager.take_steer(session_id="s1", steer_id=req.steer_id)
        assert taken is not None and taken.content == "stale"
        assert await manager.drain_steers("s1") == []


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_replaces_mirror_idempotently(self, manager):
        """启动重建：替换而非追加，重复执行结果一致。"""
        from agent_harness.session.queue import QueuedMessage

        item = QueuedMessage(
            queue_id="q1", content="pending", session_id="s1", created_at="t0",
        )
        steer = SteerRequest(
            steer_id="st1", content="go", session_id="s1", run_id="r1", created_at="t1",
        )
        for _ in range(2):
            await manager.restore(session_id="s1", queue_items=[item], steers=[steer])
        assert [m.queue_id for m in await manager.list_pending("s1")] == ["q1"]
        assert [s.steer_id for s in await manager.drain_steers("s1")] == ["st1"]

    @pytest.mark.asyncio
    async def test_restore_empty_clears_session(self, manager):
        """空集合 ⇒ 清空该 session 的镜像（事实流说没有待投递项）。"""
        await manager.enqueue(session_id="s1", content="a", created_at="t")
        await manager.register_steer(session_id="s1", content="s", run_id="r", created_at="t")
        await manager.restore(session_id="s1", queue_items=[], steers=[])
        assert await manager.pending_count("s1") == 0
        assert await manager.drain_steers("s1") == []


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_all(self, manager):
        """cleanup 清除 session 的所有队列和 steer 状态。"""
        await manager.enqueue(session_id="s1", content="a", created_at="t")
        await manager.register_steer(session_id="s1", content="s", run_id="r", created_at="t")
        await manager.cleanup("s1")

        assert await manager.pending_count("s1") == 0
        assert await manager.drain_steers("s1") == []
        assert await manager.list_pending("s1") == []
