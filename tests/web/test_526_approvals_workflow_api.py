"""#526 HTTP 面：`POST /api/sessions/{id}/approvals/revoke` 与
`POST /api/sessions/{id}/workflow-mode`（黑盒）。

- revoke：追加 `permission/approval-revoked`（last-wins）；幂等（重复撤回仍 200，
  `revoked=False` 表示当时无此 key）。
- workflow-mode：追加 `workflow/mode-changed`（last-wins）；`mode` 非法 → 422。
- 错误语义与 `archive` / `purge-stale-tools` 同口径：404 = 会话不存在，
  422 = id 形态非法（先于 404）。

黑盒：只经 HTTP + 直接读 `/events`，不 import 服务层内部符号。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel


def _app(tmp_path: Path, **overrides):
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test", **overrides
    )
    return create_app(settings, enable_cors=False)


def _client(tmp_path: Path, **overrides) -> TestClient:
    return TestClient(_app(tmp_path, **overrides))


def _create_session(client: TestClient) -> str:
    base: dict[str, object] = {"task": "建立一个会话", "budget": {"local": {"max_agent_turns": 1}}}
    with patch(
        "agent_harness.assembly.create_chat_model",
        return_value=ScriptedModel(responses=[AIMessage(content="ok")]),
    ):
        resp = client.post("/api/sessions", json=base)
    assert resp.status_code == 200, resp.text
    frames = [
        json.loads(line[len("data:"):].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    session_id = next((f["session_id"] for f in frames if f.get("session_id")), None)
    assert session_id, f"SSE 流里没有 session_id：{frames[:3]}"
    return str(session_id)


def _event_types(client: TestClient, session_id: str) -> list[str]:
    resp = client.get(f"/api/sessions/{session_id}/events")
    assert resp.status_code == 200, resp.text
    return [e["type"] for e in resp.json()]


class TestWorkflowModeEndpoint:
    def test_set_plan_then_normal(self, tmp_path: Path):
        client = _client(tmp_path)
        sid = _create_session(client)

        r1 = client.post(f"/api/sessions/{sid}/workflow-mode", json={"mode": "plan"})
        assert r1.status_code == 200, r1.text
        assert r1.json() == {"id": sid, "mode": "plan"}
        assert "workflow/mode-changed" in _event_types(client, sid)

        # 幂等：重复切同档仍 200
        r2 = client.post(f"/api/sessions/{sid}/workflow-mode", json={"mode": "plan"})
        assert r2.status_code == 200

        r3 = client.post(f"/api/sessions/{sid}/workflow-mode", json={"mode": "normal"})
        assert r3.status_code == 200, r3.text
        assert r3.json()["mode"] == "normal"

    def test_invalid_mode_422(self, tmp_path: Path):
        client = _client(tmp_path)
        sid = _create_session(client)
        r = client.post(f"/api/sessions/{sid}/workflow-mode", json={"mode": "turbo"})
        assert r.status_code == 422, r.text

    def test_unknown_session_404(self, tmp_path: Path):
        client = _client(tmp_path)
        r = client.post("/api/sessions/does-not-exist-123/workflow-mode", json={"mode": "plan"})
        assert r.status_code == 404, r.text

    def test_bad_id_422(self, tmp_path: Path):
        client = _client(tmp_path)
        r = client.post("/api/sessions/bad id!/workflow-mode", json={"mode": "plan"})
        assert r.status_code == 422, r.text


class TestRevokeApprovalEndpoint:
    def test_revoke_unknown_key_is_noop(self, tmp_path: Path):
        client = _client(tmp_path)
        sid = _create_session(client)

        r = client.post(
            f"/api/sessions/{sid}/approvals/revoke", json={"approval_key": "no-such-key"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == sid
        assert body["approval_key"] == "no-such-key"
        assert body["revoked"] is False
        # 撤回事件照样落盘（append-only 审计）
        assert "permission/approval-revoked" in _event_types(client, sid)

    def test_unknown_session_404(self, tmp_path: Path):
        client = _client(tmp_path)
        r = client.post(
            "/api/sessions/does-not-exist-123/approvals/revoke",
            json={"approval_key": "k"},
        )
        assert r.status_code == 404, r.text

    def test_bad_id_422(self, tmp_path: Path):
        client = _client(tmp_path)
        r = client.post("/api/sessions/bad id!/approvals/revoke", json={"approval_key": "k"})
        assert r.status_code == 422, r.text
