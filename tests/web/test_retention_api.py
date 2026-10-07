"""#368 [W-24]：会话保留 / 空间显示 / 显式清理的 REST 传输面。

GET /api/sessions/{id}/usage（空间显示）；POST …/cleanup/preview（快照 token +
affected/blocked）；POST …/cleanup/execute（token 复验；来源闸）。

错误码口径（spec 11 §6.1 + T4/#312 先例）：形状非法 422、状态对不上 409。
领域层见 tests/session/test_retention.py；fixture 策略对齐
tests/web/test_evidence_api.py（patch build_runtime/launch，
`POST /api/sessions?launch=false` 造空会话再打新端点）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import service as service_module
from agent_harness.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def _fake_build(**kwargs):
        return object()

    monkeypatch.setattr(service_module, "build_runtime", _fake_build)

    from agent_harness.session.runmanager import RunManager, Subscriber

    launched: list = []

    class _FakeRun:
        task = None

        def unsubscribe(self, _sub):
            pass

    def _fake_launch(self, session, runtime, user_input, user_input_metadata=None):
        launched.append(session)
        sub = Subscriber()
        sub.queue.put_nowait(self.DONE)
        return _FakeRun(), sub

    monkeypatch.setattr(RunManager, "launch", _fake_launch)

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
        enable_cors=False,
    )
    app = create_app(settings, enable_cors=False)
    app.state._launched_sessions = launched
    return TestClient(app), app


def _create_session(client) -> str:
    resp = client.post("/api/sessions?launch=false", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


class TestSessionUsage:
    def test_usage_shape(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        resp = client.get(f"/api/sessions/{sid}/usage")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for key in (
            "events_bytes",
            "artifacts_bytes",
            "progress_bytes",
            "artifact_count",
            "reclaimable_bytes",
            "computed_at",
        ):
            assert key in body, f"usage 缺少字段 {key}"
        assert body["events_bytes"] > 0
        assert body["computed_at"]

    def test_invalid_session_id_is_422(self, client) -> None:
        client, _ = client
        resp = client.get("/api/sessions/bad.id/usage")
        assert resp.status_code == 422, resp.text


class TestCleanupPreview:
    def test_preview_shape(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        resp = client.post(
            f"/api/sessions/{sid}/cleanup/preview", json={"mode": "unreferenced"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for key in (
            "snapshot_token",
            "affected",
            "evidence_invalidated",
            "reclaimable_bytes",
            "blocked",
        ):
            assert key in body, f"preview 缺少字段 {key}"
        assert isinstance(body["snapshot_token"], str) and body["snapshot_token"]

    def test_preview_defaults_mode(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        resp = client.post(f"/api/sessions/{sid}/cleanup/preview", json={})
        assert resp.status_code == 200, resp.text

    def test_preview_rejects_unknown_mode_is_422(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        resp = client.post(
            f"/api/sessions/{sid}/cleanup/preview", json={"mode": "all"}
        )
        assert resp.status_code == 422, resp.text

    def test_preview_nonexistent_session_is_404(self, client) -> None:
        client, _ = client
        resp = client.post(
            "/api/sessions/nonexistent-session-xyz/cleanup/preview", json={}
        )
        assert resp.status_code == 404, resp.text


class TestCleanupExecute:
    def _preview_token(self, client, sid: str) -> str:
        resp = client.post(f"/api/sessions/{sid}/cleanup/preview", json={})
        assert resp.status_code == 200, resp.text
        return resp.json()["snapshot_token"]

    def test_execute_shape(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        token = self._preview_token(client, sid)
        resp = client.post(
            f"/api/sessions/{sid}/cleanup/execute",
            json={"snapshot_token": token, "artifact_refs": []},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for key in ("deleted", "failed", "not_deleted"):
            assert key in body, f"execute 缺少字段 {key}"

    def test_execute_wrong_token_is_409(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        self._preview_token(client, sid)
        resp = client.post(
            f"/api/sessions/{sid}/cleanup/execute",
            json={"snapshot_token": "0" * 64, "artifact_refs": []},
        )
        assert resp.status_code == 409, resp.text

    def test_execute_cross_origin_is_403(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        token = self._preview_token(client, sid)
        resp = client.post(
            f"/api/sessions/{sid}/cleanup/execute",
            json={"snapshot_token": token, "artifact_refs": []},
            headers={"Origin": "http://evil.example"},
        )
        assert resp.status_code == 403, resp.text
