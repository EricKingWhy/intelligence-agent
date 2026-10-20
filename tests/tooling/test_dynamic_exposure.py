"""Registry-size exposure policy at the model-definition seam (#1360)."""

import pytest

from agent_harness.mcp import MCPServerConfig
from agent_harness.mcp.adapter import MCPTool
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tooling.exposure import ToolExposureController, ToolSearchTool
from tests.mcp_client.fake_server import FakeMCPServer, make_fake_tool
from tests.mcp_client.test_adapter import _built_tools
from tests.tooling.test_tool_exposure import _ProbeTool


def test_large_mcp_registry_defers_tools_but_preserves_core_menu():
    registry = ToolRegistry()
    core = _ProbeTool(name='read', description='Read a workspace file')
    registry.register(core)
    config = MCPServerConfig(name='catalog', transport='stdio', command='unused')
    for index in range(50):
        registry.register(MCPTool(None, config, make_fake_tool(f'lookup_{index}')))
    assert [d['name'] for d in registry.export_model_definitions()] == ['read']
    controller = ToolExposureController(registry)
    matches = controller.search('lookup_17', limit=1)
    assert matches[0]['name'] == 'mcp__catalog__lookup_17'
    assert [d['name'] for d in controller.current_definitions()] == [
        'read', 'mcp__catalog__lookup_17',
    ]


def test_small_mcp_registry_keeps_existing_definitions():
    registry = ToolRegistry()
    config = MCPServerConfig(name='catalog', transport='stdio', command='unused')
    for index in range(30):
        registry.register(MCPTool(None, config, make_fake_tool(f'lookup_{index}')))
    assert len(registry.export_model_definitions()) == 30


@pytest.mark.asyncio
async def test_discovered_mcp_tool_can_be_searched_activated_and_called():
    server = FakeMCPServer(tools=[
        make_fake_tool(f'lookup_{index}', read_only=True) for index in range(50)
    ])
    config = MCPServerConfig(name='catalog', transport='stdio', command='unused')
    async with _built_tools(config, server) as tools:
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        controller = ToolExposureController(registry)
        registry.register(ToolSearchTool(controller))
        executor = ToolExecutor(registry)
        search = await executor.execute_batch([
            {'id': 'search', 'name': 'tool_search', 'args': {'query': 'lookup_17', 'limit': 1}},
        ])
        assert search[0].result.ok
        assert {d['name'] for d in controller.current_definitions()} == {
            'tool_search', 'mcp__catalog__lookup_17',
        }
        result = await executor.execute_batch([
            {'id': 'lookup', 'name': 'mcp__catalog__lookup_17', 'args': {'text': 'sample'}},
        ])
        assert result[0].result.ok
        assert server.calls[0].name == 'lookup_17'


@pytest.mark.asyncio
async def test_discovery_is_available_in_read_only_plan_mode():
    from agent_harness.session.workflow import WorkflowMode
    from agent_harness.tooling.contract import PermissionPolicy
    from tests.tooling.test_tool_exposure import _registry_with_deferred

    registry = _registry_with_deferred()
    controller = ToolExposureController(registry)
    registry.register(ToolSearchTool(controller))
    result, = await ToolExecutor(
        registry, policy=PermissionPolicy.READ_ONLY, workflow_mode=WorkflowMode.PLAN,
    ).execute_batch([
        {'id': 'search', 'name': 'tool_search', 'args': {'query': 'weather'}},
    ])
    assert result.result.ok
    assert controller.activated == frozenset({'weather_lookup'})
