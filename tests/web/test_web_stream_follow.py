"""#1014（A-opt）：`GET /api/sessions/{id}/stream?follow=1` opt-in 会话级订阅。

默认（`follow` 缺省 / 0）行为**逐字不变**（见 `test_web_stream.py` 既有用例）；
`follow=1` 时：重放 `(after_seq, latest_seq]` 后**不收流**，持续推送该会话的后续事件
（含**另一客户端**起的 run 的帧），跨 run 边界保持，直到客户端断连 / `stream/truncated`。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Self

import pytest
from langchain_core.messages import AIMessage


class FastModel:
    """一轮即收尾：run/started → （空文本）→ run/completed。"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        yield AIMessage(content="done")


async def _start_server(tmp_path, monkeypatch, model=FastModel):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model", lambda config, **kw: model())
    app = create_app(Settings(
        _env_file=None, workspace_dir=str(tmp_path),
        model_api_key="sk-test", enable_cors=False,
    ))
    uv_config = uvicorn.Config(app, host="127.0.0.1", port=0,
                               log_level="error", lifespan="on")
    server = uvicorn.Server(uv_config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, serve_task, port, app


async def _shutdown(server, serve_task) -> None:
    server.should_exit = True
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass


def _parse_frame(line: str) -> dict:
    return json.loads(line.removeprefix("data:").strip())


async def _collect(port: int, path: str, body: dict | None,
                   stop_types: set[str] | None = None,
                   max_frames: int | None = None) -> list[dict]:
    import httpx2

    frames: list[dict] = []
    client = httpx2.AsyncClient(timeout=None)
    try:
        if body is not None:
            context = client.stream("POST", f"http://127.0.0.1:{port}{path}", json=body)
        else:
            context = client.stream("GET", f"http://127.0.0.1:{port}{path}")
        async with context as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                frame = _parse_frame(line)
                frames.append(frame)
                if (
                    (stop_types and frame.get("type") in stop_types)
                    or (max_frames is not None and len(frames) >= max_frames)
                ):
                    break
    finally:
        await client.aclose()
    return frames


class _Follow:
    """后台持有的 follow=1 连接：持续收集帧，能判断"是否仍打开"。"""

    def __init__(self, port: int, path: str) -> None:
        self.port = port
        self.path = path
        self.frames: list[dict] = []
        self.status: int | None = None
        self.closed = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        import httpx2

        client = httpx2.AsyncClient(timeout=None)
        try:
            async with client.stream(
                "GET", f"http://127.0.0.1:{self.port}{self.path}"
            ) as response:
                self.status = response.status_code
                if response.status_code != 200:
                    return
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        self.frames.append(_parse_frame(line))
        finally:
            await client.aclose()
            self.closed.set()

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._run())
        for _ in range(200):
            if self.status is not None or self.closed.is_set():
                break
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def types(self) -> list[str]:
        return [f.get("type") for f in self.frames]

    async def wait_for(self, pred: Callable[[list[dict]], bool],
                       timeout: float = 10.0) -> None:
        async with asyncio.timeout(timeout):
            while not pred(self.frames):
                if self.closed.is_set():
                    # 已收流且条件仍未满足（`while` 条件刚刚重查过）⇒ 失败。
                    raise AssertionError(f"流已收流但条件未满足：{self.types()}")
                await asyncio.sleep(0.02)


async def _create_idle_session(port: int) -> str:
    import httpx2

    async with httpx2.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/api/sessions?launch=false",
            json={},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


async def _run_once(port: int, session_id: str, text: str = "hi") -> None:
    await _collect(
        port, f"/api/sessions/{session_id}/messages",
        {"content": text, "mode": "queue"},
        stop_types={"run/completed", "run/failed"},
    )


@pytest.mark.asyncio
async def test_follow_unknown_session_404(tmp_path, monkeypatch):
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    import httpx2

    try:
        async with httpx2.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/api/sessions/no-such/stream"
                "?after_seq=-1&follow=1")
        assert resp.status_code == 404
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_default_stream_still_closes_on_idle(tmp_path, monkeypatch):
    """默认（不带 follow）行为逐字不变：idle 会话重放后**收流**。"""
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_idle_session(port)
        frames = await _collect(
            port, f"/api/sessions/{session_id}/stream?after_seq=-1", None)
        assert [f.get("type") for f in frames]  # 至少重放到 session/started
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_follow_idle_stays_open_and_receives_later_run(tmp_path, monkeypatch):
    """核心（#854）：follow=1 的 idle 连接**不收流**，另一客户端起的 run 帧实时到达。"""
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_idle_session(port)
        async with _Follow(
            port, f"/api/sessions/{session_id}/stream?after_seq=-1&follow=1"
        ) as follower:
            assert follower.status == 200
            await follower.wait_for(lambda fs: len(fs) >= 1)  # 重放 session/started
            # idle 静默期：连接保持（这正是要消灭的每秒建/收空转的反面）。
            await asyncio.sleep(0.5)
            assert not follower.closed.is_set(), "follow=1 idle 连接不得收流"

            # 另一客户端起 run。
            await _run_once(port, session_id, "外部 run")

            await follower.wait_for(
                lambda fs: "run/completed" in [f.get("type") for f in fs], timeout=10)
            types = follower.types()
            assert "run/started" in types, types
            assert "run/completed" in types, types

            # 跨 run 边界：run 收尾后连接仍保持。
            await asyncio.sleep(0.3)
            assert not follower.closed.is_set(), "run 收尾不得关掉 follow 连接"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_follow_replay_then_live_no_duplicates(tmp_path, monkeypatch):
    """重放 + live 无缝无重复（seq 幂等）。"""
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_idle_session(port)
        await _run_once(port, session_id, "第一轮")

        # 从会话起点重连：应重放第一轮全部事件，再接上第二轮 live。
        async with _Follow(
            port, f"/api/sessions/{session_id}/stream?after_seq=-1&follow=1"
        ) as follower:
            await follower.wait_for(
                lambda fs: "run/completed" in [f.get("type") for f in fs], timeout=10)
            replayed = [f for f in follower.frames if f.get("seq") is not None]

            await _run_once(port, session_id, "第二轮")
            await follower.wait_for(
                lambda fs: sum(1 for f in fs if f.get("type") == "run/completed") >= 2,
                timeout=10)

            seqs = [f["seq"] for f in follower.frames if f.get("seq") is not None]
            assert seqs == sorted(seqs), "帧按 seq 序"
            assert len(seqs) == len(set(seqs)), "无重复 seq（重放与 live 不重）"
            assert replayed, "应重放第一轮事件"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_follow_truncated_branch_emits_control_then_closes(tmp_path, monkeypatch):
    """backlog 超限：单帧 stream/truncated 控制帧后收流（与默认同判据）。"""
    import agent_harness.web.app as app_module

    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_idle_session(port)
        await _run_once(port, session_id, "制造若干事件")

        monkeypatch.setattr(app_module, "STREAM_REPLAY_MAX_EVENTS", 0)
        frames = await _collect(
            port, f"/api/sessions/{session_id}/stream?after_seq=-1&follow=1", None)
        assert [f.get("type") for f in frames] == ["stream/truncated"], frames
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_follow_leaves_no_subscriber_after_disconnect(tmp_path, monkeypatch):
    """客户端断连 → 会话级订阅者从注册表移除（无泄漏）。"""
    server, serve_task, port, app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_idle_session(port)
        manager = app.state.agent.run_manager
        async with _Follow(
            port, f"/api/sessions/{session_id}/stream?after_seq=-1&follow=1"
        ) as follower:
            await follower.wait_for(lambda fs: len(fs) >= 1)
            assert session_id in manager._session_followers, "连接期间应在注册表"
        # 退出上下文 = 断开客户端。
        for _ in range(200):
            if session_id not in manager._session_followers:
                break
            await asyncio.sleep(0.02)
        assert session_id not in manager._session_followers, "断连后应移除订阅者"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_default_stream_never_registers_session_follower(tmp_path, monkeypatch):
    """默认连接**不**碰会话级注册表（旧客户端零影响）。"""
    server, serve_task, port, app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_idle_session(port)
        manager = app.state.agent.run_manager
        await _collect(
            port, f"/api/sessions/{session_id}/stream?after_seq=-1", None)
        assert session_id not in manager._session_followers
        assert manager._session_followers == {}
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_follow_closes_and_unsubscribes_on_resync(tmp_path, monkeypatch):
    """慢客户端队列溢出 ⇒ 端点收到 RESYNC 哨兵即**收流并退订**（#1014）。

    真实灌满 2000 帧的溢出构造成本高且慢；这里直接向端点的会话级订阅者队列注入
    `RunManager.RESYNC`（正是 `_fanout_session` 溢出时投的产物），验证端点**响应**：
    连接收束 + 订阅者从注册表移除。溢出→哨兵那一半由
    `tests/session/test_runmanager_session_follow.py::test_queue_overflow_emits_resync` 覆盖。
    """
    server, serve_task, port, app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_idle_session(port)
        manager = app.state.agent.run_manager
        async with _Follow(
            port, f"/api/sessions/{session_id}/stream?after_seq=-1&follow=1"
        ) as follower:
            await follower.wait_for(lambda fs: len(fs) >= 1)
            for _ in range(200):
                if session_id in manager._session_followers:
                    break
                await asyncio.sleep(0.01)
            assert session_id in manager._session_followers

            manager._session_followers[session_id][0].queue.put_nowait(
                manager.RESYNC)

            for _ in range(200):
                if follower.closed.is_set():
                    break
                await asyncio.sleep(0.02)
            assert follower.closed.is_set(), "RESYNC 应令端点收流"

        for _ in range(200):
            if session_id not in manager._session_followers:
                break
            await asyncio.sleep(0.02)
        assert session_id not in manager._session_followers, "收流后应退订"
    finally:
        await _shutdown(server, serve_task)
