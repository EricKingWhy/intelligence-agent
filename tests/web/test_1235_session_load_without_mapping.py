"""#1235 HTTP 面：映射缺席的会话，per-session 写端点仍应 200。

这三条端点只用 `Session.load` 追加事件、从不碰 sandbox。映射缺席的可达形态：
早于 WorkspaceRegistry 的历史会话、映射卷被清空、手工/测试构造的会话。
旧行为是 `Session.load` 里 `registry.get()` 抛 `KeyError` → 500。

修复位置在 `Session.load` 本身（不是四个调用点各自 try/except），
所以这里只钉一条端到端路径；根因用例在
`tests/session/test_session_workspace.py::TestLoadWithoutWorkspaceMapping`。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import Session
from agent_harness.web.app import create_app


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False))


def _session_without_mapping(client: TestClient) -> str:
    """事件流存在、workspace 映射缺席——故意不调用 registry.create。"""
    session = Session.start(client.app.state.agent.store)
    registry = client.app.state.agent.workspace_registry
    assert registry.recorded_workspace_roots(session.session_id) == []
    return session.session_id


def test_per_session_write_endpoints_work_without_workspace_mapping(tmp_path: Path):
    client = _client(tmp_path)
    sid = _session_without_mapping(client)

    workflow = client.post(f"/api/sessions/{sid}/workflow-mode", json={"mode": "plan"})
    assert workflow.status_code == 200, workflow.text
    assert workflow.json() == {"id": sid, "mode": "plan"}

    revoke = client.post(
        f"/api/sessions/{sid}/approvals/revoke", json={"approval_key": "no-such-key"}
    )
    assert revoke.status_code == 200, revoke.text

    context = client.patch(
        f"/api/sessions/{sid}/context-settings", json={"context_window": 100_000}
    )
    assert context.status_code == 200, context.text
    assert context.json()["context_window"] == 100_000

    types = [e["type"] for e in client.get(f"/api/sessions/{sid}/events").json()]
    assert "workflow/mode-changed" in types
    assert "permission/approval-revoked" in types
    assert "context/window-changed" in types
