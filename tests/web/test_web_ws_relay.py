"""Q4d spec tests: WS live event relay + server-initiated logical heartbeat。

覆盖（docs/TECH_DEBT_FIX_SPEC.md §Q4d + code-review P1/P2/P3/P4）：
  - Q4d.2 live event relay：subscribe 一个**有 active run** 的 session，
    验证增量事件通过 WS 推送（信封含 type/data/seq/session_id）。
  - Q4d.4 heartbeat：服务端每 ``WS_PING_INTERVAL`` 秒下行 ``server_ping``；
    客户端不应答则在 ``WS_PING_TIMEOUT`` 后关闭连接。
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


async def _start_run_get_session_id(port: int, task: str) -> str:
    """POST /api/sessions，读首帧拿 session_id 后立即断开。

    run 是 detached（ADR-0016）：HTTP 流断开只是 unsubscribe，run 继续在途。
    """
    import httpx2

    async with httpx2.AsyncClient(timeout=None) as client, client.stream(
        "POST", f"http://127.0.0.1:{port}/api/sessions", json={"task": task},
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            sid = json.loads(line.removeprefix("data:").strip()).get("session_id")
            if sid:
                return sid
    raise AssertionError("首帧未带 session_id")


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
    """Q4d.2 live event relay：subscribe 一个有 active run 的 session，
    验证增量事件通过 WS 推送。

    规格字面：订阅时必须已有在途 run（而非先订阅 idle session 再触发新 run）。
    run 由 HTTP 发起后立即断开连接（detached 仍在途），WS 订阅接上 live 流。
    """
    server, serve_task, port, _app = await _start_server(
        tmp_path, monkeypatch,
        # 3s 慢流：HTTP 断开后 run 仍在途，WS 有充足窗口订阅并收增量
        model_factory=lambda config, **kw: _SlowStreamModel(chunks=30, interval=0.1),
    )
    try:
        session_id = await _start_run_get_session_id(port, "慢任务")

        import httpx2

        async with httpx2.AsyncClient(timeout=15) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": session_id,
            }))
            frames = await _recv_until(
                ws, lambda fs: any(f.get("type") == "snapshot" for f in fs))
            snap = next(f for f in frames if f.get("type") == "snapshot")
            assert snap["session_id"] == session_id
            assert snap["has_active_run"] is True, \
                "订阅时 run 必须在途（规格要求 active run）"

            # 继续收增量 event 帧（run 仍在流式产出）
            event_frames = await _recv_until(
                ws,
                lambda fs: len([f for f in fs if f.get("type") == "event"]) >= 1,
            )
            events = [f for f in event_frames if f.get("type") == "event"]
            assert events, "在途 run 的增量事件必须经 WS 推送"
            for ef in events:
                assert ef["session_id"] == session_id
                inner = ef["event"]
                for key in ("type", "data", "seq", "session_id"):
                    assert key in inner, f"event 信封缺少 {key}: {inner}"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ws_client_ping_pong(tmp_path, monkeypatch):
    """客户端主动 ping → 服务端 pong（既有应用层契约）。"""
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
async def test_ws_server_heartbeat_ping_arrives(tmp_path, monkeypatch):
    """Q4d.4 heartbeat：服务端按 ``WS_PING_INTERVAL`` 下行 ``server_ping``。

    ASGI 发不出 RFC 6455 PING 控制帧，故心跳是应用层帧（SDD 02 §7.9
    「logical heartbeat」）。客户端回 ``pong`` 后连接保持可用。
    """
    monkeypatch.setattr("agent_harness.web.websocket.WS_PING_INTERVAL", 0.2)
    monkeypatch.setattr("agent_harness.web.websocket.WS_PING_TIMEOUT", 5.0)
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        import httpx2

        async with httpx2.AsyncClient(timeout=5) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            frames = await _recv_until(
                ws,
                lambda fs: any(f.get("type") == "server_ping" for f in fs),
                timeout=3.0,
            )
            assert any(f.get("type") == "server_ping" for f in frames), \
                "期望 server_ping 帧，未收到"

            # 应答 pong → 连接保持可用（再发 ping 仍能拿到 pong）
            await ws.send_text(json.dumps({"type": "pong"}))
            await ws.send_text(json.dumps({"type": "ping"}))
            alive = await _recv_until(
                ws, lambda fs: any(f.get("type") == "pong" for f in fs),
                timeout=3.0)
            assert any(f.get("type") == "pong" for f in alive), \
                "应答心跳后连接应保持可用"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ws_dead_peer_closed_after_timeout(tmp_path, monkeypatch):
    """Q4d.4 heartbeat：客户端不应答 → ``WS_PING_TIMEOUT`` 后服务端关闭连接。"""
    monkeypatch.setattr("agent_harness.web.websocket.WS_PING_INTERVAL", 0.1)
    monkeypatch.setattr("agent_harness.web.websocket.WS_PING_TIMEOUT", 0.3)
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            closed = False
            try:
                for _ in range(50):
                    await asyncio.wait_for(ws.receive_text(), timeout=2.0)
            except Exception:  # noqa: BLE001 — 断开以任意传输异常收场
                closed = True
            assert closed, "客户端不应答心跳，服务端应关闭连接"
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


# ── #208：WS 快照的 backlog 保护（与 SSE 同一判据）─────────────────────


@pytest.mark.asyncio
async def test_ws_snapshot_backlog_over_threshold_emits_control_frame(
    tmp_path, monkeypatch,
):
    """backlog 超阈值 → 快照里只有一帧 stream/truncated，**不塞整段日志**。

    与 `test_web_stream.test_replay_backlog_threshold_emits_control_frame`
    同一手法（把阈值调小才可能构造超限），但走 WS 通道：断言两条通道的**判据
    与帧形状一致**——形状来自 `serialization.build_truncated_control` 这个唯一
    构建点，所以这里同时锁住了 SSE 侧不会与 WS 漂开。
    """
    from agent_harness import web as web_module

    monkeypatch.setattr(web_module.app, "STREAM_REPLAY_MAX_EVENTS", 2)

    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_session(port, "一轮")
        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client:
            events = (await client.get(
                f"http://127.0.0.1:{port}/api/sessions/{session_id}/events"
            )).json()
        latest = max(e["seq"] for e in events if e.get("seq") is not None)
        assert latest > 2, "夹具前提：event 数必须超过（调小后的）阈值"

        async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            # 从头发（after_seq=-1）⇒ 4 个事件 > 阈值 2 ⇒ 必须截断
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": session_id, "after_seq": -1}))
            frames = await _recv_until(
                ws, lambda fs: any(f.get("type") == "snapshot" for f in fs))
            snap = next(f for f in frames if f.get("type") == "snapshot")
            assert len(snap["events"]) == 1, \
                f"超限快照只应带控制帧，实际 {len(snap['events'])} 条"
            control = snap["events"][0]
            assert control["type"] == "stream/truncated"
            assert control["data"] == {"after_seq": -1, "latest_seq": latest}
            assert control["seq"] is None, "控制帧不是运行事实，无 seq"
            assert control["durability"] == "transient"
            # has_active_run=false ⇒ 客户端据此 settle 后走重建（不会等 done）
            assert snap["has_active_run"] is False

        # 客户端按重建路径回来：游标在最新处 ⇒ 不再触发，且只补窗口内那一截
        async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": session_id, "after_seq": latest}))
            frames = await _recv_until(
                ws, lambda fs: any(f.get("type") == "snapshot" for f in fs))
            snap = next(f for f in frames if f.get("type") == "snapshot")
            assert all(e["type"] != "stream/truncated" for e in snap["events"])
            assert snap["events"] == [], "游标已在最新处 ⇒ 没有可补的事件"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ws_truncation_on_active_run_leaves_no_subscriber(tmp_path, monkeypatch):
    """超限分支**不订阅、不起 relay**：被导向重建时不得留下孤儿订阅者。

    为什么这条必须单独锁：被截断的订阅者不会有人消费（客户端收到控制帧就断开去
    重建），若超限分支先 `subscribe()` 再返回，每个重建周期都会在 RunManager 的
    `subscribers` 里留一个永不清理的队列——重建次数越多泄漏越多，且这个 session
    的下一轮 fanout 会往这些死队列里写。这里用**真在途 run**（3s 慢流）才能非空洞
    地验证：跑完再订阅能收到增量，证明断言时 run 确实在途。
    """
    from agent_harness import web as web_module

    monkeypatch.setattr(web_module.app, "STREAM_REPLAY_MAX_EVENTS", 2)

    server, serve_task, port, app = await _start_server(
        tmp_path, monkeypatch,
        model_factory=lambda config, **kw: _SlowStreamModel(chunks=30, interval=0.1),
    )
    try:
        session_id = await _start_run_get_session_id(port, "慢任务")

        # 前置：等这个在途 run 产出**超过**（调小后的）阈值的事件。seq 从 0 起，
        # 所以"≥阈值+1"才保证 `replay_upto - (-1) > 2` 成立——订阅早于它就会
        # 走正常分支，本用例变成空洞的绿。
        run = None
        for _ in range(200):
            run = app.state.agent.run_manager.get_active(session_id)
            if run is not None and run.last_enqueued_seq > 2:
                break
            await asyncio.sleep(0.05)
        assert run is not None and run.last_enqueued_seq > 2, \
            f"前提：在途 run 必须已产出 >2 个事件（实际 {run and run.last_enqueued_seq}）"

        import httpx2

        async with httpx2.AsyncClient(timeout=15) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            # 从头发 ⇒ 在途 run 的事件数早已超过阈值 2 ⇒ 必须走截断分支
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": session_id, "after_seq": -1}))
            frames = await _recv_until(
                ws, lambda fs: any(f.get("type") == "snapshot" for f in fs))
            snap = next(f for f in frames if f.get("type") == "snapshot")
            assert [e["type"] for e in snap["events"]] == ["stream/truncated"]

            run = app.state.agent.run_manager.get_active(session_id)
            assert run is not None, "前提：此时 run 仍在途（否则本用例空洞）"
            assert run.subscribers == {}, \
                "超限分支不得留下订阅者——客户端会断开去重建，没人消费这些队列"

            # 旁证（用户可见后果）：这条连接上不该再有 event 帧——relay 没起。
            after = await _recv_until(
                ws, lambda fs: any(f.get("type") == "event" for f in fs),
                timeout=0.5,
            )
            assert not [f for f in after if f.get("type") == "event"], \
                "截断后不得继续推增量事件（客户端已去重建）"

        # 非空洞对照：带真实游标回来仍能收到增量 ⇒ run 确实是活的
        async with httpx2.AsyncClient(timeout=15) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            run = app.state.agent.run_manager.get_active(session_id)
            cursor = run.last_enqueued_seq if run is not None else -1
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": session_id, "after_seq": cursor}))
            frames = await _recv_until(
                ws,
                lambda fs: len([f for f in fs if f.get("type") == "event"]) >= 1,
            )
            assert [f for f in frames if f.get("type") == "event"], \
                "带正确游标回来必须能接上 live 增量"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ws_truncation_criterion_is_persisted_max_not_enqueue_cursor(
    tmp_path, monkeypatch,
):
    """阈值判据取**持久面** `latest_seq`，不取 run 的入队游标（2026-09-17 审查修正）。

    为什么必须单锁：两个量在稳态相等（`runmanager.py:131-137`：落盘先于 listener
    入队），**只有 listener 落后时**才分叉——而那一刻两条通道会给出相反裁决
    （SSE 用 `events[-1].seq`、WS 若用 `last_enqueued_seq` 就会截断），契约却写着
    "同一条判据"。分叉窗口靠真实时序等不到，所以这里**直接构造**：把在途 run 的
    入队游标推到持久 max 之外（`_last_enqueued_seq` 是 `runmanager.py:62` 的私有
    计数，测试直接写它），游标取到"持久面刚好不超限"的位置（差值 == 阈值）。
    旧实现（用入队游标）⇒ 502 > 2 ⇒ 截断（红）；现实现（用持久 max）⇒ 正常快照，
    窗口恰好两条（绿）。
    """
    from agent_harness import web as web_module

    monkeypatch.setattr(web_module.app, "STREAM_REPLAY_MAX_EVENTS", 2)

    server, serve_task, port, app = await _start_server(
        tmp_path, monkeypatch,
        model_factory=lambda config, **kw: _SlowStreamModel(chunks=30, interval=0.1),
    )
    try:
        session_id = await _start_run_get_session_id(port, "慢任务")

        # 前置：持久化事件必须已超过（调小后的）阈值——否则"没截断"是因为事件本来就
        # 少，本用例变成空洞的绿。
        run = None
        for _ in range(200):
            run = app.state.agent.run_manager.get_active(session_id)
            if run is not None and run.last_enqueued_seq > 2:
                break
            await asyncio.sleep(0.05)
        assert run is not None and run.last_enqueued_seq > 2, \
            f"前提：在途 run 必须已产出 >2 个事件（实际 {run and run.last_enqueued_seq}）"

        import httpx2

        async with httpx2.AsyncClient(timeout=15) as client:
            events = (await client.get(
                f"http://127.0.0.1:{port}/api/sessions/{session_id}/events"
            )).json()
        persisted_max = max(e["seq"] for e in events if e.get("seq") is not None)

        # 构造分叉：入队游标 >> 持久 max。真实的"落盘先于入队"不变量被打破，但判据
        # 若取持久面，就**不该**受影响。
        run._last_enqueued_seq = persisted_max + 500

        # 游标取到"持久面刚好不超限"的位置：`latest_seq - after_seq == 阈值`（不是 >）。
        # 于是两个判据给出相反裁决——持久面 ⇒ 正常快照（2 条）；入队游标 ⇒ 502 > 2
        # ⇒ 截断。这正是分叉时两条通道会打架的那一刻。
        after_seq = persisted_max - 2

        async with httpx2.AsyncClient(timeout=15) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": session_id, "after_seq": after_seq}))
            frames = await _recv_until(
                ws, lambda fs: any(f.get("type") == "snapshot" for f in fs))
            snap = next(f for f in frames if f.get("type") == "snapshot")
            types = [e["type"] for e in snap["events"]]
            assert "stream/truncated" not in types, (
                "判据取的是 run 的入队游标（被推高即误判超限）——应取持久 max "
                f"`events[-1].seq`（persisted_max={persisted_max}, after_seq={after_seq}）"
            )
            # 非空洞：窗口恰好是 (after_seq, replay_upto] 那一截（持久面 2 条）
            seqs = [e["seq"] for e in snap["events"] if e.get("seq") is not None]
            assert seqs == [persisted_max - 1, persisted_max], \
                f"窗口应只补持久面那两条，实际 {seqs}"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ws_snapshot_window_filters_by_cursor(tmp_path, monkeypatch):
    """带游标 ⇒ 快照只发 `after_seq < seq ≤ replay_upto` 那一截（#208）。

    阈值保持默认（1000）⇒ 走在窗口这条正常路径上：这是"老客户端拿全量、
    新客户端只补差额"的分界，也是重建后不重复投影的**服务端**那一半
    （另一半是客户端的 seq 幂等门）。
    """
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_session(port, "一轮")
        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client:
            events = (await client.get(
                f"http://127.0.0.1:{port}/api/sessions/{session_id}/events"
            )).json()
        seqs = [e["seq"] for e in events if e.get("seq") is not None]
        assert len(seqs) >= 3, "夹具前提：至少 3 个 durable 事件"
        mid = sorted(seqs)[1]

        async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            await ws.send_text(json.dumps({
                "type": "subscribe", "session_id": session_id, "after_seq": mid}))
            frames = await _recv_until(
                ws, lambda fs: any(f.get("type") == "snapshot" for f in fs))
            snap = next(f for f in frames if f.get("type") == "snapshot")
            got = [e["seq"] for e in snap["events"] if e.get("seq") is not None]
            assert got == [s for s in sorted(seqs) if s > mid], \
                f"窗口应为 ({mid}, {max(seqs)}]，实际 {got}"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ws_snapshot_illegal_cursor_falls_back_to_head(tmp_path, monkeypatch):
    """非法 `after_seq`（字符串 / 布尔 / 缺失）⇒ 当 -1 从头发，**不报错不断连**。

    这是纯粹的输入校验：把任意 JSON 类型交给 `replay_upto - after_seq` 会抛
    TypeError，那是**连接级**异常（整条 WS 挂掉），代价远大于"按最保守的
    从头发"。三条非法输入都必须照常拿到快照。
    """
    server, serve_task, port, _app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_session(port, "一轮")
        import httpx2

        for bad in ("9", True, None):
            payload = {"type": "subscribe", "session_id": session_id}
            if bad is not None:
                payload["after_seq"] = bad
            async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
                f"ws://127.0.0.1:{port}/api/ws"
            ) as ws:
                await ws.send_text(json.dumps(payload))
                frames = await _recv_until(
                    ws, lambda fs: any(f.get("type") == "snapshot" for f in fs))
                snap = next(
                    (f for f in frames if f.get("type") == "snapshot"), None)
                assert snap is not None, f"after_seq={bad!r} 未拿到快照"
                assert snap["events"], "回退到 -1 ⇒ 应重放全部 durable 事件"
                assert not any(f.get("type") == "error" for f in frames)
    finally:
        await _shutdown(server, serve_task)
