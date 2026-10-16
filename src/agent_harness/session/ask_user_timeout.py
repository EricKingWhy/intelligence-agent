"""#1204：`ask_user` 提问的超时闸——只在有前端连接在场时计时。

口径（#1204 票面 §5 + 用户裁决）：超时到点后 run **自动继续**，并在上下文里注入一条
系统消息"用户在 N 分钟内未回答，可能不在，请按你的判断继续"，**不替用户选择任何选项**
（不写任何 `answer`）。计时器只在**至少一个前端连接存在**时启动 / 继续；无前端连接
时不启动（抄 Claude Code）。

本模块只承载**确定性计时**这一件事，挂钟可注入（测试不实等）。谁在连接进出时调用
`set_connected`、谁在 `expired()` 后触发恢复，是上层（web 层）的接线——这一层不 import
web / session service，保持可单测。
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: 超时注入消息的正文模板（#1204 用户裁决逐字）。`minutes` = 配置秒数 / 60。
ASK_USER_TIMEOUT_MESSAGE_TEMPLATE = (
    "用户在 {minutes} 分钟内未回答，可能不在，请按你的判断继续"
)


def ask_user_timeout_message(timeout_seconds: int) -> str:
    """把配置的超时秒数（60 / 300 / 600）渲染成注入消息正文。"""
    minutes = max(1, int(timeout_seconds) // 60)
    return ASK_USER_TIMEOUT_MESSAGE_TEMPLATE.format(minutes=minutes)


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
