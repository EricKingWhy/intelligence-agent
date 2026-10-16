"""#1264（Shape A）：循环内**有界再压**——不暂停、不引入新状态。

票面原状态机（暂停 → 后端再压 → 成功自动 `run/resumed`）已证伪：暂停臂会调
`arms.terminal.mark_terminal_written()`（runtime.py:3358/:3672），之后没有任何合法
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

from agent_harness.agent import AgentRuntime
from agent_harness.agent.run_budget import (
    REASON_BUDGET_EXHAUSTED,
    BudgetConsumed,
    LaunchRunBudget,
    RunLimits,
)
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_PAUSED
from agent_harness.context.builder import ProtectedFactBudgetExceededError
from agent_harness.context.compactor import (
    CompactionFailure,
    CompactionPostWriteError,
    ContextWindowExceededError,
)
from agent_harness.session import RUN_PAUSED, RUN_RESUMED, Session
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

TRIGGER = "max_context_tokens"


def _failure(error_class: str) -> CompactionFailure:
    return CompactionFailure(
        attempt=1,
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


def _runtime(context_builder: Any, model: Any | None = None) -> AgentRuntime:
    """一个执行实例：模型与 context 装配都按剧本走，账本不设 ceiling。

    `run_id=None` ⇒ 本执行新建 run（与 `_runtime` 在 test_run_pause_resume.py 的
    默认一致）；`max_agent_turns_total=None` ⇒ 不因回合预算抢在 context 之前暂停。
    """
    registry = ToolRegistry()
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
    )
    runtime._context_builder = context_builder
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
    """AC4：再压**成功**之后若又超限，不得再压第二次（同一次 run 至多一次）。"""
    builder = _ScriptedContextBuilder(
        [
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("timeout")],
            ),
            ContextWindowExceededError(
                "Summary validation failed and original context exceeds hard guard",
                failures=[_failure("timeout")],
            ),
        ],
    )
    runtime = _runtime(builder)
    session = make_session(tmp_path)

    result = await runtime.run(session, "反复超限")

    assert result.status == STATUS_PAUSED
    assert builder.compact_calls == 1, "闸门：同一次 run 至多再压一次"


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
