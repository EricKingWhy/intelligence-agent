"""RunBudget（`#312` T4 建账本，`#313` T5 扩到四维）：一个**逻辑 run** 的累计账本
+ 暂停/恢复数据面。

语义权威（本模块**不复述**，只引用）：`02 §5.1`（三层控制互不替代、七个 counter 的
定义与计数点）、`02 §5.2`（暂停/恢复、closeout 预留）、`03 §3.4`（`run/paused` /
`run/resumed` 的字段与不变量）、`03 §5`（六值 Run 状态集合）、`11 §6.1`（422/409
与投影）、ADR-0044 D2/D3/D9。

本模块只做四件事：

1. **派生**账本 —— `derive_run_budget(events, run_id)`：version / limits / consumed /
   paused 全部从 append-only 事件算出来，**不建第二份计数器**。重建精确性不是"小心
   维护"出来的，而是"没有第二份真相"这一结构保证的（不变量 #3 / #22）。
2. **数据面** —— `build_pause_data` / `build_resume_data` 产出两个事件的 `data`
   （字段名是 `03 §3.4` 的契约，别在这里发明同义词）。
3. **开工前校验** —— `validate_ceiling_enforceability`（422：这条 Provider 链能不能
   强制这个维度）与 `validate_resume`（409：状态与新 cap 对不上）。被拒请求**不启动**
   任何 model / tool / child 工作，也不写消耗预算的事件（`11 §6.1`）。
4. **准入判定** —— `pause_trigger` / `closeout_capacity`：run 累计 ceiling 与 local
   fuse 在同一次判定里比，命中哪个维度如实回传（不合并成一个计数器）。

**计数点各自只有一个**（`02 §5.1`，混同即违约）：

- `agent_turns` ← `model/completed`（被**接纳进 loop** 的模型决策的 durable 记录）。
- `model_requests` ← `model/request`（每一次**实际** Provider 请求恰一条：primary /
  fallback / closeout 各一条；被拒绝或传输失败的请求**也在**其中）。
- `total_tokens` / `cost_usd` ← 同一条事件的 `usage` / `cost_usd` 键，**只统计 Provider
  自报的值**。任何一个请求没自报 ⇒ 该维度的累计是"未知"（`None`），不是 0
  （`11 §6.1`：不可得 = unavailable，MUST NOT 记为 0；只有"一个请求都没有"的空和才是 0）。

**四维的临界点语义分两类**（可数维度预留 closeout、计量维度到线即停）：判据与理由
写在 `_dimension_reached` / `_dimension_headroom` 的 docstring 里，本模块不复述第二遍
（准入与 closeout 容量是**两个**谓词，合并会让暂停点上的收口恒被拒）。

**本模块不实现**（别误以为漏了）：deadline（`#315`）、tool quota（`#314`）、stuck
（`#317`）、SessionBudget（`#318`）。所以 `run/paused.data.reason` 目前只会是
`budget_exhausted`——它是"预算到顶"这一类，**具体哪个维度看 `trigger_dimension`**。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from agent_harness.agent.budget import (
    BudgetConflict,
    BudgetRejection,
    LocalFuse,
)
from agent_harness.model.accounting import ProviderAccounting, decimal_or_none
from agent_harness.session.event import (
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

#: `run/paused.data.reason`：本票唯一会出现的暂停原因（其余两类由 `#315`/`#317` 接）。
REASON_BUDGET_EXHAUSTED = "budget_exhausted"

#: `run/paused.data.trigger_dimension`：命中的是哪一个 ceiling。
#: 各作用域 / 各维度**不可互相替代**（`02 §5.1`），所以投影上必须能分辨是谁到顶。
#: 取值就是**配置字段的路径名**（客户端据此知道该抬高哪一个 ceil）——不发明别名。
TRIGGER_RUN_TURNS = "run.max_agent_turns_total"
TRIGGER_RUN_REQUESTS = "run.max_model_requests"
TRIGGER_RUN_TOKENS = "run.max_total_tokens"
TRIGGER_RUN_COST = "run.max_cost_usd"
TRIGGER_LOCAL_TURNS = "local.max_agent_turns"

#: 准入判定的顺序（命中即返回，**只报一个**维度）。turns 在最前：它是
#: `#308` 起就存在的维度、也是客户端最先配的那个；同一时刻多维度同时到顶时，
#: 报哪一个不影响客户端的动作（抬高全部到顶的 ceiling 才能继续），所以顺序
#: 只需**确定**、不需要"最紧优先"这种需要全局比较的聪明规则。
TRIGGER_ORDER: tuple[str, ...] = (
    TRIGGER_RUN_TURNS,
    TRIGGER_RUN_REQUESTS,
    TRIGGER_RUN_TOKENS,
    TRIGGER_RUN_COST,
    TRIGGER_LOCAL_TURNS,
)

#: run 作用域的四个维度（= `TRIGGER_ORDER` 去掉 local fuse 的**同一份事实**，
#: 不是第二张清单：恢复判定与 closeout 容量都只看这四维）。
RUN_DIMENSIONS: tuple[str, ...] = TRIGGER_ORDER[:4]

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

#: 同一条预留，落在 `model_requests` 维度上（`#313` T5）。closeout 本身**就是**一次
#: Provider 请求（`02 §5.1` 把 closeout 与 primary/fallback 并列），所以 requests
#: 的 ceiling 必须像 turns 那样给它留一格 —— 否则"closeout 在预算之内"这句话在
#: requests 维度上会变成假话（暂停时 requests 已用满，closeout 只能越线）。
RESERVED_CLOSEOUT_REQUESTS = 1

#: continuation 的四个键（`03 §3.4`：已完成 / 剩余 / 阻塞 / 下一步安全动作）。
CONTINUATION_LIST_KEYS = ("completed", "remaining", "blockers")
CONTINUATION_ACTION_KEY = "next_safe_action"

#: 单条 continuation 文本上限（模型 closeout 是**有界**机会：不能让一次总结把
#: 事件流撑大，也不能让它决定 runtime 的继续/停止）。
_CONTINUATION_MAX_ITEMS = 20
_CONTINUATION_MAX_CHARS = 500

#: 终态事件（`03 §5`）：出现即该逻辑 run 不再可恢复。
_TERMINAL_TYPES = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED})

#: 终态事件 → `03 §5` 状态词表里的名字（事件类型是路径，状态名是词表项，别混用）。
_TERMINAL_STATE = {
    RUN_COMPLETED: "completed",
    RUN_FAILED: "failed",
    RUN_INTERRUPTED: "interrupted",
}


def _decimal_text(value: Decimal | None) -> str | None:
    """`Decimal` → wire 上的十进制**字符串**（`None` 原样）。

    成本不能进 JSON 浮点：`Decimal` 本身 `json.dumps` 会炸，而 `float()` 会引入与
    wire 不等价的二进制近似（`11 §6.1`：**二进制浮点相等不是契约**）。所以这一维
    在事件与投影里一律是十进制字符串，算术只在 `Decimal` 里做。
    """
    return None if value is None else format(value, "f")


def _decimal_or_none(raw: Any) -> Decimal | None:
    """读回十进制成本：**同一条规则**在 `model/accounting.decimal_or_none`（唯一实现）。

    Provider 响应里的成本与事件 / 投影里读回的成本必须用同一条宽容读法；这里只是
    给本模块的调用点一个短名字，规则本身不在这里再写一遍。
    """
    return decimal_or_none(raw)


@dataclass(frozen=True)
class RunLimits:
    """run 作用域的 ceiling（**绝对值**；`None` = 该维度无 ceiling）。

    `None` **不是** 0：`11 §6.1` 明文「Provider 账目缺失 = unavailable，永不记 0」，
    这里的无 ceiling 同理——投影里它是 `null` + `remaining=null`，不是"还剩 0"。

    四个维度都可能有值、也可能都没有（= 本 run 没配 run 作用域预算）。`#313` 起
    它同时是 `run/started.data.budget`、`run/paused` / `run/resumed` 的 `limits`
    快照的形状——**快照的六个维度里** ToolRetry / deadline / tool quota 三席分别
    由 `#314` / `#315` 与 tool quota 票接入（它们加键，不改本类的读法）。
    """

    max_agent_turns_total: int | None = None
    max_model_requests: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: Decimal | None = None

    @property
    def configured(self) -> bool:
        """有没有配任何一维（决定要不要在事件里落这个快照）。"""
        return any(
            value is not None
            for value in (
                self.max_agent_turns_total,
                self.max_model_requests,
                self.max_total_tokens,
                self.max_cost_usd,
            )
        )

    def ceiling_of(self, dimension: str) -> Any:
        """按 `trigger_dimension` 取值（准入判定与 continuation 文案共用一份映射）。"""
        return {
            TRIGGER_RUN_TURNS: self.max_agent_turns_total,
            TRIGGER_RUN_REQUESTS: self.max_model_requests,
            TRIGGER_RUN_TOKENS: self.max_total_tokens,
            TRIGGER_RUN_COST: self.max_cost_usd,
        }.get(dimension)

    def as_projection(self) -> dict[str, Any]:
        """客户端可读投影：**四维全在**，没配的那一维是 `null`。

        与 `as_run_started_budget` 的"一维都没配就不落键"不矛盾：那里是"这次 run
        没有 run 预算事实"（落一个全 null 的对象是占位噪声），这里是"客户端问
        ceiling 是什么"——缺键会让客户端分不清"没配"与"这个维度不存在"。
        """
        return {
            "max_agent_turns_total": self.max_agent_turns_total,
            "max_model_requests": self.max_model_requests,
            "max_total_tokens": self.max_total_tokens,
            "max_cost_usd": _decimal_text(self.max_cost_usd),
        }


@dataclass(frozen=True)
class BudgetConsumed:
    """run 作用域已消耗的账（**派生值**：全部来自 append-only 事件）。

    `total_tokens` / `cost_usd` 的 `None` = **未知**（该 run 里有请求没自报这个维度），
    与 0 是两件事：0 = "确实花了 0"，`None` = "不知道花了多少"。未知的维度上不能
    接受 ceiling（`validate_ceiling_enforceability` 的姊妹判定见 `validate_resume`：
    基数未知 ⇒ 409），因为"到线即停"在未知基数上无法成立。
    """

    agent_turns: int = 0
    #: `None` = 未知（T5 之前的暂停快照里没有这个键）。运行时算出来的总是整数。
    model_requests: int | None = 0
    total_tokens: int | None = 0
    cost_usd: Decimal | None = Decimal(0)

    def as_projection(self) -> dict[str, Any]:
        return {
            "agent_turns": self.agent_turns,
            "model_requests": self.model_requests,
            "total_tokens": self.total_tokens,
            "cost_usd": _decimal_text(self.cost_usd),
        }

    def remaining(self, limits: RunLimits) -> dict[str, Any]:
        """各维度的剩余量（`11 §6.1` 要求投影里既有 ceiling 也有 remaining）。"""
        return {
            "agent_turns": _remaining(limits.max_agent_turns_total, self.agent_turns),
            "model_requests": _remaining(limits.max_model_requests, self.model_requests),
            "total_tokens": _remaining(limits.max_total_tokens, self.total_tokens),
            "cost_usd": _decimal_text(
                None
                if limits.max_cost_usd is None or self.cost_usd is None
                else max(limits.max_cost_usd - self.cost_usd, Decimal(0))
            ),
        }

    def with_usage(self, *, usage: dict[str, int] | None, cost: Decimal | None) -> BudgetConsumed:
        """叠加一次请求的账目（内存态聚合用；durable 账仍以事件为准）。

        任一次请求缺该维度 ⇒ 该维度转**未知**并保持未知（`None` 会粘住：一旦不知道
        总和里缺了多少，后面的加数再精确也补不回来）。
        """
        tokens = self.total_tokens
        if tokens is not None:
            if usage is None or "total_tokens" not in usage:
                tokens = None
            else:
                tokens += usage["total_tokens"]
        total_cost = self.cost_usd
        if total_cost is not None:
            total_cost = None if cost is None else total_cost + cost
        requests = self.model_requests
        if requests is not None:
            requests += 1
        return BudgetConsumed(
            agent_turns=self.agent_turns,
            model_requests=requests,
            total_tokens=tokens,
            cost_usd=total_cost,
        )


@dataclass(frozen=True)
class PausedRun:
    """一个**处于暂停**的逻辑 run（从事件派生，不是进程本地状态）。"""

    run_id: str
    pause_seq: int
    step_id: int | None
    reason: str
    trigger_dimension: str
    version: int
    consumed: BudgetConsumed
    limits: RunLimits
    local_fuse: LocalFuse | None
    continuation: dict[str, Any]
    closeout_source: str
    resume_requirements: tuple[str, ...]
    #: 原 run 在会话里的序号（`run/started.turn_index`）——续跑执行的 Langfuse
    #: 归因沿用同一个序号（不跳号、也不谎报成第 1 轮）。不是暂停事实本身，
    #: 所以**不**进 `as_projection()`（客户端投影不需要它）。
    turn_index: int | None = None

    @property
    def consumed_turns(self) -> int:
        """turn 维度的消耗（T4 的调用点与用例读它；唯一真相仍是 `consumed`）。"""
        return self.consumed.agent_turns

    def as_projection(self) -> dict[str, Any]:
        """客户端可读投影（`11 §6.1` 的 run 作用域：identity / version / ceiling /
        consumed / remaining / 暂停原因 + continuation 引用）。

        `consumed` 与 `remaining` 都是**四维全在**的对象：某维度不可耗时是 `null`
        （"不知道"），不是 0——前者让客户端能区分"没花"与"没数"，后者会变成假话。
        """
        return {
            "run_id": self.run_id,
            "version": self.version,
            "state": "paused",
            "reason": self.reason,
            "trigger_dimension": self.trigger_dimension,
            "limits": self.limits.as_projection(),
            "local_fuse": self.local_fuse.as_projection() if self.local_fuse else None,
            "consumed": self.consumed.as_projection(),
            "remaining": self.consumed.remaining(self.limits),
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
    limits: RunLimits
    consumed: BudgetConsumed
    paused: PausedRun | None
    #: 终态事件**自己的**状态名（`03 §5` 词表：`completed` / `failed` / `interrupted`），
    #: 非终态为 `None`。存状态名而不是布尔：客户端投影要按冻结词表回报状态，布尔会逼
    #: 调用方回头去事件里找是哪一种终态——那就是第二份真相。
    terminal_type: str | None = None
    turn_index: int | None = None

    @property
    def consumed_turns(self) -> int:
        """turn 维度的消耗（见 `PausedRun.consumed_turns`）。"""
        return self.consumed.agent_turns

    @property
    def terminal(self) -> bool:
        return self.terminal_type is not None

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

    `consumed`（`#313`）：续跑时 = 暂停快照的账（恢复不重置、也不预支）；新 run =
    空账。runtime 用它的**副本**在内存里推进（每次请求 +1 / 累加 usage），但裁决
    仍只看 append-only 事件（`derive_run_budget`）——内存态只用于同一执行内的准入，
    崩溃重建时由事件重算，两者在"每次请求都落一条 `model/request`"上等价。
    """

    version: int = 1
    limits: RunLimits = RunLimits()
    consumed: BudgetConsumed = BudgetConsumed()
    run_id: str | None = None
    turn_index: int | None = None

    @property
    def consumed_turns(self) -> int:
        """turn 维度的消耗（同 `PausedRun.consumed_turns`）。"""
        return self.consumed.agent_turns


def _remaining(ceiling: int | None, consumed: int | None) -> int | None:
    """剩余量：无 ceiling 或**账目未知**都返回 `None`（未知不是 0，见 `BudgetConsumed`）。"""
    if ceiling is None or consumed is None:
        return None
    return max(ceiling - consumed, 0)


def _limits_from_projection(raw: Any) -> RunLimits:
    """从事件里的 `limits` 快照还原 run 作用域 ceiling（宽容读：缺键 = 该维无 ceiling）。

    逐维独立读：某一维畸形**不**连坐其余维度（读到畸形键就当它没配，其余照收）。
    """
    scope = raw.get("run") if isinstance(raw, dict) else None
    if not isinstance(scope, dict):
        return RunLimits()
    turns = scope.get("max_agent_turns_total")
    requests = scope.get("max_model_requests")
    tokens = scope.get("max_total_tokens")
    return RunLimits(
        max_agent_turns_total=(
            turns if isinstance(turns, int) and not isinstance(turns, bool) else None
        ),
        max_model_requests=(
            requests if isinstance(requests, int) and not isinstance(requests, bool) else None
        ),
        max_total_tokens=(
            tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None
        ),
        max_cost_usd=_decimal_or_none(scope.get("max_cost_usd")),
    )


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


def _state_limits(events: list[SessionEvent], run_id: str) -> RunLimits:
    """当前生效的 run ceiling：最后一次 `run/resumed` 的 limits，否则 `run/started`
    落盘的初始值（`session.begin_run(budget=...)`），否则无 ceiling。

    这条顺序就是「恢复提供**绝对** ceiling、不重置 counter」的读法来源：version 变
    发生在 `run/resumed` 上，ceiling 也以它为准。
    """
    limits = RunLimits()
    for event in events:
        if event.run_id != run_id:
            continue
        if event.type == RUN_STARTED:
            limits = _limits_from_projection(event.data.get("budget"))
        elif event.type == RUN_RESUMED:
            limits = _limits_from_projection(event.data.get("limits"))
    return limits


def add_consumed(base: BudgetConsumed, delta: BudgetConsumed) -> BudgetConsumed:
    """两个账目相加（`None` **粘性**：任一未知 ⇒ 和未知）。

    runtime 的准入判定用它把"启动时的账"（续跑继承的暂停快照）与"本次执行新发生的账"
    （同一份 `consumed_from_events` 从执行起点重算）合成逻辑 run 的累计值——合成的
    两侧都是事件派生值，所以这里不引入第二份真相。
    """
    return BudgetConsumed(
        agent_turns=base.agent_turns + delta.agent_turns,
        model_requests=(
            None if base.model_requests is None or delta.model_requests is None
            else base.model_requests + delta.model_requests
        ),
        total_tokens=(
            None if base.total_tokens is None or delta.total_tokens is None
            else base.total_tokens + delta.total_tokens
        ),
        cost_usd=(
            None if base.cost_usd is None or delta.cost_usd is None
            else base.cost_usd + delta.cost_usd
        ),
    )


def consumed_from_events(events: Iterable[SessionEvent]) -> BudgetConsumed:
    """按事件类型累计四个维度（**唯一的计数实现**，见模块 docstring 的计数点表）。

    `model/request` 的每一次都是一格 `model_requests`，并把该次自报的
    `usage.total_tokens` / `cost_usd` 累加进对应维度；任一格缺该维度 ⇒ 该维度转
    `None`（未知）并**保持**未知——`None` 会粘住：总和里缺了一项之后，后面的加数
    再精确也补不回"已经不知道的那一部分"。
    """
    turns = 0
    requests = 0
    tokens: int | None = 0
    cost: Decimal | None = Decimal(0)
    for event in events:
        if event.type == MODEL_COMPLETED:
            turns += 1
        elif event.type == MODEL_REQUEST:
            requests += 1
            usage = event.data.get("usage")
            reported_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
            if tokens is not None:
                if isinstance(reported_tokens, int) and not isinstance(reported_tokens, bool):
                    tokens += reported_tokens
                else:
                    tokens = None
            if cost is not None:
                reported_cost = _decimal_or_none(event.data.get("cost_usd"))
                cost = None if reported_cost is None else cost + reported_cost
    return BudgetConsumed(
        agent_turns=turns, model_requests=requests,
        total_tokens=tokens, cost_usd=cost,
    )


def derive_run_budget(events: Iterable[SessionEvent], run_id: str) -> RunBudgetState:
    """从 append-only 事件派生该逻辑 run 的账本（纯函数，无 IO、无缓存）。

    - `version`：初始 1，每条 `run/resumed` +1（CAS 的比较对象，`03 §3.4`）。
    - `consumed`：四维各自按计数点累计（`consumed_from_events`）。
    - `paused`：最后一条 `run/paused` 之后**没有** `run/resumed` **也没有终态** → 仍在
      暂停（终态压过暂停，见下）。
    - `terminal_type`：终态事件的状态名（`03 §5`：`completed` / `failed` / `interrupted`）。
    """
    items = [event for event in events if event.run_id == run_id]
    version = 1
    turns_so_far = 0
    paused: PausedRun | None = None
    terminal_type: str | None = None
    turn_index: int | None = None
    for event in items:
        if event.type == RUN_STARTED:
            raw_index = event.data.get("turn_index")
            if isinstance(raw_index, int) and not isinstance(raw_index, bool):
                turn_index = raw_index
        elif event.type == MODEL_COMPLETED:
            turns_so_far += 1
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
                consumed=_consumed_from_projection(
                    event.data.get("consumed"), fallback_turns=turns_so_far,
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
            terminal_type = None
        elif event.type in _TERMINAL_TYPES:
            terminal_type = _TERMINAL_STATE[event.type]
            # 终态**压过**暂停（`03 §5`：终态与 paused 互斥）。事件顺序是判据：
            # 暂停只收口"当前这段执行区间"，其后若真落了终态事件，这个 run 就已经
            # 结束了——留着 `paused` 会让投影同时说"等恢复"与"已失败"
            # （`state=paused` 配 `terminal_type=failed`、`resumable=False`），
            # 客户端按 `state` 判可恢复性就会给一个已失败的 run 亮恢复入口。
            # 今天没有生产写入点会走这条（暂停后没有活着的循环可被取消，interrupt
            # 扫描也在 `run/paused` 处收口），但对账链一接进来它就是第一个踩到的边界。
            paused = None
    return RunBudgetState(
        run_id=run_id,
        version=version,
        limits=_state_limits(items, run_id),
        consumed=consumed_from_events(items),
        paused=paused,
        terminal_type=terminal_type,
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


def _consumed_from_projection(raw: Any, *, fallback_turns: int) -> BudgetConsumed:
    """从 `consumed` 快照还原**暂停那一刻**的账（`03 §3.4` 的快照形状）。

    正常路径**必须**用快照：它是暂停那一刻的账，而重算值会随后续事件继续涨
    （恢复后的 `model/completed` / `model/request`），拿它当"暂停时的消耗"会让
    `resume_headroom_ok` 与 `run/resumed.consumed` 一起漂移——那正是"恢复不得重置
    消耗"这条不变量会破的地方。

    两类键的缺失语义不同（这个区别就是本函数的全部要点）：

    - `agent_turns` 缺失 ⇒ 回落到"按事件重算"（`fallback_turns`）。T4 时代的快照
      只有这一个键，而 turn 在那时候就已经有明确定义 ⇒ 重算是**准确**的。
    - `model_requests` / `total_tokens` / `cost_usd` 缺失 ⇒ **未知（`None`）**，绝不
      回落重算：T5 之前的事件流里根本没有 `model/request`，重算得到的 0 是一句
      假话（那些 run 确实发过请求）。"未知"会让该维度的 ceiling 在恢复时被拒
      （409，见 `validate_resume`），这正是它该有的效果。
    """
    if not isinstance(raw, dict):
        return BudgetConsumed(
            agent_turns=fallback_turns, model_requests=None,
            total_tokens=None, cost_usd=None,
        )
    turns = raw.get("agent_turns")
    requests = raw.get("model_requests")
    tokens = raw.get("total_tokens")
    return BudgetConsumed(
        agent_turns=(
            turns if isinstance(turns, int) and not isinstance(turns, bool)
            else fallback_turns
        ),
        model_requests=(
            requests if isinstance(requests, int) and not isinstance(requests, bool)
            else None
        ),
        total_tokens=(
            tokens if isinstance(tokens, int) and not isinstance(tokens, bool)
            else None
        ),
        cost_usd=_decimal_or_none(raw.get("cost_usd")) if "cost_usd" in raw else None,
    )


def _strings(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


# ── 准入判定（runtime 的**单一**判定点调用） ──────────────────────────────


def pause_trigger(
    *,
    consumed: BudgetConsumed,
    run_limits: RunLimits,
    execution_steps: int,
    local_fuse_turns: int,
) -> str | None:
    """这次执行还能不能继续（不能继续时返回命中的维度名）。

    判定用的是**下一轮**的准入（`02 §5.1`：判定发生在任何 model / tool / child 工作
    开始之前）。各维度 / 各作用域的临界点**不同**，因为它们的计数定义不同：

    - **turns / requests**（可数的"一次"）：本轮已消耗 + **预留的 closeout 容量**
      够到 ceiling 就停 ⇒ 暂停时还剩一次 closeout 的位置（`02 §5.2` 的预留；
      ticket R2 的「closeout 在 ceiling 之内」）。
    - **tokens / cost**（下一轮多大不可预知）：`consumed >= ceiling` 才停——不能在
      未知的下一轮大小上做预留，能保证的只有"**不有意**越线"。
    - **local fuse**（单实例保险丝，`02 §5.1`「按**被接纳**的模型决策计数」）：`steps` 够到
      fuse 就停。closeout 不是被接纳的决策 ⇒ 它在 fuse 上不占位，故这里**不**预留 ——
      这正是 EB-2 的临界点，也是 T3 冻结的单一判定结果（T4 只改去向，不改临界点）。

    账目**未知**（`None`）而该维度又配了 ceiling ⇒ 停：无法证明自己在预算之内时继续
    发起请求，就是"有意越线"。顺序由 `TRIGGER_ORDER` 固定（turns 最先），命中即返回。
    """
    for dimension in TRIGGER_ORDER:
        if _dimension_reached(dimension, consumed=consumed, limits=run_limits):
            return dimension
    if execution_steps >= local_fuse_turns:
        return TRIGGER_LOCAL_TURNS
    return None


def _dimension_reached(
    dimension: str, *, consumed: BudgetConsumed, limits: RunLimits,
) -> bool:
    """单个 run 维度是否禁止**再开始一轮工作**（`pause_trigger` / `validate_resume` 共用）。

    这是**准入**问题（"要不要停下来"），不是"还能不能花一点"。两者对 turns / requests
    恰好差一格：准入含 closeout 预留，而 closeout 自己的容量判定不含（见
    `_dimension_headroom`）。把两个问题合成一个谓词会让暂停点上的模型 closeout 恒被
    拒（`#313` 实现期实测踩到：`test_closeout_capacity_*` 与 pause/resume 的
    `closeout_source=model` 同时红）——那正是 T4 把这个函数单独留下的原因。
    """
    if dimension == TRIGGER_RUN_TURNS:
        ceiling = limits.max_agent_turns_total
        return ceiling is not None and consumed.agent_turns + RESERVED_CLOSEOUT_TURNS >= ceiling
    if dimension == TRIGGER_RUN_REQUESTS:
        ceiling = limits.max_model_requests
        if ceiling is None:
            return False
        if consumed.model_requests is None:
            return True
        return consumed.model_requests + RESERVED_CLOSEOUT_REQUESTS >= ceiling
    if dimension == TRIGGER_RUN_TOKENS:
        ceiling = limits.max_total_tokens
        if ceiling is None:
            return False
        return consumed.total_tokens is None or consumed.total_tokens >= ceiling
    if dimension == TRIGGER_RUN_COST:
        ceiling = limits.max_cost_usd
        if ceiling is None:
            return False
        return consumed.cost_usd is None or consumed.cost_usd >= ceiling
    return False


def _dimension_headroom(
    dimension: str, *, consumed: BudgetConsumed, limits: RunLimits,
) -> bool:
    """该维还剩**一点**余量：还能再花一次（一次请求 / 一个 token / 一分钱）而**不有意**越线。

    四维共用一条式子：`consumed < ceiling`。可数维度（turns / requests）的一个单位是
    "一次"，所以"再放一次不越线"就是 `consumed + 1 <= ceiling`，与 `consumed < ceiling`
    等价；计量维度（tokens / cost）下一轮多大不可预知，`consumed < ceiling` 就是能保证的
    全部。没配 ceiling ⇒ 该维不设限 ⇒ 有余量。账目**未知** ⇒ **没有**余量：在不知道已花
    多少的前提下声称"还能再花一点仍在预算内"是一句无法兑现的话。
    """
    if dimension == TRIGGER_RUN_TURNS:
        ceiling = limits.max_agent_turns_total
        return ceiling is None or consumed.agent_turns < ceiling
    if dimension == TRIGGER_RUN_REQUESTS:
        ceiling = limits.max_model_requests
        if ceiling is None:
            return True
        return consumed.model_requests is not None and consumed.model_requests < ceiling
    if dimension == TRIGGER_RUN_TOKENS:
        ceiling = limits.max_total_tokens
        if ceiling is None:
            return True
        return consumed.total_tokens is not None and consumed.total_tokens < ceiling
    if dimension == TRIGGER_RUN_COST:
        ceiling = limits.max_cost_usd
        if ceiling is None:
            return True
        return consumed.cost_usd is not None and consumed.cost_usd < ceiling
    return True


def closeout_capacity(*, consumed: BudgetConsumed, run_limits: RunLimits) -> bool:
    """暂停时还剩不剩 closeout 的容量（剩 ⇒ 允许一次有界模型 closeout）。

    没有容量 ⇒ 只落**确定性** continuation（`02 §5.2`：模型 closeout 不可用时不得
    超出预算去补一次调用）。

    判据是"**每一维**都还有一点余量"（`_dimension_headroom`）：closeout 也是一次真实
    请求、也花 token 与钱，任何一维已经到线/越线/账目未知，都不能再发它。与准入判定
    的分工见 `_dimension_reached` 的 docstring——T4 时这个判据在暂停点上恒为真，
    `#313` 起不再恒真（token / cost 的暂停可能恰好落在已到线的那一轮，或账目未知），
    这正是它当初被保留下来的理由。
    """
    return all(
        _dimension_headroom(dimension, consumed=consumed, limits=run_limits)
        for dimension in RUN_DIMENSIONS
    )



def resume_headroom_ok(*, consumed: BudgetConsumed, limits: RunLimits) -> bool:
    """恢复的绝对 ceiling 是否"真的能继续"（`11 §6.1` 的 409 判据之一）。

    判据是**能继续干活**，不是"数字变大"：对**每一维**配了 ceiling 的维度，都要求
    它至少放得下"一次新的准入"——turns / requests 要留出「一次决策 + 一次 closeout
    预留」，tokens / cost 只要严格大于已消耗。恰好等于 consumed 的 ceiling 会在下次
    准入立刻再次暂停，接受它等于让客户端拿到一个"恢复成功但什么都没发生"的假象。

    账目**未知**而该维度配了 ceiling ⇒ 判定为"不能继续"（无法证明在预算内）。

    **没配任何一维时这里返回 True**（没有可检查的维度）——"恢复必须至少给一个
    ceiling"是 `validate_resume` 的判定（那里才区分"没给"与"给了但不够"），
    本函数只管"给了的那些够不够"。

    这是本实现对冻结清单的**收紧**读法（`11 §6.1` 只点名「ceiling 降到已消耗之下 ⇒
    409」）：比该条更严一格，因此不会放过任何冻结文本要求拒绝的请求，只是额外拒绝
    "恢复了但一轮都跑不了"的请求。收紧的边界如实登记在 tracker（T4 段与 T5 段各一条），
    若产品要放开，改这里一处即可（前端只做展示提示，不做判定）。
    """
    return not any(
        _dimension_reached(dimension, consumed=consumed, limits=limits)
        for dimension in RUN_DIMENSIONS  # local fuse 不是"绝对 ceiling"，恢复不改它
    )


def validate_ceiling_enforceability(
    limits: RunLimits, accounting: ProviderAccounting,
) -> None:
    """显式 ceiling 在本链**能不能被强制执行**（`11 §6.1` 的 422 判据之一）。

    判定发生在**第一次 Provider 请求之前**（零副作用），依据是集成的
    **能力声明**（`ProviderAccounting`）而不是事后看响应——"这条链会不会自报
    usage / cost"只能由集成方回答，探测它就得先发一次请求，那正是 422 要避免的。

    不接受"一个永远不会触发的 ceiling"（ADR-0044 D1/D8：不静默截断、也不给假象）：
    声明不支持 ⇒ 显式配它是**请求本身**有问题（422），不是运行期再降级。
    """
    if limits.max_total_tokens is not None and not accounting.reports_usage:
        raise BudgetRejection(
            "budget.run.max_total_tokens 在本链无法强制执行："
            "当前 Provider 集成不自报 token usage（11 §6.1）；请去掉它"
        )
    if limits.max_cost_usd is not None and not accounting.reports_cost:
        raise BudgetRejection(
            "budget.run.max_cost_usd 在本链无法强制执行："
            "当前 Provider 集成不自报归属成本（11 §6.1：不臆造费率表）；请去掉它"
        )


def parse_cost_ceiling(raw: Any) -> Decimal | None:
    """`budget.run.max_cost_usd` 的形态校验（**422**；`11 §6.1`：字段值非法 ⇒ 422）。

    wire 上成本是十进制（字符串最稳，数也收——`11 §6.1`「二进制浮点相等不是契约」）；
    形状不合 / 负数 / NaN / Inf 一律**拒绝整个请求**，不做"当作没配"的静默截断
    （ADR-0044 D1/D8：不接受无法执行的 ceiling，也不给客户端一个"配了但没生效"的假象）。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise BudgetRejection("budget.run.max_cost_usd 必须是十进制数值，不是布尔")
    if isinstance(raw, Decimal):
        value = raw
    else:
        try:
            value = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError) as error:
            raise BudgetRejection(
                f"budget.run.max_cost_usd 不是合法十进制：{raw!r}"
            ) from error
    if not value.is_finite():
        raise BudgetRejection("budget.run.max_cost_usd 必须是有限值（NaN / Inf 不接受）")
    if value < 0:
        raise BudgetRejection("budget.run.max_cost_usd 必须非负")
    return value


def run_limits_from_request(
    *,
    max_agent_turns_total: int | None = None,
    max_model_requests: int | None = None,
    max_total_tokens: int | None = None,
    max_cost_usd: Any = None,
    accounting: ProviderAccounting,
) -> RunLimits:
    """请求里的四个 run 作用域 ceiling → `RunLimits`（**开工前**校验，422）。

    形态非法（见 `parse_cost_ceiling`）与**本链强制不了**的维度
    （`validate_ceiling_enforceability`）都在这里拒绝：调用方保证它在第一位副作用
    之前被调用（`11 §6.1`「无副作用」）。三个入口（Web / SessionService / CLI）共用
    这一份规则——422 的口径只有一处，不各自解释一遍。
    """
    limits = RunLimits(
        max_agent_turns_total=max_agent_turns_total,
        max_model_requests=max_model_requests,
        max_total_tokens=max_total_tokens,
        max_cost_usd=parse_cost_ceiling(max_cost_usd),
    )
    validate_ceiling_enforceability(limits, accounting)
    return limits


def _unknown_base_dimensions(
    paused: PausedRun, limits: RunLimits,
) -> list[str]:
    """暂停快照里**基数未知**、而新请求又配了 ceiling 的维度。"""
    unknown: list[str] = []
    if limits.max_model_requests is not None and paused.consumed.model_requests is None:
        unknown.append(TRIGGER_RUN_REQUESTS)
    if limits.max_total_tokens is not None and paused.consumed.total_tokens is None:
        unknown.append(TRIGGER_RUN_TOKENS)
    if limits.max_cost_usd is not None and paused.consumed.cost_usd is None:
        unknown.append(TRIGGER_RUN_COST)
    return unknown


def resume_limits(paused: RunLimits, *, request: RunLimits) -> RunLimits:
    """恢复后的**生效** ceiling 集合：请求点名的那几维取请求值，其余沿用暂停时的值。

    为什么未点名的维度是"沿用"而不是"清空"：清空等于借着一次恢复把 operator 起的
    ceiling 撤掉——那是**放大**授权（ADR-0044 D1「配置只能收窄」；D3 同时定了「恢复绝不
    重置任何 counter」，同一方向），一次"抬高 token"的恢复不该顺手删掉 turn ceiling。
    想删 ceiling 是另一个动作，本票没有那条路径。

    于是恢复请求的语义是"在这些维度上给出新的绝对 ceiling"，而不是"这就是新的全集"；
    点名了却放不下一次新准入的维度由 `validate_resume` 的 headroom 判定拒绝（409），
    未点名但已经到线的维度同样会因此被拒——那个 run 本来也继续不了，必须把真正卡住它
    的那一维一起抬起来。
    """
    return RunLimits(
        max_agent_turns_total=_pick(
            request.max_agent_turns_total, paused.max_agent_turns_total,
        ),
        max_model_requests=_pick(request.max_model_requests, paused.max_model_requests),
        max_total_tokens=_pick(request.max_total_tokens, paused.max_total_tokens),
        max_cost_usd=_pick(request.max_cost_usd, paused.max_cost_usd),
    )


def _pick(requested: Any, previous: Any) -> Any:
    """`None` 在请求里是"这一维没点名"（沿用），不是"把它设成无 ceiling"。"""
    return previous if requested is None else requested


def validate_resume(
    paused: PausedRun,
    *,
    run_id: str | None,
    expected_version: int | None,
    limits: RunLimits,
    resume_basis: str | None,
) -> RunLimits:
    """恢复请求的**开工前**校验（拒绝 ⇒ 抛领域异常，调用方零副作用）。

    422（形状）与 409（冲突）的分界按 `11 §6.1`：**字段缺 / 值非法**是 422，
    **状态对不上**是 409。所有判定都在这里，端点与 CLI 共用同一份规则
    （单一规则来源，别在 web 层再解释一遍）。

    `limits` 是恢复请求点名的**绝对** ceiling（`03 §3.4`：恢复提供绝对值、不重置
    counter）；**返回生效集合**（`resume_limits`：未点名的维度沿用暂停时的 ceiling，
    见那里的理由）。调用方必须用返回值去 launch / 落快照——用请求值会让未点名的
    ceiling 从账本上消失。

    四维各自判定，任一维"放不下一次新准入"就拒绝整个请求——接受它等于让客户端拿到
    "恢复成功但立刻再次暂停"的假象。**一个维度都不点名**同样 409：`03 §5` 要求恢复
    请求给出绝对 ceiling，"一个都不给"不是抬高。

    本票的暂停只可能由预算产生，所以"变更依据"只有一条真实路径：
    `resume_basis=budget_increase` 且新 ceilings 真的能继续（见 `resume_headroom_ok`）。
    另外三值的**证据判定**（相关 steer / 比快照更新的环境或策略版本）属 `#317`，
    现在接受它们等于假装校验过证据 ⇒ 409 明说"不接受"
    （`#315` 的 deadline 暂停同样在这里被拒：它的恢复前置条件是不同的证据）。
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
    if not limits.configured:
        # `03 §5`：恢复请求 MUST 给出**绝对** ceiling（不是"可以不给"）。四维里点
        # 哪一维由客户端决定（暂停可能落在任一维上），但一个都不点 = 客户端没有抬高
        # 任何东西，那个 run 只会在同一个维度上立刻再停一次。
        raise BudgetConflict(
            "恢复必须至少给出一个绝对 ceiling（budget.run.* 四维任一）；"
            "一个都不给不是抬高——本请求未启动任何工作"
        )
    effective = resume_limits(paused.limits, request=limits)
    unknown = _unknown_base_dimensions(paused, effective)
    if unknown:
        raise BudgetConflict(
            f"该暂停的消耗基数在 {'、'.join(unknown)} 维度上未知（暂停快照里没有这个键，"
            f"通常是 T5 之前落的暂停）——基数未知就无法证明'到线即停'成立，"
            f"本请求未启动任何工作；请去掉这些 ceiling 后重试"
        )
    if not resume_headroom_ok(consumed=paused.consumed, limits=effective):
        raise BudgetConflict(
            f"恢复必须把绝对 ceiling 提高到能继续（consumed="
            f"{paused.consumed.as_projection()}）；仍放不下一次新准入的维度见 "
            f"limits={effective.as_projection()}"
        )
    return effective


# ── 事件 data 构造（`03 §3.4` 的字段契约） ────────────────────────────────


def build_limits_snapshot(
    *, run_limits: RunLimits, local_fuse: LocalFuse,
) -> dict[str, Any]:
    """`limits` 快照：两个作用域各还原生投影（`11 §6.1`「按作用域」）。"""
    return {"local": local_fuse.as_projection(), "run": run_limits.as_projection()}


def build_pause_data(
    *,
    reason: str,
    trigger_dimension: str,
    version: int,
    consumed: BudgetConsumed,
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
        "consumed": consumed.as_projection(),
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
    consumed: BudgetConsumed,
    resume_basis: str,
) -> dict[str, Any]:
    """`run/resumed` 的 data（`03 §3.4`）。

    `consumed` **等于**暂停快照（恢复不重置、也不预支）：它是"恢复这一刻的账"，
    新工作产生的消耗由后续 `model/request` / `model/completed` 继续累加。
    四维都写（未知的是 `null`）——恢复请求的四维 ceiling 正是拿它做基数判定的。
    """
    return {
        "from_pause_seq": from_pause_seq,
        "previous_budget_version": previous_version,
        "budget_version": version,
        "limits": limits,
        "consumed": consumed.as_projection(),
        "resume_basis": resume_basis,
    }


def project_budget(
    state: RunBudgetState, *,
    accounting: ProviderAccounting,
    local_fuse: LocalFuse | None = None,
) -> dict[str, Any]:
    """run 账本的客户端投影（`11 §6.1`）：identity / version / 绝对 ceilings /
    consumed / remaining / **可执行性** / 暂停原因 + continuation。

    暂停态复用 `PausedRun.as_projection()`（那一份是超集）；非暂停态给同一形状的
    前五个键（两种状态**键集一致**，客户端不必按状态换解析器）。只有暂停才有的
    键（`reason` / `continuation` / …）在非暂停时**缺席**——"没有暂停原因"与
    "暂停原因是空的"是两件事，用一个恒为 null 的键表达会让后者看起来像前者。

    `enforcement`（哪几个维度在本链真的能强制，`11 §6.1`）由调用方注入
    `ProviderAccounting`：它是**部署能力**，不是某个 run 的事实，所以不进
    `PausedRun` 这个纯派生对象。

    `state` 取 `03 §5` 冻结词表的原词：暂停态由 `PausedRun.as_projection()` 给
    `paused`，非暂停态给 `active`，终态给**那一个**终态事件的名字
    （`completed` / `failed` / `interrupted`）——不合并成一个 `terminal`：客户端要据此
    分辨"跑完了"与"炸了"，用一个笼统词就得自己再去翻事件。词表里另有 `needs_reconcile`，
    本链还没有把它落成 run 事件的入口（对账链归 `#305` 侧的后续票），所以这里**产不出**
    它——不产不等于可以拿别的词顶替。
    唯一的例外是 `none`：会话里**一个 run 都没有**时不存在 run 状态可言，冻结词表不为
    这种情形留词；本投影用 `none` 表示"无可投影的 run"，并把这一条登记在此（它与
    `active` 的区别是测试与客户端都要认得的）。
    """
    if state.paused is not None:
        projection: dict[str, Any] = dict(state.paused.as_projection())
        projection["enforcement"] = accounting.as_projection()
        return projection
    return {
        "run_id": state.run_id,
        "version": state.version,
        "state": state.terminal_type or ("active" if state.run_id else "none"),
        "limits": state.limits.as_projection(),
        "consumed": state.consumed.as_projection(),
        "remaining": state.consumed.remaining(state.limits),
        "enforcement": accounting.as_projection(),
        "local_fuse": local_fuse.as_projection() if local_fuse else None,
    }


def as_run_started_budget(run_limits: RunLimits) -> dict[str, Any] | None:
    """`run/started.data.budget` 的值（**只在显式配了 run ceiling 时**落键）。

    不落键 = "本次 run 没有 run 作用域 ceiling"（`02 §5.1`：RunBudget 除显式配置外无
    ceiling）。缺省不写键还有一个好处：既有会话的事件序列逐字不变（golden 基线只看
    真正新增的事实，不看恒为空的占位）。

    `configured` 判据在 `#313` 起是**四维任一**（不是只看 turns）：只配 requests 的
    run 同样必须把快照落下来——否则重启后 `run/paused.limits` 重建不出客户端配的
    ceiling。
    """
    if not run_limits.configured:
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
    limits: RunLimits,
    consumed: BudgetConsumed,
) -> dict[str, Any]:
    """确定性 continuation：**只**用已持久化事实组装（`02 §5.2`）。

    允许的事实：各维度的计数（轮数 / 请求数 / token / 成本 / 工具调用数 / 工具结果数）
    与 ceiling 本身。**不允许**：把"工具调用已发出"说成"工具已成功"、编造进展、编造
    工具结果（`02 §5.2` / ADR-0044 D3）。所以 `remaining` 里只说"停止发生在什么之前"，
    具体待办由恢复后的模型自己从历史里看见。

    账目**未知**的维度在文案里写"未知"而不是 0（`11 §6.1`：不可得 ≠ 0；这里的一句
    "已花 0 元"会直接骗到正在决定要不要继续的人）。
    """
    items = [event for event in events if event.run_id == run_id]
    tool_calls = sum(1 for event in items if event.type == "tool/call")
    tool_results = sum(1 for event in items if event.type == "tool/result")
    ceiling = limits.ceiling_of(trigger_dimension)
    action = (
        f"提高绝对 ceiling（{trigger_dimension}）后以同一 run_id 恢复："
        f"当前 consumed={_dimension_text(consumed, trigger_dimension)}，"
        f"ceiling={_value_text(ceiling)}；"
        f"恢复请求需带 expected_version 与 resume_basis={RESUME_BASIS_BUDGET_INCREASE}"
    )
    return {
        "completed": [
            (
                f"本逻辑 run 已消耗 {consumed.agent_turns} 个 agent turn、"
                f"{_value_text(consumed.model_requests)} 次 Provider 请求"
            ),
            (
                f"累计 token：{_value_text(consumed.total_tokens)}；"
                f"累计成本（USD）：{_decimal_text(consumed.cost_usd) or '未知'}"
            ),
            f"已接纳 {tool_calls} 个工具调用（其中 {tool_results} 个已落工具结果）",
        ],
        "remaining": [
            "暂停发生在下一轮模型决策之前：恢复后由模型从会话历史继续",
        ],
        "blockers": [
            (
                f"{trigger_dimension} 到顶："
                f"consumed={_dimension_text(consumed, trigger_dimension)}, "
                f"ceiling={_value_text(ceiling)}"
            ),
        ],
        CONTINUATION_ACTION_KEY: action,
    }


def _dimension_text(consumed: BudgetConsumed, dimension: str) -> str:
    """某一维度的已消耗文案（未知如实说未知）。"""
    return {
        TRIGGER_RUN_TURNS: str(consumed.agent_turns),
        TRIGGER_RUN_REQUESTS: _value_text(consumed.model_requests),
        TRIGGER_RUN_TOKENS: _value_text(consumed.total_tokens),
        TRIGGER_RUN_COST: _decimal_text(consumed.cost_usd) or "未知",
    }.get(dimension, "未知")


def _value_text(value: Any) -> str:
    if value is None:
        return "未知"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
