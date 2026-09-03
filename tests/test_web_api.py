"""Web API 契约测试（Phase 9/10 精简版）。

覆盖范围：
- /api/health 总是 200
- /api/sessions 列历史（空 + 非空）
- /api/sessions/{id}/events 读历史（404 + ok）
- /api/sessions POST 起新 session 并拿到 SSE 流
- /api/sessions/{id}/approve 返回 202 seam

用 TestClient（同步接口）+ 一个 stub runtime 工厂注入，
避免测试里真起模型 + 真跑工具——契约测试只关心 HTTP 行为和事件形状。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.config import Settings
from agent_harness.session import (
    SESSION_STARTED,
    RUN_STARTED,
    JsonlSessionStore,
    Session,
)
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel


@pytest.fixture
def app(tmp_path):
    """用 tmp workspace 创建 app，避免污染真实 .agent/。"""
    settings = Settings(workspace_dir=str(tmp_path))
    return create_app(settings, enable_cors=False)


@pytest.fixture
def client(app):
    return TestClient(app)


# ── health ──


def test_health_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── sessions list ──


def test_list_sessions_empty(client):
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_sessions_returns_existing(tmp_path):
    """先往 store 写一个 session，列表应返回。"""
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store
    session = Session.start(store)
    session.append(event_type=SESSION_STARTED, data={"reason": "test"})
    client = TestClient(app)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["session_id"] == session.session_id
    assert body[0]["event_count"] >= 1


# ── session events ──


def test_get_events_404(client):
    resp = client.get("/api/sessions/nonexistent/events")
    assert resp.status_code == 404


def test_get_events_ok(tmp_path):
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store
    session = Session.start(store)
    client = TestClient(app)
    resp = client.get(f"/api/sessions/{session.session_id}/events")
    assert resp.status_code == 200
    events = resp.json()
    assert isinstance(events, list)
    assert all("type" in e for e in events)
    assert any(e["type"] == SESSION_STARTED for e in events)


# ── create session (SSE) ──


def test_create_session_streams_sse(client):
    """POST /api/sessions 应返回 text/event-stream 并流多帧 AgentEvent。"""
    # 用 stub runtime 避免 LLM 调用：patch _build_runtime 返回一个轻量 runtime
    settings = Settings(workspace_dir=str(client.app.state.agent.settings.workspace_dir))
    reg = ToolRegistry()
    exe = ToolExecutor(reg)
    stub_runtime = AgentRuntime(
        model=ScriptedModel(responses=[AIMessage(content="hello from stub")]),
        registry=reg,
        executor=exe,
        max_steps=2,
    )
    with patch("agent_harness.web.app._build_runtime", return_value=stub_runtime):
        resp = client.post(
            "/api/sessions",
            json={"task": "say hi", "max_steps": 2},
        )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    body = resp.text
    # SSE 帧以 "data: " 开头；至少应有一个 model/completed
    frames = [line[len("data:"):].strip() for line in body.splitlines() if line.startswith("data:")]
    assert len(frames) > 0
    parsed = [json.loads(f) for f in frames]
    types = [e["type"] for e in parsed]
    assert "run/started" in types
    assert "model/completed" in types
    # 每个 durable event 都带 seq
    durable = [e for e in parsed if e.get("seq") is not None]
    assert durable, "expected at least one durable event with seq"
    # 每帧都带 session_id（前端依赖它在首帧就切换 selectedId）
    session_ids = {e.get("session_id") for e in parsed}
    assert len(session_ids) == 1, f"expected one consistent session_id, got {session_ids}"
    assert next(iter(session_ids)), "session_id must be non-null"


# ── approve seam ──


def test_approve_endpoint_returns_seam(client):
    resp = client.post("/api/sessions/abc/approve", json={"approved": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert "auto-approve" in body["note"]


# ── client disconnect → producer 取消（spec 11 §4）──
#
# 断连契约：客户端断开连接后，server 端必须让 SSE producer 收到取消信号，
# 不能让 runtime.run_stream() 的 async generator 永远挂住——否则在真实 uvicorn 下
# 是泄漏的 task + 持有中的 session 锁。EventSourceResponse 内部把 disconnect 翻译成
# generator 的 GeneratorExit / CancelledError，本测试用 stub runtime 观察 finally 触发。
#
# 不能用 starlette TestClient / httpx ASGITransport：两者都是 await app(...) 整体跑完
# 才返回，且 http.disconnect 只在 response 完成后才投递——挂住的 producer 会直接死锁。
# 下面是一个最小 ASGI 替身：像真实 uvicorn 一样，客户端断开时在 receive 通道投递
# http.disconnect，EventSourceResponse 收到后取消它 spawn 出来的 event_generator task。


class _DisconnectingASGI:
    """模拟 uvicorn 在客户端中途断连时的 ASGI 行为。

    只实现能驱动 EventSourceResponse 跑起来的最小路径：app 发出 http.response.start
    后，置位 disconnected，下一次 receive() 投递 http.disconnect。
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        request_sent = asyncio.Event()
        disconnected = asyncio.Event()

        async def downstream_receive() -> dict:
            # 先把 request body 一次性发完（POST /api/sessions 有 JSON body），
            # 之后等 start 发出、disconnected 置位，再投递 http.disconnect。
            if not request_sent.is_set():
                request_sent.set()
                return {"type": "http.request", "body": b'{"task":"hi"}', "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def upstream_send(message: dict) -> None:
            # 首帧一出就触发断连——给 app 一点时间让 EventSourceResponse 进入
            # 它的接收循环，然后置位。丢弃 body（我们不关心内容）。
            if message["type"] == "http.response.start":
                await asyncio.sleep(0)  # 让控制权回到事件循环
                disconnected.set()

        app_task = asyncio.create_task(self._app(scope, downstream_receive, upstream_send))
        try:
            await asyncio.wait_for(app_task, timeout=5.0)
        except asyncio.TimeoutError:
            app_task.cancel()
            with contextlib.suppress(BaseException):
                await app_task
            raise AssertionError(
                "app 未在 5s 内响应 disconnect——EventSourceResponse 没有正确取消 producer"
            )


@pytest.mark.asyncio
async def test_disconnect_cancels_sse_producer(tmp_path):
    """客户端断连后 SSE producer 必须被取消（spec 11 §4 第 2/3 条）。

    用一个永不自然完成的 stub runtime：它只在被取消（GeneratorExit/CancelledError）
    时置位 finally 旗标。如果断连信号没传到 generator，旗标永远是 False，断言失败。
    """
    cancelled = asyncio.Event()

    class HangingRuntime:
        """run_stream 永远挂住，只有被取消时才退 finally——观察取消信号是否到达。"""

        async def run_stream(self, session: Any, task: str) -> AsyncIterator[AgentEvent]:
            try:
                await asyncio.Event().wait()  # 永不 set，模拟 agent loop 永远在等模型
                yield AgentEvent(type=RUN_STARTED)  # pragma: no cover — 永不执行到这里
            finally:
                cancelled.set()

    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)

    with patch("agent_harness.web.app._build_runtime", return_value=HangingRuntime()):
        transport = _DisconnectingASGI(app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/sessions",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "query_string": b"",
        }
        await transport(scope, None, None)

    # 关键断言：producer 的 finally 已触发——断连信号确实传到了 generator。
    assert cancelled.is_set(), (
        "客户端断连后 SSE producer 未被取消——spec 11 §4 的 disconnect 清理未实现，"
        "这会在真实 uvicorn 下泄漏 task + 持有 session 锁。"
    )
