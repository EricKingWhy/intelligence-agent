"""#298 / MEM-V2-2：一个记忆作业的预算账本与主/备尝试序列（R9 / R10 / PRD §5.3）。

两件事住在一个模块——**能花多少**（墙钟 / 调用次数 / 输入 token）与**怎么花**（主 1+2、
备 1+1，非瞬时错误直接停）。分开会让"还剩多少预算"和"下一个该问谁"各持一套状态，
而它们在执行器里是同一次循环里的同一个决定。

# 为什么是纯对象、不碰 I/O

R10 的四条上界要在**确定性假模型**下可判定（Verification 第 3 条）。账本因此只记
"已经花了多少"，时钟由构造方注入：真实执行器传 `time.monotonic`，测试传手摇计数器。
本模块没有任何 await / sleep / 真实等待。

# 数字来路

四条上界与两条尝试预算**逐字来自 PRD §5.3 第 1、2、5 条**，字面写在常量里而**不是**从
配置读——Contracts 一节把预算钉成 "fixed by §5.3"，所以这里没有 Settings 字段：
放宽预算不是部署旋钮，改它要改 PRD。测试需要更小的预算时按构造参数传 `limits`，
那是测试替身，不是配置面。

# 执行器的契约（谁用这两件东西，以及用法）

1. 入队前先确定有 primary（`roles.primary is not None`）。**没有 primary 就不该开跑**——
   本模块不表达"没有主模型"这个状态（它是装配层的缺席，不是尝试序列的一步）。
2. 每次调用前 `begin_call(input_tokens=<本次 prompt 的估算>)`；抛 `BudgetExhausted` ⇒
   终态 degraded、**零写入**（R10 末句）。估算复用 `context.tokens.estimate_message_tokens`。
3. 把 `output_token_limit` 作为该次调用的 `max_tokens`（输出上界由 provider 参数强制；
   账本不事后核对——响应已经产生，事后发现超限也只剩丢弃这一个动作）。
4. 把 `remaining_seconds()` 作为该次调用的超时上界（120 秒是**整作业**的墙钟）。
5. 调用抛异常时 `next_attempt(...)`；返回 `None` ⇒ 终态 degraded、零写入。
6. `has_fallback` 取 `roles.has_fallback`。

# 失败分类不在这里实现

"瞬时"的判据住在 `model.fallback.is_transient_model_error`（ADR-0014 决策 15，与主链
共用一份）。本模块**不重复实现**一份分类：R9 的四个非瞬时类别在本仓各有真实类型——
认证 401 / 权限 403 / 内容策略 400 都是 4xx（判据如实判 False），schema 是
`formation.ModelOutputError`（`ValueError` ⇒ 同样 False）。重复实现会让两处分类漂移，
而漂移的后果是"该停的还在烧预算"，比"分类函数放在隔壁模块"难查得多。
`test_a_non_transient_failure_is_neither_retried_nor_switched` 用真实异常类型把这条钉住。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from agent_harness.model.fallback import is_transient_model_error

#: PRD §5.3 第 5 条：整作业墙钟上界（秒）。
MEMORY_JOB_TIMEOUT_SECONDS = 120.0
#: PRD §5.3 第 5 条：单作业模型调用次数上界。
MEMORY_JOB_MAX_CALLS = 5
#: PRD §5.3 第 5 条：**累计**输入 token 上界。
MEMORY_JOB_MAX_INPUT_TOKENS = 32_000
#: PRD §5.3 第 5 条：单次调用的输出 token 上界（非累计）。
MEMORY_JOB_MAX_OUTPUT_TOKENS = 4_000
#: PRD §5.3 第 1 条：primary = 初次 + 至多 2 次重试。
PRIMARY_MAX_ATTEMPTS = 3
#: PRD §5.3 第 2 条：fallback = 初次 + 至多 1 次重试。
FALLBACK_MAX_ATTEMPTS = 2


class BudgetDimension(str, Enum):
    """耗尽的维度。T6 据此落 `memory/degraded` 的稳定 `reason_code`（§6.5）。

    字面量是**观测契约**的一部分（排障者按它对账），不随内部命名变动。
    """

    DEADLINE = "deadline"
    CALLS = "calls"
    INPUT_TOKENS = "input_tokens"


class BudgetExhausted(Exception):
    """预算耗尽 ⇒ 终态 degraded 且**零写入**（R10 末句）。

    刻意**不**继承 `ValueError`：调用方对它的处置是"记为降级终态"，而不是"输入非法"。
    继承 `ValueError` 会让 `except ValueError` 顺手吞掉它。
    """

    def __init__(self, dimension: BudgetDimension) -> None:
        super().__init__(f"memory job budget exhausted: {dimension.value}")
        self.dimension = dimension


@dataclass(frozen=True, slots=True)
class MemoryBudgetLimits:
    """四条上界的一个集合（默认 = PRD §5.3 的字面值）。"""

    timeout_seconds: float = MEMORY_JOB_TIMEOUT_SECONDS
    max_calls: int = MEMORY_JOB_MAX_CALLS
    max_input_tokens: int = MEMORY_JOB_MAX_INPUT_TOKENS
    max_output_tokens_per_call: int = MEMORY_JOB_MAX_OUTPUT_TOKENS


DEFAULT_BUDGET_LIMITS = MemoryBudgetLimits()


class MemoryJobBudget:
    """一个记忆作业的资源账本（时间 / 调用次数 / 累计输入 token）。

    单属主、无锁：并发由 durable job 的 lease（`jobs.claim`）保证——同一个作业
    只有一个执行器，所以账本不需要线程安全（与 job store 的分工一致）。
    """

    def __init__(
        self,
        *,
        limits: MemoryBudgetLimits = DEFAULT_BUDGET_LIMITS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._started_at = clock()
        self._calls = 0
        self._input_tokens = 0

    @property
    def limits(self) -> MemoryBudgetLimits:
        return self._limits

    @property
    def calls_used(self) -> int:
        return self._calls

    @property
    def input_tokens_used(self) -> int:
        return self._input_tokens

    @property
    def output_token_limit(self) -> int:
        """单次调用的输出上界——由调用方作为 `max_tokens` 传给 provider。"""
        return self._limits.max_output_tokens_per_call

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self._started_at

    def remaining_seconds(self) -> float:
        """本作业还剩的墙钟（**非负**）。执行器把它作为下一次调用的超时上界。

        返回值封底为 0 而不是负数：调用方拿它当 `timeout=` 传下去时，负数是一个
        更坏的输入（有的 SDK 会把它读成"不设超时"）。
        """
        return max(0.0, self._limits.timeout_seconds - self.elapsed_seconds)

    def begin_call(self, *, input_tokens: int) -> None:
        """记一次模型调用；任一维度已耗尽则抛 `BudgetExhausted`。

        判据顺序（deadline → calls → input_tokens）刻意固定：多个维度同时耗尽时
        必须报出**同一个**原因，否则同一状态的观测会随检查顺序漂移，`reason_code`
        也就不可对账了。顺序不是优先级——先报哪个都不改变"这是一次降级终态"。

        记账不做半截：三个判据全部通过之后才 += 计数，因此被拒的那次不占调用次数、
        也不计 token（`test_a_refused_call_does_not_consume_anything`）。
        """
        if input_tokens < 0:
            raise ValueError(f"input_tokens must not be negative, got {input_tokens}")
        if self.elapsed_seconds >= self._limits.timeout_seconds:
            raise BudgetExhausted(BudgetDimension.DEADLINE)
        if self._calls >= self._limits.max_calls:
            raise BudgetExhausted(BudgetDimension.CALLS)
        if self._input_tokens + input_tokens > self._limits.max_input_tokens:
            raise BudgetExhausted(BudgetDimension.INPUT_TOKENS)
        self._calls += 1
        self._input_tokens += input_tokens


class MemoryModelRole(str, Enum):
    """记忆作业的两个模型角色（PRD §5.3 第 3 条的别名）。"""

    PRIMARY = "primary"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class MemoryAttempt:
    """一次模型调用尝试：问哪个角色 + 这是**该角色内**的第几次（1-based）。

    编号刻意按角色重置而不做全局计数：观测里"备用试到第几次"与"主试到第几次"
    是两个独立事实，全局编号会把它们混成一个读不出来的数。
    """

    role: MemoryModelRole
    number: int


#: 序列的起点（primary 的初次调用）。
FIRST_ATTEMPT = MemoryAttempt(MemoryModelRole.PRIMARY, 1)


def next_attempt(
    current: MemoryAttempt, error: BaseException, *, has_fallback: bool
) -> MemoryAttempt | None:
    """R9 的下一次尝试；返回 `None` = 不再重试（终态 degraded、零写入）。

    - **非瞬时**错误（认证 / 权限 / schema / 内容策略 / 任何意外异常）一律返回 `None`：
      换一个 provider 修不好一个认证错，也修不好一个 bug。R3 的解析失败（schema）
      走同一条路——它消耗一次尝试，但不再获得重试。
    - primary：第 1→2→3 次；第 3 次之后**且有备用**才切到 fallback 的初次。
    - fallback：第 1→2 次；之后停。**never 切回** primary。
    """
    if not is_transient_model_error(error):
        return None
    if current.role is MemoryModelRole.PRIMARY:
        if current.number < PRIMARY_MAX_ATTEMPTS:
            return MemoryAttempt(MemoryModelRole.PRIMARY, current.number + 1)
        if has_fallback:
            return MemoryAttempt(MemoryModelRole.FALLBACK, 1)
        return None
    if current.number < FALLBACK_MAX_ATTEMPTS:
        return MemoryAttempt(MemoryModelRole.FALLBACK, current.number + 1)
    return None
