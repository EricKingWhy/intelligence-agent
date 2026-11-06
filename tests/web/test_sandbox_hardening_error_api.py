"""Web diagnostics for sandbox hardening failures."""

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.sandbox.capabilities import SandboxUnavailableError
from agent_harness.sandbox.docker import SandboxHardeningError
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.web.app import create_app


def test_create_reports_volume_hardening_failure(tmp_path, monkeypatch):
    def fail_create(self, *args, **kwargs):
        raise SandboxHardeningError("volume test-volume migration failed: permission denied")

    monkeypatch.setattr(WorkspaceRegistry, "create", fail_create)
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/sessions?launch=false", json={"sandbox_backend": "local"}
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "test-volume" in detail["message"]
    assert "permission denied" in detail["message"]
    assert detail["remediation"]


def test_resume_reports_hardening_failure(tmp_path, monkeypatch):
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        created = client.post(
            "/api/sessions?launch=false", json={"sandbox_backend": "local"}
        )
        assert created.status_code == 200
        sid = created.json()["session_id"]

        def fail_get(self, *args, **kwargs):
            raise SandboxHardeningError("volume resume-volume migration failed")

        monkeypatch.setattr(WorkspaceRegistry, "get", fail_get)
        response = client.post(f"/api/sessions/{sid}/resume", json={"task": "continue"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "resume-volume" in detail["message"]
    assert detail["remediation"]


def test_unavailable_and_unexpected_errors_keep_existing_status(tmp_path, monkeypatch):
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        def unavailable(self, *args, **kwargs):
            raise SandboxUnavailableError("docker", "not running", remediation="start Docker")

        monkeypatch.setattr(WorkspaceRegistry, "create", unavailable)
        response = client.post("/api/sessions?launch=false", json={"sandbox_backend": "local"})
        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "not running"
        assert response.json()["detail"]["remediation"] == "start Docker"

        def unexpected(self, *args, **kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(WorkspaceRegistry, "create", unexpected)
        response = client.post("/api/sessions?launch=false", json={"sandbox_backend": "local"})
        assert response.status_code == 500
        assert response.json() == {"detail": "Internal Server Error"}
