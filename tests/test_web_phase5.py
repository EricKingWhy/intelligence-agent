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

    # 补丁目标从 `assembly.S3ArtifactStore` 移到 provider 模块（#192 批 1）：store 的
    # 选择已收敛到 `storage/artifact_select.py`，而它在**调用时**从 provider 模块取类，
    # 所以"把 S3 换成内存替身"要打在类的定义处，而不是某个 import 过它的命名空间。
    monkeypatch.setattr("agent_harness.storage.s3_artifact.S3ArtifactStore", provider)
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"id": "read-1", "name": "read",
                                           "args": {"path": "data.txt"}}]),
        AIMessage(content="done"),
    ])
    monkeypatch.setattr("agent_harness.assembly.create_chat_model", lambda config, **kw: model)
    settings = Settings(_env_file=None, workspace_dir=str(tmp_path / "state"),
                        model_api_key="sk-test-placeholder",
                        artifact_store_endpoint="https://example.test", artifact_overflow_chars=200)
    # workspace 字段是安全边界（V1）：只接受 workspaces_root 下的单段目录名，不是主机路径。
    workspace = tmp_path / "state" / "workspaces" / "overflow-task"
    workspace.mkdir(parents=True)
    (workspace / "data.txt").write_text("content\n" * 1000, encoding="utf-8")
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/sessions", json={"task": "read", "workspace": "overflow-task"})
        assert response.status_code == 200
        live = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
        assert any(e["type"] == "artifact/externalized" for e in live)
        assert any(tool["name"] == "inspect_artifact" for tool in model.bound_tools)
        refreshed = client.get(f"/api/sessions/{configured_sessions[0]}/events").json()
        # refreshed 前两条是 run 开始**之前**落盘的会话事实（session/started +
        # task/defined，W-07 #351 创建即定义），不在 run 的 SSE 流里——刷新重建
        # 与 live 流的对齐从第三条开始。
        assert [(e["seq"], e["type"], e["data"]) for e in live if e["seq"] is not None] == [
            (e["seq"], e["type"], e.get("data", {})) for e in refreshed[2:]
        ]


def test_web_applies_context_budget_before_calling_model(tmp_path, monkeypatch):
    """超预算任务：装配链的 `max_context_tokens` 在**调模型之前**生效——模型一次都
    不被调用。W-04（#348）起收口是非终态 `run/paused`（`reason` 经
    `reason_for_dimension` 自动 = budget_exhausted，`trigger_dimension` =
    max_context_tokens），不再有 `context_window_exceeded` 终态帧。"""
    model = ScriptedModel([])
    monkeypatch.setattr("agent_harness.assembly.create_chat_model", lambda config, **kw: model)
    settings = Settings(_env_file=None, workspace_dir=str(tmp_path),
                        model_api_key="sk-test-placeholder", max_context_tokens=100)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/sessions", json={"task": "large " * 200})
        live = [json.loads(line[5:]) for line in response.text.splitlines()
                if line.startswith("data:")]
        paused = next(e for e in live if e["type"] == "run/paused")
        assert paused["data"]["reason"] == "budget_exhausted"
        assert paused["data"]["trigger_dimension"] == "max_context_tokens"
        assert model.snapshots == []
