"""Measure real MCP discovery schemas; no token estimate is hardcoded (#1360)."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_harness.assembly import BUILTIN_LOCAL_TOOLS
from agent_harness.context.tokens import estimate_tokens
from agent_harness.mcp import MCPServerConfig
from agent_harness.mcp.adapter import MCPTool
from agent_harness.mcp.client import MCPServerConnection
from agent_harness.sandbox.local import LocalSubprocessSandbox
from agent_harness.tooling import ToolRegistry
from agent_harness.tooling.exposure import ToolExposureController, ToolSearchTool
from agent_harness.tools.inspect_artifact import InspectArtifactTool


async def measure(binary: str, output: Path):
    config = MCPServerConfig(
        name='github', transport='stdio', command=binary, args=['stdio', '--toolsets', 'all'],
        # Listing schemas needs no GitHub API call; do not supply a live credential.
        env={'GITHUB_PERSONAL_ACCESS_TOKEN': 'schema-discovery-only'},
    )
    connection = MCPServerConnection(config)
    await connection.connect()
    try:
        discovered = sorted(await connection.list_tools(), key=lambda tool: tool.name)
        if len(discovered) < 50:
            raise RuntimeError(f'Real server exposed {len(discovered)} tools; need >=50')
        chosen = discovered[:50]
        rows = []
        with TemporaryDirectory() as directory:
            sandbox = LocalSubprocessSandbox(directory)
            for server_count in (1, 3):
                registry = ToolRegistry()
                for cls in BUILTIN_LOCAL_TOOLS:
                    if cls(None).name != 'exec':
                        registry.register(cls(sandbox))
                registry.register(InspectArtifactTool(None))
                core_names = [tool.name for tool in registry.list()]
                assert len(core_names) == 10
                for index in range(server_count):
                    server_config = config.model_copy(update={'name': f'github{index}'})
                    for remote in chosen:
                        registry.register(MCPTool(connection, server_config, remote))
                baseline = [registry.model_definition(tool) for tool in registry.list()]
                controller = ToolExposureController(registry)
                registry.register(ToolSearchTool(controller))
                resident = controller.current_definitions()
                encode = lambda data: json.dumps(data, ensure_ascii=False, sort_keys=True)
                before, after = estimate_tokens(encode(baseline)), estimate_tokens(encode(resident))
                rows.append({
                    'servers': server_count, 'tools_per_server': 50,
                    'full_schema_tokens': before, 'resident_tokens': after,
                    'reduction': 1 - after / before, 'core_tools': core_names,
                    'schema_sha256': hashlib.sha256(encode(baseline).encode()).hexdigest(),
                })
        report = {'source': 'github/github-mcp-server v2.0.2, live stdio discovery',
                  'selection': 'first 50 tool names in sorted real discovery result',
                  'discovered_tool_count': len(discovered),
                  'selected_tool_names': [tool.name for tool in chosen],
                  'counter': 'repository estimate_tokens (cl100k_base or conservative UTF-8 fallback)',
                  'cases': rows}
        output.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(report, indent=2) + '\n').encode('utf-8')
        temporary = output.with_suffix('.tmp')
        with temporary.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        assert output.read_bytes() == data
        print(json.dumps(rows))
        assert rows[0]['reduction'] >= 0.60
    finally:
        await connection.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    asyncio.run(measure(options.binary, options.output))
