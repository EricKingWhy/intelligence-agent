"""`#314` T6：per-tool 配额的**接纳点**语义（ToolExecutor 侧）。

这里钉住的是执行域那一半：**在唯一接纳点记一次逻辑调用**、每次真实尝试记一次
attempt、准入前被拒的调用不消耗配额但理由可审计。run 账本那一半（`budget_delta`
如何被求和、如何触发暂停）在 `tests/agent/test_run_budget.py` 与
`tests/agent/test_run_pause_resume.py`。

为什么这些断言值得占位置（每一条都对着票面的一句 AC）：
- 「一次多调用批次按**逻辑调用**计数」→ 并发批次超发测试；
- 「每次**实际尝试**（含 retry）计一个 `tool_attempts`」→ 抖动工具 1 次失败后成功
  ⇒ `tool_attempts=2` 而 `tool_calls=1`（两者不是别名，`02 §5.1`）；
- 「在接纳点**之前**被拒的调用 MUST NOT 消耗配额，且拒绝理由必须可审计」→
  未注册 / 参数非法 / 配额已尽 / 审批拒绝四条路径各自的 `budget_delta` 与 `error_code`。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest
from pydantic import BaseModel, Field

from agent_harness.agent.run_budget import BudgetConsumed, consumed_from_events
from agent_harness.session import TOOL_RESULT, SessionEvent
from agent_harness.tooling import (
    ApprovalResponse,
    ErrorCode,
    PermissionPolicy,
    Tool,
    ToolCall,
    ToolExecutor,
    ToolPermission,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
)
from agent_harness.tooling.quota import ToolQuotaWindow
from tests.conftest import make_session


class _ValueArgs(BaseModel):
    value: Annotated[int, Field(..., description="要计入的值")]


class _NoArgs(BaseModel):
    pass


class _CountingTool(Tool):
    """带调用计数器的只读工具：`call_count` 是"真实执行了几次"的唯一证据。"""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.call_count = 0
        self.delay = delay

    @property
    def name(self) -> str:
        return "count"

    @property
    def description(self) -> str:
        return "计入一个整数值。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _ValueArgs

    async def execute(self, args: _ValueArgs) -> ToolResult:
        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return ToolResult.success(message=f"计入 {args.value}", data={"value": args.value})


class _FlakyTool(Tool):
    """前 `fail_times` 次抛 ConnectionError（TRANSIENT_ERROR，可重试），之后成功。

    它不是权限工具（避免和 approval 交互），只用来数 attempt。
    """

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.call_count = 0

    @property
    def name(self) -> str:
        return "flaky"

    @property
    def description(self) -> str:
        return "先抖动若干次再成功。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise ConnectionError(f"第 {self.call_count} 次抖动")
        return ToolResult.success(message="恢复了", data={"attempts": self.call_count})


class _DangerTool(Tool):
    """DANGER + MUTATING：在 READ_ONLY 策略下需要审批，且让批次走串行分支。"""

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def name(self) -> str:
        return "danger"

    @property
    def description(self) -> str:
        return "高风险写入工具。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    async def execute(self, args: BaseModel) -> ToolResult:
        self.call_count += 1
        return ToolResult.success(message="写完了", data={"ok": True})


def _window(limits: dict[str, int], consumed: dict[str, int] | None = None) -> ToolQuotaWindow:
    return ToolQuotaWindow(limits=limits, consumed=consumed or {})


def _call(call_id: str, name: str = "count", **args: object) -> dict[str, object]:
    return {"id": call_id, "name": name, "args": args}


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


# ── 计数：逻辑调用一次、尝试按实际次数 ──────────────────────────────────


@pytest.mark.asyncio
async def test_unconfigured_tool_is_unlimited_but_still_counted() -> None:
    """`04 §9.1`：未配置配额 = 不限，但**仍计入** `tool_calls` / `tool_attempts`。"""
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))

    first = await executor.execute(_call("c1", value=1), tool_quota=_window({}))
    second = await executor.execute(_call("c2", value=2), tool_quota=_window({}))

    assert first.result.ok and second.result.ok
    assert tool.call_count == 2
    assert first.budget_delta == {"tool_name": "count", "tool_calls": 1, "tool_attempts": 1}
    assert second.budget_delta["tool_calls"] == 1


@pytest.mark.asyncio
async def test_retry_adds_attempts_but_not_a_second_logical_call() -> None:
    """`04 §9.1`：retry MUST NOT 产生新的逻辑调用——1 次接纳、2 次尝试。"""
    tool = _FlakyTool(fail_times=1)
    executor = ToolExecutor(_registry(tool))

    execution = await executor.execute(_call("c1", name="flaky"), tool_quota=_window({}))

    assert execution.result.ok
    assert tool.call_count == 2
    assert execution.budget_delta == {
        "tool_name": "flaky", "tool_calls": 1, "tool_attempts": 2,
    }


@pytest.mark.asyncio
async def test_attempt_count_survives_overflow_rewrite() -> None:
    """账本数的是**工具域的真实尝试**，不是最终结果对象的 metadata 巧合。

    overflow 处理器会用 `model_copy` 换掉 message/data（大输出外置），
    `attempt` 必须还在——否则外置之后 `tool_attempts` 会静默掉到 0。
    """
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))
    execution = await executor.execute(_call("c1", value=3), tool_quota=_window({}))
    rewritten = execution.result.model_copy(update={"message": "（已外置）"})

    assert rewritten.metadata.get("attempt") == 1
    assert executor._admitted_delta("count", rewritten)["tool_attempts"] == 1


# ── 闸门：耗尽 ⇒ 阻止新调用，且不消耗配额 ──────────────────────────────


@pytest.mark.asyncio
async def test_exhausted_quota_blocks_before_any_work() -> None:
    """配额用尽 ⇒ 第二条**根本不被执行**（`call_count` 不动），理由是 BUDGET_EXHAUSTED。"""
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))
    window = _window({"count": 1})

    first = await executor.execute(_call("c1", value=1), tool_quota=window)
    second = await executor.execute(_call("c2", value=2), tool_quota=window)

    assert first.result.ok
    assert tool.call_count == 1
    assert second.result.ok is False
    assert second.result.error_code is ErrorCode.BUDGET_EXHAUSTED
    assert second.result.retryable is False
    assert "1/1" in second.result.message
    # 准入前被拒 ⇒ 不消耗配额，但这条事实**落在记录里**（可审计）。
    assert second.budget_delta == {"tool_name": "count", "tool_calls": 0, "tool_attempts": 0}


@pytest.mark.asyncio
async def test_parallel_batch_never_overadmits_a_single_tool() -> None:
    """一次多调用批次按**逻辑调用**计数：并发也只有一个被接纳。

    没有窗口的"检查+预留"时，三条并发只读调用会各自看到"账上还是 0"而全部放行
    ——这正是 `execute_batch` 的 parallel 分支会走到的形状（同一批、同一工具）。
    """
    tool = _CountingTool(delay=0.01)
    executor = ToolExecutor(_registry(tool))
    window = _window({"count": 1})

    executions = await executor.execute_batch(
        [_call("c1", value=1), _call("c2", value=2), _call("c3", value=3)],
        tool_quota=window,
    )

    assert [execution.result.ok for execution in executions] == [True, False, False]
    assert tool.call_count == 1
    assert [e.result.error_code for e in executions[1:]] == [
        ErrorCode.BUDGET_EXHAUSTED, ErrorCode.BUDGET_EXHAUSTED,
    ]
    assert sum(e.budget_delta["tool_calls"] for e in executions) == 1


@pytest.mark.asyncio
async def test_serial_batch_consumes_up_to_the_ceiling() -> None:
    """串行批次：配额 2 ⇒ 收下前两条，第三条被拒；真实执行次数 = 2。"""
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))

    executions = await executor.execute_batch(
        [_call("c1", value=1), _call("c2", value=2), _call("c3", value=3)],
        tool_quota=_window({"count": 2}),
    )

    assert [execution.result.ok for execution in executions] == [True, True, False]
    assert tool.call_count == 2


# ── 拒绝路径：四条都不消耗配额 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_admission_rejections_do_not_consume_quota() -> None:
    """未注册 / 参数非法：都是准入前拒绝 ⇒ 0 增量、不影响后续调用的余量。"""
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))
    window = _window({"count": 1})

    missing = await executor.execute(_call("c0", name="nope"), tool_quota=window)
    invalid = await executor.execute(_call("c0b", value="不是整数"), tool_quota=window)
    admitted = await executor.execute(_call("c1", value=1), tool_quota=window)

    assert missing.result.error_code is ErrorCode.TOOL_NOT_FOUND
    assert invalid.result.error_code is ErrorCode.INVALID_ARGUMENT
    assert missing.budget_delta["tool_calls"] == 0
    assert invalid.budget_delta["tool_calls"] == 0
    # 两条都被拒之后，配额仍是完整的 1
    assert admitted.result.ok and tool.call_count == 1


@pytest.mark.asyncio
async def test_approval_denial_releases_the_reserved_slot() -> None:
    """审批拒绝 ⇒ **归还**槽位：同批后面那条调用不该被一个没被接纳的兄弟挤掉。

    否则会出现"调用被拒（PERMISSION_DENIED）"与"配额已尽"同时成立却账上 0 消耗
    的自相矛盾状态——暂停判定读的是账本。
    """
    tool = _DangerTool()
    decisions = iter([False, True])

    async def approve(_request: object) -> ApprovalResponse:
        approved = next(decisions)
        return ApprovalResponse(approved=approved, reason="脚本化决定")

    executor = ToolExecutor(
        _registry(tool),
        policy=PermissionPolicy.READ_ONLY,
        approval_callback=approve,
    )
    window = _window({"danger": 1})

    denied = await executor.execute(_call("c1", name="danger"), tool_quota=window)
    admitted = await executor.execute(_call("c2", name="danger"), tool_quota=window)

    assert denied.result.error_code is ErrorCode.PERMISSION_DENIED
    assert denied.budget_delta == {"tool_name": "danger", "tool_calls": 0, "tool_attempts": 0}
    assert admitted.result.ok is True
    assert admitted.budget_delta == {"tool_name": "danger", "tool_calls": 1, "tool_attempts": 1}
    assert tool.call_count == 1


@pytest.mark.asyncio
async def test_serial_cascade_cancel_does_not_consume_quota() -> None:
    """串行熔断后"未执行"的兄弟：没被接纳 ⇒ 0 增量（否则一次永久失败烧掉整批配额）。

    批次形状：首条是 MUTATING 且准入前就被拒（READ_ONLY 策略 + 无审批回调
    ⇒ PERMISSION_DENIED，永久失败）⇒ 整批走串行、后续两条**根本不被执行**。
    """
    danger = _DangerTool()
    counting = _CountingTool()
    executor = ToolExecutor(
        _registry(danger, counting), policy=PermissionPolicy.READ_ONLY,
    )

    executions = await executor.execute_batch(
        [_call("c1", name="danger"), _call("c2", value=2), _call("c3", value=3)],
        tool_quota=_window({"count": 2}),
    )

    assert [execution.result.error_code for execution in executions] == [
        ErrorCode.PERMISSION_DENIED, ErrorCode.CANCELLED, ErrorCode.CANCELLED,
    ]
    assert all(execution.budget_delta["tool_calls"] == 0 for execution in executions)
    assert (danger.call_count, counting.call_count) == (0, 0)
    # 账上 0 消耗 ⇒ 同一配额下的新窗口（模拟恢复后的新批次）仍能收下 2 条
    reopened = ToolQuotaWindow(limits={"count": 2}, consumed={})
    assert [reopened.take("count") for _ in range(3)] == [True, True, False]


# ── durable 载体：执行域写、run 账本读 ──────────────────────────────────


@pytest.mark.asyncio
async def test_emitted_result_event_carries_budget_delta_and_folds_into_the_run_ledger(
    tmp_path: object,
) -> None:
    """端到端（执行域 → 事件 → run 账本）：`tool_calls_by_tool` 是纯折叠。

    这条测试是"计数点唯一"的机械证据：账本不认识 ToolExecutor，只按
    `tool/result.data.budget_delta` 求和；把两条接纳 + 一条拒绝的事件喂进去，
    得到的就是接纳的那两条。
    """
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))
    session = make_session(tmp_path)
    window = _window({"count": 1})

    first = await executor.execute(_call("c1", value=1), tool_quota=window)
    rejected = await executor.execute(_call("c2", value=2), tool_quota=window)

    events: list[SessionEvent] = []
    for execution in (first, rejected):
        events.append(executor.emit_result_event(
            session,
            tool_call_id=execution.tool_call_id,
            content=execution.result.model_dump_json(),
            run_id="run-1", step_id=1,
            budget_delta=execution.budget_delta,
        ))

    consumed = consumed_from_events(events)

    assert isinstance(consumed, BudgetConsumed)
    assert consumed.tool_calls_by_tool == {"count": 1}
    assert consumed.tool_attempts_by_tool == {"count": 1}
    assert consumed.tool_calls == 1
    persisted = [event for event in session.since(0) if event.type == TOOL_RESULT]
    assert [event.data["budget_delta"]["tool_calls"] for event in persisted] == [1, 0]


@pytest.mark.asyncio
async def test_actionable_delta_for_a_retried_call_lands_in_the_event(
    tmp_path: object,
) -> None:
    """重试两次的调用落一条结果、`tool_attempts=2`：账本按事件求和即得。"""
    tool = _FlakyTool(fail_times=1)
    executor = ToolExecutor(_registry(tool))
    session = make_session(tmp_path)

    execution = await executor.execute(_call("c1", name="flaky"), tool_quota=_window({}))
    executor.emit_result_event(
        session, tool_call_id=execution.tool_call_id,
        content=execution.result.model_dump_json(),
        run_id="run-1", step_id=1, budget_delta=execution.budget_delta,
    )

    consumed = consumed_from_events(session.since(0))

    assert (consumed.tool_calls, consumed.tool_attempts) == (1, 2)
    assert consumed.tool_calls_by_tool == {"flaky": 1}
    assert consumed.tool_attempts_by_tool == {"flaky": 2}


@pytest.mark.asyncio
async def test_tool_call_objects_are_accepted_like_dicts() -> None:
    """闸门对入参形状中立：`ToolCall` 值对象与 dict 走同一条接纳路径。"""
    tool = _CountingTool()
    executor = ToolExecutor(_registry(tool))

    execution = await executor.execute(
        ToolCall(id="c1", name="count", args={"value": 1}), tool_quota=_window({}),
    )

    assert execution.result.ok
    assert execution.budget_delta["tool_calls"] == 1
