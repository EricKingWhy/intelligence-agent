"""#566 悬空 tool_call 的诚实分类：审批门前的「未执行」vs 执行中的「结果未知」。

票面回归要求：同样 kill 点，悬空 tool_call 的合成结果文案为「未执行（审批未通过）」。
audit 增强：证明结构 = durable Ledger 行 + 审批→执行顺序（`04 §9.1`：审批闸门 →
接纳点建账 → execute），**不是**"缺 tool/result 就推定未执行"。因此三窗口各有
durable 判据，分类只绑系统实际掌握的事实（封闭词表）：

- 窗口 A（审批中 kill）：`tool/call` + `tool/approval-requested` 无配对，Ledger
  无账行 ⇒ 接纳点未到 ⇒ 未执行（审批未通过——#337 在同一 recover 内结清 deny）。
- 窗口 B（批准落盘后 kill）：`permission/resolved(approve)` 已落盘，Ledger 无账行
  ⇒ kill 落在 resolved → 建账窗口 ⇒ 未执行（已批准，尚未开始执行）。
- 窗口 C（执行后丢结果）：Ledger 行 PENDING/RUNNING/UNKNOWN 在场 ⇒ 执行可能已
  开始 ⇒ 保持"结果未知"语义（PENDING 走 skip 策略；RUNNING/UNKNOWN 无 callback
  时 ReconcileRequired——UNKNOWN 永不盲目重跑，不变量 #14）。

两个真崩溃子进程与 `_approval_kill_child.py` 同型：`os._exit(9)` 不走 finally，
磁盘只留"进程死掉那一刻"的样子。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.model.scripted import ScriptedModel
from agent_harness.recovery import (
    RECOVERY_STALE_APPROVAL_REASON,
    RecoveryCoordinator,
)
from agent_harness.recovery.coordinator import ReconcileRequired
from agent_harness.session import JsonlSessionStore, Session, SessionEvent
from agent_harness.session.approval import (
    ApprovalOutcome,
    approval_outcome_for_call,
)
from agent_harness.session.derive import (
    DANGLING_NOT_EXECUTED,
    DANGLING_NOT_EXECUTED_APPROVED,
    DANGLING_NOT_EXECUTED_DENIED,
    DANGLING_TOOL_CONTENT,
)
from agent_harness.session.event import (
    MODEL_COMPLETED,
    PERMISSION_RESOLVED,
    RUN_COMPLETED,
    TOOL_APPROVAL_REQUESTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
)
from agent_harness.storage import Operation, OperationState, SqliteOperationLedger
from agent_harness.tooling import (
    Tool,
    ToolExecutor,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)
from agent_harness.tooling.approval import PermissionDecision

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD_A = Path(__file__).with_name("_approval_kill_child.py")
_CHILD_B = Path(__file__).with_name("_approval_admitted_kill_child.py")

#: 与子进程的 `CRASH_EXIT_CODE` 一致（子进程 import 即执行，常量只能抄一份）。
_CRASH_EXIT_CODE = 9

SESSION_ID = "sess-dangling-classification"
CALL_ID = "call-window-a"
APPROVAL_ID = "approval-window-a"

#: 窗口 A 真崩溃子进程自带的 tool_call_id（常量只能抄一份，不能 import 子进程模块）。
CALL_ID_A = "call-approval-restart"


class _NoArgs(BaseModel):
    pass


class _CountingDangerTool(Tool):
    """DANGER 工具：被调用即记数（证明"没有重跑"）。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "danger"

    @property
    def description(self) -> str:
        return "不该被执行：恢复只读事件流与账，不重跑副作用。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER

    async def execute(self, args: BaseModel) -> ToolResult:
        self.calls += 1
        return ToolResult.success("rerun")


def _append(session: Session, event_type: str, data: dict, **kwargs) -> SessionEvent:
    return session.append(event_type, data, **kwargs)


def _approval_requested(session: Session, call_id: str, approval_id: str) -> None:
    _append(
        session,
        TOOL_APPROVAL_REQUESTED,
        {
            "approval_id": approval_id,
            "tool_name": "danger",
            "tool_call_id": call_id,
            "action_type": "danger",
            "permission": "danger",
            "policy": "workspace_write",
            "reason": "高危操作",
        },
    )


def _resolved(
    session: Session, approval_id: str, decision: str, reason: str = "人工裁决"
) -> None:
    _append(
        session,
        PERMISSION_RESOLVED,
        {"approval_id": approval_id, "decision": decision, "reason": reason},
    )


def _make_session_with_dangling_call(
    store: JsonlSessionStore, session_id: str, call_id: str
) -> Session:
    """tool/call 已落盘、结果写回前崩溃的现场（无审批事件，由各用例按需补）。"""
    session = Session.start(store, session_id=session_id)
    _append(session, USER_MESSAGE, {"content": "run it"})
    _append(
        session,
        MODEL_COMPLETED,
        {
            "content": "",
            "tool_calls": [{"id": call_id, "name": "danger", "args": {}}],
        },
        run_id="run-1",
        step_id=1,
    )
    _append(
        session,
        TOOL_CALL,
        {"tool_call_id": call_id, "tool_name": "danger", "args": {}},
        run_id="run-1",
        step_id=1,
    )
    return session


async def _open(
    tmp_path: Path,
) -> tuple[JsonlSessionStore, SqliteOperationLedger]:
    store = JsonlSessionStore(tmp_path / "sessions")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    return store, ledger


def _coordinator(
    store: JsonlSessionStore,
    ledger: SqliteOperationLedger,
    tmp_path: Path,
) -> RecoveryCoordinator:
    return RecoveryCoordinator(
        session_store=store,
        workspace_registry=None,
        operation_ledger=ledger,
        database_path=tmp_path / "state.db",
        tool_registry=ToolRegistry(),
        lock_timeout_seconds=1.0,
    )


def _result_contents(session: Session, call_id: str) -> list[str]:
    return [
        str(e.data.get("content", ""))
        for e in session.events
        if e.type == TOOL_RESULT and e.data.get("tool_call_id") == call_id
    ]


# ── approval_outcome_for_call：审批侧真相的封闭枚举 ─────────────────────


def _events_with(
    *pairs: tuple[str, dict],
) -> list[SessionEvent]:
    """最小事件序列构造（纯读侧用，不经 store）。"""
    events = []
    for i, (event_type, data) in enumerate(pairs):
        events.append(
            SessionEvent(
                seq=i,
                type=event_type,
                data=data,
                time="2026-10-03T00:00:00Z",
                event_id=f"evt-{i}",
                session_id="s",
            )
        )
    return events


def test_approval_outcome_closed_vocabulary() -> None:
    events = _events_with(
        (TOOL_APPROVAL_REQUESTED, {"approval_id": "a1", "tool_call_id": "c1"}),
    )
    assert approval_outcome_for_call(events, "c1") is ApprovalOutcome.UNRESOLVED

    resolved = _events_with(
        (TOOL_APPROVAL_REQUESTED, {"approval_id": "a1", "tool_call_id": "c1"}),
        (PERMISSION_RESOLVED, {"approval_id": "a1", "decision": "approve_once"}),
    )
    assert approval_outcome_for_call(resolved, "c1") is ApprovalOutcome.APPROVED

    denied = _events_with(
        (TOOL_APPROVAL_REQUESTED, {"approval_id": "a1", "tool_call_id": "c1"}),
        (PERMISSION_RESOLVED, {"approval_id": "a1", "decision": "deny"}),
    )
    assert approval_outcome_for_call(denied, "c1") is ApprovalOutcome.DENIED

    assert (
        approval_outcome_for_call(_events_with(), "c1")
        is ApprovalOutcome.NOT_REQUESTED
    )
    # 别的 call 的审批不算数
    other = _events_with(
        (TOOL_APPROVAL_REQUESTED, {"approval_id": "a1", "tool_call_id": "c2"}),
    )
    assert approval_outcome_for_call(other, "c1") is ApprovalOutcome.NOT_REQUESTED
    # 未知决策值：不能证明放行 → fail-closed 按未结清
    weird = _events_with(
        (TOOL_APPROVAL_REQUESTED, {"approval_id": "a1", "tool_call_id": "c1"}),
        (PERMISSION_RESOLVED, {"approval_id": "a1", "decision": "???"}),
    )
    assert approval_outcome_for_call(weird, "c1") is ApprovalOutcome.UNRESOLVED


# ── 窗口 A：审批中 kill（recover 级，进程内构造现场） ────────────────────


@pytest.mark.asyncio
async def test_window_a_kill_during_approval_reports_not_executed_denied(
    tmp_path: Path,
) -> None:
    store, ledger = await _open(tmp_path)
    session = _make_session_with_dangling_call(store, SESSION_ID, CALL_ID)
    _approval_requested(session, CALL_ID, APPROVAL_ID)
    assert await ledger.get(SESSION_ID, CALL_ID) is None, "审批在接纳点之前：无账"

    counter = _CountingDangerTool()
    registry = ToolRegistry()
    registry.register(counter)
    coordinator = RecoveryCoordinator(
        session_store=store,
        workspace_registry=None,
        operation_ledger=ledger,
        database_path=tmp_path / "state.db",
        tool_registry=registry,
        lock_timeout_seconds=1.0,
    )
    recovered = await coordinator.recover(SESSION_ID)

    contents = _result_contents(recovered, CALL_ID)
    assert contents == [DANGLING_NOT_EXECUTED_DENIED], (
        "审批门前的悬空调用不得合成「结果未知」"
    )
    assert counter.calls == 0, "恢复零重跑"
    # #337 结清在同一 recover 内落盘（文案与 durable 事实一致）
    settlements = [e for e in recovered.events if e.type == PERMISSION_RESOLVED]
    assert len(settlements) == 1
    assert settlements[0].data["decision"] == "deny"
    assert settlements[0].data["reason"] == RECOVERY_STALE_APPROVAL_REASON


# ── 窗口 B：批准落盘后、建账前 kill（recover 级） ────────────────────────


@pytest.mark.asyncio
async def test_window_b_kill_after_approval_persisted_reports_approved_not_started(
    tmp_path: Path,
) -> None:
    store, ledger = await _open(tmp_path)
    session = _make_session_with_dangling_call(store, SESSION_ID, CALL_ID)
    _approval_requested(session, CALL_ID, APPROVAL_ID)
    _resolved(session, APPROVAL_ID, PermissionDecision.APPROVE_ONCE.value)
    assert await ledger.get(SESSION_ID, CALL_ID) is None, "接纳点未到：无账"

    recovered = await _coordinator(store, ledger, tmp_path).recover(SESSION_ID)

    assert _result_contents(recovered, CALL_ID) == [
        DANGLING_NOT_EXECUTED_APPROVED
    ], "批准已落盘但接纳点未到：未执行，且不该说「审批未通过」"


# ── 窗口 C：执行可能已开始 → 「结果未知」语义保持 ────────────────────────


@pytest.mark.asyncio
async def test_window_c_unknown_row_requires_reconcile(tmp_path: Path) -> None:
    """UNKNOWN 行在场：保持 NEED_RECONCILE（无 callback 安全拒绝，不伪造结果）。"""
    store, ledger = await _open(tmp_path)
    _make_session_with_dangling_call(store, SESSION_ID, CALL_ID)
    # 状态机两步链（#30）：PENDING → RUNNING → UNKNOWN，不许跳步直达。
    await ledger.create(
        Operation(
            tool_call_id=CALL_ID,
            session_id=SESSION_ID,
            run_id="run-1",
            agent_id=None,
            tool_name="danger",
            args_identity="{}",
            state=OperationState.PENDING,
            started_at="2026-10-03T00:00:00Z",
        )
    )
    await ledger.update_state(SESSION_ID, CALL_ID, OperationState.RUNNING)
    await ledger.update_state(SESSION_ID, CALL_ID, OperationState.UNKNOWN)
    events_before = store.read_events(SESSION_ID)

    with pytest.raises(ReconcileRequired):
        await _coordinator(store, ledger, tmp_path).recover(SESSION_ID)

    assert store.read_events(SESSION_ID) == events_before, "拒绝即零写入"


@pytest.mark.asyncio
async def test_window_c_pending_row_keeps_skip_policy(tmp_path: Path) -> None:
    """PENDING 行（建账后、执行前 kill）：skip 策略的「未启动即跳过」语义不变。"""
    store, ledger = await _open(tmp_path)
    _make_session_with_dangling_call(store, SESSION_ID, CALL_ID)
    await ledger.create(
        Operation(
            tool_call_id=CALL_ID,
            session_id=SESSION_ID,
            run_id="run-1",
            agent_id=None,
            tool_name="danger",
            args_identity="{}",
            state=OperationState.PENDING,
            started_at="2026-10-03T00:00:00Z",
        )
    )

    recovered = await _coordinator(store, ledger, tmp_path).recover(SESSION_ID)

    contents = _result_contents(recovered, CALL_ID)
    assert len(contents) == 1
    assert "未启动" in contents[0] or "skip" in contents[0].lower()
    assert DANGLING_TOOL_CONTENT not in contents[0]


@pytest.mark.asyncio
async def test_window_c_no_approval_no_ledger_reports_not_started(
    tmp_path: Path,
) -> None:
    """无需审批的调用在接纳点前 kill（无审批事件 + 无账行）⇒ 未执行（未开始）。"""
    store, ledger = await _open(tmp_path)
    _make_session_with_dangling_call(store, SESSION_ID, CALL_ID)

    recovered = await _coordinator(store, ledger, tmp_path).recover(SESSION_ID)

    assert _result_contents(recovered, CALL_ID) == [DANGLING_NOT_EXECUTED]


# ── Session.resume：只有「审批无决议」能在无账读数下证明未执行 ───────────


@pytest.mark.asyncio
async def test_resume_marks_unresolved_approval_dangling_as_not_executed(
    tmp_path: Path,
) -> None:
    store, _ = await _open(tmp_path)
    session = _make_session_with_dangling_call(store, SESSION_ID, CALL_ID)
    _approval_requested(session, CALL_ID, APPROVAL_ID)

    resumed = Session.resume(store, SESSION_ID)

    assert _result_contents(resumed, CALL_ID) == [DANGLING_NOT_EXECUTED_DENIED]


@pytest.mark.asyncio
async def test_resume_keeps_conservative_text_without_approval_evidence(
    tmp_path: Path,
) -> None:
    """无账读数下「无审批事件」不能排除执行已开始 ⇒ 保留「结果未知」占位。"""
    store, _ = await _open(tmp_path)
    _make_session_with_dangling_call(store, SESSION_ID, CALL_ID)

    resumed = Session.resume(store, SESSION_ID)

    assert _result_contents(resumed, CALL_ID) == [DANGLING_TOOL_CONTENT]


# ── 真崩溃集成：窗口 A（审批等待中被杀，复用 #337 子进程） ────────────────


def _crash(child: Path, tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(child), json.dumps({
            "root": str(tmp_path), "session_id": SESSION_ID,
        })],
        timeout=120,
        env=env,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == _CRASH_EXIT_CODE, (
        f"子进程应以 os._exit({_CRASH_EXIT_CODE}) 崩溃，实际 {completed.returncode}："
        f"{completed.stderr[-1500:]}"
    )
    assert "READY" in completed.stdout, completed.stdout
    assert completed.stderr == "", "子进程前提自查报错——现场不成立"


@pytest.mark.asyncio
async def test_crash_window_a_kill_during_approval_wait(tmp_path: Path) -> None:
    _crash(_CHILD_A, tmp_path)
    store, ledger = await _open(tmp_path)
    events = store.read_events(SESSION_ID)
    assert any(e.type == TOOL_APPROVAL_REQUESTED for e in events)
    assert await ledger.get(SESSION_ID, CALL_ID_A) is None, "审批在接纳点之前：无账"
    assert not (tmp_path / "mutation.log").exists(), "副作用未发生"

    counter = _CountingDangerTool()
    registry = ToolRegistry()
    registry.register(counter)
    coordinator = RecoveryCoordinator(
        session_store=store,
        workspace_registry=None,
        operation_ledger=ledger,
        database_path=tmp_path / "state.db",
        tool_registry=registry,
        lock_timeout_seconds=1.0,
    )
    recovered = await coordinator.recover(SESSION_ID)

    assert _result_contents(recovered, CALL_ID_A) == [DANGLING_NOT_EXECUTED_DENIED]
    assert counter.calls == 0
    # 恢复后的下一个 run 照常完成（#337 链路不受文案变化影响）
    runtime = AgentRuntime(
        model=ScriptedModel(responses=[AIMessage(content="恢复后直接完成")]),
        registry=registry,
        executor=ToolExecutor(registry, operation_ledger=ledger),
        max_agent_turns=10,
    )
    await runtime.run(recovered, "继续")
    final_events = store.read_events(SESSION_ID)
    assert any(e.type == RUN_COMPLETED for e in final_events)


# ── 真崩溃集成：窗口 B（批准落盘后、建账前被杀） ─────────────────────────


@pytest.mark.asyncio
async def test_crash_window_b_kill_after_approval_persisted(tmp_path: Path) -> None:
    _crash(_CHILD_B, tmp_path)
    store, ledger = await _open(tmp_path)
    events = store.read_events(SESSION_ID)
    assert any(
        e.type == PERMISSION_RESOLVED
        and e.data.get("decision") == PermissionDecision.APPROVE_ONCE.value
        for e in events
    ), "现场前提：resolved(approve) 已落盘"
    assert await ledger.get(SESSION_ID, CALL_ID) is None, "接纳点未到：无账"
    assert not (tmp_path / "mutation.log").exists(), "副作用未发生"

    recovered = await _coordinator(store, ledger, tmp_path).recover(SESSION_ID)

    assert _result_contents(recovered, CALL_ID) == [
        DANGLING_NOT_EXECUTED_APPROVED
    ]
