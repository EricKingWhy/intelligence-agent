"""#298 / MEM-V2-2 的作业预算与主/备尝试序列（R9 / R10 / PRD §5.3）。

两条 seam，都是纯对象、都不碰 I/O：

- `MemoryJobBudget`：120 秒 / 5 次调用 / 32k 累计输入 token 的账本；
- `next_attempt`：主 1+2、备 1+1，**非瞬时错误不重试也不切备**。

时钟由构造方注入 ⇒ "已经过了 120 秒"在测试里是**手摇**的，不是真的等（Verification
第 3 条要求确定性假模型下的预算判定）。

# 非瞬时那一条为什么用**真实异常类型**钉

R9 的四个非瞬时类别（authentication / permission / schema / policy）在本仓各有对应的
真实类型：认证与权限是带 `status_code` 的 4xx，schema 是 `formation.ModelOutputError`。
本文件直接用它们（以及真实 `httpx` 异常）而不是自造的假类，因为判据本身住在
`model.fallback.is_transient_model_error`——用假类测等于在测另一套东西。
"""

from __future__ import annotations

import httpx
import pytest

from agent_harness.memory.v2.budget import (
    FALLBACK_MAX_ATTEMPTS,
    FIRST_ATTEMPT,
    MEMORY_JOB_MAX_CALLS,
    MEMORY_JOB_MAX_INPUT_TOKENS,
    MEMORY_JOB_MAX_OUTPUT_TOKENS,
    MEMORY_JOB_TIMEOUT_SECONDS,
    PRIMARY_MAX_ATTEMPTS,
    BudgetDimension,
    BudgetExhausted,
    MemoryAttempt,
    MemoryBudgetLimits,
    MemoryJobBudget,
    MemoryModelRole,
    next_attempt,
)
from agent_harness.memory.v2.executor import DegradedReason
from agent_harness.memory.v2.formation import ModelOutputError
from agent_harness.model.fallback import is_transient_model_error
from agent_harness.model.stall import ModelStallError

# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


class _Clock:
    """手摇时钟（`MemoryJobBudget` 的 `clock` 注入点）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _StatusError(Exception):
    """openai SDK 风格：异常自带 `status_code`（判据按码分类的那一条通道）。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """真实 `httpx.HTTPStatusError`（判据的另一个通道：按 `response.status_code`）。"""
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status_code, request=request))


def _budget(clock: _Clock, **limits: float) -> MemoryJobBudget:
    if limits:
        return MemoryJobBudget(clock=clock, limits=MemoryBudgetLimits(**limits))
    return MemoryJobBudget(clock=clock)


def _walk(*, has_fallback: bool, error: BaseException) -> list[MemoryAttempt]:
    """从头走到停，返回**尝试序列**（循环上限刻意用调用预算兜底，防死循环挂住用例）。"""
    attempts: list[MemoryAttempt] = []
    attempt: MemoryAttempt | None = FIRST_ATTEMPT
    while attempt is not None:
        attempts.append(attempt)
        assert len(attempts) <= MEMORY_JOB_MAX_CALLS, "尝试序列没有终止"
        attempt = next_attempt(attempt, error, has_fallback=has_fallback)
    return attempts


# --------------------------------------------------------------------------------------
# 0 · 上界本身
# --------------------------------------------------------------------------------------


def test_the_budget_numbers_are_the_ones_the_prd_fixes() -> None:
    assert MEMORY_JOB_TIMEOUT_SECONDS == 120.0
    assert MEMORY_JOB_MAX_CALLS == 5
    assert MEMORY_JOB_MAX_INPUT_TOKENS == 32_000
    assert MEMORY_JOB_MAX_OUTPUT_TOKENS == 4_000
    assert PRIMARY_MAX_ATTEMPTS == 3
    assert FALLBACK_MAX_ATTEMPTS == 2


def test_the_role_attempt_budgets_add_up_to_the_call_budget() -> None:
    """两条独立数字必须自洽：否则"尝试序列"能合法地跑出调用预算之外。"""
    assert PRIMARY_MAX_ATTEMPTS + FALLBACK_MAX_ATTEMPTS == MEMORY_JOB_MAX_CALLS


def test_the_budget_dimensions_are_stable_strings() -> None:
    """`memory/degraded` 的 `reason_code` 由它们派生 ⇒ 字面量不能随手改。"""
    assert [dimension.value for dimension in BudgetDimension] == [
        "deadline", "calls", "input_tokens",
    ]


# --------------------------------------------------------------------------------------
# 1 · 账本
# --------------------------------------------------------------------------------------


def test_a_fresh_budget_reports_nothing_spent() -> None:
    budget = _budget(_Clock())

    assert budget.calls_used == 0
    assert budget.input_tokens_used == 0
    assert budget.elapsed_seconds == 0.0
    assert budget.remaining_seconds() == MEMORY_JOB_TIMEOUT_SECONDS
    assert budget.output_token_limit == MEMORY_JOB_MAX_OUTPUT_TOKENS


def test_a_call_is_recorded_with_its_input_tokens() -> None:
    budget = _budget(_Clock())

    budget.begin_call(input_tokens=100)
    budget.begin_call(input_tokens=50)

    assert budget.calls_used == 2
    assert budget.input_tokens_used == 150


def test_the_sixth_call_is_refused() -> None:
    budget = _budget(_Clock())
    for _ in range(MEMORY_JOB_MAX_CALLS):
        budget.begin_call(input_tokens=1)

    with pytest.raises(BudgetExhausted) as caught:
        budget.begin_call(input_tokens=1)

    assert caught.value.dimension is BudgetDimension.CALLS
    assert budget.calls_used == MEMORY_JOB_MAX_CALLS


def test_input_tokens_accumulate_across_calls() -> None:
    budget = _budget(_Clock())
    budget.begin_call(input_tokens=20_000)

    with pytest.raises(BudgetExhausted) as caught:
        budget.begin_call(input_tokens=20_000)

    assert caught.value.dimension is BudgetDimension.INPUT_TOKENS
    assert budget.input_tokens_used == 20_000


def test_exactly_reaching_the_input_budget_is_allowed_and_one_more_token_is_not() -> None:
    budget = _budget(_Clock())
    budget.begin_call(input_tokens=MEMORY_JOB_MAX_INPUT_TOKENS)

    assert budget.input_tokens_used == MEMORY_JOB_MAX_INPUT_TOKENS
    with pytest.raises(BudgetExhausted) as caught:
        budget.begin_call(input_tokens=1)

    assert caught.value.dimension is BudgetDimension.INPUT_TOKENS


def test_a_refused_call_does_not_consume_anything() -> None:
    """记账不做半截：被拒的那次不占调用次数、也不计 token。"""
    budget = _budget(_Clock())
    budget.begin_call(input_tokens=5)

    with pytest.raises(BudgetExhausted):
        budget.begin_call(input_tokens=MEMORY_JOB_MAX_INPUT_TOKENS + 1)

    assert budget.calls_used == 1
    assert budget.input_tokens_used == 5


def test_a_negative_input_token_count_is_refused() -> None:
    """负数是调用方的 bug（例如算错了 token 估算），不能变成"倒赚预算"。"""
    budget = _budget(_Clock())

    with pytest.raises(ValueError):
        budget.begin_call(input_tokens=-1)

    assert budget.calls_used == 0
    assert budget.input_tokens_used == 0


def test_the_output_limit_is_per_call_not_cumulative() -> None:
    budget = _budget(_Clock())
    for _ in range(3):
        budget.begin_call(input_tokens=1)

    assert budget.output_token_limit == MEMORY_JOB_MAX_OUTPUT_TOKENS


def test_smaller_limits_are_honoured() -> None:
    """测试替身要能收紧预算（真实数字是 PRD 固定的，不是部署旋钮）。"""
    budget = _budget(
        _Clock(), timeout_seconds=1.0, max_calls=2, max_input_tokens=10,
        max_output_tokens_per_call=7)

    assert budget.output_token_limit == 7
    budget.begin_call(input_tokens=10)
    with pytest.raises(BudgetExhausted) as caught:
        budget.begin_call(input_tokens=1)

    assert caught.value.dimension is BudgetDimension.INPUT_TOKENS


# --------------------------------------------------------------------------------------
# 2 · 墙钟
# --------------------------------------------------------------------------------------


def test_elapsed_time_comes_from_the_injected_clock() -> None:
    clock = _Clock()
    budget = _budget(clock)

    assert budget.elapsed_seconds == 0.0
    clock.advance(12.5)

    assert budget.elapsed_seconds == 12.5


def test_remaining_seconds_counts_down_and_never_goes_negative() -> None:
    clock = _Clock()
    budget = _budget(clock)
    clock.advance(30.0)

    assert budget.remaining_seconds() == pytest.approx(MEMORY_JOB_TIMEOUT_SECONDS - 30.0)

    clock.advance(1_000.0)

    assert budget.remaining_seconds() == 0.0


def test_a_call_after_the_deadline_is_refused() -> None:
    clock = _Clock()
    budget = _budget(clock)
    clock.advance(MEMORY_JOB_TIMEOUT_SECONDS + 0.001)

    with pytest.raises(BudgetExhausted) as caught:
        budget.begin_call(input_tokens=1)

    assert caught.value.dimension is BudgetDimension.DEADLINE
    assert budget.calls_used == 0


def test_a_call_just_inside_the_deadline_is_allowed() -> None:
    clock = _Clock()
    budget = _budget(clock)
    clock.advance(MEMORY_JOB_TIMEOUT_SECONDS - 0.001)

    budget.begin_call(input_tokens=1)

    assert budget.calls_used == 1


def test_exactly_reaching_the_deadline_is_refused() -> None:
    clock = _Clock()
    budget = _budget(clock)
    clock.advance(MEMORY_JOB_TIMEOUT_SECONDS)

    with pytest.raises(BudgetExhausted) as caught:
        budget.begin_call(input_tokens=1)

    assert caught.value.dimension is BudgetDimension.DEADLINE


def test_the_deadline_is_reported_when_several_dimensions_are_spent() -> None:
    """多个维度同时耗尽时报哪个：判据顺序固定 ⇒ 同一状态报同一个原因（可判定）。"""
    clock = _Clock()
    budget = _budget(clock, max_calls=0)
    clock.advance(MEMORY_JOB_TIMEOUT_SECONDS + 1)

    with pytest.raises(BudgetExhausted) as caught:
        budget.begin_call(input_tokens=1)

    assert caught.value.dimension is BudgetDimension.DEADLINE


def test_the_call_budget_is_reported_before_the_token_budget() -> None:
    budget = _budget(_Clock(), max_calls=1, max_input_tokens=0)
    budget.begin_call(input_tokens=0)

    with pytest.raises(BudgetExhausted) as caught:
        budget.begin_call(input_tokens=0)

    assert caught.value.dimension is BudgetDimension.CALLS


# --------------------------------------------------------------------------------------
# 3 · 尝试序列（R9）
# --------------------------------------------------------------------------------------


def test_the_first_attempt_is_the_primary_initial_call() -> None:
    assert FIRST_ATTEMPT.role is MemoryModelRole.PRIMARY
    assert FIRST_ATTEMPT.number == 1


def test_a_transient_failure_walks_three_primaries_then_two_fallbacks() -> None:
    """R9 / AC4 的形状，一条用例钉死：主 1+2，备 1+1，顺序不可换。"""
    attempts = _walk(has_fallback=True, error=_StatusError(503))

    assert [(attempt.role.value, attempt.number) for attempt in attempts] == [
        ("primary", 1), ("primary", 2), ("primary", 3),
        ("fallback", 1), ("fallback", 2),
    ]


def test_a_fully_exhausted_transient_run_costs_exactly_the_call_budget() -> None:
    assert len(_walk(has_fallback=True, error=_StatusError(503))) == MEMORY_JOB_MAX_CALLS


def test_a_transient_failure_without_a_fallback_stops_after_three_primaries() -> None:
    attempts = _walk(has_fallback=False, error=_StatusError(503))

    assert [(attempt.role.value, attempt.number) for attempt in attempts] == [
        ("primary", 1), ("primary", 2), ("primary", 3),
    ]


def test_the_attempt_number_restarts_at_one_for_the_fallback() -> None:
    """编号是**角色内**的第几次，不是全局第几次——否则观测里读不出"备用试到第几次"。"""
    third_primary = MemoryAttempt(MemoryModelRole.PRIMARY, PRIMARY_MAX_ATTEMPTS)

    switched = next_attempt(third_primary, _StatusError(500), has_fallback=True)

    assert switched is not None
    assert switched.role is MemoryModelRole.FALLBACK
    assert switched.number == 1


def test_the_fallback_never_hands_back_to_the_primary() -> None:
    """never 切回（与默认链的两级 fallback 同一条纪律）。"""
    last_fallback = MemoryAttempt(MemoryModelRole.FALLBACK, FALLBACK_MAX_ATTEMPTS)

    assert next_attempt(last_fallback, _StatusError(503), has_fallback=True) is None


@pytest.mark.parametrize(
    "error",
    [
        _StatusError(500),
        _StatusError(502),
        _StatusError(503),
        _StatusError(429),
        TimeoutError("slow upstream"),
        httpx.ConnectError("connection refused"),
        ModelStallError(60.0),
    ],
    ids=["500", "502", "503", "429", "timeout", "connect", "stall"],
)
def test_a_transient_failure_moves_the_sequence_forward(error: BaseException) -> None:
    assert next_attempt(FIRST_ATTEMPT, error, has_fallback=True) == MemoryAttempt(
        MemoryModelRole.PRIMARY, 2
    )


def test_a_real_http_status_error_is_classified_by_its_status_code() -> None:
    assert next_attempt(FIRST_ATTEMPT, _http_status_error(503), has_fallback=True) is not None
    assert next_attempt(FIRST_ATTEMPT, _http_status_error(401), has_fallback=True) is None


@pytest.mark.parametrize(
    "error",
    [
        ModelOutputError("model output is not JSON"),
        _StatusError(401),   # authentication
        _StatusError(403),   # permission
        _StatusError(400),   # policy（供应商侧内容策略拒绝）
        _StatusError(422),   # schema（请求/响应形状不被接受）
        RuntimeError("unexpected internal error"),
    ],
    ids=["schema", "auth", "permission", "policy", "unprocessable", "unexpected"],
)
@pytest.mark.parametrize(
    "current",
    [FIRST_ATTEMPT, MemoryAttempt(MemoryModelRole.FALLBACK, 1)],
    ids=["primary", "fallback"],
)
def test_a_non_transient_failure_is_neither_retried_nor_switched(
    error: BaseException, current: MemoryAttempt
) -> None:
    """R9 末句。意外内部错误也停：换一个 provider 修不好一个 bug（fail closed）。"""
    assert next_attempt(current, error, has_fallback=True) is None


def test_the_schema_case_is_the_formation_contract_error() -> None:
    """R9 的 "schema" 在本仓就是 `formation.ModelOutputError`（R3 的解析失败）。

    R3 说它"是一次失败尝试"——所以它消耗一次尝试，但不再获得重试。这条把两个 contract
    连起来：`ModelOutputError` 是 `ValueError`，因此 `is_transient_model_error` 判 False，
    R9 的"不重试"由**同一个判据**承担（本模块不重复实现一份分类）。
    """
    assert issubclass(ModelOutputError, ValueError)
    assert is_transient_model_error(ModelOutputError("x")) is False
    assert next_attempt(FIRST_ATTEMPT, ModelOutputError("x"), has_fallback=True) is None


def test_degraded_reasons_cover_every_budget_dimension() -> None:
    """`executor.DegradedReason` 的前三个必须与 `BudgetDimension` **逐字对齐**。

    `executor.py` 的 docstring 一直宣称"前三个逐字对齐、有用例钉着"，但那个用例名当时是
    未填的 `test_...` 占位符——等于把"未证"写成"已证"（T8 两轴审查 P3）。这条把它兑现。

    为什么仍需要一条静态锁：漂移的后果本身是响亮的（`DegradedReason(exhausted.dimension
    .value)` 会抛 `ValueError`），但"响亮"只在真跑到预算耗尽那条分支时才发生；这里锁的是
    "在跑到它之前，两套名字就没有分叉"。
    """
    assert {dimension.value for dimension in BudgetDimension} <= {
        reason.value for reason in DegradedReason
    }, "预算维度必须都能表示成降级归因码（executor 直接拿 dimension.value 构造它）"
