"""#1220：`AskUserTimeoutDriver` 的确定性单测（挂钟注入，不实等）。

驱动把"WS 连接级订阅计数"接到 `AskUserTimeoutTracker`，并在到点经注入的 `on_expire`
触发一次恢复。这里只测驱动的**语义守卫**（与 tracker 层互补）：

- 无连接在场 ⇒ 不启动、永不到点；
- 连接进出 ⇒ 起表 / 冻结；断线重续**累计不清零**；
- 已答复 / 已整卡跳过 ⇒ 撤销，不再触发；
- 到点 ⇒ 恰好触发一次恢复（同一 request 不重复）；
- 到点但**此刻无连接** ⇒ 不触发（#1220 复审 A3）；可重试失败 ⇒ 下个窗口重试（A2）；
  冻结条目有上限（A1）。
"""

from __future__ import annotations

import pytest

from agent_harness.session.ask_user_timeout import ExpireOutcome
from agent_harness.session.event import (
    RUN_STARTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.web.ask_user_timeout_driver import (
    _MAX_FROZEN_TRACKERS,
    AskUserTimeoutDriver,
)
from tests.fake_clock import FakeClock


def _events(*, answered: bool = False) -> list[SessionEvent]:
    events = [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1,
            data={"request_id": "req-1", "kind": "ask_user"},
        ),
    ]
    if answered:
        events.append(SessionEvent(
            type=USER_MESSAGE, run_id="r1", seq=2,
            data={"input_request_id": "req-1"},
        ))
    return events


def _driver(clock, timeout: float = 60.0, fired: list | None = None):
    fired = fired if fired is not None else []

    async def on_expire(session_id: str, request_id: str) -> ExpireOutcome:
        fired.append((session_id, request_id))
        return ExpireOutcome.RESUMED

    return AskUserTimeoutDriver(
        timeout_seconds=timeout, on_expire=on_expire, clock=clock,
    ), fired


@pytest.mark.asyncio
async def test_no_connection_never_starts_and_never_expires():
    clock = FakeClock()
    driver, fired = _driver(clock)
    # 有提问但**没有**前端连接：装配也不起表。
    driver.note_session_state("s1", _events())

    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_connection_enter_starts_and_expires_once():
    clock = FakeClock()
    driver, fired = _driver(clock)
    driver.connection_subscribed("s1")
    driver.note_session_state("s1", _events())

    clock.advance(60)
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]
    # 已收口：再 tick 不再触发。
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_disconnect_then_reconnect_accumulates_without_reset():
    clock = FakeClock()
    driver, _fired = _driver(clock, timeout=100.0)
    driver.note_session_state("s1", _events())
    driver.connection_subscribed("s1")
    clock.advance(60)
    driver.connection_closed("s1")
    clock.advance(1_000)  # 断开期间不计时
    driver.connection_subscribed("s1")
    driver.note_session_state("s1", _events())  # 重连重读，仍待回答 ⇒ 保留已累计

    clock.advance(39)
    assert await driver.check_expiries() == []  # 60+39=99 < 100
    clock.advance(1)
    assert await driver.check_expiries() == ["s1"]  # 100 ⇒ 到点


@pytest.mark.asyncio
async def test_answered_request_is_disarmed_and_never_fires():
    clock = FakeClock()
    driver, fired = _driver(clock)
    driver.connection_subscribed("s1")
    driver.note_session_state("s1", _events())

    # 用户答复落盘后重读：不再有待回答 ⇒ 撤销。
    driver.note_session_state("s1", _events(answered=True))
    assert driver.is_armed("s1") is False
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_resolved_outcome_is_disarmed_and_does_not_report_fired():
    """`on_expire` 判 `RESOLVED`（已答复/已跳过）⇒ 收口、不计入 fired。"""
    clock = FakeClock()
    calls: list = []

    async def on_expire(session_id: str, request_id: str) -> ExpireOutcome:
        calls.append((session_id, request_id))
        return ExpireOutcome.RESOLVED

    driver = AskUserTimeoutDriver(
        timeout_seconds=60.0, on_expire=on_expire, clock=clock,
    )
    driver.connection_subscribed("s1")
    driver.note_session_state("s1", _events())
    clock.advance(60)
    assert await driver.check_expiries() == []
    # 收口后不再重复打扰恢复出口（避免每心跳空转）。
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert calls == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_retry_outcome_keeps_timer_and_retries_next_window():
    """`RETRY`（可重试失败）⇒ 重置基线保留在场，下个超时窗口再试（不永久丢失）。"""
    clock = FakeClock()
    outcomes = [ExpireOutcome.RETRY, ExpireOutcome.RESUMED]
    calls: list = []

    async def on_expire(session_id: str, request_id: str) -> ExpireOutcome:
        calls.append((session_id, request_id))
        return outcomes.pop(0)

    driver = AskUserTimeoutDriver(
        timeout_seconds=60.0, on_expire=on_expire, clock=clock,
    )
    driver.connection_subscribed("s1")
    driver.note_session_state("s1", _events())

    clock.advance(60)
    assert await driver.check_expiries() == []  # 第一次 RETRY：未发起恢复
    assert driver.is_armed("s1") is True  # 保留计时（重置基线）
    clock.advance(60)
    assert await driver.check_expiries() == ["s1"]  # 下个窗口重试成功
    assert calls == [("s1", "req-1"), ("s1", "req-1")]


@pytest.mark.asyncio
async def test_expired_but_disconnected_is_not_fired():
    """到点但**此刻无连接** ⇒ 不触发（#1220 复审 A3），重连后才恢复。"""
    clock = FakeClock()
    driver, fired = _driver(clock)
    driver.connection_subscribed("s1")
    driver.note_session_state("s1", _events())

    clock.advance(60)  # 已到点（累计时确有连接）
    driver.connection_closed("s1")  # 触发前连接离开

    assert await driver.check_expiries() == []  # 无连接 ⇒ 不触发
    assert fired == []
    assert driver.is_armed("s1") is True  # 计时保留

    # 重连（仍待回答）⇒ 已累计超阈值，下个 tick 触发。
    driver.connection_subscribed("s1")
    driver.note_session_state("s1", _events())
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_frozen_trackers_are_capped():
    """被放弃的冻结计时器有上限，防止临时接缝里的无界增长（#1220 复审 A1）。"""
    clock = FakeClock()
    driver, _fired = _driver(clock, timeout=60.0)

    total = _MAX_FROZEN_TRACKERS + 5
    for i in range(total):
        sid = f"s{i}"
        driver.connection_subscribed(sid)
        driver.note_session_state(sid, _events())
        driver.connection_closed(sid)  # 冻结（未到点）

    assert len(driver._trackers) <= _MAX_FROZEN_TRACKERS
    # 最早的被淘汰（重连后重头计时，属可接受降级）。
    assert driver.is_armed("s0") is False
    assert driver.is_armed(f"s{total - 1}") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [ExpireOutcome.RESUMED, ExpireOutcome.RESOLVED, ExpireOutcome.RETRY],
)
async def test_expiry_does_not_clobber_request_armed_during_await(outcome):
    """`await on_expire` 期间同一会话装了**新** request ⇒ 收尾不得误拆它（复审 A-F1）。"""
    clock = FakeClock()
    driver: AskUserTimeoutDriver | None = None

    async def on_expire(session_id: str, request_id: str) -> ExpireOutcome:
        assert driver is not None
        driver.arm(session_id, request_id="req-2")  # 模拟 await 期间的新装臂
        return outcome

    driver = AskUserTimeoutDriver(
        timeout_seconds=60.0, on_expire=on_expire, clock=clock,
    )
    driver.connection_subscribed("s1")
    driver.note_session_state("s1", _events())
    clock.advance(60)

    fired = await driver.check_expiries()
    assert driver.is_armed("s1") is True  # 新请求的计时器未被连坐拆掉
    assert driver._armed_request.get("s1") == "req-2"
    assert ("s1" in fired) is (outcome is ExpireOutcome.RESUMED)


@pytest.mark.asyncio
async def test_arm_without_connection_is_accounted_as_frozen():
    """无连接时装臂的计时器须计入冻结账（否则既不被扫描、也不受上限约束；复审 A-F2）。"""
    clock = FakeClock()
    driver, _fired = _driver(clock)
    driver.arm("s1", request_id="req-1")  # 无连接

    assert driver.is_armed("s1") is True
    assert "s1" in driver._frozen
    clock.advance(10_000)
    assert await driver.check_expiries() == []  # 无连接 ⇒ 不触发
