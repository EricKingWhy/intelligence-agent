"""#875 T6：MCP server 描述预检（静态解析，零连接、零 secret 回显）。

覆盖票面 4 条 AC：
- AC1 只接受 `MCPServerConfig` 可表达的显式描述；没有描述 → 报"需要薄适配描述"，不猜。
- AC2 报告 name/transport/command/URL/权限覆写/环境变量名与依赖；不回显 secret 值。
- AC3 未知字段、缺 command/URL、重复名、越界路径、不支持 transport 明确失败。
- AC4 OAuth-only 标 `oauth_required_unsupported`；resources/prompts 逐项列缺口。

"零连接 / 零 spawn"在单测里可机械判定：子进程里跑一遍预检后查 `sys.modules`
（`test_precheck_imports_no_connection_machinery`），以及把 `subprocess.Popen/run`
打桩成会炸（`test_precheck_never_spawns_a_process`）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_harness.plugins.mcp_precheck import inspect_mcp_servers


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _package(tmp_path: Path, payload: object, *, name: str = "mcp.json") -> Path:
    root = tmp_path / "plugin"
    root.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        _write(root / name, json.dumps(payload, ensure_ascii=False))
    return root


def _server(report: dict, name: str) -> dict:
    return next(item for item in report["servers"] if item["name"] == name)


def _codes(report: dict, name: str) -> list[str]:
    return [error["code"] for error in _server(report, name)["errors"]]


# ------------------------------------------------------------------ AC1：显式描述 / 无描述

def test_package_without_description_asks_for_one_instead_of_guessing(tmp_path: Path) -> None:
    root = _package(tmp_path, None)
    # 周围放上"看起来像能猜出来"的东西：预检必须一个字都不读它们。
    _write(root / "package.json", json.dumps({"bin": {"evil": "./evil.js"}}))
    _write(root / "README.md", "# Plugin\n\nRun `npx evil-mcp --token=hunter2` to start.\n")

    report = inspect_mcp_servers(root)

    assert report["status"] == "needs-adaptation"
    assert report["servers"] == []
    assert report["errors"] == []
    assert [item["kind"] for item in report["requirements"]] == ["mcp-description"]
    assert "hunter2" not in json.dumps(report, ensure_ascii=False)


def test_description_file_is_discovered_in_declared_order(tmp_path: Path) -> None:
    root = _package(tmp_path, {"mcpServers": {"first": {"type": "stdio", "command": "npx"}}})
    _write(root / ".mcp.json", json.dumps({"mcpServers": {"second": {"type": "stdio", "command": "npx"}}}))

    report = inspect_mcp_servers(root)

    assert report["description_file"] == "mcp.json"
    assert [item["name"] for item in report["servers"]] == ["first"]


def test_empty_description_is_rejected_not_treated_as_no_description(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {}}))

    assert report["status"] == "unsupported"
    assert "MCP_DESCRIPTION_EMPTY" in {error["code"] for error in report["errors"]}


# ------------------------------------------------------------------ AC2：报告内容与零泄漏

def test_stdio_report_lists_transport_command_env_names_and_dependencies(tmp_path: Path) -> None:
    root = _package(tmp_path, {"mcpServers": {"local": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@example/mcp-server"],
        "env": {"API_KEY": "${EXAMPLE_API_KEY}", "CACHE_DIR": "${EXAMPLE_CACHE:-/tmp/cache}"},
        "tool_permissions": {"search": "read-only", "delete": "danger"},
        "timeout": 120000,
    }}})

    report = inspect_mcp_servers(root)
    server = _server(report, "local")

    assert server["status"] == "needs-adaptation"  # 凭据引用待运行时判定
    assert server["transport"] == "stdio"
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@example/mcp-server"]  # 不含引用的参数原样回显
    assert server["timeout_seconds"] == 120.0  # Claude Code 的 timeout 单位是毫秒
    assert server["tool_permissions"] == [
        {"tool": "search", "permission": "read-only"},
        {"tool": "delete", "permission": "danger"},
    ]
    assert [item["name"] for item in server["environment"]] == ["API_KEY", "CACHE_DIR"]
    assert [item["name"] for item in server["dependencies"]] == ["npx"]
    assert {item["name"] for item in server["requirements"]} == {"EXAMPLE_API_KEY", "EXAMPLE_CACHE"}


def test_http_report_lists_url_header_names_without_echoing_values(tmp_path: Path) -> None:
    root = _package(tmp_path, {"mcpServers": {"remote": {
        "type": "streamable-http",
        "url": "https://api.example.com/mcp",
        "headers": {"Authorization": "Bearer ${EXAMPLE_TOKEN}", "X-Tenant": "acme"},
    }}})

    report = inspect_mcp_servers(root)
    server = _server(report, "remote")

    assert server["transport"] == "http"  # streamable-http 是 http 的别名
    assert server["url"] == "https://api.example.com/mcp"
    assert [item["name"] for item in server["headers"]] == ["Authorization", "X-Tenant"]
    assert {item["name"] for item in server["requirements"]} == {"EXAMPLE_TOKEN"}
    serialized = json.dumps(report, ensure_ascii=False)
    assert "acme" not in serialized  # 明文 header 值不回显
    assert "Bearer" not in serialized


def test_secret_values_are_never_echoed_for_plaintext_or_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXAMPLE_API_KEY", "super-secret-value")
    root = _package(tmp_path, {"mcpServers": {"local": {
        "type": "stdio",
        "command": "npx",
        "env": {"REFERENCED": "${EXAMPLE_API_KEY}", "PLAINTEXT": "also-a-secret"},
    }}})

    report = inspect_mcp_servers(root)
    serialized = json.dumps(report, ensure_ascii=False)

    assert "super-secret-value" not in serialized  # 预检不读部署环境
    assert "also-a-secret" not in serialized
    assert _server(report, "local")["environment"] == [
        {"name": "REFERENCED", "value": {"kind": "reference", "references": [
            {"name": "EXAMPLE_API_KEY", "has_default": False}]}},
        {"name": "PLAINTEXT", "value": {"kind": "literal"}},
    ]


def test_native_shape_is_accepted_and_reported(tmp_path: Path) -> None:
    root = _package(tmp_path, {"servers": [{
        "name": "native",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@example/mcp"],
        "tool_permissions": {"list": "read-only"},
    }]})

    report = inspect_mcp_servers(root)

    assert report["status"] == "complete"
    assert _server(report, "native")["status"] == "supported"


def test_precheck_imports_no_connection_machinery(tmp_path: Path) -> None:
    """零连接的机械判据：跑完预检，进程里没有**任何连接机制**被加载。

    必须开子进程测：同一次 pytest 会话里别的用例（如 `tests/capability/
    test_mcp_wiring.py`）早把 client / SDK transport 导进来了，in-process 的
    "sys.modules 里没有" 会被别人的导入污染成一个永远为真的断言。
    （stdlib 的 `socket`/`subprocess` 模块本身会被 `skills.inspection` 的导入链带进来，
    模块存在 ≠ 发起连接；本用例断言的是 mcp client / HTTP / async 运行时一个都没加载。）
    """
    root = _package(tmp_path, {"mcpServers": {"local": {"type": "stdio", "command": "npx"}}})
    probe = (
        "import sys, json;"
        "from agent_harness.plugins.mcp_precheck import inspect_mcp_servers;"
        f"report = inspect_mcp_servers({str(root)!r});"
        "loaded = sorted(n for n in sys.modules if n.startswith(('mcp.', 'agent_harness.mcp.')));"
        "runtimes = sorted(n for n in ('httpx2', 'anyio') if n in sys.modules);"
        "print(json.dumps({'loaded': loaded, 'runtimes': runtimes, 'status': report['status']}))"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=True
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result["status"] == "complete"  # 静态描述本身无缺口
    # 只允许加载**纯 schema** 模块（config）；client / session / SDK transport 一个都不许有。
    assert result["loaded"] == ["agent_harness.mcp.config"]
    assert result["runtimes"] == []


def test_precheck_never_spawns_a_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """零启动的机械判据：连"包内可执行文件"在场时也不 spawn。"""
    import subprocess as subprocess_module

    root = _package(tmp_path, {"mcpServers": {"local": {
        "type": "stdio", "command": "./servers/db-server", "cwd": "./servers",
    }}})
    _write(root / "servers" / "db-server", "#!/bin/sh\ntouch /tmp/should-not-exist\n")
    os.chmod(root / "servers" / "db-server", 0o755)

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("precheck must not spawn any process")

    monkeypatch.setattr(subprocess_module, "Popen", explode)
    monkeypatch.setattr(subprocess_module, "run", explode)

    report = inspect_mcp_servers(root)

    assert _codes(report, "local") == []
    assert report["servers"][0]["command"] == "${PACKAGE}/servers/db-server"


def test_precheck_does_not_write_into_the_package(tmp_path: Path) -> None:
    root = _package(tmp_path, {"mcpServers": {"local": {"type": "stdio", "command": "npx"}}})
    before = sorted((path.relative_to(root).as_posix(), path.stat().st_size) for path in root.rglob("*"))

    inspect_mcp_servers(root)

    after = sorted((path.relative_to(root).as_posix(), path.stat().st_size) for path in root.rglob("*"))
    assert after == before


# ------------------------------------------------------------------ AC3：明确失败面

@pytest.mark.parametrize(
    ("entry", "expected_code"),
    [
        ({"type": "sse", "url": "https://example.com/sse"}, "MCP_TRANSPORT_UNSUPPORTED"),
        ({"type": "ws", "url": "wss://example.com/socket"}, "MCP_TRANSPORT_UNSUPPORTED"),
        ({"url": "https://example.com/mcp"}, "MCP_TRANSPORT_UNSUPPORTED"),  # 有 url 没 type
        ({"type": "stdio", "args": ["-y", "@example/mcp"]}, "MCP_COMMAND_MISSING"),
        ({"type": "http", "headers": {}}, "MCP_URL_MISSING"),
        (
            {"type": "stdio", "command": "npx", "headersHelper": "/opt/bin/auth.sh"},
            "MCP_FIELD_UNKNOWN",
        ),
    ],
)
def test_unsupported_shapes_fail_explicitly(tmp_path: Path, entry: dict, expected_code: str) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"bad": entry}}))

    # AC3 的"明确失败"：整份描述判 unsupported（不是"待适配"——那是留缺口的语义）。
    assert report["status"] == "unsupported"
    assert _server(report, "bad")["status"] == "unsupported"
    assert expected_code in _codes(report, "bad")


def test_duplicate_server_names_fail_even_in_claude_shape(tmp_path: Path) -> None:
    # Claude 形状以 name 为键 ⇒ 同名只能以 JSON 重复键出现，json.loads 会静默取后者。
    raw = '{"mcpServers": {"dup": {"type": "stdio", "command": "a"}, "dup": {"type": "stdio", "command": "b"}}}'
    root = tmp_path / "plugin"
    _write(root / "mcp.json", raw)

    report = inspect_mcp_servers(root)

    assert report["status"] == "unsupported"
    assert "MCP_SERVER_NAME_DUPLICATE" in {error["code"] for error in report["errors"]}


def test_duplicate_server_names_fail_in_native_shape(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"servers": [
        {"name": "dup", "transport": "stdio", "command": "npx"},
        {"name": "dup", "transport": "stdio", "command": "npx"},
    ]}))

    assert report["status"] == "unsupported"
    assert "MCP_SERVER_NAME_DUPLICATE" in {error["code"] for error in report["errors"]}


def test_unknown_field_fails_loudly(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"bad": {
        "type": "stdio", "command": "npx", "alwaysLoad": True,
    }}}))

    assert "MCP_FIELD_UNKNOWN" in _codes(report, "bad")


@pytest.mark.parametrize("command", ["../outside.sh", "../../etc/passwd", "/usr/bin/env"])
def test_command_outside_package_fails(tmp_path: Path, command: str) -> None:
    root = _package(tmp_path, {"mcpServers": {"bad": {"type": "stdio", "command": command}}})
    _write(tmp_path / "outside.sh", "#!/bin/sh\n")

    report = inspect_mcp_servers(root)

    assert "MCP_PATH_OUTSIDE_PACKAGE" in _codes(report, "bad")


def test_command_inside_package_must_exist(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"bad": {
        "type": "stdio", "command": "./servers/missing.js",
    }}}))

    assert "MCP_PATH_MISSING" in _codes(report, "bad")


def test_command_inside_package_is_reported_with_relative_label(tmp_path: Path) -> None:
    root = _package(tmp_path, {"mcpServers": {"local": {
        "type": "stdio", "command": "./servers/db-server", "cwd": "./servers",
    }}})
    _write(root / "servers" / "db-server", "#!/bin/sh\n")
    os.chmod(root / "servers" / "db-server", 0o755)

    report = inspect_mcp_servers(root)
    server = _server(report, "local")

    assert _codes(report, "local") == []
    assert server["command"] == "${PACKAGE}/servers/db-server"
    assert server["cwd"] == "${PACKAGE}/servers"
    # server 段里不出现预检机的绝对路径；`source` 是包根本身（与 Skill 报告同约定）。
    assert str(tmp_path) not in json.dumps(server, ensure_ascii=False)


def test_escaping_symlink_description_is_refused(tmp_path: Path) -> None:
    root = _package(tmp_path, None)
    outside = tmp_path / "outside.json"
    _write(outside, json.dumps({"mcpServers": {"evil": {"type": "stdio", "command": "npx"}}}))
    try:
        (root / "mcp.json").symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 无权限
        pytest.skip("symlink unsupported")

    report = inspect_mcp_servers(root)

    # 描述文件**在**、但越界读不了 ⇒ 这是失败，不是"包里没有描述"（两者要用户做的事不同）。
    assert report["servers"] == []
    assert report["status"] == "unsupported"
    assert "MCP_DESCRIPTION_OUTSIDE_PACKAGE" in {error["code"] for error in report["errors"]}
    assert report["requirements"] == []


def test_malformed_json_fails_without_crashing(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write(root / "mcp.json", "{not json")

    report = inspect_mcp_servers(root)

    assert report["status"] == "unsupported"
    assert "MCP_DESCRIPTION_INVALID" in {error["code"] for error in report["errors"]}


def test_invalid_secret_reference_syntax_fails(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"bad": {
        "type": "stdio", "command": "npx", "env": {"BAD": "${}"},
    }}}))

    assert "MCP_SECRET_REFERENCE_INVALID" in _codes(report, "bad")


def test_invalid_tool_permission_fails(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"bad": {
        "type": "stdio", "command": "npx", "tool_permissions": {"search": "sudo"},
    }}}))

    assert "MCP_TOOL_PERMISSION_INVALID" in _codes(report, "bad")


def test_both_shapes_at_once_fails(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"a": {"type": "stdio", "command": "npx"}},
                                                    "servers": [{"name": "b", "transport": "stdio", "command": "npx"}]}))

    assert report["status"] == "unsupported"
    assert "MCP_DESCRIPTION_INVALID" in {error["code"] for error in report["errors"]}


# ------------------------------------------------------------------ AC4：OAuth 与能力缺口

def test_oauth_only_server_is_marked_unsupported(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"login": {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "oauth": {"callbackPort": 8080, "scopes": "files:read"},
    }}}))

    server = _server(report, "login")
    assert server["status"] == "needs-adaptation"
    assert "complete" != report["status"]
    assert any(item["kind"] == "oauth_required_unsupported" for item in server["requirements"])
    assert all(item["support"] != "supported" for item in server["requirements"])


def test_resources_and_prompts_declarations_become_listed_gaps(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"rich": {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "resources": ["file:///data"],
        "prompts": ["summarize"],
    }}}))

    server = _server(report, "rich")
    kinds = {item["kind"] for item in server["requirements"] if item["support"] == "unsupported"}
    assert {"resources", "prompts"} <= kinds
    assert server["status"] == "needs-adaptation"


def test_complete_status_requires_every_requirement_supported(tmp_path: Path) -> None:
    report = inspect_mcp_servers(_package(tmp_path, {"servers": [
        {"name": "clean", "transport": "stdio", "command": "npx"},
    ]}))

    assert report["status"] == "complete"
    assert report["errors"] == []


def test_cli_plugins_inspect_merges_skill_and_mcp_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """真实 CLI：一个包同时有 SKILL.md 与 mcp.json 时，一次 inspect 给两段结论。"""
    from agent_harness import cli

    package = _package(tmp_path, {"mcpServers": {"local": {
        "type": "stdio", "command": "npx", "args": ["-y", "@example/mcp"],
    }}})
    _write(package / "SKILL.md", "---\nname: plugin\ndescription: Has an MCP server.\n---\nBody.\n")
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _write(workspace / "plugin-installs.json", '{"project": []}\n')
    _write(global_skills / "plugin-installs.json", '{"global": []}\n')
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: type("S", (), {
            "workspace_dir": str(workspace),
            "skill_global_dir": str(global_skills),
            "capabilities": None,
        })(),
    )
    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "inspect", str(package), "--scope", "project"])

    cli.main()

    report = json.loads(capsys.readouterr().out)
    assert report["name"] == "plugin"  # Skill 段仍是 #870 的形状
    assert report["mcp"]["description_file"] == "mcp.json"
    assert [item["name"] for item in report["mcp"]["servers"]] == ["local"]
    assert report["mcp"]["servers"][0]["transport"] == "stdio"


def test_cli_plugins_inspect_mcp_only_package_still_prints_the_mcp_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """纯 MCP 包（没有 SKILL.md）：MCP 段照出（T7 要用），包级状态仍由 Skill 段判。

    这是**有意保留**的入口语义：`plugins inspect` 至今接受的是 Skill 包，本票只让
    MCP 描述进入报告，不改变"包必须先是 Skill 包"的判据（该边界已在交付回报里登记，
    等协调者裁决是否放宽）。
    """
    from agent_harness import cli

    package = _package(tmp_path, {"mcpServers": {"local": {"type": "stdio", "command": "npx"}}})
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _write(workspace / "plugin-installs.json", '{"project": []}\n')
    _write(global_skills / "plugin-installs.json", '{"global": []}\n')
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: type("S", (), {
            "workspace_dir": str(workspace),
            "skill_global_dir": str(global_skills),
            "capabilities": None,
        })(),
    )
    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "inspect", str(package), "--scope", "project"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "unsupported"
    assert "SKILL_FILE_MISSING" in {error["code"] for error in report["errors"]}
    assert report["mcp"]["servers"][0]["name"] == "local"  # 描述仍然进了报告


# ------------------------------------------------------------------ 审查修复面（R1）

def test_absurd_timeout_value_fails_instead_of_crashing(tmp_path: Path) -> None:
    """P0-1：`timeout` 是任意大整数时，除毫秒→秒会抛 OverflowError 掀翻 CLI。"""
    huge = json.loads("1" + "0" * 400)
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"bad": {
        "type": "stdio", "command": "npx", "timeout": huge,
    }}}))

    assert "MCP_SERVER_INVALID" in _codes(report, "bad")
    assert _server(report, "bad")["status"] == "unsupported"


def test_plaintext_next_to_a_broken_reference_is_not_echoed(tmp_path: Path) -> None:
    """P1-1：`hunter2${}` 这种"明文 + 坏引用"不能把明文抄进报告。"""
    root = _package(tmp_path, {"mcpServers": {"bad": {
        "type": "stdio",
        "command": "npx",
        "args": ["--token=hunter2${API_KEY}"],
        "env": {"PASSWORD": "hunter2${}"},
    }}})

    report = inspect_mcp_servers(root)
    serialized = json.dumps(report, ensure_ascii=False)

    assert "MCP_SECRET_REFERENCE_INVALID" in _codes(report, "bad")
    assert "hunter2" not in serialized
    # args 里"明文 + 引用"混排时只回显引用名（AC2：不回显 secret 值）
    assert _server(report, "bad")["args"] == [
        {"kind": "reference", "references": [{"name": "API_KEY", "has_default": False}]}
    ]


def test_unreadable_description_file_is_a_failure_not_a_missing_one(tmp_path: Path) -> None:
    """P1-2：描述文件在、但读不出来时，不能落进"请补一份描述"那条正常结论。"""
    root = _package(tmp_path, {"mcpServers": {"s": {"type": "stdio", "command": "npx"}}})
    (root / "mcp.json").chmod(0)
    if os.access(root / "mcp.json", os.R_OK):  # root 绕过权限位（本仓已知环境假红形态）
        pytest.skip("running as a user that can read mode-000 files")

    report = inspect_mcp_servers(root)

    assert report["status"] == "unsupported"
    assert "MCP_DESCRIPTION_UNREADABLE" in {error["code"] for error in report["errors"]}
    assert report["requirements"] == []  # 不得同时说"包里没有描述"


def test_description_that_is_not_a_regular_file_is_a_failure(tmp_path: Path) -> None:
    """同一类：mcp.json 是目录 ⇒ 失败，而不是"没有描述"。"""
    root = _package(tmp_path, None)
    (root / "mcp.json").mkdir()

    report = inspect_mcp_servers(root)

    assert report["status"] == "unsupported"
    assert "MCP_DESCRIPTION_NOT_A_FILE" in {error["code"] for error in report["errors"]}


@pytest.mark.parametrize("name", ["evil.tool", "a__b/c", "bad name", "", "中文"])
@pytest.mark.parametrize("shape", ["claude", "native"])
def test_server_names_the_runtime_model_rejects_are_rejected_here(
    tmp_path: Path, name: str, shape: str
) -> None:
    """P0-4：`name` 会成为工具命名空间段，合法性由运行期模型定（别抄第二份规则）。

    `MCPServerConfig._validate_name` 只认 `^[A-Za-z0-9_-]+$`；预检比它松的话，
    一份判成 `complete` 的描述到了装配期会 `init_failed`。
    """
    payload = (
        {"mcpServers": {name: {"type": "stdio", "command": "npx"}}}
        if shape == "claude"
        else {"servers": [{"name": name, "transport": "stdio", "command": "npx"}]}
    )

    report = inspect_mcp_servers(_package(tmp_path, payload))

    assert report["status"] == "unsupported"
    # 两种形状的落点不同：Claude 形状以键为名（名字合法性问题），native 形状
    # 空/非字符串名在结构层就被拒（`MCP_SERVER_INVALID`）——都是明确失败。
    codes = {error["code"] for error in report["errors"]}
    assert codes & {"MCP_SERVER_NAME_INVALID", "MCP_SERVER_INVALID"}, codes


def test_resources_declaration_is_a_gap_in_both_shapes(tmp_path: Path) -> None:
    """P2-3：native 形状下 `resources` 只能被"能力缺口"判一次，不再叠一条未知字段。"""
    report = inspect_mcp_servers(_package(tmp_path, {"servers": [{
        "name": "rich", "transport": "stdio", "command": "npx",
        "resources": ["file:///x"], "prompts": ["y"],
    }]}))

    server = _server(report, "rich")
    kinds = {item["kind"] for item in server["requirements"] if item["support"] == "unsupported"}
    assert {"resources", "prompts"} <= kinds
    assert "MCP_FIELD_UNKNOWN" not in _codes(report, "rich")


@pytest.mark.parametrize("declared", ["file:///x", ["x"], {"a": 1}, True, 1])
def test_resources_declared_in_any_form_is_a_gap(tmp_path: Path, declared: object) -> None:
    """P2：声明形态不止 list/dict——字符串/数字的声明不能被静默丢掉。"""
    report = inspect_mcp_servers(_package(tmp_path, {"mcpServers": {"rich": {
        "type": "http", "url": "https://a/mcp", "resources": declared,
    }}}))

    server = _server(report, "rich")
    assert any(item["kind"] == "resources" for item in server["requirements"])
    assert server["status"] == "needs-adaptation"


# ------------------------------------------------------------------ AC4 的安装期闸门

def test_attach_mcp_section_downgrades_a_package_with_an_oauth_only_server(
    tmp_path: Path,
) -> None:
    """AC4：OAuth-only 描述不得以"完整兼容"过关——降级必须发生在**这份报告**里。"""
    from agent_harness.plugins.mcp_precheck import attach_mcp_section

    report = {
        "status": "complete",
        "source": str(_package(tmp_path, {"mcpServers": {"login": {
            "type": "http", "url": "https://mcp.example.com/mcp", "oauth": {"callbackPort": 8080},
        }}})),
        "requirements": [],
        "errors": [],
    }

    merged = attach_mcp_section(report)

    assert merged["status"] == "needs-adaptation"
    assert any(item["kind"] == "mcp-oauth_required_unsupported" for item in merged["requirements"])
    assert merged["mcp"]["servers"][0]["status"] == "needs-adaptation"


def test_attach_mcp_section_leaves_a_clean_skill_package_complete(tmp_path: Path) -> None:
    """反向：包里没有 MCP 描述时，包级 status 不被 MCP lane 的结论污染。"""
    from agent_harness.plugins.mcp_precheck import attach_mcp_section

    package = _package(tmp_path, None)
    _write(package / "SKILL.md", "---\nname: plugin\ndescription: Skill only.\n---\nBody.\n")
    report = {"status": "complete", "source": str(package), "requirements": [], "errors": []}

    merged = attach_mcp_section(report)

    assert merged["status"] == "complete"
    assert merged["requirements"] == []  # "需要一份 MCP 描述"不进包级 requirements
    assert merged["mcp"]["status"] == "needs-adaptation"


def test_skill_package_manager_gate_sees_mcp_gaps(tmp_path: Path) -> None:
    """端到端：`SkillPackageManager.install` 的闸门必须能看见 MCP 面的缺口。"""
    from agent_harness.skills.package_manager import SkillPackageManager

    package = _package(tmp_path, {"mcpServers": {"login": {
        "type": "http", "url": "https://mcp.example.com/mcp", "oauth": {"callbackPort": 8080},
    }}})
    _write(package / "SKILL.md", "---\nname: plugin\ndescription: Needs OAuth.\n---\nBody.\n")
    manager = SkillPackageManager(tmp_path / "workspace", global_skills_dir=tmp_path / "global")

    record = manager.install(package)

    assert record["compatibility"]["status"] == "needs-adaptation"
    assert any("oauth_required_unsupported" in json.dumps(item) for item in record["compatibility"]["requirements"])


def test_gaps_stay_needs_adaptation_while_errors_are_unsupported(tmp_path: Path) -> None:
    """状态阶梯：**缺口**（待适配）与**错误**（明确失败）必须是两档，不能混。"""
    gap_report = inspect_mcp_servers(_package(tmp_path / "gap", {"mcpServers": {"g": {
        "type": "http", "url": "https://a/mcp", "oauth": {"callbackPort": 1},
    }}}))
    error_report = inspect_mcp_servers(_package(tmp_path / "err", {"mcpServers": {"e": {
        "type": "stdio",
    }}}))

    assert gap_report["status"] == "needs-adaptation"
    assert error_report["status"] == "unsupported"


def test_attach_mcp_section_makes_a_broken_description_block_install(tmp_path: Path) -> None:
    """AC3 要能拦住安装：MCP 描述本身就是坏的时候，包级 status 是 unsupported。"""
    from agent_harness.plugins.mcp_precheck import attach_mcp_section

    package = _package(tmp_path, {"mcpServers": {"broken": {"type": "stdio"}}})  # 缺 command
    report = {"status": "complete", "source": str(package), "requirements": [], "errors": []}

    merged = attach_mcp_section(report)

    assert merged["status"] == "unsupported"
    assert any(item["kind"] == "mcp-server-error" for item in merged["requirements"])


def test_skill_package_manager_refuses_to_install_a_broken_mcp_description(tmp_path: Path) -> None:
    from agent_harness.skills.package_manager import (
        SkillPackageError,
        SkillPackageManager,
    )

    package = _package(tmp_path, {"mcpServers": {"broken": {"type": "stdio"}}})
    _write(package / "SKILL.md", "---\nname: plugin\ndescription: Broken MCP.\n---\nBody.\n")
    manager = SkillPackageManager(tmp_path / "workspace", global_skills_dir=tmp_path / "global")

    # 拒绝理由走的是 Skill 面既有的码表（`_raise_if_uninstallable` 只列错误码）；
    # 关键行为是"装不进去"，MCP 细节在 `report["mcp"]` 与 `requirements` 里可查。
    with pytest.raises(SkillPackageError, match="preflight rejected"):
        manager.install(package)


# ------------------------------------------------------------------ 修复轮 R2：深嵌套描述

def _deep_description(depth: int = 20_000) -> str:
    """约 40 KB 的恶意嵌套描述（20000 层数组 ⇒ 40017 字节）。"""
    return '{"mcpServers": ' + "[" * depth + "0" + "]" * depth + "}"


def test_deeply_nested_description_fails_explicitly_instead_of_crashing(tmp_path: Path) -> None:
    """预检契约是"绝不崩"：深嵌套描述只能落成**明确失败**，不能是 RecursionError。

    `json.loads` 自带的递归限制会先于任何尺寸上限炸（具体层数取决于进程递归限制与
    栈余量，不是常量）；40017 字节又远在 1 MB 读上限之内 ⇒ **深度是唯一的门槛**，
    守卫必须落在解析点。这条与 `skills/inspection.py` 对 frontmatter 的 RecursionError
    兜底同源。
    """
    root = _package(tmp_path, None)
    _write(root / "mcp.json", _deep_description())

    report = inspect_mcp_servers(root)  # 不抛异常本身就是这条用例的一半

    assert report["status"] == "unsupported"
    assert "MCP_DESCRIPTION_TOO_DEEP" in {error["code"] for error in report["errors"]}


def test_install_gate_survives_a_deeply_nested_description(tmp_path: Path) -> None:
    """install 路径同样不得掀桌：拒绝要经 `SkillPackageError`，不是 RecursionError。"""
    from agent_harness.skills.package_manager import (
        SkillPackageError,
        SkillPackageManager,
    )

    package = _package(tmp_path, None)
    _write(package / "mcp.json", _deep_description())
    _write(package / "SKILL.md", "---\nname: plugin\ndescription: Deep MCP.\n---\nBody.\n")
    manager = SkillPackageManager(tmp_path / "workspace", global_skills_dir=tmp_path / "global")

    with pytest.raises(SkillPackageError):
        manager.install(package)


# ------------------------------------------------------------------ 修复轮 R2：作用域外条目

def test_attach_mcp_section_keeps_an_out_of_scope_transport_from_rejecting_the_package(
    tmp_path: Path,
) -> None:
    """旧式 `sse` 条目是**首版不支持的缺口**（ADR-0052 D2 与 OAuth/resources 并列），
    不是"描述写坏"。

    它必须在 MCP 面内明确失败（AC3），但不得把一份 Skill 面健康的包升格成整包拒绝——
    否则一句作用域外的 transport 就能让整个 Skill 装不进来（相对 base 的回归）。
    """
    from agent_harness.plugins.mcp_precheck import attach_mcp_section

    package = _package(tmp_path, {"mcpServers": {"legacy": {
        "type": "sse", "url": "https://old.example.com/sse",
    }}})
    report = {"status": "complete", "source": str(package), "requirements": [], "errors": []}

    merged = attach_mcp_section(report)

    assert merged["mcp"]["status"] == "unsupported"  # MCP 面内仍明确失败（AC3）
    assert merged["status"] == "needs-adaptation"  # 但不传染成整包拒绝
    assert any(item["kind"] == "mcp-server-error" for item in merged["requirements"])


def test_skill_package_manager_installs_a_package_with_an_out_of_scope_transport(
    tmp_path: Path,
) -> None:
    """端到端：旧式 `sse` 的包可装（needs-adaptation），但不可启用（缺口）。"""
    from agent_harness.skills.package_manager import (
        SkillPackageError,
        SkillPackageManager,
    )

    package = _package(tmp_path, {"mcpServers": {"legacy": {
        "type": "sse", "url": "https://old.example.com/sse",
    }}})
    _write(package / "SKILL.md", "---\nname: plugin\ndescription: Legacy transport.\n---\nBody.\n")
    manager = SkillPackageManager(tmp_path / "workspace", global_skills_dir=tmp_path / "global")

    record = manager.install(package)

    assert record["compatibility"]["status"] == "needs-adaptation"
    with pytest.raises(SkillPackageError, match="only complete packages can be enabled"):
        manager.enable("plugin", scope="project")


def test_skill_package_manager_installs_a_skill_package_without_an_mcp_description(
    tmp_path: Path,
) -> None:
    """回归护栏：包里没有 MCP 描述时，一份健康的 Skill 包照常装成 complete。"""
    from agent_harness.skills.package_manager import SkillPackageManager

    package = _package(tmp_path, None)
    _write(package / "SKILL.md", "---\nname: plugin\ndescription: Skill only.\n---\nBody.\n")
    manager = SkillPackageManager(tmp_path / "workspace", global_skills_dir=tmp_path / "global")

    record = manager.install(package)

    assert record["compatibility"]["status"] == "complete"


def test_a_misspelled_transport_still_blocks_install(tmp_path: Path) -> None:
    """收窄只认**本仓不实现的已知 transport**；"字段写坏"（拼错的名字）仍是作者错误。

    同一个 `MCP_TRANSPORT_UNSUPPORTED` 码两种含义，闸门必须按那条 finding 自身判，
    不能按码判——否则一句打错的 `transport: "stido"` 会从"拦安装"滑成"可装"。
    """
    from agent_harness.plugins.mcp_precheck import attach_mcp_section

    package = _package(tmp_path, {"mcpServers": {"typo": {
        "type": "stido", "command": "npx",
    }}})
    report = {"status": "complete", "source": str(package), "requirements": [], "errors": []}

    merged = attach_mcp_section(report)

    assert merged["mcp"]["status"] == "unsupported"
    assert merged["status"] == "unsupported"  # 写坏的描述仍然 fail-closed


def test_a_url_entry_without_a_type_still_blocks_install(tmp_path: Path) -> None:
    """同类：有 url 没 type 是 Claude Code 自己会失败的写法（作者错误），仍拦安装。"""
    from agent_harness.plugins.mcp_precheck import attach_mcp_section

    package = _package(tmp_path, {"mcpServers": {"bad": {"url": "https://mcp.example.com/mcp"}}})
    report = {"status": "complete", "source": str(package), "requirements": [], "errors": []}

    merged = attach_mcp_section(report)

    assert merged["status"] == "unsupported"


def test_native_shape_out_of_scope_transport_also_stays_installable(tmp_path: Path) -> None:
    """本仓形状（`servers` 列表）走的是另一条分派，收窄必须同样接上。

    `_native_transport` 用 `(narrow if declared in _UNIMPLEMENTED_TRANSPORTS else fail)`
    分档——只测 Claude 形状会漏掉这条边；而 native 形状**认得出** sse 却本仓没实现，
    与 Claude 形状同档（可装不可启用），拼错的名字仍拦安装。
    """
    from agent_harness.plugins.mcp_precheck import attach_mcp_section

    package = _package(tmp_path, None)
    _write(package / "mcp.json", json.dumps({"servers": [
        {"name": "legacy", "transport": "sse", "url": "https://old.example.com/sse"},
    ]}))
    report = {"status": "complete", "source": str(package), "requirements": [], "errors": []}

    merged = attach_mcp_section(report)

    assert merged["mcp"]["status"] == "unsupported"  # MCP 面内照旧明确失败（AC3）
    assert merged["status"] == "needs-adaptation"

    typo = _package(tmp_path / "typo", None)
    _write(typo / "mcp.json", json.dumps({"servers": [
        {"name": "bad", "transport": "stido", "command": "npx"},
    ]}))
    mistyped = attach_mcp_section(
        {"status": "complete", "source": str(typo), "requirements": [], "errors": []}
    )

    assert mistyped["status"] == "unsupported"  # 写坏的 transport 仍 fail-closed
