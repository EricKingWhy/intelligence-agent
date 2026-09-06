"""delegate 工具 + InProcessSubagentProvider（Phase 13 T2, #83, ADR-0015）。

tracer bullet 契约（阻塞串行最小版）：
- delegate {target, task, constraints?} → child 独立 Session（独立 JSONL）
  + AgentRuntime.run → SubAgentResult {agent_id, status, summary} 回填；
- target 必须是预定义 profile，未注册显式报错列出可选；
- child 的 registry 经 Factory 过滤（depth=1：无 delegate），与父共享
  同一 sandbox（spec §9：coding 改动 review 可见）；
- child 失败 → ToolResult.failure（status=failed + summary），决策归 supervisor；
- 未激活（build_runtime 未接好依赖）→ 明确失败，绝不静默。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent.factory import AgentFactory
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.scripted_model import ScriptedModel


class _EchoArgs(BaseModel):
    text: str = Field(default="x", description="回显")


class EchoTool:
    """最小工具替身（不需要完整 Tool 基类继承——registry 只看 name 等属性）。"""

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "read"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EchoArgs

    async def execute(self, args: _EchoArgs) -> object:
        from agent_harness.tooling import ToolResult

        return ToolResult.success(message=args.text)


def _activated_tool(tmp_path: Path, child_model: ScriptedModel | None = None,
                    captured: list | None = None
                    ) -> tuple[DelegateTool, WorkspaceRegistry, str, InProcessSubagentProvider]:
    store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    parent_session_id = "parent-0001"
    workspace_registry.create(parent_session_id, workspace_root=tmp_path / "ws")

    provider_impl = InProcessSubagentProvider()
    tool = DelegateTool(provider_impl)

    child_model = child_model or ScriptedModel([AIMessage(content="child 完成")])

    def executor_factory(registry):
        if captured is not None:
            captured.append(registry)
        return ToolExecutor(registry)

    factory = AgentFactory(model=child_model, primary_model_name="main-model",
                           executor_factory=executor_factory)
    source_registry = ToolRegistry()
    source_registry.register(EchoTool())
    source_registry.register(tool)  # delegate 本身在全量 registry 里（父可见）

    provider_impl.activate(
        factory=factory,
        source_registry=source_registry,
        session_store=store,
        workspace_registry=workspace_registry,
        parent_session_id=parent_session_id,
    )
    return tool, workspace_registry, parent_session_id, provider_impl


def _args(target: str, task: str) -> object:
    return type("_Args", (), {"target": target, "task": task,
                              "constraints": []})()


class TestDelegateTool:
    @pytest.mark.asyncio
    async def test_delegate_spawns_child_and_returns_summary(self, tmp_path):
        captured: list = []
        tool, _, _, _ = _activated_tool(tmp_path, captured=captured)

        result = await tool.execute(_args("coding", "实现一个函数"))

        assert result.ok
        payload = json.loads(result.data["output"])
        assert payload["agent_id"] == "coding"
        assert payload["status"] == "completed"
        assert payload["summary"] == "child 完成"
        # child registry 经过滤：无 delegate（depth=1），有 read
        assert captured and {t.name for t in captured[0].list()} == {"read"}

    @pytest.mark.asyncio
    async def test_child_session_persisted_independently(self, tmp_path):
        tool, _, _, _ = _activated_tool(tmp_path)
        await tool.execute(_args("coding", "实现一个函数"))
        # child JSONL 独立落盘（parent-0001 的事件不在其中）
        roots = list((tmp_path / "sessions").glob("*/events.jsonl"))
        assert roots, "child session 必须独立落盘"

    @pytest.mark.asyncio
    async def test_unknown_target_lists_options(self, tmp_path):
        tool, _, _, _ = _activated_tool(tmp_path)
        result = await tool.execute(_args("nonexistent", "x"))
        assert not result.ok
        assert "coding" in result.message and "research_review" in result.message

    @pytest.mark.asyncio
    async def test_child_failure_yields_failed_tool_result(self, tmp_path):
        child_model = ScriptedModel([AIMessage(content="")])  # 空响应 → run 失败
        tool, _, _, _ = _activated_tool(tmp_path, child_model=child_model)

        result = await tool.execute(_args("coding", "必失败任务"))

        assert not result.ok
        payload = json.loads(result.metadata["output"])
        assert payload["status"] == "failed"
        assert result.retryable is False  # 决策归 supervisor，不自动重试

    @pytest.mark.asyncio
    async def test_child_shares_parent_sandbox(self, tmp_path):
        tool, workspace_registry, parent_session_id, provider = _activated_tool(tmp_path)

        await tool.execute(_args("coding", "x"))

        assert provider.last_child_sessions, "child session 可观测（hub/lineage 挂点）"
        child = provider.last_child_sessions[-1]
        assert child.sandbox is workspace_registry.get(parent_session_id)

    @pytest.mark.asyncio
    async def test_unactivated_provider_fails_explicitly(self, tmp_path):
        from agent_harness.multiagent.provider import InProcessSubagentProvider

        tool = DelegateTool(InProcessSubagentProvider())
        result = await tool.execute(_args("coding", "x"))
        assert not result.ok
        assert "激活" in result.message or "activate" in result.message.lower()

    @pytest.mark.asyncio
    async def test_tool_properties(self, tmp_path):
        from agent_harness.tooling.contract import ToolPermission
        from agent_harness.tooling.reconcile import ReconcileHint

        tool, _, _, _ = _activated_tool(tmp_path)
        assert tool.name == "delegate"
        assert tool.permission == ToolPermission.WORKSPACE_WRITE
        assert isinstance(tool.reconcile_hint, ReconcileHint)


class TestDelegationBudget:
    """#87：max_delegations 按 run 计数；超预算 = 明确失败（不静默截断）。"""

    def _budget_tool(self, tmp_path: Path, max_delegations: int) -> tuple[DelegateTool, InProcessSubagentProvider]:
        from agent_harness.session import run_context_var

        child_model = ScriptedModel([
            AIMessage(content="child 完成") for _ in range(10)
        ])
        tool, _, _, provider = _activated_tool(tmp_path, child_model=child_model)
        tool = DelegateTool(provider, max_delegations=max_delegations)
        token = run_context_var.set("run-budget-1")
        self._token = token
        return tool, provider

    @pytest.mark.asyncio
    async def test_over_budget_fails_explicitly(self, tmp_path):
        tool, _ = self._budget_tool(tmp_path, max_delegations=2)

        r1 = await tool.execute(_args("coding", "任务一"))
        r2 = await tool.execute(_args("coding", "任务二"))
        r3 = await tool.execute(_args("coding", "任务三"))

        assert r1.ok and r2.ok
        assert not r3.ok
        assert "预算耗尽" in r3.message
        assert "2/2" in r3.message, "失败消息必须带已用/上限（模型可决策收尾）"

    @pytest.mark.asyncio
    async def test_budget_resets_per_run(self, tmp_path, caplog):
        """计数按 run_id 隔离：不同 run 各自独立预算。"""
        import logging

        from agent_harness.session import run_context_var

        child_model = ScriptedModel([
            AIMessage(content="child 完成") for _ in range(10)
        ])
        tool, _, _, _ = _activated_tool(tmp_path, child_model=child_model)
        tool = DelegateTool(tool._provider, max_delegations=1)
        t1 = run_context_var.set("run-a")
        try:
            r1 = await tool.execute(_args("coding", "a"))
            over = await tool.execute(_args("coding", "a2"))
        finally:
            run_context_var.reset(t1)
        assert r1.ok and not over.ok

        t2 = run_context_var.set("run-b")
        try:
            with caplog.at_level(logging.DEBUG, logger="agent_harness.agent"):
                r2 = await tool.execute(_args("coding", "b"))
        finally:
            run_context_var.reset(t2)
        for rec in caplog.records:
            if "异常终止" in rec.getMessage():
                print("DEBUG-ERR:", rec.error, "|", rec.error_type)
        assert r2.ok, "新 run 预算必须重置"
