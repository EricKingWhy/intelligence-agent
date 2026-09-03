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

import json
from typing import AsyncIterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.config import Settings
from agent_harness.session import SESSION_STARTED, JsonlSessionStore, Session
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


# ── approve seam ──


def test_approve_endpoint_returns_seam(client):
    resp = client.post("/api/sessions/abc/approve", json={"approved": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert "auto-approve" in body["note"]
