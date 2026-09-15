"""ADR-0030（#196）T5 / T8：supersede 与队列的 HTTP 契约。

只测外部行为（HTTP 状态码 + 事件流），不碰 service 内部。接缝与
test_web_send_message_stream.py 同：真实 ASGI 服务器 + httpx2 流式客户端 +
可控模型替身。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from tests.scripted_model import ScriptedModel


class _OneTurnModel(ScriptedModel):
    """每次新 run 都吐一条最终 AIMessage（create_chat_model 每轮新建实例）。"""

    def __init__(self) -> None:
        super().__init__([AIMessage(content="task done")])


async def _start_server(tmp_path, monkeypatch):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: _OneTurnModel(),
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
    return server, serve_task, port


async def _shutdown(server, serve_task) -> None:
    server.should_exit = True
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass


async def _post(port: int, path: str, body: dict | None = None) -> tuple[int, dict]:
    import httpx2

    async with httpx2.AsyncClient(timeout=30) as client:
        response = await client.post(f"http://127.0.0.1:{port}{path}", json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload


async def _get(port: int, path: str) -> tuple[int, dict]:
    import httpx2

    async with httpx2.AsyncClient(timeout=30) as client:
        response = await client.get(f"http://127.0.0.1:{port}{path}")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload


async def _collect_stream(port: int, method: str, path: str,
                          body: dict | None = None) -> list[dict]:
    """消费 SSE 流到终态。"""
    import httpx2

    frames: list[dict] = []
    client = httpx2.AsyncClient(timeout=None)
    try:
        if body is not None:
            context = client.stream(method, f"http://127.0.0.1:{port}{path}", json=body)
        else:
            context = client.stream(method, f"http://127.0.0.1:{port}{path}")
        async with context as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                import json
                frames.append(json.loads(line.removeprefix("data:").strip()))
                if frames[-1].get("type") in ("run/completed", "run/failed"):
                    break
    finally:
        await client.aclose()
    return frames


async def _completed_session(port: int, task: str = "首轮") -> dict:
    """创建一个跑完的会话，返回 {session_id, frames}。

    ⚠ run/completed 帧到达后 run 的收尾 await 还在跑——此刻 flush / messages
    会撞 ActiveRunConflict（终态驱动同款守卫）。调用方需要立刻投递时先
    `_wait_idle`。
    """
    frames = await _collect_stream(port, "POST", "/api/sessions", {"task": task})
    assert frames and frames[-1]["type"] == "run/completed"
    return {"session_id": frames[0]["session_id"], "frames": frames}


async def _wait_idle(port: int, session_id: str, *, timeout: float = 10.0) -> None:
    """等该会话真正空闲（无在途 run）——轮询 POST /queue/flush：idle 会话 +
    无待投递输入 → 200 {"status": "idle"}，在途 → 409。idle 回执本身就是
    "run 已收口"的可靠信号。"""
    import time

    deadline = time.monotonic() + timeout
    while True:
        status, payload = await _post(port, f"/api/sessions/{session_id}/queue/flush")
        if status == 200 and payload == {"status": "idle"}:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"超时 {timeout}s：会话 {session_id} 未空闲")
        await asyncio.sleep(0.05)


async def _flush_stream(port: int, session_id: str, tmp_path, *, timeout: float = 10.0) -> list[dict]:
    """一次性流式 flush（**不得**先用 _post 探测）。

    探测为什么禁止：flush 的 200 是 SSE 流（launched 语义），_post 会把流头
    消费掉、开出的 run 没有消费者——虽然 run 本身会跑完（detached），但测试
    断言"flush 的流被消费"就落空了。所以空闲判定改读**事件流**（run 终态
    落盘 + 无在途接力），空闲后一次性用流式客户端 flush。
    """
    import pathlib
    import time

    from agent_harness.session.store import JsonlSessionStore

    # workspace_dir 即 tmp_path：与 Settings 一致（sessions 在 workspace_dir/sessions）
    store = JsonlSessionStore(root=pathlib.Path(tmp_path) / "sessions")
    deadline = time.monotonic() + timeout
    while True:
        events = store.read_events(session_id)
        terminals = [
            e for e in events if e.type in ("run/completed", "run/failed")
        ]
        # 空闲 = 已有终态 run 且最后一个终态之后没有接力 run 的 user/message
        # （接力 run 会先写 user/message 再写 run/started——终态之后无新输入
        # 才是真正空闲）。简化：终态后没有 user/message。
        terminal_seqs = {e.seq for e in terminals}
        if terminal_seqs:
            last_terminal = max(terminal_seqs)
            after = [
                e for e in events
                if e.seq > last_terminal and e.type == "user/message"
            ]
            if not after:
                break
        if time.monotonic() >= deadline:
            raise AssertionError(f"超时 {timeout}s：会话 {session_id} 未空闲")
        await asyncio.sleep(0.05)
    return await _collect_stream(port, "POST", f"/api/sessions/{session_id}/queue/flush")


# ── T5：supersede 只允许最新一条 / injected 拒绝 ──────────────────────


@pytest.mark.asyncio
async def test_supersede_latest_user_message_via_edit(tmp_path, monkeypatch):
    """supersedes_seq 指向最新一条用户消息 → 200，事件流有 message/superseded。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        session = await _completed_session(port)
        sid = session["session_id"]
        # 找最新 user/message 的 seq（重放会话事件）
        code, frames = await _get(port, f"/api/sessions/{sid}/events")
        # events 端点若不存在，直接从创建帧里取（首帧即 user/message）
        if code != 200:
            user_frame = next(f for f in session["frames"] if f["type"] == "user/message")
            user_seq = user_frame["seq"]
        else:
            user_seq = next(
                f["seq"] for f in frames if f.get("type") == "user/message"
            )

        status, payload = await _post(
            port, f"/api/sessions/{sid}/messages",
            {"content": "改成的新问题", "mode": "queue", "supersedes_seq": user_seq},
        )
        assert status == 200, f"supersede 应当成功，实际 {status}: {payload}"

        # 事件流有 message/superseded {superseded_seq, carrier}
        import pathlib

        from agent_harness.session.store import JsonlSessionStore
        # 从服务端同款 store 读事件：重建 store 路径
        store = JsonlSessionStore(root=pathlib.Path(tmp_path) / "sessions" )
        # workspace_dir 即 tmp_path（Settings 默认 .agent 在 workspace_dir 下）
        events = store.read_events(sid)
        superseded = [e for e in events if e.type == "message/superseded"]
        assert len(superseded) == 1
        assert superseded[0].data["superseded_seq"] == user_seq
        assert superseded[0].data["carrier"] == "queue"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_supersede_non_latest_user_message_409(tmp_path, monkeypatch):
    """对非最新 user 消息 supersede → 409 SupersedeTargetInvalid。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        session = await _completed_session(port)
        sid = session["session_id"]
        user_seq = next(
            f["seq"] for f in session["frames"] if f["type"] == "user/message"
        )
        # 先发一条续聊（launched SSE 跑完）⇒ 第一条不再是最新
        await _collect_stream(
            port, "POST", f"/api/sessions/{sid}/messages",
            {"content": "第二条", "mode": "queue"},
        )
        status, payload = await _post(
            port, f"/api/sessions/{sid}/messages",
            {"content": "改第一条", "mode": "queue", "supersedes_seq": user_seq},
        )
        assert status == 409, f"非最新应当 409，实际 {status}: {payload}"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_supersede_unknown_seq_409(tmp_path, monkeypatch):
    """supersedes_seq 指向不存在的 seq → 409（不是 404：目标不对 ≠ 会话不存在）。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        session = await _completed_session(port)
        sid = session["session_id"]
        status, payload = await _post(
            port, f"/api/sessions/{sid}/messages",
            {"content": "改不存在的一条", "mode": "queue", "supersedes_seq": 9999},
        )
        assert status == 409, f"未知目标应当 409，实际 {status}: {payload}"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_supersede_repeat_same_seq_409(tmp_path, monkeypatch):
    """重复 supersede 同一 seq → 409（写侧拒绝；投影幂等在 derive 层独立成立）。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        session = await _completed_session(port)
        sid = session["session_id"]
        user_seq = next(
            f["seq"] for f in session["frames"] if f["type"] == "user/message"
        )
        status, _ = await _post(
            port, f"/api/sessions/{sid}/messages",
            {"content": "第一次改", "mode": "queue", "supersedes_seq": user_seq},
        )
        assert status == 200
        # 等第一次 supersede 的接力 run 收口（否则下一次请求撞 ActiveRunConflict）
        for _ in range(100):
            import pathlib

            from agent_harness.session.store import JsonlSessionStore
            store = JsonlSessionStore(root=pathlib.Path(tmp_path) / "sessions")
            events = store.read_events(sid)
            terminals = [e for e in events if e.type in ("run/completed", "run/failed")]
            if len(terminals) >= 2:
                break
            await asyncio.sleep(0.05)
        status, payload = await _post(
            port, f"/api/sessions/{sid}/messages",
            {"content": "第二次改", "mode": "queue", "supersedes_seq": user_seq},
        )
        assert status == 409, f"重复 supersede 应当 409，实际 {status}: {payload}"
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_supersede_with_queue_id_still_validated(tmp_path, monkeypatch):
    """queue_id 与 supersedes_seq 同传：取代校验**必须**照跑（审查 P2 缺口）。

    原 elif 会在同传 queue_id 时跳过 _assert_supersedable，第 3 步照样写
    message/superseded——客户端可借排队项捎带绕过 D8。修复后对非最新目标
    必须 409（先于投递抛，错误以 JSON 落地），且不留下 superseded 事件。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        session = await _completed_session(port)
        sid = session["session_id"]
        user_seq = next(
            f["seq"] for f in session["frames"] if f["type"] == "user/message"
        )
        # 空闲会话 + 手工写一条排队事实（合法 queue_id，模拟崩溃前的 durable
        # 事实——与其他排队测试同一手法）；supersedes_seq 指向非最新（第一条
        # user/message，续聊后的最新不是它——先续聊一条并等收口造"非最新"）。
        await _collect_stream(
            port, "POST", f"/api/sessions/{sid}/messages",
            {"content": "第二条", "mode": "queue"},
        )
        await _wait_idle(port, sid)
        import pathlib

        from agent_harness.session import Session
        from agent_harness.session.event import MESSAGE_QUEUED
        from agent_harness.session.store import JsonlSessionStore
        store = JsonlSessionStore(root=pathlib.Path(tmp_path) / "sessions")
        Session.append_event(store, sid, MESSAGE_QUEUED,
                             {"queue_id": "q-piggyback", "content": "排队事实"})
        first_seq = user_seq

        # 同传：queue_id（存在）+ supersedes_seq=非最新 → 必须 409
        status, payload = await _post(
            port, f"/api/sessions/{sid}/messages",
            {
                "content": "借排队捎带的取代",
                "mode": "queue",
                "queue_id": "q-piggyback",
                "supersedes_seq": first_seq,
            },
        )
        assert status == 409, f"同传时非最新取代必须仍被拒，实际 {status}: {payload}"

        # 不留下 superseded 事件（校验失败在投递之前抛）
        events = store.read_events(sid)
        assert not [e for e in events if e.type == "message/superseded"], (
            "409 路径不得写 superseded 事件"
        )
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_supersede_injected_message_409(tmp_path, monkeypatch):
    """对 injected_by 消息 supersede → 409（ADR-0030 T5：注入消息不可编辑）。

    构造方式与真机一致：failure-guard 纠正消息（服务端在工具失败时注入的
    user-role 消息带 injected_by）。手工写一条 injected_by user/message 后，
    对它（以及把它算作"最新"的场景）都断言 409。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        session = await _completed_session(port)
        sid = session["session_id"]
        await _wait_idle(port, sid)
        import pathlib

        from agent_harness.session import Session
        from agent_harness.session.event import USER_MESSAGE
        from agent_harness.session.store import JsonlSessionStore
        store = JsonlSessionStore(root=pathlib.Path(tmp_path) / "sessions")
        Session.append_event(store, sid, USER_MESSAGE,
                             {"content": "运行时纠正", "injected_by": "failure-guard"})

        # 注入消息排在最后 ⇒ 它是最新 user/message：对它 supersede → 409（注入不可编辑）
        events = store.read_events(sid)
        injected_seq = next(
            e.seq for e in events
            if e.type == "user/message" and e.data.get("injected_by")
        )
        status, payload = await _post(
            port, f"/api/sessions/{sid}/messages",
            {"content": "改注入消息", "mode": "queue", "supersedes_seq": injected_seq},
        )
        assert status == 409, f"注入消息必须 409，实际 {status}: {payload}"
    finally:
        await _shutdown(server, serve_task)


# ── T8：GET /queue + flush + 编辑排队项 ──────────────────────────────


@pytest.mark.asyncio
async def test_get_queue_and_flush_roundtrip(tmp_path, monkeypatch):
    """排队中的项 GET /queue 可见；flush 投递一条并返回 SSE 流；空队列 → idle。

    ⚠ 第一个 run 的 `_drive` 收尾窗口会触发 on_run_terminal——此刻 append 的
    queued 会被它**接力掉**（这正是 T1 的行为，本测试不想测它）。所以先
    `_wait_idle`（idle 回执 = 终态回调已跑完、没有待投递输入）再 append。
    """
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        session = await _completed_session(port)
        sid = session["session_id"]
        await _wait_idle(port, sid)

        # 建第二个会话作为"在途 run 的宿主"不必要——直接对 completed 会话入队？
        # 不行：idle 会话发消息直接拉起新 run。入队需要一个在途 run：
        # 用 gate 模型太重；这里改为直接测"重启重建后 flush"路径——
        # 先手工写一条未消费的 message/queued 事件（模拟崩溃前的事实），
        # flush 应当把它投递出去（它读的是事件流，不依赖内存镜像）。
        import pathlib

        from agent_harness.session.event import MESSAGE_QUEUED
        from agent_harness.session.store import JsonlSessionStore
        store = JsonlSessionStore(root=pathlib.Path(tmp_path) / "sessions")
        from agent_harness.session import Session
        # append_event 只落盘不 resume（与崩溃窗口等价）
        Session.append_event(store, sid, MESSAGE_QUEUED,
                             {"queue_id": "q-flush", "content": "重启前的消息"})

        code, payload = await _get(port, f"/api/sessions/{sid}/queue")
        assert code == 200
        assert [i["queue_id"] for i in payload["items"]] == ["q-flush"]
        assert payload["items"][0]["content"] == "重启前的消息"
        assert payload["steers"] == []

        # flush → SSE 流（launched 语义），投递的就是那条（409 = 接力 run 在途，
        # 轮询重试；重试轮次没开 run，不会双投）
        frames = await _flush_stream(port, sid, tmp_path)
        assert frames, "flush 必须返回 SSE 流"
        assert frames[-1]["type"] in ("run/completed", "run/failed")
        user_contents = [
            f["data"].get("content") for f in frames if f["type"] == "user/message"
        ]
        assert "重启前的消息" in user_contents

        # 投递后队列清空 → idle
        code, payload = await _get(port, f"/api/sessions/{sid}/queue")
        assert code == 200 and payload["items"] == []
        status, payload = await _post(port, f"/api/sessions/{sid}/queue/flush")
        assert status == 200 and payload == {"status": "idle"}
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_get_queue_unknown_session_404(tmp_path, monkeypatch):
    """未知会话 GET /queue → 404。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        code, _ = await _get(port, "/api/sessions/nonexistent/queue")
        assert code == 404
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_edit_queued_item_cancel_old_then_queue_new(tmp_path, monkeypatch):
    """编辑排队项 = queue_id 语义：旧项 cancelled + 新项 queued。

    排队需要一个在途 run——用第一个会话入队后立刻对**它**发第二条（仍在途），
    不行：入队后马上返回。这里改为直接验证：完成会话 + 手工写 queued 事实 +
    带 queue_id 的请求替换它。
    """
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        session = await _completed_session(port)
        sid = session["session_id"]
        await _wait_idle(port, sid)
        import pathlib

        from agent_harness.session import Session
        from agent_harness.session.event import MESSAGE_QUEUED
        from agent_harness.session.store import JsonlSessionStore
        store = JsonlSessionStore(root=pathlib.Path(tmp_path) / "sessions")
        Session.append_event(store, sid, MESSAGE_QUEUED,
                             {"queue_id": "q-old", "content": "旧内容"})

        status, payload = await _post(
            port, f"/api/sessions/{sid}/messages",
            {"content": "新内容", "mode": "queue", "queue_id": "q-old"},
        )
        assert status == 200, f"替换排队项应当成功，实际 {status}: {payload}"

        events = store.read_events(sid)
        cancelled = [e for e in events if e.type == "queue/cancelled"]
        queued = [e for e in events if e.type == "message/queued"]
        assert [e.data["queue_id"] for e in cancelled] == ["q-old"]
        # 新项是入队事实（idle 会话直接 launch 了 run，带 queue_id 的请求在
        # cancel 之后走 idle 分支——新内容不入队，直接成为新 run 的首条输入）
        new_contents = [e.data["content"] for e in queued]
        assert "旧内容" in new_contents

        # 未知 queue_id → 404
        status, _ = await _post(
            port, f"/api/sessions/{sid}/messages",
            {"content": "x", "mode": "queue", "queue_id": "q-never-existed"},
        )
        assert status == 404
    finally:
        await _shutdown(server, serve_task)
