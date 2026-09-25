"""`#312` T4：RunBudget 账本与暂停/恢复数据面（纯函数，零 IO）。

判据来源（本文件不复述机制，只钉实现是否照抄）：`02 §5.1`（三层控制互不替代 /
counter 的计数点）、`02 §5.2`（预留 closeout、暂停不是完成也不是失败）、
`03 §3.4`（两个事件的字段与不变量）、`03 §5`（六值 Run 状态）、`11 §6.1`
（422 = 形状 / 409 = 状态对不上，且被拒请求零副作用）、ADR-0044 D2/D3/D9。

覆盖面：
- `derive_run_budget`：version / consumed / paused / terminal **全部**从 append-only
  事件算出（换一枚聚合、丢掉进程内存后仍然一样 ⇒ 恢复不放大权限）；
- 三个准入判定的边界（严格不等号 + 预留 1 轮），含"ceiling 恰好等于 consumed+1
  必须被拒"——否则客户端会拿到"恢复成功但什么都没发生"的假象；
- `validate_resume` 的 422/409 分界；
- continuation：模型产出只做规范化不补齐；确定性兜底只用持久化事实（不许把
  "工具调用已发出"说成"工具已成功"）。
"""

from __future__ import annotations

import pytest

from agent_harness.agent.budget import BudgetConflict, BudgetRejection, LocalFuse
from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    CLOSEOUT_MODEL,
    CONTINUATION_ACTION_KEY,
    REASON_BUDGET_EXHAUSTED,
    RESERVED_CLOSEOUT_TURNS,
    RESUME_BASIS_BUDGET_INCREASE,
    RESUME_BASIS_RELEVANT_STEER,
    TRIGGER_LOCAL_TURNS,
    TRIGGER_RUN_TURNS,
    LaunchRunBudget,
    RunTurnLimits,
    as_run_started_budget,
    build_limits_snapshot,
    build_pause_data,
    build_resume_data,
    closeout_capacity,
    derive_run_budget,
    deterministic_continuation,
    latest_paused_run,
    latest_run_id,
    normalize_continuation,
    pause_trigger,
    resume_ceiling_ok,
    validate_resume,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    SessionEvent,
)

RUN_ID = "run-1"
OTHER_RUN = "run-2"
FUSE = LocalFuse(max_agent_turns=500, source="deployment")


# ── 事件工厂：只造派生的输入，不造真相 ────────────────────────────────────


def _ev(seq: int, type_: str, *, run_id: str | None = RUN_ID, **data: object) -> SessionEvent:
    return SessionEvent(seq=seq, type=type_, session_id="s1", run_id=run_id, data=dict(data))


def _started(seq: int = 1, *, run_id: str = RUN_ID, ceiling: int | None = 3) -> SessionEvent:
    data: dict[str, object] = {}
    if ceiling is not None:
        data["budget"] = {"run": {"max_agent_turns_total": ceiling}}
    return _ev(seq, RUN_STARTED, run_id=run_id, **data)


def _paused(
    seq: int, *, run_id: str = RUN_ID, consumed: int = 2, version: int = 1,
    ceiling: int | None = 3, closeout: str = CLOSEOUT_DETERMINISTIC,
    continuation: dict | None = None,
) -> SessionEvent:
    return _ev(
        seq, RUN_PAUSED, run_id=run_id,
        **build_pause_data(
            reason=REASON_BUDGET_EXHAUSTED,
            trigger_dimension=TRIGGER_RUN_TURNS,
            version=version,
            consumed_turns=consumed,
            limits=build_limits_snapshot(
                run_limits=RunTurnLimits(max_agent_turns_total=ceiling), local_fuse=FUSE,
            ),
            continuation=continuation or deterministic_continuation(
                events=[], run_id=run_id, trigger_dimension=TRIGGER_RUN_TURNS,
                ceiling=ceiling, consumed_turns=consumed,
            ),
            closeout_source=closeout,
        ),
    )


def _resumed(seq: int, *, run_id: str = RUN_ID, consumed: int = 2, version: int = 2,
             ceiling: int | None = 4) -> SessionEvent:
    return _ev(
        seq, RUN_RESUMED, run_id=run_id,
        **build_resume_data(
            from_pause_seq=seq - 1, previous_version=version - 1, version=version,
            limits=build_limits_snapshot(
                run_limits=RunTurnLimits(max_agent_turns_total=ceiling), local_fuse=FUSE,
            ),
            consumed_turns=consumed,
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        ),
    )


# ── derive_run_budget：账本只有一份真相 ──────────────────────────────────


def test_version_starts_at_one_and_increments_per_resume() -> None:
    state = derive_run_budget([_started(), _ev(2, MODEL_COMPLETED)], RUN_ID)
    assert state.version == 1

    resumed = derive_run_budget(
        [_started(), _ev(2, MODEL_COMPLETED), _paused(3), _resumed(4)], RUN_ID,
    )
    assert resumed.version == 2
    assert resumed.paused is None, "run/resumed 之后不再处于暂停"


def test_consumed_counts_accepted_decisions_and_a_model_closeout() -> None:
    """计数点是"被接纳的模型决策"（`model/completed`）+ **模型** closeout 那一次。

    `model/failed`（拒绝或传输失败）不计数——它不是一个被接纳的决策。
    """
    events = [
        _started(),
        _ev(2, MODEL_COMPLETED),
        _ev(3, "model/failed"),
        _ev(4, MODEL_COMPLETED),
        _paused(5, consumed=2, closeout=CLOSEOUT_MODEL),
        _resumed(6, consumed=3),
        _ev(7, MODEL_COMPLETED),
    ]
    state = derive_run_budget(events, RUN_ID)

    # 2 次普通决策 + 1 次模型 closeout + 恢复后 1 次 = 4
    assert state.consumed_turns == 4


def test_derivation_ignores_other_runs() -> None:
    events = [_started(), _ev(2, MODEL_COMPLETED), _started(3, run_id=OTHER_RUN),
              _ev(4, MODEL_COMPLETED, run_id=OTHER_RUN)]

    assert derive_run_budget(events, OTHER_RUN).consumed_turns == 1
    assert derive_run_budget(events, RUN_ID).consumed_turns == 1


def test_paused_snapshot_is_read_from_the_event_not_recomputed() -> None:
    """`consumed` 快照以事件为准（含 closeout 那一轮），不是"重数一遍事件"。

    暂停事件是**当时**的账；之后的恢复/新决策只影响 state.consumed_turns。
    """
    events = [_started(), _ev(2, MODEL_COMPLETED), _paused(3, consumed=7, closeout=CLOSEOUT_MODEL)]
    state = derive_run_budget(events, RUN_ID)

    assert state.paused is not None
    assert state.paused.consumed_turns == 7
    assert state.consumed_turns == 2, "state 的累计仍按事件重算（快照只描述那一刻）"


@pytest.mark.parametrize("terminal", [RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED])
def test_any_terminal_makes_the_run_not_resumable(terminal: str) -> None:
    events = [_started(), _paused(2), _ev(3, terminal)]

    state = derive_run_budget(events, RUN_ID)

    assert state.terminal is True
    assert state.resumable is False


def test_paused_projection_carries_identity_version_limits_remaining_and_continuation() -> None:
    """`11 §6.1` 的 run 作用域投影：identity / version / ceiling / consumed /
    remaining / 暂停原因 + continuation 引用。"""
    events = [_started(), _ev(2, MODEL_COMPLETED), _ev(3, MODEL_COMPLETED), _paused(4)]
    state = derive_run_budget(events, RUN_ID)

    assert state.paused is not None
    projection = state.paused.as_projection()
    assert projection["run_id"] == RUN_ID
    assert projection["state"] == "paused"
    assert projection["version"] == 1
    assert projection["reason"] == REASON_BUDGET_EXHAUSTED
    assert projection["limits"] == {"max_agent_turns_total": 3}
    assert projection["consumed"] == {"agent_turns": 2}
    assert projection["remaining"] == 1
    assert projection["local_fuse"] == {"max_agent_turns": 500, "source": "deployment"}
    assert set(projection["continuation"]) >= {
        "completed", "remaining", "blockers", CONTINUATION_ACTION_KEY,
    }
    assert projection["resume_requirements"] == []


def test_remaining_is_null_without_a_ceiling_never_zero() -> None:
    """无 ceiling ⇒ `remaining=null`（`11 §6.1`：缺席不是 0）。"""
    events = [_started(ceiling=None), _paused(2, ceiling=None, consumed=1)]
    state = derive_run_budget(events, RUN_ID)

    assert state.paused is not None
    assert state.paused.as_projection()["remaining"] is None


def test_resume_limit_snapshot_replaces_the_ceiling() -> None:
    """恢复提供的是**绝对** ceiling：生效值以最后一次 `run/resumed` 为准。"""
    events = [_started(ceiling=3), _paused(2), _resumed(3, ceiling=9)]
    state = derive_run_budget(events, RUN_ID)

    assert state.limits.max_agent_turns_total == 9


def test_latest_run_id_and_latest_paused_run_only_consider_the_latest_run() -> None:
    """更早的暂停 run 已被后续 run 取代 ⇒ 不可再恢复（否则等于跳回历史）。"""
    events = [
        _started(1), _ev(2, MODEL_COMPLETED), _paused(3, consumed=1),
        _started(4, run_id=OTHER_RUN), _ev(5, MODEL_COMPLETED, run_id=OTHER_RUN),
    ]

    assert latest_run_id(events) == OTHER_RUN
    assert latest_paused_run(events) is None

    # 反过来：最新 run 正暂停 ⇒ 能拿到它（且是它自己的 run_id）
    assert latest_paused_run([_started(1), _paused(2, consumed=1)]).run_id == RUN_ID


# ── 准入判定：边界、预留与两个作用域 ─────────────────────────────────────


def test_pause_trigger_fires_one_turn_before_the_ceiling() -> None:
    """判定含预留 turn：`consumed + RESERVED >= ceiling`。

    于是暂停时**还剩**一次 closeout 容量（`02 §5.2` 的预留），第 ceiling 轮不会
    变成"既想 closeout 又没有容量"。
    """
    limits = RunTurnLimits(max_agent_turns_total=3)

    assert pause_trigger(
        consumed_turns=1, run_limits=limits, execution_steps=1, local_fuse_turns=500,
    ) is None
    assert pause_trigger(
        consumed_turns=2, run_limits=limits, execution_steps=2, local_fuse_turns=500,
    ) == TRIGGER_RUN_TURNS


def test_pause_trigger_prefers_the_run_scope() -> None:
    """两个作用域同时命中时优先报 run ceiling——它是客户端配的那个、可恢复的动作是抬高它。"""
    assert pause_trigger(
        consumed_turns=2, run_limits=RunTurnLimits(max_agent_turns_total=3),
        execution_steps=2, local_fuse_turns=3,
    ) == TRIGGER_RUN_TURNS


def test_local_fuse_fires_with_its_own_counter() -> None:
    """local fuse 是**实例级**保险丝：用本执行的步数计（续跑执行拿到的是新实例）。"""
    unlimited = RunTurnLimits(max_agent_turns_total=None)

    # 判定含预留：0+1 >= 2 不成立 ⇒ 还能跑；1+1 >= 2 成立 ⇒ 停
    assert pause_trigger(
        consumed_turns=99, run_limits=unlimited, execution_steps=0, local_fuse_turns=2,
    ) is None
    assert pause_trigger(
        consumed_turns=500, run_limits=unlimited, execution_steps=1, local_fuse_turns=2,
    ) == TRIGGER_LOCAL_TURNS


def test_closeout_capacity_needs_the_ceiling_to_be_strictly_above_consumed() -> None:
    assert closeout_capacity(
        consumed_turns=2, run_limits=RunTurnLimits(max_agent_turns_total=3),
    ) is True
    assert closeout_capacity(
        consumed_turns=3, run_limits=RunTurnLimits(max_agent_turns_total=3),
    ) is False
    assert closeout_capacity(
        consumed_turns=3, run_limits=RunTurnLimits(max_agent_turns_total=None),
    ) is True, "无 run ceiling ⇒ 预留容量由 local fuse 决定（这里视为有）"


def test_resume_must_leave_room_for_a_turn_plus_the_reserved_closeout() -> None:
    """`ceiling == consumed + 1` 必须被拒：它会在下一次准入立刻再次暂停（假恢复）。"""
    assert resume_ceiling_ok(consumed_turns=2, ceiling=3) is False
    assert resume_ceiling_ok(consumed_turns=2, ceiling=4) is True
    assert resume_ceiling_ok(consumed_turns=2, ceiling=None) is False


def test_reserved_closeout_turns_is_one() -> None:
    """预算是"容量"不是豁免：常量是 1，且它**算在** ceiling 之内（`02 §5.2`）。"""
    assert RESERVED_CLOSEOUT_TURNS == 1


# ── validate_resume：422（形状）/ 409（状态对不上）───────────────────────


def _paused_run(consumed: int = 2, version: int = 1):
    """造一个**真实派生**的暂停事实：`version` 由事件决定（=1 + run/resumed 条数）。"""
    events: list[SessionEvent] = [_started()]
    for _ in range(version - 1):
        events.append(_paused(len(events) + 1, consumed=consumed))
        events.append(_resumed(len(events) + 1, consumed=consumed))
    events.append(_paused(len(events) + 1, consumed=consumed, version=version))
    paused = derive_run_budget(events, RUN_ID).paused
    assert paused is not None
    assert paused.version == version, "用例前提：派生的 version 必须等于声明值"
    return paused


def test_missing_declarations_are_shape_errors() -> None:
    paused = _paused_run()
    for kwargs in (
        {"run_id": None, "expected_version": 1, "ceiling": 4, "resume_basis": RESUME_BASIS_BUDGET_INCREASE},
        {"run_id": RUN_ID, "expected_version": None, "ceiling": 4, "resume_basis": RESUME_BASIS_BUDGET_INCREASE},
        {"run_id": RUN_ID, "expected_version": 1, "ceiling": 4, "resume_basis": None},
        {"run_id": RUN_ID, "expected_version": 1, "ceiling": 4, "resume_basis": "unknown_basis"},
    ):
        with pytest.raises(BudgetRejection):
            validate_resume(paused, **kwargs)


def test_wrong_run_id_is_a_state_conflict() -> None:
    with pytest.raises(BudgetConflict):
        validate_resume(
            _paused_run(), run_id=OTHER_RUN, expected_version=1, ceiling=4,
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        )


def test_stale_version_is_a_state_conflict() -> None:
    with pytest.raises(BudgetConflict) as excinfo:
        validate_resume(
            _paused_run(version=2), run_id=RUN_ID, expected_version=1, ceiling=4,
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        )
    assert "version" in str(excinfo.value)


def test_lowering_or_keeping_the_ceiling_is_a_state_conflict() -> None:
    """绝对 ceiling 必须**真提高**（不能降低、也不能不动）。"""
    for ceiling in (None, 1, 2, 3):
        with pytest.raises(BudgetConflict):
            validate_resume(
                _paused_run(consumed=2), run_id=RUN_ID, expected_version=1,
                ceiling=ceiling, resume_basis=RESUME_BASIS_BUDGET_INCREASE,
            )


def test_only_budget_increase_is_accepted_in_this_ticket() -> None:
    """其余三值的**证据判定**属 `#317`：现在收下它们等于假装校验过证据 ⇒ 409。"""
    with pytest.raises(BudgetConflict):
        validate_resume(
            _paused_run(), run_id=RUN_ID, expected_version=1, ceiling=4,
            resume_basis=RESUME_BASIS_RELEVANT_STEER,
        )


def test_valid_resume_passes() -> None:
    validate_resume(
        _paused_run(), run_id=RUN_ID, expected_version=1, ceiling=4,
        resume_basis=RESUME_BASIS_BUDGET_INCREASE,
    )


# ── continuation：模型产出只规范化；兜底只用持久化事实 ────────────────────


def test_normalize_continuation_requires_all_four_keys() -> None:
    good = {
        "completed": ["已跑完 X"],
        "remaining": ["还剩 Y"],
        "blockers": [],
        "next_safe_action": "继续 Y",
    }
    assert normalize_continuation(good) == good
    for key in ("completed", "remaining", "blockers", CONTINUATION_ACTION_KEY):
        broken = {k: v for k, v in good.items() if k != key}
        assert normalize_continuation(broken) is None, f"缺 {key} 必须判无效（不补齐）"
    assert normalize_continuation({"completed": [], "remaining": [], "blockers": [],
                                   "next_safe_action": "  "}) is None


def test_normalize_continuation_bounds_size() -> None:
    """有界：单条文本截断、条数封顶（一次 closeout 不得把事件流撑大）。"""
    normalized = normalize_continuation({
        "completed": ["x" * 900] * 40,
        "remaining": [],
        "blockers": [],
        "next_safe_action": "y" * 900,
    })
    assert normalized is not None
    assert len(normalized["completed"]) == 20
    assert len(normalized["completed"][0]) == 500
    assert len(normalized[CONTINUATION_ACTION_KEY]) == 500


def test_deterministic_continuation_only_claims_persisted_facts() -> None:
    """只用持久化事实：计数 + ceiling + "停在下一轮模型决策之前"。

    **不许**把"工具调用已发出"说成"工具已成功"，也不编造待办清单。
    """
    events = [
        _started(ceiling=3),
        _ev(2, MODEL_COMPLETED),
        _ev(3, "tool/call", tool_call_id="c1"),
        _ev(4, "tool/result", tool_call_id="c1"),
    ]
    continuation = deterministic_continuation(
        events=events, run_id=RUN_ID, trigger_dimension=TRIGGER_RUN_TURNS,
        ceiling=3, consumed_turns=2,
    )

    assert set(continuation) == {"completed", "remaining", "blockers", CONTINUATION_ACTION_KEY}
    blob = " ".join(
        [*continuation["completed"], *continuation["remaining"], *continuation["blockers"],
         continuation[CONTINUATION_ACTION_KEY]],
    )
    assert "consumed=2" in blob
    assert "ceiling=3" in blob
    assert TRIGGER_RUN_TURNS in blob
    assert "成功" not in blob, "兜底不得声称工具成功（工具结果与调用分开计数）"


# ── 数据面：run/started 的 budget 与启动上下文 ───────────────────────────


def test_run_started_budget_key_is_omitted_without_a_ceiling() -> None:
    """不落键 = "本次 run 没有 run 作用域 ceiling"（缺省请求的事件序列逐字不变）。"""
    assert as_run_started_budget(RunTurnLimits()) is None
    assert as_run_started_budget(RunTurnLimits(max_agent_turns_total=7)) == {
        "run": {"max_agent_turns_total": 7},
    }


def test_launch_context_defaults_to_a_new_run() -> None:
    """`run_id=None` ⇒ 新逻辑 run（runtime 自己 begin_run）；非 None ⇒ 续跑同一 run。"""
    fresh = LaunchRunBudget()
    assert (fresh.run_id, fresh.version, fresh.consumed_turns) == (None, 1, 0)
    resuming = LaunchRunBudget(
        version=2, limits=RunTurnLimits(max_agent_turns_total=9), consumed_turns=3,
        run_id=RUN_ID, turn_index=4,
    )
    assert (resuming.run_id, resuming.version, resuming.consumed_turns) == (RUN_ID, 2, 3)
    assert resuming.turn_index == 4
