"""`#312` T4：一个逻辑 run 的暂停与同 run 续跑（runtime 层，真实事件流 + 真实 Store）。

判据来源：`02 §5.2`（暂停 / 预留 closeout / 恢复沿用同一 run）、`03 §3.4`（两个事件的
字段与不变量）、`03 §5`（`paused` 可恢复、不是终态）、`11 §6.1`（投影可重建）、
ADR-0044 D2/D3。

本文件补的是**领域模块与 golden 之外的第三条腿**：
- 纯函数（`test_run_budget.py`）证明账本算术对；
- golden（`test_event_sequence_golden.py`）证明事件序列没变；
- 这里证明**跨执行**的语义对——暂停一次执行、用同一个 run_id 再跑一次，消耗累计、
  身份不变、重启后投影一致（"进程内记忆"在这条链上不参与任何判定）。

账目约定（多处断言的前提，`02 §5.1` 的七个 counter 互不混同）：`consumed.agent_turns`
只数**被接纳进 loop 的模型决策**（`model/completed`）；closeout 那一次是 `model_requests`，
**不**进这个 counter。它在预算里的位置由 **run ceiling 的预留**表达：暂停发生在 ceiling
前一轮（`consumed + 1 >= ceiling`），于是 `ceiling=N` 的 run 接纳 N−1 个产出轮之后收口。
local fuse **没有**这条预留（EB-2 的"500 步到顶"就是到顶），这条不对称是刻意的。

`#313` 起暂停快照是**四维**的（turns / requests / tokens / cost）：本文件的用例多为
"模型没报 usage / cost"的剧本，所以那两个维度断言为 `None`（未知）而不是 0——把已知的
部分写成 0 会让"不可得 ≠ 0"这条契约在测试自己身上先破掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.agent.budget import SOURCE_DEPLOYMENT
from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    CLOSEOUT_MODEL,
    CONTINUATION_ACTION_KEY,
    REASON_BUDGET_EXHAUSTED,
    REASON_DEADLINE,
    TRIGGER_LOCAL_TURNS,
    TRIGGER_RUN_DEADLINE,
    TRIGGER_RUN_TURNS,
    BudgetConsumed,
    LaunchRunBudget,
    RunLimits,
    derive_run_budget,
    latest_paused_run,
)
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_PAUSED
from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_REQUEST,
    OPERATION_RECONCILE_REQUIRED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    Session,
)
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import (
    OperationState,
    SqliteOperationLedger,
    has_unproven_side_effect,
)
from agent_harness.storage import (
    needs_reconcile as needs_reconcile_state,
)
from agent_harness.tooling import (
    ErrorCode,
    Tool,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel
from tests.tooling.test_deadline_admission import _SlowMutatingTool

TOOL_ID = "call_t4"


class _EchoArgs(BaseModel):
    text: str = "x"


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "原样回显。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EchoArgs

    async def execute(self, args: _EchoArgs) -> ToolResult:
        return ToolResult.success(message=args.text)


def _runtime(
    model, *, ceiling: int | None, consumed: int = 0, run_id: str | None = None,
    version: int = 1, local_fuse: int = 500,
    operation_ledger: SqliteOperationLedger | None = None,
    tool_call_limits: dict[str, int] | None = None,
    tools: Sequence[Tool] = (),
    deadline_at: datetime | None = None,
) -> AgentRuntime:
    """一个执行实例：`local_fuse` 是本实例的保险丝，`run_budget` 是启动时的 run 账本。

    `operation_ledger` 给了就按**生产接线**挂上（`tracks_operations=True`）——那会让
    `model/completed` 走到延迟落盘分支，是另一条时序（不是配置口味）。
    `tool_call_limits`（`#314`）是 run 档的 per-tool 绝对配额（未配 ⇒ 该工具不限）。
    `tools`（`#315`）是额外注册的工具（默认只有 echo）。
    `deadline_at`（`#315`）是本 run 的绝对截止时刻（判据只看它和"现在"，见 `_now`）。
    """
    registry = ToolRegistry()
    registry.register(_EchoTool())
    for tool in tools:
        registry.register(tool)
    return AgentRuntime(
        model=model, registry=registry,
        executor=ToolExecutor(registry, operation_ledger=operation_ledger),
        max_agent_turns=local_fuse, local_fuse_source=SOURCE_DEPLOYMENT,
        run_budget=LaunchRunBudget(
            version=version,
            limits=RunLimits(
                max_agent_turns_total=ceiling,
                tool_call_limits=tool_call_limits or {},
                deadline_at=deadline_at,
            ),
            consumed=BudgetConsumed(agent_turns=consumed), run_id=run_id,
        ),
    )


def _clock(runtime: AgentRuntime, *times: datetime) -> None:
    """把本执行的"现在"钉在给定的时刻序列上（`#315`；最后一个值粘住）。

    `AgentRuntime._now` 是本执行读挂钟的唯一入口（收成一处的理由就是让用例能这样
    驱动"到点了吗"，不必 sleep 或改系统时钟）。用例给几个值就按调用次序推进——
    deadline 判定与 closeout 容量判定读的都是它，所以"第一步还没到点、第二步到点"
    这种时序在测试里是可复现的，而不是靠掐秒表。
    """
    remaining = list(times)

    def _now() -> datetime:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    runtime._now = _now  # type: ignore[method-assign]


def _tool_round(idx: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"{TOOL_ID}_{idx}", "name": "echo", "args": {"text": f"t{idx}"}}],
    )


def _reporting(message: AIMessage, *, cost: str) -> AIMessage:
    """给剧本消息挂上 Provider 自报的 usage 与**归属成本**（`response_metadata["cost"]`,
    `#313` 的 cost 维唯一来源）。原样保留内容与 tool_calls，只加报账字段。"""
    return AIMessage(
        content=message.content,
        tool_calls=list(message.tool_calls),
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        response_metadata={"cost": cost},
    )


def _continuation_json(**overrides: object) -> AIMessage:
    payload: dict[str, object] = {
        "completed": ["已完成第一步"],
        "remaining": ["还剩第二步"],
        "blockers": [],
        "next_safe_action": "继续第二步",
    }
    payload.update(overrides)
    return AIMessage(content=json.dumps(payload, ensure_ascii=False))


class _ForbiddenModel:
    """被调用即失败的模型：用来证明"没有容量时一次调用都不发"。"""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages: list, **kwargs) -> AIMessage:
        self.calls += 1
        raise AssertionError("没有预留容量时不得调用模型")

    async def astream(self, messages: list, **kwargs):
        self.calls += 1
        raise AssertionError("没有预留容量时不得调用模型")
        yield AIMessage(content="")


def _run_id_of(session: Session) -> str:
    return next(e.run_id for e in session.events if e.type == RUN_STARTED and e.run_id)


# ── 暂停：预留容量内的有界 closeout ──────────────────────────────────────


@pytest.mark.asyncio
async def test_model_closeout_is_a_model_request_not_an_accepted_turn(tmp_path) -> None:
    """ceiling=3 且模型给了合法 continuation ⇒ 暂停时 `agent_turns` 只记 2。

    账：2 个普通轮（被接纳） + 1 次 closeout 模型调用。那次调用**不**记进
    `consumed.agent_turns`（它是 `model_requests`，`02 §5.1` 禁止把七个 counter 混同）；
    它的位置由**预留**表达——判定含预留轮（`consumed + 1 >= ceiling`），所以第 3 轮
    不会既当普通轮又当收口轮。暂停不是终态：既没有 `run/completed`，也没有
    `run/failed`——"到顶"本身不是失败（PRD #305 EB-2）。
    """
    scripted = ScriptedModel([_tool_round(1), _tool_round(2), _continuation_json()])
    runtime = _runtime(scripted, ceiling=3)
    session = make_session(tmp_path)

    result = await runtime.run(session, "两轮之后该收口了")

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["reason"] == REASON_BUDGET_EXHAUSTED
    assert paused.data["trigger_dimension"] == TRIGGER_RUN_TURNS
    assert paused.data["closeout_source"] == CLOSEOUT_MODEL
    assert paused.data["consumed"] == {
        "agent_turns": 2, "model_requests": 3, "total_tokens": None, "cost_usd": None,
        # `#314`：两轮各一条被接纳的 echo 调用（各一次尝试）——预算是**运行**事实，
        # 与幂等无关：被接纳就算，哪怕工具这次是读操作。
        "tool_calls": 2, "tool_attempts": 2,
        "tool_calls_by_tool": {"echo": 2}, "tool_attempts_by_tool": {"echo": 2},
    }, "四维快照：2 个被接纳的轮 + 3 次真实请求（2 个普通轮 + 1 次 closeout）"
    assert paused.data["limits"]["run"]["max_agent_turns_total"] == 3
    assert paused.data["continuation"] == {
        "completed": ["已完成第一步"],
        "remaining": ["还剩第二步"],
        "blockers": [],
        CONTINUATION_ACTION_KEY: "继续第二步",
    }
    assert paused.data["resume_requirements"] == []
    assert [e.type for e in session.events if e.type in (RUN_COMPLETED, RUN_FAILED)] == []
    # closeout 真的过了一次模型（有界机会），但那不是被接纳的一轮：模型侧调用 3 次
    assert len(scripted.snapshots) == 3

    state = derive_run_budget(session.events, _run_id_of(session))
    assert state.version == 1
    assert state.consumed_turns == 2, "派生账本 = 事件说的事实（closeout 不进计数）"
    assert state.paused is not None
    assert state.resumable is True


@pytest.mark.asyncio
async def test_provider_reported_cost_is_summed_exactly_across_steps_and_closeout(
    tmp_path,
) -> None:
    """Provider 自报归属成本（`response_metadata["cost"]`）时逐次入账，**十进制精确**。

    `#313` 的 cost 维只有一条来源：Provider 自己报的归属成本（`02 §5.1`：只统计
    Provider 自报的值，不臆造费率表）。本用例把这条正路走一遍：普通轮与 closeout
    各报一次，三次请求（2 个被接纳的轮 + 1 次 closeout）合计必须**逐字**
    等于手工相加的十进制值——`float` 累加会让 `0.0025 × 3` 变成 `0.007500000000000001`，
    而 wire 上的成本是十进制（`11 §6.1`：二进制浮点相等不是契约）。

    第二个用例走"先有后无"：closeout 那次没报 ⇒ 整本账未知（`None`），不是
    "已知部分的和"——把未知当 0 会得到一份看起来精确、实际上少了钱的账。
    """
    cost = "0.0025"
    # 每一次响应都自报同一笔归属成本（脚本消息原样吐回，字段随消息走）。
    scripted = ScriptedModel([
        _reporting(_tool_round(1), cost=cost),
        _reporting(_tool_round(2), cost=cost),
        _reporting(_continuation_json(), cost=cost),
    ])
    runtime = _runtime(scripted, ceiling=3)
    session = make_session(tmp_path)

    result = await runtime.run(session, "两轮之后收口，成本逐次报账")

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    requests = [e for e in session.events if e.type == MODEL_REQUEST]
    assert len(requests) == 3, "2 个普通轮 + 1 次 closeout，恰三次真实请求"
    assert [e.data.get("cost_usd") for e in requests] == [cost, cost, cost]
    assert paused.data["consumed"]["cost_usd"] == "0.0075", (
        "三次请求各 0.0025 ⇒ 0.0075（十进制字符串，不是浮点近似）"
    )
    assert paused.data["consumed"]["total_tokens"] == 45

    state = derive_run_budget(session.events, _run_id_of(session))
    assert state.consumed.cost_usd == Decimal("0.0075")
    assert state.consumed.total_tokens == 45


@pytest.mark.asyncio
async def test_a_later_request_without_cost_makes_the_whole_dimension_unknown(
    tmp_path,
) -> None:
    """一次没自报归属成本 ⇒ 该维**未知**（`None`），不是"已知部分之和"。

    `None` 粘性（`runtime._TerminalArms.add_cost`）：总和 = 已知部分 + 未知部分，
    后者不可知，所以整本是未知。生产链路（`reports_cost=False`）恒走这一格——
    把它写成 0 会让对账以为"这个 run 不要钱"（`11 §6.1`：不可得 ≠ 0）。
    """
    scripted = ScriptedModel([
        _reporting(_tool_round(1), cost="0.0100"),
        # closeout 那次响应不带 cost（Provider 静默没报）：`response_metadata` 里没有这个键。
        _continuation_json(),
    ])
    runtime = _runtime(scripted, ceiling=2)
    session = make_session(tmp_path)

    await runtime.run(session, "第二次没报成本")

    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert [e.data.get("cost_usd") for e in session.events if e.type == MODEL_REQUEST] == [
        "0.0100", None,
    ]
    assert paused.data["consumed"]["cost_usd"] is None, "一次缺失 ⇒ 整维未知，不是 0.01"


@pytest.mark.asyncio
async def test_paused_projection_reads_the_consumed_snapshot(tmp_path) -> None:
    """`PausedRun.consumed_turns` 取的是**事件里的快照**，不是"重数一遍事件"。

    两者在"暂停之后又续跑"时会分道扬镳：重算值继续涨，快照停在暂停那一刻。拿重算值
    当"暂停时的消耗"会让 `resume_headroom_ok` 与 `run/resumed.consumed` 一起漂移
    ——那正是"恢复不得重置/放大消耗"这条不变量会破的地方。
    """
    scripted = ScriptedModel([_tool_round(1), _continuation_json()])
    runtime = _runtime(scripted, ceiling=2)
    session = make_session(tmp_path)

    await runtime.run(session, "一轮就收口")

    run_id = _run_id_of(session)
    paused = latest_paused_run(session.events)
    assert paused is not None
    assert paused.run_id == run_id
    # ceiling=2 ⇒ 1 个普通轮 + 1 次 closeout（后者不进 counter）
    assert paused.consumed_turns == 1
    assert derive_run_budget(session.events, run_id).consumed_turns == paused.consumed_turns


@pytest.mark.asyncio
async def test_no_model_call_at_all_when_the_closeout_has_no_capacity(tmp_path) -> None:
    """`consumed >= ceiling` ⇒ 一次模型调用都不发，直接落确定性 continuation。

    这是"closeout 不得超出适用预算"的边界（`02 §5.2`）。生产上对应"活动 run 的
    ceiling 被下压到恰好等于已消耗"（PRD §3 允许下压到不低于已消耗，下压入口本身
    不在本票内），所以用启动上下文直接摆到这个边界上。`budget_version` 如实回传启动
    上下文的值（暂停臂不自己加一）。
    """
    model = _ForbiddenModel()
    runtime = _runtime(
        model, ceiling=1, consumed=1, run_id="run-lowered-ceiling", version=2,
    )
    session = make_session(tmp_path)

    result = await runtime.run(session, None)

    assert model.calls == 0
    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["closeout_source"] == CLOSEOUT_DETERMINISTIC
    assert paused.data["consumed"] == {
        "agent_turns": 1, "model_requests": 0, "total_tokens": 0, "cost_usd": "0",
        # `#314`：工具维同源（启动上下文里就是空的 0/{}，本执行没有工具批次）
        "tool_calls": 0, "tool_attempts": 0,
        "tool_calls_by_tool": {}, "tool_attempts_by_tool": {},
    }, "快照沿用启动上下文，不重数（本执行一个请求都没发 ⇒ 空和真的是 0）"
    assert paused.data["budget_version"] == 2
    assert paused.data["trigger_dimension"] == TRIGGER_RUN_TURNS


# ── 暂停边界那一轮的工具批次与延迟落盘 ────────────────────────────────────


@pytest.mark.asyncio
async def test_deferred_model_event_stays_behind_the_tool_batch_at_the_pause_boundary(
    tmp_path,
) -> None:
    """挂 Ledger 的执行器 + 暂停边界那一轮：`model/completed` 仍在它引用的工具之后。

    延迟落盘的语义是"model 决策的 durable 记录不早于它引用的那批工具"，**与预算无关**：
    预算判定在循环顶部，只决定**下一轮**还起不起模型调用，本轮整批工具照跑。把预算前瞻
    掺回这个判定（`#312` 审查前的形状）会让暂停轮——恰恰是最需要可对账的那一轮——退回
    "model/completed 先于工具"，而且**没有任何既有用例看得见**：golden 的 `local_fuse_pause`
    用无 Ledger 的执行器（那条分支根本不走），
    `tests/agent/test_operation_ledger_runtime.py` 只钉了无 Ledger 时的相反形状。
    """
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    scripted = ScriptedModel([_tool_round(1), _continuation_json()])
    runtime = _runtime(scripted, ceiling=2, operation_ledger=ledger)
    session = make_session(tmp_path)

    result = await runtime.run(session, "跑一轮工具就撞 ceiling")

    assert result.status == STATUS_PAUSED
    types = [e.type for e in session.events]
    assert types.index(TOOL_CALL) < types.index(MODEL_COMPLETED) < types.index(TOOL_RESULT), (
        "暂停轮里 model 决策的 durable 记录必须晚于它引用的 tool/call"
    )
    assert types.index(MODEL_COMPLETED) < types.index(RUN_PAUSED), "暂停收口在账目之后"


# ── 恢复：同一个 run_id、消耗不重置 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_same_run_resume_completes_without_resetting_accounting(tmp_path) -> None:
    """暂停 → 抬高绝对 ceiling → 同 run 续跑完成：run_id 不变、消耗继续累加。

    续跑执行的输入是 `LaunchRunBudget(run_id=..., version+1, consumed 快照)`——runtime
    因此**不**再 `begin_run`（事件流里只有一个 `run/started`），也不落 `user/message`
    （`user_input=None`：普通预算恢复不需要新任务文本）。
    """
    first = ScriptedModel([_tool_round(1), _tool_round(2), _continuation_json()])
    session = make_session(tmp_path)
    await _runtime(first, ceiling=3).run(session, "先跑两轮")

    paused = latest_paused_run(session.events)
    assert paused is not None
    pause_seq = len(session.events)
    assert paused.consumed_turns == 2

    second = ScriptedModel([AIMessage(content="第二步也做完了")])
    resumed_runtime = _runtime(
        second, ceiling=5, consumed=paused.consumed_turns, run_id=paused.run_id,
        version=paused.version + 1,
    )
    result = await resumed_runtime.run(session, None)

    assert result.status == STATUS_COMPLETED
    assert result.final_text == "第二步也做完了"

    new_events = list(session.events[pause_seq:])
    assert [e.type for e in new_events].count(RUN_RESUMED) == 0, (
        "run/resumed 由 SessionService 在 launch 之前落盘（这里直接驱动 runtime）"
    )
    assert new_events[0].type == MODEL_REQUEST, (
        "续跑第一次请求就落账（`#313` 起请求有自己的计数点）"
    )
    assert new_events[-1].type == RUN_COMPLETED
    # run 身份单一：恢复沿用同一逻辑 run——没有第二条 run/started、也没有新 user/message
    assert [e.type for e in session.events].count(RUN_STARTED) == 1
    assert [e.type for e in session.events].count(USER_MESSAGE) == 1
    assert {e.run_id for e in session.events if e.run_id} == {paused.run_id}

    state = derive_run_budget(session.events, paused.run_id)
    assert state.consumed_turns == paused.consumed_turns + 1, "消耗继续累加，绝不重置"
    # version 只由 `run/resumed` 推进（CAS 的比较对象），不是"执行了几次"——这条链上
    # 没有那个事件（服务层才落），所以仍是 1。反过来说：任何实现都不能靠 +1 猜版本。
    assert state.version == 1
    assert state.terminal is True
    assert state.resumable is False

    # 续跑那次模型看到的最后一条消息是**已有历史**（工具结果），不是伪造的新任务文本
    assert second.snapshots[0].messages[-1].type == "tool", (
        "普通预算恢复不伪一条新用户消息（票面明文）"
    )


@pytest.mark.asyncio
async def test_resume_without_new_task_text_keeps_the_original_instruction(tmp_path) -> None:
    """续跑不重落 `user/message`：原任务文本仍在上下文里（不是"没任务可做"）。"""
    first = ScriptedModel([_tool_round(1), _continuation_json()])
    session = make_session(tmp_path)
    original = "把 A 改成 B"
    await _runtime(first, ceiling=2).run(session, original)

    paused = latest_paused_run(session.events)
    assert paused is not None
    second = ScriptedModel([AIMessage(content="done")])
    await _runtime(
        second, ceiling=4, consumed=paused.consumed_turns, run_id=paused.run_id,
        version=paused.version + 1,
    ).run(session, None)

    texts = [m.content for m in second.snapshots[0].messages if m.type == "human"]
    assert original in texts


@pytest.mark.asyncio
async def test_local_fuse_counts_the_current_execution_not_the_run_ledger(tmp_path) -> None:
    """local fuse 是**实例级**的：续跑执行从 0 起步数它自己的步数。

    构造：没有 run ceiling（`max_agent_turns_total=None`），local fuse = 2，启动账本
    已有 3 轮 ⇒ 续跑执行跑满 2 个普通轮才触发 local fuse（`steps >= 2`），run 账本
    （3）与本判定无关（fuse 不吃"预留 closeout"那一轮，也不看累计消耗）。命中维度如实
    回传 `local.max_agent_turns`——两个作用域互不替代（`02 §5.1`），投影上必须能分辨
    是谁到顶。closeout 仍可发生（run 侧无 ceiling ⇒ 不设限），其来源照实记进快照。
    """
    session = make_session(tmp_path)
    scripted = ScriptedModel([_tool_round(1), _tool_round(2), _continuation_json()])
    runtime = _runtime(
        scripted, ceiling=None, consumed=3, run_id="run-existing", version=2, local_fuse=2,
    )

    result = await runtime.run(session, None)

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["trigger_dimension"] == TRIGGER_LOCAL_TURNS
    # run 账本：启动 3 + 本执行 2 个普通轮 = 5；closeout 那一次是 model_requests
    assert paused.data["consumed"] == {
        "agent_turns": 5, "model_requests": 3, "total_tokens": None, "cost_usd": None,
        # `#314`：工具维只数**本执行**接纳的调用（启动上下文里的工具账是空的）
        "tool_calls": 2, "tool_attempts": 2,
        "tool_calls_by_tool": {"echo": 2}, "tool_attempts_by_tool": {"echo": 2},
    }
    assert paused.data["closeout_source"] == CLOSEOUT_MODEL
    assert paused.data["limits"]["run"]["max_agent_turns_total"] is None
    assert paused.data["limits"]["local"]["max_agent_turns"] == 2


# ── 进程重启 / 重放：投影可重建 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_rebuilds_the_same_paused_projection(tmp_path) -> None:
    """丢掉进程内对象、重新加载聚合 ⇒ 同一份暂停投影（版本 / 消耗 / continuation / 位置）。

    这条是 AC "Process restart/replay reconstructs the same paused run, counters,
    version and continuation without resetting them" 的确定性版本：账本没有第二份
    真相（`derive_run_budget` 纯函数），所以"重建"就是"重读"。
    """
    scripted = ScriptedModel([_tool_round(1), _tool_round(2), _continuation_json()])
    session = make_session(tmp_path)
    await _runtime(scripted, ceiling=3).run(session, "撞到 ceiling")
    before = latest_paused_run(session.events)
    assert before is not None

    # "进程重启"：同一份 JSONL，新的聚合对象
    store = JsonlSessionStore(root=tmp_path)
    reloaded = Session.load(store, session.session_id)
    after = latest_paused_run(store.read_events(session.session_id))

    assert after is not None
    assert after.as_projection() == before.as_projection()
    assert after.version == before.version
    assert after.consumed_turns == before.consumed_turns
    assert after.pause_seq == before.pause_seq
    assert after.continuation == before.continuation

    # 重新加载的聚合还能继续追加（seq 计数器不落后 ⇒ 日志不重复、可再次 resume）
    reloaded.append(USER_MESSAGE, {"content": "reload 之后的追加"}, run_id=before.run_id)
    again = Session.load(store, session.session_id)
    assert again.events[-1].data["content"] == "reload 之后的追加"
    assert [e.seq for e in again.events] == list(range(len(again.events)))


@pytest.mark.asyncio
async def test_paused_stream_ends_cleanly_on_the_pause_frame(tmp_path) -> None:
    """暂停把**当前执行流**干净收口：最后一帧是 `run/paused`，之后没有终态帧。"""
    scripted = ScriptedModel([_continuation_json()])
    runtime = _runtime(scripted, ceiling=1)
    session = make_session(tmp_path)

    frames = [frame async for frame in runtime.run_stream(session, "流式撞顶")]

    assert frames[-1].type == RUN_PAUSED
    assert frames[-1].seq is not None, "暂停是 durable 事实（要参与重连重放）"
    assert [f.type for f in frames if f.type in (RUN_COMPLETED, RUN_FAILED)] == []


# ── `#314` T6：per-tool 配额的 run 生命周期 ──────────────────────────────


def _two_call_round(idx: int) -> AIMessage:
    """同一轮里对同一工具发**两条**调用（一次多调用批次的最小形状）。"""
    return AIMessage(
        content="",
        tool_calls=[
            {"id": f"{TOOL_ID}_{idx}a", "name": "echo", "args": {"text": f"t{idx}a"}},
            {"id": f"{TOOL_ID}_{idx}b", "name": "echo", "args": {"text": f"t{idx}b"}},
        ],
    )


@pytest.mark.asyncio
async def test_per_tool_quota_blocks_the_extra_call_and_pauses_the_run(tmp_path) -> None:
    """同一批两条 echo、配额 1：第二条在**接纳前**被拒，run 按 T4 的生命周期暂停。

    票面（`#314`）要的三件事在同一条时间线上：

    - 配额是**逻辑调用**的绝对上限（不是"每轮一次"）：同一批的第一条照常执行、
      第二条被拒——判定在接纳点之前，所以拒绝**不**消耗配额；
    - 拒绝可审计：结果里 `error_code=BUDGET_EXHAUSTED`，同一条结果的
      `budget_delta.tool_calls=0`（"没消耗"是记录里的字面事实）；
    - 用尽 ⇒ 复用 T4 的暂停生命周期（一条非终态 `run/paused`），而**不是**把 run
      判失败；`trigger_dimension` 指名那一维 `run.tool_call_limits.echo`。
    """
    scripted = ScriptedModel([_two_call_round(1), _continuation_json()])
    runtime = _runtime(scripted, ceiling=3, tool_call_limits={"echo": 1})
    session = make_session(tmp_path)

    result = await runtime.run(session, "同一轮里把 echo 调两次")

    assert result.status == STATUS_PAUSED
    results = [e for e in session.events if e.type == TOOL_RESULT]
    assert len(results) == 2, "两条 tool/call 各有自己的结果（保序配对）"
    payloads = [json.loads(e.data["content"]) for e in results]
    assert [p["ok"] for p in payloads] == [True, False]
    assert payloads[1]["error_code"] == ErrorCode.BUDGET_EXHAUSTED.value
    assert [e.data["budget_delta"]["tool_calls"] for e in results] == [1, 0]

    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["trigger_dimension"] == "run.tool_call_limits.echo"
    assert paused.data["consumed"]["tool_calls"] == 1
    assert paused.data["consumed"]["tool_calls_by_tool"] == {"echo": 1}
    assert paused.data["limits"]["run"]["tool_call_limits"] == {"echo": 1}
    assert [e.type for e in session.events if e.type in (RUN_COMPLETED, RUN_FAILED)] == []

    # ── 续跑：抬高**绝对**配额（不是增量）⇒ 同一工具再次可用，run 走到完成 ──
    resumed = ScriptedModel([_tool_round(2), AIMessage(content="做完了")])
    resumed_runtime = _runtime(
        resumed, ceiling=5, consumed=paused.data["consumed"]["agent_turns"],
        run_id=paused.run_id, version=paused.data["budget_version"] + 1,
        tool_call_limits={"echo": 2},
    )
    final = await resumed_runtime.run(session, None)

    assert final.status == STATUS_COMPLETED
    assert [e.type for e in session.events].count(RUN_PAUSED) == 1, (
        "抬高配额之后不再触发暂停：计数是**累计**的（1 + 1 = 2 = 新 ceiling）"
    )
    state = derive_run_budget(session.events, paused.run_id)
    assert state.consumed.tool_calls_by_tool == {"echo": 2}
    assert state.consumed.tool_attempts_by_tool == {"echo": 2}
    assert state.consumed_turns == 3, (
        "恢复不重置任何 counter（`#314` 沿用 T4 的账）：暂停前 1 轮 + 续跑 2 轮"
    )


# ── `#315` T7：deadline 到点时的对账闸门 ────────────────────────────────


class _SlowReadOnlyTool(_SlowMutatingTool):
    """与超时的写工具同一形状，只把副作用类别改成只读（反例用）。"""

    @property
    def name(self) -> str:
        return "slow_read"

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.READ_ONLY


def _write_round(idx: int, tool_name: str = "slow_write") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"{TOOL_ID}_{idx}", "name": tool_name, "args": {}}],
    )


def _deadline_window(ahead_seconds: int = 30) -> tuple[datetime, datetime, datetime]:
    """（deadline，第一步的"现在"，第二步的"现在"）——三者都相对**真实**挂钟。

    为什么要锚在真实挂钟：执行域的 deadline 闸门读的是**它自己的**挂钟（生产接线的
    `utc_now()`，没有注入口），而 runtime 的暂停判定读的是本执行唯一入口 `_now`
    （`_clock` 可以钉住）。两者若不同源，用例会得到自相矛盾的现场：runtime 认为
    "还没到点"而执行域已经拒收工具调用。所以时间线这样排：

    - `deadline` 在真实现在之后 ⇒ 第一步的工具调用**被接纳并真的执行**（超时）；
    - 第二步的"现在"（由 `_clock` 钉住）在 `deadline` 之后 ⇒ 收口判到点。

    这不是"把时间调快"，而是把**同一瞬时**的两个判据放在各自能读到的位置上。
    """
    now = datetime.now(UTC)
    return now + timedelta(seconds=ahead_seconds), now, now + timedelta(
        seconds=ahead_seconds + 1
    )


@pytest.mark.asyncio
async def test_deadline_boundary_promotes_the_unproven_mutation(tmp_path) -> None:
    """到点时在途的 MUTATING 调用 ⇒ 暂停 + 该操作升 NEED_RECONCILE + continuation 点名。

    这是 `#315` 的主场景，一条时间线上要同时成立四件事：

    1. 工具**真的超时**（`timeout_seconds` 到点被掐断）⇒ 执行域把它留成 `UNKNOWN`
       并打上"副作用未证"标记（`07 §7`：write/edit 不确定 ⇒ NEED_RECONCILE）；
    2. 下一步正要开始模型决策时**已经到点** ⇒ 不再接纳任何新工作，按 deadline 暂停
       （`04 §9.1`：过了 deadline 不启动新的 Provider 请求 / ToolCall / 子 Agent）；
    3. 暂停收口时把那条未证的操作**提升到 NEED_RECONCILE** 并落
       `operation/reconcile-required`——到点之后世界状态可能已经变了，"不知道"
       必须落成 durable 事实（ADR-0044 D4：不得落一个暗示可安全续跑的暂停）；
    4. continuation 里**不能**再写"抬高 ceiling 后恢复"：那条路会被开工前的
       409「存在未 reconcile 的副作用」拒掉（`03 §5`：对账优先于恢复）。

    时刻按 `_deadline_window` 排：`deadline` 在真实现在之后（工具才会被接纳并真的
    超时），而收口那次判定的"现在"（`_clock` 钉住）已在其后——不是掐秒表。
    """
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    deadline, step_one, step_two = _deadline_window()
    scripted = ScriptedModel([_write_round(1), AIMessage(content="不该走到这里")])
    runtime = _runtime(
        scripted, ceiling=8, operation_ledger=ledger,
        tools=[_SlowMutatingTool()], deadline_at=deadline,
    )
    _clock(runtime, step_one, step_two)
    session = make_session(tmp_path)

    result = await runtime.run(session, "把配置改掉")

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["reason"] == REASON_DEADLINE
    assert paused.data["trigger_dimension"] == TRIGGER_RUN_DEADLINE
    assert paused.data["resume_requirements"] == [], (
        "预算 / deadline 暂停没有额外前置条件（`03 §3.4`）——欠账由账本与事件表达"
    )
    assert paused.data["closeout_source"] == CLOSEOUT_DETERMINISTIC, (
        "到点后连 closeout 都不发（它是真实 Provider 请求）⇒ 只能走确定性组装"
    )
    assert len(scripted.snapshots) == 1, "第二步一个模型请求都没有发"

    # 1 + 3：那条超时调用确实"未证"，并且在暂停收口时被提升
    operation = await ledger.get(session.session_id, f"{TOOL_ID}_1")
    assert operation is not None
    assert has_unproven_side_effect(operation) is True
    assert operation.state is OperationState.NEED_RECONCILE, (
        "RUNNING → UNKNOWN → NEED_RECONCILE 两步链由状态机强制（`07 §4`）"
    )

    reconcile_events = [
        e for e in session.events if e.type == OPERATION_RECONCILE_REQUIRED
    ]
    assert len(reconcile_events) == 1
    assert reconcile_events[0].data == {
        "tool_call_id": f"{TOOL_ID}_1",
        "tool_name": "slow_write",
        "args_identity": operation.args_identity,
        "state": OperationState.NEED_RECONCILE.value,
    }, "形状与 RecoveryCoordinator 落的**同一份**（客户端只认一种形状）"
    assert reconcile_events[0].run_id == paused.run_id
    # 顺序：先"某操作进入 NEED_RECONCILE"，再"本次执行在 deadline 上暂停"
    types = [e.type for e in session.events]
    assert types.index(OPERATION_RECONCILE_REQUIRED) < types.index(RUN_PAUSED)

    # 4：continuation 如实写出阻塞项，并换掉"抬高 ceiling 后恢复"那句动作
    continuation = paused.data["continuation"]
    assert len(continuation["blockers"]) == 2, (
        "一条写 deadline 到点，一条写未 reconcile 的副作用"
    )
    blocker = continuation["blockers"][1]
    assert "slow_write" in blocker and f"{TOOL_ID}_1" in blocker
    assert "NEED_RECONCILE" in blocker
    action = continuation[CONTINUATION_ACTION_KEY]
    assert "先 reconcile" in action
    assert "提高绝对 ceiling" not in action, (
        "在 reconcile 解除前恢复会被 409 拒——不能指一条走不通的路"
    )

    # 暂停仍是"可恢复的暂停"这一档（`#317` 的 stuck 面不变），但恢复会被账本闸门拦住
    assert [e.type for e in session.events if e.type in (RUN_COMPLETED, RUN_FAILED)] == []
    assert latest_paused_run(session.events) is not None


@pytest.mark.asyncio
async def test_a_read_only_timeout_at_the_deadline_does_not_claim_reconcile(tmp_path) -> None:
    """反例：到点时超时的是**只读**调用 ⇒ 不喊对账，也不升 NEED_RECONCILE。

    "副作用未证"只说 write/edit 这一类（`07 §7`）：只读调用超时没有"世界状态未知"
    的问题，执行域照常落 `FAILED`。若这里也升对账，`#315` 就会把每一次读操作超时
    都变成人工关卡——那是把闸门用坏，不是用严。
    """
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    deadline, step_one, step_two = _deadline_window()
    scripted = ScriptedModel(
        [_write_round(1, "slow_read"), AIMessage(content="不该走到这里")]
    )
    runtime = _runtime(
        scripted, ceiling=8, operation_ledger=ledger,
        tools=[_SlowReadOnlyTool()], deadline_at=deadline,
    )
    _clock(runtime, step_one, step_two)
    session = make_session(tmp_path)

    result = await runtime.run(session, "读一次慢配置")

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["reason"] == REASON_DEADLINE
    blockers = paused.data["continuation"]["blockers"]
    assert len(blockers) == 1 and blockers[0].startswith(
        f"{TRIGGER_RUN_DEADLINE} 已到点：deadline="
    ), "只有到点这一条阻塞项，没有凭空多出一条对账要求"
    operation = await ledger.get(session.session_id, f"{TOOL_ID}_1")
    assert operation is not None and operation.state is OperationState.FAILED
    assert has_unproven_side_effect(operation) is False
    assert [e for e in session.events if e.type == OPERATION_RECONCILE_REQUIRED] == []


@pytest.mark.asyncio
async def test_a_budget_pause_points_the_ledger_debt(tmp_path) -> None:
    """预算暂停的**账本面**：那一行升到 NEED_RECONCILE + 落一条对账事件。

    与下面那条端到端用例分工（两处的变异是不同的检查点，分开钉住才能分别证明）：
    这一条只看"账本有没有被点名"，不看 continuation 文案——所以它在"改写漏了"这种
    缺陷下仍然是绿的（那正是它存在的意义：把两种缺陷的失败面分开）。
    """
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    scripted = ScriptedModel([_write_round(1), _continuation_json()])
    runtime = _runtime(
        scripted, ceiling=2, operation_ledger=ledger, tools=[_SlowMutatingTool()],
    )
    session = make_session(tmp_path)

    result = await runtime.run(session, "把配置改掉")

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    operation = await ledger.get(session.session_id, f"{TOOL_ID}_1")
    assert operation is not None
    assert has_unproven_side_effect(operation) is True
    assert operation.state is OperationState.NEED_RECONCILE
    reconcile_events = [
        e for e in session.events if e.type == OPERATION_RECONCILE_REQUIRED
    ]
    assert len(reconcile_events) == 1 and reconcile_events[0].run_id == paused.run_id
    assert reconcile_events[0].data["tool_call_id"] == f"{TOOL_ID}_1"


@pytest.mark.asyncio
async def test_a_budget_pause_also_promotes_the_unproven_mutation(tmp_path) -> None:
    """对账闸门**不以暂停原因为条件**：预算暂停带着未证行时同样点名（`#315`）。

    为什么单列一条（2026-09-26 两轴审查的 P1，来源 = Correctness 轴）：闸门原先只长在
    deadline 边界（`if reason == deadline`），而**恢复闸门是 session 级的**——它读的是
    Ledger 上有没有未结清的行，不读暂停原因。于是"预算暂停 + 一条 MUTATING 超时留下的
    未证行"会落成一个暗示可安全续跑的暂停（载荷里写着"提高 ceiling 后恢复"），而那次
    恢复必被开工前的 409 挡死——正是 `03 §5` / ADR-0044 D4 禁止的形态。

    这条同时钉住**模型 closeout 那一支**：预算暂停的 closeout 有容量 ⇒ 走 `CLOSEOUT_MODEL`
    （deadline 那支没容量 ⇒ 恒走确定性 fallback，光看 deadline 用例看不出这里的缺口）。
    所以本用例既要求"账本被点名"（另一条用例单独钉），也要求"模型写的那句'继续第二步'
    被改写成先对账"。全程**不给 deadline**：这就是"与原因无关"的构造。
    """
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    scripted = ScriptedModel([_write_round(1), _continuation_json()])
    runtime = _runtime(
        scripted, ceiling=2, operation_ledger=ledger, tools=[_SlowMutatingTool()],
    )
    session = make_session(tmp_path)

    result = await runtime.run(session, "把配置改掉")

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["reason"] == REASON_BUDGET_EXHAUSTED, "这条停的是预算，不是 deadline"
    assert paused.data["trigger_dimension"] == TRIGGER_RUN_TURNS
    assert paused.data["closeout_source"] == CLOSEOUT_MODEL, (
        "有容量 ⇒ 模型给了 continuation ⇒ 走的正是那条曾经漏掉改写的分支"
    )

    operation = await ledger.get(session.session_id, f"{TOOL_ID}_1")
    assert operation is not None
    assert has_unproven_side_effect(operation) is True
    assert operation.state is OperationState.NEED_RECONCILE, (
        "RUNNING → UNKNOWN → NEED_RECONCILE 两步链由状态机强制（`07 §4`）"
    )
    reconcile_events = [
        e for e in session.events if e.type == OPERATION_RECONCILE_REQUIRED
    ]
    assert len(reconcile_events) == 1 and reconcile_events[0].run_id == paused.run_id
    types = [e.type for e in session.events]
    assert types.index(OPERATION_RECONCILE_REQUIRED) < types.index(RUN_PAUSED)

    continuation = paused.data["continuation"]
    assert len(continuation["blockers"]) == 1, (
        "模型自己写的 blockers 是空的（它认为没什么挡住继续），产品只**追加**已确证的"
        "那一条——不替模型编一条到顶说明（`02 §5.2`：不许伪造进展），也不把它的空列表"
        "当成'没有阻塞'"
    )
    assert continuation["completed"] == ["已完成第一步"], (
        "模型写的 completed 照旧：改写只碰 blockers 与 next_safe_action"
    )
    blocker = continuation["blockers"][0]
    assert "slow_write" in blocker and f"{TOOL_ID}_1" in blocker
    action = continuation[CONTINUATION_ACTION_KEY]
    assert "先 reconcile" in action
    assert "提高绝对 ceiling" not in action, (
        "在 reconcile 解除前那次恢复会被 409 拒——不能指一条走不通的路"
    )


@pytest.mark.asyncio
async def test_a_second_execution_does_not_duplicate_the_reconcile_event(tmp_path) -> None:
    """同一 run 再收口一次：`operation/reconcile-required` 只留一条（`#30` 的老规矩）。

    重复触发是正常时序（客户端重放、多次收口尝试），不是异常路径——账本行已在
    `NEED_RECONCILE` 就不再推进，事件已存在就不再追加，但 continuation 仍然如实
    写出阻塞项（"现在仍欠着"是每次收口都要说的话）。
    """
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    deadline, step_one, step_two = _deadline_window()
    first = _runtime(
        ScriptedModel([_write_round(1), AIMessage(content="不该走到这里")]),
        ceiling=8, operation_ledger=ledger, tools=[_SlowMutatingTool()],
        deadline_at=deadline,
    )
    _clock(first, step_one, step_two)
    session = make_session(tmp_path)
    await first.run(session, "把配置改掉")
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    reconcile_event = next(
        e for e in session.events if e.type == OPERATION_RECONCILE_REQUIRED
    )

    second = _runtime(
        ScriptedModel([AIMessage(content="不该走到这里")]), ceiling=8,
        consumed=paused.data["consumed"]["agent_turns"], run_id=paused.run_id,
        version=paused.data["budget_version"] + 1, operation_ledger=ledger,
        tools=[_SlowMutatingTool()],
        # 第二次执行一个工具都不会碰（第一步就判到点），所以 deadline 给一个
        # "相对第一次的现在也已过去"的时刻即可——判定只比它和 `_clock` 的现在
        deadline_at=step_two,
    )
    _clock(second, step_two)
    result = await second.run(session, None)

    assert result.status == STATUS_PAUSED
    events = [e for e in session.events if e.type == OPERATION_RECONCILE_REQUIRED]
    assert len(events) == 1, "已存在的事件不重复落（与 RecoveryCoordinator 同一条判据）"
    assert events[0].seq == reconcile_event.seq
    operation = await ledger.get(session.session_id, f"{TOOL_ID}_1")
    assert operation is not None and operation.state is OperationState.NEED_RECONCILE
    pauses = [e for e in session.events if e.type == RUN_PAUSED]
    assert len(pauses) == 2, "收口了两次就有两条 run/paused（各自一次执行）"
    assert "先 reconcile" in pauses[1].data["continuation"][CONTINUATION_ACTION_KEY]


class _FastWriteTool(_SlowMutatingTool):
    """MUTATING 但**很快完成**（timeout 给得足够宽）：到点时它的结果已知。"""

    @property
    def name(self) -> str:
        return "quick_write"

    @property
    def timeout_seconds(self) -> float:
        return 5.0

    async def execute(self, args: BaseModel) -> ToolResult:
        self.call_count += 1
        return ToolResult.success("写完了")


@pytest.mark.asyncio
async def test_a_known_in_flight_tool_is_recorded_before_the_deadline_pause(tmp_path) -> None:
    """在途的工具**完成**了（结果已知）⇒ 先落它的记录，再落暂停（AC 顺序要求）。

    "到点"不改变已经发生的事：那次调用被接纳过、执行过、有结论——它的 `tool/result`
    与 Ledger 终态必须在 `run/paused` **之前**落盘，否则暂停的收口快照会说"有个调用
    还悬着"，而真相是它已经完成（`07 §3` 的 Checkpoint 边界同理：先记事实，再收口）。
    已知结果的调用**不**产生对账债（`07 §7`：不确定才是 NEED_RECONCILE 的来源）。
    """
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    deadline, step_one, step_two = _deadline_window()
    scripted = ScriptedModel(
        [_write_round(1, "quick_write"), AIMessage(content="不该走到这里")]
    )
    runtime = _runtime(
        scripted, ceiling=8, operation_ledger=ledger,
        tools=[_FastWriteTool()], deadline_at=deadline,
    )
    _clock(runtime, step_one, step_two)
    session = make_session(tmp_path)

    result = await runtime.run(session, "快速写一次然后在到点处收口")

    assert result.status == STATUS_PAUSED
    types = [e.type for e in session.events]
    assert types.index(TOOL_RESULT) < types.index(RUN_PAUSED), (
        "已知结果先落盘，暂停后收口——顺序倒过来会得到一份自相矛盾的快照"
    )
    operation = await ledger.get(session.session_id, f"{TOOL_ID}_1")
    assert operation is not None and operation.state is OperationState.SUCCEEDED
    assert needs_reconcile_state(operation) is False
    assert OPERATION_RECONCILE_REQUIRED not in types, "已知结果不产生对账债"
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["reason"] == REASON_DEADLINE
    assert len(paused.data["continuation"]["blockers"]) == 1, "只有到点这一条"
    assert "提高绝对 ceiling" not in paused.data["continuation"][CONTINUATION_ACTION_KEY], (
        "没有对账债时动作仍是'给一个新的未来时刻'"
    )


@pytest.mark.asyncio
async def test_explicit_cancel_stays_terminal_and_is_not_rewritten_into_a_pause(
    tmp_path,
) -> None:
    """显式取消**不**被改写成 deadline 暂停（`03 §5` 明文，AC 要求可区分）。

    现场刻意选在"本执行的时钟已经到点"这一刻取消（`_clock` 的第二个值就是过去）：
    如果取消臂被 deadline 判定抢走，这里会出现 `run/paused(reason=deadline)` 且没有
    终结事件；实际契约相反——取消是**立即**的终态（`run/failed`，
    reason=cancelled），与暂停的"非终态收口"是两件事。

    取消的机制面（GeneratorExit / 断连的真实窗口）由
    `tests/agent/test_stream_disconnect_recovery.py` 覆盖；本用例只补"与 deadline
    暂停的区分"这一条。
    """
    deadline, step_one, step_two = _deadline_window()
    scripted = ScriptedModel(
        [AIMessage(content="", tool_calls=[
            {"id": f"{TOOL_ID}_1", "name": "quick_write", "args": {}},
        ])]
    )
    runtime = _runtime(
        scripted, ceiling=8, tools=[_FastWriteTool()], deadline_at=deadline,
    )
    _clock(runtime, step_one, step_two, step_two)
    session = make_session(tmp_path)

    agen = runtime.run_stream(session, "写一半就被取消")
    tool_call_seen = asyncio.Event()

    async def consume() -> None:
        async for frame in agen:
            if frame.type == TOOL_CALL:
                tool_call_seen.set()
                await asyncio.Event().wait()  # 挂住消费者：断连钉在这一帧之后

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(tool_call_seen.wait(), timeout=5.0)
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer
    await agen.aclose()  # 消费者消失 = producer generator 被关闭
    await asyncio.sleep(0)

    types = [e.type for e in session.events]
    assert RUN_PAUSED not in types, "取消是立即终态，绝不被改写成暂停"
    terminal = [e for e in session.events if e.type in (RUN_COMPLETED, RUN_FAILED)]
    assert [e.type for e in terminal] == [RUN_FAILED]
    assert terminal[-1].data.get("reason") == "cancelled"
    assert [e for e in session.events if e.type == OPERATION_RECONCILE_REQUIRED] == []
    assert latest_paused_run(session.events) is None
