"""工具曝光级别（#528 / IMP-11）：定义集控制器 + 内置 tool_search 工具。

机制来源（PORT DESIGN，判定与来源核实见 ``docs/agents/528-research.md``）：
Pi 的 ``ToolExposure`` + ``tool_search``（``packages/coding-agent/src/core/extensions/
types.ts:509``、``extensions/tool-search/tool.ts``，MIT，commit ``28dcce2b``）的
Python 收缩版；Anthropic / OpenAI 的 ``defer_loading`` 是 provider 服务端特性、
模型支持面受限，只借鉴其语义边界（省的是上下文暴露，发现 ≠ 授权）。

职责边界（``AGENTS.md`` §7 不变量）：
- 曝光级别只控制**模型菜单**（``export_model_definitions`` / bind_tools 的定义集），
  不控制执行权——执行权边界仍是 Registry 成员资格 + Permission/Approval；
  ``tool_search`` 发现一个 deferred 工具不构成授权（审计备注硬约束）。
- ``ToolSearchTool`` 是注册进 Registry 的**普通 Tool**，经统一 ToolExecutor 执行，
  不产生第二条调用路径（不变量 #18 / No hidden second path）。
- 本模块不做权限过滤：它搜索的 Registry 已经过 assembly 的档位/权限收窄，
  被剔除的工具物理不在册 ⇒ 搜不到、调不到（与既有 dropped_tools 边界一致）。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from agent_harness.tooling.contract import Tool, ToolExposure, exposure_of
from agent_harness.tooling.registry import ToolRegistry
from agent_harness.tooling.result import ToolResult

_TOKEN_SPLIT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _tokenize(text: str) -> list[str]:
    """小写后按非字母数字（保留 CJK）切词；空段丢弃。"""
    return [t for t in _TOKEN_SPLIT.split(text.lower()) if t]


def _schema_text(args_schema: type[BaseModel]) -> str:
    """参数 schema 的可搜索文本：属性名 + 属性描述（对标 Pi 的搜索文档构成）。"""
    try:
        schema = args_schema.model_json_schema()
    except Exception:  # noqa: BLE001 — schema 反射失败不阻断搜索，退化为仅 name/description
        return ""
    parts: list[str] = []
    for name, prop in (schema.get("properties") or {}).items():
        parts.append(name)
        if isinstance(prop, dict) and isinstance(prop.get("description"), str):
            parts.append(prop["description"])
    return " ".join(parts)


class ToolExposureController:
    """定义集控制器：direct 恒在 + deferred 按激活进入 + hidden 恒不在。

    每个 AgentRuntime 装配面一个实例（assembly 在 Registry 定型后创建）。
    激活集是纯内存态：跨 run 不持久（deferred 工具下一 run 重新发现即可，
    与"完整保存 ≠ 完整注入"同向——发现结果本来就是上下文态而非会话事实）。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._activated: set[str] = set()

    @property
    def activated(self) -> frozenset[str]:
        """当前已激活的 deferred 工具名（只读快照）。"""
        return frozenset(self._activated)

    def current_definitions(self) -> list[dict]:
        """当前应绑定给模型的定义集：direct + 已激活 deferred，按注册顺序。"""
        definitions: list[dict] = []
        for tool in self._registry.list():
            if exposure_of(tool) is ToolExposure.DIRECT or (
                tool.name in self._activated
                and exposure_of(tool) is ToolExposure.DEFERRED
            ):
                definitions.append({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.args_schema.model_json_schema(),
                })
        return definitions

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """按关键词搜索 deferred 工具；命中即激活（幂等），返回命中定义。

        排序：query 词元在搜索文档（name + description + schema 属性名/描述）
        中的子串命中数，降序；同分保持注册顺序。V1 不搬 Pi 的 BM25——
        deferred 工具量级（MCP server 级）下词元命中已够用，复杂度进 Backlog。
        """
        query_tokens = [t for t in _tokenize(query) if t]
        if not query_tokens or limit <= 0:
            return []
        scored: list[tuple[int, int, Tool]] = []
        for order, tool in enumerate(self._registry.list()):
            if exposure_of(tool) is not ToolExposure.DEFERRED:
                continue
            doc_text = " ".join((
                tool.name,
                tool.description,
                _schema_text(tool.args_schema),
            )).lower()
            score = sum(1 for token in query_tokens if token in doc_text)
            if score > 0:
                scored.append((score, order, tool))
        scored.sort(key=lambda item: (-item[0], item[1]))
        matches = [tool for _, _, tool in scored[:limit]]
        self._activated.update(tool.name for tool in matches)
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.args_schema.model_json_schema(),
            }
            for tool in matches
        ]


class ToolSearchTool(Tool):
    """内置工具发现工具：模型先搜、命中后目标工具进入下一轮定义集。

    搜索/激活逻辑全部在 ``ToolExposureController``（本工具只是一个经统一
    Executor 的调用入口）；它自己必须是 ``DIRECT``——模型看不到搜索工具
    就发现不了任何 deferred 工具。
    """

    def __init__(self, controller: ToolExposureController) -> None:
        self._controller = controller

    @property
    def name(self) -> str:
        return "tool_search"

    @property
    def description(self) -> str:
        return (
            "Search for additional tools that are not listed in the current tool "
            "menu. Use this when the task seems to need a capability you do not "
            "see among the available tools (for example a specific domain lookup). "
            "Provide keyword(s) describing the capability; matching tools are "
            "loaded and become callable in the next turn."
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _ToolSearchArgs

    async def execute(self, args: _ToolSearchArgs) -> ToolResult:
        matches = self._controller.search(args.query, limit=args.limit)
        if not matches:
            return ToolResult.success(
                message=f"没有找到与 '{args.query}' 相关的未列出的工具。",
                data={"tools": []},
            )
        loaded = ", ".join(tool["name"] for tool in matches)
        return ToolResult.success(
            message=(
                f"找到 {len(matches)} 个工具并已加载，下一轮起可直接调用：{loaded}。"
                "定义见 data.tools。"
            ),
            data={"tools": matches},
        )


class _ToolSearchArgs(BaseModel):
    """tool_search 参数。"""

    query: str = Field(..., description="描述要找的能力的关键词。")
    limit: int = Field(
        default=5, ge=1, le=50, description="最多返回的匹配工具数。",
    )


__all__ = ["ToolExposureController", "ToolSearchTool"]
