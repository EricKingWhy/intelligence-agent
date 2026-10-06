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

logger = logging.getLogger(__name__)


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
        # #362 / W-18：server 名 → 连接（按 server 断开用；连接按配置顺序先到先得，
        # 同名冲突的工具已在 build 时丢弃，连接本身保留首个）。
        # getattr 防御：测试替身（StubConnection）可能没有 config_name。
        self._by_name: dict[str, MCPServerConnection] = {}
        for connection in connections:
            name = getattr(connection, "config_name", None)
            if name is not None:
                self._by_name.setdefault(name, connection)

    def contributes_tools(self) -> list[Any]:
        return list(self._tools)

    def server_names(self) -> list[str]:
        """已连接的 server 名（#362 / W-18：状态查询用）。"""
        return list(self._by_name)

    def is_connected(self, name: str) -> bool:
        """指定 server 是否仍连接（#362 / W-18）。"""
        connection = self._by_name.get(name)
        return connection is not None and connection.connected

    async def disconnect_server(self, name: str) -> bool:
        """断开指定 server（#362 / W-18：Chrome MCP 手动断开）。

        关闭其连接并从名表中移除；该 server 的工具此后调用失败（连接已死，
        按既有语义走 MCPServerDownError → 失败结果，不静默）。
        返回 True=断开了一个连接；False=没有这个 server（幂等）。
        """
        connection = self._by_name.pop(name, None)
        if connection is None:
            return False
        await _discard_connection(connection)
        return True

    async def aclose(self) -> None:
        for connection in self._connections:
            await _discard_connection(connection)


async def _discard_connection(connection: MCPServerConnection) -> None:
    """降级路径就地关闭连接；清理失败只落日志（降级语义优先于清理噪声）。"""
    try:
        await connection.aclose()
    except Exception:
        logger.debug("MCP 连接清理失败（已按 server 降级，忽略）", exc_info=True)


async def build_mcp_capability(servers: list[MCPServerConfig]) -> MCPCapability:
    """逐 server 连接 + discovery + 工具包装；server 级故障隔离降级（Q10）。

    隔离覆盖整条 per-server 链路（连接 / tools/list / MCPTool 构造）——任何
    一环失败只降级该 server（关闭其连接、errors 可观察），其余 server 与核心
    不受影响（不变量 #21）。
    工具名冲突按 ADR-0012 决策 4 处理：有效注册名（mcp__{server}__{tool}）按
    配置顺序先到先得，冲突工具逐条丢弃并显式记录（`__` 分隔符注入与 server
    端重名都落到同一条规则）——冲突不拖垮 server，也不等到 runtime 注册才炸。
    全部 server 都不可达时 capability 为空（wiring 据此跳过注册）。
    """
    connections: list[MCPServerConnection] = []
    tools: list[MCPTool] = []
    errors: list[str] = []
    claimed: dict[str, str] = {}  # 有效注册名 → 先到 server

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
            # anyio 错误、MCPTool 构造可能抛 pydantic 错误——漏接一种就会让
            # 整个 capability 降级（健康 server 的工具全丢）且已连接的 exit
            # stack 泄漏。按 server 关闭后降级缺席。
            await _discard_connection(connection)
            errors.append(f"server '{server.name}' 接线失败（{type(error).__name__}: {error}），已降级缺席")
            continue
        connections.append(connection)
        for tool in server_tools:
            previous = claimed.get(tool.name)
            if previous is not None:
                errors.append(
                    f"工具 {tool.name} 名字冲突（server '{previous}' 先到，"
                    f"server '{server.name}' 的同名工具按先到先得丢弃）"
                )
                continue
            claimed[tool.name] = server.name
            tools.append(tool)
    return MCPCapability(connections, tools, errors)
