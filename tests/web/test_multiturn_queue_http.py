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

    ⚠ run/completed 帧到达后 run 的收尾还在跑（`_drive` finally 才置终态旗标），
    此刻 flush / messages 会撞 ActiveRunConflict；且该 run 的终态回调还会把
    "此时才入队"的输入接力掉。需要确定性的「无 run」起点请用 `_empty_session`。
    """
    frames = await _collect_stream(port, "POST", "/api/sessions", {"task": task})
    assert frames and frames[-1]["type"] == "run/completed"
    return {"session_id": frames[0]["session_id"], "frames": frames}


async def _empty_session(port: int) -> str:
    """建一个**空会话**（launch=false，不启动 run），返回 session_id。

    「重启后 flush」类用例需要这样一个起点：会话只有 `session/started`、
    没有任何在途或已终结的 run。原因见 `test_get_queue_and_flush_roundtrip`
    的说明——若先跑一个 run 再手工写 queued 事件，会与那个 run 收尾回调的
    接力行为竞争。空会话没有回调可竞争，是确定性的。
    """
    import httpx2

    async with httpx2.AsyncClient(timeout=30) as client:
        # body 必须是 {}（不是 None）：task 字段可省略，但 CreateSessionRequest
        # 本身是必填 body，json=None 会被 FastAPI 判 422 missing body。
        response = await client.post(
            f"http://127.0.0.1:{port}/api/sessions?launch=false", json={}
        )
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


async def _wait_idle(port: int, session_id: str, *, timeout: float = 10.0) -> None:
    """等该会话**没有待投递输入**——轮询 POST /queue/flush：无待投递 → 200
    {"status": "idle"}（有则 200 SSE / 在途 409）。

    ⚠ 它**不**证明"没有在途 run"：`deliver_next_undelivered` 先看有无待投递
    输入，无则直接返回 idle，根本不看 active。所以 idle ≠ RunManager 已摘掉
    active；需要"run 已彻底收口"的用例不能只靠它（见 `_empty_session`）。"""
    import time

    deadline = time.monotonic() + timeout
    while True:
        status, payload = await _post(port, f"/api/sessions/{session_id}/queue/flush")
        if status == 200 and payload == {"status": "idle"}:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"超时 {timeout}s：会话 {session_id} 未空闲")
        await asyncio.sleep(0.05)


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
        # 断言是**取代校验**拒的，不是在途 run 拒的：两个都返回 409，只断言状态码
        # 会让 ActiveRunConflict 冒充通过（本用例要锁的正是"同传 queue_id 不跳过校验"）。
        assert "不是最新一条用户消息" in payload.get("detail", ""), payload

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
        # 同「同传」用例：区分"注入不可编辑"与"在途 run"两种 409。
        assert "注入的消息" in payload.get("detail", ""), payload
    finally:
        await _shutdown(server, serve_task)


# ── T8：GET /queue + flush + 编辑排队项 ──────────────────────────────


@pytest.mark.asyncio
async def test_get_queue_and_flush_roundtrip(tmp_path, monkeypatch):
    """排队中的项 GET /queue 可见；flush 投递一条并返回 SSE 流；空队列 → idle。

    起点必须是**空会话**（`launch=false`）：本用例模拟「重启后手工写一条 queued
    事实，再 flush 投递」。早先版本先跑一个 run 再 append，会与那个 run 收尾
    回调的接力（§4.5.5：终态驱动自动投递未消费输入）竞争——回调可能在
    "append 之后、flush 之前"把这条**接力掉**，于是 flush 正确地返回了
    `{"status":"idle"}`（无待投递），断言却期望 SSE 流。空会话没有在途/已终结
    run，就没有回调可竞争。
    """
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        sid = await _empty_session(port)

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

        # flush → SSE 流（launched 语义），投递的就是那条
        frames = await _collect_stream(
            port, "POST", f"/api/sessions/{sid}/queue/flush"
        )
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

    起点用**空会话**（同 `test_get_queue_and_flush_roundtrip`）：先跑一个 run 再
    手工写 queued 事实，会与该 run 收尾回调的接力竞争——回调可能在 append 之后
    才投递，使这里期待 200 的请求撞上 ActiveRunConflict 409。
    """
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        sid = await _empty_session(port)
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
