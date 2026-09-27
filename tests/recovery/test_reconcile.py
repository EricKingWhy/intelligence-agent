"""ReconcileCallback 人工裁决 contract tests（#30）。

覆盖 #30 全部 AC：
- RUNNING 崩溃状态进入 UNKNOWN，再进入 NEED_RECONCILE（两步状态机被 Ledger 强制）；
- UNKNOWN 与 NEED_RECONCILE 始终调用 ReconcileCallback；没有 callback 时安全拒绝；
- CONFIRM_SUCCESS / CONFIRM_FAILURE / RETRY / ABANDON 四种显式裁决；
- 进入 NEED_RECONCILE 时追加 operation/reconcile-required SessionEvent；
- UNKNOWN bash 永不自动重跑（协调器没有执行器，也无 callback 时拒绝恢复）；
- checkpoint/saved 仍不进入事件流。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_harness.recovery import (
    ReconcileCallback,
    ReconcileVerdict,
    RecoveryCoordinator,
    RecoveryError,
)
from agent_harness.sandbox.local import LocalSubprocessSandbox
from agent_harness.session import (
    MODEL_COMPLETED,
    SESSION_RESUMED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
    detect_dangling,
)
from agent_harness.storage import (
    Operation,
    OperationState,
    SqliteOperationLedger,
    unproven_meta,
)
from agent_harness.tooling import ErrorCode, ReconcileHint, ToolRegistry, ToolResult
from agent_harness.tools import WriteTool

# ── 事件类型常量（#30 新增词汇） ──

OPERATION_RECONCILE_REQUIRED = "operation/reconcile-required"

# ── 测试夹具 ──

_SEED_CHAIN: dict[OperationState, list[OperationState]] = {
    # PENDING 是"刚开单、还没被接纳"的起点本身：链为空（状态机不认 PENDING→PENDING）
    OperationState.PENDING: [],
    OperationState.RUNNING: [OperationState.RUNNING],
    OperationState.UNKNOWN: [OperationState.RUNNING, OperationState.UNKNOWN],
    OperationState.NEED_RECONCILE: [
        OperationState.RUNNING,
        OperationState.UNKNOWN,
        OperationState.NEED_RECONCILE,
    ],
    OperationState.SUCCEEDED: [OperationState.RUNNING, OperationState.SUCCEEDED],
    OperationState.FAILED: [OperationState.RUNNING, OperationState.FAILED],
    OperationState.CANCELLED: [OperationState.RUNNING, OperationState.CANCELLED],
}


def _make_crashed_session(
    store: JsonlSessionStore,
    call_id: str = "call-1",
    tool_name: str = "bash",
) -> Session:
    """tool 执行后、结果写回前崩溃的 session（tool/call 已在，无 tool/result）。"""
    session = Session.start(store)
    session.append(USER_MESSAGE, {"content": "run the migration"})
    session.append(
        MODEL_COMPLETED,
        {
            "content": "",
            "tool_calls": [{"id": call_id, "name": tool_name, "args": {}}],
        },
        run_id="run-1",
        step_id=1,
    )
    session.append(
        TOOL_CALL,
        {"tool_call_id": call_id, "tool_name": tool_name, "args": {}},
        run_id="run-1",
        step_id=1,
    )
    return session


async def _seed_operation(
    ledger: SqliteOperationLedger,
    session_id: str,
    tool_call_id: str,
    state: OperationState,
    *,
    tool_name: str = "bash",
    result_json: str | None = None,
    reconcile_meta: str | None = None,
) -> None:
    await ledger.create(
        Operation(
            tool_call_id=tool_call_id,
            session_id=session_id,
            run_id="run-1",
            agent_id="default",
            tool_name=tool_name,
            args_identity='{"command": "migrate"}',
            state=OperationState.PENDING,
            started_at="2026-09-04T00:00:00+00:00",
        )
    )
    chain = _SEED_CHAIN[state]
    for step in chain:
        await ledger.update_state(
            session_id,
            tool_call_id,
            step,
            result_json=result_json if step is chain[-1] else None,
            reconcile_meta=reconcile_meta if step is chain[-1] else None,
        )


class _ScriptedCallback(ReconcileCallback):
    """记录调用并返回预设裁决的 fake callback。"""

    def __init__(self, verdict: ReconcileVerdict) -> None:
        self.verdict = verdict
        self.calls: list[tuple[Operation, ReconcileHint]] = []

    async def resolve(
        self, operation: Operation, hint: ReconcileHint
    ) -> ReconcileVerdict:
        self.calls.append((operation, hint))
        return self.verdict


class _BlockingCallback(ReconcileCallback):
    """阻塞到外部放行，用于证明恢复锁不跨越人工等待。"""

    def __init__(self, verdict: ReconcileVerdict) -> None:
        self.verdict = verdict
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve(
        self, operation: Operation, hint: ReconcileHint
    ) -> ReconcileVerdict:
        self.started.set()
        await self.release.wait()
        return self.verdict


class _FailingCallback(ReconcileCallback):
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def resolve(
        self, operation: Operation, hint: ReconcileHint
    ) -> ReconcileVerdict:
        raise self.error


def _make_coordinator(
    store: JsonlSessionStore,
    ledger: SqliteOperationLedger,
    database_path: Path,
    *,
    reconcile_callback: ReconcileCallback | None = None,
    tool_registry: ToolRegistry | None = None,
) -> RecoveryCoordinator:
    return RecoveryCoordinator(
        session_store=store,
        workspace_registry=None,
        operation_ledger=ledger,
        database_path=database_path,
        reconcile_callback=reconcile_callback,
        tool_registry=tool_registry,
        lock_timeout_seconds=1.0,
    )


def _result_events(session: Session) -> dict[str, str]:
    return {
        event.data["tool_call_id"]: event.data["content"]
        for event in session.events
        if event.type == TOOL_RESULT
    }


def _reconcile_required_events(session: Session) -> list:
    return [
        event
        for event in session.events
        if event.type == OPERATION_RECONCILE_REQUIRED
    ]


# ── 状态机：RUNNING → UNKNOWN → NEED_RECONCILE 两步强制 ──


@pytest.mark.asyncio
async def test_ledger_enforces_two_step_unknown_transition(tmp_path: Path) -> None:
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    session_id = "session-ledger"
    await _seed_operation(
        ledger, session_id, "call-a", OperationState.RUNNING
    )

    # 两步链合法：RUNNING → UNKNOWN → NEED_RECONCILE → 终态。
    await ledger.update_state(session_id, "call-a", OperationState.UNKNOWN)
    await ledger.update_state(session_id, "call-a", OperationState.NEED_RECONCILE)
    final = await ledger.update_state(session_id, "call-a", OperationState.SUCCEEDED)
    assert final.state is OperationState.SUCCEEDED

    # 跳步非法：RUNNING 不允许直达 NEED_RECONCILE。
    await _seed_operation(ledger, session_id, "call-b", OperationState.RUNNING)
    with pytest.raises(ValueError, match="RUNNING -> NEED_RECONCILE"):
        await ledger.update_state(session_id, "call-b", OperationState.NEED_RECONCILE)


# ── #254 锁外人工裁决 ──


@pytest.mark.asyncio
async def test_callback_wait_does_not_hold_recovery_lock(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session_a = _make_crashed_session(store, call_id="call-a")
    session_b = _make_crashed_session(store, call_id="call-b")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, session_a.session_id, "call-a", OperationState.RUNNING)
    await _seed_operation(ledger, session_b.session_id, "call-b", OperationState.SUCCEEDED)

    callback = _BlockingCallback(ReconcileVerdict.CONFIRM_SUCCESS)
    coordinator_a = _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback
    )
    coordinator_b = _make_coordinator(store, ledger, tmp_path / "state.db")

    pending_a = asyncio.create_task(coordinator_a.recover(session_a.session_id))
    await callback.started.wait()
    recovered_b = await asyncio.wait_for(
        coordinator_b.recover(session_b.session_id), timeout=1.0
    )
    assert "call-b" in _result_events(recovered_b)

    callback.release.set()
    recovered_a = await pending_a
    assert "call-a" in _result_events(recovered_a)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("timed out"), ValueError("broken")])
async def test_callback_failure_does_not_write_fake_completion(
    failure: BaseException, tmp_path: Path
) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    crashed = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, crashed.session_id, "call-1", OperationState.RUNNING)

    coordinator = _make_coordinator(
        store, ledger, tmp_path / "state.db",
        reconcile_callback=_FailingCallback(failure),
    )
    with pytest.raises(type(failure), match=str(failure)):
        await coordinator.recover(crashed.session_id)

    operation = await ledger.get(crashed.session_id, "call-1")
    assert operation is not None and operation.state is OperationState.NEED_RECONCILE
    events = store.read_events(crashed.session_id)
    assert not [event for event in events if event.type == TOOL_RESULT]


@pytest.mark.asyncio
async def test_cancelled_callback_does_not_write_fake_completion(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    crashed = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, crashed.session_id, "call-1", OperationState.RUNNING)

    coordinator = _make_coordinator(
        store, ledger, tmp_path / "state.db",
        reconcile_callback=_FailingCallback(asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await coordinator.recover(crashed.session_id)

    operation = await ledger.get(crashed.session_id, "call-1")
    assert operation is not None and operation.state is OperationState.NEED_RECONCILE
    assert not [
        event for event in store.read_events(crashed.session_id)
        if event.type == TOOL_RESULT
    ]


@pytest.mark.asyncio
async def test_concurrent_adjudication_has_one_reconciled_outcome(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    crashed = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, crashed.session_id, "call-1", OperationState.RUNNING)

    callback_a = _BlockingCallback(ReconcileVerdict.CONFIRM_SUCCESS)
    callback_b = _BlockingCallback(ReconcileVerdict.CONFIRM_FAILURE)
    coordinator_a = _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback_a
    )
    coordinator_b = _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback_b
    )

    first = asyncio.create_task(coordinator_a.recover(crashed.session_id))
    await callback_a.started.wait()
    second = asyncio.create_task(coordinator_b.recover(crashed.session_id))
    await callback_b.started.wait()

    callback_a.release.set()
    first_result = await first
    callback_b.release.set()
    with pytest.raises(RecoveryError, match="stale reconcile verdict"):
        await second

    outcomes = [
        event
        for event in store.read_events(crashed.session_id)
        if event.type == TOOL_RESULT and event.data["tool_call_id"] == "call-1"
    ]
    assert len(outcomes) == 1
    operation = await ledger.get(crashed.session_id, "call-1")
    assert operation is not None and operation.state is OperationState.SUCCEEDED
    assert first_result is not None


@pytest.mark.asyncio
async def test_stale_verdict_is_rejected_without_result_event(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    crashed = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, crashed.session_id, "call-1", OperationState.RUNNING)

    callback = _BlockingCallback(ReconcileVerdict.CONFIRM_SUCCESS)
    coordinator = _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback
    )
    pending = asyncio.create_task(coordinator.recover(crashed.session_id))
    await callback.started.wait()
    await ledger.update_state(
        crashed.session_id, "call-1", OperationState.CANCELLED,
    )
    callback.release.set()

    with pytest.raises(RecoveryError, match="stale reconcile verdict"):
        await pending
    operation = await ledger.get(crashed.session_id, "call-1")
    assert operation is not None and operation.state is OperationState.CANCELLED
    assert not [
        event for event in store.read_events(crashed.session_id)
        if event.type == TOOL_RESULT
    ]


# ── 四种裁决 ──


@pytest.mark.asyncio
async def test_running_crash_reaches_callback_and_confirms_success(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store, tool_name="bash")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    result_json = ToolResult.success("migration applied").model_dump_json()
    await _seed_operation(
        ledger,
        session.session_id,
        "call-1",
        OperationState.RUNNING,
        result_json=result_json,
    )
    callback = _ScriptedCallback(ReconcileVerdict.CONFIRM_SUCCESS)

    recovered = await _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback
    ).recover(session.session_id)

    # 裁决结果落 Ledger：SUCCEEDED + 原 result_json。
    op = await ledger.get(session.session_id, "call-1")
    assert op is not None and op.state is OperationState.SUCCEEDED
    # reconcile-required 事件可观察，携带 tool_call_id 与 NEED_RECONCILE。
    events = _reconcile_required_events(recovered)
    assert len(events) == 1
    assert events[0].data["tool_call_id"] == "call-1"
    assert events[0].data["state"] == "NEED_RECONCILE"
    # 恢复结果复用原 tool_call_id，内容来自 Ledger result_json。
    synthesized = ToolResult.model_validate_json(_result_events(recovered)["call-1"])
    assert synthesized.ok is True
    assert synthesized.message == "migration applied"


@pytest.mark.asyncio
async def test_callback_receives_operation_and_hint(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store, tool_name="write")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(
        ledger, session.session_id, "call-1", OperationState.RUNNING,
        tool_name="write",
    )
    registry = ToolRegistry()
    registry.register(WriteTool(LocalSubprocessSandbox(workspace_root=tmp_path)))
    callback = _ScriptedCallback(ReconcileVerdict.CONFIRM_SUCCESS)

    await _make_coordinator(
        store,
        ledger,
        tmp_path / "state.db",
        reconcile_callback=callback,
        tool_registry=registry,
    ).recover(session.session_id)

    operation, hint = callback.calls[0]
    assert operation.tool_call_id == "call-1"
    assert operation.tool_name == "write"
    assert hint.verifiable is True  # write 覆写了 verifiable hint
    assert hint.suggested_action


@pytest.mark.asyncio
async def test_confirm_success_without_ledger_result_synthesizes_confirmed(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, session.session_id, "call-1", OperationState.RUNNING)

    recovered = await _make_coordinator(
        store,
        ledger,
        tmp_path / "state.db",
        reconcile_callback=_ScriptedCallback(ReconcileVerdict.CONFIRM_SUCCESS),
    ).recover(session.session_id)

    synthesized = ToolResult.model_validate_json(_result_events(recovered)["call-1"])
    assert synthesized.ok is True
    assert "确认成功" in synthesized.message
    op = await ledger.get(session.session_id, "call-1")
    assert op is not None
    assert op.state is OperationState.SUCCEEDED
    assert op.result_json is not None  # 裁决结果回写 Ledger


@pytest.mark.asyncio
async def test_confirm_failure_marks_failed(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, session.session_id, "call-1", OperationState.RUNNING)

    recovered = await _make_coordinator(
        store,
        ledger,
        tmp_path / "state.db",
        reconcile_callback=_ScriptedCallback(ReconcileVerdict.CONFIRM_FAILURE),
    ).recover(session.session_id)

    synthesized = ToolResult.model_validate_json(_result_events(recovered)["call-1"])
    assert synthesized.ok is False
    assert "确认失败" in synthesized.message
    op = await ledger.get(session.session_id, "call-1")
    assert op is not None and op.state is OperationState.FAILED


@pytest.mark.asyncio
async def test_retry_verdict_produces_user_authorized_retryable_result(
    tmp_path: Path,
) -> None:
    """RETRY 只能来自用户裁决：合成 retryable=True 的失败结果，
    模型看到后重新发起【新的】tool_call（新 Operation），原调用终止。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, session.session_id, "call-1", OperationState.RUNNING)

    recovered = await _make_coordinator(
        store,
        ledger,
        tmp_path / "state.db",
        reconcile_callback=_ScriptedCallback(ReconcileVerdict.RETRY),
    ).recover(session.session_id)

    synthesized = ToolResult.model_validate_json(_result_events(recovered)["call-1"])
    assert synthesized.ok is False
    assert synthesized.retryable is True
    assert synthesized.error_code is ErrorCode.CANCELLED
    op = await ledger.get(session.session_id, "call-1")
    assert op is not None and op.state is OperationState.CANCELLED
    assert op.reconcile_meta is not None
    assert json.loads(op.reconcile_meta)["verdict"] == "RETRY"


@pytest.mark.asyncio
async def test_abandon_verdict_marks_cancelled_non_retryable(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, session.session_id, "call-1", OperationState.RUNNING)

    recovered = await _make_coordinator(
        store,
        ledger,
        tmp_path / "state.db",
        reconcile_callback=_ScriptedCallback(ReconcileVerdict.ABANDON),
    ).recover(session.session_id)

    synthesized = ToolResult.model_validate_json(_result_events(recovered)["call-1"])
    assert synthesized.ok is False
    assert synthesized.retryable is False
    assert synthesized.error_code is ErrorCode.CANCELLED
    op = await ledger.get(session.session_id, "call-1")
    assert op is not None and op.state is OperationState.CANCELLED
    assert json.loads(op.reconcile_meta)["verdict"] == "ABANDON"


# ── 既有 UNKNOWN / NEED_RECONCILE 状态 ──


@pytest.mark.asyncio
async def test_preexisting_unknown_operation_flows_through_callback(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, session.session_id, "call-1", OperationState.UNKNOWN)
    callback = _ScriptedCallback(ReconcileVerdict.CONFIRM_SUCCESS)

    recovered = await _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback
    ).recover(session.session_id)

    assert len(callback.calls) == 1
    assert len(_reconcile_required_events(recovered)) == 1
    op = await ledger.get(session.session_id, "call-1")
    assert op is not None and op.state is OperationState.SUCCEEDED


@pytest.mark.asyncio
async def test_preexisting_need_reconcile_also_asks_user(tmp_path: Path) -> None:
    """上次恢复在裁决前崩溃遗留 NEED_RECONCILE：本次恢复再次询问用户。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(
        ledger, session.session_id, "call-1", OperationState.NEED_RECONCILE
    )
    callback = _ScriptedCallback(ReconcileVerdict.ABANDON)

    recovered = await _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback
    ).recover(session.session_id)

    assert len(callback.calls) == 1
    assert len(_reconcile_required_events(recovered)) == 1
    assert "call-1" in _result_events(recovered)


# ── 安全默认：没有 callback 时拒绝 ──


@pytest.mark.asyncio
async def test_missing_callback_refuses_safely(tmp_path: Path) -> None:
    """没有 ReconcileCallback：安全拒绝——不合成、不改状态、不写事件。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, session.session_id, "call-1", OperationState.RUNNING)
    events_before = store.read_events(session.session_id)

    coordinator = _make_coordinator(store, ledger, tmp_path / "state.db")
    with pytest.raises(RecoveryError, match="ReconcileCallback"):
        await coordinator.recover(session.session_id)

    # Ledger 状态未被推进（安全拒绝 = 完全不写）。
    op = await ledger.get(session.session_id, "call-1")
    assert op is not None and op.state is OperationState.RUNNING
    # 事件流零写入。
    assert store.read_events(session.session_id) == events_before


@pytest.mark.asyncio
async def test_unknown_bash_never_reruns_automatically(tmp_path: Path) -> None:
    """UNKNOWN bash 永不自动重跑：无 callback → 拒绝；有 callback → 只有合成结果，
    协调器没有执行器，不可能执行任何命令。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store, tool_name="bash")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, session.session_id, "call-1", OperationState.RUNNING)

    # 无 callback：拒绝恢复，bash 不执行。
    coordinator = _make_coordinator(store, ledger, tmp_path / "state.db")
    with pytest.raises(RecoveryError):
        await coordinator.recover(session.session_id)

    # 有 callback（RETRY）：合成的结果只是"请重新发起"的裁决表达，
    # 不含任何真实命令输出（stdout/exit_code 字段不存在）。
    recovered = await _make_coordinator(
        store,
        ledger,
        tmp_path / "state.db",
        reconcile_callback=_ScriptedCallback(ReconcileVerdict.RETRY),
    ).recover(session.session_id)
    synthesized = ToolResult.model_validate_json(_result_events(recovered)["call-1"])
    assert synthesized.data is None  # 没有真实执行数据
    assert "重新发起" in synthesized.message


@pytest.mark.asyncio
async def test_no_reconcile_required_recovery_unaffected_by_missing_callback(
    tmp_path: Path,
) -> None:
    """没有 UNKNOWN 需求时，缺 callback 不影响普通恢复（#29 行为不回退）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(
        ledger,
        session.session_id,
        "call-1",
        OperationState.SUCCEEDED,
        result_json=ToolResult.success("done").model_dump_json(),
    )

    recovered = await _make_coordinator(store, ledger, tmp_path / "state.db").recover(
        session.session_id
    )

    assert "call-1" in _result_events(recovered)
    assert recovered.events[-1].type == SESSION_RESUMED


# ── 事件流契约 ──


@pytest.mark.asyncio
async def test_reconcile_required_event_observable_and_no_checkpoint_events(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(ledger, session.session_id, "call-1", OperationState.RUNNING)

    recovered = await _make_coordinator(
        store,
        ledger,
        tmp_path / "state.db",
        reconcile_callback=_ScriptedCallback(ReconcileVerdict.CONFIRM_SUCCESS),
    ).recover(session.session_id)

    types = [event.type for event in recovered.events]
    assert OPERATION_RECONCILE_REQUIRED in types
    # checkpoint/saved 仍不进入事件流（不变量：checkpoint 不是 SessionEvent）。
    assert not [t for t in types if t.startswith("checkpoint")]


@pytest.mark.asyncio
async def test_mixed_recovery_resolves_terminal_and_reconciles_unknown(
    tmp_path: Path,
) -> None:
    """同一批：终态走 #29 精确合成，RUNNING 走 #30 人工裁决，互不干扰。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    session = Session.start(store)
    session.append(USER_MESSAGE, {"content": "run two tools"})
    session.append(
        MODEL_COMPLETED,
        {
            "content": "",
            "tool_calls": [
                {"id": "call-a", "name": "bash", "args": {}},
                {"id": "call-b", "name": "bash", "args": {}},
            ],
        },
        run_id="run-1",
        step_id=1,
    )
    for call_id in ("call-a", "call-b"):
        session.append(
            TOOL_CALL,
            {"tool_call_id": call_id, "tool_name": "bash", "args": {}},
            run_id="run-1",
            step_id=1,
        )
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(
        ledger,
        session.session_id,
        "call-a",
        OperationState.SUCCEEDED,
        result_json=ToolResult.success("a done").model_dump_json(),
    )
    await _seed_operation(ledger, session.session_id, "call-b", OperationState.RUNNING)

    recovered = await _make_coordinator(
        store,
        ledger,
        tmp_path / "state.db",
        reconcile_callback=_ScriptedCallback(ReconcileVerdict.ABANDON),
    ).recover(session.session_id)

    results = _result_events(recovered)
    assert ToolResult.model_validate_json(results["call-a"]).message == "a done"
    assert ToolResult.model_validate_json(results["call-b"]).retryable is False
    assert len(_reconcile_required_events(recovered)) == 1
    # 恢复后投影无 dangling。
    recovered.derive_messages()


# ── 损坏 result_json 不阻塞人工裁决（#30 容错回归）──


@pytest.mark.asyncio
async def test_confirm_success_with_corrupt_ledger_result_falls_back(
    tmp_path: Path,
) -> None:
    """裁决 CONFIRM_SUCCESS 时若 Ledger 的 result_json 不是合法 ToolResult，
    不能让 ValidationError 冒泡——回调已裁决、协调器不能因数据腐烂拒绝恢复。

    _verdict_outcome 在 CONFIRM_SUCCESS/CONFIRM_FAILURE 两路都直调
    ToolResult.model_validate_json(operation.result_json)：这里钉住两路守卫都
    降级到"已由用户确认"的合成结果。
    """
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(
        ledger,
        session.session_id,
        "call-1",
        OperationState.RUNNING,
        result_json="not-json",
    )
    callback = _ScriptedCallback(ReconcileVerdict.CONFIRM_SUCCESS)

    recovered = await _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback
    ).recover(session.session_id)

    synthesized = ToolResult.model_validate_json(_result_events(recovered)["call-1"])
    assert synthesized.ok is True
    assert "用户确认成功" in synthesized.message


@pytest.mark.asyncio
async def test_confirm_failure_with_corrupt_ledger_result_falls_back(
    tmp_path: Path,
) -> None:
    """CONFIRM_FAILURE + 损坏 result_json：降级到"已由用户确认失败"合成结果。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(
        ledger,
        session.session_id,
        "call-1",
        OperationState.RUNNING,
        result_json="{garbage",
    )
    callback = _ScriptedCallback(ReconcileVerdict.CONFIRM_FAILURE)

    recovered = await _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback
    ).recover(session.session_id)

    synthesized = ToolResult.model_validate_json(_result_events(recovered)["call-1"])
    assert synthesized.ok is False
    assert "用户确认失败" in synthesized.message


# ── T4 #120：reconcile reason 进 JSONL 诊断层（spec 12 §2）──


@pytest.mark.asyncio
async def test_reconcile_verdict_recorded_in_jsonl_diagnostic_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_crashed_session(store, tool_name="bash")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    result_json = ToolResult.success("migration applied").model_dump_json()
    await _seed_operation(
        ledger,
        session.session_id,
        "call-1",
        OperationState.RUNNING,
        result_json=result_json,
    )

    with caplog.at_level(logging.INFO, logger="agent_harness.recovery"):
        await _make_coordinator(
            store, ledger, tmp_path / "state.db",
            reconcile_callback=_ScriptedCallback(ReconcileVerdict.CONFIRM_SUCCESS),
        ).recover(session.session_id)

    verdict_lines = [
        r for r in caplog.records
        if getattr(r, "outcome", None) == "reconciled"
        and getattr(r, "reconcile_verdict", None) == "CONFIRM_SUCCESS"
    ]
    assert len(verdict_lines) == 1, "reconcile 裁决必须恰好一条 JSONL 诊断行"
    line = verdict_lines[0]
    assert getattr(line, "tool_call_id", None) == "call-1"
    assert getattr(line, "component", None) == "recovery"
    assert getattr(line, "ledger_state", None) == "SUCCEEDED"


# ── `#315` T7：**非悬空**的"副作用未证"行 ───────────────────────────────


def _make_timeout_session(
    store: JsonlSessionStore,
    call_id: str = "call-1",
    tool_name: str = "write_file",
) -> Session:
    """MUTATING 工具**超时**的现场：tool/call 与 tool/result **都齐**。

    与 `_make_crashed_session` 的区别就是这一条：悬空调用的 tool/result 缺席，
    而这里执行域已经如实落了一条"这次尝试超时、副作用状态未证"的结果。所以
    "只看悬空调用"的旧收集面永远看不到它——`#315` 要补的正是这个洞
    （`03 §5`：对账优先于恢复）。
    """
    session = Session.start(store)
    session.append(USER_MESSAGE, {"content": "把配置改掉"})
    session.append(
        MODEL_COMPLETED,
        {
            "content": "",
            "tool_calls": [{"id": call_id, "name": tool_name, "args": {}}],
        },
        run_id="run-1",
        step_id=1,
    )
    session.append(
        TOOL_CALL,
        {"tool_call_id": call_id, "tool_name": tool_name, "args": {}},
        run_id="run-1",
        step_id=1,
    )
    session.append(
        TOOL_RESULT,
        {
            "tool_call_id": call_id,
            "content": ToolResult.failure(
                "执行超时", error_code=ErrorCode.TIMEOUT, retryable=False,
            ).model_dump_json(),
        },
        run_id="run-1",
        step_id=1,
    )
    return session


def _results_for(session: Session, tool_call_id: str) -> list:
    return [
        event
        for event in session.events
        if event.type == TOOL_RESULT and event.data.get("tool_call_id") == tool_call_id
    ]


@pytest.mark.asyncio
async def test_non_dangling_unproven_operation_requires_a_callback(
    tmp_path: Path,
) -> None:
    """非悬空 + 未证 + 无 callback ⇒ 安全拒绝，且一个字节都不写。

    这是 resume 闸门（`#315`：`service.resume_and_launch` 发现账本欠账就调
    `recover()`）在生产上的实际结局：没有 ReconcileCallback 就落 409。此处证明
    它**先于任何写入**发生——拒绝路径不推进 Ledger、不补事件。
    """
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_timeout_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(
        ledger, session.session_id, "call-1", OperationState.UNKNOWN,
        tool_name="write_file",
        reconcile_meta=unproven_meta(
            error_code=ErrorCode.TIMEOUT.value, note="MUTATING 超时：副作用未证"
        ),
    )
    events_before = store.read_events(session.session_id)
    # 前提：这条调用**不是**悬空的（旧判据看不见它，正是本票要补的洞）
    assert detect_dangling(events_before) == []

    coordinator = _make_coordinator(store, ledger, tmp_path / "state.db")
    with pytest.raises(RecoveryError, match="UNKNOWN"):
        await coordinator.recover(session.session_id)

    operation = await ledger.get(session.session_id, "call-1")
    assert operation is not None and operation.state is OperationState.UNKNOWN
    assert store.read_events(session.session_id) == events_before, "安全拒绝 = 零写入"


@pytest.mark.asyncio
async def test_non_dangling_unproven_operation_is_reconciled_without_a_second_result(
    tmp_path: Path,
) -> None:
    """有 callback ⇒ 裁决只落一次，且**不**补第二条 `tool/result`。

    非悬空的行已经有一条如实的结果（"超时、状态未证"）——再补一条会破坏
    `derive_messages` 依赖的 1:1 配对（`07 §8`）。所以裁决的 durable 落点是
    **Ledger 行本身**：状态进终态、`reconcile_meta` 记下裁决。事件流里多出来的
    只有 `operation/reconcile-required`（必填人工关卡）与收尾的 `session/resumed`。
    """
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_timeout_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(
        ledger, session.session_id, "call-1", OperationState.UNKNOWN,
        tool_name="write_file",
        reconcile_meta=unproven_meta(
            error_code=ErrorCode.TIMEOUT.value, note="MUTATING 超时：副作用未证"
        ),
    )
    original_content = _result_events(session)["call-1"]
    callback = _ScriptedCallback(ReconcileVerdict.CONFIRM_SUCCESS)

    recovered = await _make_coordinator(
        store, ledger, tmp_path / "state.db", reconcile_callback=callback
    ).recover(session.session_id)

    assert len(callback.calls) == 1
    assert len(_reconcile_required_events(recovered)) == 1
    results = _results_for(recovered, "call-1")
    assert len(results) == 1, "同一 tool_call_id 只能有一条 tool/result（07 §8 的配对）"
    assert results[0].data["content"] == original_content, "原来那条如实的结果不被改写"
    operation = await ledger.get(session.session_id, "call-1")
    assert operation is not None and operation.state is OperationState.SUCCEEDED
    assert operation.reconcile_meta is not None
    assert (
        json.loads(operation.reconcile_meta)["verdict"]
        == ReconcileVerdict.CONFIRM_SUCCESS.value
    ), "裁决内容覆盖那一格标记：疑问已解除，不需要第二个清除标记"


@pytest.mark.asyncio
async def test_non_dangling_pending_operation_is_not_a_reconcile_case(
    tmp_path: Path,
) -> None:
    """反例：**非终态**不等于"欠对账"——`PENDING` 明文可以按策略重执行（`07 §6`）。

    这条行同样是"非悬空 + 非终态"，但 `needs_reconcile` 刻意把它排除在外：
    "能证明尚未启动"就没有世界状态未知的问题。缺 callback 也必须照常恢复——
    否则闸门会宽到把每次"刚开单还没跑"的调用都变成人工关卡。
    """
    store = JsonlSessionStore(tmp_path / "sessions")
    session = _make_timeout_session(store)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await _seed_operation(
        ledger, session.session_id, "call-1", OperationState.PENDING,
        tool_name="write_file",
    )

    recovered = await _make_coordinator(store, ledger, tmp_path / "state.db").recover(
        session.session_id
    )

    assert _reconcile_required_events(recovered) == []
    assert recovered.events[-1].type == SESSION_RESUMED
    operation = await ledger.get(session.session_id, "call-1")
    assert operation is not None and operation.state is OperationState.PENDING
