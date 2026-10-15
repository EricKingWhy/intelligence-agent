"""#1130 AC-4：Docker 会话项目指令重载走 Sandbox workspace API（公共端点回归）。

Docker 后端回归（需要真实 Docker daemon + `python:3-slim` 镜像）：
经公共 `POST /api/sessions/{id}/project-instructions/reload` 端点断言——

- 重载读的是容器内私有 `/workspace` 快照（Sandbox workspace API），与初始
  指令加载一致（#1048 AC8，`reload_for_sandbox_session`）；
- 会话创建后对宿主的指令改动不出现在隔离会话中；私有 workspace 内的改动出现。

若重载路由退回旧的 fall-through（`reload_for_session(session_id, host_cwd)`），
本用例以两处证据变红：`source_paths` 变成宿主路径、重载内容变成宿主改动。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.context.project_instructions import project_instruction_store
from agent_harness.web.app import create_app, session_service


def _docker_ready() -> bool:
    try:
        docker = importlib.import_module("docker")
        client = docker.from_env(use_context=False)
        client.ping()
        client.images.get("python:3-slim")
        client.close()
    except Exception:  # noqa: BLE001 — environment-dependent integration test
        return False
    return True


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "harness"),
        artifact_dir="",
        model_api_key="test",
    )


def test_docker_session_reload_reads_private_workspace_via_public_endpoint(
    tmp_path: Path,
) -> None:
    if not _docker_ready():
        pytest.skip("Docker daemon or python:3-slim image is unavailable")

    project = tmp_path / "project"
    project.mkdir()
    host_instructions = project / "AGENTS.md"
    host_instructions.write_text("host version one", encoding="utf-8")
    settings = _settings(tmp_path)

    session_id: str | None = None
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/sessions",
            params={"launch": "false"},
            json={"cwd": str(project), "sandbox_backend": "docker"},
        )
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        try:
            registry = session_service(client.app.state.agent).workspace_registry
            assert registry.exists(session_id)
            sandbox = registry.get(session_id)

            # 会话创建后：私有 workspace 内改动 + 宿主改动（隔离会话只应看到前者）。
            sandbox.write_text("AGENTS.md", "private workspace version")
            host_instructions.write_text("host version two", encoding="utf-8")

            resp = client.post(
                f"/api/sessions/{session_id}/project-instructions/reload",
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "loaded"
            # 旧 fall-through 会报告宿主路径；沙箱语义必须是容器内路径。
            assert body["source_paths"] == ["/workspace/AGENTS.md"]

            store = project_instruction_store(settings)
            snapshot = store._sessions[session_id].snapshot  # 同进程白盒断言
            prompt = snapshot.prompt_text or ""
            assert "private workspace version" in prompt
            assert "host version two" not in prompt
            assert "host version one" not in prompt
        finally:
            assert session_id is not None
            deleted = client.delete(f"/api/sessions/{session_id}")
            assert deleted.status_code == 200, deleted.text
