"""运行期 tool_call 前置决策钩子（decision hook）。

设计依据：`docs/agents/521-design-proposal.md` §4（IMP-02）。
机制对标 Claude Code `PreToolUse`（PORT DESIGN，不引依赖）：
before 可拦截（ALLOW / DENY / REWRITE），after 只读反馈——
after 走 notification 管道（#447 范围），本模块只实现 before 决策缝。

两条管道按"是否读返回值"分界（对标 Pi 运行期钩子，内部对照）：
notification = fire-and-forget、不读返回值；
decision = awaitable、读返回值、串行等待。

MCP elicitation 的 `accept` / `decline` / `cancel` 三态未来可映射到
ALLOW / DENY / REWRITE（ADAPT 缝），但 elicitation 本体维持 DEFER
（ADR-0012 D1），本模块不实现它。

铁律（§4.1）：`after`（工具已执行后）只能走 notification 管道，
永远不得进入 decision 管道——工具结果一旦落盘即为 append-only 事实。
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from agent_harness.tooling.contract import ToolPermission, ToolSideEffect

logger = logging.getLogger("agent_harness.tooling.decision_hooks")

#: decision hook 默认超时（秒）。保守小值（§4.5）：Hook 是可选能力，
#: 超时不得拖垮 Core；具体数值可在 `DecisionHookRunner` 上覆盖。
DEFAULT_DECISION_HOOK_TIMEOUT_SECONDS = 5.0


class DecisionVerdict(str, Enum):
    """before-tool 决策的三态（对标 Claude Code PreToolUse 语义）。"""

    ALLOW = "allow"
    DENY = "deny"
    REWRITE = "rewrite"


@dataclass(frozen=True)
class BeforeToolDecision:
    """Hook 返回的决策结论。

    结构上**没有** permission / side_effect / policy 槽位——Hook 返回值
    只能携带新的 args，**不能**抬高授权级别（§4.3 不提权）；
    权限仍由 Executor 的 `_check_approval` 依据工具静态元数据裁决。
    """

    verdict: DecisionVerdict
    reason: str = ""
    args: dict[str, Any] | None = None  # 仅 REWRITE 时有效

    @staticmethod
    def allow() -> BeforeToolDecision:
        """原样放行，继续 Validation → Permission。"""
        return BeforeToolDecision(verdict=DecisionVerdict.ALLOW)

    @staticmethod
    def deny(reason: str) -> BeforeToolDecision:
        """准入前拒绝：零执行、不占配额（§4.2，与既有准入前拒绝同族）。"""
        return BeforeToolDecision(verdict=DecisionVerdict.DENY, reason=reason)

    @staticmethod
    def rewrite(args: dict[str, Any]) -> BeforeToolDecision:
        """改写参数：调用方必须拿返回的 args 重走 Validation（§4.3）。

        入口即深拷贝：决策携带的是"一次性的确定快照"，调用方在构造后
        再改原 dict 不影响已产出的决策（隔离不只依赖 runner 的 _normalize）。
        """
        if not isinstance(args, dict):
            raise TypeError(f"REWRITE 的 args 必须是 dict，收到 {type(args).__name__}")
        return BeforeToolDecision(
            verdict=DecisionVerdict.REWRITE, args=copy.deepcopy(args)
        )


@dataclass(frozen=True)
class DecisionRequest:
    """发给 Hook 的请求：lookup 之后可读的静态元数据 + 参数深拷贝。

    `args` 是深拷贝后的副本——Hook 可读可改，任何就地修改都回流不到
    调用方的原始参数；fail-open 时继续的是【原始参数】，
    不会出现"改写未完成却部分生效"（§4.5）。
    """

    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    permission: ToolPermission
    side_effect: ToolSideEffect


#: 可插拔决策钩子：接收 DecisionRequest，返回 BeforeToolDecision。
#: 不提供时 Executor 行为与现状逐字节一致（§4.5 末 / AC-1）。
DecisionHook = Callable[[DecisionRequest], Awaitable[BeforeToolDecision]]


class DecisionFailPolicy(str, Enum):
    """Hook 超时 / 抛异常 / 返回非法时的兜底策略（§4.5）。"""

    FAIL_OPEN = "fail_open"  # 默认：视为 ALLOW，按原始参数继续
    FAIL_CLOSED = "fail_closed"  # 显式可配：视为 DENY，准入前拒绝、零执行


@dataclass(frozen=True)
class DecisionResolution:
    """一次决策的确定结论：`decide()` 绝不冒泡异常（`CancelledError` 除外）。"""

    decision: BeforeToolDecision
    degraded: bool = False  # True = 走了 fail_open/fail_closed 兜底，非 Hook 本意


class DecisionHookRunner:
    """before-tool 决策钩子的执行器：管超时、管异常、管返回值隔离。

    Executor 持有它（可为 None）；热路径用 `active` 判空短路，
    未注册时零 await、零分配、零事件（§4.6 / AC-1）。
    """

    def __init__(
        self,
        hook: DecisionHook | None = None,
        *,
        timeout_seconds: float = DEFAULT_DECISION_HOOK_TIMEOUT_SECONDS,
        on_error: DecisionFailPolicy = DecisionFailPolicy.FAIL_OPEN,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds 必须为正数，收到 {timeout_seconds}"
                "（asyncio.timeout(0) 会立即超时，把每次决策都静默降级）。"
            )
        self._hook = hook
        self._timeout_seconds = timeout_seconds
        self._on_error = on_error

    @property
    def active(self) -> bool:
        """是否注册了 Hook。调用方用 `if runner.active:` 短路（§4.6）。"""
        return self._hook is not None

    async def decide(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        raw_args: dict[str, Any],
        permission: ToolPermission,
        side_effect: ToolSideEffect,
    ) -> DecisionResolution:
        """跑一次 before-tool 决策，任何失败都收敛成确定结论。

        - 未注册 Hook：直接 ALLOW（无 Hook 语义，行为与现状逐字节一致）；
        - Hook 正常返回：校验并隔离后返回（REWRITE 的 args 再深拷贝一份）；
        - 超时 / 抛异常 / 返回非法：按 `on_error` 兜底，`degraded=True`；
        - `asyncio.CancelledError`：run 取消，原样传播，绝不吞成 ALLOW。
        """
        if self._hook is None:
            return DecisionResolution(decision=BeforeToolDecision.allow())

        try:
            # M-1 修复：请求构造（含 deepcopy）与返回值规范化都在 try 内——
            # raw_args 或 hook 返回的 args 若含不可深拷贝对象，异常同样走
            # fail 策略收敛，绝不冒出 decide()（不变量 #21）。
            # `except CancelledError` 必须在最前：run 取消绝不能被吞成 ALLOW。
            request = DecisionRequest(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=copy.deepcopy(raw_args),
                permission=permission,
                side_effect=side_effect,
            )
            async with asyncio.timeout(self._timeout_seconds):
                decision = await self._hook(request)
            normalized = self._normalize(decision)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 —— Hook 是开放世界，异常类型不可预知
            return self._degraded(error)

        if normalized is None:
            return self._degraded(ValueError("decision hook 返回了非法结论"))
        return DecisionResolution(decision=normalized)

    @staticmethod
    def _normalize(decision: object) -> BeforeToolDecision | None:
        """校验并隔离 Hook 的返回值；非法 → None（调用方走 fail 策略）。

        REWRITE 的 args 深拷贝后再对外：防止 Hook 返回后仍持有引用、
        在决策被消费期间就地改动——改写必须是"一次性的确定快照"。
        """
        if not isinstance(decision, BeforeToolDecision):
            return None
        # M-2 修复：verdict 必须是枚举成员。裸字符串（如 verdict="deny"）
        # 在 executor 侧 `is DecisionVerdict.DENY` 判定不命中，会静默变成
        # 无标记的 ALLOW——fail-closed 下这是安全语义绕过。
        if not isinstance(decision.verdict, DecisionVerdict):
            return None
        if decision.verdict is DecisionVerdict.REWRITE:
            if not isinstance(decision.args, dict):
                return None
            return BeforeToolDecision.rewrite(copy.deepcopy(decision.args))
        return decision

    def _degraded(self, error: Exception) -> DecisionResolution:
        """超时 / 异常 / 非法返回的兜底：按配置二选一，绝不部分应用改写。"""
        if self._on_error is DecisionFailPolicy.FAIL_CLOSED:
            logger.warning(
                "decision hook 失败（%s），按 fail-closed 拒绝本次调用：%s",
                type(error).__name__,
                error,
            )
            return DecisionResolution(
                decision=BeforeToolDecision.deny(
                    "before-tool 决策钩子失败"
                    f"（{type(error).__name__}），按 fail-closed 拒绝本次调用。"
                ),
                degraded=True,
            )
        logger.warning(
            "decision hook 失败（%s），按 fail-open 放行（沿用原始参数）：%s",
            type(error).__name__,
            error,
        )
        return DecisionResolution(decision=BeforeToolDecision.allow(), degraded=True)


__all__ = [
    "DEFAULT_DECISION_HOOK_TIMEOUT_SECONDS",
    "BeforeToolDecision",
    "DecisionFailPolicy",
    "DecisionHook",
    "DecisionHookRunner",
    "DecisionRequest",
    "DecisionResolution",
    "DecisionVerdict",
]
