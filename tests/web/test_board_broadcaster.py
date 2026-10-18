"""#1305 §7：看板 WS 全量快照 + 250ms leading/trailing 节流（AC-14）。

判据口径（票面 §7 与 AC-14 原文）：

① 订阅 ``subscribe_board`` 后连续制造 10 条触发集事件（间隔 <250ms）⇒ 1s 内
   ``board_snapshot`` 帧数 **≤3 且 ≥1**；
② 最后一帧 ``sessions`` 与 ``GET /api/sessions?include_archived=true`` **逐字段相等**；
③ 断开 WS 后制造事件，**不再收到任何帧**。

另加三条结构性判据（票面 §7「触发与节流」的硬要求，AC-14 不直接覆盖）：

- **无订阅者时 ``mark_dirty`` 是空操作**（不算快照、不炸）；
- **``mark_dirty`` 必须线程安全**：``_persist_event`` 可能跑在 worker 线程，
  非事件循环线程调用不得直接碰 asyncio 原语；
- **连接关闭清订阅**（AC-14 ③ 的服务端侧机械判据——不留悬空订阅者）。

节流形状照抄 Orca ``useDashboardPopoutBridge.ts:13`` / ``:187-209``（@ ``de1f6e3d``，MIT）：
``PUBLISH_THROTTLE_MS = 250``，窗口内首次变化立即推（leading）、突发合并成一次尾推
（trailing），窗口外的下一次变化重新起一个 leading。

⚠ 帧计数一律用**本文件内的闭包/队列**，不依赖 fixtures——``fixtures.ts`` 的钩子是
回调不是计数器（#1304 审查 P2-5 的教训，票面同样点名）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.task import apply_task_definition
from agent_harness.web.app import create_app
from agent_harness.web.board import PUBLISH_THROTTLE_MS, BoardBroadcaster

#: 突发间隔：10 条 × 20ms = 200ms，整体落在同一个 250ms 窗口内。
_BURST_INTERVAL_S = 0.02
_BURST_SIZE = 10


# ── 测试替身 ────────────────────────────────────────────────────────────────


class _SnapshotStub:
    """快照工厂替身：记录调用次数，返回固定行（节流判据不需要真会话）。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.calls = 0
        self._rows = rows if rows is not None else [{"session_id": "s1"}]

    async def __call__(self) -> list[dict]:
        self.calls += 1
        return self._rows


async def _drain(queue: asyncio.Queue) -> list[dict]:
    """非阻塞取空队列（判据是**帧数**，不是「等到某一帧」）。"""
    frames: list[dict] = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    return frames


def _drain_blocking(ws, settle: float = 0.35) -> list[dict]:
    """读 WS 上当前已排队的业务帧，遇到 ``pong`` 栅栏即停。

    ⚠ **必须先 ``settle`` 再发 ping**：板帧走**出站队列**（``_write_loop``），
    而 ``pong`` 由读循环直接 ``send_text``——两条路径都往同一个 socket 写，
    ping 紧跟订阅发出时 pong 可能**抢在**尚未被写循环调度的板帧前面到达
    （实测：栅栏读到 0 帧而板帧随后才到）。``settle`` 取 > 节流窗口（250ms），
    保证任何已产生的板帧（含 trailing）都已刷出，栅栏才是确定性的。
    """
    time.sleep(settle)
    ws.send_text(json.dumps({"type": "ping"}))
    frames: list[dict] = []
    for _ in range(50):
        frame = json.loads(ws.receive_text())
        if frame.get("type") == "pong":
            return frames
        # 应用层心跳（2s 一次）不是业务帧——`settle` 跨到心跳节拍时它会混进来，
        # 判据只量**板帧**，跳过它（同 `_recv_ws_frame` 的既有形状）。
        if frame.get("type") == "server_ping":
            continue
        frames.append(frame)
    raise AssertionError("等待 pong 栅栏时读了 50 帧仍未见到——协议可能变了")


# ── ① 节流帧数上下界（AC-14 ①）─────────────────────────────────────────────


def test_burst_within_window_yields_bounded_frame_count() -> None:
    """10 条间隔 20ms 的触发 ⇒ 1s 内 1~3 帧（leading 立即 + 突发合并尾推）。"""

    async def main() -> list[dict]:
        stub = _SnapshotStub()
        broadcaster = BoardBroadcaster(snapshot=stub)
        queue = await broadcaster.subscribe()
        # 订阅本身推的首帧不计入突发读数（AC-14 ① 量的是"制造事件之后"）。
        await asyncio.wait_for(queue.get(), timeout=2.0)

        for _ in range(_BURST_SIZE):
            broadcaster.mark_dirty()
            await asyncio.sleep(_BURST_INTERVAL_S)

        await asyncio.sleep(1.0)
        return await _drain(queue)

    frames = asyncio.run(main())
    assert 1 <= len(frames) <= 3, f"突发帧数越界：{len(frames)}（判据 1..3）"
    assert all(f["type"] == "board_snapshot" for f in frames), frames


def test_leading_edge_publishes_immediately_without_waiting_for_window() -> None:
    """窗口外（距上次推送 ≥250ms）的变化**立即**成帧，不压到尾部。

    ⚠ 订阅本身推的首帧就落在窗口起点上，所以必须先**等过一个完整窗口**再
    ``mark_dirty``——否则测的是 trailing 分支（实测：不等就断言，读到的是
    恰好 250ms 的尾推）。
    """

    async def main() -> float:
        stub = _SnapshotStub()
        broadcaster = BoardBroadcaster(snapshot=stub)
        queue = await broadcaster.subscribe()
        await asyncio.wait_for(queue.get(), timeout=2.0)  # 订阅首帧

        await asyncio.sleep(PUBLISH_THROTTLE_MS / 1000 + 0.1)  # 让窗口过期
        loop = asyncio.get_running_loop()
        started = loop.time()
        broadcaster.mark_dirty()
        await asyncio.wait_for(queue.get(), timeout=2.0)
        return loop.time() - started

    elapsed = asyncio.run(main())
    # leading：远早于 250ms 窗口就成帧（给足调度余量，仍远小于节流窗口）。
    assert elapsed < PUBLISH_THROTTLE_MS / 1000 / 2, f"leading 未立即成帧：{elapsed:.3f}s"


def test_trailing_edge_fires_once_after_the_window() -> None:
    """窗口内的突发**合并成一次**尾推（不是每来一条推一次）。"""

    async def main() -> list[dict]:
        stub = _SnapshotStub()
        broadcaster = BoardBroadcaster(snapshot=stub)
        queue = await broadcaster.subscribe()
        await asyncio.wait_for(queue.get(), timeout=2.0)

        broadcaster.mark_dirty()
        await asyncio.wait_for(queue.get(), timeout=2.0)  # leading 帧
        for _ in range(5):
            broadcaster.mark_dirty()  # 全部落在同一个窗口内
        await asyncio.sleep(PUBLISH_THROTTLE_MS / 1000 + 0.2)
        return await _drain(queue)

    frames = asyncio.run(main())
    assert len(frames) == 1, f"窗口内 5 次变化应合并为 1 次尾推，实得 {len(frames)}"


def test_leading_and_trailing_are_distinguishable_when_window_has_expired() -> None:
    """窗口**已过期**时连来两条变化 ⇒ 2 帧（leading 立即 + 一条尾推）。

    这条用例存在的理由（变异实测，不是推测）：``elapsed >= 阈值`` 那一支若被
    短路（``if False``），窗口过期后的 ``call_later(负数)`` 会**立即**触发 ——
    于是"leading 立即成帧"的读数**照样成立**，``test_leading_edge_*`` 与
    ``test_burst_*`` 都鉴别不出（实测 M1 变异存活，15 passed）。本用例钉的是
    两支的**可区分差别**：leading 支还会**清掉**残留的尾推定时器，因此窗口过期
    后的第一条变化立即成帧、第二条再排一个尾推 ⇒ 恰好 2 帧；短路后两条变化都
    落到同一次延迟为零的尾推上 ⇒ 1 帧。
    """

    async def main() -> list[dict]:
        stub = _SnapshotStub()
        broadcaster = BoardBroadcaster(snapshot=stub)
        queue = await broadcaster.subscribe()
        await asyncio.wait_for(queue.get(), timeout=2.0)  # 订阅首帧

        await asyncio.sleep(PUBLISH_THROTTLE_MS / 1000 + 0.1)  # 让窗口过期
        broadcaster.mark_dirty()  # leading：立即
        broadcaster.mark_dirty()  # 紧随其后：排一个尾推
        await asyncio.sleep(PUBLISH_THROTTLE_MS / 1000 + 0.3)
        return await _drain(queue)

    frames = asyncio.run(main())
    assert len(frames) == 2, (
        f"窗口过期后两条变化应为 leading+trailing 共 2 帧，实得 {len(frames)}"
    )


# ── 无订阅者空操作 ─────────────────────────────────────────────────────────


def test_mark_dirty_is_a_noop_without_subscribers() -> None:
    """没有订阅者时 ``mark_dirty`` 不算快照、不抛异常（票面 §7 硬要求）。"""

    async def main() -> int:
        stub = _SnapshotStub()
        broadcaster = BoardBroadcaster(snapshot=stub)
        for _ in range(5):
            broadcaster.mark_dirty()
        await asyncio.sleep(0.05)
        assert broadcaster.subscriber_count == 0
        return stub.calls

    assert asyncio.run(main()) == 0, "无订阅者时不得计算快照"


def test_last_subscriber_leaving_stops_the_snapshot_work() -> None:
    """退订后 ``mark_dirty`` 回到空操作（订阅集合是唯一开关）。"""

    async def main() -> int:
        stub = _SnapshotStub()
        broadcaster = BoardBroadcaster(snapshot=stub)
        queue = await broadcaster.subscribe()
        await asyncio.wait_for(queue.get(), timeout=2.0)
        broadcaster.unsubscribe(queue)
        assert broadcaster.subscriber_count == 0

        calls_after_unsubscribe = stub.calls
        broadcaster.mark_dirty()
        await asyncio.sleep(PUBLISH_THROTTLE_MS / 1000 + 0.2)
        return stub.calls - calls_after_unsubscribe

    assert asyncio.run(main()) == 0, "退订后仍在算快照"


# ── 线程安全（``call_soon_threadsafe``）────────────────────────────────────


def test_mark_dirty_from_worker_thread_reaches_subscribers() -> None:
    """worker 线程调 ``mark_dirty`` 必须能成帧——且**不得**直接碰 asyncio 原语。

    判据是"从非事件循环线程调用后帧真的到了"：``asyncio.Queue.put_nowait`` 与
    定时器都不是线程安全的，只有经 ``loop.call_soon_threadsafe`` 回到循环才成立。
    """

    async def main() -> dict:
        stub = _SnapshotStub()
        broadcaster = BoardBroadcaster(snapshot=stub)
        queue = await broadcaster.subscribe()
        await asyncio.wait_for(queue.get(), timeout=2.0)  # 订阅首帧

        worker = threading.Thread(target=broadcaster.mark_dirty)
        worker.start()
        worker.join(timeout=5.0)
        assert not worker.is_alive(), "worker 线程未退出"

        return await asyncio.wait_for(queue.get(), timeout=2.0)

    frame = asyncio.run(main())
    assert frame["type"] == "board_snapshot", frame


def test_mark_dirty_from_worker_thread_without_a_loop_is_a_noop() -> None:
    """从未绑定事件循环时调用（无订阅者）不得炸——它在生产里就是空操作。"""
    broadcaster = BoardBroadcaster(snapshot=_SnapshotStub())
    broadcaster.mark_dirty()  # 不抛即通过
    assert broadcaster.subscriber_count == 0


# ── 帧形状 ─────────────────────────────────────────────────────────────────


def test_frame_carries_iso8601_generated_at_and_session_rows() -> None:
    """下行帧形状 = ``{type, generated_at, sessions}``，``generated_at`` 是 ISO8601。"""

    async def main() -> dict:
        broadcaster = BoardBroadcaster(snapshot=_SnapshotStub([{"session_id": "s9"}]))
        queue = await broadcaster.subscribe()
        return await asyncio.wait_for(queue.get(), timeout=2.0)

    frame = asyncio.run(main())
    assert set(frame) == {"type", "generated_at", "sessions"}, frame
    assert frame["type"] == "board_snapshot"
    assert frame["sessions"] == [{"session_id": "s9"}]
    # ISO8601 可往返解析（不锁具体时区/精度，只锁"是 ISO8601"）。
    from datetime import datetime

    datetime.fromisoformat(frame["generated_at"])


# ── ② 快照与 REST 逐字段相等（AC-14 ②）──────────────────────────────────


def _app(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False))


def _seed(tmp_path: Path, client: TestClient, count: int) -> list[str]:
    """直接经 store 造会话（``task/defined`` 属触发集，会推动投影更新）。"""
    store: JsonlSessionStore = client.app.state.agent.store
    session_ids: list[str] = []
    for index in range(count):
        session = Session.start(store, cwd=str(tmp_path))
        outcome = apply_task_definition(
            session,
            task_text=f"看板推送 {index}",
            criteria=[{"text": "能推送", "origin": "user"}],
        )
        assert outcome.ok, outcome.reason
        session_ids.append(session.session_id)
    return session_ids


def test_ws_snapshot_matches_rest_rows_field_by_field(tmp_path: Path) -> None:
    """AC-14 ②：WS 帧 ``sessions`` 与 ``GET /api/sessions?include_archived=true`` 逐字段相等。

    契约漂移是本票点名的风险（REST 在 ``app.py`` 逐字段手写映射、WS 另写序列化）
    ⇒ 必须断言**值**相等，不只是键在。
    """
    client = _app(tmp_path)
    _seed(tmp_path, client, 3)

    rest = client.get("/api/sessions", params={"include_archived": "true"})
    assert rest.status_code == 200, rest.text
    rest_rows = rest.json()
    assert len(rest_rows) == 3, rest_rows

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        frames = _drain_blocking(ws)

    assert len(frames) == 1, f"订阅应恰好推一帧初始快照，实得 {len(frames)}"
    frame = frames[0]
    assert frame["type"] == "board_snapshot", frame
    assert frame["sessions"] == rest_rows, "WS 快照与 REST 行不一致（契约漂移）"


def test_ws_snapshot_includes_archived_rows(tmp_path: Path) -> None:
    """快照走 ``include_archived=True`` 同一个函数（归档行不得在 WS 侧静默消失）。"""
    client = _app(tmp_path)
    session_ids = _seed(tmp_path, client, 2)
    archived = client.post(f"/api/sessions/{session_ids[0]}/archive")
    assert archived.status_code == 200, archived.text

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        frames = _drain_blocking(ws)

    rows = frames[0]["sessions"]
    assert len(rows) == 2, rows
    assert any(row["archived"] is True for row in rows), rows


# ── ③ 断订阅 / 断连后不再收帧（AC-14 ③）────────────────────────────────


def test_unsubscribe_board_stops_frames_but_keeps_the_connection_alive(tmp_path: Path) -> None:
    """``unsubscribe_board`` 后订阅集合归零，且连接仍可用（ping ⇒ pong）。"""
    client = _app(tmp_path)
    _seed(tmp_path, client, 1)
    broadcaster = client.app.state.agent.board_broadcaster

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        assert len(_drain_blocking(ws)) == 1
        assert broadcaster.subscriber_count == 1

        ws.send_text(json.dumps({"type": "unsubscribe_board"}))
        ws.send_text(json.dumps({"type": "ping"}))
        assert _drain_blocking(ws) == [], "退订后不得再收到看板帧"
        assert broadcaster.subscriber_count == 0, "退订未清订阅"

        # 连接仍活着：后续 ping 照常有应答。
        ws.send_text(json.dumps({"type": "ping"}))
        assert _drain_blocking(ws) == []


def test_events_after_unsubscribe_produce_no_frames(tmp_path: Path) -> None:
    """AC-14 ③（服务端侧）：退订后制造触发事件，订阅集合仍为空。"""
    client = _app(tmp_path)
    _seed(tmp_path, client, 1)
    store: JsonlSessionStore = client.app.state.agent.store
    broadcaster = client.app.state.agent.board_broadcaster

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        _drain_blocking(ws)

        ws.send_text(json.dumps({"type": "unsubscribe_board"}))
        _drain_blocking(ws)

        session = Session.start(store, cwd=str(tmp_path))
        outcome = apply_task_definition(
            session,
            task_text="退订之后",
            criteria=[{"text": "不该有帧", "origin": "user"}],
        )
        assert outcome.ok, outcome.reason
        assert broadcaster.subscriber_count == 0

        assert _drain_blocking(ws) == [], "退订后制造事件仍收到帧"


def test_closing_the_socket_clears_the_board_subscription(tmp_path: Path) -> None:
    """连接关闭清订阅（票面 §7「连接关闭时清订阅」）。"""
    client = _app(tmp_path)
    _seed(tmp_path, client, 1)
    broadcaster = client.app.state.agent.board_broadcaster

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        ws.send_text(json.dumps({"type": "ping"}))
        _drain_blocking(ws)
        assert broadcaster.subscriber_count == 1

    assert broadcaster.subscriber_count == 0, "连接关闭未清看板订阅"


# ── 触发源：投影更新点（票面 §7「触发源 = T1 投影更新点」）──────────────


def test_projection_trigger_event_pushes_a_frame_to_subscribers(tmp_path: Path) -> None:
    """触发集事件（``task/defined``）⇒ 经投影更新点推出新帧（端到端接线判据）。"""
    client = _app(tmp_path)
    _seed(tmp_path, client, 1)
    store: JsonlSessionStore = client.app.state.agent.store

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        initial = _drain_blocking(ws)
        assert len(initial) == 1

        # 窗口已过（首帧即 leading）⇒ 下一次触发是新的 leading，立即成帧。
        session = Session.start(store, cwd=str(tmp_path))
        outcome = apply_task_definition(
            session,
            task_text="新增一张卡",
            criteria=[{"text": "要出现", "origin": "user"}],
        )
        assert outcome.ok, outcome.reason

        frames = _drain_blocking(ws)

    assert len(frames) == 1, f"触发集事件应推一帧，实得 {len(frames)}"
    assert frames[0]["type"] == "board_snapshot", frames[0]
    assert len(frames[0]["sessions"]) == 2, frames[0]["sessions"]


def test_non_trigger_event_does_not_push_a_frame(tmp_path: Path) -> None:
    """触发集外的事件不推帧（节流只在服务端做一次，不靠前端二次节流兜）。"""
    client = _app(tmp_path)
    _seed(tmp_path, client, 1)
    store: JsonlSessionStore = client.app.state.agent.store
    broadcaster = client.app.state.agent.board_broadcaster

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        ws.send_text(json.dumps({"type": "ping"}))
        _drain_blocking(ws)

        # 冷启动：先让该会话的投影算过一次，再追加触发集外的 USER_MESSAGE。
        session = Session.start(store, cwd=str(tmp_path))
        from agent_harness.session.event import USER_MESSAGE

        session.append(USER_MESSAGE, {"content": "普通聊天"})
        assert broadcaster.subscriber_count == 1

        assert _drain_blocking(ws) == [], "触发集外事件不该推帧"
