"""SessionService 领域层测试（T1 / #131）。

测试 SessionService 在隔离环境下正确封装 session 生命周期操作。
行为与原 Web handler 完全一致——这是纯重构，无行为变化。

Seams:
- SessionService 的公开方法（list_sessions / get_events / has_session /
  create_and_launch / resume_and_launch / stream_reconnect / cancel /
  resolve_approval / recover）。
- 领域异常（SessionNotFound / ActiveRunConflict / InvalidSessionId / 等）
  在正确的场景下被抛出。
"""

from __future__ import annotations

import pytest

from agent_harness.session import USER_MESSAGE, Session
from agent_harness.session.event import TOOL_APPROVAL_REQUESTED
from agent_harness.session.service import (
    ActiveRunConflict,
    ApprovalQueueMissing,
    ApprovalRequestMissing,
    InvalidDecision,
    InvalidSessionId,
    RecoveryConflict,
    SessionNotFound,
    SessionServiceError,
    WorkspaceNameInvalid,
)
from agent_harness.session.store import SessionSummaryStats
from agent_harness.tooling.approval import ApprovalRequest
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.approve_policy import PolicyGranularity
from agent_harness.tooling.contract import PermissionPolicy, ToolPermission
from agent_harness.web.app import session_service

# ── 异常层级 ──────────────────────────────────────────────────────────


class TestExceptionHierarchy:
    """所有领域异常都继承 SessionServiceError，便于调用方统一 catch。"""

    @pytest.mark.parametrize("exc_class", [
        SessionNotFound,
        InvalidSessionId,
        ActiveRunConflict,
        ApprovalQueueMissing,
        ApprovalRequestMissing,
        InvalidDecision,
        RecoveryConflict,
        WorkspaceNameInvalid,
    ])
    def test_inherits_session_service_error(self, exc_class):
        assert issubclass(exc_class, SessionServiceError)


# ── SessionService 实例化 + 属性透传 ─────────────────────────────────


class TestServiceConstruction:
    def test_exposes_store_and_run_manager(self, app_state):
        """SessionService 正确透传 AppState 的核心属性。"""
        service = session_service(app_state)
        assert service.store is app_state.store
        assert service.run_manager is app_state.run_manager
        assert service.approval_queues is app_state.approval_queues
        assert service.workspaces_root is app_state.workspaces_root
        assert service.settings is app_state.settings
        assert service.workspace_registry is app_state.workspace_registry


# ── session_id 安全校验 ──────────────────────────────────────────────


class TestSessionIdValidation:
    """session_id 含路径分隔符 / 绝对路径 / 点点时必须拒绝。"""

    @pytest.mark.parametrize("bad_id", [
        "../etc/passwd",
        "..",
        "C:\\Users\\me",
        "/absolute/path",
        "a/b",
        "a\\b",
    ])
    @pytest.mark.asyncio
    async def test_get_events_rejects_unsafe_id(self, app_state, bad_id):
        service = session_service(app_state)
        with pytest.raises(InvalidSessionId):
            await service.get_events(bad_id)

    @pytest.mark.asyncio
    async def test_has_session_rejects_unsafe_id(self, app_state):
        service = session_service(app_state)
        with pytest.raises(InvalidSessionId):
            await service.has_session("../etc/passwd")

    @pytest.mark.asyncio
    async def test_cancel_rejects_unsafe_id(self, app_state):
        service = session_service(app_state)
        with pytest.raises(InvalidSessionId):
            await service.cancel("../etc/passwd")


# ── get_events / has_session ──────────────────────────────────────────


class TestReadOperations:
    @pytest.mark.asyncio
    async def test_get_events_raises_not_found_for_missing(self, app_state):
        service = session_service(app_state)
        with pytest.raises(SessionNotFound):
            await service.get_events("nonexistent-uuid-here")

    @pytest.mark.asyncio
    async def test_has_session_false_for_missing(self, app_state):
        service = session_service(app_state)
        result = await service.has_session("nonexistent-uuid-here")
        assert result is False

    @pytest.mark.asyncio
    async def test_has_session_true_for_existing(self, app_state, existing_session):
        service = session_service(app_state)
        result = await service.has_session(existing_session)
        assert result is True

    @pytest.mark.asyncio
    async def test_get_events_returns_events_for_existing(
        self, app_state, existing_session
    ):
        service = session_service(app_state)
        events = await service.get_events(existing_session)
        assert len(events) >= 1
        assert events[0].type == "session/started"


# ── list_sessions ────────────────────────────────────────────────────


class TestListSessions:
    @pytest.mark.asyncio
    async def test_empty_when_no_sessions(self, app_state):
        service = session_service(app_state)
        result = await service.list_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_lists_existing_session(self, app_state, existing_session):
        service = session_service(app_state)
        result = await service.list_sessions()
        assert len(result) == 1
        assert result[0].session_id == existing_session
        assert result[0].event_count >= 1

    @pytest.mark.asyncio
    async def test_returns_domain_dataclass_not_dict(self, app_state, existing_session):
        """ARCH-4：列表行是领域 dataclass（不再是 dict 中转）——类型系统能抓住字段漂移。"""
        service = session_service(app_state)
        result = await service.list_sessions()
        assert isinstance(result[0], SessionSummaryStats)

    @pytest.mark.asyncio
    async def test_carries_terminal_trace_id(self, app_state, existing_session):
        """OBS-010 + ARCH-4b：列表行回填最近一次 run 终结事件的 trace_id 与 trace_url。"""
        session = Session.resume(app_state.store, existing_session)
        run_id, _ = session.begin_run()
        session.append(USER_MESSAGE, {"content": "hi"}, run_id=run_id)
        session.end_run(run_id, status="completed", final_text="ok",
                        trace_id="tr-list",
                        trace_url="https://lf.example/trace/tr-list")

        service = session_service(app_state)
        result = await service.list_sessions()
        row = next(r for r in result if r.session_id == existing_session)
        assert (row.trace_id, row.trace_url) == (
            "tr-list", "https://lf.example/trace/tr-list")

    @pytest.mark.asyncio
    async def test_trace_id_none_without_terminal_event(self, app_state, existing_session):
        """只有 session/started（无 run 终态）→ trace_id / trace_url 都为 None，不伪造。"""
        service = session_service(app_state)
        result = await service.list_sessions()
        row = next(r for r in result if r.session_id == existing_session)
        assert (row.trace_id, row.trace_url) == (None, None)


# ── cancel ───────────────────────────────────────────────────────────


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_raises_not_found_for_missing(self, app_state):
        service = session_service(app_state)
        with pytest.raises(SessionNotFound):
            await service.cancel("nonexistent-uuid-here")

    @pytest.mark.asyncio
    async def test_cancel_returns_false_when_no_active_run(
        self, app_state, existing_session
    ):
        """没有在途 run 时幂等返回 False。"""
        service = session_service(app_state)
        result = await service.cancel(existing_session)
        assert result is False


# ── resolve_approval ──────────────────────────────────────────────────


class TestResolveApproval:
    @pytest.mark.asyncio
    async def test_raises_not_found_for_missing_session(self, app_state):
        service = session_service(app_state)
        with pytest.raises(SessionNotFound):
            await service.resolve_approval(
                session_id="nonexistent-uuid-here",
                approval_id="some-id",
            )

    @pytest.mark.asyncio
    async def test_raises_when_no_queue(self, app_state, existing_session):
        """session 存在但没有交互式审批队列 → ApprovalQueueMissing。"""
        service = session_service(app_state)
        with pytest.raises(ApprovalQueueMissing):
            await service.resolve_approval(
                session_id=existing_session,
                approval_id="some-id",
            )

    @pytest.mark.asyncio
    async def test_policy_granularity_reaches_response(self, app_state):
        """#684 P1-1：审批粒度经 resolve_approval 透传进 ApprovalResponse。

        第三档「以后都允许」的粒度若不落到 response，ToolExecutor 就永远只能装
        exact 档——本测试锁住这条生产链路。
        """
        sid, approval_id, _queue = _seed_pending_policy_approval(app_state)
        service = session_service(app_state)
        result = await service.resolve_approval(
            session_id=sid,
            approval_id=approval_id,
            decision="approve_policy",
            policy_granularity="command",
        )
        assert result.decision.value == "approve_policy"
        assert result.response.policy_granularity is PolicyGranularity.COMMAND

    @pytest.mark.asyncio
    async def test_policy_granularity_defaults_to_exact(self, app_state):
        """#684：缺省粒度由 ApprovalResponse 归一为 exact（不猜 command）。"""
        sid, approval_id, _queue = _seed_pending_policy_approval(app_state)
        service = session_service(app_state)
        result = await service.resolve_approval(
            session_id=sid,
            approval_id=approval_id,
            decision="approve_policy",
        )
        assert result.response.policy_granularity is PolicyGranularity.EXACT

    @pytest.mark.asyncio
    async def test_invalid_policy_granularity_rejected(self, app_state):
        """非法粒度 = 违反契约 → InvalidDecision（422），绝不静默回落（F22）。"""
        sid, approval_id, _queue = _seed_pending_policy_approval(app_state)
        service = session_service(app_state)
        with pytest.raises(InvalidDecision):
            await service.resolve_approval(
                session_id=sid,
                approval_id=approval_id,
                decision="approve_policy",
                policy_granularity="fuzzy",
            )

    @pytest.mark.asyncio
    async def test_valid_decision_not_in_allowed_rejected(self, app_state):
        """合法枚举但不在 requested 事件的 allowed_decisions → InvalidDecision（422）。

        batch-51 G3 原以 bash e2e 钉此边界（当时 allowed=[deny, approve_once]）；
        #526/#684 后可缓存审批的 allowed 含全部四档，e2e 前提消失——失败关闭钉
        下沉到本处（service.resolve_approval 是该边界的执行点）。
        """
        sid, approval_id, _queue = _seed_pending_policy_approval(app_state)
        service = session_service(app_state)
        with pytest.raises(InvalidDecision):
            await service.resolve_approval(
                session_id=sid,
                approval_id=approval_id,
                decision="approve_session",
            )
        # 拒绝路径零持久化副作用（审查 P3-2）：allowed 检查先于 resolve，
        # 同一审批未被消费——随后合法决策仍可正常解决；若未来顺序回归
        # （先 resolve 后校验），这里会以 ApprovalAlreadyResolved 变红。
        result = await service.resolve_approval(
            session_id=sid,
            approval_id=approval_id,
            decision="approve_policy",
            policy_granularity="exact",
        )
        assert result.decision.value == "approve_policy"


def _seed_pending_policy_approval(app_state):
    """建一个 session + live queue + 一条允许 approve_policy 的待审批请求。

    返回 `(session_id, approval_id, queue)`；`app_state.approval_queues` 是
    `resolve_approval` 查找 live resolver 的唯一入口（同生产装配）。
    """
    session = Session.start(app_state.store)
    queue = PendingApprovalQueue()
    request = ApprovalRequest(
        tool_name="bash",
        args={"command": "echo hi"},
        permission=ToolPermission.DANGER,
        policy=PermissionPolicy.WORKSPACE_WRITE,
        reason="r",
        tool_call_id="c1",
        approval_key="key-1",
    )
    approval_id = queue.register(request)
    session.append(
        TOOL_APPROVAL_REQUESTED,
        {
            "approval_id": approval_id,
            "tool_name": "bash",
            "allowed_decisions": ["approve_once", "deny", "approve_policy"],
        },
    )
    app_state.approval_queues[session.session_id] = queue
    return session.session_id, approval_id, queue


# ── workspace name 校验 ──────────────────────────────────────────────


class TestWorkspaceNameValidation:
    @pytest.mark.parametrize("bad_name", [
        "../etc",
        "..",
        "C:\\Users\\me",
        "/absolute",
        "a/b",
        "a\\b",
    ])
    def test_workspace_validation_rejects_paths(self, app_state, bad_name):
        """workspace 含路径 → WorkspaceNameInvalid。"""
        service = session_service(app_state)
        with pytest.raises(WorkspaceNameInvalid):
            service._validate_workspace_name(bad_name)

    def test_workspace_none_returns_none(self, app_state):
        """workspace=None → 返回 None（向后兼容）。"""
        service = session_service(app_state)
        assert service._validate_workspace_name(None) is None


@pytest.mark.asyncio
async def test_list_without_workspace_index_rows_ungrouped(
    make_session_service, tmp_path
) -> None:
    """#516 回归（原 tests/web 私有断言迁移）：装配里没有 workspace 索引
    （CLI / 无 header 的 RecoveryStores）→ 列表行全部未分组（workspace=None）。

    这不是"查不到"，而是**根本没有查的地方**——refs 构建分支在 index 为 None
    时直接给空映射，绝不伪造。
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_harness.session import JsonlSessionStore, Session

    store = JsonlSessionStore(root=tmp_path / "sessions")
    Session.start(store)
    service = make_session_service(
        store=store,
        session_meta_store=SimpleNamespace(list_all=AsyncMock(return_value=[])),
    )

    result = await service.list_sessions()

    assert len(result) == 1
    assert result[0].workspace is None


class TestApprovalQueueGcIdentity:
    """#545 review（A 轴 P1）：run 收尾 GC 只许删**本 run 自己注册**的队列。

    接力（ADR-0030 §4.7）/ 立即续聊路径会在旧 run 的 done-callback 触发**之前**，
    为下一个 run 注册新队列（共享同一 session_id 键）——无条件按键 pop 会把
    下一个 run 的队列误删，其审批决策从此 404（ApprovalQueueMissing），拖到
    fail-closed 超时 deny：human-in-the-loop 系统性失效。

    确定性：GC 是否已执行不可直接观察，故在同一 task 上**后挂**一个探针
    done-callback（asyncio 按注册序执行）——探针置位即证明 GC 回调已跑，
    断言不靠 sleep 猜时序。
    """

    @staticmethod
    def _run_with_gc(service, session_id):
        import asyncio
        from types import SimpleNamespace

        gc_ran = asyncio.Event()

        async def _body():
            await asyncio.sleep(0)

        task = asyncio.create_task(_body())

        def _probe(_t):
            gc_ran.set()

        service._attach_approval_queue_gc(SimpleNamespace(task=task), session_id)
        task.add_done_callback(_probe)
        return task, gc_ran

    @pytest.mark.asyncio
    async def test_gc_does_not_delete_the_next_runs_queue(self, app_state):
        service = session_service(app_state)
        session_id = "gc-identity-session"
        own_queue, next_queue = object(), object()

        service.approval_queues[session_id] = own_queue
        task, gc_ran = self._run_with_gc(service, session_id)
        # 旧 run 收尾前，下一个 run 已经注册了自己的队列（接力路径的时序）。
        service.approval_queues[session_id] = next_queue
        await task
        await gc_ran.wait()
        assert service.approval_queues.get(session_id) is next_queue, (
            "旧 run 的 GC 不得误删下一个 run 的审批队列"
        )

    @pytest.mark.asyncio
    async def test_gc_still_removes_its_own_queue(self, app_state):
        service = session_service(app_state)
        session_id = "gc-own-session"

        service.approval_queues[session_id] = object()
        task, gc_ran = self._run_with_gc(service, session_id)
        await task
        await gc_ran.wait()
        assert session_id not in service.approval_queues, "防泄漏语义必须保留"
