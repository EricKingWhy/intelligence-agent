"""MCPToolAdapter（T4，ADR-0012 决策 4/5/8）：把 MCP 工具包成统一 Tool 契约。

- 命名 `mcp__{server}__{tool}`（双下划线命名空间段，Claude Code/ZCode 惯例）。
- 权限映射：readOnlyHint=true → READ_ONLY；无注解/非只读 → DANGER（最严默认）；
  server 配置 tool_permissions 可覆写；readOnlyHint 永不升级权限（自报元数据
  不是安全边界）。
- 入参校验：MCP inputSchema → 动态 pydantic 模型（常用子集），server 仍是
  校验权威；schema 复杂/不支持时回退 permissive 模型（不猜、不丢字段）。
- 输出预算：MAX_OUTPUT_CHARS 截断 + 标记（与 read 工具同一哲学）。
- MCP isError → ToolResult.failure（retryable=False，业务错误交模型自纠）；
  transport 死亡 → failure（retryable=False，重连由 connection 在下次调用处理）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from agent_harness.mcp.client import (
    MCPCallError,
    MCPServerConnection,
    MCPServerDownError,
)
from agent_harness.mcp.config import MAX_OUTPUT_CHARS, MCPServerConfig
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode

_TRUNCATION_MARKER = "…[truncated: full output exceeds inline budget]"

_JSON_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """工具注册名：`mcp__{server}__{tool}`（双下划线 = 命名空间分隔）。"""
    return f"mcp__{server_name}__{tool_name}"


def _pydantic_model_from_json_schema(
    schema: dict[str, Any], model_name: str
) -> type[BaseModel]:
    """把 MCP inputSchema（object 子集）转成动态 pydantic 模型。

    支持常用标量/数组/对象属性；schema 形状不可识别（非 object / 空字段）时
    回退到 extra=allow 的宽松模型——server 才是入参校验的权威，本地校验只是
    快速失败礼遇，绝不能因为转换失败而丢工具。
    """
    properties = schema.get("properties") if isinstance(schema, dict) else None
    required = set(schema.get("required", []) if isinstance(schema, dict) else [])
    if not isinstance(properties, dict) or not properties:
        # 回退：宽松模型（extra=allow）——任意 dict 通过，server 才是校验权威。
        return create_model(model_name, __config__=ConfigDict(extra="allow"))

    fields: dict[str, tuple[Any, Any]] = {}
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            prop_schema = {}
        json_type = prop_schema.get("type", "any")
        python_type = _JSON_TYPE_MAP.get(json_type, Any)
        description = prop_schema.get("description", "")
        if prop_name in required:
            fields[prop_name] = (
                python_type,
                Field(..., description=description) if description else ...,
            )
        else:
            default = prop_schema.get("default", None)
            fields[prop_name] = (
                python_type | None,
                Field(default=default, description=description) if description
                else default,
            )
    return create_model(model_name, **fields)


class MCPTool(Tool):
    """一个 remote MCP 工具的统一契约包装（每次连接 discovery 生成一个实例）。"""

    def __init__(
        self,
        connection: MCPServerConnection,
        server_config: MCPServerConfig,
        remote_tool: Any,
    ) -> None:
        self._connection = connection
        self._server_config = server_config
        self._remote_name = remote_tool.name
        self._name = mcp_tool_name(server_config.name, remote_tool.name)
        self._description = (getattr(remote_tool, "description", None) or "").strip()
        annotations = getattr(remote_tool, "annotations", None)
        read_only_hint = bool(getattr(annotations, "read_only_hint", False)) if annotations else False
        self._read_only = read_only_hint

        override = server_config.tool_permissions.get(self._remote_name)
        if override is not None:
            self._permission = override
        elif read_only_hint:
            self._permission = ToolPermission.READ_ONLY
        else:
            self._permission = ToolPermission.DANGER

        self._args_model = _pydantic_model_from_json_schema(
            getattr(remote_tool, "inputSchema", {}) or {},
            f"{'_'.join(p for p in (server_config.name, remote_tool.name) if p)}_args",
        )

    @property
    def remote_name(self) -> str:
        return self._remote_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        # 预算写进 description：模型自知输出会被截断（与 read 工具同一约定）。
        base = self._description or f"远程 MCP 工具 {self._remote_name}。"
        return (
            f"{base}\n"
            f"[MCP server: {self._server_config.name}] "
            f"输出超过 {MAX_OUTPUT_CHARS} 字符将被截断并附标记。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return self._args_model

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.READ_ONLY if self._read_only else ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return self._permission

    @property
    def timeout_seconds(self) -> float:
        return self._server_config.timeout_seconds

    @property
    def reconcile_hint(self) -> ReconcileHint:
        # 远端副作用无法本地验证：安全默认 unverifiable（ NEED_RECONCILE 路径）。
        return ReconcileHint(verifiable=False)

    async def execute(self, args: BaseModel) -> ToolResult:
        try:
            result = await self._connection.call_tool(
                self._remote_name, args.model_dump(exclude_none=True)
            )
        except MCPServerDownError as error:
            return ToolResult.failure(
                message=f"MCP server 不可用：{error}",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
                retryable=False,
            )
        except MCPCallError as error:
            return ToolResult.failure(
                message=f"MCP 调用被拒绝：{error}",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
                retryable=False,
            )
        text = _content_to_text(result.content)
        if result.is_error:
            return ToolResult.failure(
                message=_cap(text),
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
                retryable=False,
            )
        return ToolResult.success(
            message=f"MCP 工具 {self._remote_name} 已执行。",
            data={"output": _cap(text)},
        )


def _content_to_text(content: list[Any]) -> str:
    """把 MCP content blocks 拼成纯文本（TextContent 取 text，其余记类型占位）。"""
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        parts.append(text if isinstance(text, str) else f"[{type(block).__name__}]")
    return "\n".join(parts)


def _cap(text: str, cap: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + _TRUNCATION_MARKER
