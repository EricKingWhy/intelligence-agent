"""MCP server 连接生命周期（T2/T3，ADR-0012 决策 7）。

- 连接时机 = wiring 时（per-server 超时；仅约束连接建立，不约束工具调用——
  工具调用超时由 adapter 映射 Tool.timeout_seconds、Executor 统一执行，
  避免同一路径两层计时器）。
- 重连只恢复连接、不隐式重执行（Gate 2）：调用中 transport 死亡 → 本次调用
  失败；下次模型主动发起的调用先重连再执行（新调用 ≠ 重放旧调用）。
- 失败语义（Q10）：连接失败向 wiring 抛 MCPServerDownError（降级缺席）；
  协议层错误（McpError）连接仍然存活，原样上抛。
- stdio 启动环境 = OS 必需项白名单 + 配置 env（第三方 server 不可信，
  不继承全量进程 env——C2 同款泄漏防线）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters, types

from agent_harness.mcp.config import MCPServerConfig

try:  # HTTP transport（SDK 2.x）
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # pragma: no cover — SDK 老版本降级路径
    streamable_http_client = None  # type: ignore[assignment]

try:
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover
    stdio_client = None  # type: ignore[assignment]

import httpx2


def build_http_client(config: MCPServerConfig) -> httpx2.AsyncClient:
    """为 http transport 构造带认证 headers 的 HTTP 客户端（SDK 2.x 把
    headers/auth 收敛到预配置的 httpx2 客户端；值已经过 parse 层 ${VAR} 展开）。

    read/write 不设上限：远端工具调用时长由 Tool.timeout_seconds（Executor
    统一执行）约束，双层超时只会产生更难归因的取消；连接与池化给保守上限。
    """
    return httpx2.AsyncClient(
        headers=dict(config.headers) if config.headers else None,
        timeout=httpx2.Timeout(timeout=None, connect=10.0, pool=30.0),
    )

from mcp.shared.exceptions import MCPError

#: stdio server 启动环境的 OS 必需项（与 LocalSubprocessSandbox 白名单同一原则；
#: MCP server 是第三方代码，继承全量 env 等于把部署机密钥喂给不可信进程）。
MCP_STDIO_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
    "HOMEDRIVE", "HOMEPATH", "USERPROFILE", "USERNAME", "OS",
    "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "USER", "LOGNAME", "SHELL",
    "PYTHONIOENCODING", "PYTHONUTF8",
)


class MCPServerDownError(RuntimeError):
    """连接不可用（连接失败 / transport 死亡）——adapter 映射为失败结果。"""


class MCPCallError(RuntimeError):
    """协议层错误（server 拒绝：未知工具 / 非法参数等）——连接仍然存活。"""


def build_stdio_launch_env(config: MCPServerConfig) -> dict[str, str]:
    """stdio server 启动环境：白名单 + 配置 env（已展开），不继承全量 env。"""
    launch_env = {k: os.environ[k] for k in MCP_STDIO_ENV_ALLOWLIST if k in os.environ}
    launch_env.update(config.env)
    return launch_env


#: session_factory 返回 async CM，yield 已初始化的 ClientSession（测试注入点）。
SessionFactory = Callable[[], Any]


class MCPServerConnection:
    """一个 MCP server 的连接生命周期；持有 async exit stack 保活会话。"""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        session_factory: (Callable[[], Any] | None) = None,
    ) -> None:
        """session_factory：测试注入点——返回 async CM，yield 已初始化的
        ClientSession（绕过真实 transport）。生产路径按 transport 走 stdio/http。"""
        self._config = config
        self._session_factory = session_factory
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self.connected = False

    async def connect(self) -> None:
        """建立并初始化会话（wiring 时调用；幂等）。失败抛 MCPServerDownError。

        per-server 超时覆盖**整个建立阶段**（transport 启动 + initialize）——
        挂死的 server 进程/端点在 connect 这一步就会被掐断，而不是把 hang
        带进 wiring。工具调用超时不在这里（Executor 经 Tool.timeout_seconds
        统一执行，避免同一路径两层计时器）。
        """
        if self.connected:
            return
        stack = AsyncExitStack()
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                if self._session_factory is not None:
                    session = await stack.enter_async_context(self._session_factory())
                elif self._config.transport == "stdio":
                    assert self._config.command is not None
                    params = StdioServerParameters(
                        command=self._config.command,
                        args=list(self._config.args),
                        env=build_stdio_launch_env(self._config),
                        cwd=self._config.cwd,
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))  # type: ignore[misc]
                    session = await stack.enter_async_context(ClientSession(read, write))
                else:
                    assert self._config.url is not None
                    if streamable_http_client is None:  # pragma: no cover
                        raise MCPServerDownError("SDK 缺少 streamable HTTP transport")
                    http_client = build_http_client(self._config)
                    stack.push_async_callback(http_client.aclose)
                    read, write = await stack.enter_async_context(
                        streamable_http_client(self._config.url, http_client=http_client)  # type: ignore[misc]
                    )
                    session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
        except asyncio.CancelledError:
            await stack.aclose()
            raise
        except BaseException as error:
            await stack.aclose()
            raise MCPServerDownError(
                f"server '{self._config.name}' 连接失败: {type(error).__name__}"
            ) from error
        self._exit_stack = stack
        self._session = session
        self.connected = True

    async def reconnect(self) -> None:
        """恢复连接（不重执行任何历史调用）。"""
        await self.aclose()
        await self.connect()

    async def list_tools(self) -> list[types.Tool]:
        """discovery：tools/list。要求已连接。"""
        session = self._require_session()
        return (await session.list_tools()).tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """执行远程工具；transport 死亡 → 标记断开 + MCPServerDownError。

        协议层 McpError（未知工具/非法参数）连接仍存活，包成 MCPCallError 上抛。
        下次调用若已断开：先重连（恢复连接），再执行本次模型主动发起的调用。
        """
        if not self.connected:
            await self.reconnect()
        session = self._require_session()
        try:
            return await session.call_tool(name, arguments or {})
        except asyncio.CancelledError:
            raise
        except MCPError as error:
            raise MCPCallError(
                f"server '{self._config.name}' 拒绝调用 {name!r}: {error}"
            ) from error
        except BaseException as error:
            # transport/协议框架死亡（ClosedResourceError 等）→ 连接标记断开；
            # 本次调用失败，下次调用重连。
            self.connected = False
            raise MCPServerDownError(
                f"server '{self._config.name}' transport failure during call {name!r}: "
                f"{type(error).__name__}"
            ) from error

    async def aclose(self) -> None:
        stack, self._exit_stack = self._exit_stack, None
        self._session = None
        self.connected = False
        if stack is not None:
            await stack.aclose()

    def _require_session(self) -> ClientSession:
        if not self.connected or self._session is None:
            raise MCPServerDownError(f"server '{self._config.name}' 未连接")
        return self._session

