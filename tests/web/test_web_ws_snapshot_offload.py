"""#275 站点 1：WS 快照的序列化下放线程（方案 A），**帧结构逐字不变**。

本票对 WS 这一侧的**红线**是「1000 事件窗口的 `to_dict()` × N + `json.dumps` 跑在
事件循环线程上」（实测 P90 ≥5.6ms / 最长 7–32ms，见 `docs/PERF_BASELINE.md` B7 节）。
所以这里锁三件事：

1. **重活不在循环线程上**（AC3）：`_send_json_offloaded` 必须在工作线程里调 renderer；
2. **发送仍在循环线程上**（票面 Risks：把 `send_text` 也搬线程会破坏 WebSocket
   并发发送语义）——这一条同样必须被测试钉住，否则「顺手把发送也搬了」没人拦得住；
3. **帧字节不变**（AC6）：`_render_snapshot` 的输出与改造前那段内联表达式逐字相同
   （键序 + `default=str` + `ensure_ascii=True` 全部钉死；键序变了线上字节就变了，
   虽然语义等价，但那属于契约变化）。

另有一条**接线守卫**（`test_snapshot_path_uses_offload_helper`）：这是本票唯一
可能被静默回退的地方——把调用改回内联渲染，测试全绿而优化消失。守卫直接读 handler
源码断言「内联列表推导已不在 handler 里」，并在改造前就会红。
"""

from __future__ import annotations

import dis
import inspect
import json
import threading

import pytest

import agent_harness.web.websocket as wsmod
from agent_harness.session.event import SessionEvent

SESSION = "sess-ws-offload-1"

#: 改造前 `_push_snapshot` 里那段内联列表推导的原样文本（键序与它绑定）。
_LEGACY_INLINE = 'events": [e.to_dict() for e in window]'


class _FakeWebSocket:
    """只实现 `send_text`，并记录**发送发生在哪个线程**。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, threading.Thread]] = []

    async def send_text(self, text: str) -> None:
        self.sent.append((text, threading.current_thread()))


class _BrokenWebSocket:
    async def send_text(self, text: str) -> None:
        del text  # 与真实签名对齐；未用参数显式丢弃，避免 ARG002
        raise RuntimeError("client already gone")


def _event(seq: int) -> SessionEvent:
    return SessionEvent(
        event_id=f"evt-{seq}", seq=seq, time="2026-09-18T00:00:00.000+00:00",
        type="text/delta", session_id=SESSION, run_id="run-1", step_id=1,
        block_id="b1", data={"text": "你好"},
    )


# —— AC6：帧字节不变 ——


class TestFrameBytesUnchanged:
    def test_matches_legacy_inline_expression(self) -> None:
        events = [_event(1), _event(2)]
        frame = wsmod._render_snapshot(SESSION, events, 7, True)
        # 改造前那段表达式的**逐字**等价物
        expected = json.dumps({
            "type": "snapshot",
            "session_id": SESSION,
            "events": [e.to_dict() for e in events],
            "replay_upto": 7,
            "has_active_run": True,
        }, default=str)
        assert frame == expected

    def test_key_order_is_pinned(self) -> None:
        """键序就是线上字节序——不是"字典序随便"的事。"""
        frame = wsmod._render_snapshot(SESSION, [], 0, False)
        positions = [frame.index(f'"{key}"') for key in
                     ("type", "session_id", "events", "replay_upto", "has_active_run")]
        assert positions == sorted(positions), frame

    def test_ensure_ascii_true_is_pinned(self) -> None:
        r"""WS 帧走 `ensure_ascii` 默认 True（中文转成 `\uXXXX` 转义）——与磁盘上的
        JSONL（`ensure_ascii=False`）不同；这条差异决定了帧比磁盘行大得多，
        它是 B7 基线数字的前提之一。"""
        frame = wsmod._render_snapshot(SESSION, [_event(1)], 1, True)
        assert "\\u4f60\\u597d" in frame
        assert "你好" not in frame

    def test_empty_window_still_renders_snapshot_shape(self) -> None:
        payload = json.loads(wsmod._render_snapshot(SESSION, [], -1, False))
        assert payload == {
            "type": "snapshot", "session_id": SESSION, "events": [],
            "replay_upto": -1, "has_active_run": False,
        }


# —— AC3：重活不在循环线程、发送必须在循环线程 ——


class TestOffload:
    @pytest.mark.asyncio
    async def test_render_runs_off_loop_thread(self) -> None:
        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []

        def render(*args: object) -> str:
            seen.append(threading.current_thread())
            return json.dumps(list(args), default=str)

        ws = _FakeWebSocket()
        await wsmod._send_json_offloaded(ws, render, 1, "two")

        assert len(seen) == 1, "renderer 必须恰好被调用一次"
        assert seen[0] is not loop_thread, (
            f"renderer 仍在事件循环线程 {seen[0].name} 上跑——站点 1 没搬"
        )
        assert ws.sent and ws.sent[0][0] == json.dumps([1, "two"], default=str)

    @pytest.mark.asyncio
    async def test_send_stays_on_loop_thread(self) -> None:
        """票面 Risks：`send_text` **不得**搬线程（并发写同一 socket）。"""
        loop_thread = threading.current_thread()
        ws = _FakeWebSocket()
        await wsmod._send_json_offloaded(ws, lambda: "x")

        assert ws.sent, "必须真的发出去了"
        assert ws.sent[0][1] is loop_thread, (
            f"send_text 跑到 {ws.sent[0][1].name} 上去了——发送语义会被破坏"
        )

    @pytest.mark.asyncio
    async def test_render_failure_is_silently_ignored(self) -> None:
        """与 `_send_json` 同一条静默路径：渲染失败 = 断连，不新增异常层级。"""
        ws = _FakeWebSocket()

        def boom() -> str:
            raise RuntimeError("render failed")

        await wsmod._send_json_offloaded(ws, boom)  # 不得外泄
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_send_failure_is_silently_ignored(self) -> None:
        await wsmod._send_json_offloaded(_BrokenWebSocket(), lambda: "x")  # 不得外泄


# —— 接线守卫（本票唯一可能被静默回退的点）——


class TestWiring:
    def test_snapshot_path_uses_offload_helper(self) -> None:
        """快照路径必须经由下放助手；内联的那段列表推导不得再出现在 handler 里。"""
        source = inspect.getsource(wsmod.handle_websocket)
        assert "_send_json_offloaded(" in source, (
            "快照又回到循环上渲染了——`_send_json_offloaded` 调用没了"
        )
        assert _LEGACY_INLINE not in source, (
            "`_push_snapshot` 里又出现内联的 `[e.to_dict() for e in window]`："
            "那段序列化会重新落回事件循环线程（#275 站点 1 回退）"
        )

    def test_render_snapshot_is_a_plain_sync_function(self) -> None:
        """可下放线程的前提：它不是协程、且体内**一个 await 都没有**。

        用 `GET_AWAITABLE` 操作码判定，而不是在源码里找 "await" 字样——它的 docstring
        里恰好就写着 `await websocket.send_text(...)`（解释为什么要整段一起搬），
        源码文本匹配会把自己绊倒。
        """
        assert not inspect.iscoroutinefunction(wsmod._render_snapshot)
        opnames = {i.opname for i in dis.get_instructions(wsmod._render_snapshot)}
        assert "GET_AWAITABLE" not in opnames
