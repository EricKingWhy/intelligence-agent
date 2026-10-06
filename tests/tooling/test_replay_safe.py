"""`Tool.replay_safe` 契约测试（#357 W-13 契约 7）。

`replay_safe` 回答的是**崩溃恢复时本工具被中断后"盲目重跑是否安全"**——
安全默认 False（unverifiable / 未声明即 unsafe），只有 `side_effect == READ_ONLY`
且重执行幂等、无任何外部副作用（含远程）的工具才允许声明 True。

与 `reconcile_hint` 正交：hint 只说"能否在外部核验"，本属性只说"能否安全重放"。
形制取自 Pi durable `replay:"safe"`（双 safe 才重跑）；语义锚不变量 #14
（UNKNOWN 高风险不盲重跑）——本属性只驱动"可与不可"的默认方向，协调器仍不自动重跑。

为什么 MCPTool 即便 `readOnlyHint` 也不声明 True：远程副作用本地不可证，保守。
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from agent_harness.knowledge.tools import (
    IngestDocumentTool,
    ReadKnowledgeSourceTool,
    RetrieveKnowledgeTool,
)
from agent_harness.mcp import MCPServerConfig
from agent_harness.mcp.adapter import MCPTool
from agent_harness.memory.v2.search_tool import RetrieveMemoryV2Tool
from agent_harness.memory.v2.tools import ForgetMemoryV2Tool, RememberMemoryV2Tool
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.tooling import Tool
from agent_harness.tools import (
    ApplyPatchTool,
    BashTool,
    EditTool,
    GitDiffTool,
    GitStatusTool,
    GlobTool,
    GrepTool,
    InspectArtifactTool,
    ReadArtifactTool,
    ReadTool,
    WriteTool,
)
from agent_harness.tools.update_plan import UpdatePlanTool
from tests.mcp_client.fake_server import make_fake_tool


class _MinimalTool(Tool):
    """不覆写任何可选元数据的最小 Tool——验证 ABC 安全默认值。"""

    @property
    def name(self) -> str:
        return "minimal"

    @property
    def description(self) -> str:
        return "minimal tool"

    @property
    def args_schema(self) -> type[BaseModel]:
        class _Args(BaseModel):
            pass

        return _Args

    async def execute(self, args: BaseModel):  # pragma: no cover - 不应被调用
        raise AssertionError("minimal tool 不应被执行")


def _instantiate(tool_cls: type) -> Tool:
    """按构造签名喂 None 造实例：这些构造器只存依赖，属性读取不触碰依赖。"""
    positional = [
        p
        for p in inspect.signature(tool_cls.__init__).parameters.values()
        if p.name != "self" and p.default is p.empty
    ]
    return tool_cls(*([None] * len(positional)))


# ── R1：ABC 安全默认 ──────────────────────────────────────────────


def test_tool_default_replay_safe_is_false() -> None:
    assert _MinimalTool().replay_safe is False


# ── R2：只读、幂等、无外部副作用的工具声明 True ────────────────────

_REPLAY_SAFE_READ_ONLY = [
    ReadTool,
    GrepTool,
    GlobTool,
    GitStatusTool,
    GitDiffTool,
    InspectArtifactTool,
    ReadArtifactTool,
    RetrieveKnowledgeTool,
    ReadKnowledgeSourceTool,
    RetrieveMemoryV2Tool,
]


@pytest.mark.parametrize("tool_cls", _REPLAY_SAFE_READ_ONLY)
def test_read_only_tools_declare_replay_safe(tool_cls: type) -> None:
    assert _instantiate(tool_cls).replay_safe is True


# ── R3：高危 / 写 / 远程 / 委派保持默认 False ──────────────────────

_UNSAFE_TOOLS = [
    BashTool,  # 07 §7：各命令副作用彼此不同，不允许统一假装安全
    WriteTool,
    EditTool,
    ApplyPatchTool,
    UpdatePlanTool,  # 写会话状态（plan_updated）
    RememberMemoryV2Tool,  # MUTATING
    ForgetMemoryV2Tool,  # MUTATING
    IngestDocumentTool,  # 知识库写入
    DelegateTool,  # 多 Agent 副作用不可控
]


@pytest.mark.parametrize("tool_cls", _UNSAFE_TOOLS)
def test_unsafe_tools_keep_replay_safe_false(tool_cls: type) -> None:
    assert _instantiate(tool_cls).replay_safe is False


def _mcp_tool(*, read_only: bool) -> MCPTool:
    remote = make_fake_tool("ro_probe", read_only=read_only)
    config = MCPServerConfig.model_validate(
        {"name": "probe", "transport": "stdio", "command": "npx"}
    )
    return MCPTool(None, config, remote)


def test_mcp_tool_stays_unsafe_even_when_read_only() -> None:
    """远程副作用本地不可证：即便 server 声明 readOnlyHint 也不重放声明。"""
    assert _mcp_tool(read_only=True).replay_safe is False
    assert _mcp_tool(read_only=False).replay_safe is False
