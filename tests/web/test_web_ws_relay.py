"""Q4d spec tests: WS live event relay + client-initiated heartbeat。

覆盖（docs/TECH_DEBT_FIX_SPEC.md §Q4d + code-review P1/P2/P3/P4）：
  - P2 live event relay：WS ``send_message`` 触发续聊 → 收到增量 event 帧，
    帧结构完整（信封含 type/data/seq/session_id）。
  - P4 heartbeat：client-initiated ping → server pong 应用层心跳。
  - P1 multisession：单 WS 订阅两个真实 session → 各自收到 snapshot。
  - P3 disconnect cleanup：WS 断开后 RunManager 订阅者归零。

测试接缝：真实 uvicorn ASGI 服务器 + httpx2 WebSocket 客户端
+ ScriptedModel 测试桩（astream 提供确定性 AIMessage 流）。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from tests.scripted_model import ScriptedModel


class _OneTurnModel(ScriptedModel):
    """每次新 run 都吐一条最终 AIMessage（ScriptedModel 语义：剧本耗尽即报错）。

    create_chat_model 每轮 build_runtime 都会被调用、新建实例，因此首轮与
    续聊轮各自从第一条剧本开始，单条剧本足够。
    """

    def __init__(self) -> None:
        super().__init__([AIMessage(content="task done")])


class _SlowStreamModel:
    """流式吐 N 个 chunk（每 chunk 间隔 sleep）：让 run 在途保持足够久。

    供「订阅在途 run / 断开后订阅者清理」类测试使用——run 没在几毫秒内
    终结，WS 有充足窗口建订阅 / 触发断开清理。
    """

    def __init__(self, chunks: int = 10, interval: float = 0.1) -> None:
        self._chunks = chunks
        self._interval = interval

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        for i in range(self._chunks):
            await asyncio.sleep(self._interval)
            yield AIMessageChunk(content=f"chunk{i} ")


async def _start_server(tmp_path, monkeypatch, model_factory=None):
    """启动真实 uvicorn 服务器，返回 (server, serve_task, port, app)。

    app 供测试直接检查 RunManager 内部状态（subscribers 清理验证）。
    """
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    if model_factory is None:
        model_factory = lambda config, **kw: _OneTurnModel()
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model", model_factory
    )
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


async def _create_session(port: int, task: str) -> str:
    """POST /api/sessions → 消费 SSE 到 run/completed → 返回 session_id。"""
    import httpx2

    frames: list[dict] = []
    async with httpx2.AsyncClient(timeout=None) as client, client.stream(
        "POST", f"http://127.0.0.1:{port}/api/sessions",
        json={"task": task},
    ) as response:
        assert response.status_code == 200, \
                f"期望 200，实际 {response.status_code}"
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            frame = json.loads(line.removeprefix("data:").strip())
            frames.append(frame)
            if frame.get("type") == "run/completed":
                break
    assert frames, "创建会话必须收到 SSE 帧"
    return frames[0]["session_id"]


async def _recv_until(ws, predicate, timeout: float = 8.0) -> list[dict]:
    """持续收 WS 文本帧直到 predicate(累计帧列表) 为真或超时。"""
    frames: list[dict] = []

    async def _drain() -> None:
        while True:
            raw = await ws.receive_text()
            frames.append(json.loads(raw))
            if predicate(frames):
                return

    try:
        await asyncio.wait_for(_drain(), timeout=timeout)
    except TimeoutError:
        pass
    return frames


@pytest.mark.asyncio
async def test_ws_live_event_relay(tmp_path, monkeypatch):
    """P2 live event relay：WS send_message 触发续聊 → 增量事件经 WS 推送。

    不通过 SSE 中转：全程走 /api/ws。subscribe 拿到 snapshot 后，用 WS 的
    send_message 拉新一轮 run——WS 层自动订阅并把增量 event 帧推到客户端。
    """
    server, serve_task, port, _app = await _start_server(
        tmp_path, monkeypatch,
        model_factory=lambda config, **kw: _SlowStreamModel(chunks=8, interval=0.05),
    )
    try:
        # 先建一个已完成的 session（idle 可续聊）
        session_id = await _create_session(port, "首轮任务")

        import httpx2

        async with httpx2.AsyncClient(timeout=15) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            # subscribe → snapshot
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": session_id,
            }))
            snap = await _recv_until(
                ws, lambda fs: any(f.get("type") == "snapshot" for f in fs))
            snap_frame = next(f for f in snap if f.get("type") == "snapshot")
            assert snap_frame["session_id"] == session_id
            assert "events" in snap_frame
            assert "replay_upto" in snap_frame

            # WS send_message 续聊 → launched 确认 + 增量 event 帧
            await ws.send_text(json.dumps({
                "type": "send_message",
                "session_id": session_id,
                "content": "继续",
                "mode": "queue",
            }))

            def _has_launched_then_events(fs: list[dict]) -> bool:
                types = [f.get("type") for f in fs]
                if "launched" not in types:
                    return False
                # launched 之后的 event 帧（非 snapshot）
                launched_at = types.index("launched")
                events_after = [f for f in fs[launched_at + 1:]
                                if f.get("type") == "event"]
                return len(events_after) >= 1

            frames = await _recv_until(ws, _has_launched_then_events)
            types = [f.get("type") for f in frames]
            assert "launched" in types, f"缺少 launched 帧：{types}"
            launched_at = types.index("launched")
            event_frames = [f for f in frames[launched_at + 1:]
                            if f.get("type") == "event"]
            assert event_frames, "WS 必须收到至少一个增量 event 帧"
            for ef in event_frames:
                assert ef["session_id"] == session_id
                inner = ef["event"]
                for key in ("type", "data", "seq", "session_id"):
                    assert key in inner, f"event 信封缺少 {key}: {inner}"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ws_client_ping_pong(tmp_path, monkeypatch):
    """P4 heartbeat：client-initiated ping → server pong 应用层心跳。

    应用层心跳（非 WebSocket PING control frame）：客户端周期性发
    ``{"type": "ping"}`` 探测连接活性，服务端回 ``{"type": "pong"}``。
    """
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        import httpx2

        async with httpx2.AsyncClient(timeout=5) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            await ws.send_text(json.dumps({"type": "ping"}))
            frames = await _recv_until(
                ws, lambda fs: any(f.get("type") == "pong" for f in fs),
                timeout=3.0)
            assert any(f.get("type") == "pong" for f in frames), \
                "期望 pong 帧，未收到"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ws_multisession_real_snapshots(tmp_path, monkeypatch):
    """P1 multisession：单 WS 订阅两个真实 session → 各自收到 snapshot。

    修复前测试只订阅不存在的 session（永远走 error 分支），从未验证
    多 session 的真实事件投递。此处建两个真实 session 后订阅，验证
    快照按 session_id 正确路由、互不串台。
    """
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        sid_a = await _create_session(port, "任务 A")
        sid_b = await _create_session(port, "任务 B")
        assert sid_a != sid_b

        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            # 依次订阅两个真实 session
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": sid_a}))
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": sid_b}))

            def _got_both(fs: list[dict]) -> bool:
                snaps = [f for f in fs if f.get("type") == "snapshot"]
                got = {s["session_id"] for s in snaps}
                return got >= {sid_a, sid_b}

            frames = await _recv_until(ws, _got_both)
            snaps = {f["session_id"]: f for f in frames
                     if f.get("type") == "snapshot"}
            assert sid_a in snaps and sid_b in snaps, \
                f"应收到两个真实 session 的 snapshot，实际 {list(snaps)}"
            for sid, snap in snaps.items():
                assert snap["session_id"] == sid
                assert "events" in snap
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ws_disconnect_cleans_subscriber(tmp_path, monkeypatch):
    """P3 disconnect cleanup：WS 断开后 RunManager 订阅者归零。

    修复前 disconnect 测试只发 ping（从未真正订阅），从不验证服务端
    subscription 清理。此处用慢流模型保持 run 在途，WS subscribe 到
    active run（→ 创建 subscriber），断开后轮询 RunManager，确认
    ManagedRun.subscribers 最终为空。
    """
    server, serve_task, port, app = await _start_server(
        tmp_path, monkeypatch,
        # 2s 慢流：subscribe → disconnect 窗口内 run 始终在途
        model_factory=lambda config, **kw: _SlowStreamModel(chunks=20, interval=0.1),
    )
    try:
        import httpx2

        # 发起一轮 run（HTTP SSE 流式收集，但不等到 run/completed——
        # 慢流 2s，我们 0.3s 内就断开连接，run 仍在途）
        async with httpx2.AsyncClient(timeout=10) as http:
            stream_ctx = http.stream(
                "POST", f"http://127.0.0.1:{port}/api/sessions",
                json={"task": "慢任务"},
            )
            stream = await stream_ctx.__aenter__()
            assert stream.status_code == 200
            # 读首帧拿 session_id 即可（后续帧丢弃）
            session_id = None
            try:
                async for line in stream.aiter_lines():
                    if line.startswith("data:"):
                        first = json.loads(line.removeprefix("data:").strip())
                        session_id = first.get("session_id")
                        break
            finally:
                await stream_ctx.__aexit__(None, None, None)
            assert session_id

            # WS subscribe 到在途 run → 服务端订阅（ManagedRun.subscribers=1）
            async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
                f"ws://127.0.0.1:{port}/api/ws"
            ) as ws:
                await ws.send_text(json.dumps({
                    "type": "subscribe", "session_id": session_id}))
                frames = await _recv_until(
                    ws, lambda fs: any(f.get("type") == "snapshot" for f in fs))
                assert any(f.get("type") == "snapshot" for f in frames)

                run = app.state.agent.run_manager.get_active(session_id)
                assert run is not None and run.subscribers, \
                    "subscribe active run 后应有订阅者"
                assert len(run.subscribers) >= 1

            # WS 已断开：服务端 finally 清理订阅 → subscribers 归零
            async def _subscribers_empty() -> bool:
                for _ in range(50):
                    run = app.state.agent.run_manager.get_active(session_id)
                    if run is None or not run.subscribers:
                        return True
                    await asyncio.sleep(0.05)
                return False

            assert await _subscribers_empty(), \
                "WS 断开后 ManagedRun.subscribers 应归零"
    finally:
        await _shutdown(server, serve_task)
