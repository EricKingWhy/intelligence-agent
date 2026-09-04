"""通过 HTTP 验证 Settings 装配和刷新事件。"""

import json

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel


def test_web_configures_overflow_and_refresh_returns_same_events(tmp_path, monkeypatch):
    store = FakeArtifactStore()
    configured_sessions = []

    def provider(settings, *, session_id):
        configured_sessions.append(session_id)
        return store

    monkeypatch.setattr("agent_harness.web.app.S3ArtifactStore", provider)
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"id": "read-1", "name": "read",
                                           "args": {"path": "data.txt"}}]),
        AIMessage(content="done"),
    ])
    monkeypatch.setattr("agent_harness.web.app.create_chat_model", lambda config: model)
    settings = Settings(_env_file=None, workspace_dir=str(tmp_path / "state"),
                        artifact_store_endpoint="https://example.test", artifact_overflow_chars=200)
    # workspace 字段是安全边界（V1）：只接受 workspaces_root 下的单段目录名，不是主机路径。
    workspace = tmp_path / "state" / "workspaces" / "overflow-task"
    workspace.mkdir(parents=True)
    (workspace / "data.txt").write_text("content\n" * 1000, encoding="utf-8")
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/sessions", json={"task": "read", "workspace": "overflow-task"})
        assert response.status_code == 200
        live = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
        assert any(e["type"] == "artifact/created" for e in live)
        assert any(tool["name"] == "inspect_artifact" for tool in model.bound_tools)
        refreshed = client.get(f"/api/sessions/{configured_sessions[0]}/events").json()
        assert [(e["seq"], e["type"], e["data"]) for e in live if e["seq"] is not None] == [
            (e["seq"], e["type"], e.get("data", {})) for e in refreshed[1:]
        ]


def test_web_applies_context_budget_before_calling_model(tmp_path, monkeypatch):
    model = ScriptedModel([])
    monkeypatch.setattr("agent_harness.web.app.create_chat_model", lambda config: model)
    settings = Settings(_env_file=None, workspace_dir=str(tmp_path), max_context_tokens=100)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/sessions", json={"task": "large " * 200})
        assert "context_window_exceeded" in response.text
        assert model.snapshots == []
