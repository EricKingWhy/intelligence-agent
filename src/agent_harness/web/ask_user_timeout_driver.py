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
    ExpireOutcome,
    ask_user_timeout_seconds_from_payload,
    latest_pending_input_request,
)
from agent_harness.session.event import SessionEvent

logger = logging.getLogger("agent_harness.web.ask_user_timeout_driver")

#: 到点恢复的出口：`(session_id, request_id) -> ExpireOutcome`（见 `session.ask_user_timeout`）。
ExpireCallback = Callable[[str, str], Awaitable[ExpireOutcome]]

#: 冻结（无连接、不会触发）计时器的保留上限：只兜住"被彻底放弃的待回答会话"在临时接缝
#: 里的无界增长。超限按最早冻结者淘汰——被淘汰会话重连后重头计时，属可接受的降级
#: （正常重连远达不到该量级）。W-12 删本模块时一并作废。
_MAX_FROZEN_TRACKERS = 256


class AskUserTimeoutDriver:
    """连接级在场计数 + 每个会话一个 `AskUserTimeoutTracker` 的临时驱动。

    两个正交维度：
    - **在场**（`connection_subscribed` / `connection_closed`）：跨连接引用计数，
      决定该会话的计时器此刻是否累计。
    - **待回答**（`arm` / `disarm` / `note_session_state`）：该会话最新 run 上是否有
      一个未答复的 `ask_user`；没有则不装配（无连接时也无从累计 ⇒ 永不超时）。

    单事件循环内使用（与 WS handler 同一 loop），不做跨线程同步。

    #1284 P2-2：`timeout_seconds` 构造参数只是**旧事件兜底**；每个请求的计时档读
    `USER_INPUT_REQUESTED` 载荷快照（`timeout_minutes` 优先，缺失/非法回落
    `timeout_seconds`，再回落本参数——见 `note_session_state`）。
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
        #: 每个已装配请求的**生效**超时档（读请求载荷快照；旧事件才回落构造参数）。
        #: RETRY 重装时必须沿用它，否则会掉回启动配置（#1284 P2-2）。
        self._armed_timeout: dict[str, int] = {}
        #: 冻结中的会话（无连接、计时暂停）——插入序 = 冻结先后，供超限淘汰。
        self._frozen: dict[str, None] = {}

    # ── 在场计数 ──────────────────────────────────────────────────────

    def connection_subscribed(self, session_id: str) -> None:
        """一条前端连接开始观看该会话（幂等由调用方按连接去重保证）。"""
        count = self._connections.get(session_id, 0) + 1
        self._connections[session_id] = count
        if count == 1:
            self._frozen.pop(session_id, None)
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
        if tracker is None:
            return
        tracker.set_connected(False)
        # 冻结条目保留"已累计"供重连续计；仅以宽松上限兜住被放弃会话的无界增长。
        self._freeze(session_id)

    def _freeze(self, session_id: str) -> None:
        """登记一个**无连接**的计时器；超 `_MAX_FROZEN_TRACKERS` 按最早冻结者淘汰。"""
        self._frozen[session_id] = None
        while len(self._frozen) > _MAX_FROZEN_TRACKERS:
            oldest = next(iter(self._frozen))
            logger.info("淘汰被放弃的 ask_user 超时计时器（session=%s）", oldest)
            self.disarm(oldest)

    # ── 待回答装配 / 撤销 ─────────────────────────────────────────────

    def note_session_state(self, session_id: str, events: Iterable[SessionEvent]) -> None:
        """按事件流现状装配 / 撤销计时器（subscribe / 重连时调用，守卫的单一入口）。

        最新 run 上还有未答复的 `ask_user` ⇒ 装配（同一 request_id 重复调用保留已累计
        时长——断线重连续计不清零）；否则**撤销**——已答复 / 整卡跳过 / 无提问的会话
        不再触发超时恢复。

        #1284 P2-2：计时档读**请求载荷快照**（`data.timeout_seconds`），不读启动配置。
        运维改配置并重启后，旧请求仍按创建时的档位到期；只有 #1224 之前落盘、没有
        快照的旧事件才回落到 `self.timeout_seconds`。
        """
        request = latest_pending_input_request(events, kind="ask_user")
        if request is None:
            self.disarm(session_id)
            return
        self.arm(
            session_id,
            request_id=request.data["request_id"],
            timeout_seconds=ask_user_timeout_seconds_from_payload(
                request.data, fallback=int(self.timeout_seconds),
            ),
        )

    def arm(
        self, session_id: str, *, request_id: str, timeout_seconds: int | None = None,
    ) -> None:
        """为一个待回答请求装配计时器（同一 request_id 幂等，保留已累计时长）。"""
        if self._armed_request.get(session_id) == request_id and session_id in self._trackers:
            return
        effective_timeout = int(
            self.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        tracker = AskUserTimeoutTracker(effective_timeout, clock=self.clock)
        if self._connections.get(session_id, 0) > 0:
            tracker.set_connected(True)
            self._frozen.pop(session_id, None)
        else:
            # 无连接时装臂的计时器同样计入冻结账（否则它既不被扫描、也不受上限约束，
            # 成无界残留——#1220 复审 A-F2）。
            self._freeze(session_id)
        self._trackers[session_id] = tracker
        self._armed_request[session_id] = request_id
        self._armed_timeout[session_id] = effective_timeout

    def disarm(self, session_id: str) -> None:
        self._trackers.pop(session_id, None)
        self._armed_request.pop(session_id, None)
        self._armed_timeout.pop(session_id, None)
        self._frozen.pop(session_id, None)

    def is_armed(self, session_id: str) -> bool:
        return session_id in self._trackers

    # ── 到点检查 ──────────────────────────────────────────────────────

    async def check_expiries(self) -> list[str]:
        """推进一次到期检查：**此刻有连接在场**且已到点的会话经 `on_expire` 触发恢复。

        只遍历"当前至少有一个前端连接"的会话（#1220 复审 A1/A3）：到点意味着计时器在
        有连接时累计够了，但**触发**同样要求此刻有连接——无连接时不触发（与"无连接时
        不启动/继续"同一口径，提问保持挂起）；同时冻结条目既不触发也不参与每心跳扫描。

        每次尝试按 `on_expire` 的结果分流（#1220 复审 A2）：
        - `RESUMED`：已发起恢复 → 收口（disarm），计入返回列表；
        - `RESOLVED`：该请求已不再待回答 → 收口；
        - `RETRY`：可重试失败 → 重置计时基线（保留在场），下个超时窗口再试——既不永久
          丢失该提问的超时，也不每心跳空转。
        回调抛异常按 `RETRY` 处理（一次失败不得打垮心跳 / 其余会话）。

        返回**确实发起恢复**（`RESUMED`）的 session_id 列表（供断言/观测）。
        """
        fired: list[str] = []
        for session_id in list(self._connections):
            tracker = self._trackers.get(session_id)
            if tracker is None or not tracker.expired():
                continue
            request_id = self._armed_request.get(session_id)
            if request_id is None:
                self.disarm(session_id)
                continue
            try:
                outcome = await self._on_expire(session_id, request_id)
            except Exception:  # 一次恢复失败不得打垮心跳 / 其余会话
                logger.exception(
                    "ask_user 超时恢复失败（session=%s, request=%s）",
                    session_id, request_id,
                )
                outcome = ExpireOutcome.RETRY
            if outcome is ExpireOutcome.RESUMED:
                fired.append(session_id)
            # 收口/RETRY 仅在仍装配着**同一** request 时进行：`await on_expire` 期间 relay /
            # `note_session_state` 可能已为同一会话装了**新的** request，误拆会丢掉它的超时
            # （#1220 复审 A-F1，TOCTOU）。
            if self._armed_request.get(session_id) != request_id:
                continue
            if outcome is ExpireOutcome.RETRY:
                # 保留在场、重置累计：下个超时窗口再试（见本方法 docstring）。
                # #1284：重装必须沿用该请求的**快照**档位，不得掉回启动配置。
                armed_timeout = self._armed_timeout.get(session_id)
                self.disarm(session_id)
                self.arm(
                    session_id, request_id=request_id,
                    timeout_seconds=armed_timeout,
                )
            else:
                self.disarm(session_id)
        return fired
