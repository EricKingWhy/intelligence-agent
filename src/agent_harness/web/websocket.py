"""WebSocket 多路复用通道（T2 / #132，PRD §5.1）。

单连接多路复用所有 session 的 streaming / 推送：
- 客户端 ``subscribe(session_id)`` 后，服务端推该 session 的增量事件。
- 断线重连：客户端开新 WS + ``subscribe(session_id)``，服务端先推一份
  完整快照（session 当前事件投影 + 在途 run baseline），之后增量。
- 心跳是**客户端主动**的：客户发 ``{"type": "ping"}``，服务端回
  ``{"type": "pong"}``（应用层心跳，供客户端检测连接活性）。

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
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

from agent_harness.web.serialization import build_event_payload

if TYPE_CHECKING:
    from agent_harness.web.app import AppState
    from agent_harness.web.runmanager import RunManager

logger = logging.getLogger("agent_harness.web.websocket")


async def handle_websocket(websocket: WebSocket, state: AppState) -> None:
    """WebSocket 主入口：接受连接 → 多路复用 session 事件流。

    上行消息格式（JSON）：
      {"type": "subscribe", "session_id": "..."}
      {"type": "send_message", "session_id": "...", "content": "...", "mode": "queue"}
      {"type": "steer", "session_id": "...", "content": "..."}
      {"type": "cancel", "session_id": "..."}
      {"type": "ping"}  — client-initiated ping, server replies pong

    下行消息格式（JSON）：
      {"type": "snapshot", "session_id": "...", "events": [...]}
      {"type": "event", "session_id": "...", "event": {...}}
      {"type": "done", "session_id": "..."}
      {"type": "error", "message": "..."}
      {"type": "pong"}
    """
    await websocket.accept()

    # 每 WS 连接的订阅追踪：session_id → (ManagedRun, Subscriber)
    subscriptions: dict[str, tuple[Any, Any]] = {}
    # 读循环和写循环之间的事件桥
    outbound_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    run_manager: RunManager = state.run_manager

    async def _send_json(payload: dict[str, Any]) -> None:
        """安全发送 JSON（连接关闭时静默忽略）。"""
        try:
            await websocket.send_text(json.dumps(payload, default=str))
        except Exception:
            logger.debug("WS send 失败（客户端可能已断开）", exc_info=True)

    async def _push_snapshot(session_id: str) -> None:
        """服务端权威快照：session 当前事件投影 + 在途 run baseline。

        客户端重连时先收到完整快照，之后增量事件无缝接上。
        """
        from agent_harness.session.service import InvalidSessionId, SessionService

        service = SessionService(state)
        try:
            events = await service.get_events(session_id)
        except InvalidSessionId as e:
            await _send_json({"type": "error", "message": str(e)})
            return
        except Exception:  # noqa: BLE001 — WS 错误兜底：任意服务异常都要回 error 帧
            await _send_json({"type": "error", "message": "snapshot failed"})
            return

        active_run = run_manager.get_active(session_id)
        subscriber = active_run.subscribe() if active_run is not None else None
        replay_upto = (
            active_run.last_enqueued_seq if active_run is not None else events[-1].seq
        )

        # 推快照：全部 durable 事件（客户端据 seq 去重）
        await _send_json({
            "type": "snapshot",
            "session_id": session_id,
            "events": [e.to_dict() for e in events],
            "replay_upto": replay_upto,
            "has_active_run": active_run is not None,
        })

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
        """读客户端上行消息。"""
        try:
            while True:
                raw = await websocket.receive_text()

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await _send_json({"type": "error", "message": "invalid JSON"})
                    continue

                msg_type = msg.get("type", "")
                if msg_type == "subscribe":
                    sid = msg.get("session_id", "")
                    if sid:
                        await _push_snapshot(sid)
                elif msg_type == "ping":
                    await _send_json({"type": "pong"})
                elif msg_type == "send_message":
                    # 续聊走 SessionService（业务逻辑不进 WS 层）
                    from agent_harness.session.service import (
                        InvalidSessionId,
                        SessionNotFound,
                        SessionService,
                    )

                    sid = msg.get("session_id", "")
                    content = msg.get("content", "")
                    mode = msg.get("mode", "queue")
                    if not sid or not content:
                        await _send_json({"type": "error", "message": "missing session_id or content"})
                        continue
                    service = SessionService(state)
                    try:
                        result = await service.send_message(
                            session_id=sid, content=content, mode=mode,
                        )
                    except (InvalidSessionId, SessionNotFound) as e:
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
                        from agent_harness.session.service import SessionService

                        service = SessionService(state)
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

    read_task = asyncio.create_task(_read_loop())
    write_task = asyncio.create_task(_write_loop())

    try:
        # 等待读循环结束（客户端断开）
        await read_task
    finally:
        write_task.cancel()
        # 清理所有订阅
        for sid, (run, sub) in list(subscriptions.items()):
            if run is not None:
                run.unsubscribe(sub)
        subscriptions.clear()
        with contextlib.suppress(asyncio.CancelledError):
            await write_task
