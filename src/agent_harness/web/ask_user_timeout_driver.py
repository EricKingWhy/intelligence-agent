"""#1220 临时接缝：WS 连接级订阅计数 → `AskUserTimeoutTracker` → `ask_user` 超时恢复。

用户裁决 2A（现在修、临时接缝、不等 W-12）：`#1204` 已交付**确定性**计时语义
（`session/ask_user_timeout.py`），但没有在线驱动。本模块提供今日产品装配里唯一真实
存在的"前端连接进出"事实——**WS 连接级订阅计数**——把它接到计时器上，并在到点时经
既有恢复链（`resume_basis='user_input'` + `{request_id, timeout:true}`）触发超时恢复。

在场信号来源（一手）：`web/websocket.py::handle_websocket` 的 `subscribe` 分支
（一个前端连接 `subscribe(session_id)` = "这个前端在看着这个会话"）+ 连接的
`finally` 收尾（离开）。按会话做**跨连接引用计数**：0→≥1 `set_connected(True)`，
归零 `set_connected(False)`；断线重连不清零（同名请求重复装配保留已累计时长）。

⚠ **临时接缝——W-12 落地后删除本模块**：
    TODO(#1220 → W-12)：产品客户端在场协议
    `agent/client_presence.py::ClientPresenceGate.enroll()`（W-22/#366）落地后，
    把在场信号迁到那道闸门（`enroll()` 登记 / `mark_absent()` 缺席），并把本模块
    及其对 WS 订阅计数的直接依赖**整体删除**。本接缝只为补上"谁在场"这一格，
    不是长期形态（W-12 的登记 / 心跳 / 明确退出 vs 意外断线区分不在本模块承担）。

确定性：挂钟与超时档都可注入（单测不实等）；`on_expire` 是唯一的恢复出口（由组合根
注入 session service 的调用，本模块不 import session service，避免 web↔service 环）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable

from agent_harness.session.ask_user_timeout import (
    AskUserTimeoutTracker,
    pending_ask_user_request_id,
)
from agent_harness.session.event import SessionEvent

logger = logging.getLogger("agent_harness.web.ask_user_timeout_driver")

#: 到点恢复的出口：`(session_id, request_id) -> 是否真的发起了恢复`。
ExpireCallback = Callable[[str, str], Awaitable[bool]]


class AskUserTimeoutDriver:
    """连接级在场计数 + 每个会话一个 `AskUserTimeoutTracker` 的临时驱动。

    两个正交维度：
    - **在场**（`connection_subscribed` / `connection_closed`）：跨连接引用计数，
      决定该会话的计时器此刻是否累计。
    - **待回答**（`arm` / `disarm` / `note_session_state`）：该会话最新 run 上是否有
      一个未答复的 `ask_user`；没有则不装配（无连接时也无从累计 ⇒ 永不超时）。

    单事件循环内使用（与 WS handler 同一 loop），不做跨线程同步。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float,
        on_expire: ExpireCallback,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock
        self._on_expire = on_expire
        self._connections: dict[str, int] = {}
        self._trackers: dict[str, AskUserTimeoutTracker] = {}
        self._armed_request: dict[str, str] = {}

    # ── 在场计数 ──────────────────────────────────────────────────────

    def connection_subscribed(self, session_id: str) -> None:
        """一条前端连接开始观看该会话（幂等由调用方按连接去重保证）。"""
        count = self._connections.get(session_id, 0) + 1
        self._connections[session_id] = count
        if count == 1:
            tracker = self._trackers.get(session_id)
            if tracker is not None:
                tracker.set_connected(True)

    def connection_closed(self, session_id: str) -> None:
        """一条前端连接停止观看该会话；归零即冻结计时（已累计的不清零）。"""
        count = self._connections.get(session_id, 0) - 1
        if count > 0:
            self._connections[session_id] = count
            return
        self._connections.pop(session_id, None)
        tracker = self._trackers.get(session_id)
        if tracker is not None:
            tracker.set_connected(False)

    # ── 待回答装配 / 撤销 ─────────────────────────────────────────────

    def note_session_state(self, session_id: str, events: Iterable[SessionEvent]) -> None:
        """按事件流现状装配 / 撤销计时器（subscribe / 重连时调用，守卫的单一入口）。

        最新 run 上还有未答复的 `ask_user` ⇒ 装配（同一 request_id 重复调用保留已累计
        时长——断线重连续计不清零）；否则**撤销**——已答复 / 整卡跳过 / 无提问的会话
        不再触发超时恢复。
        """
        request_id = pending_ask_user_request_id(events)
        if request_id is None:
            self.disarm(session_id)
            return
        self.arm(session_id, request_id=request_id)

    def arm(self, session_id: str, *, request_id: str) -> None:
        """为一个待回答请求装配计时器（同一 request_id 幂等，保留已累计时长）。"""
        if self._armed_request.get(session_id) == request_id and session_id in self._trackers:
            return
        tracker = AskUserTimeoutTracker(self.timeout_seconds, clock=self.clock)
        if self._connections.get(session_id, 0) > 0:
            tracker.set_connected(True)
        self._trackers[session_id] = tracker
        self._armed_request[session_id] = request_id

    def disarm(self, session_id: str) -> None:
        self._trackers.pop(session_id, None)
        self._armed_request.pop(session_id, None)

    def is_armed(self, session_id: str) -> bool:
        return session_id in self._trackers

    # ── 到点检查 ──────────────────────────────────────────────────────

    async def check_expiries(self) -> list[str]:
        """推进一次到期检查：到点且仍装配着的会话经 `on_expire` 触发一次恢复。

        触发前**先收口**（disarm）——同一请求至多触发一次，且"是否真的恢复"由
        `on_expire` 按同一份 `pending_ask_user_request_id` 判据复查（已答复 / 已跳过
        的不恢复）。返回**确实发起恢复**的 session_id 列表（供断言/观测）。
        """
        fired: list[str] = []
        for session_id, tracker in list(self._trackers.items()):
            if not tracker.expired():
                continue
            request_id = self._armed_request.get(session_id)
            self.disarm(session_id)
            if request_id is None:
                continue
            try:
                resumed = await self._on_expire(session_id, request_id)
            except Exception:  # 一次恢复失败不得打垮心跳 / 其余会话
                logger.exception(
                    "ask_user 超时恢复失败（session=%s, request=%s）",
                    session_id, request_id,
                )
                continue
            if resumed:
                fired.append(session_id)
        return fired
