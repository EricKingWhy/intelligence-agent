"""#1204：`ask_user` 超时闸的确定性单测（时钟注入，不实等）。

口径：计时器只在**至少一个前端连接存在**时启动 / 继续；无前端连接时不启动。
三档（60 / 300 / 600 秒）各验一例；注入消息正文逐字断言。
"""

from __future__ import annotations

import pytest

from agent_harness.session.ask_user_timeout import (
    AskUserTimeoutTracker,
    ask_user_timeout_message,
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


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_timer_does_not_start_without_a_frontend_connection():
    clock = _FakeClock()
    tracker = AskUserTimeoutTracker(60, clock=clock)

    clock.advance(10_000)

    assert tracker.running is False
    assert tracker.elapsed_seconds() == 0.0
    assert tracker.expired() is False


def test_timer_only_accumulates_while_connected():
    clock = _FakeClock()
    tracker = AskUserTimeoutTracker(300, clock=clock)

    tracker.set_connected(True)
    clock.advance(30)
    tracker.set_connected(False)
    clock.advance(10_000)  # 断开期间不计时

    assert tracker.elapsed_seconds() == 30
    assert tracker.expired() is False


def test_reconnect_resumes_from_where_it_froze():
    clock = _FakeClock()
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
    clock = _FakeClock()
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
