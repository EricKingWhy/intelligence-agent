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


def _read_until_pong(ws) -> list[dict]:
    """读到 ``pong`` 栅栏为止，返回途中收到的业务帧（跳过心跳 ``server_ping``）。"""
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


def _drain_blocking(ws, settle: float = 0.35) -> list[dict]:
    """把 WS 上"当前该到的"业务帧收干净（**静默判定**，不是固定轮数）。

    两个坑，都是实测踩出来的：

    1. **``pong`` 会超车板帧**。板帧走**出站队列**（``_write_loop``），而 ``pong``
       由读循环直接 ``send_text``——两条路径写同一个 socket，ping 紧跟订阅发出时
       pong 可能**抢在**尚未被写循环调度的板帧前面到达（实测：栅栏读到 0 帧而板帧
       随后才到）。所以**每轮先 ``settle``（> 节流窗口 250ms）再发 ping**。
    2. **"读到 pong 就返回"不等于"帧已到齐"**。实测：订阅后追加一条事件，帧在
       出站队列里排到了**下一轮**才刷出（`TestClient` 的 portal 调度），单轮栅栏
       读到 0 帧就返回 ⇒ 断言恒真（假绿）。所以这里**连读到连续两轮为空**才收工。

    语义 = "等到安静为止"，与调用方此前是否发过 ping 无关（第一轮会吃掉遗留的
    pong）。断言"没有帧"（``== []``）时也成立：连续两轮空才是真的空。
    """
    frames: list[dict] = []
    quiet = 0
    for _ in range(8):
        time.sleep(settle)
        ws.send_text(json.dumps({"type": "ping"}))
        batch = _read_until_pong(ws)
        if batch:
            frames.extend(batch)
            quiet = 0
        else:
            quiet += 1
            if quiet >= 2:
                return frames
    return frames


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


def test_subscribe_survives_a_failing_snapshot(tmp_path: Path) -> None:
    """快照计算失败时订阅**不得**把异常甩给调用方（#1305 审查 P1-1）。

    ``_publish`` 按观察者纪律吞掉快照失败（只记日志、不入队）⇒ 订阅返回的队列
    可能是**空的**。`websocket.py::_subscribe_board` 原来无条件 ``get_nowait()``
    ⇒ 抛 ``asyncio.QueueEmpty``（不是 ``CancelledError``，`_unsubscribe_board` 的
    suppress 也覆盖不到）冲出订阅分支：轻则读循环中断、订阅从未建立，重则未处理
    任务异常。用户侧表现是"板永远空着且**不降级**"（前端降级只认 onerror/onclose，
    收不到服务端异常语义）。

    判据 = 订阅照常返回、订阅集合照常登记（连接仍可用），且随后一次成功的发布
    能把帧送达。
    """
    calls = 0

    async def flaky_snapshot() -> list[dict]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("会话 JSONL 损坏")
        return [{"session_id": "s1"}]

    async def main() -> tuple[bool, list[dict]]:
        broadcaster = BoardBroadcaster(snapshot=flaky_snapshot)
        # 首帧失败：不得抛，队列是空的。
        queue = await broadcaster.subscribe()
        assert queue.empty(), "快照失败时不该有首帧"
        # 订阅本身已登记（连接可用），后续成功的发布照常送达。
        assert broadcaster.subscriber_count == 1
        broadcaster.mark_dirty()
        await asyncio.sleep(PUBLISH_THROTTLE_MS / 1000 + 0.3)
        return True, await _drain(queue)

    ok, frames = asyncio.run(main())
    assert ok
    assert len(frames) == 1, f"后续发布应送达 1 帧，实得 {len(frames)}"
    assert frames[0]["sessions"] == [{"session_id": "s1"}]


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """轮询等 ``predicate()`` 为真（``ws.send_text`` 是 fire-and-forget，服务端处理
    完之前不能断言）。超时返回 False——**不阻塞**，所以变异下是用例失败而非挂死。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_subscribe_board_with_a_failing_snapshot_keeps_the_socket_usable(
    tmp_path: Path,
) -> None:
    """端到端：快照失败时 ``subscribe_board`` 不打断读循环（P1-1 的接线判据）。

    两个坑，都是实测踩出来的：

    - **不能用"读一帧"当活性判据**：读循环真断了的话，``receive_text()`` 会**永久
      阻塞**（用例挂死而不是失败，M1 变异下正是这个形状）⇒ 活性探针改用
      ``unsubscribe_board``（同样由读循环处理，效果可从 ``subscriber_count`` 读到，
      不发也不收 socket 帧）。
    - **``ws.send_text`` 是 fire-and-forget**：发完立刻断言会读到尚未处理的旧状态
      （本用例初版就这么假红过）⇒ 一律用 :func:`_wait_until` 轮询。

    判据是"订阅**建立并保持**"：M1 变异（去掉 ``suppress(QueueEmpty)``）下
    ``subscribe()`` 先登记、异常随即冲出读循环、``finally`` 再摘掉 ⇒ 计数先 1 后 0，
    轮询超时（或保持断言失败）。
    """
    client = _app(tmp_path)
    broadcaster = client.app.state.agent.board_broadcaster

    # 把快照工厂换成"总是失败"——模拟 JSONL 损坏 / 读盘异常。
    async def boom() -> list[dict]:
        raise RuntimeError("快照算不出来")

    broadcaster._snapshot = boom

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        assert _wait_until(lambda: broadcaster.subscriber_count == 1), (
            "订阅没建立：快照异常冲出了订阅分支（读循环已断）"
        )
        # 保持性：异常发生在 subscribe 返回**之后**（`get_nowait` 那一步），
        # 若没被 suppress，finally 会在这一刻把订阅摘掉。
        time.sleep(0.3)
        assert broadcaster.subscriber_count == 1, "订阅被快照异常连带摘掉了"

        # 读循环还活着：同一连接上还能处理下一条上行。
        ws.send_text(json.dumps({"type": "unsubscribe_board"}))
        assert _wait_until(lambda: broadcaster.subscriber_count == 0), (
            "读循环被快照异常打断了（unsubscribe_board 没被处理）"
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
    """触发集事件（``task/defined``）⇒ 经投影更新点推出新帧（端到端接线判据）。

    会话**先建、再订阅**：``Session.start`` 写的是 ``SESSION_STARTED``（触发集外的
    水位事件），#1305 起它**也会**推帧（板行含 ``event_count``，见下一条用例）——
    放在订阅后建会让本用例量到两帧，掩盖"一次触发事件推一帧"这条判据。
    """
    client = _app(tmp_path)
    _seed(tmp_path, client, 1)
    store: JsonlSessionStore = client.app.state.agent.store
    session = Session.start(store, cwd=str(tmp_path))

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        initial = _drain_blocking(ws)
        assert len(initial) == 1
        before = {row["session_id"]: row for row in initial[0]["sessions"]}
        assert session.session_id in before, "订阅首帧应已含新建的会话"

        # 窗口已过（首帧即 leading）⇒ 下一次触发是新的 leading，立即成帧。
        outcome = apply_task_definition(
            session,
            task_text="新增一张卡",
            criteria=[{"text": "要出现", "origin": "user"}],
        )
        assert outcome.ok, outcome.reason

        frames = _drain_blocking(ws)

    assert len(frames) == 1, f"一次触发集事件应推一帧，实得 {len(frames)}"
    assert frames[0]["type"] == "board_snapshot", frames[0]
    rows = {row["session_id"]: row for row in frames[0]["sessions"]}
    # 帧确实反映**新事件**（不是把订阅首帧又发了一遍）：水位前进了。
    assert rows[session.session_id]["event_count"] > before[session.session_id]["event_count"]


def test_non_trigger_event_still_pushes_a_frame(tmp_path: Path) -> None:
    """**触发集外**的事件也推帧（#1305 审查 P2-1 的判据）。

    板行含 `event_count` / `last_event_time`（`app.py::session_rows` 逐字段映射
    `SessionSummaryStats`），而这两个值**每条事件都会变**——`ASSISTANT_MESSAGE` /
    `TOOL_RESULT` 都在触发集外。只按触发集通知的话，长 run 期间卡片的时间标签会
    停在最后一个触发事件那一刻（前端 `KanbanCard.tsx` 渲染 `last_event_time`，
    `lib/kanban.ts` 列内同键排序也用它）。

    成本侧由 250ms 节流封顶（4 帧/秒）+ 无订阅者时空操作，见 `web/board.py`。
    """
    client = _app(tmp_path)
    _seed(tmp_path, client, 1)
    store: JsonlSessionStore = client.app.state.agent.store
    broadcaster = client.app.state.agent.board_broadcaster
    # 会话在**订阅之前**建好：`SESSION_STARTED` 也是触发集外的水位事件，#1305 起
    # 它同样推帧——放订阅后建会让本用例量到两帧，说不清是哪条事件推的。
    session = Session.start(store, cwd=str(tmp_path))

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "subscribe_board"}))
        initial = _drain_blocking(ws)
        assert len(initial) == 1
        before = {row["session_id"]: row for row in initial[0]["sessions"]}

        # 触发集外的高频事件（`TOOL_RESULT` 不在 `_BOARD_PROJECTION_TRIGGER_TYPES`）。
        # 不能用 `USER_MESSAGE`：#1308 起它已在触发集内，用它测"触发集外"是假绿。
        from agent_harness.session.event import TOOL_RESULT

        session.append(
            TOOL_RESULT,
            {"tool_call_id": "call-1", "content": "工具输出"},
        )
        assert broadcaster.subscriber_count == 1

        frames = _drain_blocking(ws)

    assert len(frames) == 1, (
        f"触发集外事件也应推一帧（板行含 event_count/last_event_time），实得 {len(frames)}"
    )
    rows = {row["session_id"]: row for row in frames[0]["sessions"]}
    # 帧确实反映了这条非触发事件（水位前进）——这正是"只按触发集通知"会漏掉的更新。
    assert rows[session.session_id]["event_count"] > before[session.session_id]["event_count"]


def test_dirty_listener_can_be_removed(tmp_path: Path) -> None:
    """``remove_dirty_listener`` 摘得掉、幂等（#1305 审查 P2-1(S)/P2-3(C)）。

    投影 store 是**进程级单例**，监听器是绑定方法 ⇒ 强引用监听者整棵对象图。
    没有移除 API 的话，测试进程里每构造一个 `AppState` 就多钉一份，GC 收不掉。
    """
    from agent_harness.session.board_projection import get_board_projection_store

    client = _app(tmp_path)
    state = client.app.state.agent
    projection_store = get_board_projection_store(state.store)
    broadcaster = state.board_broadcaster

    assert state._board_listener in projection_store._dirty_listeners

    projection_store.remove_dirty_listener(state._board_listener)
    assert state._board_listener not in projection_store._dirty_listeners

    # 幂等：再摘一次不抛（`suppress(ValueError)`）。
    projection_store.remove_dirty_listener(state._board_listener)

    # 摘掉后事件不再触达该广播器（订阅者数为 0 时本就是空操作 ⇒ 这里直接验接线
    # 断了：重新挂回再摘，仍不炸）。
    projection_store.add_dirty_listener(state._board_listener)
    assert broadcaster.subscriber_count == 0


def test_shutdown_removes_the_dirty_listener(tmp_path: Path) -> None:
    """``AppState.shutdown()`` 摘监听器（生命周期归 lifespan，不留悬空引用）。

    必须用 ``with`` 进 TestClient：lifespan 只在上下文管理器里跑（裸构造不跑
    startup/teardown），``client.close()`` 单独调不触发 ``state.shutdown()``。
    """
    from agent_harness.session.board_projection import get_board_projection_store

    with _app(tmp_path) as client:
        state = client.app.state.agent
        projection_store = get_board_projection_store(state.store)
        assert state._board_listener in projection_store._dirty_listeners

    # 退出 with ⇒ lifespan 的 finally 跑过 `state.shutdown()`。
    assert state._board_listener not in projection_store._dirty_listeners, (
        "shutdown 未摘监听器：单例会永久钉住 AppState 对象图"
    )
