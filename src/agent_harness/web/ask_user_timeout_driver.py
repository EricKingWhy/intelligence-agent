"""#1220 临时接缝：WS 连接级订阅计数 → `AskUserTimeoutTracker` → `ask_user` 超时恢复。

用户裁决 2A（现在修、临时接缝、不等 W-12）：`#1204` 已交付**确定性**计时语义
（`session/ask_user_timeout.py`），但没有在线驱动。本模块提供今日产品装配里唯一真实
存在的"前端连接进出"事实——**WS 连接级订阅计数**——把它接到计时器上，并在到点时经
既有恢复链（`resume_basis='user_input'` + `{request_id, timeout:true}`）触发超时恢复。

在场信号来源（一手）：`web/websocket.py::handle_websocket` 的 `subscribe` 分支
（一个前端连接 `subscribe(session_id)` = "这个前端在看着这个会话"）+ 连接的
`finally` 收尾（离开）。按会话做**跨连接引用计数**：0→≥1 `set_connected(True)`，
归零 `set_connected(False)`；断线重连不清零（同名请求重复装配保留已累计时长）。

⚠ **临时接缝——W-12 登记协议（#356 中 DEFER 的部分）落地后删除本模块**：
    TODO(#1220)：产品客户端在场协议
    `agent/client_presence.py::ClientPresenceGate.enroll()`（W-22/#366）落地后，
    把在场信号迁到那道闸门（`enroll()` 登记 / `mark_absent()` 缺席），并把本模块
    及其对 WS 订阅计数的直接依赖**整体删除**。本接缝只为补上"谁在场"这一格，
    不是长期形态（W-12 的登记 / 心跳 / 明确退出 vs 意外断线区分不在本模块承担）。

    触发条件为什么是"登记协议"而不是"W-12 落地"：#356 已于 2026-10-06 关单
    （选项 B：只做明确退出信号，**整套 presence 登记表/宽限 DEFER**），#366 也于
    2026-10-04 关单——但 `enroll()` 至今**没有生产调用方**。照旧措辞读，grep 到
    `#356` CLOSED 的人会得出"现在就该删"或"永远不会删"两种相反结论。

    **删除清单（按文件，`git grep -n "#1220" -- src` 可复核）**：
      - `web/websocket.py` 7 处：`driver = state.ask_user_timeout_driver` 绑定
        （~144-145）、`_watch_session`/`watching` 在场计数（~142-153）、
        `_push_snapshot` 的装配与订阅后补读（~209-228、补读块 ~264-279）、
        relay 的 `arm`（~314-325）、
        launch 处的 `_watch_session`（~507）、心跳里的 `check_expiries`（~579-582）、
        `finally` 的 `connection_closed` 归零（~622-626）；
      - `web/app.py` 2 处：import（~171）+ 组合根装配（~1265-1278）；
      - `session/service.py`：import 行（~127）+ `resume_ask_user_timeout`
        整个方法（~3050-3100）；
      - `session/ask_user_timeout.py`：模块 docstring 的接缝段（~14-25）；
      - 本模块（整个文件）；
      - `tests/web/test_ask_user_timeout_driver.py`、`tests/web/test_ask_user_timeout_online.py`。
    `pending_ask_user_request_id`（`session/ask_user_timeout.py`）的**生产调用点全部
    属于本接缝**（本模块与 `service.resume_ask_user_timeout`）⇒ 删完接缝它只剩测试
    消费者。届时二选一并记录：新闸门复用同一份扫描，或连同其测试一并删除——不得
    让它悄悄退回"只在测试里被实例化"的 #1220 前状态。

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
    pending_ask_user_request_id,
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
            self._frozen.pop(session_id, None)
        else:
            # 无连接时装臂的计时器同样计入冻结账（否则它既不被扫描、也不受上限约束，
            # 成无界残留——#1220 复审 A-F2）。
            self._freeze(session_id)
        self._trackers[session_id] = tracker
        self._armed_request[session_id] = request_id

    def disarm(self, session_id: str) -> None:
        self._trackers.pop(session_id, None)
        self._armed_request.pop(session_id, None)
        self._frozen.pop(session_id, None)

    def is_armed(self, session_id: str) -> bool:
        """该会话是否装着计时器（含无连接时冻结的条目——冻结≠撤销）。"""
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
                # 产品行为改变（用户未回答、run 自动继续）必须在日志里留下第一条
                # 线索——否则排障时"为什么这个 run 自己跑了"只能靠事件流事后回溯。
                logger.info(
                    "ask_user 超时到点，已发起恢复（session=%s, request=%s, 档位=%.0fs）",
                    session_id, request_id, self.timeout_seconds,
                )
                fired.append(session_id)
            # 收口/RETRY 仅在仍装配着**同一** request 时进行：`await on_expire` 期间 relay /
            # `note_session_state` 可能已为同一会话装了**新的** request，误拆会丢掉它的超时
            # （#1220 复审 A-F1，TOCTOU）。
            if self._armed_request.get(session_id) != request_id:
                continue
            if outcome is ExpireOutcome.RETRY:
                # 保留在场、重置累计：下个超时窗口再试（见本方法 docstring）。
                self.disarm(session_id)
                self.arm(session_id, request_id=request_id)
            else:
                self.disarm(session_id)
        return fired
