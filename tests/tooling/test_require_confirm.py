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
    tool: Tool, *, callback=None, rules=None, project_root: Path | None = None
) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(
        registry,
        policy=PermissionPolicy.READ_ONLY,
        approval_callback=callback,
        permission_rules=rules,
        project_root=project_root,
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
