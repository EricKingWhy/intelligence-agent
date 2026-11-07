"""#1514（并入章节的原 #1514）：等待/活性辅助器**被违反时必须失败**，不能挂死。

`tests/web/` 里两个共享辅助器是这段判据的现场：

- `test_board_broadcaster.py::_read_until_pong` 有 50 **帧**上限，但单次
  `ws.receive_text()` 没有外层**时间**界——读循环真断了时它永久阻塞，用例挂死而不是失败；
- `test_web_ws_relay.py::_recv_until` 有 `asyncio.wait_for`，但超时后 `except
  TimeoutError: pass` 把异常吞了并返回半截帧列表 ⇒ 调用方拿到的是"看起来正常"的
  空/短列表，失败点无法归因（聚合方案簇 C 的原话：**不能把所有超时都称为假绿**，
  要逐调用方确认判据）。

本文件用**替身 ws** 直接钉这两条语义（不启服务器，跑得快且不怕负载）：
被违反时必须在有限时间内抛出、且错误里带得出等待点，而不是返回半截结果。

变异判据（改完必须复跑，见回报）：把 `_receive_text_within` 的 `wait_for` 去掉、
或把 `_recv_until` 的 `partial_ok` 默认改回 True，本文件必须红。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from tests.web.test_board_broadcaster import _drain_blocking, _read_until_pong
from tests.web.test_web_ws_relay import _recv_until


class _FakePortal:
    """starlette `TestClient` 的 portal 替身：把协程跑在自己的循环里。"""

    def call(self, fn, *args):
        return asyncio.run(fn(*args))


class _NeverReceivingSendRx:
    """`_send_rx` 替身：`receive()` 永不返回（模拟读循环已断的死连接）。"""

    async def receive(self):
        await asyncio.sleep(3600)


class _SilentWs:
    """对端永不发帧的 ws 替身（board 辅助器用的那一套接口）。"""

    def __init__(self) -> None:
        self.portal = _FakePortal()
        self._send_rx = _NeverReceivingSendRx()
        self.sent: list[str] = []

    def send_text(self, data: str) -> None:
        self.sent.append(data)

    def _raise_on_close(self, message) -> None:  # pragma: no cover - 不会走到
        raise AssertionError(f"不该收到消息：{message!r}")


def test_read_until_pong_fails_within_a_bound_instead_of_hanging() -> None:
    """marker 永不出现 ⇒ 有界失败，错误里点名等待点（原票验收框 ①）。"""
    ws = _SilentWs()
    started = time.monotonic()
    with pytest.raises(AssertionError) as excinfo:
        _read_until_pong(ws, timeout=0.4)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"辅助器没有时间界：{elapsed:.1f}s 才返回（应 ~0.4s 失败）"
    assert "pong" in str(excinfo.value), f"失败未点名等待点：{excinfo.value}"


def test_drain_blocking_is_bounded_end_to_end_when_no_frame_ever_arrives() -> None:
    """整条 `_drain_blocking` 也必须有界（每轮 receive 有界 ⇒ 总时长有界）。"""
    ws = _SilentWs()
    started = time.monotonic()
    with pytest.raises(AssertionError):
        _drain_blocking(ws, settle=0.0, timeout=0.3)
    elapsed = time.monotonic() - started
    assert elapsed < 20.0, f"多轮 drain 累加成无界等待：{elapsed:.1f}s"


class _RecordingWs:
    """给 `_recv_until` 用的替身：按剧本吐帧，剧本耗尽后静默。

    `receive_text` 与 httpx2 同签名（带 `timeout` 参数），超时抛 `TimeoutError`。
    """

    def __init__(self, frames: list[dict]) -> None:
        self._frames = list(frames)
        self.calls = 0

    async def receive_text(self, timeout: float | None = None) -> str:
        self.calls += 1
        if self._frames:
            return json.dumps(self._frames.pop(0))
        await asyncio.sleep(timeout if timeout is not None else 0.05)
        raise TimeoutError


@pytest.mark.asyncio
async def test_recv_until_raises_when_the_predicate_never_holds() -> None:
    """条件永不成立 ⇒ 抛错并带上等待点与已收帧数，**不**返回半截列表。"""
    ws = _RecordingWs([{"type": "snapshot"}, {"type": "ping"}])
    with pytest.raises(AssertionError) as excinfo:
        await _recv_until(
            ws, lambda fs: any(f.get("type") == "done" for f in fs), timeout=0.3
        )
    message = str(excinfo.value)
    assert "done" in message or "predicate" in message, message
    assert "2" in message, f"应报出已收帧数（2），实际：{message}"


@pytest.mark.asyncio
async def test_recv_until_keeps_the_partial_frames_contract_when_opted_in() -> None:
    """显式 opt-in 的"短窗口、预期为空"用法照旧返回已收帧（既有断言不被削弱）。"""
    ws = _RecordingWs([{"type": "snapshot"}])
    frames = await _recv_until(
        ws,
        lambda fs: any(f.get("type") == "event" for f in fs),
        timeout=0.2,
        partial_ok=True,
    )
    assert frames == [{"type": "snapshot"}]


@pytest.mark.asyncio
async def test_recv_until_returns_as_soon_as_the_predicate_holds() -> None:
    """正常路径不受影响：一次命中即返回（不把超时当常规路径）。"""
    ws = _RecordingWs([{"type": "snapshot"}])
    frames = await _recv_until(ws, lambda fs: bool(fs), timeout=2.0)
    assert frames == [{"type": "snapshot"}]
    assert ws.calls == 1, f"命中后不应继续收帧（实收 {ws.calls} 次）"


@pytest.mark.asyncio
async def test_recv_until_bounds_a_single_receive_not_just_the_loop() -> None:
    """外层 deadline 必须真正包住**单次** receive（原票验收框 ①的后半句）。"""
    seen_timeouts: list[float | None] = []

    class _SlowWs:
        async def receive_text(self, timeout: float | None = None) -> str:
            seen_timeouts.append(timeout)
            await asyncio.sleep(3600)

    started = time.monotonic()
    with pytest.raises(AssertionError):
        await _recv_until(_SlowWs(), lambda fs: bool(fs), timeout=0.3)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"单次 receive 无界：{elapsed:.1f}s"
    assert seen_timeouts and seen_timeouts[0] is not None, (
        "单次 receive 必须自带 deadline（不能只靠外层 wait_for 取消）"
    )
    assert seen_timeouts[0] <= 0.3 + 1e-6, seen_timeouts
