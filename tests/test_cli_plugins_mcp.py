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
