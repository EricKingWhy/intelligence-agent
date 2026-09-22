"""WebSocket 多路复用通道（T2 / #132，PRD §5.1）。

单连接多路复用所有 session 的 streaming / 推送：
- 客户端 ``subscribe(session_id)`` 后，服务端推该 session 的增量事件。
- 断线重连：客户端开新 WS + ``subscribe(session_id)``，服务端先推一份
  完整快照（session 当前事件投影 + 在途 run baseline），之后增量。
- 心跳：服务端每 ``WS_PING_INTERVAL`` 秒下行 ``{"type": "server_ping"}``，
  客户端须回 ``{"type": "pong"}``；``WS_PING_TIMEOUT`` 秒内未收到任何
  上行消息则判定对端已死并关闭连接（详见 ``_heartbeat_loop``）。

设计原则（不变量守护）：
- WS 不维护第二套 session 真相（#22）：所有事件来自 RunManager 订阅，
  它的源头是 append-only Session listener。
- WS 只是一个传输通道，不做业务决策——业务走 SessionService。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import anyio
from fastapi import WebSocket, WebSocketDisconnect

from agent_harness.web.serialization import build_event_payload

if TYPE_CHECKING:
    from agent_harness.session.runmanager import RunManager
    from agent_harness.web.app import AppState

logger = logging.getLogger("agent_harness.web.websocket")

#: 服务端心跳间隔（秒）——每 N 秒下行一个应用层 ``server_ping``。
WS_PING_INTERVAL: float = 2.0
#: 客户端静默超时（秒）——超过该时长未收到任何上行消息则关闭连接。
WS_PING_TIMEOUT: float = 30.0


def _render_snapshot(
    session_id: str, window: list[Any], replay_upto: int, has_active_run: bool,
) -> str:
    """把快照渲染成 WS 文本帧（**纯函数**，供下放线程用；#275 站点 1）。

    为什么整段（`to_dict()` × N + `json.dumps`）一起搬：两者之间**没有** `await`——
    列表推导在 dict 字面量求值时跑完，`json.dumps` 的实参求值又发生在
    `await websocket.send_text(...)` 之前，所以它们是**一整块**循环占用，分开搬没有意义。

    帧结构**逐字不变**（判定级的键序与 `default=str` 都是改造前的原样）：
    键序变化会改变线上字节，虽然语义等价，但那属于契约变化（票面 AC6）。
    """
    return json.dumps({
        "type": "snapshot",
        "session_id": session_id,
        "events": [e.to_dict() for e in window],
        "replay_upto": replay_upto,
        "has_active_run": has_active_run,
    }, default=str)


async def _send_json_offloaded(
    websocket: WebSocket, render: Callable[..., str], *args: Any,
) -> None:
    """在**线程里**渲染文本、回到循环再发送（#275 站点 1，方案 A）。

    `try` 的覆盖面与 `_send_json` 逐字相同（渲染 + 发送都在里面），所以
    「渲染失败」与「连接已断」仍是同一条静默忽略路径，不新增异常层级。

    ⚠ **`send_text` 不得搬线程**：并发写同一 socket 会破坏 WebSocket 的发送语义
    （票面 Risks）。这里下放的只是**产出字符串**那一段，发送仍在循环上。

    参数按位置转发给 `render`（`anyio.to_thread.run_sync` 的 `*args` 语义），
    所以 `render` 必须是**纯函数**——它会在另一个线程里被调用。
    """
    try:
        text = await anyio.to_thread.run_sync(render, *args)
        await websocket.send_text(text)
    except Exception:
        logger.debug("WS send 失败（客户端可能已断开）", exc_info=True)


async def handle_websocket(websocket: WebSocket, state: AppState) -> None:
    """WebSocket 主入口：接受连接 → 多路复用 session 事件流。

    上行消息格式（JSON）：
      {"type": "subscribe", "session_id": "...", "after_seq": N}
           after_seq 可选（#208）：本地游标，只补 (after_seq, replay_upto]；
           缺失/非法 = -1（从头发）。超阈值改发一帧 stream/truncated。
      {"type": "send_message", "session_id": "...", "content": "...", "mode": "queue"}
      {"type": "steer", "session_id": "...", "content": "..."}
      {"type": "cancel", "session_id": "..."}
      {"type": "ping"}  — client-initiated ping, server replies pong
      {"type": "pong"}  — reply to server_ping（心跳应答；任意上行都算活性）

    下行消息格式（JSON）：
      {"type": "snapshot", "session_id": "...", "events": [...]}
      {"type": "event", "session_id": "...", "event": {...}}
      {"type": "done", "session_id": "..."}
      {"type": "error", "message": "..."}
      {"type": "pong"}
      {"type": "server_ping"}  — 服务端心跳探测，客户端须回 {"type": "pong"}
    """
    await websocket.accept()

    # 每 WS 连接的订阅追踪：session_id → (ManagedRun, Subscriber)
    subscriptions: dict[str, tuple[Any, Any]] = {}
    # 读循环和写循环之间的事件桥
    outbound_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    run_manager: RunManager = state.run_manager

    # 死对端检测游标：任何上行消息都会刷新（PONG 是应用层帧，能到达
    # receive_text()——控制帧 PONG 不行，见 _heartbeat_loop 注释）。
    last_received: list[float] = [time.monotonic()]

    async def _send_json(payload: dict[str, Any]) -> None:
        """安全发送 JSON（连接关闭时静默忽略）。"""
        try:
            await websocket.send_text(json.dumps(payload, default=str))
        except Exception:
            logger.debug("WS send 失败（客户端可能已断开）", exc_info=True)

    async def _push_snapshot(session_id: str, after_seq: int = -1) -> None:
        """服务端权威快照：session 当前事件投影 + 在途 run baseline。

        客户端重连时先收到完整快照，之后增量事件无缝接上。

        ``after_seq``（#208，可选）：客户端的本地游标，语义与 SSE `GET /stream`
        的 `after_seq` **逐字相同**——只发 ``after_seq < seq <= replay_upto``
        的那一截，且 backlog 超阈值时改发 `stream/truncated` 控制帧。

        为什么必须收这个游标（而不是继续"全发 + 客户端过滤"）：backlog 保护
        一旦只按"总事件数"判定，就无法区分「客户端已经有 10k 事件、只差尾部」
        与「客户端什么都没有」——前者会被反复要求全量重建，而重建后重新订阅
        仍然超阈值 ⇒ **重建-订阅死循环**。游标是唯一能让两条通道语义一致的
        输入。不带（旧客户端 / 首次订阅）= -1 = 从头发：会话**未超阈值**时拿到的
        就是全量快照，与 #208 之前一致；一旦超阈值，服务端只回那一帧控制帧
        ——从不带游标的客户端拿不到增量，只能每次都被导向全量重建。本仓客户端
        （`web/src/lib/wsStream.ts`）恒带游标，被契约文档与两侧测试锁住。
        """
        from agent_harness.session.service import InvalidSessionId
        from agent_harness.web.app import session_service
        from agent_harness.web.serialization import build_truncated_control

        service = session_service(state)
        try:
            events = await service.get_events(session_id)
        except InvalidSessionId as e:
            await _send_json({"type": "error", "message": str(e)})
            return
        except Exception:  # noqa: BLE001 — WS 错误兜底：任意服务异常都要回 error 帧
            await _send_json({"type": "error", "message": "snapshot failed"})
            return

        active_run = run_manager.get_active(session_id)
        # 阈值判据取**持久化最大 seq**——与 SSE 同一个量（`app.py:1336` 的
        # `handle.latest_seq`）。不能用 run 的入队游标：两者稳态相等（落盘先于
        # listener 入队），但 listener 落后时入队游标领先，同一会话 + 同一游标
        # 会在两条通道上得到**相反裁决**（SSE 重放、WS 截断），而契约写的是
        # "同一条判据"。控制帧里的 `latest_seq` 也取它 ⇒ 两条通道逐字一致。
        latest_seq = events[-1].seq if events else -1
        # 重放窗口上界仍是入队游标：那一截含已入队未落盘的几条，先补上才不丢。
        replay_upto = (
            active_run.last_enqueued_seq if active_run is not None else latest_seq
        )

        # backlog 保护（#208）：与 SSE 通道**同一判据、同一常量**（惰性导入读
        # 模块属性，测试要把它调小才可能构造超限——与 test_web_stream 同法）。
        # 超限只发控制帧：客户端走既有 `GET /events` 全量重建路径，再带真实
        # max seq 回来订阅（那时 backlog 已是 0，不会二次触发）。
        # **不订阅、不起 relay**：这一帧之后客户端会 cancel 本流，先订阅就等于
        # 把队列挂到一个没人消费的 subscriber 上（泄漏，且会随重建次数累积）。
        from agent_harness.web.app import STREAM_REPLAY_MAX_EVENTS

        if latest_seq - after_seq > STREAM_REPLAY_MAX_EVENTS:
            await _send_json({
                "type": "snapshot",
                "session_id": session_id,
                "events": [build_truncated_control(
                    session_id, after_seq=after_seq, latest_seq=latest_seq,
                )],
                "replay_upto": replay_upto,
                # `false` 描述的是**这条连接**（不起 relay、永远不发 done），
                # 不是"快照是全量"——超限时快照恰恰不是全量。客户端据此 settle
                # 收流，再按控制帧去重建（契约 §3 第 2 条同义）。
                "has_active_run": False,
            })
            return

        subscriber = active_run.subscribe() if active_run is not None else None
        # 推快照：窗口内的 durable 事件（客户端仍按 seq 去重——服务端窗口与
        # 客户端游标可能因一次丢帧而错开，双保险比互相信任便宜）。
        window = [e for e in events if after_seq < e.seq <= replay_upto]
        # 整段渲染（`to_dict()` × N + `json.dumps`）下放线程：票面的 5ms 阈值判定为
        # 「必须搬」（#275 站点 1 方案 A，实测数字见 `docs/PERF_BASELINE.md` B7 节）。
        # 帧结构逐字不变。
        await _send_json_offloaded(
            websocket, _render_snapshot, session_id, window, replay_upto,
            active_run is not None,
        )

        if subscriber is not None:
            subscriptions[session_id] = (active_run, subscriber)
            # 启动事件转发 task
            asyncio.create_task(
                _relay_events(session_id, subscriber, outbound_queue, run_manager)
            )

    async def _relay_events(
        session_id: str,
        subscriber: Any,
        out_q: asyncio.Queue,
        rm: RunManager,
    ) -> None:
        """从 subscriber 队列消费事件，放入 WS 出站队列。"""
        try:
            while True:
                event = await subscriber.queue.get()
                if event is rm.DONE:
                    await out_q.put({"type": "done", "session_id": session_id})
                    break
                await out_q.put({
                    "type": "event",
                    "session_id": session_id,
                    "event": build_event_payload(event, session_id),
                })
        except asyncio.CancelledError:
            pass
        finally:
            # 订阅清理
            run = rm.get_active(session_id)
            if run is not None:
                run.unsubscribe(subscriber)
            subscriptions.pop(session_id, None)

    # ── 主读写循环 ──

    async def _read_loop() -> None:
        """读客户端上行消息；任何消息都刷新 last_received（活性信号）。"""
        try:
            while True:
                raw = await websocket.receive_text()
                last_received[0] = time.monotonic()

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send_json({"type": "error", "message": "invalid JSON"})
                    continue

                msg_type = msg.get("type", "")
                if msg_type == "subscribe":
                    sid = msg.get("session_id", "")
                    if sid:
                        # 游标可选（#208）：非 int / 布尔 / 缺失一律当 -1（从头发）。
                        # 不校验就等于把 `replay_upto - after_seq` 交给任意 JSON
                        # 类型去抛 TypeError——那是**连接级**异常，会让整条 WS 挂掉，
                        # 代价远大于"按最保守的从头发"。
                        raw_after = msg.get("after_seq", -1)
                        after = (
                            raw_after
                            if isinstance(raw_after, int) and not isinstance(raw_after, bool)
                            else -1
                        )
                        await _push_snapshot(sid, after)
                elif msg_type == "ping":
                    await _send_json({"type": "pong"})
                elif msg_type == "pong":
                    # server_ping 的应答：活性已由上面的 last_received 刷新，
                    # 无需回包（未知 type 本就被忽略，此分支只为契约显式化）。
                    pass
                elif msg_type == "send_message":
                    # 续聊走 SessionService（业务逻辑不进 WS 层）
                    from agent_harness.session.service import (
                        InvalidSessionId,
                        SessionNotFound,
                        WorkspaceBindingConflict,
                    )
                    from agent_harness.web.app import session_service

                    sid = msg.get("session_id", "")
                    content = msg.get("content", "")
                    mode = msg.get("mode", "queue")
                    if not sid or not content:
                        await _send_json({"type": "error", "message": "missing session_id or content"})
                        continue
                    service = session_service(state)
                    try:
                        result = await service.send_message(
                            session_id=sid, content=content, mode=mode,
                        )
                    # WorkspaceBindingConflict（#266）：WS 是 HTTP 三个端点之外的第四个
                    # 续聊入口——不在这里收编，它会逃到外层的 `except Exception`（只
                    # `logger.debug`）并让连接静默死掉，用户看不到任何原因。
                    except (InvalidSessionId, SessionNotFound, WorkspaceBindingConflict) as e:
                        await _send_json({"type": "error", "message": str(e)})
                        continue
                    if result.status == "launched":
                        # 新 run 启动 → 自动订阅
                        run, sub = result.run, result.subscriber
                        subscriptions[sid] = (run, sub)
                        asyncio.create_task(
                            _relay_events(sid, sub, outbound_queue, run_manager)
                        )
                        await _send_json({
                            "type": "launched",
                            "session_id": sid,
                            "run_started": True,
                        })
                    elif result.status == "queued":
                        await _send_json({
                            "type": "queued",
                            "session_id": sid,
                            "queue_id": result.queued_message.queue_id,
                        })
                    elif result.status == "steered":
                        await _send_json({
                            "type": "steered",
                            "session_id": sid,
                            "steer_id": result.steer_request.steer_id,
                        })
                elif msg_type == "cancel":
                    sid = msg.get("session_id", "")
                    if sid:
                        from agent_harness.web.app import session_service

                        service = session_service(state)
                        try:
                            await service.cancel(sid)
                            await _send_json({"type": "cancelled", "session_id": sid})
                        except Exception:  # noqa: BLE001 — cancel 兜底
                            await _send_json({"type": "error", "message": "cancel failed"})
                # 未知 type → 忽略
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("WS read loop 异常", exc_info=True)

    async def _write_loop() -> None:
        """写出站队列消息。"""
        try:
            while True:
                payload = await outbound_queue.get()
                await _send_json(payload)
        except asyncio.CancelledError:
            pass

    async def _heartbeat_loop() -> None:
        """服务端应用层心跳 + 死对端回收（SDD 02 §7.9「logical heartbeat」）。

        为什么不是控制帧：ASGI 只定义 ``websocket.send``/``websocket.close``，
        Starlette ``WebSocket`` 无 ``send_ping``，uvicorn 对 ``websocket.ping``
        直接抛 RuntimeError——应用层发不出 RFC 6455 PING 控制帧。因此按
        SDD 02 §7.9 允许的「逻辑心跳」实现：

        - 每 ``WS_PING_INTERVAL`` 秒下行 ``{"type": "server_ping"}``；
        - 客户端须以 ``{"type": "pong"}`` 应答（任意上行消息都会刷新
          ``last_received``——应用层帧能到达 ``receive_text()``，而控制帧
          PONG 在协议层被消费、应用层看不到）；
        - ``now - last_received > WS_PING_TIMEOUT`` → 对端已死，close(1001)。

        对照 deepseek-harness：其 Node 服务端能发控制帧 Ping/Pong，故用
        控制帧；本项目受 ASGI 限制，语义一致、载体改为应用层帧。
        """
        try:
            while True:
                await asyncio.sleep(WS_PING_INTERVAL)
                idle = time.monotonic() - last_received[0]
                if idle > WS_PING_TIMEOUT:
                    logger.info("WS 心跳超时（%.0fs 无上行消息），关闭连接", idle)
                    with contextlib.suppress(Exception):
                        await websocket.close(code=1001, reason="heartbeat timeout")
                    break
                try:
                    await websocket.send_text(json.dumps({"type": "server_ping"}))
                except Exception:
                    logger.debug(
                        "WS server_ping 发送失败（对端可能已断开）", exc_info=True
                    )
                    break
        except asyncio.CancelledError:
            pass

    read_task = asyncio.create_task(_read_loop())
    write_task = asyncio.create_task(_write_loop())
    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    try:
        # 等待读循环结束（客户端断开 / 心跳判死关闭）
        await read_task
    finally:
        heartbeat_task.cancel()
        write_task.cancel()
        # 清理所有订阅
        for sid, (run, sub) in list(subscriptions.items()):
            if run is not None:
                run.unsubscribe(sub)
        subscriptions.clear()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        with contextlib.suppress(asyncio.CancelledError):
            await write_task
