"""MCP package management stays separate from the unified tool execution path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_harness.plugins.mcp_package_manager import MCPPackageError, MCPPackageManager


def _package(
    root: Path, *, command: str = "python", token: str = "${MCP_TEST_TOKEN}"
) -> Path:
    root.mkdir(parents=True)
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "weather",
                        "transport": "stdio",
                        "command": command,
                        "args": ["-m", "weather_mcp"],
                        "env": {"API_TOKEN": token},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_installed_mcp_package_requires_explicit_trust_before_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _package(tmp_path / "source")
    workspace = tmp_path / "workspace"
    manager = MCPPackageManager(workspace)
    record = manager.install(source, package_id="weather-tools")

    assert record["type"] == "mcp"
    assert record["current_version"]["trust_status"] == "untrusted"
    with pytest.raises(MCPPackageError, match="trust"):
        manager.enable("weather-tools")

    monkeypatch.setenv("MCP_TEST_TOKEN", "runtime-secret")
    manager.trust("weather-tools")
    manager.enable("weather-tools")
    servers = manager.selected_servers()

    assert len(servers) == 1
    assert servers[0].name == "weather"
    assert servers[0].env["API_TOKEN"] == "runtime-secret"
    manifest_text = manager.manifest_path.read_text(encoding="utf-8")
    assert "runtime-secret" not in manifest_text
    assert "${MCP_TEST_TOKEN}" not in manifest_text


def test_package_relative_stdio_paths_resolve_inside_immutable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _package(tmp_path / "source", command="./bin/weather-server")
    executable = source / "bin" / "weather-server"
    executable.parent.mkdir()
    executable.write_text("server placeholder", encoding="utf-8")
    executable.chmod(0o755)
    manager = MCPPackageManager(tmp_path / "workspace")
    manager.install(source, package_id="weather-tools")
    manager.trust("weather-tools")
    manager.enable("weather-tools")
    monkeypatch.setenv("MCP_TEST_TOKEN", "runtime-secret")

    server = manager.selected_servers()[0]

    package_root = Path(server.cwd)
    assert (
        package_root
        == manager.versions_root
        / "weather-tools"
        / manager.list_packages()["weather-tools"]["current_version"]["sha256"]
    )
    assert Path(server.command) == package_root / "bin" / "weather-server"
    assert Path(server.command).is_file()


def test_global_package_is_not_selected_implicitly_and_scope_conflict_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_dir = tmp_path / "global" / "skills"
    workspace = tmp_path / "workspace"
    project_manager = MCPPackageManager(workspace, global_skills_dir=global_dir)
    global_manager = MCPPackageManager(
        workspace,
        scope="global",
        global_skills_dir=global_dir,
    )
    project_manager.install(_package(tmp_path / "project"), package_id="weather-tools")
    global_manager.install(
        _package(tmp_path / "global-source"), package_id="weather-tools"
    )
    project_manager.trust("weather-tools")
    global_manager.trust("weather-tools")

    with pytest.raises(MCPPackageError, match="both project and global"):
        project_manager.enable("weather-tools")

    project_manager.enable("weather-tools", scope="global")
    assert (
        project_manager.enable_results()["weather-tools"]["selected_scope"] == "global"
    )
    monkeypatch.setenv("MCP_TEST_TOKEN", "runtime-secret")
    assert project_manager.selected_servers()[0].name == "weather"


def test_local_versions_are_staged_then_rolled_back_after_runtime_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = MCPPackageManager(tmp_path / "workspace")
    first = manager.install(
        _package(tmp_path / "first", command="python"), package_id="weather-tools"
    )
    manager.trust("weather-tools")
    manager.enable("weather-tools")
    monkeypatch.setenv("MCP_TEST_TOKEN", "runtime-secret")

    next_source = _package(tmp_path / "next", command="python3")
    staged = manager.update("weather-tools", source=next_source)
    assert staged["current_version"]["sha256"] == first["current_version"]["sha256"]
    assert staged["pending_version"]["trust_status"] == "untrusted"
    assert manager.selected_servers()[0].command == "python"
    manager.trust("weather-tools", pending=True)
    assert manager.selected_servers(apply_pending=False)[0].command == "python"

    assert manager.selected_servers()[0].command == "python3"
    applied = manager.list_packages()["weather-tools"]
    assert applied["current_version"]["sha256"] == staged["pending_version"]["sha256"]
    assert applied["previous_version"]["sha256"] == first["current_version"]["sha256"]

    manager.rollback("weather-tools")
    rolled_back = manager.list_packages()["weather-tools"]["pending_version"]
    assert rolled_back["sha256"] == first["current_version"]["sha256"]
    assert manager.selected_servers()[0].command == "python"


def test_conflicting_pending_server_names_keep_selected_versions_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = MCPPackageManager(tmp_path / "workspace")
    weather_source = _package(tmp_path / "weather", command="python")
    forecast_source = _package(tmp_path / "forecast", command="python")
    (forecast_source / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "forecast", "transport": "stdio", "command": "python"}
                ]
            }
        ),
        encoding="utf-8",
    )
    first = manager.install(weather_source, package_id="weather-tools")
    manager.install(forecast_source, package_id="forecast-tools")
    for package_id in ("weather-tools", "forecast-tools"):
        manager.trust(package_id)
        manager.enable(package_id)
    monkeypatch.setenv("MCP_TEST_TOKEN", "runtime-secret")

    conflicting_source = _package(tmp_path / "weather-next", command="python")
    (conflicting_source / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "forecast", "transport": "stdio", "command": "python"}
                ]
            }
        ),
        encoding="utf-8",
    )
    staged = manager.update("weather-tools", source=conflicting_source)
    manager.trust("weather-tools", pending=True)

    servers = manager.selected_servers()

    current = manager.list_packages()["weather-tools"]
    assert current["current_version"]["sha256"] == first["current_version"]["sha256"]
    assert current["pending_version"]["sha256"] == staged["pending_version"]["sha256"]
    assert {server.name for server in servers} == {"weather", "forecast"}
    assert any(
        "pending version" in error and "conflict" in error for error in manager.errors
    )

    manager.disable("forecast-tools")
    servers = manager.selected_servers(reserved_server_names={"forecast"})

    assert [server.name for server in servers] == ["weather"]
    assert (
        manager.list_packages()["weather-tools"]["pending_version"]["sha256"]
        == staged["pending_version"]["sha256"]
    )

    servers = manager.selected_servers()

    assert [server.name for server in servers] == ["forecast"]
    assert manager.list_packages()["weather-tools"]["pending_version"] is None
    assert (
        manager.list_packages()["weather-tools"]["current_version"]["sha256"]
        == staged["pending_version"]["sha256"]
    )


def test_git_source_is_pinned_staged_and_rolled_back_by_full_commit(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source = _package(repo / "servers" / "weather", command="weather-v1", token="")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first server version")
    first_commit = _git(repo, "rev-parse", "HEAD")

    (source / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "weather", "transport": "stdio", "command": "weather-v2"}
                ]
            }
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "second server version")
    second_commit = _git(repo, "rev-parse", "HEAD")

    manager = MCPPackageManager(tmp_path / "workspace")
    installed = manager.install_git(
        repo.as_uri(),
        ref=first_commit,
        subdirectory="servers/weather",
        package_id="weather-tools",
    )
    assert installed["current_version"]["resolved_commit"] == first_commit
    manager.trust("weather-tools")
    manager.enable("weather-tools")

    staged = manager.update("weather-tools", ref=second_commit)
    assert staged["current_version"]["resolved_commit"] == first_commit
    assert staged["pending_version"]["resolved_commit"] == second_commit
    manager.trust("weather-tools", pending=True)
    assert manager.selected_servers(apply_pending=False)[0].command == "weather-v1"

    assert manager.selected_servers()[0].command == "weather-v2"
    assert (
        manager.list_packages()["weather-tools"]["previous_version"]["resolved_commit"]
        == first_commit
    )
    manager.rollback("weather-tools")
    assert manager.selected_servers()[0].command == "weather-v1"
