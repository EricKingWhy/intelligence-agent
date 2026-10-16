"""#1220：`AskUserTimeoutDriver` 的确定性单测（挂钟注入，不实等）。

驱动把"WS 连接级订阅计数"接到 `AskUserTimeoutTracker`，并在到点经注入的 `on_expire`
触发一次恢复。这里只测驱动的**语义守卫**（与 tracker 层互补）：

- 无连接在场 ⇒ 不启动、永不到点；
- 连接进出 ⇒ 起表 / 冻结；断线重续**累计不清零**；
- 已答复 / 已整卡跳过 ⇒ 撤销，不再触发；
- 到点 ⇒ 恰好触发一次恢复（同一 request 不重复）。
"""

from __future__ import annotations

import pytest

from agent_harness.session.event import (
    RUN_STARTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.web.ask_user_timeout_driver import AskUserTimeoutDriver


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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


def _driver(clock: _FakeClock, timeout: float = 60.0, fired: list | None = None):
    fired = fired if fired is not None else []

    async def on_expire(session_id: str, request_id: str) -> bool:
        fired.append((session_id, request_id))
        return True

    return AskUserTimeoutDriver(
        timeout_seconds=timeout, on_expire=on_expire, clock=clock,
    ), fired


@pytest.mark.asyncio
async def test_no_connection_never_starts_and_never_expires():
    clock = _FakeClock()
    driver, fired = _driver(clock)
    # 有提问但**没有**前端连接：装配也不起表。
    driver.note_session_state("s1", _events())

    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_connection_enter_starts_and_expires_once():
    clock = _FakeClock()
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
    clock = _FakeClock()
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
    clock = _FakeClock()
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
async def test_on_expire_false_does_not_report_fired():
    clock = _FakeClock()

    async def on_expire(session_id: str, request_id: str) -> bool:
        return False  # 复查发现已不再暂停 / 已答复

    driver = AskUserTimeoutDriver(
        timeout_seconds=60.0, on_expire=on_expire, clock=clock,
    )
    driver.connection_subscribed("s1")
    driver.note_session_state("s1", _events())
    clock.advance(60)
    assert await driver.check_expiries() == []
