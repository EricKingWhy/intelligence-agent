"""#1200 Phase 2：插件管理写端点 + 状态持久化（workspace 级 plugin-settings.json）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.mcp.capability import MCPCapability
from agent_harness.mcp.config import MCPServerConfig
from agent_harness.web.app import create_app


def _config(name: str) -> MCPServerConfig:
    return MCPServerConfig.model_validate(
        {"name": name, "transport": "stdio", "command": "npx"}
    )


class FakeConnection:
    def __init__(self, name: str, *, needs_auth: bool = False) -> None:
        self.config_name = name
        self.connected = True
        self.needs_auth = needs_auth

    async def aclose(self) -> None:
        self.connected = False

    async def reconnect(self) -> bool:
        self.connected = True
        return True


def _tool(server: str, tool: str) -> SimpleNamespace:
    return SimpleNamespace(
        server_name=server,
        remote_name=tool,
        name=f"mcp__{server}__{tool}",
        description=f"{tool} description",
    )


class _SkillsCapability:
    def __init__(self) -> None:
        self._entries = [
            SimpleNamespace(
                name="code-review",
                description="Review code.",
                when_to_use="",
                source_path=Path("/ws/skills/code-review/SKILL.md"),
                scope="workspace",
                origin="project",
                plugin=None,
                enabled=True,
            )
        ]

    def catalog(self):
        return [e for e in self._entries if e.enabled]

    def all_entries(self):
        return list(self._entries)

    def errors(self):
        return []

    def conflicts(self):
        return []


class _FakeAgent:
    def __init__(self, settings: Settings, wiring) -> None:
        self.settings = settings
        self._wiring = wiring

    async def get_wiring(self):
        return None, self._wiring


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _client(workspace: Path, *, needs_auth: bool = False):
    settings = Settings(
        _env_file=None, workspace_dir=str(workspace), model_api_key="test-key"
    )
    app = create_app(settings)
    capability = MCPCapability(
        connections=[FakeConnection("playwright", needs_auth=needs_auth), FakeConnection("github")],
        tools=[
            _tool("playwright", "browser_navigate"),
            _tool("playwright", "browser_upload"),
            _tool("github", "create_issue"),
        ],
        errors=[],
        configs=[_config("playwright"), _config("github")],
        server_tools={
            "playwright": [_tool("playwright", "browser_navigate"), _tool("playwright", "browser_upload")],
            "github": [_tool("github", "create_issue")],
        },
    )
    wiring = SimpleNamespace(
        lifecycle=[capability],
        skills=_SkillsCapability(),
        mcp_packages=[],
    )
    app.state.agent = _FakeAgent(settings, wiring)
    return TestClient(app), capability


def _settings_file(workspace: Path) -> dict:
    return json.loads((workspace / "plugin-settings.json").read_text(encoding="utf-8"))


def _client_without_mcp(workspace: Path, *, capabilities: str = ""):
    """未装配 MCP capability 的 app（wiring.lifecycle 为空）。"""
    settings = Settings(
        _env_file=None,
        workspace_dir=str(workspace),
        model_api_key="test-key",
        capabilities=capabilities,
    )
    app = create_app(settings)
    wiring = SimpleNamespace(lifecycle=[], skills=_SkillsCapability(), mcp_packages=[])
    app.state.agent = _FakeAgent(settings, wiring)
    return TestClient(app)


def test_list_servers_falls_back_to_config_without_capability(workspace: Path) -> None:
    """装配失败/未装配时仍按配置层列出——"配了但没连上"不能错报成"没配"（B1）。"""
    capabilities = json.dumps(
        {
            "mcp": {
                "provider": "builtin",
                "options": {
                    "servers": [
                        {"name": "playwright", "transport": "stdio", "command": "npx"}
                    ]
                },
            }
        }
    )
    client = _client_without_mcp(workspace, capabilities=capabilities)
    with client:
        body = client.get("/api/mcp/servers").json()
    assert body["servers"] == [
        {
            "name": "playwright",
            "connected": False,
            "enabled": True,
            "needs_auth": False,
            "tool_count": 0,
            "require_confirm": False,
            # 无 runtime capability 时配置摘要不可得：空串，不伪造。
            "transport": "",
            "command": "",
            "url": "",
        }
    ]
    assert body["errors"] == []


def test_list_servers_reports_broken_capabilities_config(workspace: Path) -> None:
    client = _client_without_mcp(workspace, capabilities="{not json")
    with client:
        body = client.get("/api/mcp/servers").json()
    assert body["servers"] == []
    assert len(body["errors"]) == 1
    assert "MCP 配置解析失败" in body["errors"][0]


def test_list_servers_reports_unreadable_settings_without_losing_rows(workspace: Path) -> None:
    """选择态文件损坏不拖垮只读列表：按默认（全启用）投影，错误如实附带。"""
    (workspace / "plugin-settings.json").write_text("{broken", encoding="utf-8")
    client, _ = _client(workspace)
    with client:
        body = client.get("/api/mcp/servers").json()
    assert {s["name"]: s["enabled"] for s in body["servers"]} == {
        "playwright": True,
        "github": True,
    }
    assert any("plugin-settings.json 不可读" in e for e in body["errors"])


def test_list_servers_reports_management_fields(workspace: Path) -> None:
    client, _ = _client(workspace, needs_auth=True)
    with client:
        body = client.get("/api/mcp/servers").json()
    by_name = {s["name"]: s for s in body["servers"]}
    assert by_name["playwright"] == {
        "name": "playwright",
        "connected": True,
        "enabled": True,
        "needs_auth": True,
        "tool_count": 2,
        "require_confirm": False,
        # Phase 1 详情页配置摘要（配置层原文）。
        "transport": "stdio",
        "command": "npx",
        "url": "",
    }
    assert by_name["github"]["tool_count"] == 1
    assert by_name["github"]["needs_auth"] is False


def test_disable_then_enable_server_persists(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        r = client.post("/api/mcp/servers/playwright/disable")
        assert r.status_code == 200, r.text
        assert r.json() == {"name": "playwright", "enabled": False}
        assert _settings_file(workspace)["disabled_servers"] == ["playwright"]
        listed = {s["name"]: s for s in client.get("/api/mcp/servers").json()["servers"]}
        assert listed["playwright"]["enabled"] is False

        r = client.post("/api/mcp/servers/playwright/enable")
        assert r.json() == {"name": "playwright", "enabled": True}
        assert _settings_file(workspace)["disabled_servers"] == []
        listed = {s["name"]: s for s in client.get("/api/mcp/servers").json()["servers"]}
        assert listed["playwright"]["enabled"] is True


def test_unknown_server_is_404(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        assert client.post("/api/mcp/servers/nope/disable").status_code == 404
        assert client.post("/api/mcp/servers/nope/enable").status_code == 404
        assert client.post("/api/mcp/servers/nope/reconnect").status_code == 404


def test_reconnect_returns_connected(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        r = client.post("/api/mcp/servers/playwright/reconnect")
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "playwright", "connected": True}


def test_tool_level_enable_disable(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        tools = client.get("/api/mcp/servers/playwright/tools").json()["tools"]
        assert {t["name"]: t["enabled"] for t in tools} == {
            "browser_navigate": True,
            "browser_upload": True,
        }

        r = client.post("/api/mcp/servers/playwright/tools/browser_upload/disable")
        assert r.status_code == 200, r.text
        assert r.json() == {"name": "playwright", "tool": "browser_upload", "enabled": False}
        assert _settings_file(workspace)["disabled_tools"] == {
            "playwright": ["browser_upload"]
        }

        tools = client.get("/api/mcp/servers/playwright/tools").json()["tools"]
        assert {t["name"]: t["enabled"] for t in tools} == {
            "browser_navigate": True,
            "browser_upload": False,
        }

        r = client.post("/api/mcp/servers/playwright/tools/browser_upload/enable")
        assert r.json() == {"name": "playwright", "tool": "browser_upload", "enabled": True}
        assert _settings_file(workspace)["disabled_tools"] == {}


def test_tools_unknown_server_is_404(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        assert client.get("/api/mcp/servers/nope/tools").status_code == 404
        assert (
            client.post("/api/mcp/servers/nope/tools/x/disable").status_code == 404
        )


def test_require_confirm_round_trip(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        r = client.post(
            "/api/mcp/servers/github/require-confirm", json={"require_confirm": True}
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"name": "github", "require_confirm": True}
        assert _settings_file(workspace)["require_confirm"] == ["github"]
        listed = {s["name"]: s for s in client.get("/api/mcp/servers").json()["servers"]}
        assert listed["github"]["require_confirm"] is True

        r = client.post(
            "/api/mcp/servers/github/require-confirm", json={"require_confirm": False}
        )
        assert r.json() == {"name": "github", "require_confirm": False}
        assert _settings_file(workspace)["require_confirm"] == []


def test_require_confirm_rejects_non_bool(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        r = client.post(
            "/api/mcp/servers/github/require-confirm", json={"require_confirm": "yes"}
        )
    assert r.status_code == 422


def test_skill_enable_disable_persists(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        r = client.post("/api/skills/code-review/disable")
        assert r.status_code == 200, r.text
        assert r.json() == {"name": "code-review", "enabled": False}
        assert _settings_file(workspace)["skill_overrides"] == {
            "code-review": {"enabled": False}
        }

        r = client.post("/api/skills/code-review/enable")
        assert r.json() == {"name": "code-review", "enabled": True}
        assert _settings_file(workspace)["skill_overrides"] == {}


def test_skill_name_validation_is_422(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        assert client.post("/api/skills/Bad%20Name/disable").status_code == 422
        assert client.post("/api/skills/UPPER/disable").status_code == 422
        assert client.post("/api/skills/a%2Fb/disable").status_code == 422


def test_unknown_skill_is_404(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        assert client.post("/api/skills/nope/disable").status_code == 404


def test_selection_persists_across_app_instances(workspace: Path) -> None:
    client, _ = _client(workspace)
    with client:
        client.post("/api/mcp/servers/playwright/disable")
        client.post("/api/mcp/servers/github/tools/create_issue/disable")
        client.post("/api/skills/code-review/disable")

    restarted, _ = _client(workspace)
    with restarted:
        servers = {
            s["name"]: s for s in restarted.get("/api/mcp/servers").json()["servers"]
        }
        assert servers["playwright"]["enabled"] is False
        tools = restarted.get("/api/mcp/servers/github/tools").json()["tools"]
        assert {t["name"]: t["enabled"] for t in tools}["create_issue"] is False
        skills = restarted.get("/api/skills").json()["skills"]
        assert {s["name"]: s["enabled"] for s in skills}["code-review"] is False
