import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app


def test_status_and_explicit_reload_are_visible_without_session_events(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    workspace = repository / "workspace"
    workspace.mkdir()
    instruction_file = repository / "AGENTS.md"
    instruction_file.write_text("first version", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "harness"),
        artifact_dir="",
        model_api_key="test-key",
    )

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/sessions",
            params={"launch": "false"},
            json={"cwd": str(workspace)},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        events_before = client.get(f"/api/sessions/{session_id}/events").json()

        before = client.get(
            f"/api/sessions/{session_id}/project-instructions",
        )
        first_reload = client.post(
            f"/api/sessions/{session_id}/project-instructions/reload",
        )
        instruction_file.write_text("second version", encoding="utf-8")
        after_change = client.get(
            f"/api/sessions/{session_id}/project-instructions",
        )
        second_reload = client.post(
            f"/api/sessions/{session_id}/project-instructions/reload",
        )
        events_after = client.get(f"/api/sessions/{session_id}/events").json()

    assert before.status_code == 200
    assert before.json()["status"] == "loaded"
    assert before.json()["source_paths"] == [str(instruction_file)]
    assert first_reload.status_code == 200
    assert first_reload.json()["status"] == "loaded"
    assert first_reload.json()["source_paths"] == [str(instruction_file)]
    assert after_change.json() == first_reload.json()
    assert second_reload.status_code == 200
    assert second_reload.json()["status"] == "loaded"
    assert second_reload.json()["source_paths"] == [str(instruction_file)]
    assert events_after == events_before


def test_reload_exposes_missing_and_unreadable_instruction_files(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    workspace = repository / "workspace"
    workspace.mkdir()
    bad_instruction = repository / "AGENTS.md"
    bad_instruction.mkdir()
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "harness"),
        artifact_dir="",
        model_api_key="test-key",
    )

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/sessions",
            params={"launch": "false"},
            json={"cwd": str(workspace)},
        )
        session_id = created.json()["session_id"]
        status = client.post(
            f"/api/sessions/{session_id}/project-instructions/reload",
        )

    assert status.status_code == 200
    assert status.json()["status"] == "unreadable"
    assert status.json()["unreadable_sources"][0]["path"] == str(bad_instruction)


def test_docker_reload_reads_private_workspace_instructions(tmp_path: Path) -> None:
    try:
        docker = importlib.import_module("docker")
        docker_client = docker.from_env(use_context=False)
        docker_client.ping()
    except Exception as exc:  # noqa: BLE001 — environment-dependent integration test
        pytest.skip(f"Docker SDK or daemon is unavailable: {exc}")
    else:
        docker_client.close()

    from agent_harness.web.app import session_service

    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    host_instruction = repository / "AGENTS.md"
    host_instruction.write_text("host initial", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "harness"),
        artifact_dir="",
        model_api_key="test-key",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions",
            params={"launch": "false"},
            json={"cwd": str(repository), "sandbox_backend": "docker"},
        )
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        service = session_service(app.state.agent)
        sandbox = service.workspace_registry.get(session_id)

        private_content = "private snapshot after edit"
        host_content = "host-only change outside the private snapshot"
        sandbox.write_text("AGENTS.md", private_content)
        host_instruction.write_text(host_content, encoding="utf-8")
        reloaded = client.post(
            f"/api/sessions/{session_id}/project-instructions/reload",
        )

        try:
            assert reloaded.status_code == 200, reloaded.text
            status = reloaded.json()
            assert status["source_paths"] == ["/workspace/AGENTS.md"]
            assert status["total_included_bytes"] == len(
                private_content.encode("utf-8")
            )
        finally:
            service.workspace_registry.delete(session_id)
