"""`#312` T4 建账本 / `#313` T5 扩到四维：RunBudget 账本与暂停/恢复数据面（纯函数，零 IO）。

判据来源（本文件不复述机制，只钉实现是否照抄）：`02 §5.1`（三层控制互不替代 /
七个 counter 的**唯一**计数点）、`02 §5.2`（预留 closeout、暂停不是完成也不是失败）、
`03 §3.4`（两个事件的字段与不变量）、`03 §5`（六值 Run 状态）、`11 §6.1`
（422 = 形状 / 409 = 状态对不上，且被拒请求零副作用；不可得 = unavailable ≠ 0）、
ADR-0044 D2/D3/D4/D9。

覆盖面：

- `derive_run_budget`：version / consumed / paused / terminal **全部**从 append-only
  事件算出（换一枚聚合、丢掉进程内存后仍然一样 ⇒ 恢复不放大权限）；
- 四维计数：`model/completed` 数 `agent_turns`，`model/request` 数 `model_requests`
  并累加自报的 usage / cost——**任一请求缺报 ⇒ 该维度未知（`None`）**，不是 0；
- 准入判定的边界（turns/requests 预留 1 次 closeout；tokens/cost 到线即停；
  含"ceiling 恰好等于 consumed + 预留 必须被拒"——否则客户端会拿到"恢复成功但什么都
  没发生"的假象）；
- `validate_ceiling_enforceability` / `parse_cost_ceiling` / `run_limits_from_request`：
  422 的三类形状判定；
- `validate_resume` 的 422/409 分界（含"消耗基数未知 ⇒ 409"与"一个 ceiling 都不给 ⇒ 409"）；
- `project_budget`：四维投影与可执行性；
- continuation：模型产出只做规范化不补齐；确定性兜底只用持久化事实（不许把
  "工具调用已发出"说成"工具已成功"，也不许把"未知"写成 0）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent_harness.agent.budget import BudgetConflict, BudgetRejection, LocalFuse
from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    CLOSEOUT_MODEL,
    CONTINUATION_ACTION_KEY,
    REASON_BUDGET_EXHAUSTED,
    RESERVED_CLOSEOUT_REQUESTS,
    RESERVED_CLOSEOUT_TURNS,
    RESUME_BASIS_BUDGET_INCREASE,
    RESUME_BASIS_RELEVANT_STEER,
    TRIGGER_LOCAL_TURNS,
    TRIGGER_RUN_COST,
    TRIGGER_RUN_REQUESTS,
    TRIGGER_RUN_TOKENS,
    TRIGGER_RUN_TURNS,
    BudgetConsumed,
    LaunchRunBudget,
    RunBudgetState,
    RunLimits,
    add_consumed,
    as_run_started_budget,
    build_limits_snapshot,
    build_pause_data,
    build_resume_data,
    closeout_capacity,
    consumed_from_events,
    derive_run_budget,
    deterministic_continuation,
    latest_paused_run,
    latest_run_id,
    normalize_continuation,
    parse_cost_ceiling,
    pause_trigger,
    project_budget,
    resume_headroom_ok,
    run_limits_from_request,
    validate_ceiling_enforceability,
    validate_resume,
)
from agent_harness.model.accounting import (
    PROVIDER_ROLE_CLOSEOUT,
    PROVIDER_ROLE_FALLBACK,
    PROVIDER_ROLE_PRIMARY,
    REQUEST_OUTCOME_COMPLETED,
    REQUEST_OUTCOME_FAILED,
    HARNESS_MODEL_ACCOUNTING,
    ProviderAccounting,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_REQUEST,
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

#: 一个"能报 usage、报不了 cost"的链（= 当前生产集成的能力声明）。
USAGE_ONLY = ProviderAccounting(reports_usage=True, reports_cost=False)
#: 两样都报（测试替身；显式 cost ceiling 只在这条路上合法）。
FULL = ProviderAccounting(reports_usage=True, reports_cost=True)


# ── 事件工厂：只造派生的输入，不造真相 ────────────────────────────────────


def _ev(seq: int, type_: str, *, run_id: str | None = RUN_ID, **data: object) -> SessionEvent:
    return SessionEvent(seq=seq, type=type_, session_id="s1", run_id=run_id, data=dict(data))


def _started(seq: int = 1, *, run_id: str = RUN_ID, ceiling: int | None = 3) -> SessionEvent:
    data: dict[str, object] = {}
    if ceiling is not None:
        data["budget"] = {"run": {"max_agent_turns_total": ceiling}}
    return _ev(seq, RUN_STARTED, run_id=run_id, **data)


def _request(
    seq: int, *, run_id: str = RUN_ID, role: str = PROVIDER_ROLE_PRIMARY,
    outcome: str = REQUEST_OUTCOME_COMPLETED, tokens: int | None = None,
    cost: str | None = None,
) -> SessionEvent:
    """一条 `model/request`（`model_requests` / token / cost 三维的唯一计数点）。"""
    data: dict[str, object] = {"role": role, "outcome": outcome}
    if tokens is not None:
        data["usage"] = {"total_tokens": tokens}
    if cost is not None:
        data["cost_usd"] = cost
    return _ev(seq, MODEL_REQUEST, run_id=run_id, **data)


def _paused(
    seq: int, *, run_id: str = RUN_ID, consumed: int = 2, version: int = 1,
    ceiling: int | None = 3, closeout: str = CLOSEOUT_DETERMINISTIC,
    continuation: dict | None = None, snapshot: BudgetConsumed | None = None,
    limits: RunLimits | None = None,
) -> SessionEvent:
    effective_limits = limits if limits is not None else RunLimits(
        max_agent_turns_total=ceiling,
    )
    effective_consumed = snapshot if snapshot is not None else BudgetConsumed(
        agent_turns=consumed,
    )
    return _ev(
        seq, RUN_PAUSED, run_id=run_id,
        **build_pause_data(
            reason=REASON_BUDGET_EXHAUSTED,
            trigger_dimension=TRIGGER_RUN_TURNS,
            version=version,
            consumed=effective_consumed,
            limits=build_limits_snapshot(run_limits=effective_limits, local_fuse=FUSE),
            continuation=continuation or deterministic_continuation(
                events=[], run_id=run_id, trigger_dimension=TRIGGER_RUN_TURNS,
                limits=effective_limits, consumed=effective_consumed,
            ),
            closeout_source=closeout,
        ),
    )


def _resumed(
    seq: int, *, run_id: str = RUN_ID, consumed: int = 2, version: int = 2,
    ceiling: int | None = 4, snapshot: BudgetConsumed | None = None,
) -> SessionEvent:
    return _ev(
        seq, RUN_RESUMED, run_id=run_id,
        **build_resume_data(
            from_pause_seq=seq - 1, previous_version=version - 1, version=version,
            limits=build_limits_snapshot(
                run_limits=RunLimits(max_agent_turns_total=ceiling), local_fuse=FUSE,
            ),
            consumed=snapshot if snapshot is not None else BudgetConsumed(agent_turns=consumed),
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


def test_consumed_counts_accepted_decisions_only() -> None:
    """`agent_turns` 的计数点是"被**接纳进 loop** 的模型决策"（`model/completed`）。

    `model/failed`（拒绝或传输失败）不计数；**模型 closeout 那一次也不计数** —— 它是
    `model_requests`（`02 §5.1` 明文并列 primary / fallback / closeout / 子 Agent），
    记进 `agent_turns` 就是该节 AC 禁止的"计数混同"。
    """
    events = [
        _started(),
        _ev(2, MODEL_COMPLETED),
        _ev(3, "model/failed"),
        _ev(4, MODEL_COMPLETED),
        _paused(5, consumed=2, closeout=CLOSEOUT_MODEL),
        _resumed(6, consumed=2),
        _ev(7, MODEL_COMPLETED),
    ]
    state = derive_run_budget(events, RUN_ID)

    # 2 次普通决策 + 恢复后 1 次 = 3（closeout 不进这个数）
    assert state.consumed_turns == 3


def test_model_requests_count_every_actual_provider_call() -> None:
    """`model_requests` 数**每一次实际请求**：primary / fallback / closeout 各一次，
    失败与被拒的也在内（`02 §5.1`：它们不增 `agent_turns`，但确实发生过请求）。"""
    events = [
        _started(),
        # 一次决策 = primary 失败 + fallback 成功 ⇒ 两次请求、一次轮次
        _request(2, outcome=REQUEST_OUTCOME_FAILED),
        _request(3, role=PROVIDER_ROLE_FALLBACK),
        _ev(4, MODEL_COMPLETED),
        # 暂停前的 closeout 调用
        _request(5, role=PROVIDER_ROLE_CLOSEOUT),
        _paused(6, consumed=2, closeout=CLOSEOUT_MODEL),
    ]
    state = derive_run_budget(events, RUN_ID)

    assert state.consumed.agent_turns == 1
    assert state.consumed.model_requests == 3


def test_tokens_and_cost_come_only_from_provider_reported_values() -> None:
    """token / cost 只累加**自报**值；全都自报时与逐次相加相等。"""
    events = [
        _started(),
        _request(2, tokens=100, cost="0.0010"),
        _request(3, role=PROVIDER_ROLE_FALLBACK, tokens=250, cost="0.0025"),
    ]
    consumed = consumed_from_events(events)

    assert consumed.total_tokens == 350
    assert consumed.cost_usd == Decimal("0.0035")


def test_a_single_unreported_dimension_makes_the_total_unknown_not_zero() -> None:
    """**不可得 ≠ 0**（`11 §6.1`）：任一请求没自报该维度 ⇒ 该维度累计是"未知"。

    未知会**粘住**（后面的加数再精确也补不回已经不知道的那一部分）——这正是"到线即停"
    在未知基数上无法成立的原因，也是 `validate_resume` 拒绝"基数未知 + 新 ceiling"的依据。
    """
    events = [
        _started(),
        _request(2, tokens=100, cost="0.0010"),
        _request(3, tokens=None, cost=None),  # Provider 没报
        _request(4, tokens=40, cost="0.0004"),
    ]
    consumed = consumed_from_events(events)

    assert consumed.model_requests == 3
    assert consumed.total_tokens is None, "缺一次自报 ⇒ 未知（**不是** 140、更不是 0）"
    assert consumed.cost_usd is None


def test_zero_requests_is_a_legitimate_empty_sum_of_zero() -> None:
    """空和真的是 0（"一个请求都没有"与"有请求但不知道花了多少"是两件事）。"""
    consumed = consumed_from_events([_started(), _ev(2, MODEL_COMPLETED)])

    assert consumed.model_requests == 0
    assert consumed.total_tokens == 0
    assert consumed.cost_usd == Decimal(0)


def test_add_consumed_is_none_sticky_on_both_sides() -> None:
    """合成（启动时的账 + 本次执行新发生的账）也守同一条纪律：任一未知 ⇒ 和未知。"""
    known = BudgetConsumed(agent_turns=1, model_requests=2, total_tokens=10,
                           cost_usd=Decimal("0.1"))
    unknown = BudgetConsumed(agent_turns=1, model_requests=None, total_tokens=None,
                             cost_usd=None)

    summed = add_consumed(known, unknown)
    assert summed.agent_turns == 2
    assert (summed.model_requests, summed.total_tokens, summed.cost_usd) == (None, None, None)
    assert add_consumed(known, known).cost_usd == Decimal("0.2")


def test_closeout_source_is_provenance_not_a_counter() -> None:
    """同一个暂停点，`closeout_source` 换值不改变 `consumed`（它不是计数点）。"""
    events = [
        _started(),
        _ev(2, MODEL_COMPLETED),
        _paused(3, consumed=2, closeout=CLOSEOUT_MODEL),
    ]
    with_model = derive_run_budget(events, RUN_ID)
    with_deterministic = derive_run_budget(
        [_started(), _ev(2, MODEL_COMPLETED), _paused(3, consumed=2,
                                                       closeout=CLOSEOUT_DETERMINISTIC)],
        RUN_ID,
    )

    assert with_model.consumed_turns == with_deterministic.consumed_turns == 1
    assert with_model.paused is not None and with_deterministic.paused is not None
    assert with_model.paused.closeout_source == CLOSEOUT_MODEL
    assert with_deterministic.paused.closeout_source == CLOSEOUT_DETERMINISTIC


def test_derivation_ignores_other_runs() -> None:
    events = [_started(), _ev(2, MODEL_COMPLETED), _started(3, run_id=OTHER_RUN),
              _ev(4, MODEL_COMPLETED, run_id=OTHER_RUN)]

    assert derive_run_budget(events, OTHER_RUN).consumed_turns == 1
    assert derive_run_budget(events, RUN_ID).consumed_turns == 1


def test_paused_snapshot_is_read_from_the_event_not_recomputed() -> None:
    """`consumed` 快照以事件为准，不是"重数一遍事件"。

    暂停事件是**当时**的账；之后的恢复/新决策只影响 state.consumed。
    """
    events = [_started(), _ev(2, MODEL_COMPLETED), _paused(3, consumed=7, closeout=CLOSEOUT_MODEL)]
    state = derive_run_budget(events, RUN_ID)

    assert state.paused is not None
    assert state.paused.consumed_turns == 7
    assert state.consumed_turns == 1, "state 的累计仍按事件重算（快照只描述那一刻）"


def test_pre_t5_pause_snapshot_leaves_the_new_dimensions_unknown() -> None:
    """T5 之前的暂停快照只有 `agent_turns` 一个键。

    turn 在那时候就有明确定义 ⇒ 可以按事件重算（准确）；而 requests / tokens / cost
    那时**根本没有计数点**，重算得到的 0 是一句假话（那些 run 确实发过请求）⇒ 必须是
    **未知**。未知会让该维度的新 ceiling 在恢复时被拒（`validate_resume` 的 409）。
    """
    legacy = _ev(
        3, RUN_PAUSED,
        run_id=RUN_ID,
        reason=REASON_BUDGET_EXHAUSTED,
        trigger_dimension=TRIGGER_RUN_TURNS,
        budget_version=1,
        consumed={"agent_turns": 2},
        limits={"local": FUSE.as_projection(),
                "run": {"max_agent_turns_total": 3}},
        continuation=normalize_continuation({
            "completed": [], "remaining": [], "blockers": [],
            "next_safe_action": "提高 ceiling",
        }),
        closeout_source=CLOSEOUT_DETERMINISTIC,
        resume_requirements=[],
    )
    state = derive_run_budget([_started(), _ev(2, MODEL_COMPLETED), legacy], RUN_ID)

    assert state.paused is not None
    assert state.paused.consumed.agent_turns == 2, "turn 维可重算（T4 时代就有定义）"
    assert state.paused.consumed.model_requests is None
    assert state.paused.consumed.total_tokens is None
    assert state.paused.consumed.cost_usd is None


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
    assert projection["limits"] == {
        "max_agent_turns_total": 3, "max_model_requests": None,
        "max_total_tokens": None, "max_cost_usd": None,
    }
    assert projection["consumed"] == {
        "agent_turns": 2, "model_requests": 0, "total_tokens": 0, "cost_usd": "0",
    }
    assert projection["remaining"] == {
        "agent_turns": 1, "model_requests": None, "total_tokens": None, "cost_usd": None,
    }
    assert projection["local_fuse"] == {"max_agent_turns": 500, "source": "deployment"}
    assert set(projection["continuation"]) >= {
        "completed", "remaining", "blockers", CONTINUATION_ACTION_KEY,
    }
    assert projection["resume_requirements"] == []


def test_remaining_is_null_without_a_ceiling_never_zero() -> None:
    """无 ceiling ⇒ 该维 `remaining=null`（`11 §6.1`：缺席不是 0）。"""
    events = [_started(ceiling=None), _paused(2, ceiling=None, consumed=1)]
    state = derive_run_budget(events, RUN_ID)

    assert state.paused is not None
    remaining = state.paused.as_projection()["remaining"]
    assert remaining["agent_turns"] is None
    assert remaining["model_requests"] is None


def test_remaining_is_null_when_the_account_is_unknown() -> None:
    """账目未知 ⇒ remaining 也是 null（"不知道花了多少"推不出"还剩多少"）。"""
    snapshot = BudgetConsumed(agent_turns=2, model_requests=None, total_tokens=None,
                              cost_usd=None)
    limits = RunLimits(max_agent_turns_total=4, max_total_tokens=1000)
    events = [_started(), _paused(2, snapshot=snapshot, limits=limits)]
    state = derive_run_budget(events, RUN_ID)

    assert state.paused is not None
    remaining = state.paused.as_projection()["remaining"]
    assert remaining["agent_turns"] == 2
    assert remaining["total_tokens"] is None, "未知基数的剩余是 null（不是 1000、也不是 0）"


def test_cost_is_a_decimal_string_on_the_wire() -> None:
    """cost 在事件与投影里是十进制**字符串**：JSON 浮点相等不是契约（`11 §6.1`）。"""
    snapshot = BudgetConsumed(agent_turns=1, model_requests=1, total_tokens=10,
                              cost_usd=Decimal("0.0012"))
    limits = RunLimits(max_cost_usd=Decimal("0.01"))
    events = [_started(), _paused(2, snapshot=snapshot, limits=limits)]
    state = derive_run_budget(events, RUN_ID)

    assert state.paused is not None
    projection = state.paused.as_projection()
    assert projection["consumed"]["cost_usd"] == "0.0012"
    assert projection["limits"]["max_cost_usd"] == "0.01"
    assert projection["remaining"]["cost_usd"] == "0.0088"


def test_resume_limit_snapshot_replaces_the_ceiling() -> None:
    """恢复提供的是**绝对** ceiling：生效值以最后一次 `run/resumed` 为准。"""
    events = [_started(ceiling=3), _paused(2), _resumed(3, ceiling=9)]
    state = derive_run_budget(events, RUN_ID)

    assert state.limits.max_agent_turns_total == 9


def test_limits_are_read_per_dimension_without_cross_contamination() -> None:
    """某一维畸形**不**连坐其余维度（宽容读：读到畸形键就当它没配，其余照收）。"""
    started = _ev(
        1, RUN_STARTED,
        run_id=RUN_ID,
        budget={"run": {
            "max_agent_turns_total": 5,
            "max_model_requests": "nonsense",   # 畸形 ⇒ 当没配
            "max_total_tokens": 1000,
            "max_cost_usd": "-1",               # 负值 ⇒ 当没配（不臆造）
        }},
    )
    state = derive_run_budget([started], RUN_ID)

    assert state.limits.max_agent_turns_total == 5
    assert state.limits.max_model_requests is None
    assert state.limits.max_total_tokens == 1000
    assert state.limits.max_cost_usd is None


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

#: 无 ceiling 的 limits（判定的"没配这一维"那一支）。
_NO_LIMITS = RunLimits()


def test_pause_trigger_fires_one_turn_before_the_ceiling() -> None:
    """判定含预留 turn：`consumed + RESERVED >= ceiling`。

    于是暂停时**还剩**一次 closeout 容量（`02 §5.2` 的预留），第 ceiling 轮不会
    变成"既想 closeout 又没有容量"。
    """
    limits = RunLimits(max_agent_turns_total=3)

    assert pause_trigger(
        consumed=BudgetConsumed(agent_turns=1), run_limits=limits,
        execution_steps=1, local_fuse_turns=500,
    ) is None
    assert pause_trigger(
        consumed=BudgetConsumed(agent_turns=2), run_limits=limits,
        execution_steps=2, local_fuse_turns=500,
    ) == TRIGGER_RUN_TURNS


def test_pause_trigger_prefers_the_run_scope() -> None:
    """两个作用域同时命中时优先报 run ceiling——它是客户端配的那个、可恢复的动作是抬高它。"""
    assert pause_trigger(
        consumed=BudgetConsumed(agent_turns=2),
        run_limits=RunLimits(max_agent_turns_total=3),
        execution_steps=2, local_fuse_turns=3,
    ) == TRIGGER_RUN_TURNS


def test_local_fuse_fires_with_its_own_counter() -> None:
    """local fuse 是**实例级**保险丝：用本执行的步数计（续跑执行拿到的是新实例）。

    临界点是 `steps >= fuse`（`02 §5.1`：fuse 按**被接纳**的模型决策计数），**不**含
    closeout 预留——closeout 不是被接纳的决策，它在 fuse 上不占位，且这正是 EB-2 与
    T3 冻结的那个判定点（`#312` 只改它的去向，不改临界点）。
    """
    unlimited = RunLimits(max_agent_turns_total=None)

    assert pause_trigger(
        consumed=BudgetConsumed(agent_turns=99), run_limits=unlimited,
        execution_steps=1, local_fuse_turns=2,
    ) is None
    assert pause_trigger(
        consumed=BudgetConsumed(agent_turns=500), run_limits=unlimited,
        execution_steps=2, local_fuse_turns=2,
    ) == TRIGGER_LOCAL_TURNS


def test_requests_dimension_reserves_one_closeout_request() -> None:
    """requests 与 turns 同族：每次准入预留一次 closeout（closeout **就是**一次请求）。"""
    limits = RunLimits(max_model_requests=3)

    assert pause_trigger(
        consumed=BudgetConsumed(agent_turns=1, model_requests=1), run_limits=limits,
        execution_steps=1, local_fuse_turns=500,
    ) is None
    assert pause_trigger(
        consumed=BudgetConsumed(agent_turns=2, model_requests=2), run_limits=limits,
        execution_steps=2, local_fuse_turns=500,
    ) == TRIGGER_RUN_REQUESTS
    assert RESERVED_CLOSEOUT_REQUESTS == 1


def test_token_and_cost_dimensions_stop_at_the_line_without_reservation() -> None:
    """token / cost 的临界点是 `consumed >= ceiling`（下一轮多大不可预知）。

    `consumed == ceiling - 1` **仍然放行**：那一轮自己可能越线，这正是"不可预知"的
    诚实代价——规格要求的是"不得**有意**越线"，不是"保证不越线"。
    """
    limits = RunLimits(max_total_tokens=100, max_cost_usd=Decimal("1"))

    assert pause_trigger(
        consumed=BudgetConsumed(total_tokens=99, cost_usd=Decimal("0.99")),
        run_limits=limits, execution_steps=1, local_fuse_turns=500,
    ) is None, "差一格要放行（下一轮大小不可预知）"
    assert pause_trigger(
        consumed=BudgetConsumed(total_tokens=100, cost_usd=Decimal("0")),
        run_limits=limits, execution_steps=1, local_fuse_turns=500,
    ) == TRIGGER_RUN_TOKENS, "恰好到线即停"
    assert pause_trigger(
        consumed=BudgetConsumed(total_tokens=0, cost_usd=Decimal("1")),
        run_limits=limits, execution_steps=1, local_fuse_turns=500,
    ) == TRIGGER_RUN_COST


def test_unknown_account_stops_a_dimension_that_has_a_ceiling() -> None:
    """账目未知 + 该维有 ceiling ⇒ 停：无法证明在预算内时继续发起请求就是"有意越线"。"""
    limits = RunLimits(max_total_tokens=1000, max_cost_usd=Decimal("5"))
    unknown = BudgetConsumed(agent_turns=1, model_requests=2, total_tokens=None,
                             cost_usd=None)

    assert pause_trigger(
        consumed=unknown, run_limits=limits, execution_steps=1, local_fuse_turns=500,
    ) == TRIGGER_RUN_TOKENS


def test_closeout_capacity_needs_the_ceiling_to_be_strictly_above_consumed() -> None:
    assert closeout_capacity(
        consumed=BudgetConsumed(agent_turns=2),
        run_limits=RunLimits(max_agent_turns_total=3),
    ) is True
    assert closeout_capacity(
        consumed=BudgetConsumed(agent_turns=3),
        run_limits=RunLimits(max_agent_turns_total=3),
    ) is False
    assert closeout_capacity(
        consumed=BudgetConsumed(agent_turns=3),
        run_limits=RunLimits(max_agent_turns_total=None),
    ) is True, "无 run ceiling ⇒ 预留容量由 local fuse 决定（这里视为有）"


def test_closeout_capacity_is_false_when_the_token_account_is_unknown() -> None:
    """closeout 也要花 token / 也要钱：账目未知时声称它仍在预算内是无法兑现的。"""
    assert closeout_capacity(
        consumed=BudgetConsumed(total_tokens=None),
        run_limits=RunLimits(max_total_tokens=1000),
    ) is False


def test_resume_must_leave_room_for_a_turn_plus_the_reserved_closeout() -> None:
    """`ceiling == consumed + 1` 必须被拒：它会在下一次准入立刻再次暂停（假恢复）。"""
    assert resume_headroom_ok(
        consumed=BudgetConsumed(agent_turns=2),
        limits=RunLimits(max_agent_turns_total=3),
    ) is False
    assert resume_headroom_ok(
        consumed=BudgetConsumed(agent_turns=2),
        limits=RunLimits(max_agent_turns_total=4),
    ) is True
    assert resume_headroom_ok(
        consumed=BudgetConsumed(agent_turns=2), limits=RunLimits(),
    ) is True, "没配任何一维 ⇒ 没有可检查的维度（'至少给一个'是 validate_resume 的判定）"


def test_resume_headroom_checks_every_configured_dimension() -> None:
    """四维各自判定：任一维放不下一次新准入就整个拒绝（不做"部分恢复"）。"""
    consumed = BudgetConsumed(agent_turns=2, model_requests=2, total_tokens=100,
                              cost_usd=Decimal("1"))

    assert resume_headroom_ok(
        consumed=consumed,
        limits=RunLimits(max_agent_turns_total=4, max_model_requests=4,
                         max_total_tokens=200, max_cost_usd=Decimal("2")),
    ) is True, "四维都放得下一次新准入 ⇒ 接受"
    assert resume_headroom_ok(
        consumed=consumed,
        limits=RunLimits(max_agent_turns_total=3, max_model_requests=4,
                         max_total_tokens=200, max_cost_usd=Decimal("2")),
    ) is False, "turns 恰好等于 consumed + 预留 ⇒ 下一次准入立刻再暂停（假恢复）"
    assert resume_headroom_ok(
        consumed=consumed,
        limits=RunLimits(max_agent_turns_total=4, max_model_requests=3,
                         max_total_tokens=200, max_cost_usd=Decimal("2")),
    ) is False, "requests 与 turns 同族：同样要求 consumed + 预留 < ceiling"
    assert resume_headroom_ok(
        consumed=consumed,
        limits=RunLimits(max_agent_turns_total=4, max_model_requests=4,
                         max_total_tokens=100, max_cost_usd=Decimal("2")),
    ) is False, "token 恰好到线 ⇒ 同样拒绝（没有可花钱的余地）"
    assert resume_headroom_ok(
        consumed=consumed,
        limits=RunLimits(max_agent_turns_total=4, max_model_requests=4,
                         max_total_tokens=200, max_cost_usd=Decimal("1")),
    ) is False, "cost 恰好到线 ⇒ 同样拒绝"


def test_reserved_closeout_turns_is_one() -> None:
    """预留是**两个可数维度**的容量，常量是 1（`02 §5.2`）；local fuse 不吃它。"""
    assert RESERVED_CLOSEOUT_TURNS == 1


# ── 422：形状与可执行性（`11 §6.1`）──────────────────────────────────────


def test_cost_ceiling_accepts_decimal_strings_and_numbers() -> None:
    assert parse_cost_ceiling(None) is None
    assert parse_cost_ceiling("0.0012") == Decimal("0.0012")
    assert parse_cost_ceiling(0) == Decimal(0)
    assert parse_cost_ceiling(Decimal("7.5")) == Decimal("7.5")


@pytest.mark.parametrize("raw", ["abc", "", "-1", "NaN", "Infinity", True, object()])
def test_malformed_cost_ceiling_is_rejected_with_422(raw: object) -> None:
    """形状非法 ⇒ 422（不静默当作没配——那会让客户端以为配上了）。"""
    with pytest.raises(BudgetRejection):
        parse_cost_ceiling(raw)


def test_unenforceable_dimensions_are_rejected_before_any_request() -> None:
    """本链强制不了的维度 ⇒ 422（`11 §6.1`）。

    "这条链会不会自报 usage / cost"只能由集成方声明（探测它就得先发一次请求，
    那正是 422 要避免的）。生产链报不了 cost ⇒ 显式 `max_cost_usd` 恒 422。
    """
    # usage 能报、cost 报不了
    validate_ceiling_enforceability(RunLimits(max_total_tokens=1000), USAGE_ONLY)
    with pytest.raises(BudgetRejection) as excinfo:
        validate_ceiling_enforceability(RunLimits(max_cost_usd=Decimal("1")), USAGE_ONLY)
    assert "max_cost_usd" in str(excinfo.value)

    # 一条 usage 都报不了的链 ⇒ token ceiling 同样拒绝
    no_usage = ProviderAccounting(reports_usage=False, reports_cost=False)
    with pytest.raises(BudgetRejection):
        validate_ceiling_enforceability(RunLimits(max_total_tokens=1000), no_usage)

    # 生产能力声明下的形状：cost 不可强制（它是**部署能力**，不是某个 run 的事实）
    assert HARNESS_MODEL_ACCOUNTING.as_projection() == {
        "max_total_tokens": "enforceable", "max_cost_usd": "unavailable",
    }


def test_run_limits_from_request_is_the_single_422_rule_source() -> None:
    """Web / SessionService / CLI 共用的入口：形状 + 可执行性一次判完。"""
    limits = run_limits_from_request(
        max_agent_turns_total=5, max_model_requests=9, max_total_tokens=1000,
        accounting=FULL,
    )
    assert limits.max_agent_turns_total == 5
    assert limits.max_model_requests == 9
    assert limits.max_total_tokens == 1000

    with pytest.raises(BudgetRejection):
        run_limits_from_request(max_cost_usd="-2", accounting=FULL)
    with pytest.raises(BudgetRejection):
        run_limits_from_request(max_cost_usd="1", accounting=USAGE_ONLY)
    with pytest.raises(BudgetRejection):
        run_limits_from_request(max_total_tokens=10, accounting=ProviderAccounting(
            reports_usage=False, reports_cost=False,
        ))


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


def _limits(ceiling: int | None = 4) -> RunLimits:
    return RunLimits(max_agent_turns_total=ceiling)


def test_missing_declarations_are_shape_errors() -> None:
    paused = _paused_run()
    for kwargs in (
        {"run_id": None, "expected_version": 1, "limits": _limits(), "resume_basis": RESUME_BASIS_BUDGET_INCREASE},
        {"run_id": RUN_ID, "expected_version": None, "limits": _limits(), "resume_basis": RESUME_BASIS_BUDGET_INCREASE},
        {"run_id": RUN_ID, "expected_version": 1, "limits": _limits(), "resume_basis": None},
        {"run_id": RUN_ID, "expected_version": 1, "limits": _limits(), "resume_basis": "unknown_basis"},
    ):
        with pytest.raises(BudgetRejection):
            validate_resume(paused, **kwargs)


def test_wrong_run_id_is_a_state_conflict() -> None:
    with pytest.raises(BudgetConflict):
        validate_resume(
            _paused_run(), run_id=OTHER_RUN, expected_version=1, limits=_limits(),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        )


def test_stale_version_is_a_state_conflict() -> None:
    with pytest.raises(BudgetConflict) as excinfo:
        validate_resume(
            _paused_run(version=2), run_id=RUN_ID, expected_version=1, limits=_limits(),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        )
    assert "version" in str(excinfo.value)


def test_lowering_or_keeping_the_ceiling_is_a_state_conflict() -> None:
    """绝对 ceiling 必须**真提高**（不能降低、也不能不动）。"""
    for ceiling in (1, 2, 3):
        with pytest.raises(BudgetConflict):
            validate_resume(
                _paused_run(consumed=2), run_id=RUN_ID, expected_version=1,
                limits=_limits(ceiling), resume_basis=RESUME_BASIS_BUDGET_INCREASE,
            )


def test_a_resume_without_any_ceiling_is_a_state_conflict() -> None:
    """一个 ceiling 都不给 = 去掉全部 run ceiling（不是"抬高后恢复"）⇒ 409。"""
    with pytest.raises(BudgetConflict) as excinfo:
        validate_resume(
            _paused_run(), run_id=RUN_ID, expected_version=1, limits=RunLimits(),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        )
    assert "至少" in str(excinfo.value)


def test_a_resume_on_an_unknown_base_dimension_is_a_state_conflict() -> None:
    """消耗基数未知（T5 之前的暂停快照）时，不得给该维配 ceiling——"到线即停"无法成立。"""
    legacy = _ev(
        3, RUN_PAUSED, run_id=RUN_ID,
        reason=REASON_BUDGET_EXHAUSTED, trigger_dimension=TRIGGER_RUN_TURNS,
        budget_version=1, consumed={"agent_turns": 2},
        limits={"local": FUSE.as_projection(), "run": {"max_agent_turns_total": 3}},
        continuation={"completed": [], "remaining": [], "blockers": [],
                      "next_safe_action": "提高 ceiling"},
        closeout_source=CLOSEOUT_DETERMINISTIC, resume_requirements=[],
    )
    state = derive_run_budget([_started(), _ev(2, MODEL_COMPLETED), legacy], RUN_ID)
    assert state.paused is not None

    # turn 维（基数已知）可恢复
    validate_resume(
        state.paused, run_id=RUN_ID, expected_version=1, limits=_limits(5),
        resume_basis=RESUME_BASIS_BUDGET_INCREASE,
    )
    # requests / token / cost 维（基数未知）被拒
    for limits in (
        RunLimits(max_agent_turns_total=5, max_model_requests=9),
        RunLimits(max_agent_turns_total=5, max_total_tokens=1000),
        RunLimits(max_agent_turns_total=5, max_cost_usd=Decimal("1")),
    ):
        with pytest.raises(BudgetConflict) as excinfo:
            validate_resume(
                state.paused, run_id=RUN_ID, expected_version=1, limits=limits,
                resume_basis=RESUME_BASIS_BUDGET_INCREASE,
            )
        assert "未知" in str(excinfo.value)


def test_only_budget_increase_is_accepted_in_this_ticket() -> None:
    """其余三值的**证据判定**属 `#317`：现在收下它们等于假装校验过证据 ⇒ 409。"""
    with pytest.raises(BudgetConflict):
        validate_resume(
            _paused_run(), run_id=RUN_ID, expected_version=1, limits=_limits(),
            resume_basis=RESUME_BASIS_RELEVANT_STEER,
        )


def test_valid_resume_passes_and_returns_the_effective_ceilings() -> None:
    """通过时**返回生效集合**：调用方拿它去 launch / 落快照（不是拿请求值）。"""
    effective = validate_resume(
        _paused_run(), run_id=RUN_ID, expected_version=1, limits=_limits(4),
        resume_basis=RESUME_BASIS_BUDGET_INCREASE,
    )
    assert effective == RunLimits(max_agent_turns_total=4)


def test_resume_ceilings_overlay_the_paused_run_never_clear_unnamed_dimensions() -> None:
    """未点名的维度**沿用**暂停时的 ceiling，不会被一次恢复清空。

    清空 = 借着"抬高 token"把 operator 起的 turn ceiling 撤掉 = **放大**授权
    （ADR-0044 §10：「恢复…不能重置或放大 ceiling」）。所以恢复请求的语义是"在这几维
    给新的绝对 ceiling"，不是"这是新的全集"。
    """
    snapshot = BudgetConsumed(agent_turns=1, model_requests=1, total_tokens=100,
                              cost_usd=None)
    limits = RunLimits(max_agent_turns_total=8, max_total_tokens=100)
    events = [_started(), _paused(2, snapshot=snapshot, limits=limits)]
    paused = derive_run_budget(events, RUN_ID).paused
    assert paused is not None

    effective = validate_resume(
        paused, run_id=RUN_ID, expected_version=1,
        limits=RunLimits(max_total_tokens=200),
        resume_basis=RESUME_BASIS_BUDGET_INCREASE,
    )

    assert effective.max_total_tokens == 200, "点名的维度取请求值"
    assert effective.max_agent_turns_total == 8, "未点名的维度沿用暂停时的 ceiling"
    assert effective.max_model_requests is None, "本来就没有 ceiing 的维度仍是 None（不是 0）"


def test_resume_must_raise_the_dimension_that_actually_stopped_the_run() -> None:
    """真正卡住 run 的那一维没被抬高 ⇒ 409（即使请求抬了另一维）。

    这是 overlay 的配套判定：抬一个无关维度不会让 run 走下去，接受它等于给客户端一个
    "恢复成功但立刻又停"的假象。错误消息里必须能看见仍放不下的那一维。
    """
    snapshot = BudgetConsumed(agent_turns=1, model_requests=1, total_tokens=100)
    limits = RunLimits(max_agent_turns_total=2, max_total_tokens=100)
    events = [_started(), _paused(2, snapshot=snapshot, limits=limits)]
    paused = derive_run_budget(events, RUN_ID).paused
    assert paused is not None

    with pytest.raises(BudgetConflict) as excinfo:
        validate_resume(
            paused, run_id=RUN_ID, expected_version=1,
            limits=RunLimits(max_total_tokens=500),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        )
    assert "max_agent_turns_total" in str(excinfo.value)


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
    consumed = BudgetConsumed(agent_turns=2)
    continuation = deterministic_continuation(
        events=events, run_id=RUN_ID, trigger_dimension=TRIGGER_RUN_TURNS,
        limits=RunLimits(max_agent_turns_total=3), consumed=consumed,
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


def test_deterministic_continuation_says_unknown_instead_of_zero() -> None:
    """账目未知时文案写"未知"——一句"已花 0 元"会直接骗到正在决定要不要继续的人。"""
    consumed = BudgetConsumed(agent_turns=2, model_requests=None, total_tokens=None,
                              cost_usd=None)
    continuation = deterministic_continuation(
        events=[_started()], run_id=RUN_ID, trigger_dimension=TRIGGER_RUN_TOKENS,
        limits=RunLimits(max_total_tokens=1000), consumed=consumed,
    )
    blob = " ".join(
        [*continuation["completed"], *continuation["blockers"],
         continuation[CONTINUATION_ACTION_KEY]],
    )
    assert "未知" in blob
    assert "0 元" not in blob and "cost=0" not in blob


# ── 投影：project_budget（`11 §6.1`）────────────────────────────────────


def test_project_budget_running_state_carries_the_enforcement_projection() -> None:
    events = [_started(), _ev(2, MODEL_COMPLETED)]
    state = derive_run_budget(events, RUN_ID)

    projection = project_budget(state, accounting=USAGE_ONLY, local_fuse=FUSE)

    assert projection["run_id"] == RUN_ID
    assert projection["state"] == "running"
    assert projection["consumed"]["agent_turns"] == 1
    assert projection["enforcement"] == {
        "max_total_tokens": "enforceable", "max_cost_usd": "unavailable",
    }
    assert "reason" not in projection, "没有暂停原因时不落恒为 null 的键（缺席 ≠ 空值）"


def test_project_budget_terminal_and_none_states() -> None:
    terminal = derive_run_budget([_started(), _ev(2, MODEL_COMPLETED), _ev(3, RUN_COMPLETED)], RUN_ID)
    assert project_budget(terminal, accounting=USAGE_ONLY)["state"] == "terminal"

    # "none" 的构造与 `SessionService.budget_projection` 一致：会话里一个 run 都没有时
    # 它自己造一个 `run_id=None` 的空状态（`derive_run_budget` 只会被真实存在的 run 调用）。
    empty = RunBudgetState(
        run_id=None, version=1, limits=RunLimits(), consumed=BudgetConsumed(),
        paused=None, terminal=False,
    )
    projection = project_budget(empty, accounting=USAGE_ONLY)
    assert projection["state"] == "none"
    assert projection["run_id"] is None
    assert projection["remaining"]["agent_turns"] is None, "没有 run ⇒ 没有可言的剩余"


def test_project_budget_paused_state_is_a_superset_of_the_pause_projection() -> None:
    events = [_started(), _ev(2, MODEL_COMPLETED), _pause_event_with_usage()]
    state = derive_run_budget(events, RUN_ID)

    projection = project_budget(state, accounting=USAGE_ONLY)

    assert projection["state"] == "paused"
    assert projection["consumed"]["model_requests"] == 1
    assert projection["consumed"]["total_tokens"] == 120
    assert projection["enforcement"]["max_total_tokens"] == "enforceable"


def _pause_event_with_usage() -> SessionEvent:
    return _ev(3, RUN_PAUSED, run_id=RUN_ID, **build_pause_data(
        reason=REASON_BUDGET_EXHAUSTED, trigger_dimension=TRIGGER_RUN_TURNS,
        version=1,
        consumed=BudgetConsumed(agent_turns=1, model_requests=1, total_tokens=120,
                                cost_usd=Decimal("0.001")),
        limits=build_limits_snapshot(
            run_limits=RunLimits(max_agent_turns_total=3), local_fuse=FUSE,
        ),
        continuation={"completed": [], "remaining": [], "blockers": [],
                      "next_safe_action": "提高 ceiling"},
        closeout_source=CLOSEOUT_MODEL,
    ))


# ── 数据面：run/started 的 budget 与启动上下文 ───────────────────────────


def test_run_started_budget_key_is_omitted_without_a_ceiling() -> None:
    """不落键 = "本次 run 没有 run 作用域 ceiling"（缺省请求的事件序列逐字不变）。"""
    assert as_run_started_budget(RunLimits()) is None
    assert as_run_started_budget(RunLimits(max_agent_turns_total=7)) == {
        "run": {"max_agent_turns_total": 7, "max_model_requests": None,
                "max_total_tokens": None, "max_cost_usd": None},
    }


def test_run_started_budget_key_is_written_when_only_a_new_dimension_is_set() -> None:
    """`#313` 起 `configured` 是**四维任一**：只配 requests 的 run 同样要落快照——
    否则重启后 `run/paused.limits` 重建不出客户端配的 ceiling。"""
    budget = as_run_started_budget(RunLimits(max_model_requests=9))

    assert budget == {"run": {"max_agent_turns_total": None, "max_model_requests": 9,
                              "max_total_tokens": None, "max_cost_usd": None}}


def test_launch_context_defaults_to_a_new_run() -> None:
    """`run_id=None` ⇒ 新逻辑 run（runtime 自己 begin_run）；非 None ⇒ 续跑同一 run。"""
    fresh = LaunchRunBudget()
    assert (fresh.run_id, fresh.version, fresh.consumed_turns) == (None, 1, 0)
    resuming = LaunchRunBudget(
        version=2, limits=RunLimits(max_agent_turns_total=9),
        consumed=BudgetConsumed(agent_turns=3, model_requests=4, total_tokens=500,
                                cost_usd=Decimal("0.02")),
        run_id=RUN_ID, turn_index=4,
    )
    assert (resuming.run_id, resuming.version, resuming.consumed_turns) == (RUN_ID, 2, 3)
    assert resuming.consumed.model_requests == 4
    assert resuming.turn_index == 4
