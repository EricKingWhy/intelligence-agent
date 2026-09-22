"""F18-A (#282)：`POST /api/sessions/{id}/permission` 端点（200 / 422 / 404 / 409）。

对齐既有 `POST /api/sessions/{id}/model`（`tests/web/test_web_model_fork.py`）：
只写事件、不打断在途 run；下一轮 run 从事件流派生新档生效。

策略：patch `service.build_runtime` + `RunManager.launch`（避免真实装配与 run），
用 `POST /api/sessions?launch=false` 造一个空会话，再打新端点。

领域层见 `tests/session/test_permission_change.py`。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import service as service_module
from agent_harness.session.event import SESSION_STARTED
from agent_harness.web.app import create_app


class _FakeRun:
    task = None

    def unsubscribe(self, _sub):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def _fake_build(**kwargs):
        return object()

    monkeypatch.setattr(service_module, "build_runtime", _fake_build)

    from agent_harness.session.runmanager import RunManager, Subscriber

    def _fake_launch(self, session, runtime, user_input):
        sub = Subscriber()
        sub.queue.put_nowait(self.DONE)
        return _FakeRun(), sub

    monkeypatch.setattr(RunManager, "launch", _fake_launch)

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        enable_cors=False,
    )
    app = create_app(settings, enable_cors=False)
    return TestClient(app), app


def _create_session(client, app, *, permission_mode: str = "read-only") -> str:
    resp = client.post(
        "/api/sessions?launch=false",
        json={"permission_mode": permission_mode},
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    started = app.state.agent.store.read_events(session_id)[0]
    assert started.type == SESSION_STARTED
    assert started.data["permission_mode"] == permission_mode
    return session_id


class TestChangeSessionPermission:
    def test_200_appends_event_and_returns_effective_values(self, client):
        client, app = client
        session_id = _create_session(client, app)

        resp = client.post(
            f"/api/sessions/{session_id}/permission",
            json={"permission_mode": "workspace-write", "auto_approve": False},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "status": "changed",
            "permission_mode": "workspace-write",
            "auto_approve": False,
        }
        events = app.state.agent.store.read_events(session_id)
        assert events[-1].type == "permission/changed"
        assert events[-1].data == {
            "permission_mode": "workspace-write",
            "auto_approve": False,
        }

    def test_200_is_bi_directional(self, client):
        """降档无需任何额外字段（升档确认是前端 F18-B 的责任）。"""
        client, app = client
        session_id = _create_session(client, app, permission_mode="danger-full-access")

        resp = client.post(
            f"/api/sessions/{session_id}/permission",
            json={"permission_mode": "read-only", "auto_approve": True},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["permission_mode"] == "read-only"

    def test_422_unknown_permission_mode(self, client):
        client, app = client
        session_id = _create_session(client, app)

        resp = client.post(
            f"/api/sessions/{session_id}/permission",
            json={"permission_mode": "delete-everything", "auto_approve": True},
        )

        assert resp.status_code == 422, resp.text

    def test_422_missing_auto_approve(self, client):
        """两件事必须一起给（冻结决策 2）：漏 auto_approve → 422，不静默取默认值。"""
        client, app = client
        session_id = _create_session(client, app)

        resp = client.post(
            f"/api/sessions/{session_id}/permission",
            json={"permission_mode": "read-only"},
        )

        assert resp.status_code == 422, resp.text

    def test_404_missing_session(self, client):
        client, _ = client

        resp = client.post(
            "/api/sessions/does-not-exist/permission",
            json={"permission_mode": "read-only", "auto_approve": True},
        )

        assert resp.status_code == 404, resp.text

    def test_422_invalid_session_id(self, client):
        client, _ = client

        resp = client.post(
            "/api/sessions/..%2F..%2Fetc/permission",
            json={"permission_mode": "read-only", "auto_approve": True},
        )

        assert resp.status_code in (404, 422), resp.text

    def test_409_pending_approval(self, client):
        """冻结决策 5：有未裁决审批 → 409（先裁决再改档）。"""
        client, app = client
        session_id = _create_session(client, app)
        queue = MagicMock()
        queue.pending_ids = MagicMock(return_value=["approval-1"])
        app.state.agent.approval_queues[session_id] = queue

        resp = client.post(
            f"/api/sessions/{session_id}/permission",
            json={"permission_mode": "workspace-write", "auto_approve": True},
        )

        assert resp.status_code == 409, resp.text
        # 409 时不得落事件
        events = app.state.agent.store.read_events(session_id)
        assert all(e.type != "permission/changed" for e in events)

    def test_200_when_queue_has_no_pending(self, client):
        client, app = client
        session_id = _create_session(client, app)
        queue = MagicMock()
        queue.pending_ids = MagicMock(return_value=[])
        app.state.agent.approval_queues[session_id] = queue

        resp = client.post(
            f"/api/sessions/{session_id}/permission",
            json={"permission_mode": "workspace-write", "auto_approve": True},
        )

        assert resp.status_code == 200, resp.text
