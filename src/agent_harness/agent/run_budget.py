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
- `tool_calls` / `tool_attempts` ← `tool/result` 的 `budget_delta` 键（`#314` 起）。唯一的
  写入者是 `ToolExecutor` 的**接纳点**：一次规范化逻辑调用记 `tool_calls: 1`，该调用
  真实 `tool.execute` 的每一次尝试（含 retry）记一格 `tool_attempts`。**retry 不产生
  新的逻辑调用**；准入前被拒的调用记 `0`（拒绝理由在同一条结果的 `error_code` 里，
  可审计）。接纳边界的完整读法在 ADR-0045——本模块只做"按 delta 求和"。

**四维的临界点语义分两类**（可数维度预留 closeout、计量维度到线即停）：判据与理由
写在 `_dimension_reached` / `_dimension_headroom` 的 docstring 里，本模块不复述第二遍
（准入与 closeout 容量是**两个**谓词，合并会让暂停点上的收口恒被拒）。

**deadline（`#315`）不是"第五维计数"**：它是**绝对时刻**（RFC 3339 UTC），判据是
"现在到点了没有"，不是"消耗比 ceiling"。因此它不进 `TRIGGER_ORDER` / `RUN_DIMENSIONS`
（那两张表描述的是"consumed 与 ceiling 比较"的维度，`closeout_capacity` 与
`resume_headroom_ok` 的逐维扫描也从它们派生），而在 `pause_trigger` 里单独判、**最先判**：
到点后任何新工作都不许开始（`04 §9.1` 的接纳边界），而"抬高一个数字"治不了它——
恢复要给的是**一个新的未来时刻**（判据见 `resume_headroom_ok`）。投影只给绝对时刻
（`limits.deadline_at`），**不给剩余秒数**：倒计时是客户端从绝对时刻自己算的派生量，
服务端投影保持与"当前时刻"无关（本模块反复强调的可逐字节比对性质）。

**本模块不实现**（别误以为漏了）：stuck（`#317`）、SessionBudget（`#318`）、以及
`budget.run.max_tool_calls`（工具调用**总数**的上限：`04 §9.1` 只冻结了"按名字给的显式
配额"，总数只观测不设限）。所以 `run/paused.data.reason` 目前只会是 `budget_exhausted`
或 `deadline`——**具体哪个维度看 `trigger_dimension`**（前者是"预算到顶"这一类）。

per-tool 配额（`#314`）在这张表上是**动态维度**：每个已配置的工具名各占一个
`run.tool_call_limits.<tool_name>`，因此它不在 `TRIGGER_ORDER` 这个定长元组里，
而由 `tool_dimensions()` 按名字排序给出（顺序必须**确定**，不能依赖 dict 插入序）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    TOOL_RESULT,
    SessionEvent,
)

#: `run/paused.data.reason`：本票唯一会出现的暂停原因（其余两类由 `#315`/`#317` 接）。
REASON_BUDGET_EXHAUSTED = "budget_exhausted"

#: `run/paused.data.reason`：绝对 deadline 到点（`#315`；`03 §3.4` 三值里的第二个）。
#: 与 `budget_exhausted` 的分工：后者的处置是"抬高某个 ceiling"，前者是"给一个新的
#: 未来时刻"——两个不同的客户端动作，所以必须是两个取值（`02 §5.1` 三层控制不可合并的
#: 同一方向）。`#317` 的 stuck 是第三个，本票不产。
REASON_DEADLINE = "deadline"

#: `03 §5` 冻结的 run 状态词表里**唯一**由对账（而非暂停 / 终态）给出的那一个（`#315` 起可产）。
#: 它在投影里**覆盖** `paused`：`03 §5` 明写「`needs_reconcile` 同样 MUST 先 reconcile 才
#: 允许恢复」，所以一条"暂停 + 欠着未对账副作用"的 run，其状态词是这一个——暂停原因
#: （`reason` / `continuation`）照旧可读，只是不再冒充"可直接恢复的暂停"。
STATE_NEEDS_RECONCILE = "needs_reconcile"

#: `run/paused.data.trigger_dimension`：命中的是哪一个 ceiling。
#: 各作用域 / 各维度**不可互相替代**（`02 §5.1`），所以投影上必须能分辨是谁到顶。
#: 取值就是**配置字段的路径名**（客户端据此知道该抬高哪一个 ceil）——不发明别名。
TRIGGER_RUN_TURNS = "run.max_agent_turns_total"
TRIGGER_RUN_REQUESTS = "run.max_model_requests"
TRIGGER_RUN_TOKENS = "run.max_total_tokens"
TRIGGER_RUN_COST = "run.max_cost_usd"
TRIGGER_LOCAL_TURNS = "local.max_agent_turns"

#: per-tool 配额的维度名 = **前缀 + 工具名**（`#314`）：它仍然是"配置字段的路径名"
#: 这一条规则的延伸（`budget.run.tool_call_limits.<name>`），客户端据此知道该抬高
#: 映射里的哪一个键。为什么是动态维度而不是固定常量：工具名由请求给、数量不定，
#: 塞不进 `TRIGGER_ORDER` 这种定长元组。
TRIGGER_RUN_TOOL_PREFIX = "run.tool_call_limits."

#: deadline 的维度名（`#315`）：同样是配置字段的路径名。它**不在** `TRIGGER_ORDER` /
#: `RUN_DIMENSIONS` 里——那两张表是"consumed 与 ceiling 比较"的维度清单，而 deadline
#: 判的是"当前时刻与截止时刻比较"。`trigger_dimension` 仍然按同一规则给路径名，
#: 客户端据此知道该换哪一个字段。
TRIGGER_RUN_DEADLINE = "run.deadline_at"


def utc_now() -> datetime:
    """本模块的时间源（**唯一一处**读挂钟）。

    为什么要有这个函数而不是各处 `datetime.now(UTC)`：deadline 判定必须能在测试里
    被钉死在确定时刻上（`#315` 的用例要造"刚好到点""差一秒"这类边界），monkeypatch
    一个函数比给每个调用点加 clock 参数更好——调用点（runtime / service / CLI）不必
    各自记得传时间，也不会出现"两处各读一次挂钟"导致同一次判定里时间不一致。
    返回**带时区的 UTC** 时刻；朴素时间在本模块一律不接受（`11 §6.1`：所有 deadline
    都是 UTC 瞬时，客户端时钟不是权威）。
    """
    return datetime.now(UTC)


def deadline_reached(deadline_at: datetime | None, *, now: datetime) -> bool:
    """到点了吗（`#315` / ADR-0046 §2 D1）——**唯一**的边界判据。

    `now >= deadline_at`，到点那一刻就停，不留半格余量："时刻"没有"下一次"可言
    （与 `consumed vs ceiling` 那类"还能不能再花一点"的判定不同，后者才有差一格的问题）。
    `deadline_at is None` ⇒ 不设 deadline，永不判"到点"（**不是**"立刻到点"）。
    形状由请求面 `parse_deadline_at` 卡死成 RFC 3339 UTC、事件回读走 `_deadline_or_none`，
    所以这里不会再遇到朴素时间。

    为什么抽出来：这条比较在 agent 侧有三处消费者（`pause_trigger` 的准入判定 /
    `closeout_capacity` / `resume_headroom_ok`），三处各自写一遍就会各自漂移。
    ⚠ **跨层边界（如实登记，不是遗漏）**：`ToolExecutor` 那道闸门用的是同一个比较但
    **不复用本函数**——`tooling/**` 不依赖 `agent/**`（工具运行时不该认识 run 预算），
    两边的一致只能靠"同一语义 + 各自用例"维持（ADR-0046 §2 D2 记了这条边界）。
    """
    return deadline_at is not None and now >= deadline_at


#: 准入判定的顺序（命中即返回，**只报一个**维度）。turns 在最前：它是
#: `#308` 起就存在的维度、也是客户端最先配的那个；同一时刻多维度同时到顶时，
#: 报哪一个不影响客户端的动作（抬高全部到顶的 ceiling 才能继续），所以顺序
#: 只需**确定**、不需要"最紧优先"这种需要全局比较的聪明规则。
#: per-tool 维度紧随四维之后、local fuse 之前（都是 run 作用域，见 `pause_trigger`）。
TRIGGER_ORDER: tuple[str, ...] = (
    TRIGGER_RUN_TURNS,
    TRIGGER_RUN_REQUESTS,
    TRIGGER_RUN_TOKENS,
    TRIGGER_RUN_COST,
    TRIGGER_LOCAL_TURNS,
)

#: run 作用域的四维（= `TRIGGER_ORDER` 去掉 local fuse 的**同一份事实**，
#: 不是第二张清单：恢复判定与 closeout 容量都只看这四维）。写成推导而不是 `[:4]`
#: 切片：切片会把"前四个恰好是 run 维度"这条巧合变成隐式约束，日后调 `TRIGGER_ORDER`
#: 的顺序就会静默改变这里的集合。
RUN_DIMENSIONS: tuple[str, ...] = tuple(
    dimension for dimension in TRIGGER_ORDER if dimension != TRIGGER_LOCAL_TURNS
)


def tool_trigger_dimension(tool_name: str) -> str:
    """工具名 → 该工具的配额维度名（配置字段路径）。"""
    return f"{TRIGGER_RUN_TOOL_PREFIX}{tool_name}"


def tool_name_of_dimension(dimension: str) -> str | None:
    """配额维度名 → 工具名；不是 per-tool 维度时 `None`。"""
    if not dimension.startswith(TRIGGER_RUN_TOOL_PREFIX):
        return None
    name = dimension[len(TRIGGER_RUN_TOOL_PREFIX):]
    return name or None


def tool_dimensions(limits: RunLimits) -> tuple[str, ...]:
    """该 ceiling 集合里的 per-tool 维度（**按工具名排序**，顺序确定）。

    排序而不是插入序：请求里的映射顺序是客户端的书写顺序，不是事实；把它当判定顺序
    会让"同时到顶报哪一个"随 JSON 键序漂移，暂停载荷也就不可复现（`03 §3.4` 要求
    可 replay 重建同样的状态）。
    """
    return tuple(tool_trigger_dimension(name) for name in sorted(limits.tool_call_limits))


def reason_for_dimension(dimension: str) -> str:
    """命中的维度 → `run/paused.data.reason`（`03 §3.4` 的三值词表）。

    只有 `run.deadline_at` 落在 deadline 上；其余（四维计数 / local fuse / per-tool
    配额）都是预算类——它们的共同点是"把绝对值抬高就能继续"。`stuck` **不由维度
    产生**（它是 guard 的判定，`#317`），所以这里没有它的入口：映射表里不预置一个
    当下产不出的值，就不必在别处解释它为什么恒不出现。
    """
    return REASON_DEADLINE if dimension == TRIGGER_RUN_DEADLINE else REASON_BUDGET_EXHAUSTED


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


def _deadline_text(value: datetime | None) -> str | None:
    """`datetime` → wire 上的 UTC ISO 8601 文本（`None` 原样）。

    一律用 `Z` 收尾的 UTC 形式（`11 §6.1`：`deadline_at` 是 RFC 3339 UTC）：
    `+00:00` 与 `Z` 描述同一瞬时，但**字节不同**，而本模块的投影要能跨执行逐字节
    比对（事件落盘 / 证据复核都依赖它），所以出口只允许一种写法。
    """
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _deadline_or_none(raw: Any) -> datetime | None:
    """读回事件里的 `deadline_at`（**宽容**：读不出来就当没配，不连坐其余维度）。

    投影读回与请求校验**故意不同**：请求里的畸形值要响亮拒绝（`parse_deadline_at`
    抛 422），因为那是"客户端以为设了"的假象来源；而事件里的畸形值只可能是历史
    数据或人工篡改，重建账本时按"没配这一维"处理才能让其余维度照常可用
    （与 `_int_map` 的逐项宽容同一条纪律）。
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


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
    快照的形状——快照的六个维度里**每工具配额**这一席由 `#314` 接入
    （`tool_call_limits`，只收**已注册**工具名），**deadline** 这一席由 `#315` 接入
    （`deadline_at`，RFC 3339 UTC 绝对时刻或 `null`）。

    `deadline_at`（`#315`）：**绝对时刻**，不是"还剩多久"。`null` = 本 run 不设 deadline
    （只观测不限制，`11 §6.1` 的缺省）。落进事件与投影时一律是 UTC 的 ISO 8601 文本
    （`Z` 收尾），因此同一份 ceiling 跨执行逐字节可比；**没有"剩余秒数"键**——那是
    客户端从绝对时刻自己算的派生量，服务端投影不与当前时刻挂钩。

    `tool_call_limits`（`#314`）：工具名 → 正整数**绝对**上限。空映射 = 本 run 没有
    任何 per-tool 配额（**不是**"所有工具上限为 0"）。它与其他四维的区别是"一维变多维"：
    它一次携带整张表，而准入判定按 `run.tool_call_limits.<name>` 逐名进行。
    """

    max_agent_turns_total: int | None = None
    max_model_requests: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: Decimal | None = None
    #: 绝对截止时刻（UTC，aware）。`None` = 不限。
    deadline_at: datetime | None = None
    #: 工具名 → 绝对 ceiling。只读（冻结实例不阻止改内容，故一律经 `tool_call_limits()`
    #: 之外的构造入口赋值，且投影/读回都**复制**出去，不把内部字典交到调用方手上）。
    tool_call_limits: Mapping[str, int] = field(default_factory=dict)

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
                self.deadline_at,
            )
        ) or bool(self.tool_call_limits)

    def ceiling_of(self, dimension: str) -> Any:
        """按 `trigger_dimension` 取值（准入判定与 continuation 文案共用一份映射）。"""
        tool_name = tool_name_of_dimension(dimension)
        if tool_name is not None:
            return self.tool_call_limits.get(tool_name)
        return {
            TRIGGER_RUN_TURNS: self.max_agent_turns_total,
            TRIGGER_RUN_REQUESTS: self.max_model_requests,
            TRIGGER_RUN_TOKENS: self.max_total_tokens,
            TRIGGER_RUN_COST: self.max_cost_usd,
            TRIGGER_RUN_DEADLINE: self.deadline_at,
        }.get(dimension)

    def as_projection(self) -> dict[str, Any]:
        """客户端可读投影：**各维全在**，没配的那一维是 `null`。

        与 `as_run_started_budget` 的"一维都没配就不落键"不矛盾：那里是"这次 run
        没有 run 预算事实"（落一个全 null 的对象是占位噪声），这里是"客户端问
        ceiling 是什么"——缺键会让客户端分不清"没配"与"这个维度不存在"。

        `tool_call_limits` **按名字排序**输出（同一份事实的字节稳定形式：事件落盘与
        投影要能跨执行逐字节比对）；没配工具配额时是 `{}`，与请求侧的缺省形状一致。
        `deadline_at` 是 UTC 的 ISO 文本或 `null`——**没有**"剩余秒数"键（见类 docstring）。
        """
        return {
            "max_agent_turns_total": self.max_agent_turns_total,
            "max_model_requests": self.max_model_requests,
            "max_total_tokens": self.max_total_tokens,
            "max_cost_usd": _decimal_text(self.max_cost_usd),
            "deadline_at": _deadline_text(self.deadline_at),
            "tool_call_limits": {
                name: self.tool_call_limits[name] for name in sorted(self.tool_call_limits)
            },
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
    #: 工具名 → 已接纳的逻辑调用数（`#314`）。`None` = 未知（T6 之前的暂停快照里
    #: 没有这个键）；`{}` = 已知且一个都没调用。**总数与每工具数只有一份真相**：
    #: 总数是这张表的和（见 `tool_calls`），不另立字段——两个字段就能不一致。
    tool_calls_by_tool: Mapping[str, int] | None = field(default_factory=dict)
    #: 工具名 → 实际尝试次数（含 retry），与上一张表同构、各占一维
    #: （`02 §5.1`：`tool_attempts` MUST NOT 被当成 `tool_calls` 的别名）。
    tool_attempts_by_tool: Mapping[str, int] | None = field(default_factory=dict)

    @property
    def tool_calls(self) -> int | None:
        """被接纳的**规范化逻辑工具调用**总数（`None` = 未知）。"""
        return _map_total(self.tool_calls_by_tool)

    @property
    def tool_attempts(self) -> int | None:
        """工具执行域的实际尝试总数（含 retry，`None` = 未知）。"""
        return _map_total(self.tool_attempts_by_tool)

    def calls_for(self, tool_name: str) -> int | None:
        """某工具已接纳的逻辑调用数（表未知时 `None`，没出现过时 0）。"""
        if self.tool_calls_by_tool is None:
            return None
        return self.tool_calls_by_tool.get(tool_name, 0)

    def as_projection(self) -> dict[str, Any]:
        return {
            "agent_turns": self.agent_turns,
            "model_requests": self.model_requests,
            "total_tokens": self.total_tokens,
            "cost_usd": _decimal_text(self.cost_usd),
            # 总数与两张表都写：总数是 `02 §5.1` 的 counter 名（客户端按它读"花了多少
            # 工具调用"），两张表是 per-tool 配额与可观测性要的分维；三者的关系是
            # "总数 = 表和"，所以这里不引入第二份真相，只是把同一份账按两种粒度给出。
            "tool_calls": self.tool_calls,
            "tool_attempts": self.tool_attempts,
            "tool_calls_by_tool": _map_text(self.tool_calls_by_tool),
            "tool_attempts_by_tool": _map_text(self.tool_attempts_by_tool),
        }

    def remaining(self, limits: RunLimits) -> dict[str, Any]:
        """各维度的剩余量（`11 §6.1` 要求投影里既有 ceiling 也有 remaining）。

        只给**配了 ceiling 的维度**（`11 §6.1` 的原话就是"有 ceiling 时的 remaining"）：
        工具调用**总数**没有 ceiling 故不出现在这里，per-tool 配额按配置名逐名给。
        """
        return {
            "agent_turns": _remaining(limits.max_agent_turns_total, self.agent_turns),
            "model_requests": _remaining(limits.max_model_requests, self.model_requests),
            "total_tokens": _remaining(limits.max_total_tokens, self.total_tokens),
            "cost_usd": _decimal_text(
                None
                if limits.max_cost_usd is None or self.cost_usd is None
                else max(limits.max_cost_usd - self.cost_usd, Decimal(0))
            ),
            "tool_call_limits": {
                name: _remaining(ceiling, self.calls_for(name))
                for name, ceiling in sorted(limits.tool_call_limits.items())
            },
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
            # 工具账与"一次 Provider 请求"无关，原样带过去（漏了这两行就是把工具账
            # 在聚合点清零——那是"第二份真相"最容易长出来的地方）。
            tool_calls_by_tool=self.tool_calls_by_tool,
            tool_attempts_by_tool=self.tool_attempts_by_tool,
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


def _map_total(counts: Mapping[str, int] | None) -> int | None:
    """分维计数表的总数（表未知 ⇒ 总数未知；空表 ⇒ 0，是"确实一次都没调用"）。"""
    return None if counts is None else sum(counts.values())


def _map_text(counts: Mapping[str, int] | None) -> dict[str, int] | None:
    """分维计数表的 wire 形式：**排序**输出，未知保持 `None`（不写成 `{}`）。

    `None` 与 `{}` 在投影里必须可区分：前者是"不知道调了多少次"，后者是"一次都没调"。
    排序的理由同 `RunLimits.as_projection`（事件与投影要跨执行逐字节可比）。
    """
    if counts is None:
        return None
    return {name: counts[name] for name in sorted(counts)}


def _add_maps(
    base: Mapping[str, int] | None, delta: Mapping[str, int] | None,
) -> dict[str, int] | None:
    """两张分维表相加（任一未知 ⇒ 和未知，`None` 粘性同其余维度）。"""
    if base is None or delta is None:
        return None
    total = dict(base)
    for name, value in delta.items():
        total[name] = total.get(name, 0) + value
    return total


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
        deadline_at=_deadline_or_none(scope.get("deadline_at")),
        tool_call_limits=_int_map(scope.get("tool_call_limits")),
    )


def _int_map(raw: Any, *, minimum: int = 1) -> dict[str, int]:
    """读回"名字 → 正整数"的表（**逐项**宽容：畸形项丢掉，不连坐其余项）。

    判据与请求侧 `parse_tool_call_limits` 对齐（键非空字符串、值 ≥ `minimum` 的整数）；
    但读回事件时**不抛**——读已落盘的事实崩掉会让整个 run 打不开，报文侧的 422 才是
    拒绝入口。`bool` 必须显式排除：`True` 在 Python 里是 `int` 的实例，收下它等于
    把 `true` 读成 1。
    """
    if not isinstance(raw, Mapping):
        return {}
    parsed: dict[str, int] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            continue
        parsed[name] = value
    return parsed


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
        tool_calls_by_tool=_add_maps(base.tool_calls_by_tool, delta.tool_calls_by_tool),
        tool_attempts_by_tool=_add_maps(
            base.tool_attempts_by_tool, delta.tool_attempts_by_tool,
        ),
    )


def _add_tool_delta(
    calls: dict[str, int] | None,
    attempts: dict[str, int] | None,
    raw: Any,
) -> tuple[dict[str, int] | None, dict[str, int] | None]:
    """把一条 `tool/result.data.budget_delta` 累加进两张工具表（原位改，返回入参）。

    三条读法：

    - **表未知**（`None`，T6 之前的暂停快照）⇒ 保持未知：一张读不出来的表上加任何数
      仍然是"不知道"，把未知当 0 会让配额从 0 起算（那是假账）。
    - **没有 delta 键 / delta 读不懂** ⇒ 这条结果不来自 `ToolExecutor` 的接纳点
      （recovery 与 dangling 修复写的是合成结果），贡献 0。**不**把一条读不懂的记录
      升级成"整维未知"：那会让一个 run 因为一格读不懂就再也续不了，而它其实只是
      少记了一格。谁会写坏这个键？只有本仓自己的 Executor——它不是外部输入。
    - **贡献为 0 的工具名不落进表**（准入前被拒的调用写 `tool_calls: 0`）：表是"消耗了
      多少"，不是"出现过哪些工具名"；留下 0 行只会让投影里多出一堆没花过配额的名字。
    """
    if calls is None or attempts is None or not isinstance(raw, Mapping):
        return calls, attempts
    name = raw.get("tool_name")
    if not isinstance(name, str) or not name:
        return calls, attempts
    for key, table in (("tool_calls", calls), ("tool_attempts", attempts)):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        table[name] = table.get(name, 0) + value
    return calls, attempts


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
    calls: dict[str, int] | None = {}
    attempts: dict[str, int] | None = {}
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
        elif event.type == TOOL_RESULT:
            calls, attempts = _add_tool_delta(
                calls, attempts, event.data.get("budget_delta"),
            )
    return BudgetConsumed(
        agent_turns=turns, model_requests=requests,
        total_tokens=tokens, cost_usd=cost,
        tool_calls_by_tool=calls, tool_attempts_by_tool=attempts,
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
            tool_calls_by_tool=None, tool_attempts_by_tool=None,
        )
    turns = raw.get("agent_turns")
    requests = raw.get("model_requests")
    tokens = raw.get("total_tokens")
    calls = raw.get("tool_calls_by_tool")
    attempts = raw.get("tool_attempts_by_tool")
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
        # 工具两表同样**不回落重算**：T6 之前的 run 真的调用过工具，重算得到的 0 是
        # 假话（与 requests / tokens / cost 同一条理由，见上面的 docstring）。表存在
        # 但形状不对 ⇒ 也是未知，不把"读不懂"当"没花过"。
        tool_calls_by_tool=(_int_map(calls, minimum=0) if isinstance(calls, Mapping) else None),
        tool_attempts_by_tool=(
            _int_map(attempts, minimum=0) if isinstance(attempts, Mapping) else None
        ),
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
    now: datetime | None = None,
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
    发起请求，就是"有意越线"。顺序由 `TRIGGER_ORDER` 固定（turns 最先），命中即返回；
    **per-tool 配额**（`#314`）紧随四维之后、local fuse 之前——它同样是 run 作用域
    （`02 §5.1`：RunBudget 的"每工具配额"与 local fuse 是两层不同的控制），按工具名
    排序逐名判（`tool_dimensions`）。

    **deadline（`#315`）先判、且判据不同**：它比的是**当前时刻**（`now` 缺省取
    `utc_now()`）与绝对截止时刻，`now >= deadline_at` 即到点（到点那一刻就停，不留
    半格余量——"时刻"没有"下一次"可言）。为什么排在最前：其余维度的处置都是"抬高一
    个数字"，而 deadline 的处置是"给一个新的未来时刻"，一个数字治不了它；同一次判定里
    同时命中时先报 deadline，客户端才不会被引去抬一个无关的 ceiling。deadline **不进**
    `TRIGGER_ORDER` / `closeout_capacity` / `resume_headroom_ok` 的逐维扫描（那些表描述
    "consumed vs ceiling"），它的恢复判据另行实现（见 `resume_headroom_ok`）。
    """
    if deadline_reached(run_limits.deadline_at, now=now if now is not None else utc_now()):
        return TRIGGER_RUN_DEADLINE
    for dimension in RUN_DIMENSIONS:
        if _dimension_reached(dimension, consumed=consumed, limits=run_limits):
            return dimension
    for dimension in tool_dimensions(run_limits):
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

    三类临界点（`04 §9.1` 只钉了前两类，第三类是 `#314` 的直接延伸）：

    - **可数且会被 closeout 用到**（turns / requests）：`consumed + 预留 >= ceiling`。
    - **计量、下一轮大小不可预知**（tokens / cost）：`consumed >= ceiling`。
    - **per-tool 配额**（可数，但 closeout **不**调用工具 ⇒ 不预留）：`consumed >= ceiling`。
      预留的语义是"保证暂停时还收得了口"（`02 §5.2`），而 closeout 是一次模型请求、
      不产生任何工具调用，所以工具配额根本不约束它——给它留一格等于凭空虚设一个
      永远用不上的余量，那会让"配额 = 3"实际允许 4 次调用。
    """
    tool_name = tool_name_of_dimension(dimension)
    if tool_name is not None:
        ceiling = limits.tool_call_limits.get(tool_name)
        if ceiling is None:
            return False
        used = consumed.calls_for(tool_name)
        return used is None or used >= ceiling
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

    per-tool 维度与 `_dimension_reached` 成对地一并实现（同一个 switch 要穷尽）：它今天
    不会从 `closeout_capacity` 走到这里（那里只扫四维，closeout 不调用工具），但两个
    谓词的语义必须互为补集，否则谁多传一维就会得到静默矛盾的答案。
    """
    tool_name = tool_name_of_dimension(dimension)
    if tool_name is not None:
        ceiling = limits.tool_call_limits.get(tool_name)
        if ceiling is None:
            return True
        used = consumed.calls_for(tool_name)
        return used is not None and used < ceiling
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


def closeout_capacity(
    *, consumed: BudgetConsumed, run_limits: RunLimits, now: datetime | None = None,
) -> bool:
    """暂停时还剩不剩 closeout 的容量（剩 ⇒ 允许一次有界模型 closeout）。

    没有容量 ⇒ 只落**确定性** continuation（`02 §5.2`：模型 closeout 不可用时不得
    超出预算去补一次调用）。

    判据是"**每一维**都还有一点余量"（`_dimension_headroom`）：closeout 也是一次真实
    请求、也花 token 与钱，任何一维已经到线/越线/账目未知，都不能再发它。与准入判定
    的分工见 `_dimension_reached` 的 docstring——T4 时这个判据在暂停点上恒为真，
    `#313` 起不再恒真（token / cost 的暂停可能恰好落在已到线的那一轮，或账目未知），
    这正是它当初被保留下来的理由。

    扫的是**四维**（`RUN_DIMENSIONS`）：per-tool 配额（`#314`）不参与——closeout 是一次
    模型请求、不调用任何工具，工具配额约束不到它。把工具维度算进来会让"工具配额的暂停"
    在配额恰好用满时永久降级成确定性 continuation。

    **deadline（`#315`）不是"余量"而是"时刻"**：到点后连这一次请求也不发（返回
    False ⇒ 走确定性 continuation）。理由不是省预算，而是 `04 §9.1` / ADR-0044 D4
    把"到点后不启动任何新工作"写死了，而 closeout 就是一次真实的 Provider 请求
    （它会在事件里落一条 `model/request`）。确定性 continuation 只用已持久化事实、
    不发请求，所以它在到点后仍然可用——这正是"暂停必须永远收得了口"的保证。
    """
    moment = now if now is not None else utc_now()
    if deadline_reached(run_limits.deadline_at, now=moment):
        return False
    return all(
        _dimension_headroom(dimension, consumed=consumed, limits=run_limits)
        for dimension in RUN_DIMENSIONS
    )


def resume_headroom_ok(
    *, consumed: BudgetConsumed, limits: RunLimits, now: datetime | None = None,
) -> bool:
    """恢复的绝对 ceiling 是否"真的能继续"（`11 §6.1` 的 409 判据之一）。

    判据是**能继续干活**，不是"数字变大"：对**每一维**配了 ceiling 的维度，都要求
    它至少放得下"一次新的准入"——turns / requests 要留出「一次决策 + 一次 closeout
    预留」，tokens / cost / **per-tool 配额**（`#314`）只要严格大于已消耗。恰好等于
    consumed 的 ceiling 会在下次准入立刻再次暂停，接受它等于让客户端拿到一个"恢复成功
    但什么都没发生"的假象。

    账目**未知**而该维度配了 ceiling ⇒ 判定为"不能继续"（无法证明在预算内）。

    **deadline（`#315`）的判据是"严格在未来"**（`limits.deadline_at > now`）：等于
    当前时刻或已过去 ⇒ 恢复后下一次准入立刻再停一次，与上面那条"不许接受立刻再暂停的
    恢复"是**同一条**判据在时间维度上的形式。它**不**参与 `_dimension_reached` 的扫描
    （deadline 不是 consumed 与 ceiling 的比较），所以单独判在这一处、只判一次。

    **没配任何一维时这里返回 True**（没有可检查的维度）——"恢复必须至少给一个
    ceiling"是 `validate_resume` 的判定（那里才区分"没给"与"给了但不够"），
    本函数只管"给了的那些够不够"。

    这是本实现对冻结清单的**收紧**读法（`11 §6.1` 只点名「ceiling 降到已消耗之下 ⇒
    409」）：比该条更严一格，因此不会放过任何冻结文本要求拒绝的请求，只是额外拒绝
    "恢复了但一轮都跑不了"的请求。收紧的边界如实登记在 `docs/SDD_TICKET_TRACKER.md`
    的 T4 / T5 / T7 三段（deadline 那一条在 T7），若产品要放开，改这里一处即可
    （前端只做展示提示，不做判定）。
    """
    if limits.deadline_at is not None:
        moment = now if now is not None else utc_now()
        if deadline_reached(limits.deadline_at, now=moment):
            return False
    return not any(
        _dimension_reached(dimension, consumed=consumed, limits=limits)
        # local fuse 不是"绝对 ceiling"，恢复不改它；per-tool 维度**要**检
        # （恢复请求可以点名抬高某个工具的配额，那就是拿它作为依据继续跑）。
        for dimension in (*RUN_DIMENSIONS, *tool_dimensions(limits))
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


def parse_tool_call_limits(raw: Any) -> dict[str, int]:
    """`budget.run.tool_call_limits` 的形态校验（**422**；`04 §9.1` / `11 §6.1`）。

    形状：工具名 → 正整数**绝对** ceiling。这里只判"形状 + 正整数"（键是非空且无首尾空白的
    字符串、值是 ≥ 1 的整数、显式拒绝布尔）；**"已注册"这一条判不了**——注册表是装配层的
    产物（capability wiring / artifact store / profile 收窄都发生在那里），判据落在
    `validate_tool_call_limits_registered`。`None` 与 `{}` 都是"没配 per-tool 配额"。

    任何一项不合形状都**拒绝整个请求**（不静默丢弃那一项）：丢一项等于给客户端一个
    "配了但没生效"的假象（ADR-0044 D1/D8，与 `parse_cost_ceiling` 同一条纪律）。
    名字**不做 strip 归一化**：`" bash "` 不是 `"bash"`，静默改写标识符会让"配的是哪个
    工具"变成实现口味；名字可疑就响亮拒绝（`validate_*` 那条会给出明确的未注册名）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise BudgetRejection(
            "budget.run.tool_call_limits 必须是 工具名→正整数 的映射，"
            f"收到 {type(raw).__name__}"
        )
    parsed: dict[str, int] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or name.strip() != name:
            raise BudgetRejection(
                f"budget.run.tool_call_limits 的工具名必须是非空、无首尾空白的字符串：{name!r}"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise BudgetRejection(
                f"budget.run.tool_call_limits[{name!r}] 必须是正整数绝对 ceiling"
                f"（0 / 负数 / 布尔 / 非整数都不是）：{value!r}"
            )
        parsed[name] = value
    return parsed


def parse_deadline_at(raw: Any) -> datetime | None:
    """`budget.run.deadline_at` 的形态校验（**422**；`#315` / `11 §6.1`）。

    形状（冻结）：RFC 3339 **UTC** 绝对时刻，或 `null`（= 不设 deadline，只观测）。
    接受的写法：带 `Z` 或带 `+00:00` 的 ISO 8601；带其他偏移（如 `+08:00`）也**收**，
    但**归一化到 UTC** 存储——瞬时不变、字节稳定，客户端不必为"同一时刻两种写法"辩护。
    **朴素时间**（无时区）一律拒绝：`naive` 的语义取决于读它的机器，而本项目的
    deadline 是绝对瞬时（`11 §6.1`：所有 deadline 都是 UTC 瞬时，客户端时钟不是权威）；
    收下它会得到"同一份请求在不同机器上到点时刻不同"的假实现。

    非法值**拒绝整个请求**（不静默当成没配）：与 `parse_cost_ceiling` 同一条纪律
    （ADR-0044 D1/D8：不给客户端"配了但没生效"的假象）。本函数只管**形状**；
    "这个 deadline 已经过去了"是**状态**问题，按 `11 §6.1` 归 409 面
    （见 `pause_trigger` 的即时暂停与 `resume_headroom_ok` 的恢复判据）——
    开工前给一个已过期的 deadline 是合法的（等于立刻到点），不是形状错误。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise BudgetRejection("budget.run.deadline_at 必须是 RFC 3339 UTC 文本，不是布尔")
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise BudgetRejection(
                "budget.run.deadline_at 不能是空字符串（不设 deadline 请给 null）"
            )
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise BudgetRejection(
                f"budget.run.deadline_at 不是合法的 RFC 3339 时刻：{raw!r}"
                "（例：2026-09-26T04:30:00Z）"
            ) from error
    else:
        raise BudgetRejection(
            f"budget.run.deadline_at 必须是 RFC 3339 UTC 文本或 null，"
            f"收到 {type(raw).__name__}"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BudgetRejection(
            f"budget.run.deadline_at 必须带时区（RFC 3339 UTC）：{raw!r} 是朴素时间，"
            "它在不同机器上代表不同瞬时，不能作为绝对截止时刻"
        )
    return parsed.astimezone(UTC)


def validate_tool_call_limits_registered(
    limits: RunLimits, *, registered: Iterable[str],
) -> None:
    """`budget.run.tool_call_limits` 里的名字必须在**本 runtime 的注册表**里（**422**）。

    判据是"已注册"（`04 §9.1` 明文）：配额的意义是"该工具被真实使用的次数到顶"，给一个
    根本调不到的名字配配额是**请求本身**有问题（客户端以为它在限制什么），不是运行期
    再忽略——所以拒绝整个请求，与"不静默截断"同一条纪律（ADR-0044 D1/D8）。

    **判定落点为什么在装配层**：注册表是 `build_runtime` 的产物（内置工具 + artifact store
    选出的读回工具 + capability tools，最后按 agent_profile 的 tool_scope 收窄），在那之前
    "哪些工具已注册"根本没有事实可言。该落点仍然满足 `11 §6.1` 的"无副作用"：它在任何
    model / tool / child 工作之前，也不写任何消耗预算的事件。判据用**收窄之后**的注册表：
    被 profile 剔除的工具本次 run 调不到，给它配 ceiling 等于配一个永远不触发的上限
    （理由同 `validate_ceiling_enforceability`）。
    """
    if not limits.tool_call_limits:
        return
    known = set(registered)
    unknown = sorted(name for name in limits.tool_call_limits if name not in known)
    if unknown:
        raise BudgetRejection(
            f"budget.run.tool_call_limits 含未注册的工具名 {unknown}；"
            f"本 run 已注册的工具名是 {sorted(known)}。"
            f"（04 §9.1：显式配额只接受已注册工具名）"
        )


def run_limits_from_request(
    *,
    max_agent_turns_total: int | None = None,
    max_model_requests: int | None = None,
    max_total_tokens: int | None = None,
    max_cost_usd: Any = None,
    deadline_at: Any = None,
    tool_call_limits: Any = None,
    accounting: ProviderAccounting,
) -> RunLimits:
    """请求里的 run 作用域 ceiling → `RunLimits`（**开工前**校验，422）。

    形态非法（见 `parse_cost_ceiling` / `parse_deadline_at` / `parse_tool_call_limits`）
    与**本链强制不了**的维度（`validate_ceiling_enforceability`）都在这里拒绝：调用方
    保证它在第一位副作用之前被调用（`11 §6.1`「无副作用」）。三个入口（Web /
    SessionService / CLI）共用这一份规则——422 的口径只有一处，不各自解释一遍。

    「已注册工具名」这条不在本函数里判（它要注册表，这里还没有）：调用方在装配层用
    `validate_tool_call_limits_registered` 补上，见那里的 docstring。deadline **不**
    受"可执行性"约束（它由 Runtime 自己在接纳点判，不依赖 Provider 链的能力），
    所以只过 `parse_deadline_at` 的形状判定。
    """
    limits = RunLimits(
        max_agent_turns_total=max_agent_turns_total,
        max_model_requests=max_model_requests,
        max_total_tokens=max_total_tokens,
        max_cost_usd=parse_cost_ceiling(max_cost_usd),
        deadline_at=parse_deadline_at(deadline_at),
        tool_call_limits=parse_tool_call_limits(tool_call_limits),
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
    if limits.tool_call_limits and paused.consumed.tool_calls_by_tool is None:
        unknown.extend(tool_dimensions(limits))
    return unknown


def resume_limits(paused: RunLimits, *, request: RunLimits) -> RunLimits:
    """恢复后的**生效** ceiling 集合：请求点名的那几维取请求值，其余沿用暂停时的值。

    为什么未点名的维度是"沿用"而不是"清空"：清空等于借着一次恢复把 operator 起的
    ceiling 撤掉——那是**放大**授权（ADR-0044 D1「配置只能收窄」；D3 同时定了「恢复绝不
    重置任何 counter」，同一方向），一次"抬高 token"的恢复不该顺手删掉 turn ceiling。
    想删 ceiling 是另一个动作，本票没有那条路径。

    per-tool 配额（`#314`）是**逐键**合并而不是整表替换：映射是"一维变多维"的那个维度，
    「点名」的单位是键——请求只写 `{"bash": 9}` 时，暂停时配的 `{"read_file": 3}` 必须
    留下（整表替换会静默撤掉它，正是上面那条"一次恢复顺手删 ceiling"）。于是恢复能给
    某个工具**新增**一个配额（收窄，允许）或抬高已有的（点名的本意），但删不掉。

    deadline（`#315`）沿同一条规则：点名的取请求值，没点名的沿用暂停时的时刻。于是
    "上次的 deadline 已过去"的恢复**必须点名一个新的未来时刻**，否则 `validate_resume`
    的 headroom 判定会以"恢复后会立刻再停"拒绝它（409）——沿用不是"删掉"，也不是
    "顺手延长"。

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
        deadline_at=_pick(request.deadline_at, paused.deadline_at),
        tool_call_limits={**paused.tool_call_limits, **request.tool_call_limits},
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
    now: datetime | None = None,
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

    本票的暂停只可能由预算或 deadline 产生，所以"变更依据"只有一条真实路径：
    `resume_basis=budget_increase` 且新 ceilings 真的能继续（见 `resume_headroom_ok`）。
    另外三值的**证据判定**（相关 steer / 比快照更新的环境或策略版本）属 `#317`，
    现在接受它们等于假装校验过证据 ⇒ 409 明说"不接受"。

    **deadline 暂停（`#315`）走同一条 `budget_increase` 路径**，理由：deadline 本身就是
    `budget.run` 作用域的一维（`11 §6.1` 的公开形状里它就在 `run` 对象内），恢复它给的
    是"新的绝对时刻"——与给新 ceiling 是同一个动作类别（`03 §3.4`：恢复只接受绝对值、
    不重置 counter），不需要 `#317` 那三类"变更依据"的证据。真正的判据是
    `resume_headroom_ok` 里的"deadline 严格在未来"：沿用一个已过去的时刻会被拒（409），
    而这不是形状错误（形状在 `parse_deadline_at` 判过）。`stuck`（`#317`）仍是 409。
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
    if paused.reason not in (REASON_BUDGET_EXHAUSTED, REASON_DEADLINE):
        raise BudgetConflict(
            f"暂停原因 reason={paused.reason} 的恢复前置条件本票未实现"
            f"（#317 stuck 负责），拒绝启动工作"
        )
    if resume_basis != RESUME_BASIS_BUDGET_INCREASE:
        raise BudgetConflict(
            f"预算 / deadline 暂停只接受 resume_basis={RESUME_BASIS_BUDGET_INCREASE}："
            f"{resume_basis} 的有效性需要变更证据（属 #317 的责任域），"
            f"本票不假装校验过它"
        )
    if not limits.configured:
        # `03 §5`：恢复请求 MUST 给出**绝对** ceiling（不是"可以不给"）。各维里点
        # 哪一维由客户端决定（暂停可能落在任一维上），但一个都不点 = 客户端没有抬高
        # 任何东西，那个 run 只会在同一个维度上立刻再停一次。
        raise BudgetConflict(
            "恢复必须至少给出一个绝对 ceiling（budget.run.* 任一维，含 deadline_at）；"
            "一个都不给不是抬高——本请求未启动任何工作"
        )
    effective = resume_limits(paused.limits, request=limits)
    if paused.reason == REASON_DEADLINE and effective.deadline_at is None:
        # deadline 暂停的**恢复依据**就是"换一个新的未来时刻"：连时刻都没有（请求没点名，
        # 暂停快照里也没有）时，这次恢复没有任何依据让这个 run 继续跑下去——它只会在
        # 下一次准入上因为别的原因再停一次。这一条与 headroom 是**两个**判据：headroom
        # 管"给了但不够（已过去）"，这里管"根本没给"。缺了它，一个 reason=deadline
        # 但快照里没带 deadline 的行会被自由放行（`#315` 用例 `test_..._deadline...` 盯着）。
        raise BudgetConflict(
            "暂停原因是 deadline，但生效的 ceiling 里没有 deadline_at："
            "恢复必须点名一个**未来**的 deadline（budget.run.deadline_at）——"
            "沿用一个没有时刻的暂停等于假装它还能继续跑"
        )
    unknown = _unknown_base_dimensions(paused, effective)
    if unknown:
        raise BudgetConflict(
            f"该暂停的消耗基数在 {'、'.join(unknown)} 维度上未知（暂停快照里没有这个键，"
            f"通常是 T5 之前落的暂停）——基数未知就无法证明'到线即停'成立，"
            f"本请求未启动任何工作；请去掉这些 ceiling 后重试"
        )
    if not resume_headroom_ok(consumed=paused.consumed, limits=effective, now=now):
        raise BudgetConflict(
            f"恢复必须把绝对 ceiling 提高到能继续（consumed="
            f"{paused.consumed.as_projection()}）；仍放不下一次新准入的维度见 "
            f"limits={effective.as_projection()}（deadline 维度的判据是"
            f"「严格在未来」，沿用一个已过去的时刻同样在这里被拒）"
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
    reconcile_pending: Sequence[str] = (),
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
    分辨"跑完了"与"炸了"，用一个笼统词就得自己再去翻事件。词表里的 `needs_reconcile`
    只有一个来源：下面的 `reconcile_pending` 覆盖（`#315`），且只覆盖**暂停态**。
    唯一的例外是 `none`：会话里**一个 run 都没有**时不存在 run 状态可言，冻结词表不为
    这种情形留词；本投影用 `none` 表示"无可投影的 run"，并把这一条登记在此（它与
    `active` 的区别是测试与客户端都要认得的）。

    `reconcile_pending`（`#315`）：本 run 在 Operation Ledger 上**仍欠着对账**的
    tool_call_id 列表。由调用方查账本得出——本函数是纯派生投影，不碰存储（同
    `local_fuse` 的分工）。非空时一律附 `reconcile` 子对象，而 `state` 只在**暂停态**
    被它覆盖成 `needs_reconcile`（`03 §5`）：

      * run **暂停** ⇒ `state` 报 `needs_reconcile`。这是**覆盖**而非替换：
        `reason` / `trigger_dimension` / `continuation` 照旧在——"为什么停"与"停了
        之后欠了什么"是两件事，都要能看见。覆盖的理由是客户端只看 `state` 就会把这条
        run 当普通暂停，给出一个点了必然 409 的恢复入口（`03 §5`：对账优先于恢复），
        状态词是唯一的那个刹车灯。
      * run **在途** ⇒ `state` 仍是 `active`（它确实还在跑：一条 MUTATING 超时留下的
        未证行不改变这件事），欠账由 `reconcile` 子对象表达。
      * run **已终态** ⇒ `state` 保持终态名（`completed` 是既成事实，不能因为账本上
        另有一笔欠账就把它报成没跑完），`reconcile` 子对象照旧出现，让"这个会话还欠
        一笔对账"有地方可读。

    空列表**不落键**（同本函数对"缺席 vs 空值"的一贯口径：没有欠账与欠账为空不是
    同一件事，用一个恒为 null 的键表达会让后者看起来像前者）。
    """
    if state.paused is not None:
        projection: dict[str, Any] = dict(state.paused.as_projection())
    else:
        projection = {
            "run_id": state.run_id,
            "version": state.version,
            "state": state.terminal_type or ("active" if state.run_id else "none"),
            "limits": state.limits.as_projection(),
            "consumed": state.consumed.as_projection(),
            "remaining": state.consumed.remaining(state.limits),
            "local_fuse": local_fuse.as_projection() if local_fuse else None,
        }
    if reconcile_pending:
        projection["reconcile"] = {
            "state": STATE_NEEDS_RECONCILE,
            "tool_call_ids": sorted(reconcile_pending),
        }
        # **只有暂停态**才覆盖 `state`：那个理由（"客户端只看 state 就会给出一个点了
        # 必然 409 的恢复入口"）只对暂停成立。在途 run 报 `active` 才是事实——它确实
        # 还在跑（一条 MUTATING 超时留下的未证行不改变这一点）；已终态的 run 报它自己
        # 那个终态名（`completed` 是既成事实）。两种情形下 `reconcile` 子对象照旧出现，
        # 欠账照样看得见。
        if state.paused is not None:
            projection["state"] = STATE_NEEDS_RECONCILE
    projection["enforcement"] = accounting.as_projection()
    return projection


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
    blocked_by: Sequence[str] = (),
) -> dict[str, Any]:
    """确定性 continuation：**只**用已持久化事实组装（`02 §5.2`）。

    允许的事实：各维度的计数（轮数 / 请求数 / token / 成本 / 工具调用数 / 工具结果数）
    与 ceiling 本身。**不允许**：把"工具调用已发出"说成"工具已成功"、编造进展、编造
    工具结果（`02 §5.2` / ADR-0044 D3）。所以 `remaining` 里只说"停止发生在什么之前"，
    具体待办由恢复后的模型自己从历史里看见。

    账目**未知**的维度在文案里写"未知"而不是 0（`11 §6.1`：不可得 ≠ 0；这里的一句
    "已花 0 元"会直接骗到正在决定要不要继续的人）。

    `blocked_by`（`#315`）交给 `apply_blocked_by` 统一改写——**模型给的 continuation
    走的是同一个改写**（见那里的理由）："已确证的阻塞项"是事实，不是某种 continuation
    来源的装饰。
    """
    items = [event for event in events if event.run_id == run_id]
    tool_results = sum(1 for event in items if event.type == TOOL_RESULT)
    ceiling = limits.ceiling_of(trigger_dimension)
    if trigger_dimension == TRIGGER_RUN_DEADLINE:
        # deadline 的"下一步安全动作"与预算维度**不是**同一个动作：这里要的是一个新的
        # 未来时刻，不是把一个数字抬高。照抄预算那句会让 operator 去抬一个与停止原因
        # 无关的 ceiling（`02 §5.1`：三层控制互不替代的同一方向）。
        action = (
            f"给出新的未来 {trigger_dimension}（RFC 3339 UTC 绝对时刻）后以同一 run_id 恢复："
            f"本次 deadline={_value_text(ceiling)} 已到点（{_dimension_text(consumed, trigger_dimension)}）；"
            f"恢复请求需带 expected_version 与 resume_basis={RESUME_BASIS_BUDGET_INCREASE}"
        )
    else:
        action = (
            f"提高绝对 ceiling（{trigger_dimension}）后以同一 run_id 恢复："
            f"当前 consumed={_dimension_text(consumed, trigger_dimension)}，"
            f"ceiling={_value_text(ceiling)}；"
            f"恢复请求需带 expected_version 与 resume_basis={RESUME_BASIS_BUDGET_INCREASE}"
        )
    return apply_blocked_by({
        "completed": [
            (
                f"本逻辑 run 已消耗 {consumed.agent_turns} 个 agent turn、"
                f"{_value_text(consumed.model_requests)} 次 Provider 请求"
            ),
            (
                f"累计 token：{_value_text(consumed.total_tokens)}；"
                f"累计成本（USD）：{_decimal_text(consumed.cost_usd) or '未知'}"
            ),
            # 工具两个 counter 取 `consumed`（= `02 §5.1` 的计数点：**已接纳**的逻辑调用
            # 与**实际**尝试），不在这里重数 `tool/call` 事件：后者含准入前被拒的调用，
            # 拿它当"已接纳"会把被拒的调用说成已接纳（`#314` 之前这里就是这么数的）。
            # 工具结果条数是另一件事（含被拒调用的结果），所以照旧从事件数，并如实
            # 用"已落 N 条结果"而不是"其中 N 个"——两者不再是子集关系。
            (
                f"已接纳 {_value_text(consumed.tool_calls)} 个工具调用"
                f"（{_value_text(consumed.tool_attempts)} 次实际尝试）；"
                f"已落 {tool_results} 条工具结果"
            ),
        ],
        "remaining": [
            "暂停发生在下一轮模型决策之前：恢复后由模型从会话历史继续",
        ],
        "blockers": [
            (
                f"{trigger_dimension} 到顶："
                f"consumed={_dimension_text(consumed, trigger_dimension)}, "
                f"ceiling={_value_text(ceiling)}"
                if trigger_dimension != TRIGGER_RUN_DEADLINE
                else f"{trigger_dimension} 已到点：deadline={_value_text(ceiling)}"
            ),
        ],
        CONTINUATION_ACTION_KEY: action,
    }, blocked_by)


def apply_blocked_by(
    continuation: dict[str, Any], blocked_by: Sequence[str],
) -> dict[str, Any]:
    """把**已确证**的额外阻塞项并进一份 continuation（`#315`，两个来源共用）。

    `blocked_by` 今天唯一的来源是 `AgentRuntime._raise_reconcile_required`：
    "存在未 reconcile 的副作用"。它与 continuation 是**模型给的**还是确定性组装的
    无关——那件事已经确证了（Ledger 行 + 已落盘事件），不是某种来源才有的装饰。

    ⚠ 这一处是 2026-09-26 两轴审查那个 P1 的**后半**：前半是"闸门不以暂停原因为条件"
    （见 runtime），后半是"改写不能只发生在确定性那一支"——预算暂停在模型 closeout
    成功时会走 `CLOSEOUT_MODEL` 分支，若那里不改写，暂停载荷就留着模型写的
    "提高 ceiling 后恢复"，而那次恢复必被 409 挡死（`03 §5` / ADR-0044 D4 禁止的
    "暗示可安全续跑"）。

    改写两件事：`blockers` 追加（同一条已存在则不重复追加）、`next_safe_action` 换成
    `_reconcile_first_action`。返回**新字典**（调用方的对象不被就地改）。
    """
    if not blocked_by:
        return continuation
    merged = dict(continuation)
    blockers = list(merged.get("blockers") or [])
    for item in blocked_by:
        if item not in blockers:
            blockers.append(item)
    merged["blockers"] = blockers
    merged[CONTINUATION_ACTION_KEY] = _reconcile_first_action(blocked_by)
    return merged


def _reconcile_first_action(blocked_by: Sequence[str]) -> str:
    """有未 reconcile 副作用时的"下一步安全动作"（`#315`）。

    它**替换**掉"抬高 ceiling 后恢复"那句：在 reconcile 解除之前，恢复请求会被
    开工前拒绝（`11 §6.1` 的 409「存在未 reconcile 的副作用」），照抄预算动作
    等于指一条走不通的路。这里也不写出"重跑那个工具"——不变量 #14：未知高风险
    工具不盲重跑，重跑与否是**人**看完账本后的裁决。
    """
    count = len(blocked_by)
    return (
        f"先 reconcile 未结清的副作用（{count} 项）：确认它到底有没有落盘，"
        "再由 ReconcileCallback 给出裁决——裁决落地前本 run 不可恢复"
        "（03 §5：对账优先于恢复），也不要重跑那条调用（不变量 #14）"
    )


def _dimension_text(consumed: BudgetConsumed, dimension: str) -> str:
    """某一维度的已消耗文案（未知如实说未知）。"""
    tool_name = tool_name_of_dimension(dimension)
    if tool_name is not None:
        return _value_text(consumed.calls_for(tool_name))
    if dimension == TRIGGER_RUN_DEADLINE:
        # deadline 没有"已消耗"这个量（它不计数，只比时刻）。这里给的是**状态**，
        # 不是编造一个 0 或"未知"——0 会被读成"花了 0 时间"，"未知"会被读成"没数"，
        # 两者都是假话（`11 §6.1` 的 unavailable 纪律同一方向）。
        return "已到点"
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
    if isinstance(value, datetime):
        # deadline 的 ceiling 是时刻：一律按 wire 的 UTC 写法展示（operator 抄进
        # 恢复请求时要能直接用，`+00:00` 与 `Z` 两种写法对人是两回事、对机器是一回事）。
        return _deadline_text(value) or "未知"
    return str(value)
