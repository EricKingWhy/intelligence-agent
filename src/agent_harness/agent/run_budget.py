"""RunBudget（`#312` T4）：一个**逻辑 run** 的累计 turn 账本 + 暂停/恢复数据面。

语义权威（本模块**不复述**，只引用）：`02 §5.1`（三层控制互不替代、七个 counter 的
定义与计数点）、`02 §5.2`（暂停/恢复、closeout 预留）、`03 §3.4`（`run/paused` /
`run/resumed` 的字段与不变量）、`03 §5`（六值 Run 状态集合）、`11 §6.1`（422/409
与投影）、ADR-0044 D2/D3/D9。

本模块只做三件事：

1. **派生**账本 —— `derive_run_budget(events, run_id)`：version / limits / consumed /
   paused 全部从 append-only 事件算出来，**不建第二份计数器**。重建精确性不是"小心
   维护"出来的，而是"没有第二份真相"这一结构保证的（不变量 #3 / #22）。
2. **数据面** —— `build_pause_data` / `build_resume_data` 产出两个事件的 `data`
   （字段名是 `03 §3.4` 的契约，别在这里发明同义词）。
3. **开工前校验** —— `validate_resume` 的 409/422 判定：被拒请求**不启动**任何
   model / tool / child 工作，也不写消耗预算的事件（`11 §6.1`）。

**`agent_turns` 的计数点只有一个**：`model/completed`（被**接纳进 loop** 的模型决策的 durable
记录）。`model/failed`（拒绝或传输失败的请求）不计数；**closeout 那一次调用也不计数** ——
它是 `model_requests`（`02 §5.1` 明文把 closeout 与 primary/fallback/子 Agent 并列），
把两者记进同一个数就叫"计数混同"，而 `02 §5.1` 的 AC 明文禁止混同（R2 的「closeout 在
ceiling 之内」由**预留**表达：暂停在 ceiling 前一轮成立，closeout 因此有位置可站）。

**本票不实现**（别误以为漏了）：token / cost / deadline / tool quota / stuck /
SessionBudget —— 分别是 `#313` / `#314`（同票工具配额）/ `#315` / `#317` / `#318`。
所以这里只有 turn 一个维度，`run/paused` 的 `reason` 目前只会是 `budget_exhausted`。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agent_harness.agent.budget import (
    BudgetConflict,
    BudgetRejection,
    LocalFuse,
)
from agent_harness.session.event import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    SessionEvent,
)

#: `run/paused.data.reason`：本票唯一会出现的暂停原因（其余两类由 `#315`/`#317` 接）。
REASON_BUDGET_EXHAUSTED = "budget_exhausted"

#: `run/paused.data.trigger_dimension`：命中的是哪一个 turn ceiling。
#: 两个作用域**不可互相替代**（`02 §5.1`），所以投影上必须能分辨是谁到顶。
TRIGGER_RUN_TURNS = "run.max_agent_turns_total"
TRIGGER_LOCAL_TURNS = "local.max_agent_turns"

#: `run/paused.data.closeout_source`（`03 §3.4` 的两值）。
CLOSEOUT_MODEL = "model"
CLOSEOUT_DETERMINISTIC = "deterministic"

#: `run/resumed.data.resume_basis`（`03 §3.4` 的四值）。本票只接受 `budget_increase`：
#: 另外三值（相关 steer / 环境变更 / 策略变更）的**证据判定**属 stuck 暂停（`#317`），
#: 现在收下它们等于假装校验过证据。
RESUME_BASIS_BUDGET_INCREASE = "budget_increase"
RESUME_BASIS_RELEVANT_STEER = "relevant_steer"
RESUME_BASIS_ENVIRONMENT_CHANGE = "environment_change"
RESUME_BASIS_POLICY_CHANGE = "policy_change"
RESUME_BASIS_VALUES: frozenset[str] = frozenset(
    {
        RESUME_BASIS_BUDGET_INCREASE,
        RESUME_BASIS_RELEVANT_STEER,
        RESUME_BASIS_ENVIRONMENT_CHANGE,
        RESUME_BASIS_POLICY_CHANGE,
    }
)

#: 暂停前为 closeout **预留**的 turn 容量（`02 §5.2`「在适用预算内预留容量」，
#: ticket R2「closeout work accounted inside the configured ceiling」）。
#: **只对 run ceiling 生效**（fuse 的临界点为什么不预留，见 `pause_trigger`）。
#: 取 1 = 一次有界机会；`#305` 未固定数字，故这里是本实现的常量并如实投影。
RESERVED_CLOSEOUT_TURNS = 1

#: continuation 的四个键（`03 §3.4`：已完成 / 剩余 / 阻塞 / 下一步安全动作）。
CONTINUATION_LIST_KEYS = ("completed", "remaining", "blockers")
CONTINUATION_ACTION_KEY = "next_safe_action"

#: 单条 continuation 文本上限（模型 closeout 是**有界**机会：不能让一次总结把
#: 事件流撑大，也不能让它决定 runtime 的继续/停止）。
_CONTINUATION_MAX_ITEMS = 20
_CONTINUATION_MAX_CHARS = 500

#: 终态事件（`03 §5`）：出现即该逻辑 run 不再可恢复。
_TERMINAL_TYPES = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED})


@dataclass(frozen=True)
class RunTurnLimits:
    """run 作用域的 turn ceiling（绝对值；`None` = 无 ceiling）。

    `None` **不是** 0：`11 §6.1` 明文「Provider 账目缺失 = unavailable，永不记 0」，
    这里的无 ceiling 同理——投影里它是 `null` + `remaining=null`，不是"还剩 0 轮"。
    """

    max_agent_turns_total: int | None = None

    def as_projection(self) -> dict[str, Any]:
        return {"max_agent_turns_total": self.max_agent_turns_total}


@dataclass(frozen=True)
class PausedRun:
    """一个**处于暂停**的逻辑 run（从事件派生，不是进程本地状态）。"""

    run_id: str
    pause_seq: int
    step_id: int | None
    reason: str
    trigger_dimension: str
    version: int
    consumed_turns: int
    limits: RunTurnLimits
    local_fuse: LocalFuse | None
    continuation: dict[str, Any]
    closeout_source: str
    resume_requirements: tuple[str, ...]
    #: 原 run 在会话里的序号（`run/started.turn_index`）——续跑执行的 Langfuse
    #: 归因沿用同一个序号（不跳号、也不谎报成第 1 轮）。不是暂停事实本身，
    #: 所以**不**进 `as_projection()`（客户端投影不需要它）。
    turn_index: int | None = None

    def as_projection(self) -> dict[str, Any]:
        """客户端可读投影（`11 §6.1` 的 run 作用域：identity / version / ceiling /
        consumed / remaining / 暂停原因 + continuation 引用）。"""
        return {
            "run_id": self.run_id,
            "version": self.version,
            "state": "paused",
            "reason": self.reason,
            "trigger_dimension": self.trigger_dimension,
            "limits": self.limits.as_projection(),
            "local_fuse": self.local_fuse.as_projection() if self.local_fuse else None,
            "consumed": {"agent_turns": self.consumed_turns},
            "remaining": _remaining(self.limits.max_agent_turns_total, self.consumed_turns),
            "continuation": self.continuation,
            "closeout_source": self.closeout_source,
            "resume_requirements": list(self.resume_requirements),
            "pause_seq": self.pause_seq,
        }


@dataclass(frozen=True)
class RunBudgetState:
    """一个逻辑 run 的账本快照（纯派生值；每次调用重算，不缓存）。"""

    run_id: str | None
    version: int
    limits: RunTurnLimits
    consumed_turns: int
    paused: PausedRun | None
    terminal: bool
    turn_index: int | None = None

    @property
    def resumable(self) -> bool:
        return self.paused is not None and not self.terminal


@dataclass(frozen=True)
class LaunchRunBudget:
    """启动一次执行时的 run 账本上下文（runtime 的构造输入，只读）。

    与 `RunBudgetState` 的区别：这是"这次执行开始时账本长什么样"；执行期间的消耗
    仍以 append-only 事件为准（`derive_run_budget` 是唯一真相，本对象不参与记账）。

    `run_id=None` ⇒ 这是一次**新的**逻辑 run（runtime 自己 `begin_run`）；
    非 `None` ⇒ 续跑同一逻辑 run（`#312` 的"恢复沿用同一 run_id"），runtime
    **不**再 `begin_run`，`turn_index` 沿用原 run 的序号（Langfuse 归因不跳号）。
    """

    version: int = 1
    limits: RunTurnLimits = RunTurnLimits()
    consumed_turns: int = 0
    run_id: str | None = None
    turn_index: int | None = None


def _remaining(ceiling: int | None, consumed: int) -> int | None:
    return None if ceiling is None else max(ceiling - consumed, 0)


def _limits_from_projection(raw: Any) -> RunTurnLimits:
    """从事件里的 `limits` 快照还原 run 作用域 ceiling（宽容读：缺键 = 无 ceiling）。"""
    if not isinstance(raw, dict):
        return RunTurnLimits()
    scope = raw.get("run")
    if not isinstance(scope, dict):
        return RunTurnLimits()
    value = scope.get("max_agent_turns_total")
    if isinstance(value, bool) or not isinstance(value, int):
        return RunTurnLimits()
    return RunTurnLimits(max_agent_turns_total=value)


def _local_fuse_from_projection(raw: Any) -> LocalFuse | None:
    scope = raw.get("local") if isinstance(raw, dict) else None
    if not isinstance(scope, dict):
        return None
    turns = scope.get("max_agent_turns")
    if isinstance(turns, bool) or not isinstance(turns, int):
        return None
    source = scope.get("source")
    return LocalFuse(
        max_agent_turns=turns,
        source=source if isinstance(source, str) else "",
    )


def _state_limits(events: list[SessionEvent], run_id: str) -> RunTurnLimits:
    """当前生效的 run ceiling：最后一次 `run/resumed` 的 limits，否则 `run/started`
    落盘的初始值（`session.begin_run(budget=...)`），否则无 ceiling。

    这条顺序就是「恢复提供**绝对** ceiling、不重置 counter」的读法来源：version 变
    发生在 `run/resumed` 上，ceiling 也以它为准。
    """
    limits = RunTurnLimits()
    for event in events:
        if event.run_id != run_id:
            continue
        if event.type == RUN_STARTED:
            limits = _limits_from_projection(event.data.get("budget"))
        elif event.type == RUN_RESUMED:
            limits = _limits_from_projection(event.data.get("limits"))
    return limits


def derive_run_budget(events: Iterable[SessionEvent], run_id: str) -> RunBudgetState:
    """从 append-only 事件派生该逻辑 run 的账本（纯函数，无 IO、无缓存）。

    - `version`：初始 1，每条 `run/resumed` +1（CAS 的比较对象，`03 §3.4`）。
    - `consumed.agent_turns`：`model/completed` 计数（计数点的唯一来源，见模块 docstring）。
    - `paused`：最后一条 `run/paused` 之后**没有** `run/resumed` 也没有终态 → 仍在暂停。
    - `terminal`：出现任一终态事件（`03 §5`：completed / failed / interrupted）。
    """
    items = [event for event in events if event.run_id == run_id]
    version = 1
    consumed = 0
    paused: PausedRun | None = None
    terminal = False
    turn_index: int | None = None
    for event in items:
        if event.type == RUN_STARTED:
            raw_index = event.data.get("turn_index")
            if isinstance(raw_index, int) and not isinstance(raw_index, bool):
                turn_index = raw_index
        elif event.type == MODEL_COMPLETED:
            consumed += 1
        elif event.type == RUN_PAUSED:
            version_after = version
            closeout = event.data.get("closeout_source")
            paused = PausedRun(
                run_id=run_id,
                pause_seq=event.seq,
                step_id=event.step_id,
                reason=str(event.data.get("reason", "")) or REASON_BUDGET_EXHAUSTED,
                trigger_dimension=str(event.data.get("trigger_dimension", "")),
                version=version_after,
                consumed_turns=_consumed_from_projection(
                    event.data.get("consumed"), consumed,
                ),
                limits=_limits_from_projection(event.data.get("limits")),
                local_fuse=_local_fuse_from_projection(event.data.get("limits")),
                continuation=normalize_continuation(event.data.get("continuation")) or {},
                closeout_source=closeout if isinstance(closeout, str) else CLOSEOUT_DETERMINISTIC,
                resume_requirements=_strings(event.data.get("resume_requirements")),
                turn_index=turn_index,
            )
        elif event.type == RUN_RESUMED:
            version += 1
            paused = None
            terminal = False
        elif event.type in _TERMINAL_TYPES:
            terminal = True
    return RunBudgetState(
        run_id=run_id,
        version=version,
        limits=_state_limits(items, run_id),
        consumed_turns=consumed,
        paused=paused,
        terminal=terminal,
        turn_index=turn_index,
    )


def latest_run_id(events: Iterable[SessionEvent]) -> str | None:
    """会话里最后一个逻辑 run 的 id（`run/started` 顺序的最后一个）。"""
    latest: str | None = None
    for event in events:
        if event.type == RUN_STARTED and event.run_id:
            latest = event.run_id
    return latest


def latest_paused_run(events: list[SessionEvent]) -> PausedRun | None:
    """会话里"最新逻辑 run 正处于暂停"时的暂停事实，否则 `None`。

    只认**最新**那个 run：更早的 run 即使留有暂停也不再可恢复（它已被后续 run 取代，
    恢复它等于跳回历史），语义与 RunManager 的「一个会话一个逻辑在途 run」一致。
    """
    run_id = latest_run_id(events)
    if run_id is None:
        return None
    state = derive_run_budget(events, run_id)
    return state.paused


def _consumed_from_projection(raw: Any, fallback: int) -> int:
    """从 `consumed` 快照里取 `agent_turns`（`03 §3.4` 的快照形状：`{"agent_turns": N}`）。

    读不到才回落到"按事件重算"——那条回落只为读到畸形事件而存在（宽容读）。
    正常路径**必须**用快照：它是暂停那一刻的账，而重算值会随
    后续事件继续涨（恢复后的 `model/completed`），拿它当"暂停时的消耗"会让
    `resume_ceiling_ok` 与 `run/resumed.consumed` 一起漂移——那正是"恢复不得重置
    消耗"这条不变量会破的地方。
    """
    if isinstance(raw, dict):
        value = raw.get("agent_turns")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return fallback


def _strings(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


# ── 准入判定（runtime 的**单一**判定点调用） ──────────────────────────────


def pause_trigger(
    *,
    consumed_turns: int,
    run_limits: RunTurnLimits,
    execution_steps: int,
    local_fuse_turns: int,
) -> str | None:
    """这次执行还能不能继续（不能继续时返回命中的维度名）。

    判定用的是**下一轮**的准入（`02 §5.1`：判定发生在任何 model / tool / child 工作
    开始之前）。两个作用域的临界点**不同**，因为两个 counter 的定义不同：

    - **run ceiling**（`run.max_agent_turns_total`，累计账本，客户端配的那个上限）：
      本轮已消耗 + 预留 closeout 容量够到 ceiling 就停 ⇒ 暂停时**还剩**一次 closeout
      的容量（`02 §5.2` 的预留；ticket R2 的「closeout 在 ceiling 之内」）。
    - **local fuse**（单实例保险丝，`02 §5.1`「按**被接纳**的模型决策计数」）：`steps` 够到
      fuse 就停。closeout 不是被接纳的决策 ⇒ 它在 fuse 上不占位，故这里**不**预留 ——
      这正是 EB-2 的临界点，也是 T3 冻结的单一判定结果（本票只改去向，不改临界点）。

    run ceiling 命中时优先报它——它是客户端配的那个上限，可恢复的动作是抬高它。
    """
    ceiling = run_limits.max_agent_turns_total
    if ceiling is not None and consumed_turns + RESERVED_CLOSEOUT_TURNS >= ceiling:
        return TRIGGER_RUN_TURNS
    if execution_steps >= local_fuse_turns:
        return TRIGGER_LOCAL_TURNS
    return None


def closeout_capacity(*, consumed_turns: int, run_limits: RunTurnLimits) -> bool:
    """暂停时还剩不剩 closeout 的容量（剩 ⇒ 允许一次有界模型 closeout）。

    没有容量 ⇒ 只落**确定性** continuation（`02 §5.2`：模型 closeout 不可用时不得
    超出预算去补一次调用）。

    本票的暂停点上这个判据恒为真（`pause_trigger` 保证 `consumed` 严格低于 ceiling，
    没有 ceiling 时更是无上限）——保留它是**不变量的显式表达 + 后续维度的接口**：
    `#313`/`#314` 的 token/cost/deadline 暂停可能发生在 turn 维度之外的边界上，那时
    "还有没有一次调用的余地"就不再显然。删掉它等于把这条判断散回调用点。
    """
    ceiling = run_limits.max_agent_turns_total
    return ceiling is None or consumed_turns < ceiling


def resume_ceiling_ok(*, consumed_turns: int, ceiling: int | None) -> bool:
    """恢复的绝对 ceiling 是否"真的提高了"（`11 §6.1` 的 409 判据之一）。

    判据是**能继续干活**，不是"数字变大"：至少留出「一个可接纳的 turn + 一次 closeout
    预留」——恰好等于 consumed+1 的 ceiling 会在下次准入立刻再次暂停，接受它等于
    让客户端拿到一个"恢复成功但什么都没发生"的假象。

    这是本实现对冻结清单的**收紧**读法（`11 §6.1` 只点名「ceiling 降到已消耗之下 ⇒
    409」）：比该条更严一格，因此不会放过任何冻结文本要求拒绝的请求，只是额外拒绝
    "恢复了但一轮都跑不了"的请求。收紧的边界如实登记在 tracker 的 T4 段（§3.1），
    若产品要放开，改这里一处即可（前端只做展示提示，不做判定）。
    """
    if ceiling is None:
        return False
    return ceiling > consumed_turns + RESERVED_CLOSEOUT_TURNS


def validate_resume(
    paused: PausedRun,
    *,
    run_id: str | None,
    expected_version: int | None,
    ceiling: int | None,
    resume_basis: str | None,
) -> None:
    """恢复请求的**开工前**校验（拒绝 ⇒ 抛领域异常，调用方零副作用）。

    422（形状）与 409（冲突）的分界按 `11 §6.1`：**字段缺 / 值非法**是 422，
    **状态对不上**是 409。所有判定都在这里，端点与 CLI 共用同一份规则
    （单一规则来源，别在 web 层再解释一遍）。

    本票的暂停只可能由预算产生，所以"变更依据"只有一条真实路径：
    `resume_basis=budget_increase` 且新 ceiling 真的能继续（见 `resume_ceiling_ok`）。
    另外三值的**证据判定**（相关 steer / 比快照更新的环境或策略版本）属 `#317`，
    现在接受它们等于假装校验过证据 ⇒ 409 明说"不接受"。
    """
    if not run_id:
        raise BudgetRejection(
            "resume 必须标识被暂停的 run_id（03 §5：恢复沿用同一 run_id，绝不新建）"
        )
    if run_id != paused.run_id:
        raise BudgetConflict(
            f"run_id={run_id} 不是被暂停的 run（暂停的是 {paused.run_id}）"
        )
    if expected_version is None:
        raise BudgetRejection(
            "resume 必须带 expected_version（CAS：03 §3.4）"
        )
    if expected_version != paused.version:
        raise BudgetConflict(
            f"budget version 过期：expected_version={expected_version}，"
            f"当前 version={paused.version}（CAS 比较失败，未启动任何工作）"
        )
    if not resume_basis:
        raise BudgetRejection(
            f"resume 必须声明 resume_basis（03 §3.4：{sorted(RESUME_BASIS_VALUES)}）"
        )
    if resume_basis not in RESUME_BASIS_VALUES:
        raise BudgetRejection(f"未知 resume_basis：{resume_basis!r}")
    if paused.reason != REASON_BUDGET_EXHAUSTED:
        raise BudgetConflict(
            f"暂停原因 reason={paused.reason} 的恢复前置条件本票未实现"
            f"（#315 deadline / #317 stuck 各自负责），拒绝启动工作"
        )
    if resume_basis != RESUME_BASIS_BUDGET_INCREASE:
        raise BudgetConflict(
            f"预算暂停只接受 resume_basis={RESUME_BASIS_BUDGET_INCREASE}："
            f"{resume_basis} 的有效性需要变更证据（属 #317 的责任域），"
            f"本票不假装校验过它"
        )
    if not resume_ceiling_ok(consumed_turns=paused.consumed_turns, ceiling=ceiling):
        raise BudgetConflict(
            f"恢复必须把绝对 ceiling 提高到能继续（consumed={paused.consumed_turns}，"
            f"至少需要 > consumed+{RESERVED_CLOSEOUT_TURNS}），收到 {ceiling!r}"
        )


# ── 事件 data 构造（`03 §3.4` 的字段契约） ────────────────────────────────


def build_limits_snapshot(
    *, run_limits: RunTurnLimits, local_fuse: LocalFuse,
) -> dict[str, Any]:
    """`limits` 快照：两个作用域各还原生投影（`11 §6.1`「按作用域」）。"""
    return {"local": local_fuse.as_projection(), "run": run_limits.as_projection()}


def build_pause_data(
    *,
    reason: str,
    trigger_dimension: str,
    version: int,
    consumed_turns: int,
    limits: dict[str, Any],
    continuation: dict[str, Any],
    closeout_source: str,
    resume_requirements: Iterable[str] = (),
    trace_id: str | None = None,
) -> dict[str, Any]:
    """`run/paused` 的 data（`03 §3.4`：reason / trigger_dimension / version /
    consumed / limits / continuation / closeout_source / resume_requirements）。

    `trace_id` 不在 PRD §5 的必填清单里，但**与各终结臂同源**地带上（ADR-0033 的
    归因面）：暂停也是"本次执行收口"，用户看暂停原因时最想跳的就是那一段 trace。
    **不带 `trace_url`**：URL 由 tracer 在**终态回调**里经 SDK 合成（`RunTracer.
    _finalize_trace_url`），而暂停不调任何终态回调 ⇒ 此刻只能是 None，落一个恒为
    None 的键是假信息；要不要给暂停加一个观测端口回调属 observability 票（残余已
    登记在 tracker）。值恒为 key、可为 None（缺席实现不伪造，`11 §6.1`）。
    """
    return {
        "reason": reason,
        "trigger_dimension": trigger_dimension,
        "budget_version": version,
        "consumed": {"agent_turns": consumed_turns},
        "limits": limits,
        "continuation": continuation,
        "closeout_source": closeout_source,
        "resume_requirements": list(resume_requirements),
        "trace_id": trace_id,
    }


def build_resume_data(
    *,
    from_pause_seq: int,
    previous_version: int,
    version: int,
    limits: dict[str, Any],
    consumed_turns: int,
    resume_basis: str,
) -> dict[str, Any]:
    """`run/resumed` 的 data（`03 §3.4`）。

    `consumed` **等于**暂停快照（恢复不重置、也不预支）：它是"恢复这一刻的账"，
    新工作产生的消耗由后续 `model/completed` 继续累加。
    """
    return {
        "from_pause_seq": from_pause_seq,
        "previous_budget_version": previous_version,
        "budget_version": version,
        "limits": limits,
        "consumed": {"agent_turns": consumed_turns},
        "resume_basis": resume_basis,
    }


def as_run_started_budget(run_limits: RunTurnLimits) -> dict[str, Any] | None:
    """`run/started.data.budget` 的值（**只在显式配了 run ceiling 时**落键）。

    不落键 = "本次 run 没有 run 作用域 ceiling"（`02 §5.1`：RunBudget 除显式配置外无
    ceiling）。缺省不写键还有一个好处：既有会话的事件序列逐字不变（golden 基线只看
    真正新增的事实，不看恒为空的占位）。
    """
    if run_limits.max_agent_turns_total is None:
        return None
    return {"run": run_limits.as_projection()}


# ── continuation（模型 closeout 的解析 / 确定性兜底） ─────────────────────


def normalize_continuation(raw: Any) -> dict[str, Any] | None:
    """规范化 closeout 产出的 continuation；形状不合契约时返回 `None`。

    契约（`03 §3.4`）：`completed` / `remaining` / `blockers` 是字符串列表，
    `next_safe_action` 是非空字符串。**只做规范化，不补齐**——缺键就是无效产出，
    由调用方回落到确定性 continuation（不伪造进展）。
    """
    if not isinstance(raw, dict):
        return None
    normalized: dict[str, Any] = {}
    for key in CONTINUATION_LIST_KEYS:
        value = raw.get(key)
        if not isinstance(value, (list, tuple)):
            return None
        items: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                return None
            items.append(item.strip()[:_CONTINUATION_MAX_CHARS])
            if len(items) >= _CONTINUATION_MAX_ITEMS:
                break
        normalized[key] = items
    action = raw.get(CONTINUATION_ACTION_KEY)
    if not isinstance(action, str) or not action.strip():
        return None
    normalized[CONTINUATION_ACTION_KEY] = action.strip()[:_CONTINUATION_MAX_CHARS]
    return normalized


def deterministic_continuation(
    *,
    events: Iterable[SessionEvent],
    run_id: str,
    trigger_dimension: str,
    ceiling: int | None,
    consumed_turns: int,
) -> dict[str, Any]:
    """确定性 continuation：**只**用已持久化事实组装（`02 §5.2`）。

    允许的事实：计数（轮数 / 工具调用数 / 工具结果数）与 ceiling 本身。
    **不允许**：把"工具调用已发出"说成"工具已成功"、编造进展、编造工具结果
    （`02 §5.2` / ADR-0044 D3）。所以 `remaining` 里只说"停止发生在什么之前"，
    具体待办由恢复后的模型自己从历史里看见。
    """
    items = [event for event in events if event.run_id == run_id]
    tool_calls = sum(1 for event in items if event.type == "tool/call")
    tool_results = sum(1 for event in items if event.type == "tool/result")
    ceiling_text = "无 ceiling" if ceiling is None else str(ceiling)
    action = (
        f"提高绝对 ceiling（{TRIGGER_RUN_TURNS}）后以同一 run_id 恢复："
        f"当前 consumed={consumed_turns}，ceiling={ceiling_text}；"
        f"恢复请求需带 expected_version 与 resume_basis={RESUME_BASIS_BUDGET_INCREASE}"
    )
    return {
        "completed": [
            f"本逻辑 run 已消耗 {consumed_turns} 个 agent turn",
            f"已接纳 {tool_calls} 个工具调用（其中 {tool_results} 个已落工具结果）",
        ],
        "remaining": [
            "暂停发生在下一轮模型决策之前：恢复后由模型从会话历史继续",
        ],
        "blockers": [
            f"{trigger_dimension} 到顶：consumed={consumed_turns}, ceiling={ceiling_text}",
        ],
        CONTINUATION_ACTION_KEY: action,
    }
