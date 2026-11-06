"""Web diagnostics for sandbox hardening failures."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.sandbox.capabilities import SandboxUnavailableError
from agent_harness.sandbox.docker import DockerSandbox, SandboxHardeningError
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.web.app import create_app


def test_create_reports_volume_hardening_failure(tmp_path, monkeypatch):
    def fail_create(self, *args, **kwargs):
        raise SandboxHardeningError(
            "token=secret123 /workspace/private-file",
            volume_name="test-volume", reason="migration_failed",
        )

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
    assert detail["volume"] == "test-volume"
    assert detail["reason"] == "migration_failed"
    assert "secret123" not in response.text
    assert "/workspace/private-file" not in response.text
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
            raise SandboxHardeningError(
                "private migration log", volume_name="resume-volume", reason="migration_failed"
            )

        monkeypatch.setattr(WorkspaceRegistry, "get", fail_get)
        response = client.post(f"/api/sessions/{sid}/resume", json={"task": "continue"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["volume"] == "resume-volume"
    assert detail["reason"] == "migration_failed"
    assert "private migration log" not in response.text
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


@pytest.mark.parametrize(
    ("phase", "expected_reason"),
    [("start", "migration_helper_start_failed"), ("wait", "migration_wait_failed"), ("exit", "migration_failed")],
)
def test_real_migration_failure_is_safe_and_cleans_up(tmp_path, monkeypatch, phase, expected_reason):
    import docker

    removed = []
    main_container = SimpleNamespace(remove=lambda **kwargs: removed.append("main"))
    def wait(**kwargs):
        if phase == "wait":
            raise TimeoutError("token=secret123 /workspace/private-file")
        return {"StatusCode": 1}

    helper = SimpleNamespace(
        wait=wait,
        logs=lambda **kwargs: b"MIGRATE_ERROR token=secret123 /workspace/private-file",
        remove=lambda **kwargs: removed.append("helper"),
        stop=lambda **kwargs: removed.append("stop"),
    )

    def absent(name):
        raise docker.errors.NotFound("not found")

    def run(*args, **kwargs):
        if phase == "start":
            raise RuntimeError("token=secret123 /workspace/private-file")
        return helper

    fake_client = SimpleNamespace(
        containers=SimpleNamespace(get=absent, create=lambda *a, **k: main_container, run=run, list=lambda **k: []),
        images=SimpleNamespace(get=lambda image: SimpleNamespace(attrs={"Config": {"User": "1000:1000"}})),
        volumes=SimpleNamespace(get=lambda name: object()),
    )
    monkeypatch.setattr(docker, "from_env", lambda **kwargs: fake_client)

    def fail_create(self, *args, **kwargs):
        sandbox = DockerSandbox(image="test-image", container_name="test-container", volume_name="test-volume")
        sandbox.ensure_started()

    monkeypatch.setattr(WorkspaceRegistry, "create", fail_create)
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/sessions?launch=false", json={"sandbox_backend": "local"})
    assert response.status_code == 409
    assert response.json()["detail"]["volume"] == "test-volume"
    assert response.json()["detail"]["reason"] == expected_reason
    assert "secret123" not in response.text
    assert "/workspace/private-file" not in response.text
    assert removed == {"start": ["main"], "wait": ["stop", "helper", "main"], "exit": ["helper", "main"]}[phase]


def test_untrusted_metadata_is_not_echoed(tmp_path, monkeypatch):
    def fail_create(self, *args, **kwargs):
        raise SandboxHardeningError("secret", volume_name="/private/path", reason="secret-category")

    monkeypatch.setattr(WorkspaceRegistry, "create", fail_create)
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        allow_testserver=True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/sessions?launch=false", json={"sandbox_backend": "local"})
    assert response.status_code == 409
    assert response.json()["detail"]["volume"] is None
    assert response.json()["detail"]["reason"] == "hardening_failed"
    assert "secret" not in response.text
    assert "/private/path" not in response.text
