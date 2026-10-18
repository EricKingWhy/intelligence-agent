"""#1320：`AskUserTimeoutDriver` 的确定性单测（挂钟注入，不实等）。

驱动是**服务端 durable** 计时器：装配只看事件事实（`user/input-requested` 落盘），
推进只看挂钟，WS 连接数不参与任何判定（#1320 D1A 推翻 #1220 复审 A1/A3）。
这里测驱动的**语义守卫**：

- 零连接 ⇒ 装表即起表，到点照常触发；
- 同一请求重复装配 ⇒ 保留已累计时长；
- 已答复 / run 终态 ⇒ 撤销，不再触发；
- 到点 ⇒ 恰好触发一次恢复（同一 request 不重复）；
- 可重试失败 ⇒ 下个窗口重试；
- `await on_expire` 期间装入新请求 ⇒ 收尾不得误拆（A-F1）。
"""

from __future__ import annotations

import pytest

from agent_harness.session.ask_user_timeout import ExpireOutcome
from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_STARTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.web.ask_user_timeout_driver import AskUserTimeoutDriver
from tests.fake_clock import FakeClock


def _events(
    *, answered: bool = False, timeout_seconds: int | None = None,
    timeout_minutes: int | None = None,
) -> list[SessionEvent]:
    payload: dict[str, object] = {"request_id": "req-1", "kind": "ask_user"}
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    if timeout_minutes is not None:
        payload["timeout_minutes"] = timeout_minutes
    events = [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1, data=payload,
        ),
    ]
    if answered:
        events.append(SessionEvent(
            type=USER_MESSAGE, run_id="r1", seq=2,
            data={"input_request_id": "req-1"},
        ))
    return events


def _request_event(
    request_id: str = "req-1", *, kind: str = "ask_user",
    timeout_seconds: int | None = None,
) -> SessionEvent:
    payload: dict[str, object] = {"request_id": request_id, "kind": kind}
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    return SessionEvent(
        type=USER_INPUT_REQUESTED, run_id="r1", seq=1, data=payload,
    )


def _driver(clock, timeout: float = 60.0, fired: list | None = None):
    fired = fired if fired is not None else []

    async def on_expire(session_id: str, request_id: str) -> ExpireOutcome:
        fired.append((session_id, request_id))
        return ExpireOutcome.RESUMED

    return AskUserTimeoutDriver(
        timeout_seconds=timeout, on_expire=on_expire, clock=clock,
    ), fired


@pytest.mark.asyncio
async def test_zero_connection_arms_and_expires_once():
    """#1320 场景 A/B/C 的共同形状：没有任何 WS 连接也照常到点、恰好触发一次。"""
    clock = FakeClock()
    driver, fired = _driver(clock)
    driver.note_session_state("s1", _events())
    assert driver.is_armed("s1") is True

    clock.advance(59)
    assert await driver.check_expiries() == []
    clock.advance(1)
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]
    # 已收口：再 tick 不再触发。
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_note_event_arms_without_any_connection():
    """生产主路径：`user/input-requested` 事件落盘即装表（零连接）。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_event("s1", _request_event(timeout_seconds=60))

    assert driver.is_armed("s1") is True
    clock.advance(60)
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_note_event_ignores_other_input_kinds():
    """超时闸只负责 `ask_user`；其他用户输入 kind 不装表。"""
    clock = FakeClock()
    driver, _fired = _driver(clock)
    driver.note_event("s1", _request_event(kind="constraint_resolution"))
    assert driver.is_armed("s1") is False


@pytest.mark.asyncio
async def test_rearming_the_same_request_preserves_elapsed():
    """同一 request 重复装配（订阅补读 / 事件重放）保留已累计，不重头计时。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=100.0)
    driver.note_session_state("s1", _events())
    clock.advance(60)
    driver.note_session_state("s1", _events())
    clock.advance(39)
    assert await driver.check_expiries() == []  # 60+39=99 < 100
    clock.advance(1)
    assert await driver.check_expiries() == ["s1"]  # 100 ⇒ 到点
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_answered_request_is_disarmed_and_never_fires():
    clock = FakeClock()
    driver, fired = _driver(clock)
    driver.note_session_state("s1", _events())

    # 用户答复落盘后重读：不再有待回答 ⇒ 撤销。
    driver.note_session_state("s1", _events(answered=True))
    assert driver.is_armed("s1") is False
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_answer_event_disarms_the_matching_request():
    """事件观察者路径：带 `input_request_id` 的答复撤销对应计时器。"""
    clock = FakeClock()
    driver, fired = _driver(clock)
    driver.note_event("s1", _request_event("req-1"))

    driver.note_event("s1", SessionEvent(
        type=USER_MESSAGE, run_id="r1", seq=2,
        data={"input_request_id": "req-1"},
    ))
    assert driver.is_armed("s1") is False
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_terminal_run_event_disarms_the_timer():
    """run 已终结 ⇒ 提问不可能再被回答，撤销计时（不触发恢复）。"""
    clock = FakeClock()
    driver, fired = _driver(clock)
    driver.note_event("s1", _request_event("req-1"))

    driver.note_event("s1", SessionEvent(
        type=RUN_COMPLETED, run_id="r1", seq=2, data={},
    ))
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
    driver.note_session_state("s1", _events())
    clock.advance(60)
    assert await driver.check_expiries() == []
    # 收口后不再重复打扰恢复出口（避免每 tick 空转）。
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert calls == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_retry_outcome_keeps_timer_and_retries_next_window():
    """`RETRY`（可重试失败）⇒ 重置基线保留计时，下个超时窗口再试（不永久丢失）。"""
    clock = FakeClock()
    outcomes = [ExpireOutcome.RETRY, ExpireOutcome.RESUMED]
    calls: list = []

    async def on_expire(session_id: str, request_id: str) -> ExpireOutcome:
        calls.append((session_id, request_id))
        return outcomes.pop(0)

    driver = AskUserTimeoutDriver(
        timeout_seconds=60.0, on_expire=on_expire, clock=clock,
    )
    driver.note_session_state("s1", _events())

    clock.advance(60)
    assert await driver.check_expiries() == []  # 第一次 RETRY：未发起恢复
    assert driver.is_armed("s1") is True  # 保留计时（重置基线）
    clock.advance(60)
    assert await driver.check_expiries() == ["s1"]  # 下个窗口重试成功
    assert calls == [("s1", "req-1"), ("s1", "req-1")]


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
    driver.note_session_state("s1", _events())
    clock.advance(60)

    fired = await driver.check_expiries()
    assert driver.is_armed("s1") is True  # 新请求的计时器未被连坐拆掉
    assert driver._armed_request.get("s1") == "req-2"
    assert ("s1" in fired) is (outcome is ExpireOutcome.RESUMED)


# ── #1284 P2-2：到期计时读请求载荷快照（配置改动 + 重启后旧请求仍按旧档）──


@pytest.mark.asyncio
async def test_request_snapshot_timeout_wins_over_the_driver_config():
    """旧请求快照 60s，重启后配置 600s ⇒ 仍在 60s 到点（不是 600s）。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=600.0)
    driver.note_session_state("s1", _events(timeout_seconds=60))

    clock.advance(59)
    assert await driver.check_expiries() == []
    clock.advance(1)
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_legacy_request_without_snapshot_uses_the_driver_config():
    """#1224 之前的旧请求没有快照 ⇒ 才回落到当前配置。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state("s1", _events())

    clock.advance(60)
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_minutes_only_snapshot_arms_from_the_minute_value():
    """只带 `timeout_minutes` 的手写/更旧载荷 ⇒ 计时按分钟折秒，不读启动配置。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=600.0)
    driver.note_session_state("s1", _events(timeout_minutes=5))

    clock.advance(299)
    assert await driver.check_expiries() == []
    clock.advance(1)
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_retry_rearms_with_the_request_snapshot_timeout():
    """RETRY 重装计时器必须沿用请求快照，不得掉回启动配置（60 vs 600）。"""
    clock = FakeClock()
    outcomes = [ExpireOutcome.RETRY, ExpireOutcome.RESUMED]

    async def on_expire(session_id: str, request_id: str) -> ExpireOutcome:
        return outcomes.pop(0)

    driver = AskUserTimeoutDriver(
        timeout_seconds=600.0, on_expire=on_expire, clock=clock,
    )
    driver.note_session_state("s1", _events(timeout_seconds=60))

    clock.advance(60)
    assert await driver.check_expiries() == []  # RETRY：重装快照 60s
    clock.advance(59)
    assert await driver.check_expiries() == []  # 59 < 60，说明不是掉回 600
    clock.advance(1)
    assert await driver.check_expiries() == ["s1"]
