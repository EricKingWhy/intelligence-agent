"""#1220/#1320：`ask_user` 超时闸**在线驱动**的全链路集成（真实 uvicorn + WS/HTTP）。

覆盖票面验收：
- 有前端连接（WS subscribe 一个暂停在 ask_user 的会话）⇒ 到点自动恢复、注入票面消息、
  **零 answer 落盘**；
- **无任何前端连接**（真实 Web 前端的 HTTP 轮询形态）⇒ 提问落盘即装表、组合根 ticker
  到点推进、照常恢复——#1320 的核心口径（推翻 #1220 复审 A1/A3）；
- relay / 订阅补读路径仍读请求载荷快照。

接缝：真实 uvicorn ASGI 服务器 + httpx2 WebSocket 客户端 + ScriptedModel 剧本桩。
装配来自 `RunManager` 的会话事件观察者（#1320，不看连接数），推进来自 lifespan 里的
常驻 ticker；到点经 `SessionService.resume_ask_user_timeout` 走既有 user_input 恢复链。
测试把 ticker 周期调大（默认 3600s），需要真实 ticker 的用例单独传小值。
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


class _GatedAskModel(ScriptedModel):
    """首发前等闸门：让测试先建立 WS 订阅，再让 `ask_user` 经 relay 流经。"""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(responses=[AIMessage(content="", tool_calls=[{
            "id": "ask-1", "name": "ask_user",
            "args": {"questions": _QUESTIONS},
        }])])
        self._gate = gate

    async def astream(self, messages, **kwargs):
        await self._gate.wait()
        async for chunk in super().astream(messages, **kwargs):
            yield chunk


def _gated_ask_then_finish_factory(gate: asyncio.Event):
    """首个 runtime 用闸门模型（relay 路径），超时续跑用普通剧本。"""
    models: list[ScriptedModel] = [
        _GatedAskModel(gate),
        ScriptedModel([AIMessage(content="Continued after timeout.")]),
    ]

    def factory(config, **kwargs):
        return models.pop(0)

    return factory


async def _start_server(
    tmp_path, monkeypatch, factory, *, ask_user_timeout: int = 300,
    tick_seconds: float = 3600.0,
):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", factory)
    # 心跳只负责死对端检测；本文件自己驱动超时检查，调大以免与显式 tick 打架。
    monkeypatch.setattr("agent_harness.web.websocket.WS_PING_INTERVAL", 3600.0)
    # #1320：超时推进在组合根 ticker；默认调大，需要真实节拍的用例单独传小值。
    monkeypatch.setattr(
        "agent_harness.web.app._ASK_USER_TIMEOUT_TICK_SECONDS", tick_seconds,
    )
    # #1284：档位经 `ask_user` 工具进请求载荷快照，到期链路只认快照
    # （驱动 `timeout_seconds` 仅是旧载荷的兜底）——所以这里从 Settings 设档。
    app = create_app(Settings(
        _env_file=None, workspace_dir=str(tmp_path),
        model_api_key="sk-test", enable_cors=False,
        ask_user_question_timeout=ask_user_timeout,
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
        ask_user_timeout=60,
    )
    try:
        driver = app.state.agent.ask_user_timeout_driver
        # 挂钟换成测试可控；档位走请求快照（Settings 60s ⇒ 载荷 60s）。
        clock = FakeClock()
        driver.clock = clock

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
        assert "用户在 1 分钟内未回答，可能不在，请按你的判断继续" in answer_event["data"]["content"]
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_relay_armed_request_uses_the_payload_snapshot(tmp_path, monkeypatch):
    """#1284 复审 P2 / #1320：事件驱动装配必须读请求载荷快照，而不是 driver 启动配置。

    构造：Settings=60s（载荷快照），driver 回退值故意设为 600s。先订阅再放行模型，
    让 `ask_user` 在订阅之后落盘（事件观察者路径，原 relay 路径同源）。
    若仍读启动配置，+61s 不会到期。
    """
    gate = asyncio.Event()
    server, serve_task, port, app = await _start_server(
        tmp_path, monkeypatch, _gated_ask_then_finish_factory(gate),
        ask_user_timeout=60,
    )
    try:
        driver = app.state.agent.ask_user_timeout_driver
        clock = FakeClock()
        driver.clock = clock
        driver.timeout_seconds = 600.0  # 旧事件回退值；快照 60s 必须赢

        sid = await _start_run(port, "请设置存储层")

        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client, client.websocket(
            f"ws://127.0.0.1:{port}/api/ws"
        ) as ws:
            await ws.send_text(json.dumps({"type": "subscribe", "session_id": sid}))
            await _recv_snapshot(ws)
            gate.set()
            await _wait_paused(port, sid)
            for _ in range(100):
                if driver.is_armed(sid):
                    break
                await asyncio.sleep(0.02)
            assert driver.is_armed(sid), "事件观察者流经 ask_user 后应装配计时器"

            clock.now += 61.0
            assert await driver.check_expiries() == [sid]

            # 等恢复的 run 收口（与首用例同一纪律）：`resume_and_launch` 立即返回，
            # 直接进 `finally` 会与在途 detached run 抢跑。
            for _ in range(200):
                events = (await client.get(
                    f"http://127.0.0.1:{port}/api/sessions/{sid}/events"
                )).json()
                if any(e["type"] == "run/completed" for e in events):
                    break
                await asyncio.sleep(0.05)
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_subscribe_arms_timer_when_input_request_lands_after_snapshot_read(
    tmp_path, monkeypatch,
):
    """P2-1 回归：订阅读到旧快照也不得漏装配（#1320 后由事件观察者兜住）。

    竞态原样：subscribe 处理先读事件流（无待回答）→ `note_session_state` 判"无需装配"
    → 之后 `user/input-requested` 才落盘。旧实现在这里依赖订阅后补读才不漏；#1320 的
    事件观察者在落盘当刻就装表，补读仍保留为额外兜底。

    构造：把 `SessionService.get_events` 的**第一次**调用裁掉 `user/input-requested`
    （= 读到旧快照），其后返回真实事件流。
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
async def test_ask_user_timeout_fires_without_any_connection(tmp_path, monkeypatch):
    """#1320 场景 A/B/C：无任何 WS 连接（HTTP 轮询前端）⇒ 到点仍自动恢复。

    这是旧测试 `..._does_not_fire_without_a_connection` 的**反向改写**：旧断言
    "无连接不触发"正是 #1320 要推翻的口径——真实 Web 前端在 `run/paused` 上从不
    订阅 WS（`runState.ts` 把 `run/paused` 计为收口），只走 HTTP 轮询。
    """
    server, serve_task, port, app = await _start_server(
        tmp_path, monkeypatch, _ask_then_finish_factory(_ask_user_scripts()),
        ask_user_timeout=60, tick_seconds=0.2,
    )
    try:
        driver = app.state.agent.ask_user_timeout_driver
        clock = FakeClock()
        driver.clock = clock

        sid = await _start_run(port, "请设置存储层")
        await _wait_paused(port, sid)
        # 从未 subscribe（也无任何 WS 连接）：事件观察者已在 user/input-requested
        # 落盘时装表（#1320）。
        assert driver.is_armed(sid) is True

        clock.now += 61.0
        # 生产接线：lifespan 里的常驻 ticker 每 0.2s 推进一次，无需手工 tick。
        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client:
            events: list[dict] = []
            for _ in range(200):
                events = (await client.get(
                    f"http://127.0.0.1:{port}/api/sessions/{sid}/events"
                )).json()
                if any(e["type"] == "run/completed" for e in events):
                    break
                await asyncio.sleep(0.05)

        assert any(e["type"] == "run/resumed" for e in events), "零连接也必须到点恢复"
        answer_event = next(
            e for e in events
            if e["type"] == "user/message"
            and e["data"].get("input_request_id")
        )
        assert answer_event["data"]["input_request_answer"] == {
            "request_id": answer_event["data"]["input_request_id"],
            "timeout": True,
        }
        assert "用户在 1 分钟内未回答，可能不在，请按你的判断继续" in (
            answer_event["data"]["content"]
        )
    finally:
        await _shutdown(server, serve_task)
