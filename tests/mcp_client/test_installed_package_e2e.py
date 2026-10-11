"""Installed MCP packages use the official SDK and the existing tool runtime."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from agent_harness import cli
from agent_harness.agent import AgentRuntime
from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.wiring import CapabilityWiring, wire_capabilities
from agent_harness.config import Settings
from agent_harness.session import TOOL_CALL, TOOL_RESULT
from agent_harness.storage import OperationState, SqliteOperationLedger
from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.mcp_client.fake_server import FakeMCPServer, make_fake_tool
from tests.scripted_model import ScriptedModel


def _stdio_server_source() -> str:
    return """import anyio
import os
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

async def list_tools(ctx, params):
    return types.ListToolsResult(tools=[types.Tool(
        name="echo",
        description="Echo text from the installed package",
        inputSchema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        annotations=types.ToolAnnotations(read_only_hint=True),
    )])

async def call_tool(ctx, params):
    with open(os.environ["MCP_TEST_CALL_LOG"], "a", encoding="utf-8") as handle:
        handle.write(params.name + "\\n")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="echo:" + params.arguments["text"])],
        is_error=False,
    )

server = Server("installed-package", on_list_tools=list_tools, on_call_tool=call_tool)

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=True,
        )

anyio.run(main)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_cli_installed_package_uses_real_sdk_and_unified_runtime(
    transport: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_dir = tmp_path / "home" / "skills"
    settings = Settings(
        _env_file=None,
        workspace_dir=str(workspace),
        skill_global_dir=str(global_dir),
        model_api_key="sk-test",
    )
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            workspace_dir=str(workspace), skill_global_dir=str(global_dir)
        ),
    )
    source = tmp_path / "weather-package"
    source.mkdir()
    package_server: FakeMCPServer | None = None
    uvicorn_server = None
    uvicorn_task: asyncio.Task | None = None
    wiring: CapabilityWiring | None = None

    try:
        if transport == "stdio":
            call_log = tmp_path / "stdio-calls.log"
            (source / "server.py").write_text(_stdio_server_source(), encoding="utf-8")
            monkeypatch.setenv(
                "PATH",
                os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]]),
            )
            descriptor = {
                "servers": [
                    {
                        "name": "packaged",
                        "transport": "stdio",
                        "command": "python",
                        "args": ["server.py"],
                        "cwd": ".",
                        "env": {
                            "MCP_TEST_CALL_LOG": str(call_log),
                        },
                    }
                ]
            }
        else:
            import uvicorn

            package_server = FakeMCPServer(
                tools=[make_fake_tool("echo", read_only=True)]
            )
            app = package_server._server.streamable_http_app()
            uvicorn_server = uvicorn.Server(
                uvicorn.Config(
                    app, host="127.0.0.1", port=0, log_level="error", lifespan="on"
                )
            )
            uvicorn_task = asyncio.create_task(uvicorn_server.serve())
            for _ in range(100):
                if uvicorn_server.started:
                    break
                await asyncio.sleep(0.05)
            assert uvicorn_server.started, "fake Streamable HTTP server did not start"
            port = uvicorn_server.servers[0].sockets[0].getsockname()[1]
            descriptor = {
                "servers": [
                    {
                        "name": "packaged",
                        "transport": "http",
                        "url": f"http://127.0.0.1:{port}/mcp",
                    }
                ]
            }
        (source / "mcp.json").write_text(json.dumps(descriptor), encoding="utf-8")

        def run_cli(*args: str) -> None:
            monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "mcp", *args])
            cli.main()
            capsys.readouterr()

        run_cli("install", str(source), "--id", "weather-tools")
        run_cli("trust", "weather-tools")
        run_cli("enable", "weather-tools")

        # A new Runtime assembly is the restart boundary: pending package state is read
        # here, then discovery and calls use the normal MCP SDK and ToolExecutor path.
        registry = CapabilityRegistry()
        wiring = await wire_capabilities(registry, {}, settings=settings)
        assert [tool.name for tool in wiring.tools] == ["mcp__packaged__echo"]
        tool_registry = ToolRegistry()
        for tool in wiring.tools:
            tool_registry.register(tool)
        ledger = SqliteOperationLedger(workspace / "installed-mcp-state.db")
        await ledger.initialize()
        executor = ToolExecutor(
            tool_registry,
            policy=PermissionPolicy.WORKSPACE_WRITE,
            operation_ledger=ledger,
        )
        call_id = f"installed-{transport}-call"
        runtime = AgentRuntime(
            ScriptedModel(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": call_id,
                                "name": "mcp__packaged__echo",
                                "args": {"text": "from installed package"},
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="completed"),
                ]
            ),
            tool_registry,
            executor,
        )
        session = make_session(workspace / f"{transport}-sessions")
        result = await runtime.run(session, "use the installed MCP tool")

        assert result.completed
        operation = await ledger.get(session.session_id, call_id)
        assert operation is not None and operation.state == OperationState.SUCCEEDED
        event_types = [event.type for event in session.events]
        assert event_types.count(TOOL_CALL) == 1
        assert event_types.count(TOOL_RESULT) == 1
        result_event = next(
            event for event in session.events if event.type == TOOL_RESULT
        )
        result_payload = json.loads(result_event.data["content"])
        expected_output = (
            "echo:from installed package" if transport == "stdio" else "echo:echo"
        )
        assert result_payload["data"]["output"] == expected_output
        if transport == "stdio":
            assert call_log.read_text(encoding="utf-8").splitlines() == ["echo"]
        else:
            assert package_server is not None and package_server.call_count == 1
    finally:
        if wiring is not None:
            await wiring.aclose()
        if uvicorn_server is not None and uvicorn_task is not None:
            uvicorn_server.should_exit = True
            try:
                await asyncio.wait_for(uvicorn_task, timeout=5)
            except TimeoutError:
                uvicorn_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await uvicorn_task
