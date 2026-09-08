"""续聊端点 + WebSocket 测试（T2 / #132）。

测试 POST /api/sessions/{id}/messages 续聊端点和 WebSocket /api/ws 基础设施。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app


@pytest.fixture
def app_and_client(tmp_path):
    """创建隔离的 FastAPI app + TestClient。"""
    settings = Settings(
        workspace_dir=str(tmp_path),
        model_api_key="test-key",
        model_name="test-model",
        model_provider="openai",
        enable_cors=True,
    )
    app = create_app(settings)
    client = TestClient(app)
    yield app, client


class TestSendMessageEndpoint:
    """POST /api/sessions/{id}/messages 续聊端点。"""

    def test_messages_endpoint_not_found(self, app_and_client):
        """不存在的 session → 404。"""
        _, client = app_and_client
        response = client.post(
            "/api/sessions/nonexistent-uuid/messages",
            json={"content": "hello", "mode": "queue"},
        )
        assert response.status_code == 404

    def test_messages_endpoint_invalid_mode(self, app_and_client):
        """mode 不合法 → 422。"""
        _, client = app_and_client
        response = client.post(
            "/api/sessions/some-id/messages",
            json={"content": "hello", "mode": "invalid"},
        )
        assert response.status_code == 422

    def test_messages_endpoint_empty_content(self, app_and_client):
        """content 为空 → 422。"""
        _, client = app_and_client
        response = client.post(
            "/api/sessions/some-id/messages",
            json={"content": "", "mode": "queue"},
        )
        assert response.status_code == 422

    def test_messages_endpoint_unsafe_session_id(self, app_and_client):
        """session_id 含反斜杠 → 422（路径逃逸防护）。

        正斜杠在 URL 路径里会被 FastAPI 拆段，测不到校验；
        反斜杠作为单个段传入 → service 层 InvalidSessionId → 422。
        """
        _, client = app_and_client
        response = client.post(
            "/api/sessions/a\\b/messages",
            json={"content": "hello", "mode": "queue"},
        )
        assert response.status_code == 422


class TestQueueCancelEndpoint:
    """POST /api/sessions/{id}/queue/{queue_id}/cancel。"""

    def test_cancel_queue_not_found(self, app_and_client):
        """不存在的 session → 404。"""
        _, client = app_and_client
        response = client.post(
            "/api/sessions/nonexistent/queue/fake-queue-id/cancel",
        )
        assert response.status_code == 404


class TestWebSocketBasics:
    """WebSocket /api/ws 基础设施。"""

    def test_ws_connect_and_ping_pong(self, app_and_client):
        """WS 建连 + ping/pong 心跳。"""
        _, client = app_and_client
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({"type": "ping"}))
            msg = ws.receive_text()
            payload = json.loads(msg)
            assert payload["type"] == "pong"

    def test_ws_subscribe_nonexistent_session(self, app_and_client):
        """subscribe 不存在的 session → error。"""
        _, client = app_and_client
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({"type": "subscribe", "session_id": "nonexistent"}))
            msg = ws.receive_text()
            payload = json.loads(msg)
            assert payload["type"] == "error"

    def test_ws_invalid_json(self, app_and_client):
        """非 JSON 消息 → error。"""
        _, client = app_and_client
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text("not json at all")
            msg = ws.receive_text()
            payload = json.loads(msg)
            assert payload["type"] == "error"
            assert "invalid JSON" in payload["message"]

    def test_ws_unknown_message_type_ignored(self, app_and_client):
        """未知 type → 无响应（不报错，连接保持）。"""
        _, client = app_and_client
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({"type": "unknown_command"}))
            ws.send_text(json.dumps({"type": "ping"}))
            msg = ws.receive_text()
            payload = json.loads(msg)
            assert payload["type"] == "pong"


class TestTypedEvents:
    """新 typed SessionEvent 词汇表注册。"""

    def test_new_event_types_in_vocabulary(self):
        """4 个新事件类型都在 EVENT_TYPES 里（durable 词汇表）。"""
        from agent_harness.session.event import EVENT_TYPES

        assert "message/queued" in EVENT_TYPES
        assert "queue/cancelled" in EVENT_TYPES
        assert "steer/requested" in EVENT_TYPES
        assert "steer/applied" in EVENT_TYPES

    def test_new_event_constants_exist(self):
        """常量导出可用。"""
        from agent_harness.session.event import (
            MESSAGE_QUEUED,
            QUEUE_CANCELLED,
            STEER_APPLIED,
            STEER_REQUESTED,
        )

        assert MESSAGE_QUEUED == "message/queued"
        assert QUEUE_CANCELLED == "queue/cancelled"
        assert STEER_REQUESTED == "steer/requested"
        assert STEER_APPLIED == "steer/applied"
