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
    """逐 server 连接 + discovery + 工具包装；server 级故障隔离降级（Q10）。

    隔离覆盖整条 per-server 链路（连接 / tools/list / MCPTool 构造 / 工具名
    冲突检测）——任何一环失败只降级该 server（关闭其连接、errors 可观察），
    其余 server 与核心不受影响（不变量 #21）。
    全部 server 都不可达时 capability 为空（wiring 据此跳过注册）。
    """
    connections: list[MCPServerConnection] = []
    tools: list[MCPTool] = []
    errors: list[str] = []
    # 有效注册名（mcp__{server}__{tool}）→ 先到 server。`__` 分隔符注入
    # （server a__b × 工具 c vs server a × 工具 b__c）与 server 端重复工具名
    # 都能造出同名工具：装配期检出并降级冲突方，不能等到 runtime 注册才炸。
    claimed: dict[str, str] = {}

    for server in servers:
        connection = MCPServerConnection(server)
        try:
            await connection.connect()
            remote_tools = await connection.list_tools()
            # MCPTool 构造必须在隔离内：server 可控的 schema（如把属性命名为
            # pydantic 保护名 model_dump）会让 create_model 抛 ValueError——
            # 漏在隔离外就会拖垮整个 capability 并泄漏已连连接。
            server_tools = [MCPTool(connection, server, remote) for remote in remote_tools]
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
        names_in_server = [tool.name for tool in server_tools]
        duplicated_names = {name for name in names_in_server
                            if names_in_server.count(name) > 1}
        duplicated_names.update(name for name in names_in_server if name in claimed)
        if duplicated_names:
            try:
                await connection.aclose()
            except Exception:
                logging.getLogger(__name__).debug(
                    "MCP 连接清理失败（已按 server 降级，忽略）", exc_info=True,
                )
            errors.append(
                f"server '{server.name}' 工具名冲突 {sorted(duplicated_names)}"
                f"（与更早 server 或本 server 内重复），已降级缺席"
            )
            continue
        for tool in server_tools:
            claimed[tool.name] = server.name
        connections.append(connection)
        tools.extend(server_tools)
    return MCPCapability(connections, tools, errors)

