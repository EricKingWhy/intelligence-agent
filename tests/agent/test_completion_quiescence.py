"""完成闸门（Runtime Quiescence + CompletionPolicy）的确定性用例（T8 / `#316`）。

票面 Verification 的两层：
- **纯函数层**：`collect_quiescence_report` 的六条谓词各自独立、组合与投影形状；
- **全链层**：`AgentRuntime.run` 走真实完成边界（ScriptedModel + 生产 ToolExecutor +
  真 SQLite Ledger），逐类验证"未解工作独立阻断 `run/completed`"、默认策略静止后接受、
  自定义策略拒绝且**不改动任何 durable 状态**、结清后重入同一闸门且只留一个终态。

机制与取舍见 `docs/adr/0047-completion-quiescence-and-completion-policy.md`。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.agent.completion import (
    POLICY_REJECTED_PREFIX,
    QUIESCENCE_ACTIVE_CHILD,
    QUIESCENCE_BLOCKED_PREFIX,
    QUIESCENCE_DANGLING_TOOL,
    QUIESCENCE_NEW_TOOL_CALLS,
    QUIESCENCE_PENDING_RECONCILE,
    QUIESCENCE_UNRESOLVED_APPROVAL,
    QUIESCENCE_UNSETTLED_OPERATION,
    CompletionDecision,
    CompletionPolicy,
    DefaultCompletionPolicy,
    QuiescenceBlocker,
    QuiescenceReport,
    collect_quiescence_report,
)
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_QUIESCENCE_BLOCKED
from agent_harness.session import (
    AGENT_DELEGATION_FINISHED,
    AGENT_DELEGATION_STARTED,
    Session,
)
from agent_harness.session.approval import unresolved_approval_ids
from agent_harness.session.event import (
    MODEL_COMPLETED,
    OPERATION_RECONCILE_REQUIRED,
    PERMISSION_RESOLVED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_STARTED,
    RUN_TERMINAL_TYPES,
    TOOL_APPROVAL_REQUESTED,
    TOOL_CALL,
    TOOL_RESULT,
    SessionEvent,
)
from agent_harness.session.service import _InteractiveCallbackHolder
from agent_harness.storage import SqliteOperationLedger
from agent_harness.storage.operation import (
    Operation,
    OperationState,
    needs_reconcile,
    unproven_meta,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.contract import ToolPermission
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _NoArgs(BaseModel):
    pass


class _EchoTool(Tool):
    """一次真实（但无副作用）的工具调用：给全链用例一个可结清的 Operation。"""

    @property
    def name(self) -> str:
        return "probe"

    @property
    def description(self) -> str:
        return "Return a fixed payload."

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        return ToolResult.success("probe ok")


class _DangerTool(_EchoTool):
    """DANGER 级调用：在默认 WORKSPACE_WRITE policy 下必须过审批关卡。"""

    @property
    def name(self) -> str:
        return "danger"

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER


class _RecordingPolicy(CompletionPolicy):
    """记录"被调用过几次 / 拿到的是哪种报告"，并可按脚本拒绝。"""

    def __init__(self, *, accepted: bool = True, reason: str | None = None) -> None:
        self._accepted = accepted
        self._reason = reason
        self.calls: list[QuiescenceReport] = []

    async def decide(
        self, *, report: QuiescenceReport, final_text: str, run_id: str,
    ) -> CompletionDecision:
        self.calls.append(report)
        return CompletionDecision(accepted=self._accepted, reason=self._reason)


def _event(event_type: str, data: dict) -> SessionEvent:
    """构造一条"够用"的 SessionEvent（纯函数层用例只读 type/data）。"""
    return SessionEvent(type=event_type, data=data)


def _events_with_model_tool_call(tool_call_id: str) -> list[SessionEvent]:
    """一次"崩溃在工具执行前"的最小事件序列：模型请求过、结果从未落盘。"""
    return [
        _event(
            MODEL_COMPLETED,
            {
                "content": "",
                "tool_calls": [{"id": tool_call_id, "name": "probe", "args": {}}],
            },
        ),
    ]


def _operation(
    tool_call_id: str, *, session_id: str = "s1",
    state: OperationState = OperationState.PENDING,
    reconcile_meta: str | None = None,
) -> Operation:
    return Operation(
        tool_call_id=tool_call_id, session_id=session_id, run_id="run-1",
        tool_name="probe", args_identity="{}", state=state,
        reconcile_meta=reconcile_meta,
    )


def _durable_snapshot(session: Session) -> list[tuple[int, str, str]]:
    """会话 durable 事实的快照（seq / type / data）——用于"没有副作用"断言。"""
    return [
        (event.seq, event.type, json.dumps(event.data, sort_keys=True, ensure_ascii=False))
        for event in session.events
    ]


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    return registry


def _runtime(
    *, policy: CompletionPolicy | None = None, ledger: SqliteOperationLedger | None = None,
    tool_call_id: str = "call-probe",
) -> AgentRuntime:
    """一次"两步"执行：先调一次 probe 工具，再给出最终回答（走到完成闸门）。

    `tool_call_id` 可换：同一会话上跑第二次执行时**必须**换新的 id——账本主键是
    (session_id, tool_call_id)，沿用同一个 id 会撞既有行（生产里模型每次生成新 id）。
    """
    registry = _registry()
    return AgentRuntime(
        ScriptedModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"id": tool_call_id, "name": "probe", "args": {}}],
                ),
                AIMessage(content="done"),
            ]
        ),
        registry,
        ToolExecutor(registry, operation_ledger=ledger),
        completion_policy=policy,
    )


async def _seed_operation(
    ledger: SqliteOperationLedger, session: Session, *, tool_call_id: str,
    state: OperationState = OperationState.PENDING,
    reconcile_meta: str | None = None,
) -> None:
    """按状态机的合法链把一行推到目标状态（PENDING → RUNNING → UNKNOWN → NEED_RECONCILE）。"""
    await ledger.create(
        _operation(tool_call_id, session_id=session.session_id),
    )
    if state is OperationState.PENDING:
        return
    await ledger.update_state(session.session_id, tool_call_id, OperationState.RUNNING)
    if state is OperationState.RUNNING:
        return
    await ledger.update_state(session.session_id, tool_call_id, OperationState.UNKNOWN)
    if state is OperationState.UNKNOWN:
        return
    await ledger.update_state(
        session.session_id, tool_call_id, OperationState.NEED_RECONCILE,
        reconcile_meta=reconcile_meta,
    )


def _terminal_events(session: Session) -> list[str]:
    return [e.type for e in session.events if e.type in RUN_TERMINAL_TYPES]


# ---------------------------------------------------------------------------
# 纯函数层：六条谓词
# ---------------------------------------------------------------------------


def test_quiescent_report_when_nothing_is_outstanding() -> None:
    report = collect_quiescence_report(events=[], new_tool_calls=False, operations=[])

    assert report.quiescent is True
    assert report.refusal_reason() is None
    assert report.kinds == ()
    assert report.as_projection() == {"quiescent": True, "blockers": []}


def test_dangling_tool_call_is_a_blocker() -> None:
    """谓词 1：`model/completed` 请求过、但会话里没有对应 `tool/result`。"""
    report = collect_quiescence_report(
        events=_events_with_model_tool_call("call-dangling"), new_tool_calls=False,
    )

    assert [b.kind for b in report.blockers] == [QUIESCENCE_DANGLING_TOOL]
    assert report.blockers[0].refs == ("call-dangling",)


def test_a_synthesised_recovery_result_clears_the_dangling_predicate() -> None:
    """`02 §5.4` 第 1 条的"或恢复分类"：补过 `tool/result` 的调用不再算悬空。"""
    events = [
        *_events_with_model_tool_call("call-dangling"),
        _event(TOOL_RESULT, {"tool_call_id": "call-dangling", "content": "恢复分类"}),
    ]

    assert collect_quiescence_report(events=events, new_tool_calls=False).quiescent


def test_unresolved_approval_is_a_blocker() -> None:
    """谓词 2：`tool/approval-requested` 没有配对的 `permission/resolved`。"""
    events = [
        _event(TOOL_APPROVAL_REQUESTED, {"approval_id": "a-1", "tool_call_id": "c-1"}),
        _event(TOOL_APPROVAL_REQUESTED, {"approval_id": "a-2", "tool_call_id": "c-2"}),
        _event(PERMISSION_RESOLVED, {"approval_id": "a-2", "decision": "deny"}),
    ]

    report = collect_quiescence_report(events=events, new_tool_calls=False)

    assert [b.kind for b in report.blockers] == [QUIESCENCE_UNRESOLVED_APPROVAL]
    assert report.blockers[0].refs == ("a-1",)


def test_unfinished_child_agent_is_a_blocker() -> None:
    """谓词 3：`agent/delegation-started` 没有配对的 `-finished`。"""
    events = [
        _event(AGENT_DELEGATION_STARTED, {"child_session_id": "child-1"}),
        _event(AGENT_DELEGATION_STARTED, {"child_session_id": "child-2"}),
        _event(AGENT_DELEGATION_FINISHED, {"child_session_id": "child-2"}),
    ]

    report = collect_quiescence_report(events=events, new_tool_calls=False)

    assert [b.kind for b in report.blockers] == [QUIESCENCE_ACTIVE_CHILD]
    assert report.blockers[0].refs == ("child-1",)


@pytest.mark.parametrize(
    "state",
    [OperationState.PENDING, OperationState.RUNNING, OperationState.UNKNOWN],
)
def test_unsettled_operation_is_a_blocker(state: OperationState) -> None:
    """谓词 4：账本行停在 PENDING / RUNNING / UNKNOWN（未定 reconcile 状态）。"""
    report = collect_quiescence_report(
        events=[], new_tool_calls=False, operations=[_operation("c-1", state=state)],
    )

    assert [b.kind for b in report.blockers] == [QUIESCENCE_UNSETTLED_OPERATION]
    assert report.blockers[0].refs == ("c-1",)


@pytest.mark.parametrize(
    "operation",
    [
        _operation("c-1", state=OperationState.NEED_RECONCILE),
        _operation(
            "c-1",
            state=OperationState.SUCCEEDED,
            reconcile_meta=unproven_meta(error_code="TIMEOUT", note="未证"),
        ),
    ],
)
def test_pending_reconcile_is_a_blocker(operation: Operation) -> None:
    """谓词 5：已进对账流程的欠账——`NEED_RECONCILE` 行，或带"副作用未证"标记的行。"""
    report = collect_quiescence_report(events=[], new_tool_calls=False, operations=[operation])

    assert [b.kind for b in report.blockers] == [QUIESCENCE_PENDING_RECONCILE]
    assert report.blockers[0].refs == ("c-1",)


def test_new_tool_calls_is_a_blocker() -> None:
    """谓词 6：最新被接纳的模型决策仍在请求工具。"""
    report = collect_quiescence_report(events=[], new_tool_calls=True)

    assert [b.kind for b in report.blockers] == [QUIESCENCE_NEW_TOOL_CALLS]
    assert report.blockers[0].refs == ()


def test_settled_operations_of_every_terminal_state_are_not_blockers() -> None:
    """终态行（无未证标记）不是 blocker——否则任何跑过工具的会话都无法完成。"""
    operations = [
        _operation("c-1", state=OperationState.SUCCEEDED),
        _operation("c-2", state=OperationState.FAILED),
        _operation("c-3", state=OperationState.CANCELLED),
    ]

    assert collect_quiescence_report(
        events=[], new_tool_calls=False, operations=operations,
    ).quiescent


def test_combined_blockers_keep_spec_order_and_reason_is_sorted() -> None:
    """组合：报告按 `02 §5.4` 的编号顺序逐条列出，理由串按 kind 排序。"""
    report = collect_quiescence_report(
        events=[
            *_events_with_model_tool_call("call-dangling"),
            _event(TOOL_APPROVAL_REQUESTED, {"approval_id": "a-1"}),
            _event(AGENT_DELEGATION_STARTED, {"child_session_id": "child-1"}),
        ],
        operations=[
            _operation("c-pending", state=OperationState.PENDING),
            _operation("c-reconcile", state=OperationState.NEED_RECONCILE),
        ],
        new_tool_calls=True,
    )

    assert report.kinds == (
        QUIESCENCE_DANGLING_TOOL,
        QUIESCENCE_UNRESOLVED_APPROVAL,
        QUIESCENCE_ACTIVE_CHILD,
        QUIESCENCE_UNSETTLED_OPERATION,
        QUIESCENCE_PENDING_RECONCILE,
        QUIESCENCE_NEW_TOOL_CALLS,
    )
    assert report.refusal_reason() == (
        f"{QUIESCENCE_BLOCKED_PREFIX}:active_child,dangling_tool,new_tool_calls,"
        "pending_reconcile,unresolved_approval,unsettled_operation"
    )


def test_projection_caps_refs_but_keeps_the_full_count() -> None:
    blocker = QuiescenceBlocker("dangling_tool", tuple(f"call-{n}" for n in range(30)))

    projection = QuiescenceReport(blockers=(blocker,)).as_projection()

    assert projection["blockers"][0]["count"] == 30
    assert len(projection["blockers"][0]["refs"]) == 20
    assert "reason" not in projection, "理由只在 refusal_reason() 一处，避免两份可漂移的副本"


def test_ledger_objections_cover_needs_reconcile_for_every_state() -> None:
    """ADR-0047 D1：完成闸门的两条账本谓词**覆盖**服务层恢复闸门读的 `needs_reconcile`。

    `needs_reconcile` = {RUNNING, UNKNOWN, NEED_RECONCILE} ∪ 未证标记 ⇒ 它非空时完成闸门
    必定报 blocker（判据同源，两个闸门不会漂移）。反向不成立且是**故意**的：`PENDING`
    不欠对账（`07 §6`：可证明没开始），但"没开始"≠"已结清"，完成闸门照样挡住它。
    """
    for state in OperationState:
        operation = _operation("c-1", state=state)
        kinds = set(
            collect_quiescence_report(
                events=[], new_tool_calls=False, operations=[operation],
            ).kinds
        )

        if needs_reconcile(operation):
            assert kinds, f"{state} 欠对账却没有被完成闸门挡住"
        if state is OperationState.PENDING:
            assert kinds == {QUIESCENCE_UNSETTLED_OPERATION}, (
                "PENDING 只该命中谓词 4（未定 reconcile 状态的在途行）"
            )
        if state in (OperationState.SUCCEEDED, OperationState.FAILED,
                     OperationState.CANCELLED):
            assert kinds == set(), f"{state} 是既成事实，不该再挡完成"


@pytest.mark.asyncio
async def test_default_policy_rejects_a_non_quiescent_report() -> None:
    """策略自己也拒绝非静止报告：绕过 Runtime 直接调用不该顺带绕过六条谓词。"""
    report = QuiescenceReport(blockers=(QuiescenceBlocker(QUIESCENCE_NEW_TOOL_CALLS),))

    decision = await DefaultCompletionPolicy().decide(
        report=report, final_text="done", run_id="run-1",
    )

    assert decision.accepted is False
    assert decision.reason == report.refusal_reason()


# ---------------------------------------------------------------------------
# 全链层：真实完成边界
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quiescent_final_response_completes_and_the_tool_result_is_durable_first(
    tmp_path: Path,
) -> None:
    """票面 AC：全部静止 ⇒ 默认策略接受 ⇒ **恰好一条** `run/completed`，
    且它出现在那次调用的 `tool/result` **之后**（完成的前置 durable 事实）。"""
    ledger = SqliteOperationLedger(tmp_path / "operations.db")
    await ledger.initialize()
    session = make_session(tmp_path)

    result = await _runtime(ledger=ledger).run(session, "probe it")

    assert result.status == STATUS_COMPLETED
    assert result.reason is None
    assert _terminal_events(session) == [RUN_COMPLETED]
    types = [e.type for e in session.events]
    assert types.index(TOOL_RESULT) < types.index(RUN_COMPLETED)
    operations = await ledger.list_for_session(session.session_id)
    assert [o.state for o in operations] == [OperationState.SUCCEEDED]


@pytest.mark.parametrize(
    ("blocker", "expected_kind"),
    [
        ("dangling", QUIESCENCE_DANGLING_TOOL),
        ("approval", QUIESCENCE_UNRESOLVED_APPROVAL),
        ("child", QUIESCENCE_ACTIVE_CHILD),
        ("unsettled", QUIESCENCE_UNSETTLED_OPERATION),
        ("reconcile", QUIESCENCE_PENDING_RECONCILE),
    ],
)
@pytest.mark.asyncio
async def test_each_blocker_independently_prevents_completion(
    tmp_path: Path, blocker: str, expected_kind: str,
) -> None:
    """票面 AC 前五条：每类未解工作**各自独立**挡住 `run/completed`。

    同时钉住三件事：本次执行不落终态（`run/failed` 也没有）、不落 `run/paused`
    （`03 §5`：那样只能由预算 / deadline / stuck 三类原因产生）、以及 **policy 不被调用**
    （`02 §5.4`：调用它之前必须先证六条）。
    """
    ledger = SqliteOperationLedger(tmp_path / "operations.db")
    await ledger.initialize()
    session = make_session(tmp_path)
    policy = _RecordingPolicy()

    if blocker == "dangling":
        for event in _events_with_model_tool_call("call-dangling"):
            session.append(event.type, event.data)
    elif blocker == "approval":
        session.append(TOOL_APPROVAL_REQUESTED, {"approval_id": "a-1"})
    elif blocker == "child":
        session.append(AGENT_DELEGATION_STARTED, {"child_session_id": "child-1"})
    elif blocker == "unsettled":
        await _seed_operation(ledger, session, tool_call_id="call-stuck")
    elif blocker == "reconcile":
        await _seed_operation(
            ledger, session, tool_call_id="call-unknown",
            state=OperationState.NEED_RECONCILE,
        )

    result = await _runtime(policy=policy, ledger=ledger).run(session, "probe it")

    assert result.status == STATUS_QUIESCENCE_BLOCKED
    assert result.reason is not None
    assert result.reason.startswith(QUIESCENCE_BLOCKED_PREFIX)
    assert expected_kind in result.reason
    assert policy.calls == [], "未静止时不得调用 CompletionPolicy"
    types = [e.type for e in session.events]
    assert RUN_COMPLETED not in types
    assert RUN_FAILED not in types
    assert RUN_PAUSED not in types
    # 拒绝是**无副作用**的：不推 reconcile、不改账本、不落任何新事件（除 loop 自己的）。
    assert OPERATION_RECONCILE_REQUIRED not in types


@pytest.mark.asyncio
async def test_cancelled_approval_does_not_wedge_the_session(tmp_path: Path) -> None:
    """被取消的审批不许留下无主事实（两轴审查 P1 的回归钉）。

    闸门是**会话级**的（ADR-0047 D1），而谓词 2 的判据是"有 `tool/approval-requested`
    无 `permission/resolved`"：一条永远配不上的请求会让这段会话此后每次 run 都
    `quiescence_blocked`，且没有任何写入方能结清它。交互式 callback 在取消 / 异常退出时
    必须按 fail-closed 补上决议（reason 里的"未批准"是事实，不是猜测）。
    """
    ledger = SqliteOperationLedger(tmp_path / "operations.db")
    await ledger.initialize()
    session = make_session(tmp_path)
    queue = PendingApprovalQueue()
    holder = _InteractiveCallbackHolder(queue=queue, timeout_seconds=0)
    holder.bind_session(session)
    registry = ToolRegistry()
    registry.register(_DangerTool())
    runtime = AgentRuntime(
        ScriptedModel([
            AIMessage(
                content="",
                tool_calls=[{"id": "call-approval", "name": "danger", "args": {}}],
            ),
            AIMessage(content="done"),
        ]),
        registry,
        ToolExecutor(registry, operation_ledger=ledger, approval_callback=holder),
    )

    task = asyncio.create_task(runtime.run(session, "danger it"))
    while not queue.pending_ids():
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert unresolved_approval_ids(session.events) == []
    report = collect_quiescence_report(
        events=session.events,
        operations=await ledger.list_for_session(session.session_id),
        new_tool_calls=False,
    )
    assert QUIESCENCE_UNRESOLVED_APPROVAL not in report.kinds


@pytest.mark.asyncio
async def test_blocked_closeout_leaves_the_unresolved_owner_untouched(
    tmp_path: Path,
) -> None:
    """D3/D4：被挡住时，未解 owner 的 durable 状态逐字节不变（悬空调用仍在）。"""
    ledger = SqliteOperationLedger(tmp_path / "operations.db")
    await ledger.initialize()
    session = make_session(tmp_path)
    for event in _events_with_model_tool_call("call-dangling"):
        session.append(event.type, event.data)
    await _seed_operation(ledger, session, tool_call_id="call-stuck")
    before = _durable_snapshot(session)

    result = await _runtime(ledger=ledger).run(session, "probe it")

    assert result.status == STATUS_QUIESCENCE_BLOCKED
    # 那次执行自己的事件是前端追加的，既有前缀必须一字不动。
    assert _durable_snapshot(session)[: len(before)] == before
    operations = await ledger.list_for_session(session.session_id)
    assert {o.tool_call_id: o.state for o in operations} == {
        # 预置的欠账行原样不动（拒绝不替它做分类）
        "call-stuck": OperationState.PENDING,
        # 本次执行自己的那次调用照常结清（拒绝的是收口，不是工作）
        "call-probe": OperationState.SUCCEEDED,
    }
    assert OPERATION_RECONCILE_REQUIRED not in [e.type for e in session.events]


@pytest.mark.asyncio
async def test_rejecting_policy_prevents_completion_without_touching_durable_state(
    tmp_path: Path,
) -> None:
    """票面 AC：自定义策略拒绝 ⇒ 不落 `run/completed`、理由稳定、**不改 quiescence 状态**。"""
    ledger = SqliteOperationLedger(tmp_path / "operations.db")
    await ledger.initialize()
    session = make_session(tmp_path)
    policy = _RecordingPolicy(accepted=False, reason="domain:evidence_missing")

    result = await _runtime(policy=policy, ledger=ledger).run(session, "probe it")

    assert result.status == STATUS_QUIESCENCE_BLOCKED
    assert result.reason == "domain:evidence_missing"
    assert [report.quiescent for report in policy.calls] == [True], (
        "策略只该在静止后被调用一次"
    )
    assert _terminal_events(session) == []
    # "不改 quiescence 状态"：账本行仍是那次调用的终态，事件流里没有 reconcile 痕迹。
    operations = await ledger.list_for_session(session.session_id)
    assert [o.state for o in operations] == [OperationState.SUCCEEDED]
    types = [e.type for e in session.events]
    assert TOOL_CALL in types and TOOL_RESULT in types
    assert OPERATION_RECONCILE_REQUIRED not in types


@pytest.mark.asyncio
async def test_policy_without_a_reason_still_reports_a_stable_one(tmp_path: Path) -> None:
    """"拒绝了但没给理由"不该让调用方拿到 None——兜底串含策略类名。"""
    ledger = SqliteOperationLedger(tmp_path / "operations.db")
    await ledger.initialize()
    session = make_session(tmp_path)
    policy = _RecordingPolicy(accepted=False, reason=None)

    result = await _runtime(policy=policy, ledger=ledger).run(session, "probe it")

    assert result.reason == f"{POLICY_REJECTED_PREFIX}:_RecordingPolicy"


@pytest.mark.asyncio
async def test_settled_work_reenters_the_same_gate_with_a_single_terminal_event(
    tmp_path: Path,
) -> None:
    """票面 AC：结清后重入同一闸门；**不重复、不矛盾**的终态事件（全程恰一条）。

    首次执行被悬空调用挡住（不落终态）；补上那条恢复分类后，同一会话的下一次执行
    在同一个闸门上通过——终态事件总数仍是 1，且它属于第二次执行的那个 run。
    """
    ledger = SqliteOperationLedger(tmp_path / "operations.db")
    await ledger.initialize()
    session = make_session(tmp_path)
    for event in _events_with_model_tool_call("call-dangling"):
        session.append(event.type, event.data)

    blocked = await _runtime(ledger=ledger).run(session, "probe it")
    first_run_id = next(e.run_id for e in session.events if e.type == RUN_STARTED)

    assert blocked.status == STATUS_QUIESCENCE_BLOCKED
    assert _terminal_events(session) == []

    session.append(TOOL_RESULT, {"tool_call_id": "call-dangling", "content": "恢复分类"})
    # 下一次执行在新实例上跑（生产里同样是一次新的装配）——闸门读的是会话的 durable
    # 状态，与 Runtime 实例的寿命无关。工具调用 id 也要新（见 `_runtime` 的说明）。
    completed = await _runtime(ledger=ledger, tool_call_id="call-probe-2").run(
        session, "probe it",
    )

    assert completed.status == STATUS_COMPLETED
    assert _terminal_events(session) == [RUN_COMPLETED]
    completed_run_id = next(e.run_id for e in session.events if e.type == RUN_COMPLETED)
    assert completed_run_id != first_run_id


def test_blocked_closeout_logs_the_blockers_without_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """诊断面：`agent_decision` 带 source / reason / 逐条 blocker（只带 id）。

    这一条读**日志记录**（诊断渠道，不变量 #4：Event ≠ Diagnostic Log）——完成闸门
    **这个臂**不落任何 SessionEvent（D3；本轮的 `model/completed` 在进闸门前已按稳定
    边界落盘），所以"为什么没完成"在 durable 面没有专属帧，诊断日志是
    它在进程外的唯一观察点。读 `caplog` 而不是 JSONL 文件：JSONL 的键名走
    `_DISPLAY_KEYS` 展示映射，本用例断言的是**字段语义**而不是展示层拼写。
    """
    import asyncio
    import logging

    caplog.set_level(logging.INFO)
    ledger = SqliteOperationLedger(tmp_path / "operations.db")
    session = make_session(tmp_path)
    session.append(TOOL_APPROVAL_REQUESTED, {"approval_id": "a-1"})

    async def _run() -> None:
        await ledger.initialize()
        await _runtime(ledger=ledger).run(session, "probe it")

    asyncio.run(_run())

    blocked = [r for r in caplog.records if getattr(r, "decision", None) == "blocked"]
    assert len(blocked) == 1
    record = blocked[0]
    assert record.source == "quiescence"
    assert record.reason.startswith(QUIESCENCE_BLOCKED_PREFIX)
    assert record.quiescence["blockers"] == [
        {"kind": QUIESCENCE_UNRESOLVED_APPROVAL, "count": 1, "refs": ["a-1"]},
    ]
