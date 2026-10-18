"""#1320：`ask_user` 超时恢复的**服务端 durable** 驱动。

口径（#1320 / D1A，**推翻** #1220 复审 A1/A3）：是否上膛、是否到点，只由**服务端持久
状态**决定——事件流里存在未答复的 `ask_user` 请求 + 该请求载荷里的超时档。前端 WS
连接数**不**参与计时；连接只影响"到点恢复的结果推给谁看"（恢复照常走既有 fanout，
零订阅者时只落 durable 事件，下一次 HTTP 轮询/重连自然读到）。

为什么推翻旧口径（一手证据，见 #1320 票面）：真实 Web 前端在 `run/paused` 上**不建**
WS 订阅（`web/src/lib/runState.ts` 把 `run/paused` 计为收口 → `web/src/hooks/useSession.ts`
只走 HTTP 轮询）⇒ 旧口径把"用户正盯着页面看"判成"无人在场"，计时器永不上膛，
`ask_user` 功能死亡。

成熟产品依据：Temporal `Workflow.await(timeout, condition)` 的超时是服务端 durable
timer——到点由服务端推进状态机，不依赖任何客户端此刻是否连着。本模块照此形状：
装配与推进都发生在服务端，连接数不进入判定。

两条装配入口都**不依赖连接**：
- `note_event()`：run 期间 `user/input-requested` 落盘即装（组合根把它接到
  `RunManager` 的会话事件观察者上）；
- `note_session_state()`：进程启动时按持久事件流重建（重启不丢计时，幂等）。

到点推进由组合根的常驻 ticker 调 `check_expiries()`（不再借 WS 心跳：零连接时也必须
推进）。`on_expire` 是唯一恢复出口（组合根注入 session service 的调用，本模块不 import
session service，避免 web↔service 环）。

⚠ **迁移计划不变——W-12 登记协议（#356 中 DEFER 的部分）落地后删除本模块**：
    TODO(#1220)：产品客户端在场协议
    `agent/client_presence.py::ClientPresenceGate.enroll()`（W-22/#366）落地后，
    把 `ask_user` 的超时推进迁到该登记协议，并把本模块**整体删除**。
    #1320 已把"连接数 ⇒ 是否计时"这层解耦（见上），本模块不再读任何连接计数；
    剩余的迁移只是把 ticker + 事件观察者换成 presence 闸门的正式入口。

    触发条件为什么是"登记协议"而不是"W-12 落地"：#356 已于 2026-10-06 关单
    （选项 B：只做明确退出信号，**整套 presence 登记表/宽限 DEFER**），#366 也于
    2026-10-04 关单——但 `enroll()` 至今**没有生产调用方**。照旧措辞读，grep 到
    `#356` CLOSED 的人会得出"现在就该删"或"永远不会删"两种相反结论。

    **删除清单（按文件，`git grep -n "#1220" -- src` 可复核）**：
      - `web/app.py`：import、组合根装配、`AppState.restore_ask_user_timeouts`、
        ticker 启停；
      - `session/service.py`：import 行 + `resume_ask_user_timeout` 整个方法；
      - `session/ask_user_timeout.py`：模块 docstring 的迁移段；
      - 本模块（整个文件）；
      - `tests/web/test_ask_user_timeout_driver.py`、`tests/web/test_ask_user_timeout_online.py`。
    （`web/websocket.py` 自 #1320 起不参与装配：不绑定 driver、不做订阅补读。）
    `pending_ask_user_request_id`（`session/ask_user_timeout.py`）的**生产调用点全部
    属于本接缝**（本模块与 `service.resume_ask_user_timeout`）⇒ 删完接缝它只剩测试
    消费者。届时二选一并记录：新闸门复用同一份扫描，或连同其测试一并删除——不得
    让它悄悄退回"只在测试里被实例化"的 #1220 前状态。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable

from agent_harness.agent.types import AgentEvent
from agent_harness.session.ask_user_timeout import (
    TERMINAL_RUN_EVENTS,
    AskUserTimeoutTracker,
    ExpireOutcome,
    ask_user_timeout_seconds_from_payload,
    elapsed_seconds_since,
    latest_pending_input_request,
)
from agent_harness.session.event import (
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    SessionEvent,
)

logger = logging.getLogger("agent_harness.web.ask_user_timeout_driver")

#: 到点恢复的出口：`(session_id, request_id) -> ExpireOutcome`（见 `session.ask_user_timeout`）。
ExpireCallback = Callable[[str, str], Awaitable[ExpireOutcome]]


class AskUserTimeoutDriver:
    """每个会话一个 `AskUserTimeoutTracker` 的**服务端 durable** 驱动。

    计时与前端连接无关：`arm()` 装表即起表，`check_expiries()` 遍历**全部**已装表
    会话。连接数不参与"是否计时 / 是否触发"（#1320 D1A）。

    装配入口：`note_event()`（事件落盘即装，生产主路径）与 `note_session_state()`
    （启动时按事件流重建）。两者都幂等：同一 request_id 重复装配保留已累计时长。

    单事件循环内使用（与 run fanout / ticker 同一 loop），不做跨线程同步。

    #1284 P2-2：`timeout_seconds` 构造参数只是**旧事件兜底**；每个请求的计时档读
    `USER_INPUT_REQUESTED` 载荷快照（`timeout_minutes` 优先，缺失/非法回落
    `timeout_seconds`，再回落本参数）。
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
        self._trackers: dict[str, AskUserTimeoutTracker] = {}
        self._armed_request: dict[str, str] = {}
        #: 每个已装配请求的**生效**超时档（读请求载荷快照；旧事件才回落构造参数）。
        #: RETRY 重装时必须沿用它，否则会掉回启动配置（#1284 P2-2）。
        self._armed_timeout: dict[str, int] = {}

    # ── 装配 / 撤销 ──────────────────────────────────────────────────

    def note_event(self, session_id: str, event: AgentEvent) -> None:
        """服务端事件观察者：只按事件事实装配/撤销，不看连接数（#1320）。

        组合根把它接到 `RunManager` 的会话事件扇出上 ⇒ run 进行中落盘的
        `user/input-requested` 必然经过这里，无论有没有前端 WS 订阅（真实 Web
        前端在 `run/paused` 上只走 HTTP 轮询、从不订阅）。

        - `user/input-requested`（`kind=ask_user`）⇒ 装配；
        - 带 `input_request_id` 的 `user/message` ⇒ 该请求已答复，撤销（仅当答复
          经**本 run 的 Session** 追加时可见；HTTP 答复走另一份 `Session.load`，
          那里的撤销由到期时 `resume_ask_user_timeout` 重读事件流兜底）；
        - run 终态 ⇒ 撤销（提问不可能再被回答）。`run/paused` **不**撤销：
          `ask_user` 提问本身就是以暂停收口的。
        """
        data = getattr(event, "data", None)
        if event.type == USER_INPUT_REQUESTED:
            if (
                isinstance(data, dict)
                and data.get("kind") == "ask_user"
                and isinstance(data.get("request_id"), str)
            ):
                self.arm(
                    session_id,
                    request_id=data["request_id"],
                    timeout_seconds=ask_user_timeout_seconds_from_payload(
                        data, fallback=int(self.timeout_seconds),
                    ),
                )
            return
        if event.type == USER_MESSAGE:
            request_id = (
                data.get("input_request_id") if isinstance(data, dict) else None
            )
            if (
                isinstance(request_id, str)
                and self._armed_request.get(session_id) == request_id
            ):
                self.disarm(session_id)
            return
        if event.type in TERMINAL_RUN_EVENTS:
            self.disarm(session_id)

    def note_session_state(self, session_id: str, events: Iterable[SessionEvent]) -> None:
        """按事件流**快照**装配计时器（启动重建用，幂等）。

        最新 run 上还有未答复的 `ask_user` ⇒ 装配（同一 request_id 重复调用保留
        已累计时长）。快照里**没有**待回答请求时**不动**已有计时器：快照可能比
        进程内事件观察者更旧（读盘与落盘并发），"快照没看到"不是"请求已了结"
        的证据。撤销只由 `note_event`（权威事件落盘）驱动；漏掉的撤销由到期时
        `resume_ask_user_timeout` 重读事件流判 `RESOLVED` 兜底。

        #1320：重启重建时按请求事件的 `time` 还原已累计时长（`elapsed_seconds_since`）
        ——durable timer 的截止时刻在持久事件里，不因进程重启而顺延一个完整档位。

        #1284 P2-2：计时档读**请求载荷快照**（`timeout_minutes` 优先，缺失/非法回落
        `timeout_seconds`，见 `ask_user_timeout_seconds_from_payload`），不读启动配置。
        运维改配置并重启后，旧请求仍按创建时的档位到期；只有 #1224 之前落盘、没有
        快照的旧事件才回落到 `self.timeout_seconds`。
        """
        request = latest_pending_input_request(events, kind="ask_user")
        if request is None:
            return
        self.arm(
            session_id,
            request_id=request.data["request_id"],
            timeout_seconds=ask_user_timeout_seconds_from_payload(
                request.data, fallback=int(self.timeout_seconds),
            ),
            elapsed_seconds=elapsed_seconds_since(request.time),
        )

    def arm(
        self,
        session_id: str,
        *,
        request_id: str,
        timeout_seconds: int | None = None,
        elapsed_seconds: float = 0.0,
    ) -> None:
        """为一个待回答请求装配计时器（同一 request_id 幂等，保留已累计时长）。

        #1320：装表即起表——服务端 durable timer 不因"此刻有没有 WS 连接"冻结。
        `elapsed_seconds` 供重启重建接上请求落盘后的已累计时长。
        """
        if self._armed_request.get(session_id) == request_id and session_id in self._trackers:
            return
        effective_timeout = int(
            self.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        tracker = AskUserTimeoutTracker(
            effective_timeout, clock=self.clock, elapsed_seconds=elapsed_seconds,
        )
        self._trackers[session_id] = tracker
        self._armed_request[session_id] = request_id
        self._armed_timeout[session_id] = effective_timeout

    def disarm(self, session_id: str) -> None:
        self._trackers.pop(session_id, None)
        self._armed_request.pop(session_id, None)
        self._armed_timeout.pop(session_id, None)

    def is_armed(self, session_id: str) -> bool:
        """该会话是否装着计时器。"""
        return session_id in self._trackers

    # ── 到点检查 ──────────────────────────────────────────────────────

    async def check_expiries(self) -> list[str]:
        """推进一次到期检查：**所有**已到点的已装表会话经 `on_expire` 触发恢复。

        #1320 D1A **推翻** #1220 复审 A1/A3：旧口径要求"此刻至少有一个前端连接"
        才触发，并把零连接会话冻结在不扫描的账里。真实 Web 前端在 `run/paused`
        上从不建 WS 订阅（见模块 docstring），该口径使超时闸永久不触发。现行口径：
        遍历**全部**已装表会话，连接数不参与判定——与 Temporal 的服务端 durable
        timer 一致。`on_expire` 内部的权威守卫（`resume_ask_user_timeout` 重读事件流）
        保证已答复 / 已终结 / 请求已变的会话只会拿到 `RESOLVED`。

        每次尝试按 `on_expire` 的结果分流（#1220 复审 A2）：
        - `RESUMED`：已发起恢复 → 收口（disarm），计入返回列表；
        - `RESOLVED`：该请求已不再待回答 → 收口；
        - `RETRY`：可重试失败 → 重置计时基线，下个超时窗口再试——既不永久
          丢失该提问的超时，也不每 tick 空转。
        回调抛异常按 `RETRY` 处理（一次失败不得打垮 ticker / 其余会话）。

        返回**确实发起恢复**（`RESUMED`）的 session_id 列表（供断言/观测）。
        """
        fired: list[str] = []
        for session_id in list(self._trackers):
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
            # 收口/RETRY 仅在仍装配着**同一** request 时进行：`await on_expire` 期间
            # 事件观察者 / 启动重建可能已为同一会话装了**新的** request，误拆会丢掉它的超时
            # （#1220 复审 A-F1，TOCTOU）。
            if self._armed_request.get(session_id) != request_id:
                continue
            if outcome is ExpireOutcome.RETRY:
                # 保留计时、重置累计：下个超时窗口再试（见本方法 docstring）。
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
