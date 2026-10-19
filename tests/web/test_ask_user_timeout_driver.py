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

import logging
from datetime import UTC, datetime, timedelta

import pytest

from agent_harness.session.ask_user_timeout import ExpireOutcome
from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_PAUSED,
    RUN_STARTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.web.ask_user_timeout_driver import AskUserTimeoutDriver
from tests.fake_clock import FakeClock


def _events(
    *, timeout_seconds: int | None = None, timeout_minutes: int | None = None,
) -> list[SessionEvent]:
    payload: dict[str, object] = {"request_id": "req-1", "kind": "ask_user"}
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    if timeout_minutes is not None:
        payload["timeout_minutes"] = timeout_minutes
    return [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1, data=payload,
        ),
        SessionEvent(
            type=RUN_PAUSED, run_id="r1", seq=2,
            data={
                "reason": "user_input", "trigger_dimension": "user_input",
                "input_request_id": "req-1",
            },
        ),
    ]


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
async def test_plan_approval_never_arms_the_timeout_table():
    """#1356 AC（票面 §3.2）：`plan_approval` 待审批时超时表不安装。

    计划审批是**用户显式裁决**，没有可机械判定的默认动作——到点自动恢复会
    直接合成一个"批准/拒绝"，把「提交 → 超时自动恢复 → 再提交」变成循环。
    这条钉住 kind 过滤：将来把驱动泛化到所有 `USER_INPUT_REQUESTED` 时立刻红。
    """
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=1.0)
    driver.note_event("s1", _request_event(kind="plan_approval"))
    assert driver.is_armed("s1") is False
    clock.advance(3600)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_rearming_the_same_request_preserves_elapsed():
    """同一 request 重复装配（事件重放 / 启动重建）保留已累计，不重头计时。"""
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
async def test_snapshot_without_the_pending_request_does_not_disarm():
    """#1320 复审 P2：读盘快照可能落后于落盘事件——"没看到"不是"已了结"。

    启动重建与事件观察者可能并发：快照里没有待回答请求，不得撤掉观察者刚装上的
    表、更不得清零已累计时长。撤销只由答复 / run 终态事件驱动。
    """
    clock = FakeClock()
    driver, fired = _driver(clock)
    driver.note_session_state("s1", _events())
    clock.advance(30)

    stale = [SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={})]
    driver.note_session_state("s1", stale)
    assert driver.is_armed("s1") is True

    clock.advance(30)  # 30 + 30 = 60：已累计时长未被重置
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_restore_keeps_the_deadline_from_the_persisted_request_time():
    """#1320：启动重建按请求事件时间接上已累计时长——停机期间到期的提问立即恢复。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    asked_at = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
    driver.note_session_state("s1", [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1, time=asked_at,
            data={
                "request_id": "req-1", "kind": "ask_user",
                "timeout_seconds": 60,
            },
        ),
        # #1337：装配还要求当前确实暂停在该请求上（旧流缺这条 ⇒ 不装表）。
        SessionEvent(
            type=RUN_PAUSED, run_id="r1", seq=2,
            data={"reason": "user_input", "trigger_dimension": "user_input",
                  "input_request_id": "req-1"},
        ),
    ])

    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_restore_does_not_arm_a_request_without_a_pause_fact():
    """#1337 B 段③：只有 `user/input-requested`、没有 `run/paused` 的旧事件流不装表。

    崩溃现场可能留下「请求已落盘、run 既没暂停也没终态」的事件流。这里请求是 1
    小时前的（≫ 2×60s）：即使计时器当初正常装配也早该到期，它必然是无人等待的
    旧流。按墙钟补齐的计时器会在首轮 ticker 就判到期，把一个早已无人等待的提问
    当"刚刚超时"恢复掉（污染事件流）。新近请求（< 2×timeout）的同形状走合成暂停
    回退（见 `test_restore_arms_recent_request_in_crash_window_via_synthesized_pause`）。
    """
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state("s1", [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1,
            time=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            data={"request_id": "req-1", "kind": "ask_user",
                  "timeout_seconds": 60},
        ),
    ])

    assert driver.is_armed("s1") is False
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_restore_disarms_when_the_pause_belongs_to_another_request():
    """暂停在别的 request 上（事件流错位）⇒ 不为旧请求装表，并撤掉残留表。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_event("s1", _request_event("req-1"))  # 观察者先装了一张
    assert driver.is_armed("s1") is True

    driver.note_session_state("s1", [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1,
            data={"request_id": "req-1", "kind": "ask_user"},
        ),
        SessionEvent(
            type=RUN_PAUSED, run_id="r1", seq=2,
            data={"reason": "user_input", "trigger_dimension": "user_input",
                  "input_request_id": "req-2"},
        ),
    ])

    assert driver.is_armed("s1") is False
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_restore_arms_a_genuinely_paused_request():
    """正控：请求 + `run/paused(reason=user_input, 同一 request)` ⇒ 照常装配。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state("s1", _events())

    assert driver.is_armed("s1") is True
    clock.advance(60)
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_answer_event_disarms_the_matching_request():
    """事件观察者路径：带 `input_request_id` 的答复撤销对应计时器。"""
    clock = FakeClock()
    driver, fired = _driver(clock)
    driver.note_event("s1", _request_event("req-1"))
    assert driver.is_armed("s1") is True  # 装配前置：否则下面的撤销断言无鉴别力

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
    assert driver.is_armed("s1") is True  # 装配前置：否则下面的撤销断言无鉴别力

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
async def test_expiry_log_reports_the_payload_timeout(caplog):
    """到点日志必须写该请求的生效档，而不是进程启动配置（AC1 取证用）。"""
    clock = FakeClock()
    driver, _fired = _driver(clock, timeout=600.0)
    driver.note_session_state("s1", _events(timeout_seconds=60))

    with caplog.at_level(
        logging.INFO, logger="agent_harness.web.ask_user_timeout_driver",
    ):
        clock.advance(60)
        assert await driver.check_expiries() == ["s1"]

    assert "档位=60s" in caplog.text


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


# ── #1337 B：启动重建三段守卫 ──


def _crash_window_events(
    *, request_id: str = "req-1", asked_seconds_ago: float = 30.0,
    timeout_seconds: int = 60,
) -> list[SessionEvent]:
    """crash-window 形状：`user/input-requested` 已落盘，无 `run/paused`、无终态。"""
    return [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1,
            time=(
                datetime.now(UTC) - timedelta(seconds=asked_seconds_ago)
            ).isoformat(),
            data={
                "request_id": request_id, "kind": "ask_user",
                "timeout_seconds": timeout_seconds,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_restore_arms_recent_request_in_crash_window_via_synthesized_pause():
    """#1337 B 段②：进程死在 `user/input-requested` 与 `run/paused` 两次 append
    之间——请求是新近的（30s < 2×60s），用户确实在等。启动重建复用在线路径
    `resume_ask_user_timeout` 的同源回退（`_pending_user_input_pause`）合成暂停，
    装表并保留 #1320 durable 语义；到点正常触发。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state("s1", _crash_window_events())

    assert driver.is_armed("s1") is True
    clock.advance(29)  # 已累计 30 + 29 = 59 < 60
    assert await driver.check_expiries() == []
    clock.advance(1)  # 60 ⇒ 到点
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_restore_disarms_three_day_old_request_without_pause_fact():
    """#1337 B 段③：3 天前的请求、无暂停事实 ⇒ 旧流，首轮 ticker 零 `on_expire`。

    与 1 小时版（`test_restore_does_not_arm_a_request_without_a_pause_fact`）并存：
    这里 pin 住"远超 2×timeout"的旧流侧。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state(
        "s1", _crash_window_events(asked_seconds_ago=3 * 24 * 3600),
    )

    assert driver.is_armed("s1") is False
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_restore_arms_request_just_inside_two_timeouts():
    """边界内侧：elapsed = 2×timeout − 5s（+毫秒级墙钟漂移）⇒ 仍走回退段装表。

    5s 裕量远大于用例执行耗时，断言无抖动。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state("s1", _crash_window_events(asked_seconds_ago=115.0))

    assert driver.is_armed("s1") is True
    clock.advance(10_000)  # 已累计 115s ≥ 60s ⇒ 首轮即到点
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_restore_disarms_request_at_exactly_two_timeouts():
    """边界平局 pin 住 disarm 侧：elapsed ≥ 2×timeout（此处取等，墙钟只会再往大漂）
    ⇒ 旧流。理由见实现注释：该时刻计时器已到期满一个档位，"用户还在等"的解释
    已不可信。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state("s1", _crash_window_events(asked_seconds_ago=120.0))

    assert driver.is_armed("s1") is False
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_restore_arms_recent_request_with_legacy_pause_missing_input_request_id():
    """P2：旧格式 `run/paused`（缺 `input_request_id`，`run_budget.py:1093-1098`
    历史格式）归属无法判定——不直接按不匹配 disarm。新近请求按保守处理走回退段
    装表：误 arm 到点时会被在线守卫重读事件流判 RESOLVED 收口，而丢掉的计时器
    无法挽回。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state("s1", [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1,
            time=(datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
            data={"request_id": "req-1", "kind": "ask_user",
                  "timeout_seconds": 60},
        ),
        # 旧格式：有 reason、无 input_request_id。
        SessionEvent(
            type=RUN_PAUSED, run_id="r1", seq=2,
            data={"reason": "user_input", "trigger_dimension": "user_input"},
        ),
    ])

    assert driver.is_armed("s1") is True
    clock.advance(30)  # 已累计 30 + 30 = 60 ⇒ 到点
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]


@pytest.mark.asyncio
async def test_restore_disarms_legacy_pause_for_old_request():
    """P2 老请求侧：旧格式暂停 + 请求已老（3 天）⇒ disarm（新近才值得保守装表）。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state("s1", [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1,
            time=(datetime.now(UTC) - timedelta(days=3)).isoformat(),
            data={"request_id": "req-1", "kind": "ask_user",
                  "timeout_seconds": 60},
        ),
        SessionEvent(
            type=RUN_PAUSED, run_id="r1", seq=2,
            data={"reason": "user_input", "trigger_dimension": "user_input"},
        ),
    ])

    assert driver.is_armed("s1") is False
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_restore_disarms_when_pause_names_an_answered_older_request():
    """P2：暂停归属错位——贴近生产的形状。

    req-A 已答复（`user/message` 带 `input_request_id`，正是生产答复落盘的形状），
    run 仍暂停在它上面（答复与 `run/resumed` 之间的崩溃窗口）；req-B 是最新未答复
    请求但尚无暂停事实。暂停事实明确属于另一个请求（新格式、有 id、可判定）⇒
    disarm，不借"新近"装表。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    recent = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    driver.note_session_state("s1", [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1, time=old,
            data={"request_id": "req-A", "kind": "ask_user",
                  "timeout_seconds": 60},
        ),
        SessionEvent(
            type=RUN_PAUSED, run_id="r1", seq=2,
            data={"reason": "user_input", "trigger_dimension": "user_input",
                  "input_request_id": "req-A"},
        ),
        SessionEvent(
            type=USER_MESSAGE, run_id="r1", seq=3,
            data={"input_request_id": "req-A", "text": "好的"},
        ),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=4, time=recent,
            data={"request_id": "req-B", "kind": "ask_user",
                  "timeout_seconds": 60},
        ),
    ])

    assert driver.is_armed("s1") is False
    clock.advance(10_000)
    assert await driver.check_expiries() == []
    assert fired == []


@pytest.mark.asyncio
async def test_restore_accepts_an_event_iterator():
    """P3：`events` 以迭代器传入时，入口一次物化——`latest_pending_input_request`
    内部的 `list(events)` 不能把调用方的迭代器消费掉一半。"""
    clock = FakeClock()
    driver, fired = _driver(clock, timeout=60.0)
    driver.note_session_state("s1", iter(_crash_window_events()))

    assert driver.is_armed("s1") is True
    clock.advance(30)  # 已累计 30 + 30 = 60 ⇒ 到点
    assert await driver.check_expiries() == ["s1"]
    assert fired == [("s1", "req-1")]
