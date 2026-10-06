"""#360 W-17：POST /api/sessions/{id}/client-exit 端点 TDD。"""
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "ws"),
model_api_key="sk-test",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


class TestClientExitEndpoint:
    def test_invalid_session_id_422(self, client, tmp_path: Path):
        r = client.post("/api/sessions/bad!id/client-exit")
        assert r.status_code == 422, r.text

    def test_unknown_session_ignored_not_managed(self, client, tmp_path: Path):
        # 无在途 run → ignored_not_managed（幂等零副作用，不 404）
        r = client.post("/api/sessions/nonexistent123/client-exit")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == "nonexistent123"
        assert body["status"] == "ignored_not_managed"
