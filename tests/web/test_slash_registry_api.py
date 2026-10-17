"""#1200 Phase 3：`GET /api/plugins/slash-registry` —— `/` 菜单唯一注册表。

判据（票面 §5 / 裁决 D2=A）：
- 5 条 builtin 由后端给元数据（#1195 定稿命令表）；
- 启用中的 skill 进同一列表（kind=skill），argument-hint 来自 frontmatter；
- 插件来源 skill 带 namespace（前端渲染 `/plugin:skill`）；
- 禁用的 skill 由后端组装时排除；
- **MCP 工具永不进入**（用户裁决，显式断言）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.mcp.capability import MCPCapability
from agent_harness.mcp.config import MCPServerConfig
from agent_harness.web.app import create_app

BUILTIN_NAMES = ["goal", "plan", "compact", "permission", "model"]


def _entry(
    name: str,
    *,
    description: str = "desc",
    plugin: str | None = None,
    enabled: bool = True,
    meta: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        when_to_use="",
        source_path=Path(f"/ws/skills/{name}/SKILL.md"),
        scope="workspace",
        origin="plugin" if plugin else "project",
        plugin=plugin,
        enabled=enabled,
        meta=meta or {},
    )


class _SkillsCapability:
    """替身：`catalog()` 复刻真实装配语义——wiring 已把 plugin-settings.json 的
    skill_overrides 落到条目的 enabled 上（#1200 / 裁决 C1）。"""

    def __init__(self, entries: list[SimpleNamespace], disabled: set[str]) -> None:
        self._entries = [
            SimpleNamespace(**{**vars(e), "enabled": e.enabled and e.name not in disabled})
            for e in entries
        ]

    def catalog(self):
        return [e for e in self._entries if e.enabled]

    def all_entries(self):
        return list(self._entries)


class _FakeAgent:
    def __init__(self, settings: Settings, wiring) -> None:
        self.settings = settings
        self._wiring = wiring

    async def get_wiring(self):
        return None, self._wiring


def _mcp_capability() -> MCPCapability:
    return MCPCapability(
        connections=[],
        tools=[],
        errors=[],
        configs=[MCPServerConfig.model_validate(
            {"name": "playwright", "transport": "stdio", "command": "npx"}
        )],
        server_tools={
            "playwright": [
                SimpleNamespace(server_name="playwright", remote_name="browser_navigate",
                                name="mcp__playwright__browser_navigate", description="nav")
            ]
        },
    )


def _client(
    tmp_path: Path,
    entries: list[SimpleNamespace] | None,
    *,
    settings_body: dict | None = None,
) -> TestClient:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    if settings_body is not None:
        (workspace / "plugin-settings.json").write_text(
            json.dumps(settings_body), encoding="utf-8"
        )
    settings = Settings(
        _env_file=None, workspace_dir=str(workspace), model_api_key="test-key"
    )
    disabled = {
        name for name, override in (settings_body or {}).get("skill_overrides", {}).items()
        if not override.get("enabled", True)
    }
    wiring = SimpleNamespace(
        lifecycle=[_mcp_capability()],
        skills=_SkillsCapability(entries, disabled) if entries is not None else None,
    )
    app = create_app(settings)
    app.state.agent = _FakeAgent(settings, wiring)
    return TestClient(app)


def _commands(client: TestClient) -> list[dict]:
    with client:
        r = client.get("/api/plugins/slash-registry")
    assert r.status_code == 200, r.text
    return r.json()["commands"]


def test_five_builtins_with_backend_metadata(tmp_path: Path) -> None:
    commands = _commands(_client(tmp_path, []))
    builtins = [c for c in commands if c["kind"] == "builtin"]
    assert [c["name"] for c in builtins] == BUILTIN_NAMES
    for cmd in builtins:
        assert cmd["description"]
        assert "argumentHint" not in cmd
        assert "namespace" not in cmd


def test_skills_join_the_same_registry(tmp_path: Path) -> None:
    commands = _commands(
        _client(
            tmp_path,
            [
                _entry("deep-research", description="深度调研", meta={"argument-hint": "主题…"}),
                _entry("api-design", description="REST API 设计审查"),
            ],
        )
    )
    skills = {c["name"]: c for c in commands if c["kind"] == "skill"}
    assert set(skills) == {"deep-research", "api-design"}
    assert skills["deep-research"]["argumentHint"] == "主题…"
    assert "argumentHint" not in skills["api-design"]
    assert skills["deep-research"]["description"] == "深度调研"
    # 内置与 skill 同处一个列表（D2=A：单一注册表）
    assert [c["name"] for c in commands[: len(BUILTIN_NAMES)]] == BUILTIN_NAMES


def test_plugin_skill_carries_namespace(tmp_path: Path) -> None:
    commands = _commands(
        _client(tmp_path, [_entry("code-review", plugin="acme")])
    )
    skill = next(c for c in commands if c["kind"] == "skill")
    assert skill["namespace"] == "acme"
    assert skill["name"] == "code-review"


def test_disabled_skill_excluded_server_side(tmp_path: Path) -> None:
    commands = _commands(
        _client(
            tmp_path,
            [_entry("code-review"), _entry("api-design")],
            settings_body={"skill_overrides": {"code-review": {"enabled": False}}},
        )
    )
    names = [c["name"] for c in commands]
    assert "api-design" in names
    assert "code-review" not in names


def test_mcp_tools_never_enter_the_registry(tmp_path: Path) -> None:
    commands = _commands(_client(tmp_path, [_entry("api-design")]))
    names = {c["name"] for c in commands}
    assert names == set(BUILTIN_NAMES) | {"api-design"}
    assert not any(name.startswith("mcp__") for name in names)
    assert "browser_navigate" not in names
    # kind 只有两种（MCP 没有第三种 kind）
    assert {c["kind"] for c in commands} <= {"builtin", "skill"}


def test_builtins_survive_without_skills_capability(tmp_path: Path) -> None:
    commands = _commands(_client(tmp_path, None))
    assert [c["name"] for c in commands] == BUILTIN_NAMES


def test_namespace_omitted_when_equal_to_name(tmp_path: Path) -> None:
    """托管安装的 plugin 名就是 skill 名：自指前缀 `/code-review:code-review` 无意义。"""
    commands = _commands(_client(tmp_path, [_entry("code-review", plugin="code-review")]))
    skill = next(c for c in commands if c["kind"] == "skill")
    assert "namespace" not in skill


def test_description_truncated_to_display_cap(tmp_path: Path) -> None:
    """与 `GET /api/skills` 同一展示上限：菜单不能把整篇描述当载荷下发。"""
    commands = _commands(
        _client(tmp_path, [_entry("long", description="字" * 4000)])
    )
    skill = next(c for c in commands if c["kind"] == "skill")
    assert len(skill["description"]) == 1536


def test_description_is_single_line(tmp_path: Path) -> None:
    commands = _commands(
        _client(tmp_path, [_entry("multi", description="first\nsecond")])
    )
    skill = next(c for c in commands if c["kind"] == "skill")
    assert "\n" not in skill["description"]
