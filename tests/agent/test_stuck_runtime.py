"""`#317` T9：stuck 检测在真实 AgentRuntime 循环里的接线（ADR-0048 D5/D6）。

判据来源：`02 §5.2`（暂停是**非终态**、closeout 在适用预算内预留容量）、`02 §5.3`
（五模式与"恰好一次纠正"）、`02 §5.4`（完成闸门优先）、`03 §3.4`（`run/paused` /
`run/resumed` 的形状）、`03 §5`（paused 可恢复）。

本文件补的是三条腿里最靠外的一条：
- `test_stuck_detection.py` 证明**判定**（五阈值、指纹、进展、重启重建）；
- `tests/agent/test_run_budget.py` 证明**载荷与恢复判据**（纯函数）；
- 这里证明**接线**：检测器给出的信号真的变成"一条纠正 + 一次暂停"，暂停走的是既有
  暂停臂（closeout / 单终态 / 投影），且完成闸门赢过任何 stuck 判定。

刻意**不**在这里重测阈值（那是检测器的事）：本文件的失败剧本只负责把计数喂到阈值。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.agent.guards import (
    STUCK_LEVEL_PAUSED,
    STUCK_LEVEL_REPLAN,
    STUCK_PATTERN_PROJECT,
    STUCK_PATTERN_TOOL_FAILURE,
    STUCK_THRESHOLDS,
    StuckDetector,
    worst_stuck_signal,
)
from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    CONTINUATION_ACTION_KEY,
    REASON_STUCK,
    STUCK_RESUME_REQUIREMENTS,
    derive_run_budget,
    latest_paused_run,
)
from agent_harness.agent.types import STATUS_COMPLETED, STATUS_PAUSED
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session import (
    GUARD_STUCK,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    TOOL_FAILURE_GUARD,
    USER_MESSAGE,
    Session,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _Args(BaseModel):
    command: str = Field(default="x", description="要执行的命令")


class _AlwaysFailsTool(Tool):
    """确定性失败的工具——Live Gate 场景③在单测里的替身（真模型那半在 Live Gate）。"""

    @property
    def name(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "总是以同样的方式失败。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _Args

    async def execute(self, args: _Args) -> ToolResult:
        return ToolResult.failure(
            message=f"命令 {args.command!r} 失败", error_code="TOOL_EXECUTION_ERROR",
        )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_AlwaysFailsTool())
    return registry


def _runtime(model, *, max_agent_turns: int = 30) -> AgentRuntime:
    registry = _registry()
    return AgentRuntime(
        model=model, registry=registry, executor=ToolExecutor(registry),
        max_agent_turns=max_agent_turns,
    )


def _round(index: int, *, command: str = "ls") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{
            "id": f"call_{index:04d}", "name": "fail", "args": {"command": command},
        }],
    )


def _continuation_json() -> AIMessage:
    return AIMessage(content=json.dumps({
        "completed": ["已试过同一个命令"],
        "remaining": ["还没拿到结果"],
        "blockers": ["同一条命令反复失败"],
        "next_safe_action": "换一条路",
    }, ensure_ascii=False))


def _events(session: Session, event_type: str) -> list:
    return [event for event in session.events if event.type == event_type]


def _run_id_of(session: Session) -> str:
    return next(
        event.run_id for event in session.events
        if event.type == RUN_STARTED and event.run_id
    )


# ── ① 的接线：一条纠正 + 一次暂停 ─────────────────────────────────────────


class TestFailureLoopBecomesAPause:
    @pytest.mark.asyncio
    async def test_failure_loop_gets_one_correction_then_pauses(self, tmp_path) -> None:
        """同动作同错误失败 6 次：第 3 次纠正，第 6 次 `run/paused(reason=stuck)`。"""
        threshold = STUCK_THRESHOLDS[STUCK_PATTERN_TOOL_FAILURE]
        scripted = ScriptedModel(
            [_round(index) for index in range(threshold * 2)] + [_continuation_json()]
        )
        session = make_session(tmp_path)

        result = await _runtime(scripted).run(session, "反复试同一个失败命令")

        # 终态是**暂停**而不是失败：stuck 可恢复（`02 §5.2`）
        assert result.status == STATUS_PAUSED
        assert _events(session, RUN_FAILED) == []

        guard_events = _events(session, TOOL_FAILURE_GUARD)
        assert [(event.data["level"], event.data["consecutive_failures"])
                for event in guard_events] == [("soft", threshold)]

        stuck_events = _events(session, GUARD_STUCK)
        assert [(event.data["level"], event.data["pattern"], event.data["count"],
                 event.data["replan_count"]) for event in stuck_events] == [
            (STUCK_LEVEL_PAUSED, STUCK_PATTERN_TOOL_FAILURE, threshold * 2, 1),
        ]

        # 纠正消息**恰好一条**（`02 §5.3`）：并发的 ⑤（连续决策无进展）在同一段历史里
        # 也会到首达阈值，但全局闩只放行一条——否则模型两轮内收到两条同义纠正。
        correctives = [
            event for event in _events(session, USER_MESSAGE)
            if event.data.get("injected_by") in ("tool_failure_guard", "stuck_guard")
        ]
        assert len(correctives) == 1
        assert correctives[0].data["injected_by"] == "tool_failure_guard"
        # ① 的纠正文案沿用 ADR-0014 的既有片段（逐字不变）
        assert correctives[0].data["content"] == DEFAULT_REGISTRY.assemble(
            "corrective:tool_failure_guard", {"tool_name": "fail", "consecutive_failures": "3"},
        ).fragment_text

        paused = latest_paused_run(session.events)
        assert paused is not None and paused.reason == REASON_STUCK
        assert paused.trigger_dimension == STUCK_PATTERN_TOOL_FAILURE
        assert paused.resume_requirements == STUCK_RESUME_REQUIREMENTS

    @pytest.mark.asyncio
    async def test_the_pause_payload_carries_the_evidence_snapshot(self, tmp_path) -> None:
        """`run/paused.stuck` 七格齐全（`03 §3.4`；ADR-0048 D6）。"""
        scripted = ScriptedModel([_round(index) for index in range(6)] + [_continuation_json()])
        session = make_session(tmp_path)

        await _runtime(scripted).run(session, "反复试同一个失败命令")

        pause_event = _events(session, RUN_PAUSED)[0]
        stuck = pause_event.data["stuck"]
        assert set(stuck) == {
            "pattern", "threshold", "count", "replan_count", "fingerprint",
            "environment_revision", "policy_version",
        }
        assert stuck["pattern"] == STUCK_PATTERN_TOOL_FAILURE
        assert stuck["threshold"] == 3 and stuck["count"] == 6
        assert stuck["fingerprint"].startswith("sha256:")
        # 未注入证据端口 ⇒ 快照两格为 None（fail-closed：恢复侧那两条依据届时按
        # "无快照可比"拒绝，而不是放行）——这里如实断言"没端口就没快照"。
        assert stuck["environment_revision"] is None
        assert stuck["policy_version"] is None
        assert pause_event.data["resume_requirements"] == list(STUCK_RESUME_REQUIREMENTS)

    @pytest.mark.asyncio
    async def test_the_continuation_never_hints_at_raising_a_ceiling(self, tmp_path) -> None:
        """暂停语义换了，续跑说明也必须跟着换（ADR-0048 D6；`03 §5` 禁"暗示可续跑"）。"""
        # 剧本只给 6 轮工具调用：closeout 那次模型调用没得可发 ⇒ 回落确定性组装
        scripted = ScriptedModel([_round(index) for index in range(6)])
        session = make_session(tmp_path)

        await _runtime(scripted).run(session, "反复试同一个失败命令")

        pause_event = _events(session, RUN_PAUSED)[0]
        assert pause_event.data["closeout_source"] == CLOSEOUT_DETERMINISTIC
        continuation = pause_event.data["continuation"]
        text = json.dumps(continuation, ensure_ascii=False)
        assert "ceiling" not in text
        assert "预算" not in text
        # 确定性 continuation 给的动作与三类依据逐条对应
        action = continuation[CONTINUATION_ACTION_KEY]
        for basis in STUCK_RESUME_REQUIREMENTS:
            assert basis in action

    @pytest.mark.asyncio
    async def test_a_model_closeout_that_claims_a_budget_fix_is_still_not_trusted(
        self, tmp_path,
    ) -> None:
        """模型自己写的 closeout 也要过同一份契约（键集 / 类型），不合就回落。"""
        scripted = ScriptedModel(
            [_round(index) for index in range(6)]
            + [AIMessage(content="不是 JSON"), _continuation_json()]
        )
        session = make_session(tmp_path)

        await _runtime(scripted).run(session, "反复试同一个失败命令")

        pause_event = _events(session, RUN_PAUSED)[0]
        continuation = pause_event.data["continuation"]
        assert set(continuation) == {
            "completed", "remaining", "blockers", CONTINUATION_ACTION_KEY,
        }

    @pytest.mark.asyncio
    async def test_counts_survive_a_restart_of_the_same_run(self, tmp_path) -> None:
        """同 run 续跑后计数接着走：第 6 次仍然暂停（ADR-0048 D1 的端到端证据）。

        路径刻意用"预算暂停 → 抬高 ceiling → 同 run 续跑"来制造一次**执行边界**：
        检测器不持有跨执行的记忆，全部由事件重放重建——第二次执行在同一个 `run_id`
        上、没有任何共享的进程内状态，却接着数到 6。
        """
        from agent_harness.agent.run_budget import LaunchRunBudget, RunLimits

        registry = _registry()
        # 执行一：ceiling=6 ⇒ 接纳 5 轮之后暂停（预留 closeout 那一轮）。5 次同错失败
        # 让 ① 的计数停在 5（差一格到 2T）。
        first = AgentRuntime(
            model=ScriptedModel([_round(index) for index in range(5)]),
            registry=registry, executor=ToolExecutor(registry), max_agent_turns=30,
            run_budget=LaunchRunBudget(
                version=1,
                limits=RunLimits(max_agent_turns_total=6),
            ),
        )
        session = make_session(tmp_path)
        await first.run(session, "反复试同一个失败命令")

        paused = latest_paused_run(session.events)
        assert paused is not None and paused.stuck is None, "这次是预算暂停，不是 stuck"

        # 执行二：同一个 run、抬高 ceiling、消耗沿用快照——再失败一次就够 2T
        resumed = AgentRuntime(
            model=ScriptedModel([_round(5), _continuation_json()]),
            registry=registry, executor=ToolExecutor(registry), max_agent_turns=30,
            run_budget=LaunchRunBudget(
                version=paused.version + 1,
                limits=RunLimits(max_agent_turns_total=30),
                consumed=paused.consumed, run_id=paused.run_id,
            ),
        )
        result = await resumed.run(session, None)

        assert result.status == STATUS_PAUSED
        stuck_pause = latest_paused_run(session.events)
        assert stuck_pause is not None and stuck_pause.reason == REASON_STUCK
        assert stuck_pause.run_id == paused.run_id
        assert stuck_pause.stuck["count"] == 6
        assert stuck_pause.stuck["threshold"] == 3
        # run 身份单一：整段历史里只有一个 run/started
        assert len(_events(session, RUN_STARTED)) == 1


# ── ②–⑤ 的接线：新的结构化事件 + 新片段 ────────────────────────────────


class TestNonToolFailurePatternsAtRuntime:
    @pytest.mark.asyncio
    async def test_a_project_level_stall_emits_the_new_guard_event(self, tmp_path) -> None:
        """⑤（连续决策无进展）在 runtime 里落 `guard/stuck(level=replan)` + 新纠正片段。

        场景：每轮都是一个**不同**的失败动作（① 的"连续同动作"因此不成立），于是到
        第 4 个决策时只剩 ⑤ 在累积；纠正之后继续同一种做法，第 8 个决策处暂停。
        """
        scripted = ScriptedModel(
            [_round(index, command=f"try{index}") for index in range(8)]
            + [_continuation_json()]
        )
        session = make_session(tmp_path)

        result = await _runtime(scripted).run(session, "每轮换个命令试")

        stuck_events = _events(session, GUARD_STUCK)
        assert (STUCK_LEVEL_REPLAN, STUCK_PATTERN_PROJECT, 4) in [
            (event.data["level"], event.data["pattern"], event.data["count"])
            for event in stuck_events
        ]
        correctives = [
            event for event in _events(session, USER_MESSAGE)
            if event.data.get("injected_by") == "stuck_guard"
        ]
        assert len(correctives) == 1
        assert correctives[0].data["content"] == DEFAULT_REGISTRY.assemble(
            "corrective:stuck_pattern",
            {"pattern_label": "连续多轮没有任何可以被证实的进展", "pattern_count": "4"},
        ).fragment_text
        # 纠正之后循环继续 ⇒ 第 8 个决策处的暂停仍然到来
        assert result.status == STATUS_PAUSED


# ── 完成闸门优先（ADR-0048 D5 / `02 §5.4`） ────────────────────────────────


class TestCompletionGateWins:
    @pytest.mark.asyncio
    async def test_a_model_that_wraps_up_is_completed_even_at_the_pause_point(
        self, tmp_path,
    ) -> None:
        """第 8 个决策既"够暂停"又"能完成"时：完成赢（护栏不得越过完成闸门）。

        前 7 轮是各不相同的失败动作（无进展 ⇒ ⑤ 的窗口走到 7），第 8 个决策不调用工具、
        给出可被接受的最终答复 ⇒ `run/completed`。若护栏在完成闸门**之前**判，这里会
        变成 `run/paused`——那正是 D5 明文禁止的"模型已经给出合格答复却被一个计数推翻"。
        """
        scripted = ScriptedModel(
            [_round(index, command=f"try{index}") for index in range(7)]
            + [AIMessage(content="试完了，结论如下：这条路走不通。")]
        )
        session = make_session(tmp_path)

        result = await _runtime(scripted).run(session, "试到走不通为止")

        assert result.status == STATUS_COMPLETED
        assert _events(session, RUN_COMPLETED) != []
        assert latest_paused_run(session.events) is None
        assert [
            event for event in _events(session, GUARD_STUCK)
            if event.data["level"] == STUCK_LEVEL_PAUSED
        ] == []
        # 反证：同一段历史喂给一个全新的检测器，确实会得到"该暂停"的判定——
        # 所以上面那条 completed 不是"计数没到"，而是完成闸门赢了。
        run_id = _run_id_of(session)
        replayed = worst_stuck_signal(
            StuckDetector(run_id=run_id).advance(session.events)
        )
        assert replayed is not None
        assert replayed.level == STUCK_LEVEL_PAUSED
        assert replayed.pattern == STUCK_PATTERN_PROJECT

    @pytest.mark.asyncio
    async def test_the_projection_of_a_stuck_pause_is_rebuildable(self, tmp_path) -> None:
        """投影可重建（`11 §6.1`）：重启后从事件读出的暂停事实逐格一致。"""
        scripted = ScriptedModel([_round(index) for index in range(6)] + [_continuation_json()])
        session = make_session(tmp_path)
        await _runtime(scripted).run(session, "反复试同一个失败命令")

        first = derive_run_budget(session.events, _run_id_of(session))
        again = derive_run_budget(session.events, _run_id_of(session))
        assert first.paused is not None
        assert first.paused.as_projection() == again.paused.as_projection()
        assert first.paused.as_projection()["stuck"]["count"] == 6


# ── 与既有语义的边界 ──────────────────────────────────────────────────────


class TestBoundaries:
    @pytest.mark.asyncio
    async def test_quota_rejections_still_do_not_trip_the_guard(self, tmp_path) -> None:
        """准入前被拒（配额）不喂护栏这条语义在搬家后逐字保留（`#314`）。"""
        from agent_harness.agent.run_budget import LaunchRunBudget, RunLimits

        registry = _registry()
        runtime = AgentRuntime(
            model=ScriptedModel([_round(index) for index in range(3)]),
            registry=registry, executor=ToolExecutor(registry),
            max_agent_turns=30,
            run_budget=LaunchRunBudget(
                version=1, limits=RunLimits(tool_call_limits={"fail": 0}),
            ),
        )
        session = make_session(tmp_path)

        await runtime.run(session, "配额为 0 的工具")

        assert _events(session, TOOL_FAILURE_GUARD) == []
        assert _events(session, GUARD_STUCK) == []
        assert latest_paused_run(session.events) is not None  # 而是暂停（可恢复）

    @pytest.mark.asyncio
    async def test_a_successful_run_is_untouched(self, tmp_path) -> None:
        """正常跑完的 run 一个护栏事件都不该有（五模式全部静默）。"""
        registry = ToolRegistry()

        class _Ok(BaseModel):
            text: str = "x"

        class _OkTool(Tool):
            @property
            def name(self) -> str:
                return "ok"

            @property
            def description(self) -> str:
                return "总是成功。"

            @property
            def args_schema(self) -> type[BaseModel]:
                return _Ok

            async def execute(self, args: _Ok) -> ToolResult:
                return ToolResult.success(message=args.text)

        registry.register(_OkTool())
        model = ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "id": "call_1", "name": "ok", "args": {"text": str(index)},
            }])
            for index in range(3)
        ] + [AIMessage(content="做完了")])
        session = make_session(tmp_path)

        result = await AgentRuntime(
            model=model, registry=registry, executor=ToolExecutor(registry),
            max_agent_turns=30,
        ).run(session, "三个不同的成功调用")

        assert result.status == STATUS_COMPLETED
        assert _events(session, TOOL_FAILURE_GUARD) == []
        assert _events(session, GUARD_STUCK) == []
        assert _events(session, RUN_PAUSED) == []
        assert _events(session, RUN_RESUMED) == []
