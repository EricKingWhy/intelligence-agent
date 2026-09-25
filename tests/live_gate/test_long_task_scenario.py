"""T3（#308）长任务 Live Gate 场景的**确定性**测试（不烧真实模型调用）。

真实 3/3 证据由 `python scripts/live_gate.py run --scenario long-task-past-legacy-turn-limit`
产出并落进 `docs/live_gate/**`；本文件只钉**场景自身**的两件可机检事实，避免"跑了才知道
场景是不是恒 PASS / 恒 FAIL"：

1. **信息屏障真的成立**：链脚本一次调用最多推进一格，拿旧串不推进——这正是场景
   "必须超过 10 轮"的来源，不是"希望模型慢慢来"；
2. **断言集是诚实的**：`steps ≤ 10`、轨迹里出现 `max_steps_exceeded`、产物/步数不一致
   都要判不通过；并且在一个自洽终态上全部通过。

（替身模型跑场景**不算** Live Gate 证据——那种运行在 runner 那边会被 `seams` 锁到 FAIL；
本文件也不注册任何场景、不落盘证据。）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent_harness.agent.budget import SOURCE_DEPLOYMENT, LocalFuse
from agent_harness.config import Settings
from agent_harness.sandbox.local import LocalSubprocessSandbox
from evaluation.live_gate.registry import ScenarioContext
from evaluation.live_gate.scenarios.long_task import (
    CHAIN_SCRIPT,
    DONE_FILE,
    STEPS_FILE,
    TOKEN_FILE,
    TRANSITIONS,
    LongTaskPastLegacyTurnLimitScenario,
)

SCENARIO = LongTaskPastLegacyTurnLimitScenario()


def _settings(**overrides: Any) -> Settings:
    """真实 `Settings`，但**不读仓库 `.env`**（`_env_file=None`）：单测不该把部署机的
    凭证与策略旋钮带进进程（`local_max_agent_turns` 正是被测的前置输入）。"""
    return Settings(_env_file=None, **overrides)


def _context(tmp_path, *, settings: Any | None = None) -> ScenarioContext:
    return ScenarioContext(
        settings=settings if settings is not None else _settings(),
        sandbox=LocalSubprocessSandbox(workspace_root=tmp_path / "workspace"),
        session_root=tmp_path / "sessions",
        session_id="live-gate-long-task-a1",
        attempt_index=1,
    )


class _StubSandbox:
    """断言面替身：只实现 `read_text`（真沙箱已经在上面两个用例里被真的跑过）。"""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    def read_text(self, name: str) -> str:
        if name not in self._files:
            raise FileNotFoundError(name)
        return self._files[name]


#: 一格请求自报的 token 数（合成值）。断言比的是"与轨迹重算相同"，数字本身无关紧要，
#: 但取常数能让"少算一格 / 记成 0"这类变异一眼看得出差在哪。
TOKENS_PER_REQUEST = 11


def _events(
    *, steps: int, fuse_tripped: bool, bash_calls: int = TRANSITIONS,
    requests: int | None = None, reported_tokens: int = TOKENS_PER_REQUEST,
    terminal_tokens: int | None = None,
) -> list[Any]:
    """自洽的事件序列：`steps` 次 model/completed（各一次实际请求）+ bash/write 各配对
    + run/completed（带 `usage_total`）。

    `fuse_tripped=True` 时把 `max_steps_exceeded` 写进一次 run/failed（模拟旧行为——
    这正是"撞 fuse 的 run 不该被记成通过"的输入）；那种形状没有 `run/completed`，
    终态读数缺失 ⇒ 对账判据如实判红（正是要看见的形状）。

    每条带 `seq`（与持久化 `SessionEvent` 同名同义）：`durable_replay_matches_live`
    按 `(seq, type)` 逐条比，替身没有 seq 就比不出"复读与首读同序"这件事。

    `requests` / `reported_tokens` / `terminal_tokens` 是**故意制造分歧**的旋钮（对账判据的
    鉴别力用例）：`requests` 只动"落了几格请求"，`terminal_tokens` 只动终态那一份读数
    （默认 = 逐格累加，即同源），三者互相独立才能分别考出"哪一面漂了"。
    """
    request_count = steps if requests is None else requests
    terminal_total = (
        request_count * reported_tokens if terminal_tokens is None else terminal_tokens
    )
    events: list[Any] = [
        SimpleNamespace(seq=1, type="run/started", data={"turn_index": 1}, run_id="run-long-1"),
    ]
    for index in range(1, steps + 1):
        if index <= request_count:
            events.append(SimpleNamespace(
                seq=len(events) + 1, type="model/request",
                data={
                    "role": "primary", "outcome": "completed", "model": "test-model",
                    "usage": {"total_tokens": reported_tokens},
                },
                run_id="run-long-1",
            ))
        events.append(SimpleNamespace(
            seq=len(events) + 1, type="model/completed", data={"content": ""},
            run_id="run-long-1",
        ))
        if index <= bash_calls:
            call_id = f"call-{index}"
            events.append(SimpleNamespace(
                seq=len(events) + 1, type="tool/call",
                data={"tool_call_id": call_id, "tool_name": "bash"},
                run_id="run-long-1",
            ))
            events.append(SimpleNamespace(
                seq=len(events) + 1, type="tool/result",
                data={"tool_call_id": call_id, "tool_name": "bash"},
                run_id="run-long-1",
            ))
    events.append(SimpleNamespace(
        seq=len(events) + 1, type="tool/call",
        data={"tool_call_id": "call-write", "tool_name": "write"},
        run_id="run-long-1",
    ))
    events.append(SimpleNamespace(
        seq=len(events) + 1, type="tool/result",
        data={"tool_call_id": "call-write", "tool_name": "write"},
        run_id="run-long-1",
    ))
    if fuse_tripped:
        events.append(SimpleNamespace(
            seq=len(events) + 1, type="run/failed",
            data={"reason": "max_steps_exceeded", "message": "撞保险丝"},
            run_id="run-long-1",
        ))
    else:
        # 终态读数：usage_total = 自报格数 × 每格 token（逐格累加的同义表达）。
        events.append(SimpleNamespace(
            seq=len(events) + 1, type="run/completed",
            data={
                "final_text": "12 步完成",
                "usage_total": {"total_tokens": terminal_total},
                # 生产链不报 cost（`#313` D4）⇒ 未知，不是 0。
                "cost_usd": None,
            },
            run_id="run-long-1",
        ))
    return events


def _assertions_for(
    *, steps: int, fuse_tripped: bool, files: dict[str, str], bash_calls: int = TRANSITIONS,
    tool_calls: list[str] | None = None, replayed: list[Any] | None = None,
    **event_kwargs: Any,
) -> dict[str, Any]:
    events = _events(
        steps=steps, fuse_tripped=fuse_tripped, bash_calls=bash_calls, **event_kwargs,
    )
    calls = tool_calls if tool_calls is not None else ["bash"] * bash_calls + ["write"]
    results = SCENARIO._assertions(
        ctx=SimpleNamespace(session_id="live-gate-long-task-a1", sandbox=_StubSandbox(files)),
        run_status="completed" if not fuse_tripped else "max_steps_exceeded",
        tool_calls=calls,
        run_id="run-long-1",
        events=events,
        steps=steps,
        fuse=LocalFuse(max_agent_turns=500, source=SOURCE_DEPLOYMENT),
        replayed=list(events) if replayed is None else replayed,
    )
    return {item.name: item for item in results}


def _consistent_files() -> dict[str, str]:
    return {TOKEN_FILE: "abcd1234", STEPS_FILE: str(TRANSITIONS), DONE_FILE: "abcd1234"}


# ── prepare ──


@pytest.mark.asyncio
async def test_prepare_seeds_chain_script_and_initial_state(tmp_path):
    """prepare 在隔离工作区里放好链脚本与初值，且**不推进**任何一步。"""
    ctx = _context(tmp_path)
    assert await SCENARIO.prepare(ctx) == []
    assert "chain.py" in ctx.sandbox.read_text(CHAIN_SCRIPT)
    assert ctx.sandbox.read_text(TOKEN_FILE).strip()
    assert ctx.sandbox.read_text(STEPS_FILE).strip() == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", [10, 5])
async def test_prepare_reports_unmet_precondition_when_policy_is_too_low(tmp_path, ceiling):
    """部署把缺省 fuse 收到 ≤ 旧上限 ⇒ **前置不满足**（BLOCKED），且不 seed、不发 run。

    这是"策略不适用"与"实现有 bug"的分界：在这个配置下本场景要证明的命题（缺省入口允许
    越过旧 10 轮）本来就不成立，判 FAIL 等于把部署者的合法选择记成实现缺陷。
    """
    ctx = _context(tmp_path, settings=_settings(local_max_agent_turns=ceiling))
    unmet = await SCENARIO.prepare(ctx)
    assert unmet and any(str(ceiling) in item for item in unmet)
    assert not (tmp_path / "workspace" / CHAIN_SCRIPT).exists()


# ── 信息屏障（场景"必须超过 10 轮"的根据） ──


def test_chain_advances_only_with_the_observed_token(tmp_path):
    """拿旧串不推进；拿当前串才推进——每次调用**最多**推进一格。"""
    ctx = _context(tmp_path)
    import asyncio

    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    stale = ctx.sandbox.read_text(TOKEN_FILE).strip()
    assert ctx.sandbox.exec(f"python {CHAIN_SCRIPT} {stale}").exit_code == 0
    assert ctx.sandbox.read_text(STEPS_FILE).strip() == "1"

    # 同一条旧串再用一次：非零退出且**不推进**（信息屏障的另一半）
    replay = ctx.sandbox.exec(f"python {CHAIN_SCRIPT} {stale}")
    assert replay.exit_code != 0
    assert ctx.sandbox.read_text(STEPS_FILE).strip() == "1"

    # 用当前串一路推到 TRANSITIONS 格（每格都必须先看到上一次的输出）
    for expected in range(2, TRANSITIONS + 1):
        current = ctx.sandbox.read_text(TOKEN_FILE).strip()
        result = ctx.sandbox.exec(f"python {CHAIN_SCRIPT} {current}")
        assert result.exit_code == 0, result
        assert ctx.sandbox.read_text(STEPS_FILE).strip() == str(expected)
    assert ctx.sandbox.read_text(STEPS_FILE).strip() == str(TRANSITIONS)
    assert TRANSITIONS > 10, "链步数必须严格超过旧上限 10，否则场景证明不了任何事"


# ── 断言集诚实性 ──


def test_assertions_accept_a_consistent_end_state(tmp_path):
    """自洽终态：全部断言通过（否则场景会恒 FAIL）。"""
    results = _assertions_for(steps=TRANSITIONS + 1, fuse_tripped=False, files=_consistent_files())
    failed = [name for name, item in results.items() if not item.ok]
    assert failed == [], failed


def test_assertions_reject_a_run_that_stayed_within_the_legacy_limit(tmp_path):
    """`steps ≤ 10`：即使 run 完成、产物齐全，也不算通过——那正是本场景要排除的形状。"""
    results = _assertions_for(steps=10, fuse_tripped=False, files=_consistent_files())
    assert not results["legacy_turn_limit_exceeded"].ok


def test_assertions_reject_a_fuse_trip(tmp_path):
    """轨迹里出现 `max_steps_exceeded` → 不通过（AC：Live evidence 中没有它）。"""
    results = _assertions_for(steps=TRANSITIONS + 1, fuse_tripped=True, files=_consistent_files())
    assert not results["legacy_fuse_not_tripped"].ok
    assert not results["run_completed"].ok


@pytest.mark.parametrize(
    "files",
    [
        {TOKEN_FILE: "abcd1234", STEPS_FILE: str(TRANSITIONS - 1), DONE_FILE: "abcd1234"},  # 少一步
        {TOKEN_FILE: "abcd1234", STEPS_FILE: str(TRANSITIONS)},  # 没有 done.txt
        {TOKEN_FILE: "abcd1234", STEPS_FILE: str(TRANSITIONS), DONE_FILE: "deadbeef"},  # 串不一致
    ],
)
def test_assertions_reject_inconsistent_artifacts(tmp_path, files):
    """步数或产物对不上 → 至少一条断言不通过（产物是"模型真的观察过"的证据）。"""
    results = _assertions_for(steps=TRANSITIONS + 1, fuse_tripped=False, files=files)
    assert not all(item.ok for item in results.values())


def test_assertions_reject_missing_tool_or_model_work(tmp_path):
    """没有真实 bash 工具调用 / 模型请求 → 不通过（AC：能证明工具和模型请求真实发生）。"""
    few_tools = _assertions_for(
        steps=TRANSITIONS + 1, fuse_tripped=False, files=_consistent_files(),
        bash_calls=2, tool_calls=["bash", "bash", "write"],
    )
    assert not few_tools["tool_work_observed"].ok

    no_model = _assertions_for(
        steps=1, fuse_tripped=False, files=_consistent_files(),
        bash_calls=TRANSITIONS,
    )
    assert not no_model["model_decisions_exceeded_legacy_limit"].ok


def test_assertions_reject_a_trajectory_that_grew_after_the_first_read(tmp_path):
    """首读之后又被追加一行 → 不通过（`#312` 假 FAIL 的根因，`089de04` 的同一形态）。

    形态就是取证缺陷：场景读了 N 条并据此记 `event_count=N`，而 runner 随后复制的
    轨迹是 N+1 行（可选能力收尾落一条 `memory/degraded`）⇒ `validator.py` 的
    「记录值 ↔ 轨迹行数」比对判 FAIL，看起来像产品缺陷。复读与首读不同序就是它的
    机械特征，必须**在场景里**判红，而不是留给复核者去猜计数差从哪来。
    """
    events = _events(steps=TRANSITIONS + 1, fuse_tripped=False)
    grown = list(events) + [SimpleNamespace(
        seq=len(events) + 1, type="memory/degraded", data={"reason": "writeback"},
        run_id="run-long-1",
    )]
    results = _assertions_for(
        steps=TRANSITIONS + 1, fuse_tripped=False, files=_consistent_files(),
        replayed=grown,
    )
    assert not results["durable_replay_matches_live"].ok
    # 其余断言与这件事无关：它们仍然全绿 —— 判红只由"轨迹动了"触发（鉴别力在这一点上）
    failed = {name for name, item in results.items() if not item.ok}
    assert failed == {"durable_replay_matches_live"}


# ── 账本面（`#313` T5）────────────────────────────────────────────────


def test_assertions_reject_a_request_count_that_drifted_from_the_trajectory():
    """终态记的请求数与轨迹不等 → 判红（长跑之后账仍然要对得上）。"""
    results = _assertions_for(
        steps=TRANSITIONS + 1, fuse_tripped=False, files=_consistent_files(),
        requests=TRANSITIONS,
    )
    assert not results["budget.one_request_per_decision"].ok


def test_assertions_reject_terminal_tokens_that_do_not_match_the_trajectory():
    """终态 `usage_total` 与逐格自报的累加不等 → 判红（计数器不得漂移）。

    只动**终态那一份**读数（`terminal_tokens`），逐格自报照旧 ⇒ 红必然落在 `terminal_tokens`
    上，而不是"两边一起改所以一起对"。
    """
    steps = TRANSITIONS + 1
    results = _assertions_for(
        steps=steps, fuse_tripped=False, files=_consistent_files(),
        terminal_tokens=steps * TOKENS_PER_REQUEST + 3,
    )
    assert results["budget.one_request_per_decision"].ok
    assert not results["budget.terminal_tokens"].ok


def test_cost_stays_unknown_when_no_request_reports_it():
    """生产链不报 cost ⇒ 读数未知（不是 0）：这条在真实 3/3 上也会走到。"""
    results = _assertions_for(steps=TRANSITIONS + 1, fuse_tripped=False, files=_consistent_files())
    assert results["budget.terminal_cost"].ok
    assert "未知" in results["budget.terminal_cost"].detail
