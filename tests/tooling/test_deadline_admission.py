"""`#315` T7：deadline 的**接纳边界**与"未证副作用"的落账（ToolExecutor 侧）。

这里钉住执行域那一半，判据来自 `04 §9.1`（deadline 是一条**接纳**边界）、
`07 §4` / `07 §7`（`RUNNING → UNKNOWN → NEED_RECONCILE` 的两步链；write/edit 不确定 ⇒
NEED_RECONCILE）与 ADR-0044 D4（不得落一个暗示可安全续跑的暂停）：

- **到点即拒**：`run_deadline` 已到时，第二条调用**根本不被执行**（`call_count` 不动）、
  理由码是 `DEADLINE_EXCEEDED`、`retryable=False`（时刻不会因为再试一次就变到未来）、
  且**不落在 Ledger 上**（没被接纳就没有操作行——`07 §4` 的操作行只记被接纳的调用）；
- **未到点不拦**：给了未来的 deadline 时行为逐字不变；
- **未证副作用**：MUTATING + TIMEOUT 收尾成 `UNKNOWN` + 标记（不是 `FAILED`——那等于
  替工具断言"没生效"），READ_ONLY + TIMEOUT 仍是普通失败，确定性失败仍是 `FAILED`。

为什么单开一个文件：这两条是**不同的机制**（时钟判定的准入闸门 vs 世界状态的落账），
放在 `test_operation_ledger.py` 里会被读成"Ledger 的又一种状态"。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from agent_harness.storage import (
    OperationContext,
    OperationState,
    SqliteOperationLedger,
    has_unproven_side_effect,
    needs_reconcile,
)
from agent_harness.tooling import (
    ErrorCode,
    Tool,
    ToolCall,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
)
from agent_harness.tooling.quota import ToolQuotaWindow


class _NoArgs(BaseModel):
    pass


class _CountingTool(Tool):
    """只读 + 带调用计数器：`call_count` 是"真实执行过没有"的唯一证据。"""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.call_count = 0
        self.delay = delay

    @property
    def name(self) -> str:
        return "count"

    @property
    def description(self) -> str:
        return "数一次调用。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return ToolResult.success("ok")


class _SlowMutatingTool(_CountingTool):
    """MUTATING + 慢：`timeout_seconds` 到点即被掐断 ⇒ TIMEOUT + 副作用未证。"""

    @property
    def name(self) -> str:
        return "slow_write"

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def timeout_seconds(self) -> float:
        return 0.05

    async def execute(self, args: BaseModel) -> ToolResult:
        self.call_count += 1
        await asyncio.sleep(0.5)
        return ToolResult.success("写完了")


class _DeterministicFailTool(_CountingTool):
    """MUTATING 但**确定性**失败：世界状态是已知的（没写成）⇒ FAILED。"""

    @property
    def name(self) -> str:
        return "write_denied"

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    async def execute(self, args: BaseModel) -> ToolResult:
        self.call_count += 1
        return ToolResult.failure(
            "目标目录不存在，未写入任何字节",
            error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            retryable=False,
        )


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _call(call_id: str, name: str) -> dict[str, object]:
    return {"id": call_id, "name": name, "args": {}}


# ── 准入闸门：到点即拒，零副作用 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_deadline_in_the_past_rejects_before_any_work(tmp_path: Path) -> None:
    """到点 ⇒ 调用**不被执行**、不落 Ledger、不消耗配额，理由是 DEADLINE_EXCEEDED。"""
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool), operation_ledger=ledger)
    past = datetime.now(UTC) - timedelta(seconds=1)

    execution = await executor.execute(
        _call("c1", "count"),
        operation_context=OperationContext(session_id="s1", run_id="r1"),
        run_deadline=past,
    )

    assert execution.result.ok is False
    assert execution.result.error_code is ErrorCode.DEADLINE_EXCEEDED
    assert execution.result.retryable is False, "时刻不会因为重试而变到未来"
    assert "deadline" in execution.result.message.lower()
    # 文案必须**对模型**说清恢复后怎么办：只写"不要重复提交"会让模型把它读成
    # "本 run 到此为止"。实测（Live Gate 真实运行）：模型的原话就是
    # "The error says deadline not solved by retry. I should report status briefly."，
    # 恢复后的那一轮零工具调用 ⇒ 同 run 续跑在模型眼里成了走过场。
    assert "恢复" in execution.result.message and "继续" in execution.result.message, (
        "到点拒绝必须说明「以新的未来时刻恢复后从暂停前进度继续」"
    )
    assert tool.call_count == 0, "被拒的调用没有真实执行"
    assert execution.budget_delta == {
        "tool_name": "count", "tool_calls": 0, "tool_attempts": 0,
    }, "准入前被拒 ⇒ 不消耗配额（04 §9.1），但这条事实可审计"
    assert await ledger.list_for_session("s1") == [], (
        "没被接纳就没有操作行——07 §4 的 Ledger 只记被接纳的调用"
    )


@pytest.mark.asyncio
async def test_deadline_in_the_past_does_not_burn_the_per_tool_quota(tmp_path: Path) -> None:
    """闸门在配额 `take()` **之前**：被 deadline 拒的调用不该占掉配额槽位。"""
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))
    window = ToolQuotaWindow(limits={"count": 1}, consumed={})

    rejected = await executor.execute(
        _call("c1", "count"), tool_quota=window,
        run_deadline=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert rejected.result.error_code is ErrorCode.DEADLINE_EXCEEDED
    assert window.used("count") == 0, "被拒的调用没有占槽位"

    accepted = await executor.execute(_call("c2", "count"), tool_quota=window)
    assert accepted.result.ok is True, "同一个配额仍然可用（没有虚耗）"
    assert tool.call_count == 1


@pytest.mark.asyncio
async def test_a_future_deadline_does_not_change_anything() -> None:
    """未到点 ⇒ 行为逐字不变（default None 与未来的时刻同一条路径）。"""
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))
    future = datetime.now(UTC) + timedelta(seconds=60)

    with_deadline = await executor.execute(_call("c1", "count"), run_deadline=future)
    without = await executor.execute(_call("c2", "count"))

    assert with_deadline.result.ok is True and without.result.ok is True
    assert tool.call_count == 2
    assert with_deadline.budget_delta == without.budget_delta


@pytest.mark.asyncio
async def test_batch_admission_carries_the_same_deadline() -> None:
    """批次里每一条都过同一道闸门（`execute_batch` 把 deadline 透传给每条调用）。"""
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))

    executions = await executor.execute_batch(
        [_call("c1", "count"), _call("c2", "count")],
        run_deadline=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert [item.result.error_code for item in executions] == [
        ErrorCode.DEADLINE_EXCEEDED, ErrorCode.DEADLINE_EXCEEDED,
    ]
    assert tool.call_count == 0


# ── 收尾落账：未证副作用不是"失败" ──────────────────────────────────────


def _fake_tool(*, mutating: bool) -> Tool:
    tool = _CountingTool()
    if mutating:
        tool = _SlowMutatingTool()
    return tool


def test_settle_state_marks_a_mutating_timeout_as_unproven(tmp_path: Path) -> None:
    """`07 §7`：write/edit 不确定 ⇒ 不是 FAILED，而是 UNKNOWN + 标记（等对账）。"""
    tool = _fake_tool(mutating=True)
    timed_out = ToolResult.failure(
        "执行超时", error_code=ErrorCode.TIMEOUT, retryable=False,
    )

    state, meta = ToolExecutor._settle_state(tool, timed_out)

    assert state is OperationState.UNKNOWN
    assert meta is not None and "unproven" in meta


def test_settle_state_keeps_plain_failures_and_readonly_timeouts_as_failed() -> None:
    """READ_ONLY 超时与确定性失败都**没有**"世界状态未知"的问题 ⇒ 仍是 FAILED。"""
    readonly_timeout = ToolResult.failure(
        "执行超时", error_code=ErrorCode.TIMEOUT, retryable=True,
    )
    deterministic = ToolResult.failure(
        "坏参数", error_code=ErrorCode.TOOL_EXECUTION_ERROR, retryable=False,
    )

    assert ToolExecutor._settle_state(_fake_tool(mutating=False), readonly_timeout) == (
        OperationState.FAILED, None,
    )
    assert ToolExecutor._settle_state(_fake_tool(mutating=True), deterministic) == (
        OperationState.FAILED, None,
    )
    assert ToolExecutor._settle_state(
        _fake_tool(mutating=True), ToolResult.success("ok"),
    ) == (OperationState.SUCCEEDED, None)


@pytest.mark.asyncio
async def test_mutating_timeout_lands_in_the_ledger_as_needing_reconcile(tmp_path: Path) -> None:
    """端到端：超时的 MUTATING 调用在 Ledger 上就是"要点名对账"的那一行。"""
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    tool = _SlowMutatingTool()
    executor = ToolExecutor(_registry(tool), operation_ledger=ledger)

    execution = await executor.execute(
        _call("c1", "slow_write"),
        operation_context=OperationContext(session_id="s1", run_id="r1"),
    )

    assert execution.result.error_code is ErrorCode.TIMEOUT
    operation = await ledger.get("s1", "c1")
    assert operation is not None
    assert operation.state is OperationState.UNKNOWN
    assert has_unproven_side_effect(operation) is True
    assert needs_reconcile(operation) is True, "恢复闸门读的就是这个判据"


@pytest.mark.asyncio
async def test_a_deterministic_mutating_failure_stays_reconcilable_free(tmp_path: Path) -> None:
    """反例：确定性失败的 MUTATING 调用**不**留欠账（世界状态已知，不欠对账）。"""
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    tool = _DeterministicFailTool()
    executor = ToolExecutor(_registry(tool), operation_ledger=ledger)

    execution = await executor.execute(
        ToolCall(id="c1", name="write_denied", args={}),
        operation_context=OperationContext(session_id="s1", run_id="r1"),
    )

    assert execution.result.ok is False
    operation = await ledger.get("s1", "c1")
    assert operation is not None and operation.state is OperationState.FAILED
    assert needs_reconcile(operation) is False
