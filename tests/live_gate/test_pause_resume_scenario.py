"""T4（`#312`）预算暂停/恢复 Live Gate 场景的**确定性**测试（不烧真实模型调用）。

真实 3/3 证据由 `python scripts/live_gate.py run --scenario budget-pause-resume-same-run`
产出并落进 `docs/live_gate/**`；本文件只钉**场景自身**的可机检事实，避免"跑了才知道
场景是不是恒 PASS / 恒 FAIL"：

1. **信息屏障真的成立**：链脚本一次调用最多推进一格，拿旧串不推进 —— 这正是"恢复之后
   还有活要干"的来源，不是"希望模型慢慢来"；
2. **断言集是诚实的**：重复暂停 / 消耗账对不上 / 恢复重置了消耗 / 缺 `run/resumed` /
   ceiling 没真抬高 / 暂停前已有终态 / 产物与链状态不一致 / 悬空 tool call / 撞了 local
   fuse / live 流缺帧 —— 每一种都要判不通过；并且在一个自洽终态上全部通过；
3. **运行时目录被重定向进一次性根**（取证卫生的另一半）：轨迹必须落在
   `ctx.session_root` 下（runner 按它归档），且不得落到开发仓库。

（替身模型跑场景**不算** Live Gate 证据 —— 那种运行在 runner 那边会被 `seams` 锁到 FAIL；
本文件也不注册任何场景、不落盘证据。）
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_harness.config import Settings
from agent_harness.sandbox.local import LocalSubprocessSandbox
from evaluation.live_gate.registry import ScenarioContext
from evaluation.live_gate.scenarios.pause_resume import (
    DONE_FILE,
    LOW_CEILING,
    RESUME_CEILING,
    SEED_TOKEN,
    STEPS_FILE,
    TOKEN_FILE,
    TRANSITIONS,
    BudgetPauseResumeSameRunScenario,
    _event_text,
    _scenario_settings,
)

SCENARIO = BudgetPauseResumeSameRunScenario()
RUN_ID = "run-pause-1"
CHAIN_SCRIPT = "chain.py"
TOOL_CALL = "bash"


def _settings(**overrides: Any) -> Settings:
    """真实 `Settings`，但**不读仓库 `.env`**（`_env_file=None`）：单测不该把部署机的
    凭证与策略旋钮带进进程（`local_max_agent_turns` 正是被断言"逐字沿用"的字段之一）。"""
    return Settings(_env_file=None, **overrides)


def _context(tmp_path: Path, *, sandbox: Any | None = None,
             settings: Settings | None = None) -> ScenarioContext:
    return ScenarioContext(
        settings=settings if settings is not None else _settings(),
        sandbox=sandbox if sandbox is not None else _StubSandbox({}),
        session_root=tmp_path / "sessions",
        session_id="live-gate-pause-resume-a1",
        attempt_index=1,
    )


class _StubSandbox:
    """断言面替身：只实现 `read_text`（真沙箱在下面的链脚本用例里被真的跑过）。"""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    def read_text(self, name: str) -> str:
        if name not in self._files:
            raise FileNotFoundError(name)
        return self._files[name]


def _pause_payload(*, consumed: int = LOW_CEILING - 1, closeout: str = "model") -> dict:
    """`run/paused.data`（字段名是 `03 §3.4` 的契约：reason / trigger_dimension /
    budget_version / consumed / limits / continuation / closeout_source /
    resume_requirements）。

    `consumed.agent_turns` 的默认值是 `LOW_CEILING - 1`（= 1 个产出轮）：closeout 那次
    调用是 `model_requests`，**不**进这个 counter（`02 §5.1`），与 `closeout_source`
    是 model 还是 deterministic 无关。
    """
    return {
        "reason": "budget_exhausted",
        "trigger_dimension": "run.max_agent_turns_total",
        "budget_version": 1,
        "consumed": {"agent_turns": consumed},
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": LOW_CEILING},
        },
        "continuation": {
            "completed": ["已读初值"], "remaining": ["还有 3 格"],
            "blockers": [], "next_safe_action": "抬高 ceiling 后以同一 run_id 恢复",
        },
        "closeout_source": closeout,
        "resume_requirements": [],
        "trace_id": "trace-pause-1",
    }


def _resume_payload(*, consumed: int = LOW_CEILING - 1, run_limit: int = RESUME_CEILING) -> dict:
    """`run/resumed.data`（同 run 续跑：版本 +1、consumed 等于暂停快照、绝对 ceiling）。"""
    return {
        "from_pause_seq": 6,
        "previous_budget_version": 1,
        "budget_version": 2,
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": run_limit},
        },
        "consumed": {"agent_turns": consumed},
        "resume_basis": "budget_increase",
    }


def _events(
    *, pause_count: int = 1, resumed: bool = True,
    pause: dict | None = None, resume: dict | None = None,
    first_turn_terminal: bool = False,
) -> list[Any]:
    """自洽的事件序列：低预算执行（1 个产出轮；closeout 不落 `model/completed`）→ 恢复
    → 续跑完成。

    与真实轨迹同形：`run/started` 一条、`user/message` 一条、暂停后仍有 tool/call、
    `run/completed` 一条（同 run_id）。seq 由计数器顺序分配（改结构时不用手改数字）。
    """
    events: list[Any] = []
    seq = 0

    def add(event_type: str, data: dict | None = None) -> None:
        nonlocal seq
        events.append(
            SimpleNamespace(seq=seq, type=event_type, data=data or {}, run_id=RUN_ID, step_id=None)
        )
        seq += 1

    add("session/started")
    add("session/resumed")
    add("user/message", {"content": "chain task"})
    add("run/started", {"turn_index": 1,
                        "budget": {"run": {"max_agent_turns_total": LOW_CEILING}}})
    if first_turn_terminal:
        add("run/failed", {"reason": "boom"})
    add("model/completed", {"content": ""})
    pause_seq = seq
    for _ in range(pause_count):
        add("run/paused", pause if pause is not None else _pause_payload())
    if resumed:
        # `from_pause_seq` 恒指向暂停那一条（生产侧由 `paused.pause_seq` 直接给出，
        # 场景里再比对一次）：显式传 resume 的用例只改"某一处不对"的字段本身。
        payload = resume if resume is not None else _resume_payload()
        add("run/resumed", {**payload, "from_pause_seq": pause_seq})
    add("model/completed", {"content": ""})
    add("tool/call", {"tool_name": TOOL_CALL, "tool_call_id": "call-1"})
    add("tool/result", {"tool_call_id": "call-1"})
    add("model/completed", {"content": ""})
    add("model/completed", {"content": ""})
    add("run/completed", {"final_text": "链跑完了"})
    return events


def _with_payload(events: list[Any], event_type: str, payload: dict) -> list[Any]:
    """把某类事件的 data 换掉（其余字段不动）——用于"某一处不对"的反例。"""
    return [
        SimpleNamespace(seq=e.seq, type=e.type, data=payload, run_id=e.run_id, step_id=e.step_id)
        if e.type == event_type else e
        for e in events
    ]


def _assertions(
    tmp_path: Path, events: list[Any], *, steps: str = str(TRANSITIONS),
    done: str = "final-token", token: str = "final-token",
    streams: dict[str, list[str]] | None = None, replayed: list[Any] | None = None,
) -> list[Any]:
    """跑一次断言面（stub 沙箱承载"产物"这一面）。"""
    sandbox = _StubSandbox({STEPS_FILE: steps, DONE_FILE: done, TOKEN_FILE: token})
    ctx = _context(tmp_path, sandbox=sandbox)
    return SCENARIO._assertions(
        ctx=ctx, events=events,
        replayed=list(events) if replayed is None else replayed,
        tool_calls=[TOOL_CALL],
        streams=streams or {
            "pause": ["run/started", "model/completed", "run/paused"],
            "resume": ["model/completed", "run/completed"],
        },
    )


def _failed(assertions: list[Any]) -> set[str]:
    return {item.name for item in assertions if not item.ok}


# ── 链脚本（信息屏障）────────────────────────────────────────────────────


def test_chain_script_advances_only_with_the_current_token(tmp_path):
    """信息屏障：错串不推进（步数不动、串不变），对串推进一格并产出**新**随机串。"""
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    ctx = _context(tmp_path, sandbox=sandbox)
    assert asyncio.run(SCENARIO.prepare(ctx)) == []

    assert sandbox.read_text(TOKEN_FILE).strip() == SEED_TOKEN
    assert sandbox.read_text(STEPS_FILE).strip() == "0"

    wrong = sandbox.exec(f"python {CHAIN_SCRIPT} not-the-seed")
    assert wrong.exit_code != 0
    assert sandbox.read_text(STEPS_FILE).strip() == "0", "错串不得推进"
    assert sandbox.read_text(TOKEN_FILE).strip() == SEED_TOKEN, "错串不得改写当前串"

    ok = sandbox.exec(f"python {CHAIN_SCRIPT} {SEED_TOKEN}")
    assert ok.exit_code == 0
    assert "step=1" in ok.stdout
    assert sandbox.read_text(STEPS_FILE).strip() == "1"

    advanced = sandbox.read_text(TOKEN_FILE).strip()
    assert advanced != SEED_TOKEN
    assert "next=" in ok.stdout and advanced in ok.stdout, "新串只能从上一条输出里读出来"
    # 再拿**旧**串调用不推进：一次调用最多一格，且必须用最新串
    again = sandbox.exec(f"python {CHAIN_SCRIPT} {SEED_TOKEN}")
    assert again.exit_code != 0
    assert sandbox.read_text(STEPS_FILE).strip() == "1"


def test_prepare_reports_missing_python_as_unmet_precondition(tmp_path):
    """沙箱里没有 python ⇒ 前置不成立（BLOCKED 面），不是 FAIL，也不 seed 任何文件。"""

    class _NoPythonSandbox:
        def exec(self, command: str) -> SimpleNamespace:
            return SimpleNamespace(exit_code=1, stdout="", stderr="not found")

        def write_text(self, name: str, content: str) -> None:  # pragma: no cover
            raise AssertionError("前置不成立时不该 seed 任何文件")

    ctx = _context(tmp_path, sandbox=_NoPythonSandbox())
    problems = asyncio.run(SCENARIO.prepare(ctx))
    assert problems and "python" in problems[0]


# ── 运行时目录重定向（取证卫生）──────────────────────────────────────────


def test_runtime_dirs_are_redirected_into_the_attempt_root(tmp_path):
    """`workspace_dir` / `artifact_dir` 落在一次性根内；其余字段逐字沿用部署配置。

    轨迹落在 `<workspace_dir>/sessions/<session_id>/` 是 runner 归档证据的前提
    （`_copy_events` 从 `ctx.session_root/<session_id>` 取），而相对默认值会按进程 CWD
    落到开发仓库 —— 两条都要机械钉住。
    """
    original = _settings()
    ctx = _context(tmp_path, settings=original)
    effective = _scenario_settings(ctx)
    root = ctx.session_root.parent

    assert Path(effective.workspace_dir) == root
    assert Path(effective.workspace_dir) / "sessions" == ctx.session_root
    assert Path(effective.artifact_dir).is_relative_to(root)
    # 其余字段不变（模型链 / 策略旋钮 / 能力配置一个都没动）
    assert effective.model_provider == original.model_provider
    assert effective.model_name == original.model_name
    assert effective.local_max_agent_turns == original.local_max_agent_turns
    assert effective.capabilities == original.capabilities
    assert original.workspace_dir == Settings(_env_file=None).workspace_dir, "不得改到原对象"


# ── 断言面：自洽终态全绿 ─────────────────────────────────────────────────


def test_assertions_pass_on_a_self_consistent_sequence(tmp_path):
    """低预算暂停一次 → 同 run 恢复 → 完成：全部断言通过（场景不是恒 FAIL）。"""
    assertions = _assertions(tmp_path, _events())
    assert _failed(assertions) == set(), _failed(assertions)


def test_assertions_accept_deterministic_closeout(tmp_path):
    """closeout 回落到确定性组装（`closeout_source=deterministic`）时账不变。

    计数点是**被接纳的产出轮**：closeout 无论走模型还是确定性组装都是 `model_requests`
    （`02 §5.1`），所以同一份 `consumed` 在两种来源下都必须通过——这条和下面那条
    "把 closeout 加进 counter 就判红"合起来钉住这个语义。
    """
    events = _events(
        pause=_pause_payload(consumed=1, closeout="deterministic"),
        resume=_resume_payload(consumed=1),
    )
    assertions = _assertions(tmp_path, events)
    assert _failed(assertions) == set(), _failed(assertions)


# ── 断言面：每一种"假通过"都要判红 ───────────────────────────────────────


def test_second_pause_is_red(tmp_path):
    """续跑再次撞线（两条 run/paused）⇒ `pause_exactly_once` 红（不洗成通过）。"""
    assertions = _assertions(tmp_path, _events(pause_count=2))
    assert "pause_exactly_once" in _failed(assertions)


def test_consumed_counting_the_closeout_is_red(tmp_path):
    """把 closeout 也加进 `agent_turns`（旧写法）= 混同两个 counter ⇒ `pause_consumed_accounting` 红。

    这是"七个 counter 互不混同"（`02 §5.1`）在取证面上的守卫：真回归里若有人把
    closeout 记回这个 counter，低预算暂停的账会变成 `LOW_CEILING`（1 产出轮 + 1 次
    closeout），本用例如实判红。
    """
    events = _events(pause=_pause_payload(consumed=LOW_CEILING, closeout="model"))
    assert "pause_consumed_accounting" in _failed(_assertions(tmp_path, events))


def test_resume_resetting_consumed_is_red(tmp_path):
    """恢复把消耗重置成 0 ⇒ `resume_does_not_reset_consumed` 红（不变量：不重置）。"""
    events = _events(resume=_resume_payload(consumed=0))
    assert "resume_does_not_reset_consumed" in _failed(_assertions(tmp_path, events))


def test_missing_resume_event_is_red(tmp_path):
    """没有 `run/resumed`（等于"没恢复就接着跑"）⇒ 恢复快照与终态账本一律红。"""
    failed = _failed(_assertions(tmp_path, _events(resumed=False)))
    assert {"resume_same_run_snapshot", "resume_does_not_reset_consumed", "final_budget_state"} <= failed


def test_unraised_absolute_ceiling_is_red(tmp_path):
    """恢复给的绝对 ceiling 没真抬高 ⇒ 快照断言红（"恢复成功却什么都没发生"不得算过）。"""
    events = _events(resume=_resume_payload(run_limit=LOW_CEILING))
    assert "resume_same_run_snapshot" in _failed(_assertions(tmp_path, events))


def test_terminal_before_pause_is_red(tmp_path):
    """暂停之前就落了终态（暂停不再等于"本次执行收口"）⇒ `pause_is_not_terminal` 红。"""
    events = _events(first_turn_terminal=True)
    assert "pause_is_not_terminal" in _failed(_assertions(tmp_path, events))


def test_chain_and_artifact_mismatch_is_red(tmp_path):
    """链步数不足 / 产物内容与最终串不一致 ⇒ `chain_completed_after_resume` 红。"""
    assert "chain_completed_after_resume" in _failed(
        _assertions(tmp_path, _events(), steps=str(TRANSITIONS - 1))
    )
    assert "chain_completed_after_resume" in _failed(
        _assertions(tmp_path, _events(), done="stale-token")
    )


def test_dangling_tool_call_is_red(tmp_path):
    """悬空 tool call（有 call 无配对 result）⇒ 判红。"""
    events = list(_events())
    events.append(SimpleNamespace(
        seq=900, type="tool/call", data={"tool_name": TOOL_CALL, "tool_call_id": "call-orphan"},
        run_id=RUN_ID, step_id=None,
    ))
    assert "no_dangling_tool_calls" in _failed(_assertions(tmp_path, events))


def test_fuse_trip_is_red(tmp_path):
    """轨迹里出现 `max_steps_exceeded`（旧兜底终态）⇒ 判红。"""
    events = _with_payload(_events(), "run/completed", {"reason": "max_steps_exceeded"})
    assert "no_fuse_trip" in _failed(_assertions(tmp_path, events))


def test_stream_without_pause_or_completion_is_red(tmp_path):
    """live 流里没有 `run/paused`（或续跑流里没有 `run/completed`）⇒ 判红。"""
    assertions = _assertions(
        tmp_path, _events(),
        streams={"pause": ["run/started", "model/completed"], "resume": ["model/completed"]},
    )
    assert "stream_mirrors_pause_and_completion" in _failed(assertions)


def test_fresh_read_divergence_is_red(tmp_path):
    """重新读盘与内存态不一致（例：磁盘上少了恢复事实）⇒ `durable_replay_matches_live` 红。"""
    events = _events()
    replayed = [event for event in events if event.type != "run/resumed"]
    assert "durable_replay_matches_live" in _failed(
        _assertions(tmp_path, events, replayed=replayed)
    )


def test_malformed_pause_payload_is_red_not_crash(tmp_path):
    """畸形 `run/paused`（缺键）不得抛异常：如实判红，不伪造默认值。"""
    failed = _failed(_assertions(tmp_path, _events(pause={})))
    assert {
        "pause_reason_and_dimension", "pause_budget_snapshot", "pause_consumed_accounting",
        "pause_continuation_contract",
    } <= failed


def test_event_text_falls_back_when_str_itself_raises():
    """`_event_text` 的兜底分支：`json.dumps(default=str)` 都失败时退化成类型名，不抛穿。"""

    class _BadStr:
        def __str__(self) -> str:
            raise ValueError("no str for you")

    event = SimpleNamespace(type="run/completed", data={"x": _BadStr()})
    assert _event_text(event) == "run/completed"
