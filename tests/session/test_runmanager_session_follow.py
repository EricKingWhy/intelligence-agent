"""#1014（A-opt）：RunManager 会话级订阅者（`?follow=1` 的服务端原语）。

验收映射（issue #1014 / ADR-0016 §2.1 扩展）：
- 会话级订阅者（`subscribe_session`）跨 run 存活，收到该会话上 run 的全部 durable 事实；
- 会话级订阅者**永不**收 run 订阅者的 `DONE` 哨兵（run 收尾 = 一段安静期，连接保持）；
- `unsubscribe_session` 后不再投递；
- 无订阅者时行为逐字不变（既有 run 订阅者路径不受影响）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.session import RUN_COMPLETED, RUN_STARTED, Session
from agent_harness.session.runmanager import RunManager
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.scripted_model import ScriptedModel


def _make_session(tmp_path: Path, session_id: str = "s-follow") -> Session:
    return Session.start(
        JsonlSessionStore(root=tmp_path / "sessions"), session_id=session_id,
    )


def _runtime(model) -> AgentRuntime:
    registry = ToolRegistry()
    return AgentRuntime(
        model=model, registry=registry,
        executor=ToolExecutor(registry), max_agent_turns=5,
    )


async def _drain(queue: asyncio.Queue) -> list[AgentEvent]:
    items: list[AgentEvent] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


class TestSessionFollower:
    @pytest.mark.asyncio
    async def test_follower_receives_run_events(self, tmp_path):
        """会话级订阅者收到该会话 run 的 durable 事实（含 run/started、run/completed）。"""
        manager = RunManager(disconnect_grace_seconds=60.0)
        session = _make_session(tmp_path)
        follower = manager.subscribe_session(session.session_id)
        runtime = _runtime(ScriptedModel([AIMessage(content="done")]))
        run, _sub = manager.launch(session, runtime, "hi")
        await asyncio.wait_for(run.task, timeout=10)

        types = [e.type for e in await _drain(follower.queue)]
        assert RUN_STARTED in types, f"应收到 run/started，实收 {types}"
        assert RUN_COMPLETED in types, f"应收到 run/completed，实收 {types}"

    @pytest.mark.asyncio
    async def test_follower_never_gets_done_sentinel(self, tmp_path):
        """run 收尾**不**向会话级订阅者投 DONE（连接跨 run 保持）。"""
        manager = RunManager(disconnect_grace_seconds=60.0)
        session = _make_session(tmp_path)
        follower = manager.subscribe_session(session.session_id)
        runtime = _runtime(ScriptedModel([AIMessage(content="done")]))
        run, _sub = manager.launch(session, runtime, "hi")
        await asyncio.wait_for(run.task, timeout=10)

        items = await _drain(follower.queue)
        assert manager.DONE not in items, "会话级订阅者不得收到 DONE 哨兵"

    @pytest.mark.asyncio
    async def test_follower_survives_run_boundary(self, tmp_path):
        """一轮 run 收尾后，同一会话的下一轮 run 的帧仍到达**同一个**订阅者。"""
        manager = RunManager(disconnect_grace_seconds=60.0)
        session = _make_session(tmp_path)
        follower = manager.subscribe_session(session.session_id)

        run1, _s1 = manager.launch(
            session, _runtime(ScriptedModel([AIMessage(content="one")])), "one")
        await asyncio.wait_for(run1.task, timeout=10)
        first = [e.type for e in await _drain(follower.queue)]
        assert RUN_COMPLETED in first

        run2, _s2 = manager.launch(
            session, _runtime(ScriptedModel([AIMessage(content="two")])), "two")
        await asyncio.wait_for(run2.task, timeout=10)
        second = [e.type for e in await _drain(follower.queue)]
        assert RUN_STARTED in second and RUN_COMPLETED in second, \
            f"第二轮 run 的帧应到达同一会话级订阅者，实收 {second}"

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self, tmp_path):
        """注销后不再投递。"""
        manager = RunManager(disconnect_grace_seconds=60.0)
        session = _make_session(tmp_path)
        follower = manager.subscribe_session(session.session_id)
        manager.unsubscribe_session(session.session_id, follower)

        runtime = _runtime(ScriptedModel([AIMessage(content="done")]))
        run, _sub = manager.launch(session, runtime, "hi")
        await asyncio.wait_for(run.task, timeout=10)

        assert follower.queue.empty(), "注销后不得再收到事件"

    def test_queue_overflow_emits_resync(self, monkeypatch):
        """慢客户端队列溢出 ⇒ 作废队列并投 RESYNC 哨兵（丢帧不可自愈，端点据此收流）。"""
        import agent_harness.session.runmanager as rm

        monkeypatch.setattr(rm, "SUBSCRIBER_QUEUE_MAX", 2)
        manager = RunManager(disconnect_grace_seconds=60.0)
        follower = manager.subscribe_session("s")
        for seq in range(3):
            manager._fanout_session(
                "s", AgentEvent(type="text/delta", data={}, seq=seq))
        assert follower.queue.qsize() == 1
        assert follower.queue.get_nowait() is manager.RESYNC

    @pytest.mark.asyncio
    async def test_no_followers_keeps_run_subscriber_path_intact(self, tmp_path):
        """回归：无会话级订阅者时，既有 run 订阅者照常收帧（默认行为不变）。"""
        manager = RunManager(disconnect_grace_seconds=60.0)
        session = _make_session(tmp_path)
        runtime = _runtime(ScriptedModel([AIMessage(content="done")]))
        run, sub = manager.launch(session, runtime, "hi")
        await asyncio.wait_for(run.task, timeout=10)

        items = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        types = [e.type for e in items if isinstance(e, AgentEvent)]
        assert RUN_COMPLETED in types
        assert manager.DONE in items, "run 订阅者仍应收 DONE"
