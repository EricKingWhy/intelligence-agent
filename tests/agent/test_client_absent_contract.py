"""W-22（#366）：`client_absent` 持久暂停 / 恢复契约——纯函数层（零 IO）。

判据来源：Spec `02 §5.2.1`（产品客户端在场协议下的暂停扩展）、`03 §3.4`
（`reason` / `trigger_dimension` / `closeout_source` / `resume_basis` 的词表扩展）、
`03 §5`（Run 状态与恢复要求）、`11 §6.2`（重连不自动恢复；显式恢复 =
`client_return` + `expected_version` + 在场 + reconcile 完成）、ADR-0046。

wire 值一律按规格冻结文本的**字面量**书写（独立真相来源），不从实现常量抄——
"断言常量等于它自己"是恒真测试，钉不住契约。本文件与 `test_run_budget.py` 的
分工：那边钉 #305 家族的三类既有暂停（本票 MUST 不改它们），这边只钉扩展值。
"""

from __future__ import annotations

import pytest

from agent_harness.agent.budget import BudgetConflict, LocalFuse
from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    CONTINUATION_ACTION_KEY,
    RESUME_BASIS_BUDGET_INCREASE,
    RESUME_BASIS_VALUES,
    BudgetConsumed,
    RunLimits,
    build_limits_snapshot,
    build_pause_data,
    derive_run_budget,
    deterministic_continuation,
    reason_for_dimension,
    validate_resume,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_PAUSED,
    RUN_STARTED,
    SessionEvent,
)

RUN_ID = "run-1"
FUSE = LocalFuse(max_agent_turns=500, source="deployment")

#: `03 §3.4` 扩展词表的三个字面量（规格文本原词）。
REASON = "client_absent"
TRIGGER = "client_presence"
BASIS = "client_return"

#: 契约场景的 continuation 占位（内容判定在专门用例里做，这里只需要合法形状）。
_CONTINUATION = {
    "completed": [], "remaining": [], "blockers": [],
    "next_safe_action": "客户端回归后以同一 run_id 恢复",
}


def _ev(seq: int, type_: str, *, run_id: str | None = RUN_ID, **data: object) -> SessionEvent:
    return SessionEvent(seq=seq, type=type_, session_id="s1", run_id=run_id, data=dict(data))


def _started(seq: int = 1) -> SessionEvent:
    return _ev(seq, RUN_STARTED)


def _client_absent_pause(seq: int = 3) -> SessionEvent:
    """一条 `run/paused(reason=client_absent)`：客户端缺席不是预算事实，
    场景里没有任何 run ceiling（`limits.run` 全 null），消耗照常快照。
    `budget_version` 恒 1（场景里没有 run/resumed；CAS 的真相在事件计数）。"""
    return _ev(seq, RUN_PAUSED, **build_pause_data(
        reason=REASON,
        trigger_dimension=TRIGGER,
        version=1,
        consumed=BudgetConsumed(agent_turns=2),
        limits=build_limits_snapshot(run_limits=RunLimits(), local_fuse=FUSE),
        continuation=dict(_CONTINUATION),
        closeout_source=CLOSEOUT_DETERMINISTIC,
    ))


def _budget_pause(seq: int = 3) -> SessionEvent:
    """对照面：#305 的 `budget_exhausted` 暂停（ceiling=3、consumed=2）。"""
    limits = RunLimits(max_agent_turns_total=3)
    return _ev(seq, RUN_PAUSED, **build_pause_data(
        reason="budget_exhausted",
        trigger_dimension="run.max_agent_turns_total",
        version=1,
        consumed=BudgetConsumed(agent_turns=2),
        limits=build_limits_snapshot(run_limits=limits, local_fuse=FUSE),
        continuation=dict(_CONTINUATION),
        closeout_source=CLOSEOUT_DETERMINISTIC,
    ))


def _stuck_pause(seq: int = 3) -> SessionEvent:
    """对照面：#317 的 `stuck` 暂停（带检测快照）。"""
    return _ev(seq, RUN_PAUSED, **build_pause_data(
        reason="stuck",
        trigger_dimension="tool_result_loop",
        version=1,
        consumed=BudgetConsumed(agent_turns=4),
        limits=build_limits_snapshot(run_limits=RunLimits(), local_fuse=FUSE),
        continuation=dict(_CONTINUATION),
        closeout_source=CLOSEOUT_DETERMINISTIC,
        stuck={"pattern": "tool_result_loop", "count": 3, "threshold": 3},
    ))


def _paused_state(pause_event: SessionEvent):
    state = derive_run_budget(
        [_started(), _ev(2, MODEL_COMPLETED), pause_event], RUN_ID,
    )
    assert state.paused is not None
    return state.paused


# ── 词表：扩展值进入既有枚举，不新增事件类型 ─────────────────────────────


def test_client_return_is_a_declared_resume_basis() -> None:
    """`03 §3.4`：`client_return` 是第五个 `resume_basis` 取值（仅 `client_absent` 用）。

    它进的是**既有** `run/resumed` 事件的 data 值域——W-22 不新增事件类型，
    生成词表（EVENT_VOCABULARY / event-types.ts）因此不动。"""
    assert BASIS in RESUME_BASIS_VALUES


# ── 派生：client_absent 暂停是可恢复暂停（投影不特判） ───────────────────


def test_client_absent_pause_derives_resumable_state() -> None:
    """`03 §5`：client_absent 暂停仍是 `paused`（可恢复、非终态）；派生端
    对 reason 只做透传——重启 / replay 后同一份事件给出同一份投影。"""
    paused = _paused_state(_client_absent_pause())
    assert paused.reason == REASON
    assert paused.trigger_dimension == TRIGGER
    assert paused.closeout_source == CLOSEOUT_DETERMINISTIC, (
        "02 §5.2.1：client_absent 的 closeout 只能是 deterministic"
    )
    assert paused.version == 1
    projection = paused.as_projection()
    assert projection["state"] == "paused"
    assert projection["reason"] == REASON


# ── validate_resume：client_absent ⇄ client_return 一对一 ────────────────


def test_resume_with_client_return_is_accepted_without_naming_a_ceiling() -> None:
    """`11 §6.2` / `02 §5.2.1`：显式恢复 = `client_return` + 当前 `expected_version`。

    预算没有被耗尽 ⇒ **不要求**点名任何新 ceiling（"必须至少给一个绝对值"是
    预算暂停的恢复依据，不是客户端回归的）；未点名的维度沿用暂停快照
    （返回的生效集合 = 快照本身）。"""
    paused = _paused_state(_client_absent_pause())
    effective = validate_resume(
        paused, run_id=RUN_ID, expected_version=1,
        limits=RunLimits(), resume_basis=BASIS,
    )
    assert isinstance(effective, RunLimits)
    assert not effective.configured, "快照里没有 ceiling，恢复也不凭空造一个"


def test_resume_client_return_still_needs_current_version() -> None:
    """CAS 不因新原因而放松（`03 §3.4`：version 不匹配 = 409，零副作用）。

    派生的 version = 1 + run/resumed 条数（事件说了算，不是暂停 data 里的字段），
    本场景没有 resumed ⇒ 当前 version=1，报 2 就是过期声明。"""
    paused = _paused_state(_client_absent_pause())
    assert paused.version == 1
    with pytest.raises(BudgetConflict):
        validate_resume(
            paused, run_id=RUN_ID, expected_version=2,
            limits=RunLimits(), resume_basis=BASIS,
        )


def test_budget_increase_is_rejected_for_client_absent_pause() -> None:
    """client_absent 只接受 `client_return`；且拒绝文案不得再把它当"未知暂停原因"
    （它是本票之后词表里的已知值），也不得指向"抬高 ceiling"这条必被 409 的路。"""
    paused = _paused_state(_client_absent_pause())
    with pytest.raises(BudgetConflict) as excinfo:
        validate_resume(
            paused, run_id=RUN_ID, expected_version=1,
            limits=RunLimits(max_agent_turns_total=9),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        )
    message = str(excinfo.value)
    assert BASIS in message, "拒绝文案要指出唯一有效的依据"
    assert "未知暂停原因" not in message


def test_client_return_is_rejected_for_budget_pause() -> None:
    """`client_return` 只对 client_absent 有效（`03 §3.4`）：预算暂停收到它是
    **409**（值是合法声明、状态对不上），不是 422——#305 的"只接受
    budget_increase"分支原样接住它。"""
    paused = _paused_state(_budget_pause())
    with pytest.raises(BudgetConflict):
        validate_resume(
            paused, run_id=RUN_ID, expected_version=1,
            limits=RunLimits(max_agent_turns_total=9), resume_basis=BASIS,
        )


def test_client_return_is_rejected_for_stuck_pause() -> None:
    """stuck 暂停收到 `client_return` 同样 409（fail-closed）：stuck 缺的是
    "外部输入变了"的观测证据，客户端回归不构成那三类证据中的任何一类。"""
    paused = _paused_state(_stuck_pause())
    with pytest.raises(BudgetConflict):
        validate_resume(
            paused, run_id=RUN_ID, expected_version=1,
            limits=RunLimits(), resume_basis=BASIS,
        )


# ── reason 映射与确定性 continuation 文案 ────────────────────────────────


def test_reason_for_dimension_maps_client_presence_to_client_absent() -> None:
    """`03 §3.4`：`trigger_dimension=client_presence` ⇒ `reason=client_absent`。
    既有两个映射（deadline / 预算类）不被本票改动。"""
    assert reason_for_dimension(TRIGGER) == REASON
    assert reason_for_dimension("run.deadline_at") == "deadline"
    assert reason_for_dimension("run.max_agent_turns_total") == "budget_exhausted"


def test_client_absent_continuation_points_at_client_return_not_at_ceilings() -> None:
    """`02 §5.2.1` / ADR-0044 D4 同款禁令：确定性 continuation 不得暗示
    "提高 ceiling 后恢复"（那条路对 client_absent 必被 409 挡死），要指向
    `client_return`；且不得为收口发模型请求（那条判定在 runtime 层用例里钉）。"""
    continuation = deterministic_continuation(
        events=[], run_id=RUN_ID, trigger_dimension=TRIGGER,
        limits=RunLimits(), consumed=BudgetConsumed(agent_turns=2),
        reason=REASON,
    )
    action = continuation[CONTINUATION_ACTION_KEY]
    assert BASIS in action
    assert "提高" not in action, "不得指向必被 409 的抬 ceiling 路径"
    assert any("客户端" in item for item in continuation["blockers"])
