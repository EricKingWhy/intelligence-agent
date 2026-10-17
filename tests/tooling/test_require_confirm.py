"""#1200 Phase 2 / 裁决 E：MCP server「调用工具前需要我确认」审批拦截。

判据：
- 拦截点必须在规则引擎 `RuleVerdict.ALLOW` 早退之前；
- 会话级 grant / 项目级 ApprovePolicyStore 都不能让确认被跳过（每次调用都问人）；
- 未开启时行为逐字不变（ALLOW 免审批）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import (
    ApprovalRequest,
    ApprovalResponse,
    PermissionPolicy,
    Tool,
    ToolExecutor,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)
from agent_harness.tooling.approval import PermissionDecision
from agent_harness.tooling.permission_rules import (
    PermissionRule,
    PermissionRuleSet,
    RuleVerdict,
)
from agent_harness.tooling.result import ErrorCode


class _Args(BaseModel):
    pass


class _McpLikeTool(Tool):
    def __init__(self, *, require_confirmation: bool) -> None:
        self.require_confirmation = require_confirmation
        self.calls = 0

    @property
    def name(self) -> str:
        return "mcp__github__create_issue"

    @property
    def description(self) -> str:
        return "test MCP tool"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _Args

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, args: BaseModel) -> ToolResult:
        self.calls += 1
        return ToolResult.success(message="ok")


class _Counter:
    def __init__(self, decision: PermissionDecision = PermissionDecision.APPROVE_ONCE) -> None:
        self.calls = 0
        self._decision = decision

    async def __call__(self, _request: ApprovalRequest) -> ApprovalResponse:
        self.calls += 1
        return ApprovalResponse(approved=True, decision=self._decision)


_ALLOW_ALL = PermissionRuleSet(
    allow=(
        PermissionRule(
            name="allow-mcp",
            tools=frozenset({"mcp__github__create_issue"}),
            verdict=RuleVerdict.ALLOW,
            reason="allow",
            source="test",
        ),
    )
)


def _executor(
    tool: Tool,
    *,
    callback=None,
    rules=None,
    project_root: Path | None = None,
    human_approval_available: bool = True,
) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(
        registry,
        policy=PermissionPolicy.READ_ONLY,
        approval_callback=callback,
        permission_rules=rules,
        project_root=project_root,
        human_approval_available=human_approval_available,
    )


def _tc(call_id: str = "c1") -> dict:
    return {"id": call_id, "name": "mcp__github__create_issue", "args": {}}


@pytest.mark.asyncio
async def test_require_confirm_overrides_rule_engine_allow() -> None:
    tool = _McpLikeTool(require_confirmation=True)
    approver = _Counter()
    result = await _executor(tool, callback=approver, rules=_ALLOW_ALL).execute(_tc())
    assert result.result.ok is True
    assert approver.calls == 1, "ALLOW 早退之前必须被 require_confirm 拦截"


@pytest.mark.asyncio
async def test_without_require_confirm_allow_still_short_circuits() -> None:
    tool = _McpLikeTool(require_confirmation=False)
    approver = _Counter()
    result = await _executor(tool, callback=approver, rules=_ALLOW_ALL).execute(_tc())
    assert result.result.ok is True
    assert approver.calls == 0


@pytest.mark.asyncio
async def test_require_confirm_denied_without_callback() -> None:
    tool = _McpLikeTool(require_confirmation=True)
    result = await _executor(tool, rules=_ALLOW_ALL).execute(_tc())
    assert result.result.ok is False
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_session_grant_cannot_skip_confirmation(tmp_path: Path) -> None:
    tool = _McpLikeTool(require_confirmation=True)
    approver = _Counter(PermissionDecision.APPROVE_SESSION)
    executor = _executor(tool, callback=approver, rules=_ALLOW_ALL)
    session = Session.start(JsonlSessionStore(root=tmp_path / "sessions"))

    first = await executor.execute(_tc("c1"), session=session)
    second = await executor.execute(_tc("c2"), session=session)

    assert first.result.ok is True and second.result.ok is True
    assert approver.calls == 2, "会话级 grant 不得替代 require_confirm"


@pytest.mark.asyncio
async def test_policy_store_cannot_skip_confirmation(tmp_path: Path) -> None:
    tool = _McpLikeTool(require_confirmation=True)
    approver = _Counter(PermissionDecision.APPROVE_POLICY)
    executor = _executor(
        tool, callback=approver, rules=_ALLOW_ALL, project_root=tmp_path
    )

    first = await executor.execute(_tc("c1"))
    second = await executor.execute(_tc("c2"))

    assert first.result.ok is True and second.result.ok is True
    assert approver.calls == 2, "项目级持久规则不得替代 require_confirm"


@pytest.mark.asyncio
async def test_require_confirm_fails_closed_without_a_human_channel() -> None:
    """自动批准会话（composition root 用 auto-approve 回调顶替 None）里，
    「调用工具前需要我确认」兑现不了 ⇒ 必须 fail-closed。

    静默放行等于面板承诺了一个做不到的开关：用户勾了确认，工具却直接执行。
    """
    tool = _McpLikeTool(require_confirmation=True)
    auto_approve = _Counter()  # 顶替 None 的非人工回调（assembly.py:756 同形）
    result = await _executor(
        tool, callback=auto_approve, rules=_ALLOW_ALL, human_approval_available=False
    ).execute(_tc())
    assert result.result.ok is False
    assert result.result.error_code is ErrorCode.PERMISSION_DENIED
    assert "人工审批通道" in result.result.message
    assert tool.calls == 0
    assert auto_approve.calls == 0, "没有人工通道时不该再去问那个自动批准回调"


@pytest.mark.asyncio
async def test_without_require_confirm_the_flag_does_not_change_anything() -> None:
    """标记为 False 的工具在无人工通道的会话里照常放行（只关这一件事）。"""
    tool = _McpLikeTool(require_confirmation=False)
    result = await _executor(
        tool, callback=_Counter(), rules=_ALLOW_ALL, human_approval_available=False
    ).execute(_tc())
    assert result.result.ok is True
    assert tool.calls == 1
