"""#1204：`ask_user` 提问的超时闸——只在有前端连接在场时计时。

口径（#1204 票面 §5 + 用户裁决）：超时到点后 run **自动继续**，并在上下文里注入一条
系统消息"用户在 N 分钟内未回答，可能不在，请按你的判断继续"，**不替用户选择任何选项**
（不写任何 `answer`）。计时器只在**至少一个前端连接存在**时启动 / 继续；无前端连接
时不启动（抄 Claude Code）。

本模块只承载**确定性计时**这一件事，挂钟可注入（测试不实等）；另含"待回答用户输入"
的**唯一一份**事件流扫描（`pending_input_request` / `latest_pending_input_request`，服务
409 闸门与超时守卫共用）与超时恢复的出口结果 `ExpireOutcome`。谁在连接进出时调用
`set_connected`、谁在 `expired()` 后触发恢复，是上层（web 层）的接线——这一层不 import
web / session service，保持可单测（也是 web↔service 共用的低层家）。

⚠ **在线驱动为临时接缝（issue #1220，用户裁决 2A）**：#1204 起本模块只在测试里被实例化，
生产装配**没有**连接进出 / 超时恢复的调用方。本票以 `web/ask_user_timeout_driver.py` 的
`AskUserTimeoutDriver` 临时接线：在场信号取自 **WS 连接级订阅计数**（一个前端连接
`subscribe(session_id)` = 在场）。**TODO（W-12 落地后迁移）**：产品客户端在场协议
`agent/client_presence.py::ClientPresenceGate.enroll()`（W-22/#366）落地后，把在场信号
迁到那道闸门（`enroll()` / `mark_absent()`），并**整体删除** `AskUserTimeoutDriver`
及其对 WS 订阅计数的直接依赖——本接缝是临时的，不是长期形态。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from enum import Enum

from agent_harness.agent.run_budget import latest_run_id
from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    SessionEvent,
)

#: 逻辑 run 的终态事件——一旦出现，最新 run 上就不该再有"待回答"。
_TERMINAL_RUN_EVENTS = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED})

#: 超时注入消息的正文模板（#1204 用户裁决逐字）。`minutes` = 配置秒数 / 60。
ASK_USER_TIMEOUT_MESSAGE_TEMPLATE = (
    "用户在 {minutes} 分钟内未回答，可能不在，请按你的判断继续"
)


def ask_user_timeout_message(timeout_seconds: int) -> str:
    """把配置的超时秒数（60 / 300 / 600）渲染成注入消息正文。"""
    minutes = max(1, int(timeout_seconds) // 60)
    return ASK_USER_TIMEOUT_MESSAGE_TEMPLATE.format(minutes=minutes)


class ExpireOutcome(Enum):
    """一次超时到点恢复尝试的结果（`on_expire` 出口契约，与挂钟无关）。"""

    #: 已发起恢复（run 已续跑）。
    RESUMED = "resumed"
    #: 该请求已不再待回答（已答复 / 整卡跳过 / 已终结 / 请求已变）——收口，无需再试。
    RESOLVED = "resolved"
    #: 可重试失败（共享预算/并发冲突等）——保留计时并下个窗口重试（见 driver）。
    RETRY = "retry"


def pending_input_request(
    events: Iterable[SessionEvent], *, run_id: str, kind: str | None = None,
) -> SessionEvent | None:
    """某逻辑 run 上**最新一条未被答复**的 `USER_INPUT_REQUESTED` 事件。

    `kind` 非空时只认该 kind（`ask_user` 超时闸用），为空则认任意 kind（服务端 409
    闸门用）。"未答复"= 该请求 id 不在任一带 `input_request_id` 的 `USER_MESSAGE` 答复
    集里（含整卡跳过的 `answer=null` 与超时 `{timeout:true}` 形态）。

    这是**唯一一份**扫描（#1220 复审 B1）：服务 409 闸门与驱动/超时复查共用同一份判据，
    不各写一遍——两份拷贝迟早会漂。
    """
    events = list(events)
    answered = {
        event.data.get("input_request_id")
        for event in events
        if event.type == USER_MESSAGE
        and isinstance(event.data.get("input_request_id"), str)
    }
    return next(
        (
            event for event in reversed(events)
            if event.type == USER_INPUT_REQUESTED
            and event.run_id == run_id
            and (kind is None or event.data.get("kind") == kind)
            and isinstance(event.data.get("request_id"), str)
            and event.data["request_id"] not in answered
        ),
        None,
    )


def latest_pending_input_request(
    events: Iterable[SessionEvent], *, kind: str | None = None,
) -> SessionEvent | None:
    """最新逻辑 run 上未答复的 `USER_INPUT_REQUESTED` 事件；无 run / 已终态则 `None`。"""
    events = list(events)
    run_id = latest_run_id(events)
    if run_id is None:
        return None
    if any(
        event.run_id == run_id and event.type in _TERMINAL_RUN_EVENTS
        for event in events
    ):
        return None
    return pending_input_request(events, run_id=run_id, kind=kind)


def pending_ask_user_request_id(events: Iterable[SessionEvent]) -> str | None:
    """最新逻辑 run 上**尚未被答复**的 `ask_user` 请求 id，否则 `None`。

    这是超时恢复的守卫判据（#1220 AC3）：只有"最新 run 仍暂停在一个未答复、未整卡
    跳过的 `ask_user` 上"才允许到点恢复——已答复 / 已跳过 / 已终结的提问不再触发。
    委托 :func:`latest_pending_input_request`（与服务 409 闸门同一份扫描）。
    """
    request = latest_pending_input_request(events, kind="ask_user")
    return request.data["request_id"] if request is not None else None


class AskUserTimeoutTracker:
    """一个 `ask_user` 暂停的倒计时：累计"有前端连接在场"的时长。

    语义：`set_connected(True)` 开始累计，`set_connected(False)` 冻结（已累计的不清零
    ——重连后从断点继续，不重头计）；`expired()` 在累计时长 ≥ 阈值时为真。无前端连接
    时**从不**累计，故永不 `expired()`。挂钟经 `clock` 注入，测试可直接推进。
    """

    __slots__ = ("_accumulated", "_clock", "_connected_since", "_timeout")

    def __init__(
        self, timeout_seconds: float, *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout = float(timeout_seconds)
        self._clock = clock
        self._accumulated = 0.0
        self._connected_since: float | None = None

    def set_connected(self, connected: bool) -> None:
        """前端连接存在性变化（幂等）。True 起表 / 继续；False 冻结。"""
        now = self._clock()
        if connected and self._connected_since is None:
            self._connected_since = now
        elif not connected and self._connected_since is not None:
            self._accumulated += now - self._connected_since
            self._connected_since = None

    def elapsed_seconds(self) -> float:
        """已累计的"连接在场"时长（秒）。"""
        total = self._accumulated
        if self._connected_since is not None:
            total += self._clock() - self._connected_since
        return max(0.0, total)

    def expired(self) -> bool:
        return self.elapsed_seconds() >= self._timeout

    @property
    def running(self) -> bool:
        """是否正在计时（有前端连接在场）。"""
        return self._connected_since is not None
