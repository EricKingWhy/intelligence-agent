"""T4（ADR-0016 §2.1）：Session listener + RunManager detached-run 契约。

验收映射（规格 01 §22 场景 E）：断连不杀 run、订阅者实时收到 durable 事实
（含工具输出 delta）、孤儿宽限期回收、幂等合并（listener 与镜像 yield 不重复）。
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.session import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_STARTED,
    Session,
)
from agent_harness.session.runmanager import RunManager
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.scripted_model import ScriptedModel


def _make_session(tmp_path: Path) -> Session:
    return Session.start(JsonlSessionStore(root=tmp_path / "sessions"))


def _runtime(model, tools=()) -> AgentRuntime:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentRuntime(
        model=model, registry=registry,
        executor=ToolExecutor(registry), max_agent_turns=5,
    )


class TestSessionListener:
    @pytest.mark.asyncio
    async def test_listener_receives_appended_events(self, tmp_path):
        session = _make_session(tmp_path)
        seen: list = []
        session.add_listener(seen.append)
        session.append(RUN_STARTED, {}, run_id="r1")
        assert [e.type for e in seen] == [RUN_STARTED]
        session.remove_listener(seen.append)
        session.append(RUN_COMPLETED, {}, run_id="r1")
        assert len(seen) == 1, "移除后不再收到"

    @pytest.mark.asyncio
    async def test_listener_error_does_not_break_append(self, tmp_path):
        session = _make_session(tmp_path)

        def boom(_event):
            raise RuntimeError("listener fault")

        session.add_listener(boom)
        event = session.append(RUN_STARTED, {}, run_id="r1")
        assert event.seq >= 0, "listener 异常不得破坏 append 契约"
        assert session.events[-1].type == RUN_STARTED


class TestRunManager:
    @pytest.mark.asyncio
    async def test_disconnect_does_not_kill_run(self, tmp_path):
        """核心语义反转（D-A）：订阅者离开后 run 继续跑到终态。"""
        store = JsonlSessionStore(root=tmp_path / "sessions")
        session = _make_session(tmp_path)
        runtime = _runtime(ScriptedModel([AIMessage(content="done")]))
        manager = RunManager(disconnect_grace_seconds=60.0)
        try:
            run, subscriber = manager.launch(session, runtime, "hi")

            # 订阅者收首批事件后立即离开（模拟断连）
            first = await asyncio.wait_for(subscriber.queue.get(), timeout=5)
            assert first is not None
            run.unsubscribe(subscriber)

            # run 继续执行到终态（不因断连取消）
            await asyncio.wait_for(run.task, timeout=10)
            types = [e.type for e in store.read_events(session.session_id)]
            assert types[-1] == RUN_COMPLETED
        finally:
            await manager.aclose()

    @pytest.mark.asyncio
    async def test_subscriber_receives_events_without_duplicates(self, tmp_path):
        """durable 事实幂等合并：listener 捕获 + 镜像 yield 双通道不产生重复。"""
        session = _make_session(tmp_path)
        runtime = _runtime(ScriptedModel([AIMessage(content="hello")]))
        manager = RunManager(disconnect_grace_seconds=60.0)
        try:
            _run, subscriber = manager.launch(session, runtime, "hi")
            frames = []
            while True:
                item = await asyncio.wait_for(subscriber.queue.get(), timeout=5)
                if item is manager.DONE:
                    break
                frames.append(item)

            seqs = [f.seq for f in frames if f.seq is not None]
            assert len(seqs) == len(set(seqs)), "durable 事件不得重复入队"
            types = [f.type for f in frames]
            assert "user/message" in types and RUN_COMPLETED in types
            assert types[-1] == RUN_COMPLETED
        finally:
            await manager.aclose()

    @pytest.mark.asyncio
    async def test_orphan_run_reclaimed_after_grace(self, tmp_path, monkeypatch):
        """零订阅者超过宽限期 → run 被回收（取消臂收尾 run/failed）。"""
        class SlowModel:
            def bind_tools(self, tools, **kwargs):
                return self

            async def astream(self, messages, **kwargs):
                await asyncio.sleep(60)
                yield AIMessageChunk(content="never")

        from langchain_core.messages import AIMessageChunk

        store = JsonlSessionStore(root=tmp_path / "sessions")
        session = _make_session(tmp_path)
        runtime = _runtime(SlowModel())
        manager = RunManager(disconnect_grace_seconds=0.2)
        try:
            run, subscriber = manager.launch(session, runtime, "hi")
            await asyncio.sleep(0.05)
            run.unsubscribe(subscriber)  # 孤儿化

            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(run.task, timeout=5)
            types = [e.type for e in store.read_events(session.session_id)]
            assert types[-1] == "run/failed"
            assert any(e.type == "run/failed" and e.data.get("reason") == "orphaned"
                       for e in store.read_events(session.session_id))
        finally:
            await manager.aclose()

    @pytest.mark.asyncio
    async def test_cancel_marks_task(self, tmp_path):
        session = _make_session(tmp_path)
        runtime = _runtime(ScriptedModel([AIMessage(content="done")]))
        manager = RunManager(disconnect_grace_seconds=60.0)
        try:
            run, _subscriber = manager.launch(session, runtime, "hi")
            assert manager.cancel(session.session_id) is True
            assert manager.cancel("no-such-session") is False
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(run.task, timeout=5)
        finally:
            await manager.aclose()

    @pytest.mark.asyncio
    async def test_launch_rejected_when_closing(self, tmp_path):
        """#341：关停置位后 launch 拒绝开新 run（不起半个生命周期的 run）。"""
        manager = RunManager()
        await manager.aclose()
        session = _make_session(tmp_path)
        runtime = _runtime(ScriptedModel([AIMessage(content="done")]))
        with pytest.raises(RuntimeError, match="shutting down"):
            manager.launch(session, runtime, "hi")

    @pytest.mark.asyncio
    async def test_aclose_cancels_task_dropped_from_runs(self, tmp_path):
        """#341：`_runs` 条目被覆盖/丢失后，aclose 仍凭 `_tasks` 取消在途 task。

        同会话 resume 再 launch 会覆盖 `_runs[session_id]`，旧 task 的收尾
        可能还在跑——没有 `_tasks` 集合时 aclose 会漏取消它。"""
        from langchain_core.messages import AIMessageChunk

        class SlowModel:
            def bind_tools(self, tools, **kwargs):
                return self

            async def astream(self, messages, **kwargs):
                await asyncio.sleep(60)
                yield AIMessageChunk(content="never")

        session = _make_session(tmp_path)
        runtime = _runtime(SlowModel())
        manager = RunManager()
        run, _subscriber = manager.launch(session, runtime, "hi")
        await asyncio.sleep(0.05)
        manager._runs.clear()  # 旧条目不再可达（模拟 resume 覆盖）
        await manager.aclose()
        assert run.task is not None and run.task.done()
        assert run.task.cancelled()


# ── W-22（#366）：在场管理 run 的孤儿回收 = client_absent 暂停，不是取消 ──


class _GateArgs(BaseModel):
    text: str = "x"


class _GateTool(Tool):
    """在途 Tool 探针：execute 中段挂起，等测试放行（模拟"最后客户端退出时
    Tool 正在提交"）。执行次数进 `calls`——钉"不盲重试、恰好一次"。"""

    def __init__(self, released: asyncio.Event, calls: list[int]) -> None:
        self._released = released
        self._calls = calls

    @property
    def name(self) -> str:
        return "gate"

    @property
    def description(self) -> str:
        return "挂起直到测试放行。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _GateArgs

    async def execute(self, args: _GateArgs) -> ToolResult:
        self._calls.append(1)
        await self._released.wait()
        return ToolResult.success(message=args.text)


def _gate_round() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": "call_w22", "name": "gate", "args": {"text": "g"}}],
    )


class TestPresenceManagedRuns:
    """`02 §5.2.1` / `11 §6.2`：已纳入在场协议的 run，零订阅者超过宽限后
    **不**走「取消 ⇒ run/failed(orphaned)」，而是置缺席闸门，由 runtime 在
    下一个循环顶准入点以 run/paused(client_absent) 收口。未登记的 run（旧
    Web / CLI）维持既有孤儿回收语义（上方 `test_orphan_run_reclaimed_after_grace`
    钉住的那条，本类不改它）。"""

    @pytest.mark.asyncio
    async def test_launch_enrolls_gate_only_when_presence_managed(self, tmp_path):
        session = _make_session(tmp_path)
        runtime = _runtime(ScriptedModel([AIMessage(content="done")]))
        manager = RunManager(disconnect_grace_seconds=60.0)
        try:
            run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
            assert runtime.client_presence.managed is True
            assert run.presence_managed is True
        finally:
            await manager.aclose()

        # 未登记对照面：另起一个 manager（上一个已 aclose，关停后拒绝 launch）。
        session2 = _make_session(tmp_path)
        runtime2 = _runtime(ScriptedModel([AIMessage(content="done")]))
        manager2 = RunManager(disconnect_grace_seconds=60.0)
        try:
            _run2, _sub2 = manager2.launch(session2, runtime2, "hi")
            assert runtime2.client_presence.managed is False, "默认未登记：行为逐字不变"
        finally:
            await manager2.aclose()

    @pytest.mark.asyncio
    async def test_grace_expiry_pauses_client_absent_instead_of_orphan_cancel(
        self, tmp_path,
    ):
        """宽限到期时 Tool 正在提交：不取消（盲中止副作用）、不重试；工具照常
        收口后，run 以恰好一条 run/paused(client_absent) 停下，无 run/failed。"""
        store = JsonlSessionStore(root=tmp_path / "sessions")
        session = _make_session(tmp_path)
        released = asyncio.Event()
        calls: list[int] = []
        runtime = _runtime(
            ScriptedModel([_gate_round(), AIMessage(content="done")]),
            tools=[_GateTool(released, calls)],
        )
        manager = RunManager(disconnect_grace_seconds=0.2)
        try:
            run, subscriber = manager.launch(
                session, runtime, "hi", presence_managed=True,
            )
            for _ in range(500):
                if calls:
                    break
                await asyncio.sleep(0.01)
            assert calls == [1], "工具已开始执行（在途中）"

            run.unsubscribe(subscriber)  # 最后一个客户端离开 → 宽限计时
            await asyncio.sleep(0.4)     # 0.2s 宽限到期 → 置缺席（不是取消）
            assert run.task is not None and not run.task.done(), (
                "在途 Tool 未被取消：缺席只挡下一次准入，不中止进行中的提交"
            )
            assert not run.reap_requested, "在场管理 run 不走 orphaned 取消臂"

            released.set()               # 在途工具按现有路径收口
            await asyncio.wait_for(run.task, timeout=5)

            events = store.read_events(session.session_id)
            types = [e.type for e in events]
            assert types.count(RUN_PAUSED) == 1, "恰好一条 run/paused"
            assert RUN_FAILED not in types, "不得再走 run/failed(orphaned) 旧路径"
            paused = next(e for e in events if e.type == RUN_PAUSED)
            assert paused.data["reason"] == "client_absent"
            assert paused.data["trigger_dimension"] == "client_presence"
            assert calls == [1], "工具恰好执行一次（不盲重试）"
            assert not run.task.cancelled()
            assert run.paused is True
        finally:
            await manager.aclose()

    @pytest.mark.asyncio
    async def test_leave_after_pause_does_not_double_write(self, tmp_path):
        """「已经 paused 时又收到离开事件」：暂停后再订阅再离开（再触发一次
        回收计时），不得写第二条 run/paused，也不得翻成 run/failed——暂停
        run 的 task 已收口、runtime 引用已释放，回收分支必须无害跳过。"""
        store = JsonlSessionStore(root=tmp_path / "sessions")
        session = _make_session(tmp_path)
        released = asyncio.Event()
        calls: list[int] = []
        runtime = _runtime(
            ScriptedModel([_gate_round(), AIMessage(content="done")]),
            tools=[_GateTool(released, calls)],
        )
        manager = RunManager(disconnect_grace_seconds=0.2)
        try:
            run, subscriber = manager.launch(
                session, runtime, "hi", presence_managed=True,
            )
            for _ in range(500):
                if calls:
                    break
                await asyncio.sleep(0.01)
            run.unsubscribe(subscriber)
            await asyncio.sleep(0.4)
            released.set()
            await asyncio.wait_for(run.task, timeout=5)
            assert run.paused is True

            # 已经 paused 之后的"又一次离开"：新订阅者接入又离开 → 计时再起。
            late = run.subscribe()
            run.unsubscribe(late)
            await asyncio.sleep(0.4)

            events = store.read_events(session.session_id)
            types = [e.type for e in events]
            assert types.count(RUN_PAUSED) == 1, "不得双写 run/paused"
            assert RUN_FAILED not in types
        finally:
            await manager.aclose()
