"""T7（`#315`）deadline 边界 Live Gate 场景的**确定性**测试（不烧真实模型调用）。

真实 3/3 证据由 `python scripts/live_gate.py run --scenario run-deadline-boundary`
产出并落进 `docs/live_gate/**`；本文件只钉**场景自身**的可机检事实，避免"跑了才知道
场景是不是恒 PASS / 恒 FAIL"：

1. **信息屏障与前提都是真的**：链脚本一次调用最多推进一格、拿旧串不推进、每格真的耗
   `STEP_SLEEP_SECONDS`（⇒ 两条 deadline 窗口都跑不完）；`uncertain.py` **先落副作用、
   再挂着**（⇒ 超时收尾时世界状态真的未知，不是"其实什么都没发生"）；
2. **执行域那一半可复现**：生产 `BashTool`（MUTATING）+ 生产 `ToolExecutor` + 生产
   `SqliteOperationLedger` 在一次性工作区里走一遍 —— 到点前接纳 ⇒ `TIMEOUT` + 账上
   `UNKNOWN` + "副作用未证"；到点**后**同一条调用 ⇒ `DEADLINE_EXCEEDED` + **不留行**
   （拒收的三个证据同向：错误码 / 无账行 / 副作用计数没变）；
3. **断言集是诚实的**：两种安全结局（Arm A `safe` / Arm B `NEED_RECONCILE`）各自在一个
   自洽事件集上**全部通过**，而每一种"走错路"都要判不通过（暂停字段错 / 到点后还有请求 /
   沿用已到点时刻却被放行 / 未证副作用被当成没事 / 恢复后没继续干活 / 投影与账本不一致 /
   点名欠账的那句话缺失 / …）；
4. **运行时目录被重定向进一次性根**（取证卫生的另一半）：轨迹必须落在 `ctx.session_root`
   下（runner 按它归档），且不得落到开发仓库。

（替身模型跑场景**不算** Live Gate 证据 —— runner 的 `seams` 把那种运行锁到 FAIL；
本文件也不注册任何场景、不落盘证据。）
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_harness.agent.budget import BudgetConflict
from agent_harness.agent.run_budget import (
    REASON_DEADLINE,
    RESUME_BASIS_BUDGET_INCREASE,
    STATE_NEEDS_RECONCILE,
    TRIGGER_RUN_DEADLINE,
    BudgetConsumed,
    PausedRun,
    RunLimits,
    validate_resume,
)
from agent_harness.config import Settings
from agent_harness.sandbox.local import LocalSubprocessSandbox
from agent_harness.session import MODEL_REQUEST, Session
from agent_harness.session.errors import RecoveryConflict
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import (
    OperationContext,
    OperationState,
    SqliteOperationLedger,
    needs_reconcile,
)
from agent_harness.tooling import (
    ApprovalResponse,
    ErrorCode,
    PermissionPolicy,
    ToolCall,
    ToolExecutor,
    ToolRegistry,
)
from agent_harness.tools import BashTool
from evaluation.live_gate.registry import ScenarioContext
from evaluation.live_gate.scenarios.accounting import request_accounting
from evaluation.live_gate.scenarios.deadline import (
    CHAIN_SCRIPT,
    DEADLINE_SECONDS,
    DONE_FILE,
    RESUME_DEADLINE_SECONDS,
    SCENARIO,
    SEED_TOKEN,
    STEP_SLEEP_SECONDS,
    STEPS_FILE,
    TOKEN_FILE,
    TRANSITIONS,
    UNCERTAIN_ADMIT_SECONDS,
    UNCERTAIN_SCRIPT,
    UNCERTAIN_SENTINEL,
    UNCERTAIN_SENTINEL_LINE,
    UNCERTAIN_SLEEP_SECONDS,
    UNCERTAIN_TOOL_TIMEOUT_SECONDS,
    _deadline_text,
    _failure_text,
    _projection_reconcile_pending,
    _Refusal,
    _scenario_settings,
)

RUN_ID = "run-deadline-1"
TOOL_CALL_NAME = "bash"
CALL_ONE = "call-1"
CALL_TWO = "call-2"
CALL_AFTER_RESUME = "call-3"
UNCERTAIN_CALL = "call-uncertain-1"
UNCERTAIN_CALL_TWO = "call-uncertain-2"

#: 合成轨迹里的两个绝对时刻（固定值，与"现在的挂钟"无关：断言比的是快照回带的时刻
#: 与启动时给的那个是否**同一个瞬时**，不是"离现在多远"）。
DEADLINE_1 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
DEADLINE_2 = datetime(2026, 9, 26, 12, 1, 0, tzinfo=UTC)

#: 每格请求自报的 token（断言比的是"与轨迹重算相同"，数字本身无关紧要）。
PRIMARY_TOKENS = 13

#: 断言名清单（多一条 / 少一条都要被发现：断言集变了就是判据变了）。
EXPECTED_ASSERTIONS = {
    "deadline_pause_snapshot",
    "deadline_pause_precedes_any_terminal",
    "deadline_pause_continuation_contract",
    "real_work_admitted_before_the_deadline",
    "deadline_pause.request_count",
    "deadline_pause.tokens",
    "deadline_pause.cost",
    "expired_deadline_resume_refused",
    "deadline_outcome_is_safe_or_needs_reconcile",
    "resume_contract_holds",
    "resumed_leg_did_new_work",
    "run_ended_in_a_safe_state",
    "uncertain_mutation_recorded_as_unproven",
    "no_new_admission_after_the_deadline",
    "unreconciled_debt_blocks_recovery",
    "projection_matches_ledger_debt",
    "run_identity_and_task_shape",
    "final_budget_state",
    "durable_replay_matches_live",
    "stream_mirrors_pause",
    "no_fuse_trip",
    "no_dangling_tool_calls_in_run",
    "session_identity_present",
}


def _settings(**overrides: Any) -> Settings:
    """真实 `Settings`，但**不读仓库 `.env`**（单测不把部署机凭证带进进程）。"""
    return Settings(_env_file=None, **overrides)


def _context(tmp_path: Path, *, sandbox: Any | None = None,
             settings: Settings | None = None) -> ScenarioContext:
    return ScenarioContext(
        settings=settings if settings is not None else _settings(),
        sandbox=sandbox if sandbox is not None else _StubSandbox({}),
        session_root=tmp_path / "sessions",
        session_id="live-gate-deadline-a1",
        attempt_index=1,
    )


class _StubSandbox:
    """断言面替身：只实现 `read_text`（真沙箱在下面的脚本 / 执行域用例里被真的跑过）。"""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    def read_text(self, name: str) -> str:
        if name not in self._files:
            raise FileNotFoundError(name)
        return self._files[name]


# ── 合成事件（与真实轨迹同形）─────────────────────────────────────────────


def _ops(*, debt: bool) -> list[Any]:
    """账本行：干净（全部终态）或第二条带着"副作用未证"标记。"""
    seeded = [
        SimpleNamespace(
            tool_call_id=CALL_ONE, tool_name=TOOL_CALL_NAME,
            state=OperationState.SUCCEEDED, reconcile_meta=None,
        ),
        SimpleNamespace(
            tool_call_id=CALL_TWO, tool_name=TOOL_CALL_NAME,
            state=OperationState.SUCCEEDED, reconcile_meta=None,
        ),
    ]
    if debt:
        seeded[1].state = OperationState.NEED_RECONCILE
    return seeded


def _pause_payload(
    *, deadline: datetime, consumed: dict[str, Any], version: int,
    blockers: list[str] | None = None, next_action: str | None = None,
) -> dict[str, Any]:
    """`run/paused.data`（字段名是 `03 §3.4` 的契约）。

    deadline 暂停的四条专属形状都在这里：`reason=deadline`、
    `trigger_dimension=run.deadline_at`、`closeout_source=deterministic`（到点后不发
    Provider 请求）、`resume_requirements=()`（非空只出现在 stuck 暂停）。
    """
    return {
        "reason": REASON_DEADLINE,
        "trigger_dimension": TRIGGER_RUN_DEADLINE,
        "budget_version": version,
        "consumed": consumed,
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"deadline_at": _deadline_text(deadline)},
        },
        "continuation": {
            "completed": ["已跑过若干链步"],
            "remaining": ["暂停发生在下一轮模型决策之前"],
            "blockers": list(blockers or []),
            "next_safe_action": next_action or (
                "给出新的未来 run.deadline_at 后以同一 run_id 恢复"
            ),
        },
        "closeout_source": "deterministic",
        "resume_requirements": [],
        "trace_id": "trace-deadline-1",
    }


def _resume_payload(*, consumed: dict[str, Any]) -> dict[str, Any]:
    """`run/resumed.data`（同 run 续跑：版本 +1、换成**新**的未来时刻）。"""
    return {
        "from_pause_seq": 1,  # 由 `_events` 回填成暂停那一条的 seq
        "previous_budget_version": 1,
        "budget_version": 2,
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"deadline_at": _deadline_text(DEADLINE_2)},
        },
        "consumed": consumed,
        "resume_basis": RESUME_BASIS_BUDGET_INCREASE,
    }


def _events(
    *, arm: str = "safe", ending: str = "paused", work_after_resume: bool = True,
) -> list[Any]:
    """自洽的轨迹：到点（腿 1）⇒ 换新时刻恢复（仅 Arm A）⇒ 按 `ending` 收尾。

    每次实际请求都落一格 `model/request`（`02 §5.1` 的计数点）；deadline 暂停**没有**
    closeout 请求（到点后不发 Provider 请求）—— 这条正是 `deadline_pause.request_count`
    要钉住的：快照里的请求数只能等于到点前那几格（"本地 closeout 也是请求"只出现在
    预算暂停那一侧）。

    `arm="needs_reconcile"` 时按生产顺序组装 Arm B 该有的三件套：对账事件 →
    （带 blockers 的）`run/paused`，文案与 `runtime._raise_deadline_reconcile` /
    `run_budget._reconcile_first_action` 逐字同形。

    第二条腿的收尾形状由模型当下的选择决定（真实运行两种都出现过）：`ending="paused"`
    是又干到到点，`ending="completed"` 是它自己写一句话收尾，`ending="failed"` 是
    **不安全**的第三种（只用于反例）。`work_after_resume=False` 造出"恢复后零新调用"
    的走过场形状（同样只用于反例）。
    """
    events: list[Any] = []
    seq = 0

    def add(event_type: str, data: dict | None = None) -> None:
        nonlocal seq
        events.append(
            SimpleNamespace(
                seq=seq, type=event_type, data=data or {}, run_id=RUN_ID, step_id=None,
            )
        )
        seq += 1

    def turn(call_id: str, *, retried: bool = False) -> None:
        """一次真实作业：一次（或带失败重试的两次）请求 + 一条被接纳的决策 + 一对 call/result。

        `retried=True` 造出"请求数 ≠ 产出轮数"的真实形状（`02 §5.1`：失败的那次也是
        一次真实 Provider 请求、也进 `model_requests`，但不产出决策）。计数点分开之后
        这个差值是**正常**的，所以它必须出现在自洽轨迹里 —— 否则"把轮数当请求数"这个
        反例在本文件里会变成空转（两个数恰好相等）。
        """
        if retried:
            add(MODEL_REQUEST, {"role": "primary", "outcome": "failed",
                                "usage": {"total_tokens": PRIMARY_TOKENS}})
            add(MODEL_REQUEST, {"role": "fallback", "outcome": "completed",
                                "usage": {"total_tokens": PRIMARY_TOKENS}})
        else:
            add(MODEL_REQUEST, {"role": "primary", "outcome": "completed",
                                "usage": {"total_tokens": PRIMARY_TOKENS}})
        add("model/completed", {"content": ""})
        add("tool/call", {"tool_name": TOOL_CALL_NAME, "tool_call_id": call_id})
        add("tool/result", {"tool_call_id": call_id})

    def facts_until(cutoff: int) -> dict[str, Any]:
        """到截止点为止的四维（与 `accounting.request_accounting` 同一份重算）。"""
        facts = request_accounting([event for event in events if event.seq <= cutoff])
        return {
            "agent_turns": facts.turns,
            "model_requests": facts.requests,
            "total_tokens": facts.tokens,
            "cost_usd": None if facts.cost is None else format(facts.cost, "f"),
        }

    add("session/started")
    add("session/resumed")
    add("user/message", {"content": "chain task"})
    add("run/started", {
        "turn_index": 1, "budget": {"run": {"deadline_at": _deadline_text(DEADLINE_1)}},
    })
    turn(CALL_ONE, retried=True)
    turn(CALL_TWO)
    pause_seq = seq
    if arm == "needs_reconcile":
        # 生产顺序：对账事件先落（Ledger 先于事件、事件先于 suspend），暂停里再点名。
        add("operation/reconcile-required", {
            "tool_call_id": CALL_TWO, "tool_name": TOOL_CALL_NAME,
            "args_identity": "sha256:deadbeef",
            "state": OperationState.NEED_RECONCILE.value,
        })
        add("run/paused", _pause_payload(
            deadline=DEADLINE_1, consumed=facts_until(pause_seq), version=1,
            blockers=_production_blockers(CALL_TWO),
            next_action=_reconcile_first_action(),
        ))
        return events
    add("run/paused", _pause_payload(
        deadline=DEADLINE_1, consumed=facts_until(pause_seq), version=1,
    ))
    add("run/resumed", {**_resume_payload(consumed=facts_until(pause_seq)),
                        "from_pause_seq": pause_seq})
    if work_after_resume:
        turn(CALL_AFTER_RESUME)
    if ending == "completed":
        add("run/completed", {"final_text": "已达截止时间，报告当前进度"})
    elif ending == "failed":
        add("run/failed", {"error": "恢复后崩了（反例）"})
    else:
        add("run/paused", _pause_payload(
            deadline=DEADLINE_2, consumed=facts_until(seq), version=2,
        ))
    return events


def _facts(events: list[Any]) -> dict[str, Any]:
    """第一条暂停之前的四维（与场景自己算快照的那一份同源）。"""
    pauses = [event for event in events if event.type == "run/paused"]
    cutoff = pauses[0].seq if pauses else 10**9
    facts = request_accounting([event for event in events if event.seq <= cutoff])
    return {
        "agent_turns": facts.turns,
        "model_requests": facts.requests,
        "total_tokens": facts.tokens,
        "cost_usd": None if facts.cost is None else format(facts.cost, "f"),
    }


def _first_pause(events: list[Any]) -> Any:
    return next(event for event in events if event.type == "run/paused")


def _patched_pause(events: list[Any], payload: dict[str, Any]) -> Any:
    """换掉第一条暂停的 payload、**保留它的 seq**：反例只改要考的那一维。

    seq 是判据的一部分（`pause_facts` 按它切轨迹），随手编一个会让"某一处不对"的
    用例同时踩中消耗对账，红得没有分辨力。
    """
    return _paused_event(payload, seq=_first_pause(events).seq)


def _production_blockers(tool_call_id: str) -> list[str]:
    """生产 blockers 的**原文形状**（整句，不是 id）—— 判据是子串，这里如实复刻。"""
    return [
        (
            f"工具 '{TOOL_CALL_NAME}'（tool_call_id={tool_call_id}）的副作用状态未证："
            "已进入 NEED_RECONCILE，对账解除前本 run 不可恢复"
        ),
        f"{TRIGGER_RUN_DEADLINE} 已到点：deadline={_deadline_text(DEADLINE_1)}",
    ]


def _reconcile_first_action() -> str:
    return (
        "先 reconcile 未结清的副作用（1 项）：确认它到底有没有落盘，"
        "再由 ReconcileCallback 给出裁决——裁决落地前本 run 不可恢复"
        "（03 §5：对账优先于恢复），也不要重跑那条调用（不变量 #14）"
    )


def _with_paused_payload(events: list[Any], payload: dict[str, Any]) -> list[Any]:
    """把**第一条** `run/paused` 的 data 换掉（其余字段与后续暂停不动）—— 反例用。"""
    replaced = False
    patched: list[Any] = []
    for event in events:
        if event.type == "run/paused" and not replaced:
            replaced = True
            patched.append(SimpleNamespace(
                seq=event.seq, type=event.type, data=payload,
                run_id=event.run_id, step_id=event.step_id,
            ))
        else:
            patched.append(event)
    return patched


def _with_resumed_payload(events: list[Any], payload: dict[str, Any]) -> list[Any]:
    return [
        SimpleNamespace(seq=event.seq, type=event.type, data=payload,
                        run_id=event.run_id, step_id=event.step_id)
        if event.type == "run/resumed" else event
        for event in events
    ]


def _without(events: list[Any], event_type: str) -> list[Any]:
    return [event for event in events if event.type != event_type]


def _paused_event(payload: dict[str, Any], *, seq: int = 6) -> Any:
    """把一份 payload 包成"就是这条暂停"（反例要连 seq 一起给，因为断言读它）。"""
    return SimpleNamespace(seq=seq, type="run/paused", data=payload, run_id=RUN_ID,
                           step_id=None)


def _expired_deadline_refusal() -> Any:
    """真·生产措辞的"沿用已到点时刻"拒绝（不是手抄的一句话）。

    直接调 `validate_resume`（唯一规则来源，端点 / CLI 共用它），用一个**已过去**的
    deadline 触发 headroom 判据 ⇒ 拿到 `BudgetConflict` 与它的真实文案。场景里那条
    `"严格在未来" in reason` 的判据因此钉在**生产真的会说的话**上，而不是本文件编的一句。
    """
    paused = PausedRun(
        run_id=RUN_ID, pause_seq=6, step_id=6, reason=REASON_DEADLINE,
        trigger_dimension=TRIGGER_RUN_DEADLINE, version=1,
        consumed=BudgetConsumed(
            agent_turns=2, model_requests=2, total_tokens=2 * PRIMARY_TOKENS,
        ),
        limits=RunLimits(deadline_at=DEADLINE_1), local_fuse=None,
        continuation={}, closeout_source="deterministic", resume_requirements=(),
    )
    try:
        validate_resume(
            paused, run_id=RUN_ID, expected_version=1,
            limits=RunLimits(deadline_at=DEADLINE_1),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
            now=DEADLINE_1 + timedelta(seconds=5),
        )
    except BudgetConflict as refusal:  # 这就是被测的那条路径
        return _Refusal(raised=refusal, events_unchanged=True)
    raise AssertionError("沿用一个已到点的 deadline 竟然被放行：headroom 判据没拦住")


def _refusal(*, raised: BaseException | None, events_unchanged: bool = True) -> _Refusal:
    return _Refusal(raised=raised, events_unchanged=events_unchanged)


def _uncertain(**overrides: Any) -> dict[str, Any]:
    """执行域那一半的实测结论（形状与 `_uncertain_mutation` 的返回一致）。"""
    unproven_row = SimpleNamespace(
        tool_call_id=UNCERTAIN_CALL, tool_name=TOOL_CALL_NAME,
        state=OperationState.UNKNOWN, reconcile_meta='{"unproven_side_effect": true}',
    )
    values: dict[str, Any] = {
        "first": SimpleNamespace(result=SimpleNamespace(
            ok=False, error_code=ErrorCode.TIMEOUT, retryable=False,
        )),
        "second": SimpleNamespace(result=SimpleNamespace(
            ok=False, error_code=ErrorCode.DEADLINE_EXCEEDED, retryable=False,
        )),
        "rows": {UNCERTAIN_CALL: unproven_row},
        "rows_after_recovery": {UNCERTAIN_CALL: unproven_row},
        "sentinel_after_calls": 1,
        "sentinel_after_recovery": 1,
        "recovery_refused": RecoveryConflict("存在未 reconcile 的副作用"),
        "reconcile_pending": [UNCERTAIN_CALL],
        "needs_reconcile_first": True,
    }
    values.update(overrides)
    return values


def _stream(*types: str) -> list[Any]:
    """live 流的替身：场景按 `event.type` 读帧名（真流里是 AgentEvent）。"""
    return [SimpleNamespace(type=type_name) for type_name in types]


def _default_legs(*, resumed: bool) -> dict[str, Any]:
    """`legs` 的默认值：有恢复事件 ⇒ Arm A 的形状；没有 ⇒ Arm B 的形状。"""
    legs: dict[str, Any] = {
        "first_deadline": DEADLINE_1,
        "first_stream": _stream("run/started", "model/completed", "run/paused"),
        "resume_stream": [],
        # 两种结局下都必须被拒（Arm B 是对账优先，Arm A 是"恢复后立刻再次到点"）。
        "resume_refused": _expired_deadline_refusal(),
    }
    if resumed:
        legs["resume_deadline"] = DEADLINE_2
        legs["resume_stream"] = _stream("model/completed", "run/paused")
    else:
        legs["resume_with_new_instant_refused"] = _refusal(
            raised=RecoveryConflict("存在未 reconcile 的副作用"),
        )
    return legs


def _assertions(
    tmp_path: Path, events: list[Any], *, operations: list[Any] | None = None,
    uncertain: dict[str, Any] | None = None, projection: dict[str, Any] | None = None,
    legs: dict[str, Any] | None = None, sandbox: Any | None = None,
    pause: Any | None = None,
) -> list[Any]:
    """跑一次断言面。

    `operations` / `projection` / `legs` 的默认值由**轨迹本身**给出（有 `run/resumed`
    ⇒ Arm A 的账本与恢复事实），所以"自洽"这一侧的用例只需要给 events；要考"某一处
    不对"的用例显式传那一维。`pause` 缺省取轨迹里的第一条 —— **没有暂停就不给**
    （合成一条会让"根本没有暂停"这种反例悄悄变绿）。
    """
    pauses = [event for event in events if event.type == "run/paused"]
    resumed = any(event.type == "run/resumed" for event in events)
    if operations is None:
        operations = _ops(debt=not resumed)
    if projection is None:
        debt_ids = sorted(
            operation.tool_call_id for operation in operations if needs_reconcile(operation)
        )
        # **生产形状**（`run_budget.project_budget`）：空欠账**不落键**，非空落
        # `reconcile` 子对象。替身必须照抄这个形状——替身自己编一个键名（本文件曾是
        # `{"reconcile_pending": [...]}`）会让"读错键名"这种 bug 在离线全绿
        # （实测：真实运行三连 FAIL 才暴露）。
        projection = (
            {"reconcile": {"state": STATE_NEEDS_RECONCILE, "tool_call_ids": debt_ids}}
            if debt_ids else {}
        )
    ctx = _context(
        tmp_path, sandbox=sandbox if sandbox is not None else _StubSandbox({}),
    )
    return SCENARIO._assertions(
        ctx=ctx, events=events, replayed=list(events), tool_calls=[TOOL_CALL_NAME],
        operations=operations, projection=projection,
        legs=legs if legs is not None else _default_legs(resumed=resumed),
        uncertain=uncertain if uncertain is not None else _uncertain(),
        pause=pause if pause is not None else (pauses[0] if pauses else None),
    )


def _failed(assertions: list[Any]) -> set[str]:
    return {item.name for item in assertions if not item.ok}


def _red_details(assertions: list[Any]) -> dict[str, Any]:
    return {item.name: item.detail for item in assertions if not item.ok}


# ── 自洽的两条腿：全部通过 ────────────────────────────────────────────────


def test_safe_arm_passes_on_a_self_consistent_trajectory(tmp_path):
    """Arm A（safe）：到点 ⇒ 换新时刻同 run 续跑 ⇒ 再到点。断言集必须**全绿**。"""
    events = _events(arm="safe")
    assertions = _assertions(tmp_path, events)
    assert {item.name for item in assertions} == EXPECTED_ASSERTIONS
    assert _red_details(assertions) == {}


def test_needs_reconcile_arm_passes_on_a_trajectory_that_names_the_debt(tmp_path):
    """Arm B（NEED_RECONCILE）：一条暂停 + 点名欠账 + 换新时刻也被拒 ⇒ 全绿。"""
    events = _events(arm="needs_reconcile")
    assertions = _assertions(tmp_path, events)
    assert _red_details(assertions) == {}


def test_needs_reconcile_arm_is_red_when_the_debt_is_not_named(tmp_path):
    """Arm B 的语义就是"点名"：continuation 不提欠账 ⇒ 必须判红（不是"也算安全"）。

    这一条同时钉住 `blockers` 的判据是**子串**：`runtime._raise_deadline_reconcile`
    给的是 `工具 '<name>'（tool_call_id=<id>）…` 这样的整句，拿 id 去 `in <list>`
    做的是相等比较，会恒红。
    """
    events = _events(arm="needs_reconcile")
    pause = _patched_pause(events, _pause_payload(
        deadline=DEADLINE_1, consumed=_facts(events), version=1,
        blockers=[f"{TRIGGER_RUN_DEADLINE} 已到点：deadline={_deadline_text(DEADLINE_1)}"],
        next_action="抬高 ceiling 后以同一 run_id 恢复",
    ))
    assertions = _assertions(tmp_path, _with_paused_payload(events, pause.data), pause=pause)
    assert "deadline_outcome_is_safe_or_needs_reconcile" in _failed(assertions)


def test_needs_reconcile_arm_accepts_the_production_blocker_wording(tmp_path):
    """正向对照：整句 blocker（含 tool_call_id）+ `先 reconcile …` 动作要被认出来。"""
    assertions = _assertions(tmp_path, _events(arm="needs_reconcile"))
    assert "deadline_outcome_is_safe_or_needs_reconcile" not in _failed(assertions)


# ── 反例：每一处"走错路"都要判红 ──────────────────────────────────────────


def test_red_debt_without_naming_it_is_neither_arm(tmp_path):
    """有欠账行却按 Arm A 的形状收口（无 reconcile 事件、无 blockers）⇒ 判红。

    这一条钉的是**分类本身**：结局按账本选出 Arm，而 Arm A 要求"干净"，所以
    "有欠账 + 没点名"不能被读成安全。
    """
    events = _events(arm="safe")
    assertions = _assertions(tmp_path, events, operations=_ops(debt=True))
    failed = _failed(assertions)
    assert "deadline_outcome_is_safe_or_needs_reconcile" in failed
    assert "projection_matches_ledger_debt" not in failed, "投影跟着账本走"


def test_red_clean_ledger_cannot_be_read_as_reconcile(tmp_path):
    """账本干净却按 Arm B 收口（只有一条暂停、恢复被拒）⇒ 判红（该续跑没续跑）。

    红的落点是**恢复面**那三条，不是"结局分类"：分类只看账本有没有欠账，账本干净
    时它本来就该报 Arm A —— 错的是"该续跑却没续跑"（`resume_contract_holds` /
    `resumed_leg_did_new_work` / `run_ended_in_a_safe_state`）。
    """
    events = _events(arm="needs_reconcile")
    assertions = _assertions(tmp_path, events, operations=_ops(debt=False))
    failed = _failed(assertions)
    assert "resume_contract_holds" in failed
    assert "resumed_leg_did_new_work" in failed
    assert "run_ended_in_a_safe_state" in failed
    assert "final_budget_state" in failed, "没有恢复事件 ⇒ 派生版本仍是 1"


def test_red_pause_payload_must_be_a_deadline_pause(tmp_path):
    """暂停字段错（reason / trigger / closeout / resume_requirements / 版本）⇒ 判红。"""
    events = _events(arm="safe")
    good = _pause_payload(deadline=DEADLINE_1, consumed=_facts(events), version=1)
    mutations = {
        "reason": {"reason": "budget_exhausted"},
        "trigger": {"trigger_dimension": "run.max_agent_turns_total"},
        "closeout": {"closeout_source": "model"},
        "requirements": {"resume_requirements": ["needs_reconcile"]},
        "version": {"budget_version": 2},
    }
    for label, patch in mutations.items():
        pause = _patched_pause(events, {**good, **patch})
        assertions = _assertions(
            tmp_path, _with_paused_payload(events, pause.data), pause=pause,
        )
        assert "deadline_pause_snapshot" in _failed(assertions), label


def test_red_pause_snapshot_must_echo_the_requested_instant(tmp_path):
    """快照里的 `run.deadline_at` 必须是启动时给的那个瞬时（不是重算 / 另一个）。"""
    events = _events(arm="safe")
    good = _pause_payload(deadline=DEADLINE_1, consumed=_facts(events), version=1)
    drifted = {
        **good,
        "limits": {**good["limits"], "run": {"deadline_at": _deadline_text(DEADLINE_2)}},
    }
    pause = _patched_pause(events, drifted)
    assertions = _assertions(
        tmp_path, _with_paused_payload(events, drifted), pause=pause,
    )
    assert "deadline_pause_snapshot" in _failed(assertions)


def test_red_a_request_after_the_deadline_breaks_the_closeout_contract(tmp_path):
    """到点后**又发了一次 Provider 请求**（closeout=model + 账上多一格）⇒ 判红。

    这是本票最硬的一条：`04 §9.1` 的"到点后不启动任何新工作"在暂停快照上表现为
    `closeout_source=deterministic` 且 `model_requests` 恰好等于到点前那几格。
    """
    events = _events(arm="safe")
    emitted = _facts(events)
    payload = _pause_payload(
        deadline=DEADLINE_1,
        consumed={**emitted, "model_requests": emitted["model_requests"] + 1},
        version=1,
    )
    payload["closeout_source"] = "model"
    pause = _patched_pause(events, payload)
    assertions = _assertions(
        tmp_path, _with_paused_payload(events, payload), pause=pause,
    )
    failed = _failed(assertions)
    assert "deadline_pause_snapshot" in failed
    assert "deadline_pause.request_count" in failed


def test_red_consumed_counters_must_match_the_trajectory(tmp_path):
    """快照四维与轨迹重算不符（token / cost / 请求数）⇒ 各判红。"""
    events = _events(arm="safe")
    emitted = _facts(events)
    assert emitted["model_requests"] > emitted["agent_turns"], (
        "轨迹里必须存在'请求数 ≠ 产出轮数'（有一次失败重试）——否则下面第三个反例空转"
    )
    cases = {
        "tokens": ({"total_tokens": emitted["total_tokens"] + 7}, "deadline_pause.tokens"),
        "cost": ({"cost_usd": "0.01"}, "deadline_pause.cost"),
        # 把"产出轮"当成"请求数"：两个计数点分开是本票的契约之一
        "requests": ({"model_requests": emitted["agent_turns"]},
                     "deadline_pause.request_count"),
    }
    for label, (patch, expected) in cases.items():
        payload = _pause_payload(
            deadline=DEADLINE_1, consumed={**emitted, **patch}, version=1,
        )
        pause = _patched_pause(events, payload)
        assertions = _assertions(
            tmp_path, _with_paused_payload(events, payload), pause=pause,
        )
        assert expected in _failed(assertions), label


def test_red_pause_without_any_real_work_before_the_deadline(tmp_path):
    """到点前一次工具都没跑（纯对话）⇒ 第 1 条结构事实没有载体，判红。"""
    events = [
        event for event in _events(arm="safe")
        if event.type not in ("tool/call", "tool/result")
    ]
    assert "real_work_admitted_before_the_deadline" in _failed(_assertions(tmp_path, events))


def test_red_expired_deadline_resume_that_was_not_refused(tmp_path):
    """沿用已到点时刻恢复却被放行（或原因不对 / 事件流被改了）⇒ 判红。"""
    events = _events(arm="safe")
    cases = {
        "放行": _refusal(raised=None),
        "原因不对": _refusal(raised=BudgetConflict("恢复必须把绝对 ceiling 提高到能继续")),
        "改了事件": _refusal(
            raised=BudgetConflict("deadline 维度的判据是「严格在未来」"),
            events_unchanged=False,
        ),
    }
    for label, refusal in cases.items():
        legs = {**_default_legs(resumed=True), "resume_refused": refusal}
        assertions = _assertions(tmp_path, events, legs=legs)
        assert "expired_deadline_resume_refused" in _failed(assertions), label


def test_red_arm_a_resume_must_carry_a_new_future_instant(tmp_path):
    """Arm A 的恢复：时刻没换新 / 版本没抬 / 不是同一条暂停 ⇒ 判红。"""
    events = _events(arm="safe")
    resumed = next(event for event in events if event.type == "run/resumed")
    good = dict(resumed.data)
    mutations = {
        "沿用旧时刻": {
            **good,
            "limits": {**good["limits"], "run": {"deadline_at": _deadline_text(DEADLINE_1)}},
        },
        "版本没抬": {**good, "budget_version": 1},
        "接错暂停": {**good, "from_pause_seq": 99},
    }
    for label, payload in mutations.items():
        assertions = _assertions(tmp_path, _with_resumed_payload(events, payload))
        assert "resume_contract_holds" in _failed(assertions), label


def test_red_resume_that_starts_no_new_work(tmp_path):
    """恢复之后没有任何 `tool/call` ⇒ "续跑"是假的（信息屏障保证还有活要干）。"""
    events = _events(arm="safe")
    resumed = next(event for event in events if event.type == "run/resumed")
    trimmed = [
        event for event in events
        if not (event.type in ("tool/call", "tool/result") and event.seq > resumed.seq)
    ]
    assertions = _assertions(tmp_path, trimmed)
    assert "resumed_leg_did_new_work" in _failed(assertions)


def test_red_resume_that_starts_a_second_execution_of_the_same_run(tmp_path):
    """同 run 续跑不新建 `run/started`、不落第二条 `user/message`。"""
    events = _events(arm="safe")
    tail = max(event.seq for event in events) + 1
    extra_started = [*events, SimpleNamespace(
        seq=tail, type="run/started", data={"turn_index": 2},
        run_id=RUN_ID, step_id=None,
    )]
    failed = _failed(_assertions(tmp_path, extra_started))
    assert "resume_contract_holds" in failed
    assert "deadline_pause_precedes_any_terminal" in failed

    extra_user = [*events, SimpleNamespace(
        seq=tail, type="user/message", data={"content": "接着干"},
        run_id=RUN_ID, step_id=None,
    )]
    failed = _failed(_assertions(tmp_path, extra_user))
    assert "run_identity_and_task_shape" in failed
    assert "resume_contract_holds" in failed


def test_red_more_than_one_pause_per_leg(tmp_path):
    """一次执行只允许一条 `run/paused`（多出一条 ⇒ 收尾形状既不是暂停也不是完成）。"""
    events = _events(arm="safe")
    first_pause = next(event for event in events if event.type == "run/paused")
    duplicated = [*events, SimpleNamespace(
        seq=max(event.seq for event in events) + 1, type="run/paused",
        data=first_pause.data, run_id=RUN_ID, step_id=None,
    )]
    assert "run_ended_in_a_safe_state" in _failed(_assertions(tmp_path, duplicated))


def test_red_terminal_event_before_the_pause(tmp_path):
    """暂停之前已有终态事件 ⇒ 暂停不是本次执行的收口，判红。"""
    events = _events(arm="safe")
    shifted = [
        SimpleNamespace(seq=event.seq + 1, type=event.type, data=event.data,
                        run_id=event.run_id, step_id=event.step_id)
        for event in events
    ]
    inserted = [
        SimpleNamespace(seq=0, type="run/failed", data={"reason": "boom"},
                        run_id=RUN_ID, step_id=None),
        *shifted,
    ]
    assert "deadline_pause_precedes_any_terminal" in _failed(_assertions(tmp_path, inserted))


def test_red_continuation_must_be_complete(tmp_path):
    """continuation 四键不齐 / `next_safe_action` 空 ⇒ 判红。"""
    events = _events(arm="safe")
    good = _pause_payload(deadline=DEADLINE_1, consumed=_facts(events), version=1)
    mutations = {
        "缺键": {"continuation": {"completed": [], "remaining": [], "blockers": []}},
        "空动作": {"continuation": {**good["continuation"], "next_safe_action": "   "}},
        "类型错": {"continuation": {**good["continuation"], "completed": "已跑过"}},
    }
    for label, patch in mutations.items():
        pause = _patched_pause(events, {**good, **patch})
        assertions = _assertions(
            tmp_path, _with_paused_payload(events, pause.data), pause=pause,
        )
        assert "deadline_pause_continuation_contract" in _failed(assertions), label


def test_red_missing_pause_or_resume(tmp_path):
    """根本没有暂停（前提不成立）/ 恢复事件缺失 ⇒ 各判红。"""
    events = _events(arm="safe")

    without_pause = _without(events, "run/paused")
    failed = _failed(_assertions(tmp_path, without_pause))
    assert "deadline_pause_snapshot" in failed
    assert "real_work_admitted_before_the_deadline" in failed

    without_resume = _without(events, "run/resumed")
    failed = _failed(_assertions(
        tmp_path, without_resume,
        operations=_ops(debt=False), legs=_default_legs(resumed=True),
    ))
    assert "resume_contract_holds" in failed
    assert "resumed_leg_did_new_work" in failed
    assert "run_ended_in_a_safe_state" in failed
    assert "final_budget_state" in failed


def test_red_projection_must_match_the_ledger(tmp_path):
    """投影 `reconcile.tool_call_ids` 与账本欠账不一致（少报 / 多报）⇒ 判红。"""
    events = _events(arm="needs_reconcile")
    for label, pending in {"少报": [], "多报": ["call-ghost"]}.items():
        projection = (
            {"reconcile": {"state": STATE_NEEDS_RECONCILE, "tool_call_ids": pending}}
            if pending else {}
        )
        assertions = _assertions(tmp_path, events, projection=projection)
        assert "projection_matches_ledger_debt" in _failed(assertions), label


def test_projection_without_the_debt_key_is_red_not_a_crash(tmp_path):
    """读不到欠账键时**判红**，不得崩：`list(None)` 的那种崩法会让整次真实运行白跑。

    反例刻意用**错键名**（`reconcile_pending`）——它正是本票实证踩过的那个坑：
    替身照抄错键名时离线全绿，真实运行却在断言面抛
    `TypeError: 'NoneType' object is not iterable`（三连 FAIL，每次 ≈ 4 分钟真实调用）。
    """
    events = _events(arm="needs_reconcile")
    assertions = _assertions(
        tmp_path, events, projection={"reconcile_pending": [CALL_TWO]},
    )
    assert "projection_matches_ledger_debt" in _failed(assertions)


def test_projection_read_admits_shapes_the_product_really_emits(tmp_path):
    """三种真实形状：缺键（无欠账）/ 非字典 / 正常子对象 —— 只有一种读出欠账。"""
    assert _projection_reconcile_pending({}) == []
    assert _projection_reconcile_pending({"state": "paused"}) == []
    assert _projection_reconcile_pending(None) == []
    assert _projection_reconcile_pending({"reconcile": None}) == []
    assert _projection_reconcile_pending(
        {"reconcile": {"state": STATE_NEEDS_RECONCILE, "tool_call_ids": [CALL_TWO, CALL_ONE]}}
    ) == [CALL_ONE, CALL_TWO], "排序后返回（同一份欠账的不同快照要能直接比）"


def test_red_ledger_debt_must_block_both_paths(tmp_path):
    """欠账只在"换新时刻也被拒"这一条上成立：换新时刻被放行 ⇒ 判红（Arm B）。"""
    events = _events(arm="needs_reconcile")
    legs = {**_default_legs(resumed=False),
            "resume_with_new_instant_refused": _refusal(raised=None)}
    assert "deadline_outcome_is_safe_or_needs_reconcile" in _failed(
        _assertions(tmp_path, events, legs=legs)
    )


# ── 执行域那一半 ──────────────────────────────────────────────────────────


def test_red_uncertain_mutation_evidence(tmp_path):
    """未证副作用那一组：每一维单独改错都要判红。"""
    unproven_row = SimpleNamespace(
        tool_call_id=UNCERTAIN_CALL, tool_name=TOOL_CALL_NAME,
        state=OperationState.UNKNOWN, reconcile_meta='{"unproven_side_effect": true}',
    )
    cases: dict[str, tuple[dict[str, Any], set[str]]] = {
        "调用 A 没超时": (
            _uncertain(first=SimpleNamespace(result=SimpleNamespace(
                ok=True, error_code=None, retryable=False))),
            {"uncertain_mutation_recorded_as_unproven"},
        ),
        "账行不是 UNKNOWN": (
            _uncertain(rows={UNCERTAIN_CALL: SimpleNamespace(
                tool_call_id=UNCERTAIN_CALL, tool_name=TOOL_CALL_NAME,
                state=OperationState.SUCCEEDED, reconcile_meta=None)}),
            {"uncertain_mutation_recorded_as_unproven"},
        ),
        "副作用没落地": (
            _uncertain(sentinel_after_calls=0),
            {"uncertain_mutation_recorded_as_unproven",
             "no_new_admission_after_the_deadline"},
        ),
        "到点后又接纳了一次": (
            _uncertain(
                second=SimpleNamespace(result=SimpleNamespace(
                    ok=False, error_code=ErrorCode.TIMEOUT, retryable=True)),
                rows={
                    UNCERTAIN_CALL: unproven_row,
                    UNCERTAIN_CALL_TWO: SimpleNamespace(
                        tool_call_id=UNCERTAIN_CALL_TWO, tool_name=TOOL_CALL_NAME,
                        state=OperationState.RUNNING, reconcile_meta=None,
                    ),
                },
            ),
            {"no_new_admission_after_the_deadline"},
        ),
        "拒绝类型不对": (
            _uncertain(recovery_refused=RuntimeError("boom")),
            {"unreconciled_debt_blocks_recovery"},
        ),
        "拒绝时顺手改了账": (
            _uncertain(rows_after_recovery={UNCERTAIN_CALL: SimpleNamespace(
                tool_call_id=UNCERTAIN_CALL, tool_name=TOOL_CALL_NAME,
                state=OperationState.SUCCEEDED, reconcile_meta=None)}),
            {"unreconciled_debt_blocks_recovery"},
        ),
        "恢复时盲重跑了": (
            _uncertain(sentinel_after_recovery=2),
            {"unreconciled_debt_blocks_recovery"},
        ),
        "投影没报欠账": (
            _uncertain(reconcile_pending=[]),
            {"unreconciled_debt_blocks_recovery"},
        ),
    }
    events = _events(arm="safe")
    for label, (payload, expected) in cases.items():
        failed = _failed(_assertions(tmp_path, events, uncertain=payload))
        assert expected <= failed, f"{label}: 期望 {sorted(expected)}，实际 {sorted(failed)}"


def test_red_dangling_tool_call_and_fuse_trip(tmp_path):
    """悬空 tool call / 撞上 local fuse ⇒ 各自判红。"""
    events = _events(arm="safe")
    dangling = _without(events, "tool/result")
    assert "no_dangling_tool_calls_in_run" in _failed(_assertions(tmp_path, dangling))

    fused = [
        SimpleNamespace(
            seq=event.seq, type=event.type,
            data=({**event.data, "reason": "max_steps_exceeded"}
                  if event.type == "run/paused" else event.data),
            run_id=event.run_id, step_id=event.step_id,
        )
        for event in events
    ]
    assert "no_fuse_trip" in _failed(_assertions(tmp_path, fused))


def test_red_durable_replay_must_match_the_live_stream(tmp_path):
    """落盘轨迹与内存态不一致（重读少一条）⇒ 判红。"""
    events = _events(arm="safe")
    assertions = SCENARIO._assertions(
        ctx=_context(tmp_path, sandbox=_StubSandbox({})),
        events=events, replayed=events[:-1], tool_calls=[TOOL_CALL_NAME],
        operations=_ops(debt=False), projection={"reconcile_pending": []},
        legs=_default_legs(resumed=True), uncertain=_uncertain(),
        pause=next(event for event in events if event.type == "run/paused"),
    )
    assert "durable_replay_matches_live" in _failed(assertions)


def test_red_live_stream_without_the_pause_frame(tmp_path):
    """live 流里没有 `run/paused`（落盘有、镜像没有）⇒ 判红（帧 = 落盘日志的前缀）。"""
    events = _events(arm="safe")
    legs = {**_default_legs(resumed=True),
            "first_stream": _stream("run/started", "model/completed")}
    assert "stream_mirrors_pause" in _failed(_assertions(tmp_path, events, legs=legs))


def test_red_run_identity_must_be_single(tmp_path):
    """轨迹里混进别的 run_id ⇒ 判红。"""
    events = _events(arm="safe")
    foreign = [*events, SimpleNamespace(
        seq=max(event.seq for event in events) + 1, type="model/completed", data={},
        run_id="run-other", step_id=None,
    )]
    assert "run_identity_and_task_shape" in _failed(_assertions(tmp_path, foreign))


# ── 真沙箱：脚本的结构性质与执行域的机制 ──────────────────────────────────


def test_safe_arm_accepts_the_completed_ending(tmp_path):
    """恢复后模型自己收尾（`run/completed`）是**第二种合法安全形状**（真实运行见过）。

    实测：模型在恢复后答一句状态就收尾 —— 那不是失败，也不是产品回归（到点暂停不是
    任务结束，但"模型自己决定收尾"是它的自由，本场景的断言面明说不评模型表现）。
    断言两种都收，3/3 才不是抛硬币；同时 `final_budget_state` 必须跟着换成
    "终态与 paused 互斥"（`derive_run_budget`：终态压过暂停）——否则这一条会因为
    派生账本里 `paused=None` 而误红。
    """
    assertions = _assertions(tmp_path, _events(ending="completed"))
    assert _failed(assertions) == set(), _failed(assertions)


def test_red_resume_without_new_work(tmp_path):
    """恢复后一个新调用都没接纳 ⇒ 判红（"恢复"不是走过场）。"""
    assertions = _assertions(tmp_path, _events(work_after_resume=False))
    assert "resumed_leg_did_new_work" in _failed(assertions)


def test_red_unsafe_ending_after_resume(tmp_path):
    """恢复后以 `run/failed` 收尾（第三种形状）⇒ 判红，且不认成两种安全形状里的任何一种。"""
    assertions = _assertions(tmp_path, _events(ending="failed"))
    failed = _failed(assertions)
    assert "run_ended_in_a_safe_state" in failed
    assert "final_budget_state" in failed, "终态是 failed ⇒ 派生账本与'两种安全形状'不符"


def test_prepare_reports_missing_python_as_unmet_precondition(tmp_path):
    """沙箱里没有 python ⇒ 前置不成立（BLOCKED 面），不是 FAIL，也不 seed 任何文件。"""

    class _NoPythonSandbox:
        def exec(self, command: str) -> SimpleNamespace:
            return SimpleNamespace(exit_code=1, stdout="", stderr="not found")

        def write_text(self, name: str, content: str) -> None:  # pragma: no cover
            raise AssertionError("前置不成立时不该 seed 任何文件")

    ctx = _context(tmp_path, sandbox=_NoPythonSandbox())
    problems = asyncio.run(SCENARIO.prepare(ctx))
    assert problems and "python" in problems[0]


def test_prepare_seeds_both_scripts_and_their_state(tmp_path):
    """`prepare` 在真沙箱里 seed 链状态与两个脚本，且**不**预置完成标记。"""
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    ctx = _context(tmp_path, sandbox=sandbox)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    assert sandbox.read_text(TOKEN_FILE).strip() == SEED_TOKEN
    assert sandbox.read_text(STEPS_FILE).strip() == "0"
    chain = sandbox.read_text(CHAIN_SCRIPT)
    assert "secrets.token_hex" in chain
    # 键名刻意避开 `token=`（`live_gate/secrets.py` 的凭证回显扫描会判红）
    assert "next=" in chain
    script = sandbox.read_text(UNCERTAIN_SCRIPT)
    assert UNCERTAIN_SENTINEL in script and str(UNCERTAIN_SLEEP_SECONDS) in script
    try:
        sandbox.read_text(DONE_FILE)
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - 预置完成标记会让"链跑完了"变成假事实
        raise AssertionError(f"{DONE_FILE} 不该被 seed")


def test_chain_script_advances_only_with_the_current_token(tmp_path):
    """信息屏障：错串不推进，对串推进一格、产出新随机串、且真的耗时。"""
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    ctx = _context(tmp_path, sandbox=sandbox)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    wrong = sandbox.exec(f"python {CHAIN_SCRIPT} not-the-seed")
    assert wrong.exit_code != 0
    assert sandbox.read_text(STEPS_FILE).strip() == "0", "错串不得推进"
    assert sandbox.read_text(TOKEN_FILE).strip() == SEED_TOKEN, "错串不得改写当前串"

    started = datetime.now(UTC)
    ok = sandbox.exec(f"python {CHAIN_SCRIPT} {SEED_TOKEN}")
    elapsed = (datetime.now(UTC) - started).total_seconds()
    assert ok.exit_code == 0
    assert "step=1" in ok.stdout
    assert elapsed >= STEP_SLEEP_SECONDS, f"单步耗时 {elapsed:.2f}s 短于 {STEP_SLEEP_SECONDS}s"
    assert sandbox.read_text(STEPS_FILE).strip() == "1"

    advanced = sandbox.read_text(TOKEN_FILE).strip()
    assert advanced != SEED_TOKEN
    assert "next=" in ok.stdout and advanced in ok.stdout, "新串只能从上一条输出里读出来"
    # 再拿**旧**串调用不推进：一次调用最多一格，且必须用最新串
    again = sandbox.exec(f"python {CHAIN_SCRIPT} {SEED_TOKEN}")
    assert again.exit_code != 0
    assert sandbox.read_text(STEPS_FILE).strip() == "1"
    # 信息屏障的量化版本：两条 deadline 窗口之和也跑不完链（常数改一个就得重算）
    assert TRANSITIONS * STEP_SLEEP_SECONDS > DEADLINE_SECONDS + RESUME_DEADLINE_SECONDS, (
        "链必须在两条窗口里都跑不完"
    )


def test_uncertain_script_lands_its_side_effect_before_hanging(tmp_path):
    """`uncertain.py` 先落副作用、再挂着 ⇒ 超时收尾时世界状态**真的**未知。"""
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    ctx = _context(tmp_path, sandbox=sandbox)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    # 1s 后杀掉：脚本本该挂 8s，但副作用必须在被杀之前就已经落盘
    killed = sandbox.exec(f"python {UNCERTAIN_SCRIPT}", timeout=1.0)
    assert killed.timed_out is True
    assert sandbox.read_text(UNCERTAIN_SENTINEL).strip() == UNCERTAIN_SENTINEL_LINE


def test_uncertain_timing_constants_are_ordered(tmp_path):
    """三个时间常数的**关系**才是前提：接纳窗口 < 工具超时 < 脚本挂起时长。

    改任意一个都要重新论证（本条把论证变成机检事实）：`ADMIT < TIMEOUT` 保证调用 A
    在到点前被接纳，`TIMEOUT < SLEEP` 保证超时先发生（脚本没跑完 ⇒ 副作用未证成立）。
    """
    assert UNCERTAIN_ADMIT_SECONDS < UNCERTAIN_TOOL_TIMEOUT_SECONDS < UNCERTAIN_SLEEP_SECONDS
    assert UNCERTAIN_TOOL_TIMEOUT_SECONDS < 60.0, "生产 bash 超时是 60s，本场景只调小不调大"


def _executor_harness(tmp_path: Path) -> tuple[Any, Any, Any, Any]:
    """生产接线的一份最小复刻：`BashTool` + `ToolExecutor` + `SqliteOperationLedger`。

    与 `deadline._uncertain_mutation` 同一个类、同一个参数名、同一个审批默认值
    （`assembly.py`：`ApprovalResponse(approved=True, reason="auto-approve")`）。
    这一层不复刻场景的断言，只复刻它依赖的**机制**：让"生产 MUTATING 工具在一次性
    Sandbox 里真的走过 deadline 边界"这件事在单测里可复现、可回归。
    """
    workspace = tmp_path / "workspace"
    sandbox = LocalSubprocessSandbox(workspace_root=workspace)
    ctx = _context(tmp_path, sandbox=sandbox)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store, session_id="deadline-executor", cwd=workspace)
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    asyncio.run(ledger.initialize())
    registry = ToolRegistry()
    registry.register(BashTool(sandbox, timeout_seconds=UNCERTAIN_TOOL_TIMEOUT_SECONDS))

    async def _auto_approve(_request: Any) -> ApprovalResponse:
        return ApprovalResponse(approved=True, reason="auto-approve")

    executor = ToolExecutor(
        registry, policy=PermissionPolicy.WORKSPACE_WRITE,
        approval_callback=_auto_approve, operation_ledger=ledger,
    )
    return executor, ledger, session, ctx


def test_executor_records_an_unproven_timeout_and_refuses_after_the_deadline(tmp_path):
    """执行域那一半的机制：到点前接纳 ⇒ UNKNOWN + 未证；到点后 ⇒ 拒收 + 不留行。"""
    executor, ledger, session, ctx = _executor_harness(tmp_path)
    context = OperationContext(session_id=session.session_id, run_id=None, agent_id="default")
    deadline = datetime.now(UTC) + timedelta(seconds=UNCERTAIN_ADMIT_SECONDS)

    async def _run() -> tuple[Any, Any, list[Any]]:
        first = await executor.execute(
            ToolCall(id=UNCERTAIN_CALL, name="bash",
                     args={"command": f"python {UNCERTAIN_SCRIPT}"}),
            operation_context=context, session=session, run_deadline=deadline,
        )
        second = await executor.execute(
            ToolCall(id=UNCERTAIN_CALL_TWO, name="bash",
                     args={"command": f"python {UNCERTAIN_SCRIPT}"}),
            operation_context=context, session=session, run_deadline=deadline,
        )
        return first, second, await ledger.list_for_session(session.session_id)

    first, second, rows = asyncio.run(_run())
    by_id = {operation.tool_call_id: operation for operation in rows}

    assert first.result.ok is False
    assert first.result.error_code is ErrorCode.TIMEOUT
    assert first.result.retryable is False, "MUTATING 超时不重试（副作用未知）"
    assert by_id[UNCERTAIN_CALL].state is OperationState.UNKNOWN
    assert needs_reconcile(by_id[UNCERTAIN_CALL]) is True
    assert ctx.sandbox.read_text(UNCERTAIN_SENTINEL).strip() == UNCERTAIN_SENTINEL_LINE

    assert second.result.ok is False
    assert second.result.error_code is ErrorCode.DEADLINE_EXCEEDED
    assert second.result.retryable is False
    assert UNCERTAIN_CALL_TWO not in by_id, "准入前被拒不得在账上留行"
    assert ctx.sandbox.read_text(UNCERTAIN_SENTINEL).count(UNCERTAIN_SENTINEL_LINE) == 1


# ── 执行域那一半：场景**方法本身**（不是它的复刻）────────────────────────


def test_uncertain_mutation_leg_runs_on_the_production_composition(tmp_path):
    """`deadline._uncertain_mutation` 在一次性 Sandbox 里跑通：生产装配 + 生产恢复入口。

    与 `test_executor_records_an_unproven_timeout_...` 的分工：那条复刻的是**机制**
    （BashTool + ToolExecutor + Ledger），这条走的是**场景自己的方法**，也就是真实运行
    会走的那段代码——差额恰好在复刻之外：`AppState` 的惰性 store 初始化、
    `SessionService.recover` 的恢复闸门、`SessionService.budget_projection` 的投影。
    实测教训：三连真实 FAIL（`TypeError: 'NoneType' object is not iterable`）时机制层的
    单测全绿，说明红在没人离线跑过的那一段；把这段也钉住，红就不再需要真实调用才发现。
    """
    workspace = tmp_path / "workspace"
    sandbox = LocalSubprocessSandbox(workspace_root=workspace)
    ctx = _context(
        tmp_path, sandbox=sandbox,
        settings=_settings(
            workspace_dir=str(tmp_path), artifact_dir=str(tmp_path / "artifacts"),
        ),
    )
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    async def _leg() -> dict[str, Any]:
        from agent_harness.web.app import AppState, session_service

        state = AppState(ctx.settings)
        try:
            await state.ensure_stores()  # 与 `run()` 同一前置（生产装配是惰性的）
            return await SCENARIO._uncertain_mutation(
                ctx=ctx, state=state, service=session_service(state),
                store=JsonlSessionStore(root=ctx.session_root),
            )
        finally:
            await state.shutdown()

    result = asyncio.run(_leg())

    rows = result["rows"]
    assert result["first"].result.error_code is ErrorCode.TIMEOUT
    assert rows[UNCERTAIN_CALL].state is OperationState.UNKNOWN
    assert result["needs_reconcile_first"] is True
    assert result["sentinel_after_calls"] == 1, "副作用已落地 ⇒ 结论真的证不出来"
    assert result["second"].result.error_code is ErrorCode.DEADLINE_EXCEEDED
    assert UNCERTAIN_CALL_TWO not in rows, "准入前被拒不得在账上留行"
    assert type(result["recovery_refused"]).__name__ == "RecoveryConflict"
    assert result["sentinel_after_recovery"] == 1, "恢复被拒 ⇒ 没有盲重跑"
    assert result["reconcile_pending"] == [UNCERTAIN_CALL], "投影把欠账如实报给客户端"


# ── 失败可定位（真实运行的取证纪律）──────────────────────────────────────


def test_failure_text_names_the_failing_line():
    """失败读数必须**指得出哪一行**，不只是异常类型（真实运行只有这一次机会）。"""

    def _inner() -> None:
        payload: list[str] | None = None
        for _ in payload:
            pass

    try:
        _inner()
    except TypeError as error:
        text = _failure_text(error)
    else:  # pragma: no cover - 上面必然抛 TypeError
        raise AssertionError("预期 TypeError")

    assert text.startswith("TypeError: 'NoneType' object is not iterable")
    assert "test_deadline_scenario.py" in text, text
    assert "for _ in payload" in text, text


# ── 运行时目录重定向（取证卫生）──────────────────────────────────────────


def test_runtime_dirs_are_redirected_into_the_attempt_root(tmp_path):
    """`workspace_dir` / `artifact_dir` 落在一次性根内；其余字段逐字沿用部署配置。

    轨迹落在 `<workspace_dir>/sessions/<session_id>/` 是 runner 归档证据的前提
    （`_copy_events` 从 `ctx.session_root/<session_id>` 取），而相对默认值会按进程 CWD
    落到开发仓库 —— 两条都要机械钉住。
    """
    original = _settings()
    ctx = _context(tmp_path, settings=original)
    effective = _scenario_settings(ctx)
    root = ctx.session_root.parent

    assert Path(effective.workspace_dir) == root
    assert Path(effective.workspace_dir) / "sessions" == ctx.session_root
    assert Path(effective.artifact_dir).is_relative_to(root)
    # 其余字段不变（模型链 / 策略旋钮一个都没动）
    assert effective.model_provider == original.model_provider
    assert effective.model_name == original.model_name
    assert effective.local_max_agent_turns == original.local_max_agent_turns
    assert effective.capabilities == original.capabilities
    assert original.workspace_dir == Settings(_env_file=None).workspace_dir, "不得改到原对象"
