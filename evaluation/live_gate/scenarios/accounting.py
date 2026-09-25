"""Live Gate 的账本对账判据（`#313` T5）：从**轨迹**独立重算四维，再与产品读数对账。

## 为什么这里要再实现一遍计数器

判据是"**产品的投影 == 轨迹的事实**"。用产品自己的 `consumed_from_events` 去校验产品的
payload，等于让同一处实现自己给自己打分（分子分母同源，分歧被抹平）。所以本模块按
`02 §5.1` 的计数点**独立**重算一遍：两条独立实现算出同一个数才算对账成立，分歧会以
`FAIL` 暴露出来（`#213` 的失效形状是"手抄读数错一格而没有任何东西会报错"）。

## 三条语义（与产品一致，但独立实现；错一条都判红）

1. `model_requests` 数**每一次实际请求**（primary / fallback / closeout，失败与传输中断
   也在内），与 `agent_turns`（只数被接纳的决策）**分开**；
2. `total_tokens` 只累加 Provider 自报的 `usage.total_tokens`；**任一格没自报 ⇒ 该维度
   未知（`None`）**，不是 0（`11 §6.1`：不可得 ≠ 0）。一格请求都没有的空和才是 0；
3. `cost_usd` 同理：只认 `model/request.data.cost_usd`（十进制**字符串**，不引入浮点），
   不做任何费率推算。

判据只做**相等/形状**比对，不做"看起来合理"的主观判断（`evaluation/assertions.py` 的
同一姿势：断不出来就判 False，不猜）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from agent_harness.session import MODEL_COMPLETED, MODEL_REQUEST
from evaluation.live_gate.schema import AssertionResult


@dataclass(frozen=True)
class RequestAccounting:
    """从事件流重算出来的账（四维 + 看得见来源的分解）。"""

    turns: int
    requests: int
    by_role: dict[str, int]
    failed: int
    reported_usage: int
    reported_cost: int
    tokens: int | None
    cost: Decimal | None

    def summary(self) -> str:
        """一行读数（进断言 detail：证据里要看得见"哪几格没自报"）。"""
        roles = ",".join(f"{role}:{count}" for role, count in sorted(self.by_role.items()))
        return (
            f"轨迹重算：agent_turns={self.turns} model_requests={self.requests}"
            f"（角色 {roles or '无'}，失败 {self.failed}）"
            f" total_tokens={self.tokens if self.tokens is not None else '未知'}"
            f"（自报 {self.reported_usage}/{self.requests} 格）"
            f" cost_usd={_text(self.cost) if self.cost is not None else '未知'}"
            f"（自报 {self.reported_cost}/{self.requests} 格）"
        )


def _text(value: Decimal | None) -> str:
    return "未知" if value is None else format(value, "f")


def _decimal_or_none(raw: Any) -> Decimal | None:
    """十进制读数 → `Decimal`；形状不认识 ⇒ `None`（**不猜、不四舍五入**）。

    只接受 `str` / `int` / `Decimal`：`float` 会引入与 wire 不等价的二进制近似
    （`11 §6.1`：二进制浮点相等不是契约），所以它按"不可解析"处理。

    与产品侧的唯一读数实现（`model/accounting.py`）**不等价**：那一版收 `float`、拒负值
    与非有限。生产写入点只落 `format(Decimal, "f")` 的有限非负串 ⇒ 今天不可达；这里按判据
    侧更严的形状处理 —— 形态分歧只会判红（fail-closed），不会放行。
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, str):
        try:
            return Decimal(raw.strip())
        except InvalidOperation:
            return None
    return None


def _int_or_none(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def request_accounting(events: Iterable[Any]) -> RequestAccounting:
    """按计数点从事件流重算四维（独立于 `agent/run_budget.py::consumed_from_events`）。"""
    turns = 0
    requests = 0
    by_role: dict[str, int] = {}
    failed = 0
    reported_usage = 0
    reported_cost = 0
    tokens: int | None = 0
    cost: Decimal | None = Decimal(0)
    for event in events:
        if event.type == MODEL_COMPLETED:
            turns += 1
            continue
        if event.type != MODEL_REQUEST:
            continue
        requests += 1
        data = event.data if isinstance(event.data, Mapping) else {}
        role = str(data.get("role") or "?")
        by_role[role] = by_role.get(role, 0) + 1
        if str(data.get("outcome") or "") != "completed":
            failed += 1
        usage = data.get("usage")
        reported_tokens = _int_or_none(
            usage.get("total_tokens") if isinstance(usage, Mapping) else None
        )
        if reported_tokens is not None:
            reported_usage += 1
        if tokens is not None:
            tokens = None if reported_tokens is None else tokens + reported_tokens
        event_cost = _decimal_or_none(data.get("cost_usd"))
        if event_cost is not None:
            reported_cost += 1
        if cost is not None:
            cost = None if event_cost is None else cost + event_cost
    return RequestAccounting(
        turns=turns, requests=requests, by_role=by_role, failed=failed,
        reported_usage=reported_usage, reported_cost=reported_cost,
        tokens=tokens, cost=cost,
    )


def consumed_counter_assertions(
    *, facts: RequestAccounting, consumed: Any, label: str,
) -> list[AssertionResult]:
    """把产品某一面（暂停 / 恢复快照、API 投影的 `consumed`）与轨迹重算结果对账。

    三条判据缺一不可：请求数（含"与 `agent_turns` 分开"）、token、cost。`consumed`
    形状不可解析 ⇒ 三条全红（**不**退化成"跳过"——缺少对账对象就是没对上）。
    """
    payload = consumed if isinstance(consumed, Mapping) else None
    turns = _int_or_none(payload.get("agent_turns")) if payload is not None else None
    requests = _int_or_none(payload.get("model_requests")) if payload is not None else None
    tokens = _int_or_none(payload.get("total_tokens")) if payload is not None else None
    cost = _decimal_or_none(payload.get("cost_usd")) if payload is not None else None
    return [
        AssertionResult(
            name=f"{label}.request_count",
            ok=requests == facts.requests and turns == facts.turns,
            detail=(
                f"payload model_requests={requests!r} agent_turns={turns!r}"
                f"；{facts.summary()}"
            ),
        ),
        AssertionResult(
            name=f"{label}.tokens",
            ok=tokens == facts.tokens,
            detail=(
                f"payload total_tokens={tokens if tokens is not None else '未知'}"
                f"；轨迹 {'未知（有请求未自报）' if facts.tokens is None else facts.tokens}"
                f"（自报 {facts.reported_usage}/{facts.requests} 格；不可得不得记 0）"
            ),
        ),
        AssertionResult(
            name=f"{label}.cost",
            ok=cost == facts.cost,
            detail=(
                f"payload cost_usd={_text(cost) if cost is not None else '未知'}"
                f"；轨迹 {_text(facts.cost) if facts.cost is not None else '未知'}"
                f"（自报 {facts.reported_cost}/{facts.requests} 格；不臆造费率）"
            ),
        ),
    ]


def terminal_counter_assertions(
    *, facts: RequestAccounting, terminal: Any, label: str,
) -> list[AssertionResult]:
    """终态载荷（`run/completed`）里的 `usage_total` / `cost_usd` 与轨迹对账。

    与 `consumed_counter_assertions` 是同一条判据的另一张面：暂停快照是**恢复的基数**，
    终态载荷是**已结束那次执行**的读数（`11 §6.1`）。

    ⚠ 作用域限制（`#313` 补审 Correctness F1 登记，待裁决）：runtime 的 `usage_total` 每次
    执行新建、只累加**本次**执行 ⇒ 仅在「一次执行 == 一个 run」时它与按 run 重算的 `facts`
    同值。**同 run 恢复过**的运行上两者必然不等，所以暂停 / 恢复场景目前**不接**这张面
    （接了会恒红）。这个函数因此只适用于单执行 run（smoke / long_task 那种直路）。
    """
    payload = terminal if isinstance(terminal, Mapping) else None
    usage = payload.get("usage_total") if payload is not None else None
    tokens = _int_or_none(usage.get("total_tokens") if isinstance(usage, Mapping) else None)
    cost = _decimal_or_none(payload.get("cost_usd")) if payload is not None else None
    return [
        AssertionResult(
            name=f"{label}.terminal_tokens",
            ok=tokens == facts.tokens,
            detail=(
                f"run/completed.usage_total.total_tokens="
                f"{tokens if tokens is not None else '缺失或未知'}"
                f"；轨迹 {'未知' if facts.tokens is None else facts.tokens}"
                f"（Provider 自报累加，不可得不得记 0）"
            ),
        ),
        AssertionResult(
            name=f"{label}.terminal_cost",
            ok=cost == facts.cost,
            detail=(
                f"run/completed.cost_usd={_text(cost) if cost is not None else '未知'}"
                f"；轨迹 {_text(facts.cost) if facts.cost is not None else '未知'}"
                "（只认 Provider 归属的读数）"
            ),
        ),
    ]


def plain_path_request_assertion(*, facts: RequestAccounting, label: str) -> AssertionResult:
    """无 fallback / 无 pause 的直路：每个被接纳的决策**恰好**一次成功请求。

    判据是 `failed == 0 and requests == turns` 的**合取**，不是恒等式
    `requests - turns == failed`：失败请求不产出决策（不增 `agent_turns`），所以直路上两者
    必须同时成立；而恒等式在"fallback 接住了一次失败"的形状（`requests=2 / turns=1 /
    failed=1`）上**同样成立** —— 拿它当判据会把"重试过"静默读成"干净直路"。
    """
    ok = facts.failed == 0 and facts.requests == facts.turns
    return AssertionResult(
        name=f"{label}.one_request_per_decision", ok=ok,
        detail=f"{facts.summary()}（直路：requests == turns 且无失败请求）",
    )
