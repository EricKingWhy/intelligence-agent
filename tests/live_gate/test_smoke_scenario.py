"""smoke Live Gate 场景的**确定性**测试（不烧真实模型调用）。

真实 3/3 证据由 `python scripts/live_gate.py run --scenario smoke-production-tools`
产出并落进 `docs/live_gate/**`；本文件只钉**场景自身**可机检的两件事：

1. **断言集是诚实的**：run 未完成 / 产物内容不等 / 缺 write 或命令类工具 / 悬空
   tool call 都要判不通过，而一个自洽终态上全绿；
2. **终态读盘的静止自证**：首读之后轨迹又被追加一行（`#312` 假 FAIL 的根因，
   `089de04` 修的是同一形态）⇒ `durable_replay_matches_live` 判红，且**只**它判红。

（替身模型跑场景**不算** Live Gate 证据——那种运行在 runner 那边会被 `seams` 锁到
FAIL；本文件也不注册任何场景、不落盘证据。）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from evaluation.live_gate.scenarios.smoke import (
    ARTIFACT,
    ARTIFACT_CONTENT,
    SmokeProductionToolsScenario,
)

SCENARIO = SmokeProductionToolsScenario()

SESSION_ID = "live-gate-smoke-a1"
RUN_ID = "run-smoke-1"


class _StubSandbox:
    """断言面替身：只实现 `read_text`（真沙箱由真实 Live Gate 运行覆盖）。"""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    def read_text(self, name: str) -> str:
        if name not in self._files:
            raise FileNotFoundError(name)
        return self._files[name]


#: 一格请求自报的 token 数（合成值）。用**能整除的常数**：断言比的是"和轨迹重算相同"，
#: 数字本身是多少无关紧要，但让"少算一格 / 记成 0"这类变异一眼看得出差在哪。
TOKENS_PER_REQUEST = 7


def _events(
    *, tool_calls: list[str] | None = None, requests: int | None = None,
    reported_tokens: int | None = None, terminal_usage: int | None = None,
) -> list[Any]:
    """自洽轨迹：run/started + 每次工具调用各一对 call/result + 一次决策 + run/completed。

    每条带 `seq`（与持久化 `SessionEvent` 同名同义）：复读判据按 `(seq, type)` 逐条比。
    三个 `*_tokens` / `requests` 参数是**故意制造分歧**的旋钮（对账判据的鉴别力用例）：
    缺省时三者自洽（1 格请求、自报 7、终态 usage_total=7）。
    """
    calls = ["write", "bash", "git_status"] if tool_calls is None else tool_calls
    request_count = 1 if requests is None else requests
    self_reported = TOKENS_PER_REQUEST if reported_tokens is None else reported_tokens
    total = self_reported if terminal_usage is None else terminal_usage
    events: list[Any] = [SimpleNamespace(
        seq=1, type="run/started", data={"turn_index": 1}, run_id=RUN_ID,
    )]
    for index, name in enumerate(calls, start=1):
        call_id = f"call-{index}"
        events.append(SimpleNamespace(
            seq=len(events) + 1, type="tool/call",
            data={"tool_call_id": call_id, "tool_name": name}, run_id=RUN_ID,
        ))
        events.append(SimpleNamespace(
            seq=len(events) + 1, type="tool/result",
            data={"tool_call_id": call_id, "tool_name": name}, run_id=RUN_ID,
        ))
    for _ in range(request_count):
        events.append(SimpleNamespace(
            seq=len(events) + 1, type="model/request",
            data={
                "role": "primary", "outcome": "completed", "model": "test-model",
                "usage": {"total_tokens": self_reported},
            },
            run_id=RUN_ID,
        ))
    events.append(SimpleNamespace(
        seq=len(events) + 1, type="model/completed", data={"content": "完成"}, run_id=RUN_ID,
    ))
    events.append(SimpleNamespace(
        seq=len(events) + 1, type="run/completed",
        data={"final_text": "完成", "usage_total": {"total_tokens": total}, "cost_usd": None},
        run_id=RUN_ID,
    ))
    return events


def _assertions_for(
    *, run_status: str = "completed", tool_calls: list[str] | None = None,
    files: dict[str, str] | None = None, replayed: list[Any] | None = None,
    session_id: str = SESSION_ID, **event_kwargs: Any,
) -> dict[str, Any]:
    events = _events(tool_calls=tool_calls, **event_kwargs)
    calls = [
        str(event.data.get("tool_name")) for event in events if event.type == "tool/call"
    ]
    payload = {ARTIFACT: ARTIFACT_CONTENT} if files is None else files
    results = SCENARIO._assertions(
        ctx=SimpleNamespace(session_id=session_id, sandbox=_StubSandbox(payload)),
        run_status=run_status,
        tool_calls=calls,
        run_id=RUN_ID,
        events=events,
        replayed=list(events) if replayed is None else replayed,
    )
    return {item.name: item for item in results}


def test_assertions_accept_a_consistent_end_state():
    """自洽终态（产物对、write + 命令类工具都出现过、无悬空 call）→ 全绿。"""
    results = _assertions_for()
    failed = [name for name, item in results.items() if not item.ok]
    assert failed == [], failed


def test_assertions_reject_an_incomplete_run():
    """run 没有 completed → 判红（本场景的第一条硬判据）。"""
    results = _assertions_for(run_status="failed")
    assert not results["run_completed"].ok


def test_assertions_reject_a_wrong_or_missing_artifact():
    """产物内容不等 / 文件不在 → 判红（"看起来像"不算证据，相等才算）。"""
    wrong = _assertions_for(files={ARTIFACT: ARTIFACT_CONTENT + "\n"})
    assert not wrong["artifact_content"].ok

    missing = _assertions_for(files={})
    assert not missing["artifact_content"].ok


def test_assertions_reject_missing_write_or_command_tool():
    """只有 write、没有任何命令 / Git 类生产工具 → `command_or_git_tool_used` 判红。"""
    results = _assertions_for(tool_calls=["write"])
    assert results["write_tool_used"].ok
    assert not results["command_or_git_tool_used"].ok


def test_assertions_reject_a_dangling_tool_call():
    """只有 tool/call 没有配对的 tool/result → 判红（轨迹内部一致性的机械判据）。"""
    events = _events() + [SimpleNamespace(
        seq=99, type="tool/call",
        data={"tool_call_id": "call-dangling", "tool_name": "bash"}, run_id=RUN_ID,
    )]
    results = SCENARIO._assertions(
        ctx=SimpleNamespace(session_id=SESSION_ID, sandbox=_StubSandbox({ARTIFACT: ARTIFACT_CONTENT})),
        run_status="completed",
        tool_calls=[str(event.data.get("tool_name")) for event in events if event.type == "tool/call"],
        run_id=RUN_ID,
        events=events,
        replayed=list(events),
    )
    by_name = {item.name: item for item in results}
    assert not by_name["no_dangling_tool_calls"].ok


def test_assertions_reject_a_trajectory_that_grew_after_the_first_read():
    """首读之后又被追加一行 → 不通过（`#312` 假 FAIL 的根因，`089de04` 的同一形态）。

    形态就是取证缺陷：场景读了 N 条并据此记 `event_count=N`，而 runner 随后复制的
    轨迹是 N+1 行（可选能力收尾落一条 `memory/degraded`）⇒ `validator.py` 的
    「记录值 ↔ 轨迹行数」比对判 FAIL，看起来像产品缺陷。复读与首读不同序就是它的
    机械特征，必须**在场景里**判红，而不是留给复核者去猜计数差从哪来。
    """
    events = _events()
    grown = list(events) + [SimpleNamespace(
        seq=len(events) + 1, type="memory/degraded", data={"reason": "writeback"},
        run_id=RUN_ID,
    )]
    results = SCENARIO._assertions(
        ctx=SimpleNamespace(session_id=SESSION_ID, sandbox=_StubSandbox({ARTIFACT: ARTIFACT_CONTENT})),
        run_status="completed",
        tool_calls=[str(event.data.get("tool_name")) for event in events if event.type == "tool/call"],
        run_id=RUN_ID,
        events=events,
        replayed=grown,
    )
    by_name = {item.name: item for item in results}
    assert not by_name["durable_replay_matches_live"].ok
    # 其余断言与这件事无关：判红只由"轨迹动了"触发（鉴别力在这一点上）
    failed = {name for name, item in by_name.items() if not item.ok}
    assert failed == {"durable_replay_matches_live"}


def test_assertions_reject_missing_session_identity():
    """没有 session_id → 判红（证据必须能指回它跑在哪个会话上）。"""
    results = _assertions_for(session_id="")
    assert not results["session_identity_present"].ok


# ── 账本面（`#313` T5）────────────────────────────────────────────────


def test_assertions_reject_requests_out_of_step_with_decisions():
    """请求数 ≠ 决策数 → 判红（直路：一次决策恰一次实际请求）。"""
    results = _assertions_for(requests=2)
    assert not results["budget.one_request_per_decision"].ok


def test_assertions_reject_tokens_that_do_not_match_the_trajectory():
    """终态 `usage_total` 与轨迹重算不等 → 判红（半格差也要看得见）。"""
    results = _assertions_for(terminal_usage=TOKENS_PER_REQUEST + 1)
    assert results["budget.one_request_per_decision"].ok
    assert not results["budget.terminal_tokens"].ok


def test_cost_is_unknown_when_no_request_reports_it():
    """没有任何一格自报 cost ⇒ 读数必须是**未知**（`None`），不是 0。

    生产的 Provider 归属链当前不报 cost（`#313` 的 D4：不臆造费率），所以这条在真实
    3/3 上也会走到——它正是"不可得 ≠ 0"在证据里的落点。
    """
    results = _assertions_for()
    assert results["budget.terminal_cost"].ok
    assert "未知" in results["budget.terminal_cost"].detail
