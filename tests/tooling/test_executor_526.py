"""#526 执行域测试：会话级审批授予缓存（A2）+ Plan 模式只读门禁（B1）。

接缝：真 LocalSubprocessSandbox + ToolRegistry + ToolExecutor；会话用真
JsonlSessionStore 上的 Session.start——授权、撤回、工作流档都是 durable
事件投影，全程不依赖内存影子状态。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.session.approval import (
    append_approval_revoke,
    derive_approval_grants,
)
from agent_harness.session.event import PERMISSION_GRANTED
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.session.workflow import WorkflowMode
from agent_harness.tooling import (
    ApprovalRequest,
    ApprovalResponse,
    ErrorCode,
    PermissionPolicy,
    ToolExecutor,
    ToolRegistry,
)
from agent_harness.tooling.approval import PermissionDecision
from agent_harness.tools import BashTool, ReadTool, WriteTool
from agent_harness.tools.update_plan import UpdatePlanTool


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


@pytest.fixture
def registry(sandbox: LocalSubprocessSandbox) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ReadTool(sandbox))
    reg.register(WriteTool(sandbox))
    reg.register(BashTool(sandbox))
    reg.register(UpdatePlanTool())
    return reg


@pytest.fixture
def session(tmp_path: Path) -> Session:
    return Session.start(JsonlSessionStore(root=tmp_path / "sessions"))


def _tc(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"id": call_id, "name": name, "args": args}


class _CountingApproval:
    """计数审批回调：记录被调用次数，恒返回指定 decision（默认会话内批准）。"""

    def __init__(
        self,
        decision: PermissionDecision = PermissionDecision.APPROVE_SESSION,
    ) -> None:
        self.calls = 0
        self._decision = decision

    async def __call__(self, _req: ApprovalRequest) -> ApprovalResponse:
        self.calls += 1
        return ApprovalResponse(approved=True, decision=self._decision)


class TestApprovalSessionGrant:
    @pytest.mark.asyncio
    async def test_approve_session_caches_grant(
        self, sandbox: LocalSubprocessSandbox, registry: ToolRegistry,
        session: Session,
    ):
        """APPROVE_SESSION 落 durable grant；同命令第二次命中缓存不再问人。"""
        approver = _CountingApproval()
        executor = ToolExecutor(
            registry,
            policy=PermissionPolicy.READ_ONLY,
            approval_callback=approver,
        )

        first = await executor.execute(
            _tc("bash", {"command": "echo hi"}, "c1"), session=session
        )
        second = await executor.execute(
            _tc("bash", {"command": "echo hi"}, "c2"), session=session
        )

        assert first.result.ok is True
        assert second.result.ok is True
        # 两次执行只问了一次人——第二次命中会话级 grant。
        assert approver.calls == 1
        assert any(e.type == PERMISSION_GRANTED for e in session.events)

    @pytest.mark.asyncio
    async def test_grant_cache_miss_on_different_command(
        self, sandbox: LocalSubprocessSandbox, registry: ToolRegistry,
        session: Session,
    ):
        """identity 精确到命令原文：不同命令 = 不同 key，需重新审批。"""
        approver = _CountingApproval()
        executor = ToolExecutor(
            registry,
            policy=PermissionPolicy.READ_ONLY,
            approval_callback=approver,
        )

        await executor.execute(
            _tc("bash", {"command": "echo hi"}, "c1"), session=session
        )
        await executor.execute(
            _tc("bash", {"command": "echo other"}, "c2"), session=session
        )

        assert approver.calls == 2

    @pytest.mark.asyncio
    async def test_revoke_forces_reapproval(
        self, sandbox: LocalSubprocessSandbox, registry: ToolRegistry,
        session: Session,
    ):
        """撤回授权 key 后，同命令再次执行必须重新走人工审批。"""
        approver = _CountingApproval()
        executor = ToolExecutor(
            registry,
            policy=PermissionPolicy.READ_ONLY,
            approval_callback=approver,
        )
        await executor.execute(
            _tc("bash", {"command": "echo hi"}, "c1"), session=session
        )
        assert approver.calls == 1

        # 从 durable 投影取 key 再撤回（撤销写口的唯一入口）。
        key = next(iter(derive_approval_grants(session.events)))
        append_approval_revoke(session, key)

        await executor.execute(
            _tc("bash", {"command": "echo hi"}, "c2"), session=session
        )
        assert approver.calls == 2

    @pytest.mark.asyncio
    async def test_grant_respects_policy_change(
        self, sandbox: LocalSubprocessSandbox, registry: ToolRegistry,
        session: Session,
    ):
        """grant 绑定 policy_at_approval：换档后旧授权失效，重新审批。"""
        # 先在 READ_ONLY 下授权 echo hi，落一条 policy_at_approval=read-only 的 grant。
        authorize = _CountingApproval()
        read_only = ToolExecutor(
            registry,
            policy=PermissionPolicy.READ_ONLY,
            approval_callback=authorize,
        )
        await read_only.execute(
            _tc("bash", {"command": "echo hi"}, "c1"), session=session
        )
        assert authorize.calls == 1

        # 换 WORKSPACE_WRITE：bash(DANGER) 仍需审批（needs_approval 为真），
        # 而旧 grant 绑定的 policy 与当前不符 → 失效 → 必须再次调用 callback。
        recheck = _CountingApproval()
        workspace = ToolExecutor(
            registry,
            policy=PermissionPolicy.WORKSPACE_WRITE,
            approval_callback=recheck,
        )
        await workspace.execute(
            _tc("bash", {"command": "echo hi"}, "c2"), session=session
        )
        assert recheck.calls == 1


class TestPlanMode:
    @pytest.mark.asyncio
    async def test_plan_mode_blocks_write(
        self, sandbox: LocalSubprocessSandbox, registry: ToolRegistry,
        session: Session,
    ):
        """Plan 档下可变副作用工具（write）被只读门禁拦下。

        档位经会话事件设定（durable 真相）；构造快照只在无 session 时回落。
        """
        from agent_harness.session.workflow import append_workflow_mode_change

        append_workflow_mode_change(session, WorkflowMode.PLAN)
        executor = ToolExecutor(registry)

        result = await executor.execute(
            _tc("write", {"path": "f.txt", "content": "x"}), session=session
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_plan_mode_allows_read(
        self, sandbox: LocalSubprocessSandbox, registry: ToolRegistry,
        session: Session,
    ):
        """Plan 档允许探索性只读：read 放行。"""
        from agent_harness.session.workflow import append_workflow_mode_change

        append_workflow_mode_change(session, WorkflowMode.PLAN)
        sandbox.write_text("f.txt", "content")
        executor = ToolExecutor(registry)

        result = await executor.execute(
            _tc("read", {"path": "f.txt"}), session=session
        )

        assert result.result.ok is True

    @pytest.mark.asyncio
    async def test_plan_mode_exempts_update_plan(
        self, sandbox: LocalSubprocessSandbox, registry: ToolRegistry,
        session: Session,
    ):
        """update_plan 显式豁免（plan_mode_exempt）：不被 Plan 只读门禁拦截。"""
        from agent_harness.session.workflow import append_workflow_mode_change

        append_workflow_mode_change(session, WorkflowMode.PLAN)
        executor = ToolExecutor(registry)

        result = await executor.execute(
            _tc(
                "update_plan",
                {"items": [{
                    "id": "t1",
                    "content": "探索代码",
                    "activeForm": "探索代码中",
                    "status": "pending",
                    "source": "agent",
                }]},
            ),
            session=session,
        )

        # 只断言没被 Plan 门禁拦；执行成功与否不在此用例范围（无 run 上下文）。
        assert result.result.error_code != ErrorCode.PERMISSION_DENIED
        assert "Plan 模式" not in result.result.message

    @pytest.mark.asyncio
    async def test_normal_mode_unaffected(
        self, sandbox: LocalSubprocessSandbox, registry: ToolRegistry,
        session: Session,
    ):
        """默认 NORMAL 档不受影响：write 正常放行。"""
        executor = ToolExecutor(registry)

        result = await executor.execute(
            _tc("write", {"path": "f.txt", "content": "x"}), session=session
        )

        assert result.result.ok is True

    @pytest.mark.asyncio
    async def test_session_event_mode_wins_over_constructor(
        self, sandbox: LocalSubprocessSandbox, registry: ToolRegistry,
        session: Session,
    ):
        """会话 durable 事件是档位真相：构造快照 NORMAL，但事件切到 plan 即拦截；
        再切回 normal 即放行。"""
        from agent_harness.session.workflow import append_workflow_mode_change

        executor = ToolExecutor(registry)  # 构造快照 NORMAL

        append_workflow_mode_change(session, WorkflowMode.PLAN)
        blocked = await executor.execute(
            _tc("write", {"path": "f.txt", "content": "x"}), session=session
        )
        assert blocked.result.ok is False
        assert blocked.result.error_code == ErrorCode.PERMISSION_DENIED

        append_workflow_mode_change(session, WorkflowMode.NORMAL)
        allowed = await executor.execute(
            _tc("write", {"path": "f.txt", "content": "x"}), session=session
        )
        assert allowed.result.ok is True
