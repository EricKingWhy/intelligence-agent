"""W-22（#366）：`client_absent` 的 runtime 层——循环顶准入闸门与确定性收口。

判据来源：`02 §5.2.1`（阻止新的 Model/Tool/Child 接纳；在途 Tool 按 Ledger 收口；
暂停时不得再发"总结用"模型请求）、`03 §3.4`（恰好一条 run/paused，
closeout_source=deterministic）、`11 §6.2`（宽限内不发新模型步骤属 W-12 的注册协议，
本文件钉 W-22 的闸门本体）、ADR-0046。

与 `test_client_absent_contract.py` 的分工：那边钉纯函数契约值；这里钉**真实事件流**
（真 Session + 真 Store + ScriptedModel）上闸门的位置与收口形状。未登记的 run
（CLI / 旧 Web）必须逐字保持 W-22 之前的行为——惰性闸门就是那条边界。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.agent.run_budget import (
    LaunchRunBudget,
    RunLimits,
    derive_run_budget,
    project_budget,
)
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_PAUSED
from agent_harness.model.accounting import (
    HARNESS_MODEL_ACCOUNTING,
    PROVIDER_ROLE_CLOSEOUT,
)
from agent_harness.session import (
    MODEL_REQUEST,
    OPERATION_RECONCILE_REQUIRED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_PAUSED,
    RUN_STARTED,
    TOOL_RESULT,
)
from agent_harness.storage import (
    OperationState,
    SqliteOperationLedger,
    has_unproven_side_effect,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel
from tests.tooling.test_deadline_admission import _SlowMutatingTool


class _TextArgs(BaseModel):
    text: str = "x"


class _CommittingTool(Tool):
    """模拟「Tool 正在提交时最后客户端退出」：execute 中段把在场闸门置为缺席。

    工具本身**照常成功返回**（在途 Tool 按现有路径收口，不盲重试、不丢弃结果）；
    缺席事实只影响**下一次**循环顶准入（`02 §5.2.1`：稳定边界暂停）。"""

    def __init__(self, holder: list[AgentRuntime]) -> None:
        self._holder = holder

    @property
    def name(self) -> str:
        return "commit"

    @property
    def description(self) -> str:
        return "提交并在中段标记客户端缺席。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _TextArgs

    async def execute(self, args: _TextArgs) -> ToolResult:
        self._holder[0].client_presence.mark_absent()
        return ToolResult.success(message=args.text)


def _runtime(
    model, *, tools: Sequence[Tool] = (), ceiling: int | None = None,
    operation_ledger: SqliteOperationLedger | None = None,
) -> AgentRuntime:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentRuntime(
        model=model, registry=registry,
        executor=ToolExecutor(registry, operation_ledger=operation_ledger),
        max_agent_turns=5,
        run_budget=LaunchRunBudget(limits=RunLimits(max_agent_turns_total=ceiling)),
    )


def _tool_round(name: str = "commit") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": "call_w22", "name": name, "args": {"text": "t1"}}],
    )


@pytest.mark.asyncio
async def test_absence_during_tool_commit_pauses_at_next_admission_point(tmp_path) -> None:
    """「最后客户端退出时 Tool 正在提交」：工具照常收口（结果落盘、不盲重试），
    下一次循环顶准入被挡 ⇒ 恰好一条 run/paused(client_absent)，且**没有**为收口
    发过任何模型请求（closeout_source=deterministic，`02 §5.2.1`）。"""
    scripted = ScriptedModel([_tool_round(), AIMessage(content="never reached")])
    holder: list[AgentRuntime] = []
    runtime = _runtime(scripted, tools=[_CommittingTool(holder)])
    holder.append(runtime)
    runtime.client_presence.enroll()
    session = make_session(tmp_path)

    result = await runtime.run(session, "提交途中客户端离开")

    assert result.status == STATUS_PAUSED
    paused_events = [e for e in session.events if e.type == RUN_PAUSED]
    assert len(paused_events) == 1, "恰好一条 run/paused（不双写）"
    data = paused_events[0].data
    assert data["reason"] == "client_absent"
    assert data["trigger_dimension"] == "client_presence"
    assert data["closeout_source"] == "deterministic"
    assert data["resume_requirements"] == []
    assert "client_return" in data["continuation"]["next_safe_action"]
    # 在途工具按现有路径收口：结果落了、只落一次（不盲重试），账如实计数。
    assert sum(1 for e in session.events if e.type == TOOL_RESULT) == 1
    assert data["consumed"]["tool_calls"] == 1
    assert data["consumed"]["tool_attempts"] == 1
    assert data["consumed"]["agent_turns"] == 1
    # 暂停时不得再发"总结用"模型请求：模型只被调用过 1 次（产出轮），零 closeout。
    assert len(scripted.snapshots) == 1
    assert not any(
        e.type == MODEL_REQUEST and e.data.get("role") == PROVIDER_ROLE_CLOSEOUT
        for e in session.events
    )
    # 暂停不是终态（02 §5.2 明文）。
    assert not [
        e for e in session.events
        if e.type in (RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED)
    ]


@pytest.mark.asyncio
async def test_absence_before_first_step_admits_zero_model_calls(tmp_path) -> None:
    """AC「离开后新请求数 0」的最强形态：run 启动前就已缺席 ⇒ 循环顶第一次准入
    即被挡，模型调用数为 **0**（steer / ContextBuilder / 模型都在闸门之后）。"""
    scripted = ScriptedModel([AIMessage(content="never reached")])
    runtime = _runtime(scripted)
    runtime.client_presence.enroll()
    runtime.client_presence.mark_absent()
    session = make_session(tmp_path)

    result = await runtime.run(session, "客户端不在场")

    assert result.status == STATUS_PAUSED
    assert len(scripted.snapshots) == 0, "缺席后不得接纳任何新模型步骤"
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["reason"] == "client_absent"
    assert paused.data["consumed"]["agent_turns"] == 0
    assert not [
        e for e in session.events
        if e.type in (RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED)
    ]


@pytest.mark.asyncio
async def test_unenrolled_runtime_is_inert_and_completes_normally(tmp_path) -> None:
    """未登记（CLI / 旧 Web，无产品 presence 注册）⇒ 闸门恒惰性：即使被误置缺席
    也不改变行为，run 照常跑到终态——W-22 之前的语义逐字保留（`11 §6.2`：
    协议只接管明确登记的 Task）。"""
    scripted = ScriptedModel([AIMessage(content="done")])
    runtime = _runtime(scripted)
    runtime.client_presence.mark_absent()  # 未 enroll：惰性，不生效
    session = make_session(tmp_path)

    result = await runtime.run(session, "普通 run")

    assert result.status == STATUS_COMPLETED
    assert [e.type for e in session.events if e.type == RUN_PAUSED] == []
    assert [e.type for e in session.events if e.type == RUN_COMPLETED]


@pytest.mark.asyncio
async def test_budget_trigger_is_not_masked_by_presence(tmp_path) -> None:
    """同一准入点两个事实同时成立时，先报**账本**事实（budget_exhausted）：
    预算到顶是更硬的停止原因，client_absent 不遮蔽它（判定顺序 = 既有预算判定
    在前、在场闸门在后；两个维度不可互相替代，`02 §5.1` 同一方向）。"""
    scripted = ScriptedModel([AIMessage(content="never reached")])
    runtime = _runtime(scripted, ceiling=1)
    runtime.client_presence.enroll()
    runtime.client_presence.mark_absent()
    session = make_session(tmp_path)

    result = await runtime.run(session, "两个触发同时成立")

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["reason"] == "budget_exhausted"
    assert paused.data["trigger_dimension"] == "run.max_agent_turns_total"


class _LeavingSlowWrite(_SlowMutatingTool):
    """「最后客户端退出时 Tool 正在提交」的慢写工具：execute 开头置缺席闸门，
    随后照旧被自身 `timeout_seconds` 掐断 ⇒ MUTATING 超时 = 副作用未证（UNKNOWN，
    `07 §7`）。缺席与超时在同一次提交里叠加，正是 AC 要钉的那个现场。"""

    def __init__(self, holder: list[AgentRuntime]) -> None:
        super().__init__()
        self._holder = holder

    async def execute(self, args):
        self._holder[0].client_presence.mark_absent()
        return await super().execute(args)


@pytest.mark.asyncio
async def test_unproven_mutation_takes_priority_over_client_absent_pause(tmp_path) -> None:
    """AC「UNKNOWN 先 NEED_RECONCILE」：提交中超时的 MUTATING 调用（副作用未证）
    遇上客户端缺席 ⇒ 对账闸门**无条件**先于暂停收口跑（`02 §5.2.1`：
    UNKNOWN / NEED_RECONCILE 优先于 paused），continuation 不得暗示可安全续跑，
    投影把状态词覆盖成 needs_reconcile（原因照旧可读，`03 §5`）。"""
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    holder: list[AgentRuntime] = []
    scripted = ScriptedModel([
        AIMessage(
            content="",
            tool_calls=[{"id": "call_w22_unk", "name": "slow_write", "args": {}}],
        ),
        AIMessage(content="不该走到这里"),
    ])
    runtime = _runtime(
        scripted, tools=[_LeavingSlowWrite(holder)], operation_ledger=ledger,
    )
    holder.append(runtime)
    runtime.client_presence.enroll()
    session = make_session(tmp_path)

    result = await runtime.run(session, "改配置，客户端中途离开")

    assert result.status == STATUS_PAUSED
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["reason"] == "client_absent"
    assert paused.data["closeout_source"] == "deterministic"
    assert len(scripted.snapshots) == 1, "离开后零模型请求（收口也不发）"

    # 未证行升 NEED_RECONCILE（RUNNING → UNKNOWN → NEED_RECONCILE，`07 §4`）
    operation = await ledger.get(session.session_id, "call_w22_unk")
    assert operation is not None
    assert has_unproven_side_effect(operation) is True
    assert operation.state is OperationState.NEED_RECONCILE

    reconcile_events = [
        e for e in session.events if e.type == OPERATION_RECONCILE_REQUIRED
    ]
    assert len(reconcile_events) == 1
    assert reconcile_events[0].run_id == paused.run_id
    types = [e.type for e in session.events]
    assert types.index(OPERATION_RECONCILE_REQUIRED) < types.index(RUN_PAUSED), (
        "顺序：先'某操作进入 NEED_RECONCILE'，再'本次执行暂停'"
    )

    # continuation 被 blocked_by 改写：不再指"客户端回归后恢复"，先指对账
    continuation = paused.data["continuation"]
    action = continuation["next_safe_action"]
    assert action.startswith("先 reconcile")
    assert "client_return" not in action, "对账未结清前不得指恢复那条路（必被 409）"
    assert any("NEED_RECONCILE" in b for b in continuation["blockers"])

    # 投影：needs_reconcile 覆盖 paused（覆盖而非替换——reason 照旧可读）
    run_id = next(
        e.run_id for e in session.events if e.type == RUN_STARTED and e.run_id
    )
    state = derive_run_budget(session.events, run_id)
    projection = project_budget(
        state, accounting=HARNESS_MODEL_ACCOUNTING,
        reconcile_pending=["call_w22_unk"],
    )
    assert projection["state"] == "needs_reconcile"
    assert projection["reason"] == "client_absent"
