"""#1204：`ask_user` 提问的超时闸——只在有前端连接在场时计时。

口径（#1204 票面 §5 + 用户裁决）：超时到点后 run **自动继续**，并在上下文里注入一条
系统消息"用户在 N 分钟内未回答，可能不在，请按你的判断继续"，**不替用户选择任何选项**
（不写任何 `answer`）。计时器只在**至少一个前端连接存在**时启动 / 继续；无前端连接
时不启动（抄 Claude Code）。

本模块只承载**确定性计时**这一件事，挂钟可注入（测试不实等）；另含一个纯函数
`pending_ask_user_request_id`（按事件流判"最新逻辑 run 上还有没有未答复的 ask_user"）。
谁在连接进出时调用 `set_connected`、谁在 `expired()` 后触发恢复，是上层（web 层）的接线
——这一层不 import web / session service，保持可单测。

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


def pending_ask_user_request_id(events: Iterable[SessionEvent]) -> str | None:
    """最新逻辑 run 上**尚未被答复**的 `ask_user` 请求 id，否则 `None`。

    这是超时恢复的守卫判据（#1220 AC3）：只有"最新 run 仍暂停在一个未答复、未整卡
    跳过的 `ask_user` 上"才允许到点恢复。以下任一即返回 `None`：没有 run、最新 run
    已终态、该 run 的最新 `USER_INPUT_REQUESTED(kind=ask_user)` 已被一条带
    `input_request_id` 的 `USER_MESSAGE` 答复（含整卡跳过的 `answer=null` 与超时
    `{timeout:true}` 形态）——**已答复/已跳过的提问不再触发超时恢复**。

    纯函数、无副作用：驱动（装配/复查）与 session service（发起恢复前）共用同一份
    判据，不各写一遍（写了两次就会漂）。
    """
    events = list(events)
    run_id = latest_run_id(events)
    if run_id is None:
        return None
    if any(
        event.run_id == run_id and event.type in _TERMINAL_RUN_EVENTS
        for event in events
    ):
        return None
    answered = {
        event.data.get("input_request_id")
        for event in events
        if event.type == USER_MESSAGE
        and isinstance(event.data.get("input_request_id"), str)
    }
    for event in reversed(events):
        if (
            event.type == USER_INPUT_REQUESTED
            and event.run_id == run_id
            and event.data.get("kind") == "ask_user"
            and isinstance(event.data.get("request_id"), str)
        ):
            request_id = event.data["request_id"]
            return None if request_id in answered else request_id
    return None


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
