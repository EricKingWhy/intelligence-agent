"""`#317` T9：五模式 stuck 检测器的单元测试（ADR-0048 D1–D4）。

判据来源：`02 §5.3`（五个模式与各自的首达阈值）、`02 §5.4`（判定只读已落盘事实）。

本文件只喂**事件**给 `StuckDetector`（不跑模型、不跑工具）：检测器的全部输入是
`model/completed` + `tool/call` + `tool/result` 三类 durable 事件，所以"阈值在第几次
触发""重启后计数一样""什么算进展"这三件事都能在这里精确断言。真实 AgentRuntime
循环里的接线（replan 恰好一次、`run/paused(reason=stuck)`）在
`tests/agent/test_stuck_runtime.py` 覆盖。

事件构造刻意**不经** `Session`：这样每条事件的 seq 由用例自己给，重启重放
（`from_events` 只吃前一半）与"游标之后才是新事件"都能直接构造。
"""

from __future__ import annotations

import json

import pytest

from agent_harness.agent.guards import (
    GUARD_EXEMPT_ERROR_CODES,
    STUCK_LEVEL_PAUSED,
    STUCK_LEVEL_REPLAN,
    STUCK_PATTERN_ALTERNATING,
    STUCK_PATTERN_MONOLOGUE,
    STUCK_PATTERN_OBSERVATION,
    STUCK_PATTERN_PROJECT,
    STUCK_PATTERN_TOOL_FAILURE,
    STUCK_THRESHOLDS,
    RepeatedToolFailureGuard,
    StuckDetector,
    action_fingerprint,
    decision_signature,
    external_failure_signal,
    outcome_fingerprint,
    worst_stuck_signal,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    SessionEvent,
)

RUN_ID = "run-stuck"


class _Seq:
    """事件序号分配器（用例自己掌握 seq，重放边界才可控）。"""

    def __init__(self) -> None:
        self._next = 0

    def take(self) -> int:
        value = self._next
        self._next += 1
        return value


def _completed(
    seq: int, *, content: str = "", calls: tuple[tuple[str, dict], ...] = (),
    call_ids: tuple[str, ...] = (), run_id: str = RUN_ID,
) -> SessionEvent:
    """一条 `model/completed`（带 tool_calls 的形状与 runtime 落盘的一致）。

    `tool_calls[].id` **必须**与 `tool/call.tool_call_id` 同值（生产里两侧都取
    AIMessage 里的 `c.id`）：检测器靠它把结果配回决策。id 不同的话决策永远收不了口，
    ③④⑤ 的判定会晚一轮才发生——那是夹具 bug，不是被测行为。
    """
    raw = [
        {
            "id": call_ids[index] if index < len(call_ids) else f"c{seq}_{index}",
            "name": name,
            "args": args,
        }
        for index, (name, args) in enumerate(calls)
    ]
    return SessionEvent(
        seq=seq, type=MODEL_COMPLETED, data={"content": content, "tool_calls": raw},
        run_id=run_id,
    )


def _call(seq: int, *, call_id: str, name: str, args: dict, run_id: str = RUN_ID) -> SessionEvent:
    return SessionEvent(
        seq=seq, type=TOOL_CALL,
        data={"tool_call_id": call_id, "tool_name": name, "args": args},
        run_id=run_id,
    )


def _result(
    seq: int, *, call_id: str, ok: bool, message: str = "", data=None,
    error_code: str | None = None, run_id: str = RUN_ID,
) -> SessionEvent:
    payload = {"ok": ok, "message": message, "data": data, "error_code": error_code}
    return SessionEvent(
        seq=seq, type=TOOL_RESULT,
        data={"tool_call_id": call_id, "content": json.dumps(payload)},
        run_id=run_id,
    )


def _tool_round(
    clock: _Seq, *, index: int, name: str, args: dict, ok: bool,
    message: str = "", data=None, error_code: str | None = None,
    run_id: str = RUN_ID,
) -> list[SessionEvent]:
    """一轮"模型要工具 → 工具调用 → 工具结果"（三事件，call id 两处同值）。"""
    call_id = f"call-{index}"
    return [
        _completed(
            clock.take(), calls=((name, args),), call_ids=(call_id,), run_id=run_id,
        ),
        _call(clock.take(), call_id=call_id, name=name, args=args, run_id=run_id),
        _result(
            clock.take(), call_id=call_id, ok=ok, message=message, data=data,
            error_code=error_code, run_id=run_id,
        ),
    ]


def _monologue(clock: _Seq, *, text: str, run_id: str = RUN_ID) -> SessionEvent:
    return _completed(clock.take(), content=text, run_id=run_id)


def _advance(detector: StuckDetector, events: list[SessionEvent]):
    """喂事件并取本轮唯一的动作（与 runtime 的消费方式一致）。"""
    return worst_stuck_signal(detector.advance(events))


def _only(signals, pattern: str):
    """只看某个模式的信号：五模式在同一段历史上会重叠（ADR-0048 D4/D5），
    "某个模式不触发"的断言必须按模式过滤，否则会被别的模式的正确触发干扰。"""
    return [signal for signal in signals if signal.pattern == pattern]


# ── ① 同动作 + 同错误的失败 ────────────────────────────────────────────────


class TestPatternToolFailure:
    """① 阈值 T=3、暂停 2T=6（`02 §5.3` 的表；ADR-0048 D2/D5）。"""

    def test_replan_at_three_and_pause_at_six(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        signals: list[tuple[int, str, str]] = []
        for index in range(6):
            events += _tool_round(
                clock, index=index, name="bash", args={"command": "ls"},
                ok=False, message="boom", error_code="TOOL_EXECUTION_ERROR",
            )
            signal = _advance(detector, events)
            if signal is not None:
                signals.append((signal.count, signal.level, signal.pattern))
        assert signals == [
            (3, STUCK_LEVEL_REPLAN, STUCK_PATTERN_TOOL_FAILURE),
            (6, STUCK_LEVEL_PAUSED, STUCK_PATTERN_TOOL_FAILURE),
        ]

    def test_one_turn_of_six_identical_failures_gets_the_replan_first(self) -> None:
        """一个模型回合并列 6 条相同失败：T 与 2T 同批到线，**先执行 replan**（T9 审查 P1）。

        生产形状：一次 `model/completed` 同时请求同一个动作 6 次（Live Gate 场景每轮 3 次
        是它的缩小版）。`RepeatedToolFailureGuard` 在计数 3 发 SOFT、6 发 HARD，两条信号
        落在同一个 `advance` 里。原实现"暂停优先"会把同批的 replan 直接压掉：模型一条
        纠正都没收到，而载荷写着 `replan_count=1`（对不上账的"已用掉"）。

        正确顺序来自 `02 §5.3` 的"**replan 后**同一模式仍持续 ⇒ 暂停"——"之后"要求至少
        一次后续观测。所以这一批执行 replan，暂停留给下一次仍然重复的失败。
        """
        clock = _Seq()
        args = {"command": "ls"}
        call_ids = tuple(f"same-{index}" for index in range(6))
        events: list[SessionEvent] = [
            _completed(
                clock.take(),
                calls=tuple(("bash", args) for _ in call_ids),
                call_ids=call_ids,
            ),
        ]
        for call_id in call_ids:
            events.append(_call(clock.take(), call_id=call_id, name="bash", args=args))
        for call_id in call_ids:
            events.append(_result(
                clock.take(), call_id=call_id, ok=False, message="boom",
                error_code="TOOL_EXECUTION_ERROR",
            ))

        detector = StuckDetector(run_id=RUN_ID)
        first = _advance(detector, events)
        assert first is not None
        assert (first.level, first.pattern, first.count) == (
            STUCK_LEVEL_REPLAN, STUCK_PATTERN_TOOL_FAILURE, 3,
        )
        # 暂停没丢：第 7 条同样的失败 ⇒ 只发暂停，计数是 7（不是被折叠掉的 6）。
        events += _tool_round(
            clock, index=9, name="bash", args=args, ok=False, message="boom",
            error_code="TOOL_EXECUTION_ERROR",
        )
        second = _advance(detector, events)
        assert second is not None
        assert (second.level, second.pattern, second.count) == (
            STUCK_LEVEL_PAUSED, STUCK_PATTERN_TOOL_FAILURE, 7,
        )

    def test_a_different_error_code_restarts_the_count(self) -> None:
        """① 的判据是"同动作 + 同错误"：换了错误种类就是另一个模式（`02 §5.3`）。"""
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        for index, error in enumerate(["E_ONE", "E_ONE", "E_TWO"]):
            events += _tool_round(
                clock, index=index, name="bash", args={"command": "ls"},
                ok=False, message="boom", error_code=error,
            )
        assert _advance(detector, events) is None

    def test_a_different_action_restarts_the_count(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        for index, command in enumerate(["ls", "ls", "pwd"]):
            events += _tool_round(
                clock, index=index, name="bash", args={"command": command},
                ok=False, message="boom", error_code="TOOL_EXECUTION_ERROR",
            )
        assert _advance(detector, events) is None

    def test_exempt_error_codes_do_not_feed_the_counter(self) -> None:
        """配额 / 到点被拒不算工具失败，也不打断已有计数（`#314`/`#315` 语义搬家）。"""
        assert "BUDGET_EXHAUSTED" in GUARD_EXEMPT_ERROR_CODES
        assert "DEADLINE_EXCEEDED" in GUARD_EXEMPT_ERROR_CODES
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        for index in range(3):
            events += _tool_round(
                clock, index=index, name="bash", args={"command": "ls"},
                ok=False, message="boom", error_code="TOOL_EXECUTION_ERROR",
            )
        # 第 4 轮是"配额拒绝"：它不计数……
        events += _tool_round(
            clock, index=3, name="bash", args={"command": "ls"},
            ok=False, message="quota", error_code="BUDGET_EXHAUSTED",
        )
        signal = _advance(detector, events)
        assert signal is not None and signal.count == 3 and signal.level == STUCK_LEVEL_REPLAN

    def test_an_injected_engine_only_contributes_its_threshold(self) -> None:
        """注入引擎 = 换阈值（不是带状态）：报告里的 threshold 与触发计数同源。"""
        clock = _Seq()
        detector = StuckDetector(
            run_id=RUN_ID,
            failure_guard=RepeatedToolFailureGuard(soft_threshold=1, hard_threshold=1),
        )
        events = _tool_round(
            clock, index=0, name="bash", args={"command": "ls"},
            ok=False, message="boom", error_code="TOOL_EXECUTION_ERROR",
        )
        signal = _advance(detector, events)
        assert signal is not None
        assert (signal.count, signal.threshold) == (1, 1)

    def test_replaying_the_same_events_rebuilds_the_same_count(self) -> None:
        """重启 / 同 run 恢复：同一个事件前缀 ⇒ 同一个下一个动作（D1）。"""
        clock = _Seq()
        history: list[SessionEvent] = []
        for index in range(2):
            history += _tool_round(
                clock, index=index, name="bash", args={"command": "ls"},
                ok=False, message="boom", error_code="TOOL_EXECUTION_ERROR",
            )
        rebuilt = StuckDetector.from_events(history, RUN_ID)
        tail = _tool_round(
            clock, index=2, name="bash", args={"command": "ls"},
            ok=False, message="boom", error_code="TOOL_EXECUTION_ERROR",
        )
        signal = _advance(rebuilt, history + tail)
        assert signal is not None and signal.count == 3

    def test_events_of_another_run_do_not_count(self) -> None:
        """每个逻辑 run 的循环各自干净起步：别的 run 的同错不进本 run 的计数。"""
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        for index in range(3):
            events += _tool_round(
                clock, index=index, name="bash", args={"command": "ls"},
                ok=False, message="boom", error_code="TOOL_EXECUTION_ERROR",
                run_id="run-other",
            )
        assert _advance(detector, events) is None


# ── ② 同动作 + 同观察（成功） ──────────────────────────────────────────────


class TestPatternObservation:
    """② 阈值 4、暂停 8：同动作每次都成功、且观察一模一样（`02 §5.3`）。"""

    def test_replan_at_four_and_pause_at_eight(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        signals: list[tuple[int, str]] = []
        for index in range(8):
            events += _tool_round(
                clock, index=index, name="read", args={"file": "a.txt"},
                ok=True, message="same content", data={"text": "same"},
            )
            signal = _advance(detector, events)
            if signal is not None:
                signals.append((signal.count, signal.level))
        assert signals == [(4, STUCK_LEVEL_REPLAN), (8, STUCK_LEVEL_PAUSED)]

    def test_a_new_observation_restarts_it(self) -> None:
        """同动作但读出**不同**内容 = 状态真的变了（`02 §6` 的"edit 后再次 read"）。"""
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        for index in range(4):
            events += _tool_round(
                clock, index=index, name="read", args={"file": "a.txt"},
                ok=True, message=f"content {index}", data={"text": f"v{index}"},
            )
        assert _advance(detector, events) is None

    def test_a_failure_breaks_the_run_of_observations(self) -> None:
        """② 只在成功结果上累积；一次失败让"同一个观察"不再连续。"""
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        for index in range(2):
            events += _tool_round(
                clock, index=index, name="read", args={"file": "a.txt"},
                ok=True, message="same", data={"text": "same"},
            )
        events += _tool_round(
            clock, index=2, name="read", args={"file": "a.txt"},
            ok=False, message="io error", error_code="TOOL_EXECUTION_ERROR",
        )
        signals = detector.advance(events)
        # 失败那一轮把 ② 的连击清零（同一个动作这一次的观察已经不是同一个了）
        assert _only(signals, STUCK_PATTERN_OBSERVATION) == []
        # 再连着两次同样的成功也回不到 4
        for index in range(3, 5):
            events += _tool_round(
                clock, index=index, name="read", args={"file": "a.txt"},
                ok=True, message="same", data={"text": "same"},
            )
        signals = detector.advance(events)
        assert _only(signals, STUCK_PATTERN_OBSERVATION) == []

    def test_cosmetic_argument_differences_keep_the_same_fingerprint(self) -> None:
        """等价参数（键序 / 空白）是同一个动作：否则"原地打转"会被伪装成"换了动作"。"""
        first = action_fingerprint("bash", {"command": "ls  -la", "cwd": "/w"})
        second = action_fingerprint("bash", {"cwd": "/w", "command": " ls  -la "})
        assert first == second
        other = action_fingerprint("bash", {"command": "ls -la", "cwd": "/x"})
        assert other != first

    def test_credentials_never_reach_the_fingerprint(self) -> None:
        """凭证值 MUST NOT 进指纹（`02 §5.3`）：两个不同的 token 折成同一个动作。"""
        with_a = action_fingerprint("bash", {"command": "curl --token=aaa https://h"})
        with_b = action_fingerprint("bash", {"command": "curl --token=bbb https://h"})
        assert with_a == with_b
        assert "aaa" not in with_a and "bbb" not in with_a

    def test_bearer_token_never_reaches_the_fingerprint(self) -> None:
        """`Authorization: Bearer <token>` 的形状也要折掉（T9 实测的漏点）。"""
        fingerprint = action_fingerprint(
            "bash", {"command": 'curl -H "Authorization: Bearer super-secret" https://h'},
        )
        assert "super-secret" not in fingerprint
        assert fingerprint == action_fingerprint(
            "bash", {"command": 'curl -H "Authorization: Bearer other-secret" https://h'},
        )

    def test_the_observation_fingerprint_ignores_timing_noise(self) -> None:
        """观察指纹含结果内容、不含时延之类的"每次必变"字段。"""
        same = outcome_fingerprint(ok=True, error_code=None, message="ok", data={"a": 1})
        assert same == outcome_fingerprint(
            ok=True, error_code=None, message="ok", data={"a": 1},
        )
        assert same != outcome_fingerprint(
            ok=True, error_code=None, message="ok", data={"a": 2},
        )


# ── ③ 无工具调用的独白 ────────────────────────────────────────────────────


class TestPatternMonologue:
    """③ 阈值 3、暂停 6：连续"无工具调用 + 同签名"的决策（`02 §5.3`）。"""

    def test_replan_at_three_and_pause_at_six(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        signals: list[tuple[int, str]] = []
        for _ in range(6):
            events.append(_monologue(clock, text="我再想想"))
            signal = _advance(detector, events)
            if signal is not None:
                signals.append((signal.count, signal.level))
        assert signals == [(3, STUCK_LEVEL_REPLAN), (6, STUCK_LEVEL_PAUSED)]

    def test_a_different_utterance_restarts_it(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events = [
            _monologue(clock, text="我再想想"),
            _monologue(clock, text="我再想想"),
            _monologue(clock, text="换个说法"),
        ]
        assert _advance(detector, events) is None

    def test_a_decision_with_tool_calls_breaks_the_monologue(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = [
            _monologue(clock, text="我再想想"),
            _monologue(clock, text="我再想想"),
        ]
        events += _tool_round(
            clock, index=0, name="read", args={"file": "a.txt"},
            ok=True, message="ok", data={"text": "x"},
        )
        events.append(_monologue(clock, text="我再想想"))
        assert _advance(detector, events) is None

    def test_the_signature_covers_the_requested_calls(self) -> None:
        """签名含"请求了哪几个动作"：同文本但换了动作不算同一个独白。"""
        first = decision_signature(content="同", calls=[("bash", "fp-1")])
        second = decision_signature(content="同", calls=[("bash", "fp-2")])
        assert first != second


# ── ④ 两模式交替 ──────────────────────────────────────────────────────────


class TestPatternAlternating:
    """④ 阈值 6、暂停 12：两个签名交替出现（`02 §5.3`）。

    场景刻意让每一轮都**有**新的成功观察：否则 ⑤（连续决策无进展，2T=8）会先到暂停点，
    ④ 的 12 根本走不到——两条模式抢同一个"停"字，先到者赢是设计，不是缺陷。这里要证明的
    是 ④ 自己的阈值表，所以把 ⑤ 的输入（无进展）从场景里拿掉。
    """

    def _ping_pong(self, clock: _Seq, index: int) -> list[SessionEvent]:
        """a.txt / b.txt 交替读：签名交替 → ④；每轮内容都不同 → 不是 ②，也不喂 ⑤。"""
        left = index % 2 == 0
        return _tool_round(
            clock, index=index,
            name="read", args={"file": "a.txt" if left else "b.txt"},
            ok=True, message=f"content {index}", data={"text": f"v{index}"},
        )

    def test_replan_at_six_and_pause_at_twelve(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        signals: list[tuple[int, str]] = []
        for index in range(12):
            events += self._ping_pong(clock, index)
            signal = _advance(detector, events)
            if signal is not None and signal.pattern == STUCK_PATTERN_ALTERNATING:
                signals.append((signal.count, signal.level))
        assert signals == [(6, STUCK_LEVEL_REPLAN), (12, STUCK_LEVEL_PAUSED)]

    def test_a_third_signature_breaks_the_alternation(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        for index in range(9):
            # 三个签名轮转（不是"恰好两个"）——不构成交替循环
            events += _tool_round(
                clock, index=index, name="read", args={"file": f"{index % 3}.txt"},
                ok=True, message=f"content {index}", data={"text": f"v{index}"},
            )
        signals = detector.advance(events)
        assert _only(signals, STUCK_PATTERN_ALTERNATING) == []

    def test_a_single_repeated_signature_is_not_alternation(self) -> None:
        """一直同一个签名是"卡在同一个动作"，不是交替（那是别的模式的事）。"""
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        for index in range(5):
            events += _tool_round(
                clock, index=index, name="read", args={"file": "a.txt"},
                ok=True, message="same", data={"text": "same"},
            )
        signals = detector.advance(events)
        assert _only(signals, STUCK_PATTERN_ALTERNATING) == []


# ── ⑤ 项目级无进展窗口 ────────────────────────────────────────────────────


class TestPatternProjectWindow:
    """⑤ 阈值 4、暂停 8：连续四个决策都没有"新的成功观察"（`02 §5.3`）。

    场景用**各不相同**的失败动作：签名每轮都变 ⇒ ④ 不成立、① 的"连续同动作"不成立，
    剩下的唯一"没有进展"的信号就是 ⑤ 自己（这正是它"项目级"的含义）。
    """

    def _failing_novel(self, clock: _Seq, index: int) -> list[SessionEvent]:
        return _tool_round(
            clock, index=index, name="bash", args={"command": f"try{index}"},
            ok=False, message=f"failed {index}", error_code="TOOL_EXECUTION_ERROR",
        )

    def test_replan_at_four_and_pause_at_eight(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        signals: list[tuple[int, str]] = []
        for index in range(8):
            events += self._failing_novel(clock, index)
            signal = _advance(detector, events)
            if signal is not None and signal.pattern == STUCK_PATTERN_PROJECT:
                signals.append((signal.count, signal.level))
        assert signals == [(4, STUCK_LEVEL_REPLAN), (8, STUCK_LEVEL_PAUSED)]

    def test_a_new_observation_resets_the_window(self) -> None:
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        for index in range(3):
            events += self._failing_novel(clock, index)
        # 第 4 个决策带来了**新的**成功观察：项目在往前走 ⇒ 窗口清零
        events += _tool_round(
            clock, index=3, name="read", args={"file": "b.txt"},
            ok=True, message="new", data={"text": "brand new"},
        )
        for index in range(4, 6):
            events += self._failing_novel(clock, index)
        signals = detector.advance(events)
        assert _only(signals, STUCK_PATTERN_PROJECT) == []

    def test_a_repeated_observation_is_not_progress(self) -> None:
        """同一个成功观察**不算**进展（本 run 见过的观察指纹不再算新）。

        场景让每轮的动作签名都不同（②/④ 因此不成立）、结果内容却一模一样：唯一在累积的
        就只剩 ⑤ 的窗口——如果"重复的成功观察"被当成进展，这条用例会得到零信号。

        第一轮必然是"新观察"（本 run 第一次见到它）⇒ 那一轮算进展、窗口从 0 起算，
        所以第 8 次触发落在第 9 个决策上（比全失败场景晚一轮）。
        """
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        signals: list[tuple[int, str]] = []
        for index in range(9):
            events += _tool_round(
                clock, index=index, name="read", args={"file": f"f{index}.txt"},
                ok=True, message="same", data={"text": "same"},
            )
            signal = _advance(detector, events)
            if signal is not None and signal.pattern == STUCK_PATTERN_PROJECT:
                signals.append((signal.count, signal.level))
        assert signals == [(4, STUCK_LEVEL_REPLAN), (8, STUCK_LEVEL_PAUSED)]


# ── 跨模式：进展只复位相关联的那一个 ──────────────────────────────────────


class TestProgressIsPerPattern:
    def test_progress_resets_the_window_but_not_the_alternation(self) -> None:
        """§5.3 的"只复位受影响的那一个模式"：④ 与 ⑤ 在同一段历史上各算各的。

        a.txt / b.txt 交替（签名交替 ⇒ ④ 累积），而每次 a.txt 都读出**新**内容
        （⇒ ⑤ 的窗口每轮清零）。如果"进展"顺手把 ④ 也清了，这里就不会有 6 连击。
        """
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events: list[SessionEvent] = []
        alternating: list[tuple[int, str]] = []
        for index in range(6):
            left = index % 2 == 0
            events += _tool_round(
                clock, index=index,
                name="read", args={"file": "a.txt" if left else "b.txt"},
                ok=left,
                message=f"content {index}" if left else "boom",
                data={"text": f"v{index}"} if left else None,
                error_code=None if left else "TOOL_EXECUTION_ERROR",
            )
            signal = _advance(detector, events)
            if signal is not None and signal.pattern == STUCK_PATTERN_ALTERNATING:
                alternating.append((signal.count, signal.level))
        assert alternating == [(6, STUCK_LEVEL_REPLAN)]

    def test_a_later_replay_of_the_same_prefix_is_idempotent(self) -> None:
        """`advance` 是幂等的：重复喂同一批事件不会把计数喂成两倍。"""
        clock, detector = _Seq(), StuckDetector(run_id=RUN_ID)
        events = _tool_round(
            clock, index=0, name="bash", args={"command": "ls"},
            ok=False, message="boom", error_code="TOOL_EXECUTION_ERROR",
        )
        first = detector.advance(events)
        second = detector.advance(events)
        assert first == [] and second == []

    def test_thresholds_match_the_frozen_contract(self) -> None:
        """阈值是契约值（`02 §5.3`），改了这里必须同时改规格与 ADR-0048 D2。"""
        assert STUCK_THRESHOLDS == {
            STUCK_PATTERN_TOOL_FAILURE: 3,
            STUCK_PATTERN_OBSERVATION: 4,
            STUCK_PATTERN_MONOLOGUE: 3,
            STUCK_PATTERN_ALTERNATING: 6,
            STUCK_PATTERN_PROJECT: 4,
        }


@pytest.mark.parametrize("pattern", list(STUCK_THRESHOLDS))
def test_every_pattern_reports_its_own_threshold(pattern: str) -> None:
    """每个模式报出的 `threshold` 就是契约表里那一个（客户端的"到第几次了"据此解释）。"""
    assert STUCK_THRESHOLDS[pattern] > 0

class TestExternallyCountedCalls:
    """委派树账本那一格的接口（`guards.external_failure_signal` + `advance` 的免重数集）。

    机制正本见 ADR-0048 D1 的例外与残余 8；这里钉三件事：翻译的形状、免重数真的免掉、
    以及"没到线时不动作也不免重数"（否则一个没给出结论的账本会静默关掉本 run 的 ①）。
    """

    def test_a_soft_verdict_becomes_a_replan_signal(self) -> None:
        signal = external_failure_signal(
            level="soft", tool_name="delegate", fingerprint="supervisor:abc", count=3,
        )
        assert signal is not None
        assert signal.level == STUCK_LEVEL_REPLAN
        assert signal.pattern == STUCK_PATTERN_TOOL_FAILURE
        assert signal.count == 3
        assert signal.threshold == STUCK_THRESHOLDS[STUCK_PATTERN_TOOL_FAILURE]
        assert signal.tool_name == "delegate" and signal.fingerprint == "supervisor:abc"

    def test_a_hard_verdict_becomes_a_pause_signal(self) -> None:
        signal = external_failure_signal(
            level="HARD", tool_name="delegate", fingerprint="supervisor:abc", count=6,
        )
        assert signal is not None
        assert signal.level == STUCK_LEVEL_PAUSED and signal.count == 6

    def test_unlines_and_unknown_levels_do_not_produce_a_signal(self) -> None:
        """`none`（账本计数推进但没到线）与任何未知 level：不动作、也不免重数。"""
        for level in ("none", "", "weird"):
            assert external_failure_signal(
                level=level, tool_name="delegate", fingerprint="f", count=2,
            ) is None

    def test_exempted_calls_are_not_counted_by_the_detector(self) -> None:
        """同一批 3 次同错失败：不豁免 ⇒ 恰好一次 replan；豁免一条（树账本在数它）⇒ 不到线。

        这就是"同一批失败被记两遍会让阈值提前到顶"的反面证明——也让"免重数"这件事
        有一个**可观察**的差别，而不是一句注释。
        """
        def _rounds(clock: _Seq) -> list:
            events: list = []
            for index in range(3):
                events += _tool_round(
                    clock, index=index, name="delegate",
                    args={"target": "coding", "task": "same"},
                    ok=False, message="子代理失败", error_code="TOOL_EXECUTION_ERROR",
                )
            return events

        plain = StuckDetector(run_id=RUN_ID)
        assert [
            signal.level for signal in plain.advance(_rounds(_Seq()))
            if signal.pattern == STUCK_PATTERN_TOOL_FAILURE
        ] == [STUCK_LEVEL_REPLAN], "不豁免 ⇒ 第 3 次同错到线（replan 一次）"

        exempt = StuckDetector(run_id=RUN_ID)
        events = _rounds(_Seq())
        exempted = frozenset({"call-0"})
        assert [
            signal for signal in exempt.advance(
                events, externally_counted_call_ids=exempted,
            )
            if signal.pattern == STUCK_PATTERN_TOOL_FAILURE
        ] == [], "豁免的那条不算数 ⇒ 只剩 2 次，不到线"
        # 幂等：同一批事件再喂一次也不会"补"出信号（免重数是逐条的，不是整批跳过）
        assert [
            signal for signal in exempt.advance(
                events, externally_counted_call_ids=exempted,
            )
            if signal.pattern == STUCK_PATTERN_TOOL_FAILURE
        ] == [], "免重数不得被下一轮 advance 补回来"
