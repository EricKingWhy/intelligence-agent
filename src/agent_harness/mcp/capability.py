"""MCP Capability Provider（T5）：挂接 Phase 7 registry/wiring 的零旁路集成。

- tools 经 ContributesTools 统一收集进 ToolRegistry（不变量 #7，spec 08 验收
  "插件不能绕过 Tool Permission / Operation Ledger"）。
- 连接生命周期挂到 CapabilityWiring.lifecycle，由 AppState.shutdown 统一关闭。
- 失败语义（Q10）：schema 非法 = 配置错误响亮失败；连接失败 = 环境错误按
  server 隔离降级（单 server 故障不影响其他 server 与核心，不变量 #21）。
"""

from __future__ import annotations

import logging
from typing import Any

from agent_harness.mcp.adapter import MCPTool
from agent_harness.mcp.client import MCPServerConnection
from agent_harness.mcp.config import MCPServerConfig


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
        except Exception as error:  # noqa: BLE001 — 隔离必须覆盖任意失败形状：
            # connect 的 MCPServerDownError 之外，tools/list 还可能抛协议错误/
            # anyio 错误——漏接一种就会让整个 capability 降级（健康 server 的
            # 工具全丢）且已连接的 exit stack 泄漏。按 server 关闭后降级缺席。
            try:
                await connection.aclose()
            except Exception:
                logging.getLogger(__name__).debug(
                    "MCP 连接清理失败（已按 server 降级，忽略）", exc_info=True,
                )
            errors.append(f"server '{server.name}' 不可达（{type(error).__name__}: {error}），已降级缺席")
            continue
        connections.append(connection)
        tools.extend(MCPTool(connection, server, remote) for remote in remote_tools)
    return MCPCapability(connections, tools, errors)

