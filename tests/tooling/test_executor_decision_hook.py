"""Executor 与 decision hook 的集成测试（#521 IMP-02）。

断言设计文档 §8 的集成级验收：
- AC-1：未注册 hook 时行为与现状一致（零开销：无 await、无事件）；
- AC-2：DENY → 准入前拒绝：零执行、不占配额、不可重试；
- AC-3：REWRITE → 新 args 重走 Validation：合法则执行新参数，
  非法则 INVALID_ARGUMENT 且零执行；hook 不能抬高权限；
- AC-5/AC-6：hook 失败默认 fail-open（原始参数继续）；
- Hook 能读到 lookup 后的静态元数据（permission/side_effect）。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from agent_harness.tooling.approval import ApprovalRequest, ApprovalResponse
from agent_harness.tooling.contract import (
    PermissionPolicy,
    Tool,
    ToolPermission,
    ToolSideEffect,
)
from agent_harness.tooling.decision_hooks import (
    BeforeToolDecision,
    DecisionHookRunner,
    DecisionRequest,
)
from agent_harness.tooling.executor import ToolExecutor
from agent_harness.tooling.registry import ToolRegistry
from agent_harness.tooling.result import ErrorCode, ToolResult


class _CountArgs(BaseModel):
    value: int = Field(..., description="要计入的值")


class CountingTool(Tool):
    """带调用计数器的工具：证明 DENY/非法改写时 execute 次数为 0。"""

    def __init__(
        self,
        permission: ToolPermission = ToolPermission.READ_ONLY,
        side_effect: ToolSideEffect = ToolSideEffect.READ_ONLY,
    ) -> None:
        self.call_count = 0
        self.last_value: int | None = None
        self._permission = permission
        self._side_effect = side_effect

    @property
    def name(self) -> str:
        return "count"

    @property
    def description(self) -> str:
        return "计入一个整数值。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _CountArgs

    @property
    def permission(self) -> ToolPermission:
        return self._permission

    @property
    def side_effect(self) -> ToolSideEffect:
        return self._side_effect

    async def execute(self, args: _CountArgs) -> ToolResult:
        self.call_count += 1
        self.last_value = args.value
        return ToolResult.success(message=f"计入 {args.value}")


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(CountingTool())
    return reg


@pytest.fixture
def danger_registry() -> ToolRegistry:
    """DANGER 权限工具的 Registry（触发审批关卡用）。"""
    reg = ToolRegistry()
    reg.register(CountingTool(permission=ToolPermission.DANGER))
    return reg


def _tool(reg: ToolRegistry) -> CountingTool:
    return reg.get("count")  # type: ignore[return-value]


class TestNoHookZeroOverhead:
    """AC-1：未注册 hook 时，行为与现状逐字节一致。"""

    @pytest.mark.asyncio
    async def test_no_runner_executes_normally(self, registry: ToolRegistry):
        executor = ToolExecutor(registry)
        execution = await executor.execute(
            {"id": "call-1", "name": "count", "args": {"value": 42}}
        )
        assert execution.result.ok is True
        assert _tool(registry).call_count == 1

    @pytest.mark.asyncio
    async def test_inactive_runner_executes_normally(self, registry: ToolRegistry):
        executor = ToolExecutor(registry, decision_hook_runner=DecisionHookRunner())
        execution = await executor.execute(
            {"id": "call-1", "name": "count", "args": {"value": 42}}
        )
        assert execution.result.ok is True
        assert _tool(registry).call_count == 1


class TestDeny:
    """AC-2：DENY → 准入前拒绝：零执行、不占配额、不可重试。"""

    @pytest.mark.asyncio
    async def test_deny_blocks_execution(self, registry: ToolRegistry):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.deny("policy says no")

        executor = ToolExecutor(
            registry, decision_hook_runner=DecisionHookRunner(_hook)
        )
        execution = await executor.execute(
            {"id": "call-1", "name": "count", "args": {"value": 42}}
        )

        assert execution.result.ok is False
        assert execution.result.error_code is ErrorCode.PERMISSION_DENIED
        assert execution.result.retryable is False
        assert "policy says no" in (execution.result.message or "")
        # 零执行：工具根本没跑
        assert _tool(registry).call_count == 0
        # 不占配额：budget_delta 显式记 0（与 deadline/配额拒绝同族）
        assert execution.budget_delta["tool_calls"] == 0


class TestRewrite:
    """AC-3：REWRITE → 新 args 重走 Validation；hook 不能抬高权限。"""

    @pytest.mark.asyncio
    async def test_rewrite_executes_with_new_args(self, registry: ToolRegistry):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.rewrite({"value": 99})

        executor = ToolExecutor(
            registry, decision_hook_runner=DecisionHookRunner(_hook)
        )
        execution = await executor.execute(
            {"id": "call-1", "name": "count", "args": {"value": 42}}
        )

        assert execution.result.ok is True
        tool = _tool(registry)
        assert tool.call_count == 1
        assert tool.last_value == 99  # 执行的是改写后的参数

    @pytest.mark.asyncio
    async def test_rewrite_invalid_args_rejected(self, registry: ToolRegistry):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.rewrite({"value": "not-an-int"})

        executor = ToolExecutor(
            registry, decision_hook_runner=DecisionHookRunner(_hook)
        )
        execution = await executor.execute(
            {"id": "call-1", "name": "count", "args": {"value": 42}}
        )

        # 新 args 非法 → 走正常 INVALID_ARGUMENT 路径，工具零执行
        assert execution.result.ok is False
        assert execution.result.error_code is ErrorCode.INVALID_ARGUMENT
        assert _tool(registry).call_count == 0

    @pytest.mark.asyncio
    async def test_approval_sees_rewritten_args(self, danger_registry: ToolRegistry):
        """M-3/AC-4：审批关卡对【改写后参数】裁决。

        工具声明 DANGER（默认 WORKSPACE_WRITE 策略下需审批），hook 把
        value 从 1 改写为 999；审批回调必须看到改写后的 args——
        证明"改写前可过、改写后按新参数审批"的关卡生效，hook 无法
        用"先过审批再改写"的手段绕过（改写发生在审批之前）。
        """
        seen_requests: list[ApprovalRequest] = []

        async def _approve(req: ApprovalRequest) -> ApprovalResponse:
            seen_requests.append(req)
            return ApprovalResponse(approved=True)

        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.rewrite({"value": 999})

        tool = _tool(danger_registry)

        executor = ToolExecutor(
            danger_registry,
            policy=PermissionPolicy.WORKSPACE_WRITE,
            approval_callback=_approve,
            decision_hook_runner=DecisionHookRunner(_hook),
        )
        execution = await executor.execute(
            {"id": "call-1", "name": "count", "args": {"value": 1}}
        )

        assert execution.result.ok is True
        assert tool.last_value == 999
        assert len(seen_requests) == 1
        # 审批看到的是改写后的参数，不是原始的 {"value": 1}
        assert seen_requests[0].args == {"value": 999}

    @pytest.mark.asyncio
    async def test_hook_cannot_escalate_permission(self, registry: ToolRegistry):
        """hook 返回值没有 permission 槽位：结构上不可能抬高权限。

        即使 hook 想，BeforeToolDecision 上也没有地方写 permission；
        Executor 用的仍是工具的静态元数据。
        """
        seen: dict = {}

        async def _hook(req: DecisionRequest) -> BeforeToolDecision:
            seen["permission"] = req.permission
            seen["side_effect"] = req.side_effect
            return BeforeToolDecision.allow()

        executor = ToolExecutor(
            registry, decision_hook_runner=DecisionHookRunner(_hook)
        )
        await executor.execute({"id": "call-1", "name": "count", "args": {"value": 1}})
        # hook 读到的是 lookup 后的静态元数据，只能读不能改
        assert seen["permission"] is ToolPermission.READ_ONLY
        assert seen["side_effect"] is ToolSideEffect.READ_ONLY


class TestBatchSerialFallback:
    """M-4/设计 §4.3：存在 rewrite 型 Hook 时，批次保守串行（不变量 #10）。"""

    def test_active_hook_forces_serial(self, registry: ToolRegistry):
        async def _hook(_req: DecisionRequest) -> BeforeToolDecision:
            return BeforeToolDecision.allow()

        executor = ToolExecutor(
            registry, decision_hook_runner=DecisionHookRunner(_hook)
        )
        # 两个 READ_ONLY 工具本可并发，但 hook 存在 → 保守串行
        mode = executor._decide_mode(
            [
                {"id": "c1", "name": "count", "args": {"value": 1}},
                {"id": "c2", "name": "count", "args": {"value": 2}},
            ]
        )
        assert mode == "serial"

    def test_no_hook_keeps_parallel(self, registry: ToolRegistry):
        executor = ToolExecutor(registry)
        mode = executor._decide_mode(
            [
                {"id": "c1", "name": "count", "args": {"value": 1}},
                {"id": "c2", "name": "count", "args": {"value": 2}},
            ]
        )
        assert mode == "parallel"


class TestFailOpen:
    """AC-5：hook 抛异常 → 默认 fail-open，按原始参数继续。"""

    @pytest.mark.asyncio
    async def test_hook_error_fails_open(self, registry: ToolRegistry):
        async def _boom(_req: DecisionRequest) -> BeforeToolDecision:
            raise RuntimeError("hook exploded")

        executor = ToolExecutor(
            registry, decision_hook_runner=DecisionHookRunner(_boom)
        )
        execution = await executor.execute(
            {"id": "call-1", "name": "count", "args": {"value": 42}}
        )

        assert execution.result.ok is True
        tool = _tool(registry)
        assert tool.call_count == 1
        assert tool.last_value == 42  # 原始参数，未被篡改
