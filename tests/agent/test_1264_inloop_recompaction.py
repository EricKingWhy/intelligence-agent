"""#1264（Shape A）：循环内**有界再压**——不暂停、不引入新状态。

票面原状态机（暂停 → 后端再压 → 成功自动 `run/resumed`）已证伪：暂停臂会调
`arms.terminal.mark_terminal_written()`（runtime.py:3435/:3749），之后没有任何合法
写路径。本文件钉住替代语义：

1. 瞬时失败（timeout / transport_error …）⇒ **同一次 run 内**再压一次，成功即
   零操作续跑：run 从未终结，因此**没有** `run/paused`、**没有** `run/resumed`、
   没有 `resume_basis`（AC1 / AC2）。
2. 再压失败 ⇒ 落**既有**暂停收口（`trigger_dimension=max_context_tokens`）（AC3）。
3. 一次性闸门：同一次 run 至多再压一次，且结构性失败一次都不再压（AC4）。
4. 结构性失败（`summary_not_smaller` / `target_not_reached` / 保护事实 / 已落 bracket）
   直接走既有暂停，不触发再压（AC4）。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.agent.run_budget import (
    REASON_BUDGET_EXHAUSTED,
    BudgetConsumed,
    LaunchRunBudget,
    RunLimits,
)
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_FAILED, STATUS_PAUSED
from agent_harness.context.builder import ProtectedFactBudgetExceededError
from agent_harness.context.compactor import (
    CompactionFailure,
    CompactionPostWriteError,
    ContextWindowExceededError,
)
from agent_harness.session import RUN_PAUSED, RUN_RESUMED, Session
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

TRIGGER = "max_context_tokens"


class _EchoArgs(BaseModel):
    text: str = "ok"


class _EchoTool(Tool):
    """最小工具：让一轮以"还有活要干"收尾，run 才会进第二轮迭代。"""

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


class _ExplodingModel:
    """`ainvoke` / `astream` 都炸：把 run 逼进顶层失败兜底（决策未被接纳）。"""

    def bind_tools(self, _tools: list, **_kwargs: Any) -> _ExplodingModel:
        return self

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("模型炸了")

    async def astream(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("模型炸了")
        yield  # pragma: no cover —— 让本方法是 async generator


def _echo_call() -> dict[str, Any]:
    return {"name": "echo", "args": {"text": "ok"}, "id": "call-1"}


def _failure(error_class: str, attempt: int = 1) -> CompactionFailure:
    return CompactionFailure(
        attempt=attempt,
        error_class=error_class,
        message="摘要尝试失败",
        auto_limit=100,
        hard_limit=200,
        token_estimate=150,
        summary_model_id="summary-model",
        duration_ms=5,
        request_token_estimate=120,
        request_budget_tokens=200,
    )


class _ScriptedContextBuilder:
    """按剧本驱动的 ContextBuilder 替身：`build` 逐次给结果，`compact_now` 记账。

    真实 `ContextBuilder` 的阈值/投影/落盘在 `tests/context/` 已被钉死；本文件钉的
    是 **runtime 的决策**（何时再压、再压后往哪走），所以这里只保留决策相关的面。
    """

    def __init__(self, build_script: list[Any], *, compact_result: Any = None) -> None:
        self._build_script = list(build_script)
        self._compact_result = compact_result
        self.build_calls = 0
        self.compact_calls = 0
        self.protected_fact_token_budget = 1000

    async def build(self, _session: Session) -> list[Any]:
        self.build_calls += 1
        step = self._build_script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def compact_now(self, _session: Session, **_kwargs: Any) -> Any:
        self.compact_calls += 1
        if isinstance(self._compact_result, Exception):
            raise self._compact_result
        return self._compact_result


def _runtime(
    context_builder: Any, model: Any | None = None, session_budget: Any | None = None,
) -> AgentRuntime:
    """一个执行实例：模型与 context 装配都按剧本走，账本不设 ceiling。

    `run_id=None` ⇒ 本执行新建 run（与 `_runtime` 在 test_run_pause_resume.py 的
    默认一致）；`max_agent_turns_total=None` ⇒ 不因回合预算抢在 context 之前暂停。
    """
    registry = ToolRegistry()
    registry.register(_EchoTool())
    runtime = AgentRuntime(
        model=model or ScriptedModel([AIMessage(content="不应被调用")]),
        registry=registry,
        executor=ToolExecutor(registry),
        run_budget=LaunchRunBudget(
            version=1,
            limits=RunLimits(max_agent_turns_total=None, tool_call_limits={}),
            consumed=BudgetConsumed(agent_turns=0),
            run_id=None,
        ),
        context_builder=context_builder,
        session_budget=session_budget,
    )
    return runtime


@pytest.mark.asyncio
async def test_transient_failure_recompacts_once_and_continues_without_pause(tmp_path):
    """AC1 + AC2：瞬时失败 ⇒ 再压一次 ⇒ 零操作续跑（无暂停、无恢复事件）。"""
    builder = _ScriptedContextBuilder(
        [
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("timeout")],
            ),
            [AIMessage(content="压缩后续跑")],
        ],
    )
    runtime = _runtime(builder, ScriptedModel([AIMessage(content="压缩后续跑")]))
    session = make_session(tmp_path)

    result = await runtime.run(session, "先超限再续跑")

    assert result.status == STATUS_COMPLETED, "再压成功 ⇒ run 不终结，零操作续跑"
    assert builder.compact_calls == 1, "恰好再压一次"
    assert builder.build_calls == 2, "首轮失败 + 再压后重取投影"
    assert not [e for e in session.events if e.type == RUN_PAUSED], "续跑路径无暂停"
    assert not [e for e in session.events if e.type == RUN_RESUMED], "不是恢复：无 run/resumed"


@pytest.mark.asyncio
async def test_retry_failure_falls_back_to_the_existing_pause(tmp_path):
    """AC3：再压也失败 ⇒ 既有暂停收口（不新增状态、不新增 reason）。"""
    builder = _ScriptedContextBuilder(
        [
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("transport_error")],
            ),
        ],
        compact_result=ContextWindowExceededError(
            "Summary validation failed and original context exceeds hard guard",
            failures=[_failure("transport_error")],
        ),
    )
    runtime = _runtime(builder)
    session = make_session(tmp_path)

    result = await runtime.run(session, "压不动")

    assert result.status == STATUS_PAUSED
    assert builder.compact_calls == 1
    paused = next(e for e in session.events if e.type == RUN_PAUSED)
    assert paused.data["trigger_dimension"] == TRIGGER
    assert paused.data["reason"] == REASON_BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_benign_attempt_zero_diagnostic_does_not_veto_the_retry(tmp_path):
    """P1（双轴审查）：`attempt=0` 的**非摘要**诊断不得否决再压。

    真实形态：分块请求先追加一条 `chunk_bound_released`（`compactor.py` 在尝试
    循环**之前**追加，`attempt=0`），随后两次摘要尝试都死于 `timeout`。
    失败面 = [chunk_bound_released, timeout, timeout] —— 不筛 `attempt` 时
    `all()` 为假，本该续跑的一次再压被误判成结构性，用户白吃一个暂停框。
    """
    builder = _ScriptedContextBuilder(
        [
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[
                    _failure("chunk_bound_released", attempt=0),
                    _failure("timeout"),
                    _failure("timeout"),
                ],
            ),
            [AIMessage(content="压缩后续跑")],
        ],
    )
    runtime = _runtime(builder, ScriptedModel([AIMessage(content="压缩后续跑")]))
    session = make_session(tmp_path)

    result = await runtime.run(session, "分块 + 双超时")

    assert result.status == STATUS_COMPLETED, "瞬时失败仍应续跑，不被 attempt=0 诊断否决"
    assert builder.compact_calls == 1
    assert not [e for e in session.events if e.type == RUN_PAUSED]


@pytest.mark.asyncio
async def test_failure_surface_with_no_summary_attempt_is_not_retried(tmp_path):
    """反向：过滤后**一条摘要尝试都没有** ⇒ 无从判断抖动，按结构性处理（不重试）。"""
    builder = _ScriptedContextBuilder(
        [
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("chunk_bound_released", attempt=0)],
            ),
        ],
    )
    runtime = _runtime(builder)
    session = make_session(tmp_path)

    result = await runtime.run(session, "纯预检拒绝")

    assert result.status == STATUS_PAUSED
    assert builder.compact_calls == 0, "没有摘要尝试 ⇒ 再压必然同结果"


@pytest.mark.asyncio
async def test_structural_failure_is_not_retried(tmp_path):
    """AC4：结构性失败（摘要不比原文小）一次都不再压。"""
    builder = _ScriptedContextBuilder(
        [
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("summary_not_smaller")],
            ),
        ],
    )
    runtime = _runtime(builder)
    session = make_session(tmp_path)

    result = await runtime.run(session, "结构性别扭")

    assert result.status == STATUS_PAUSED
    assert builder.compact_calls == 0, "结构性失败不得再压"
    assert builder.build_calls == 1


@pytest.mark.asyncio
async def test_post_write_and_protected_facts_are_never_retried(tmp_path):
    """AC4：bracket 已落盘 / 保护事实超预算 ⇒ 不得再压（历史已变 / 与摘要无关）。"""
    for error in (
        CompactionPostWriteError("bracket 已落盘", bracket_id="b-1"),
        ProtectedFactBudgetExceededError(
            "保护事实超预算", budget_tokens=10, estimated_tokens=20, facts=[],
        ),
    ):
        builder = _ScriptedContextBuilder([error])
        runtime = _runtime(builder)
        session = make_session(tmp_path)

        result = await runtime.run(session, "不该重试")

        assert result.status == STATUS_PAUSED, type(error).__name__
        assert builder.compact_calls == 0, type(error).__name__


@pytest.mark.asyncio
async def test_the_gate_allows_at_most_one_retry_per_run(tmp_path):
    """AC4：再压**成功**之后若又超限，不得再压第二次（同一次 run 至多一次）。

    剧本必须让两次超限落在**两轮不同的迭代**：第一轮 build 失败 ⇒ 再压**成功** ⇒
    重取投影 ⇒ 跑完这轮模型调用（带工具调用，run 才会进第二轮）；第二轮 build 再
    失败 ⇒ 闸门已置位 ⇒ 直接暂停。
    （双轴审查 P2 修回：原先两条失败都写在同一次迭代的脚本里，第一次失败时
    `compact_now` 自身就抛，压根走不到第二次 build —— `compact_calls == 1` 恒真，
    删掉闸门该用例照样绿。现剧本把闸门钉死在"再压成功后又被超限"这个真实形态上。）
    """
    builder = _ScriptedContextBuilder(
        [
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("timeout")],
            ),
            [AIMessage(content="", tool_calls=[_echo_call()])],
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("timeout")],
            ),
        ],
    )
    runtime = _runtime(
        builder,
        ScriptedModel([
            AIMessage(content="", tool_calls=[_echo_call()]),
            AIMessage(content="不该被调用"),
        ]),
    )
    session = make_session(tmp_path)

    result = await runtime.run(session, "反复超限")

    assert result.status == STATUS_PAUSED, "第二轮超限时闸门已置位 ⇒ 不再压，落暂停"
    assert builder.compact_calls == 1, "闸门：同一次 run 至多再压一次"
    # 4 = 首轮 + 再压后重取 + 第二轮 + 暂停收口的 closeout 装配（最后那次撞上
    # 剧本耗尽，走既有确定性 continuation 回落——与闸门无关）。
    assert builder.build_calls == 4


@pytest.mark.asyncio
async def test_retry_success_keeps_the_session_step_reserved(tmp_path):
    """P2（双轴审查）：续跑臂把 `session_step_reserved` 置回 False —— **不退**预留。

    §10bis 的记账契约：再压成功 ⇒ 本轮继续、模型即将被调用 ⇒ 这一步真的用掉了，
    退款才是记假账。此前本文件所有用例都不传 `session_budget`，`#318` 整段记账在
    测试里是死代码（删掉 `runtime.py` 那行用例照样全绿）。

    观测点：模型调用**失败**收尾（决策未接纳）。若不置回 False，失败臂会走
    `refund_turn()` 把预留的 turn 退掉；置回后预留成为真账，`consumed.agent_turns`
    保持 1。这个差值就是那行的唯一可观测效果。
    """
    from agent_harness.agent.run_budget import SessionLimits
    from agent_harness.storage.delegation_tree import (
        InMemoryDelegationTreeLedger,
        SessionBudgetHandle,
    )

    handle = SessionBudgetHandle(
        InMemoryDelegationTreeLedger(), budget_key="root-s", root_session_id="root-s",
        limits=SessionLimits(),
    )
    builder = _ScriptedContextBuilder(
        [
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("timeout")],
            ),
            [AIMessage(content="压缩后续跑")],
        ],
    )
    runtime = _runtime(builder, _ExplodingModel(), session_budget=handle)
    session = make_session(tmp_path)

    result = await runtime.run(session, "再压成功后模型炸了")

    assert result.status == STATUS_FAILED
    assert builder.compact_calls == 1, "再压确实发生过（否则测的不是续跑臂）"
    snapshot = await handle.snapshot()
    assert snapshot.consumed.agent_turns == 1, "续跑臂不退预留：这一步真的用掉了"
    assert snapshot.consumed.model_requests == 1


@pytest.mark.asyncio
async def test_rebuild_failure_after_successful_compaction_still_pauses(tmp_path):
    """AC3：再压成功但重取投影仍超限 ⇒ 落既有暂停（不吞异常、不空跑模型）。"""
    builder = _ScriptedContextBuilder(
        [
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("timeout")],
            ),
            ContextWindowExceededError("Re-projected compaction still exceeds hard guard"),
        ],
    )
    runtime = _runtime(builder)
    session = make_session(tmp_path)

    result = await runtime.run(session, "压完还是超")

    assert result.status == STATUS_PAUSED
    assert builder.compact_calls == 1
