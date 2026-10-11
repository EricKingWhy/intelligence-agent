"""T5：MCP capability 接线（Phase 7 seam 集成）。

MCPServerConnection 打桩（config→连接是唯一注入缝）；验证：
- 正常路径：tools 经 ContributesTools 进 wiring.tools（零旁路）、命名空间正确
- schema 非法 → CapabilityError(init_failed) 响亮失败（Q10）
- 单 server 连接失败 → 隔离降级（其余 server 不受影响，errors 可观察）
- 全部不可达 → capability 跳过注册
- 生命周期：wiring.lifecycle 挂连接，AppState.shutdown 统一 aclose
"""

import json
from typing import ClassVar

import pytest

from agent_harness.capability.base import CapabilityError, CapabilityRegistry
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import wire_capabilities
from agent_harness.config import Settings
from agent_harness.mcp.client import MCPServerDownError
from agent_harness.plugins.mcp_package_manager import MCPPackageManager
from agent_harness.skills.package_manager import SkillPackageError
from tests.mcp_client.fake_server import make_fake_tool


class StubConnection:
    """连接替身：healthy 正常、dead 在 connect 时抛错；记录 aclose。"""

    instances: ClassVar[list["StubConnection"]] = []

    def __init__(self, config):
        self.config = config
        self.connected = False
        self.closed = False
        StubConnection.instances.append(self)

    async def connect(self) -> None:
        if self.config.name == "dead":
            raise MCPServerDownError("unreachable")
        self.connected = True

    async def list_tools(self):
        return [make_fake_tool("echo", read_only=True)]

    async def aclose(self) -> None:
        self.closed = True


_DEFAULT_LIST_TOOLS = StubConnection.list_tools


@pytest.fixture()
def stub_connections(monkeypatch):
    StubConnection.instances = []
    monkeypatch.setattr(StubConnection, "list_tools", _DEFAULT_LIST_TOOLS)
    monkeypatch.setattr(
        "agent_harness.mcp.capability.MCPServerConnection", StubConnection
    )
    return StubConnection


def _settings(servers: list[dict], tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        capabilities=json.dumps({
            "mcp": {"provider": "builtin", "enabled": True,
                    "options": {"servers": servers}},
        }),
    )


def _stdio(name: str, **overrides) -> dict:
    base = {"name": name, "transport": "stdio", "command": "npx", "enabled": True}
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_mcp_wiring_contributes_namespaced_tools(tmp_path, stub_connections):
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry,
        parse_capabilities_config(json.dumps({
            "mcp": {"provider": "builtin", "enabled": True,
                    "options": {"servers": [_stdio("srv1"), _stdio("srv2", enabled=False)]}},
        })),
        settings=_settings([_stdio("srv1"), _stdio("srv2", enabled=False)], tmp_path),
    )
    names = sorted(t.name for t in wiring.tools)
    assert names == ["mcp__srv1__echo"], names
    # disabled server 不连接
    assert [c.config.name for c in stub_connections.instances] == ["srv1"]
    # descriptor 已注册（registry.available 含 mcp）
    assert any(d.name == "mcp" for d in registry.available())
    # capability 挂 lifecycle（aclose 关闭其全部连接，AppState.shutdown 调用）
    assert wiring.lifecycle and wiring.lifecycle[0]._connections[0].config.name == "srv1"


@pytest.mark.asyncio
async def test_mcp_schema_error_is_loud(tmp_path, stub_connections):
    with pytest.raises(CapabilityError, match="mcp"):
        await wire_capabilities(
            CapabilityRegistry(),
            parse_capabilities_config(json.dumps({
                "mcp": {"provider": "builtin", "enabled": True,
                        "options": {"servers": [{"name": "bad", "transport": "stdio"}]}},
            })),
            settings=_settings([{"name": "bad", "transport": "stdio"}], tmp_path),
        )


@pytest.mark.asyncio
async def test_single_server_failure_is_isolated(tmp_path, stub_connections):
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry,
        parse_capabilities_config(json.dumps({
            "mcp": {"provider": "builtin", "enabled": True,
                    "options": {"servers": [_stdio("dead"), _stdio("alive")]}},
        })),
        settings=_settings([_stdio("dead"), _stdio("alive")], tmp_path),
    )
    names = sorted(t.name for t in wiring.tools)
    assert names == ["mcp__alive__echo"], "单 server 故障不得影响其他 server"
    alive = next(c for c in stub_connections.instances if c.config.name == "alive")
    assert alive.connected is True


@pytest.mark.asyncio
async def test_all_servers_down_skips_capability(tmp_path, stub_connections, caplog):
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry,
        parse_capabilities_config(json.dumps({
            "mcp": {"provider": "builtin", "enabled": True,
                    "options": {"servers": [_stdio("dead")]}},
        })),
        settings=_settings([_stdio("dead")], tmp_path),
    )
    assert wiring.tools == []
    assert not any(d.name == "mcp" for d in registry.available())
    assert any("降级" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_zero_tool_capability_still_attached_to_lifecycle(tmp_path, stub_connections):
    """连上了但 tools/list 为空的 server（mis-scoped token 的常见症状）：
    wiring 早退跳过注册时，capability 仍必须挂进 lifecycle——否则 shutdown
    永远不会关闭这些连接（owner task / stdio 子进程泄漏到进程退出）。"""
    async def _empty_tools(self):
        return []

    stub_connections.list_tools = _empty_tools
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry,
        parse_capabilities_config(json.dumps({
            "mcp": {"provider": "builtin", "enabled": True,
                    "options": {"servers": [_stdio("quiet")]}},
        })),
        settings=_settings([_stdio("quiet")], tmp_path),
    )
    assert wiring.tools == []
    assert wiring.lifecycle, "零工具 capability 必须仍挂 lifecycle（连接要能被关闭）"
    await wiring.lifecycle[0].aclose()
    assert stub_connections.instances[0].closed


@pytest.mark.asyncio
async def test_selected_mcp_package_uses_the_existing_runtime_path(
    tmp_path, stub_connections
):
    source = tmp_path / "package"
    source.mkdir()
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "installed", "transport": "stdio", "command": "fake"}
                ]
            }
        ),
        encoding="utf-8",
    )
    global_dir = tmp_path / "home" / "skills"
    manager = MCPPackageManager(tmp_path, global_skills_dir=global_dir)
    manager.install(source, package_id="installed-tools")
    manager.trust("installed-tools")
    manager.enable("installed-tools")
    selected = manager.selected_servers()
    assert [server.name for server in selected] == ["installed"], manager.errors

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        skill_global_dir=str(global_dir),
        model_api_key="sk-test",
    )
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(registry, {}, settings=settings)

    assert [tool.name for tool in wiring.tools] == ["mcp__installed__echo"]
    assert [connection.config.name for connection in stub_connections.instances] == [
        "installed"
    ]
    assert wiring.mcp_packages == [
        {
            "package_id": "installed-tools",
            "scope": "project",
            "sha256": manager.list_packages()["installed-tools"]["current_version"][
                "sha256"
            ],
            "server_names": ["installed"],
        }
    ]


@pytest.mark.asyncio
async def test_disabled_configured_server_does_not_block_package_activation(
    tmp_path, stub_connections
):
    global_dir = tmp_path / "home" / "skills"
    manager = MCPPackageManager(tmp_path, global_skills_dir=global_dir)
    current = tmp_path / "current"
    current.mkdir()
    (current / "mcp.json").write_text(
        json.dumps({"servers": [_stdio("weather")]}), encoding="utf-8"
    )
    manager.install(current, package_id="weather-tools")
    manager.trust("weather-tools")
    manager.enable("weather-tools")

    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / "mcp.json").write_text(
        json.dumps({"servers": [_stdio("reserved")]}), encoding="utf-8"
    )
    manager.update("weather-tools", source=pending)
    manager.trust("weather-tools", pending=True)

    config = parse_capabilities_config(
        json.dumps(
            {
                "mcp": {
                    "provider": "builtin",
                    "enabled": True,
                    "options": {"servers": [_stdio("reserved", enabled=False)]},
                },
            }
        )
    )
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        skill_global_dir=str(global_dir),
        model_api_key="sk-test",
    )

    wiring = await wire_capabilities(CapabilityRegistry(), config, settings=settings)

    record = manager.list_packages()["weather-tools"]
    assert record["pending_version"] is None
    assert record["current_version"]["server_names"] == ["reserved"]
    assert [tool.name for tool in wiring.tools] == ["mcp__reserved__echo"]
    assert [connection.config.name for connection in stub_connections.instances] == [
        "reserved"
    ]


@pytest.mark.asyncio
async def test_mcp_package_registry_oserror_does_not_block_core(
    tmp_path, monkeypatch, caplog
):
    def fail_registry_read(self):
        raise OSError("registry unavailable")

    monkeypatch.setattr(MCPPackageManager, "enable_results", fail_registry_read)
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
    )

    wiring = await wire_capabilities(CapabilityRegistry(), {}, settings=settings)

    assert wiring.tools == []
    assert any(
        "managed MCP package selection is unavailable" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_skills_storage_error_is_translated_at_mcp_boundary(
    tmp_path, monkeypatch, caplog
):
    def fail_scope_root(self):
        raise SkillPackageError("global package root is unsafe")

    monkeypatch.setattr(
        "agent_harness.skills.package_manager.SkillPackageManager._ensure_scope_root",
        fail_scope_root,
    )
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
    )

    wiring = await wire_capabilities(CapabilityRegistry(), {}, settings=settings)

    assert wiring.tools == []
    assert any(
        "managed MCP package selection is unavailable" in record.getMessage()
        and "global package root is unsafe" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_untrusted_selected_mcp_snapshot_is_never_started(
    tmp_path, stub_connections, caplog
):
    source = tmp_path / "package"
    source.mkdir()
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "untrusted", "transport": "stdio", "command": "fake"}
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = MCPPackageManager(
        tmp_path, global_skills_dir=tmp_path / "home" / "skills"
    )
    manager.install(source, package_id="untrusted-tools")
    with manager._locked_registry():
        manifest = manager._read_manifest()
        manifest["enable_results"]["untrusted-tools"] = {
            "selected_scope": "project",
            "source": str(source),
        }
        manager._write_manifest(manifest)

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        skill_global_dir=str(tmp_path / "home" / "skills"),
        model_api_key="sk-test",
    )
    wiring = await wire_capabilities(CapabilityRegistry(), {}, settings=settings)

    assert stub_connections.instances == []
    assert wiring.tools == []
    assert any("untrusted; server not started" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_appstate_shutdown_closes_mcp_lifecycle(tmp_path):
    from agent_harness.web.app import AppState

    class FakeLifecycle:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    state = AppState(Settings(_env_file=None, workspace_dir=str(tmp_path),
                              model_api_key="sk-test"))
    fake = FakeLifecycle()
    state._registry = None
    from agent_harness.capability.wiring import CapabilityWiring
    wiring = CapabilityWiring(lifecycle=[fake])
    state._registry, state._wiring = None, wiring
    await state.shutdown()
    assert fake.closed, "AppState.shutdown 必须关闭 lifecycle 通道"
