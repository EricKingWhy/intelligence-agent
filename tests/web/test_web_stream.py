"""T6（ADR-0016 §2.3）：GET /api/sessions/{id}/stream?after_seq=N 重连续传。

验收映射（规格 01 §22 场景 E）：断连 → run 继续 → after_seq 续传 → 无重复块；
重放按 seq 序；run 已终态 → 重放至终态收尾；backlog 过大 → 控制帧走全量重建。
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessageChunk

from agent_harness.session import JsonlSessionStore


class SlowStreamModel:
    """25 chunk × 0.08s ≈ 2s 慢流：重连窗口内 run 仍在途。"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        for i in range(25):
            await asyncio.sleep(0.08)
            yield AIMessageChunk(content=f"chunk{i} ")


async def _start_server(tmp_path, monkeypatch, model):
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
    """读 SSE 流；stop_types 命中 / 流结束 / max_frames 到达即返回（断开）。"""
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
                if (stop_types and frame.get("type") in stop_types) or (
                        max_frames is not None and len(frames) >= max_frames):
                    break
    finally:
        await client.aclose()
    return frames


async def _wait_terminal(store: JsonlSessionStore, session_id: str) -> None:
    async with asyncio.timeout(12.0):
        while True:
            events = store.read_events(session_id)
            if events and events[-1].type in ("run/completed", "run/failed"):
                return
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_stream_unknown_session_404(tmp_path, monkeypatch):
    server, serve_task, port, _app = await _start_server(
        tmp_path, monkeypatch, SlowStreamModel)
    import httpx2

    try:
        async with httpx2.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"http://127.0.0.1:{port}/api/sessions/no-such/stream?after_seq=-1")
        assert resp.status_code == 404
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_reconnect_resumes_without_duplicates_or_gaps(tmp_path, monkeypatch):
    """断连 → run 继续 → after_seq 续传：seq 连续无重复、终态可达。"""
    server, serve_task, port, _app = await _start_server(
        tmp_path, monkeypatch, SlowStreamModel)
    try:
        # 连接 A：读到第 3 帧即断开
        frames_a = await _collect(port, "/api/sessions", {"task": "慢任务"},
                                  max_frames=3)
        session_id = frames_a[0]["session_id"]
        seqs_a = [f["seq"] for f in frames_a if f.get("seq") is not None]
        last_seq = max(seqs_a)

        # 连接 B：after_seq 续传，读到终态
        frames_b = await _collect(
            port, f"/api/sessions/{session_id}/stream?after_seq={last_seq}",
            None, stop_types={"run/completed", "run/failed"})
        seqs_b = [f["seq"] for f in frames_b if f.get("seq") is not None]

        assert seqs_b, "重连必须收到重放/续传帧"
        assert all(seq > last_seq for seq in seqs_b), \
            "续传帧不得重复断连前已消费的 seq"
        assert seqs_b == sorted(seqs_b), "续传按 seq 序"
        # 无缝：B 的最小 seq 紧接 A 的最大 seq（中间无丢失窗口）
        assert min(seqs_b) == last_seq + 1
        assert frames_b[-1]["type"] in ("run/completed", "run/failed"), \
            "重放至终态收尾"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_reconnect_after_terminal_replays_to_end(tmp_path, monkeypatch):
    """run 已终态的重连：重放（可含 after_seq 之前的跳过）到终态正常收尾。"""
    server, serve_task, port, _app = await _start_server(
        tmp_path, monkeypatch, SlowStreamModel)
    try:
        frames_a = await _collect(port, "/api/sessions", {"task": "慢任务"},
                                  stop_types={"run/completed"})
        session_id = frames_a[0]["session_id"]
        all_seqs = [f["seq"] for f in frames_a if f.get("seq") is not None]
        mid = all_seqs[len(all_seqs) // 2]

        # 从中途回放（模拟刷新页面后的历史重建），须到终态收尾
        frames_b = await _collect(
            port, f"/api/sessions/{session_id}/stream?after_seq={mid - 1}",
            None, stop_types={"run/completed"})
        seqs_b = [f["seq"] for f in frames_b if f.get("seq") is not None]
        assert seqs_b == list(range(mid, max(all_seqs) + 1)), \
            "重放覆盖 (after_seq, latest]，按 seq 序"
        assert frames_b[-1]["type"] == "run/completed"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_replay_backlog_threshold_emits_control_frame(tmp_path, monkeypatch):
    """backlog 超阈值 → 单帧 stream/truncated 控制事件后收流（客户端走全量重建）。"""
    from agent_harness import web as web_module

    monkeypatch.setattr(web_module.app, "STREAM_REPLAY_MAX_EVENTS", 5)

    server, serve_task, port, _app = await _start_server(
        tmp_path, monkeypatch, SlowStreamModel)
    try:
        frames_a = await _collect(port, "/api/sessions", {"task": "慢任务"},
                                  stop_types={"run/completed"})
        session_id = frames_a[0]["session_id"]
        latest = max(f["seq"] for f in frames_a if f.get("seq") is not None)

        frames_b = await _collect(
            port,
            f"/api/sessions/{session_id}/stream?after_seq=0",
            None, max_frames=1)
        assert len(frames_b) == 1
        assert frames_b[0]["type"] == "stream/truncated"
        assert frames_b[0]["data"]["latest_seq"] == latest
        assert frames_b[0].get("seq") is None, "控制帧不是运行事实，无 seq"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_truncated_branch_leaves_no_subscriber(tmp_path, monkeypatch):
    """超限分支必须退订：不得把没人消费的队列留在 RunManager 里。

    `stream_reconnect` 按协议**先订阅后取游标** ⇒ 走到截断分支时 subscriber 已
    注册。若那一支直接 return 而不退订，这个队列就永久留在 `run.subscribers`：
    孤儿计时只在 subscribers **为空**时才武装，所以它不会被回收，run 之后每次
    fanout 还会往里写。客户端收到控制帧即去全量重建，永远没人来读它。

    需要**真在途 run**（2s 慢流）才非空洞：run 已终态时 `handle.subscriber` 本就是
    None，断言恒真。
    """
    from agent_harness import web as web_module

    monkeypatch.setattr(web_module.app, "STREAM_REPLAY_MAX_EVENTS", 5)

    server, serve_task, port, app = await _start_server(
        tmp_path, monkeypatch, SlowStreamModel)
    try:
        # 起 run 但立刻断开（detached 仍在途）——与 WS 那条用例同一手法。
        import httpx2

        async with httpx2.AsyncClient(timeout=None) as client, client.stream(
            "POST", f"http://127.0.0.1:{port}/api/sessions",
            json={"task": "慢任务"},
        ) as response:
            session_id = None
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    session_id = _parse_frame(line)["session_id"]
                    break
        assert session_id, "前提：POST 必须回出 session_id"

        run = app.state.agent.run_manager.get_active(session_id)
        assert run is not None, "前提：连接断开后 run 仍在途（detached）"
        # 等事件数超过（调小后的）阈值 5，否则截断分支不会被走到。
        for _ in range(200):
            run = app.state.agent.run_manager.get_active(session_id)
            if run is None or run.last_enqueued_seq > 5:
                break
            await asyncio.sleep(0.05)
        assert run is not None and run.last_enqueued_seq > 5, \
            f"前提：在途 run 必须已产出 >5 个事件（实际 {run and run.last_enqueued_seq}）"

        frames = await _collect(
            port, f"/api/sessions/{session_id}/stream?after_seq=0",
            None, max_frames=1)
        assert [f["type"] for f in frames] == ["stream/truncated"]

        run = app.state.agent.run_manager.get_active(session_id)
        assert run is not None, "前提：收完控制帧后 run 仍在途"
        assert run.subscribers == {}, \
            "截断分支不得留下订阅者——没人消费这些队列，且孤儿计时不会回收它"
    finally:
        await _shutdown(server, serve_task)
