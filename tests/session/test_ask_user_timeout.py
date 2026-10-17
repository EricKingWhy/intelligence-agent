"""#1204：`ask_user` 超时闸的确定性单测（时钟注入，不实等）。

口径：计时器只在**至少一个前端连接存在**时启动 / 继续；无前端连接时不启动。
三档（60 / 300 / 600 秒）各验一例；注入消息正文逐字断言。
"""

from __future__ import annotations

import pytest

from agent_harness.session.ask_user_timeout import (
    AskUserTimeoutTracker,
    ask_user_timeout_message,
    ask_user_timeout_minutes,
    ask_user_timeout_minutes_from_payload,
    ask_user_timeout_seconds_from_payload,
    pending_ask_user_request_id,
)
from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    SessionEvent,
)
from tests.fake_clock import FakeClock


def test_timer_does_not_start_without_a_frontend_connection():
    clock = FakeClock()
    tracker = AskUserTimeoutTracker(60, clock=clock)

    clock.advance(10_000)

    assert tracker.running is False
    assert tracker.elapsed_seconds() == 0.0
    assert tracker.expired() is False


def test_timer_only_accumulates_while_connected():
    clock = FakeClock()
    tracker = AskUserTimeoutTracker(300, clock=clock)

    tracker.set_connected(True)
    clock.advance(30)
    tracker.set_connected(False)
    clock.advance(10_000)  # 断开期间不计时

    assert tracker.elapsed_seconds() == 30
    assert tracker.expired() is False


def test_reconnect_resumes_from_where_it_froze():
    clock = FakeClock()
    tracker = AskUserTimeoutTracker(100, clock=clock)

    tracker.set_connected(True)
    clock.advance(60)
    tracker.set_connected(False)
    clock.advance(1_000)
    tracker.set_connected(True)
    clock.advance(39)

    assert tracker.expired() is False
    clock.advance(1)
    assert tracker.expired() is True


@pytest.mark.parametrize("timeout", [60, 300, 600])
def test_expires_at_the_configured_tier(timeout: int):
    clock = FakeClock()
    tracker = AskUserTimeoutTracker(timeout, clock=clock)

    tracker.set_connected(True)
    clock.advance(timeout - 1)
    assert tracker.expired() is False
    clock.advance(1)
    assert tracker.expired() is True


@pytest.mark.parametrize(
    ("timeout", "minutes"),
    [(60, 1), (300, 5), (600, 10)],
)
def test_timeout_message_renders_minutes(timeout: int, minutes: int):
    assert ask_user_timeout_message(timeout) == (
        f"用户在 {minutes} 分钟内未回答，可能不在，请按你的判断继续"
    )


@pytest.mark.parametrize("timeout", [0, -1, -60, 59, 90, 119])
def test_timeout_message_clamps_to_at_least_one_minute(timeout: int):
    """#1284 P2-3：钳位语义（至少 1 分钟、向下取整）在非法/小数档输入下也成立。"""
    assert ask_user_timeout_minutes(timeout) == 1
    assert ask_user_timeout_message(timeout) == (
        "用户在 1 分钟内未回答，可能不在，请按你的判断继续"
    )


# ── #1284 P2-2：到期链路读请求载荷快照（缺失才回落配置）───────────────


def test_payload_snapshot_wins_over_the_live_config():
    data = {"timeout_seconds": 60, "timeout_minutes": 1}
    assert ask_user_timeout_seconds_from_payload(data, fallback=600) == 60
    assert ask_user_timeout_minutes_from_payload(data, fallback_seconds=600) == 1


def test_legacy_payload_without_snapshot_falls_back_to_live_config():
    """#1224 之前落盘的旧请求没有 `timeout_seconds` ⇒ 只能回落当前配置。"""
    assert ask_user_timeout_seconds_from_payload({}, fallback=300) == 300
    assert ask_user_timeout_minutes_from_payload({}, fallback_seconds=60) == 1


def test_legacy_payload_with_seconds_only_derives_minutes():
    """#1224 与 #1284 之间落盘的请求只有秒 ⇒ 由同一 Python 公式换算。"""
    assert ask_user_timeout_minutes_from_payload(
        {"timeout_seconds": 600}, fallback_seconds=60,
    ) == 10


@pytest.mark.parametrize("bad", [0, -5, 1.5, "600", True, None])
def test_invalid_snapshot_values_fall_back(bad: object):
    assert ask_user_timeout_seconds_from_payload(
        {"timeout_seconds": bad}, fallback=300,
    ) == 300


# ── #1220：待回答守卫的纯判据（装配 / 复查共用）─────────────────────────


def _events(*, answered: bool = False, terminal: str | None = None,
            kind: str = "ask_user") -> list[SessionEvent]:
    events = [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(
            type=USER_INPUT_REQUESTED, run_id="r1", seq=1,
            data={"request_id": "req-1", "kind": kind},
        ),
    ]
    if answered:
        events.append(SessionEvent(
            type=USER_MESSAGE, run_id="r1", seq=2,
            data={"input_request_id": "req-1", "input_request_answer": {"request_id": "req-1"}},
        ))
    if terminal is not None:
        events.append(SessionEvent(type=terminal, run_id="r1", seq=2, data={}))
    return events


def test_pending_request_id_is_none_without_any_run():
    assert pending_ask_user_request_id([]) is None


def test_pending_request_id_returns_unanswered_ask_user():
    assert pending_ask_user_request_id(_events()) == "req-1"


def test_pending_request_id_none_when_answered_or_skipped():
    # 已答复（含整卡跳过 / 超时形态都带 input_request_id）⇒ 不再触发超时恢复。
    assert pending_ask_user_request_id(_events(answered=True)) is None


@pytest.mark.parametrize("terminal", [RUN_COMPLETED, RUN_FAILED])
def test_pending_request_id_none_when_run_terminal(terminal: str):
    assert pending_ask_user_request_id(_events(terminal=terminal)) is None


def test_pending_request_id_ignores_other_kinds():
    # 窄场景 protected_fact_conflict 不属于 ask_user 超时闸。
    assert pending_ask_user_request_id(_events(kind="protected_fact_conflict")) is None


# ── #1220 复审 B1/N1：多请求流的统一口径（跳过已答复者再往前找）─────────


def _multi_request(second_answered: bool) -> list[SessionEvent]:
    events = [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(type=USER_MESSAGE, run_id="r1", seq=1,
                     data={"input_request_id": "req-1"}),
        SessionEvent(type=USER_INPUT_REQUESTED, run_id="r1", seq=2,
                     data={"request_id": "req-2", "kind": "ask_user"}),
    ]
    if second_answered:
        events.append(SessionEvent(type=USER_MESSAGE, run_id="r1", seq=3,
                                   data={"input_request_id": "req-2"}))
    return events


def test_pending_request_id_returns_latest_unanswered_in_multi_request_run():
    assert pending_ask_user_request_id(_multi_request(second_answered=False)) == "req-2"


def test_pending_request_id_skips_answered_searches_earlier():
    """最新一条已答复、更早一条未答复 ⇒ 返回更早那条（与 409 闸门统一的"跳过已答复"口径）。

    良构事件流里不可达（产生新 `USER_INPUT_REQUESTED` 必先给旧请求写答复）；此处钉住
    统一后的判据，避免两处口径再漂（#1220 复审 B1/N1）。
    """
    events = [
        SessionEvent(type=RUN_STARTED, run_id="r1", seq=0, data={}),
        SessionEvent(type=USER_INPUT_REQUESTED, run_id="r1", seq=1,
                     data={"request_id": "req-1", "kind": "ask_user"}),
        SessionEvent(type=USER_INPUT_REQUESTED, run_id="r1", seq=2,
                     data={"request_id": "req-2", "kind": "ask_user"}),
        SessionEvent(type=USER_MESSAGE, run_id="r1", seq=3,
                     data={"input_request_id": "req-2"}),
    ]
    assert pending_ask_user_request_id(events) == "req-1"
