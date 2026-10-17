"""#1220：`ask_user` 超时闸**在线驱动**的全链路集成（真实 uvicorn + WS）。

覆盖票面验收：
- 有前端连接（WS subscribe 一个暂停在 ask_user 的会话）⇒ 到点自动恢复、注入票面消息、
  **零 answer 落盘**；
- 无前端连接 ⇒ 即使推进挂钟、驱动被 tick，也不恢复。

接缝：真实 uvicorn ASGI 服务器 + httpx2 WebSocket 客户端 + ScriptedModel 剧本桩。
在场信号取自 WS 连接级订阅计数（`web/ask_user_timeout_driver.py`），到点经
`SessionService.resume_ask_user_timeout` 走既有 user_input 恢复链。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from tests.fake_clock import FakeClock
from tests.scripted_model import ScriptedModel

_QUESTIONS = [
    {
        "question": "Which database should we use?",
        "header": "Storage",
        "options": [
            {"label": "SQLite", "description": "local file"},
            {"label": "Postgres", "description": "server"},
        ],
        "recommended": 0,
    }
]


def _ask_then_finish_factory(scripts: list[list[AIMessage]]):
    """每次 build_runtime 取一段剧本：首轮 ask_user（暂停），续跑轮吐最终消息。"""
    remaining = list(scripts)

    def factory(config, **kwargs):
        return ScriptedModel(responses=remaining.pop(0))

    return factory


def _ask_user_scripts() -> list[list[AIMessage]]:
    return [
        [AIMessage(content="", tool_calls=[{
            "id": "ask-1", "name": "ask_user", "args": {"questions": _QUESTIONS},
        }])],
        [AIMessage(content="Continued after the user did not answer.")],
    ]


async def _start_server(tmp_path, monkeypatch, factory):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", factory)
    # 心跳只用来推进超时检查；调大以免与本用例显式 tick 打架（本用例自己驱动）。
    monkeypatch.setattr("agent_harness.web.websocket.WS_PING_INTERVAL", 3600.0)
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


async def _start_run(port: int, task: str) -> str:
    """POST /api/sessions，读首帧拿 session_id 后断开（run 是 detached）。"""
    import httpx2

    async with httpx2.AsyncClient(timeout=10) as client, client.stream(
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


async def _wait_paused(port: int, session_id: str) -> None:
    import httpx2

    async with httpx2.AsyncClient(timeout=10) as client:
        for _ in range(200):
            events = (await client.get(
                f"http://127.0.0.1:{port}/api/sessions/{session_id}/events"
            )).json()
            if any(e["type"] == "run/paused" for e in events):
                return
            await asyncio.sleep(0.05)
    raise AssertionError("run 未在预期时间内暂停在 ask_user")


async def _recv_snapshot(ws, timeout: float = 5.0) -> dict:
    async def _drain() -> dict:
        while True:
            frame = json.loads(await ws.receive_text())
            if frame.get("type") == "snapshot":
                return frame

    return await asyncio.wait_for(_drain(), timeout=timeout)


@pytest.mark.asyncio
async def test_ask_user_timeout_resumes_when_connected_zero_answer(tmp_path, monkeypatch):
    """有连接 + 到点 ⇒ run 自动继续、注入票面消息、**不写任何 answer**。"""
    server, serve_task, port, app = await _start_server(
        tmp_path, monkeypatch, _ask_then_finish_factory(_ask_user_scripts()),
    )
    try:
        driver = app.state.agent.ask_user_timeout_driver
        # 把驱动的档位与挂钟换成测试可控（装配发生在 WS subscribe 那一刻）。
        clock = FakeClock()
        driver.clock = clock
        driver.timeout_seconds = 60.0

        sid = await _start_run(port, "请设置存储层")
        await _wait_paused(port, sid)

        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            await ws.send_text(json.dumps({"type": "subscribe", "session_id": sid}))
            await _recv_snapshot(ws)
            assert driver.is_armed(sid), "订阅后应装配超时计时器"

            clock.now += 61.0
            assert await driver.check_expiries() == [sid]

            # 等恢复的 run 收口。
            for _ in range(200):
                events = (await client.get(
                    f"http://127.0.0.1:{port}/api/sessions/{sid}/events"
                )).json()
                if any(e["type"] == "run/completed" for e in events):
                    break
                await asyncio.sleep(0.05)

        answer_event = next(
            e for e in events
            if e["type"] == "user/message"
            and e["data"].get("input_request_id")
        )
        assert answer_event["data"]["input_request_answer"] == {
            "request_id": answer_event["data"]["input_request_id"],
            "timeout": True,
        }
        assert "questions" not in answer_event["data"]["input_request_answer"], \
            "超时恢复不得写任何 answer（不替用户选择）"
        assert any(e["type"] == "run/resumed" for e in events)
        assert any(e["type"] == "run/completed" for e in events)
        assert "用户在 5 分钟内未回答，可能不在，请按你的判断继续" in answer_event["data"]["content"]
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_subscribe_arms_timer_when_input_request_lands_after_snapshot_read(
    tmp_path, monkeypatch,
):
    """P2-1：`get_events` 返回旧快照、提问随后落盘 ⇒ 订阅仍须装配计时器。

    竞态原样：subscribe 处理先读事件流（无待回答）→ `note_session_state` 判"无需装配"
    → 之后 `user/input-requested` 才落盘。relay 只收到**订阅之后**扇出的事件，旧快照
    又不含提问 ⇒ 不补读就永远不会装配，提问永不超时、run 永久挂起（违反 AC②）。

    构造：把 `SessionService.get_events` 的**第一次**调用裁掉 `user/input-requested`
    （= 读到旧快照），其后返回真实事件流（= 补读能看到已落盘的提问）。
    """
    server, serve_task, port, app = await _start_server(
        tmp_path, monkeypatch, _ask_then_finish_factory(_ask_user_scripts()),
    )
    try:
        driver = app.state.agent.ask_user_timeout_driver
        clock = FakeClock()
        driver.clock = clock
        driver.timeout_seconds = 60.0

        sid = await _start_run(port, "请设置存储层")
        await _wait_paused(port, sid)  # 先等提问落盘（HTTP 读不走被裁的路径）

        from agent_harness.session.event import USER_INPUT_REQUESTED
        from agent_harness.session.service import SessionService

        real_get_events = SessionService.get_events
        stale_once = {"left": 1}

        async def get_events(self, session_id):
            events = await real_get_events(self, session_id)
            if stale_once["left"] and session_id == sid:
                stale_once["left"] -= 1
                return [e for e in events if e.type != USER_INPUT_REQUESTED]
            return events

        monkeypatch.setattr(SessionService, "get_events", get_events)

        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            await ws.send_text(json.dumps({"type": "subscribe", "session_id": sid}))
            await _recv_snapshot(ws)
            assert driver.is_armed(sid), (
                "快照读到的旧状态不含提问，订阅后必须补读事件流装配计时器"
            )
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_ask_user_timeout_does_not_fire_without_a_connection(tmp_path, monkeypatch):
    """无前端连接 ⇒ 不装配、不启动，推进挂钟 + tick 也不恢复。"""
    server, serve_task, port, app = await _start_server(
        tmp_path, monkeypatch, _ask_then_finish_factory(_ask_user_scripts()),
    )
    try:
        driver = app.state.agent.ask_user_timeout_driver
        clock = FakeClock()
        driver.clock = clock
        driver.timeout_seconds = 60.0

        sid = await _start_run(port, "请设置存储层")
        await _wait_paused(port, sid)
        # 从未 subscribe ⇒ 无连接在场。
        assert driver.is_armed(sid) is False

        clock.now += 10_000.0
        assert await driver.check_expiries() == []

        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client:
            events = (await client.get(
                f"http://127.0.0.1:{port}/api/sessions/{sid}/events"
            )).json()
        assert not any(e["type"] == "run/completed" for e in events), \
            "无连接时提问必须保持挂起，不自动恢复"
        assert not any(e["type"] == "run/resumed" for e in events)
    finally:
        await _shutdown(server, serve_task)
