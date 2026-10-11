"""CLI surface for the explicit MCP package lifecycle."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_harness import cli
from agent_harness.plugins.mcp_package_manager import MCPPackageManager


def _package(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "weather", "transport": "stdio", "command": "python"}
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def test_cli_installs_trusts_selects_and_lists_mcp_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_dir = tmp_path / "home" / ".intelligence-agent" / "skills"
    settings = SimpleNamespace(
        workspace_dir=str(workspace), skill_global_dir=str(global_dir)
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        cli, "_query_current_mcp_runtime", lambda *_: ("not_running", None)
    )

    def run(*args: str) -> dict[str, object]:
        monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "mcp", *args])
        cli.main()
        return json.loads(capsys.readouterr().out)

    installed = run(
        "install", str(_package(tmp_path / "weather-tools")), "--id", "weather-tools"
    )
    assert installed["type"] == "mcp"
    assert installed["current_version"]["trust_status"] == "untrusted"
    run("trust", "weather-tools")
    selected = run("enable", "weather-tools")
    assert selected["selected_scope"] == "project"

    listed = run("list")
    package = listed["packages"][0]
    assert package["package_id"] == "weather-tools"
    assert package["saved_selection"] == "enabled"
    assert package["current_version"]["sha256"]
    assert package["pending_version"] is None
    assert package["current_runtime"] == "not_running"
    assert package["pending_restart"] is False

    stored = MCPPackageManager(workspace).list_packages()["weather-tools"]
    assert stored["current_version"]["trust_status"] == "trusted"


def test_cli_mcp_oauth_status_does_not_expose_tokens(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "workspace"
    settings = SimpleNamespace(
        workspace_dir=str(workspace),
        skill_global_dir="",
        capabilities=json.dumps({"mcp": {"options": {"servers": [{
            "name": "remote",
            "transport": "http",
            "url": "https://mcp.example.com/mcp",
            "oauth": {},
        }]}}}),
    )

    class Storage:
        async def get_tokens(self):
            from mcp.shared.auth import OAuthToken

            return OAuthToken(access_token="private-access-token", scope="files:read", expires_in=3600)

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.mcp.oauth.make_oauth_token_storage",
        lambda _server: Storage(),
    )
    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "mcp", "status", "remote"])

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "server": "remote",
        "status": "token_stored",
        "scope": "files:read",
        "expires_in": 3600,
    }
    assert "private-access-token" not in json.dumps(output)


def test_cli_mcp_oauth_status_does_not_hide_unexpected_runtime_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(
        workspace_dir=str(tmp_path / "workspace"),
        skill_global_dir="",
        capabilities=json.dumps({"mcp": {"options": {"servers": [{
            "name": "remote",
            "transport": "http",
            "url": "https://mcp.example.com/mcp",
            "oauth": {},
        }]}}}),
    )

    class Storage:
        async def get_tokens(self):
            raise RuntimeError("unexpected programming error")

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.mcp.oauth.make_oauth_token_storage",
        lambda _server: Storage(),
    )
    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "mcp", "status", "remote"])

    with pytest.raises(RuntimeError, match="unexpected programming error"):
        cli.main()


def test_cli_mcp_oauth_logout_clears_local_credentials(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "workspace"
    settings = SimpleNamespace(
        workspace_dir=str(workspace),
        skill_global_dir="",
        capabilities=json.dumps({"mcp": {"options": {"servers": [{
            "name": "remote",
            "transport": "http",
            "url": "https://mcp.example.com/mcp",
            "oauth": {},
        }]}}}),
    )

    class Storage:
        cleared = False

        async def get_tokens(self):
            return None

        async def get_client_info(self):
            return None

        async def get_oauth_metadata(self):
            return None

        async def clear(self):
            self.cleared = True

    storage = Storage()
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.mcp.oauth.make_oauth_token_storage",
        lambda _server: storage,
    )
    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "mcp", "logout", "remote"])

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "server": "remote",
        "local_cleared": True,
        "remote_revoke": "not_logged_in",
    }
    assert storage.cleared
