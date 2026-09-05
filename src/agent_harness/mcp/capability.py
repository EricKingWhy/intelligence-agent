"""MCP Capability Provider（T5）：挂接 Phase 7 registry/wiring 的零旁路集成。

- tools 经 ContributesTools 统一收集进 ToolRegistry（不变量 #7，spec 08 验收
  "插件不能绕过 Tool Permission / Operation Ledger"）。
- 连接生命周期挂到 CapabilityWiring.lifecycle，由 AppState.shutdown 统一关闭。
- 失败语义（Q10）：schema 非法 = 配置错误响亮失败；连接失败 = 环境错误按
  server 隔离降级（单 server 故障不影响其他 server 与核心，不变量 #21）。
"""

from __future__ import annotations

from typing import Any

from agent_harness.mcp.adapter import MCPTool
from agent_harness.mcp.client import MCPServerConnection, MCPServerDownError
from agent_harness.mcp.config import MCPServerConfig, parse_mcp_servers


class MCPCapability:
    """已连接 server 集合的工具贡献者；errors 可观察（不静默）。"""

    def __init__(
        self,
        connections: list[MCPServerConnection],
        tools: list[MCPTool],
        errors: list[str],
    ) -> None:
        self._connections = connections
        self._tools = tools
        self.errors = errors

    def contributes_tools(self) -> list[Any]:
        return list(self._tools)

    async def aclose(self) -> None:
        for connection in self._connections:
            await connection.aclose()


async def build_mcp_capability(servers: list[MCPServerConfig]) -> MCPCapability:
    """逐 server 连接 + discovery；连接失败按 server 隔离降级（Q10）。

    全部 server 都不可达时 capability 为空（wiring 据此跳过注册）。
    """
    connections: list[MCPServerConnection] = []
    tools: list[MCPTool] = []
    errors: list[str] = []
    for server in servers:
        connection = MCPServerConnection(server)
        try:
            await connection.connect()
            remote_tools = await connection.list_tools()
        except MCPServerDownError as error:
            errors.append(f"server '{server.name}' 不可达（{error}），已降级缺席")
            continue
        connections.append(connection)
        tools.extend(MCPTool(connection, server, remote) for remote in remote_tools)
    return MCPCapability(connections, tools, errors)


def _configured_servers(options: dict[str, Any]) -> list[MCPServerConfig]:
    """解析 + 过滤 disabled；schema 非法 → ConfigError（wiring 转响亮失败）。"""
    return [server for server in parse_mcp_servers(options) if server.enabled]
