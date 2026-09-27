"""T9（`#317`）stuck 五模式 Live Gate 场景的**确定性**测试（不烧真实模型调用）。

真实 3/3 证据由 `python scripts/live_gate.py run --scenario stuck-tool-failure-pause`
产出并落进 `docs/live_gate/**`；本文件只钉**场景自身**的可机检事实，避免"跑了才知道
场景是不是恒 PASS / 恒 FAIL"：

1. **失败面真的成立**：read 工具对着一个**还不存在**的路径必然失败（`ok=false`），
   `prepare` 的反向前置（"上游产物此刻不能已经存在"）也要判 BLOCKED；
2. **断言集是诚实的**：串长不够 / 载荷计数与轨迹对不上 / 环境快照缺失 / 出现旧 HARD 终态
   形状 / 发了两条纠正 / 无依据恢复没被拒或改了事件流 / 恢复没记账 / 恢复腿没读到产物 /
   重放判不出同样的 paused 信号 —— 每一种都要判不通过；并且在一个自洽终态上全部通过；
3. **重放走的是生产 API**：`StuckDetector.from_events` + `advance` 重建后尾部只能产出
   一个 paused 信号（无 replan 残留、再喂一次为空）——AC 的 "restart and replay" 面。

（替身模型跑场景**不算** Live Gate 证据 —— 那种运行在 runner 那边会被 `seams` 锁到 FAIL；
本文件也不注册任何场景、不落盘证据。）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_harness.agent.budget import BudgetConflict
from agent_harness.agent.guards import STUCK_PATTERN_TOOL_FAILURE
from agent_harness.agent.run_budget import STUCK_RESUME_REQUIREMENTS
from agent_harness.config import Settings
from evaluation.live_gate.registry import ScenarioContext
from evaluation.live_gate.scenarios.accounting import request_accounting
from evaluation.live_gate.scenarios.stuck import (
    AWAITED_CONTENT,
    AWAITED_FILE,
    FAILED_TOOL,
    FAILURE_MARKER,
    IDENTICAL_FAILURES,
    SCENARIO,
    STEER_TEXT,
    TASK,
    StuckToolFailurePauseScenario,
    _identical_failure_streak,
    _Refusal,
    _scenario_settings,
)

RUN_ID = "run-stuck-1"
SESSION_ID = "live-gate-stuck-a1"
TOOL_ERROR_CODE = "TOOL_EXECUTION_ERROR"
#: 每格请求自报的账（数字本身无关紧要：断言比的是"与轨迹重算相同"）。
TOKENS_PER_REQUEST = 7
COST_PER_REQUEST = "0.01"
#: 合成轨迹里的两个快照（形状与生产同源：`sha256:` 前缀 + 十六进制）。
ENVIRONMENT_REVISION = "sha256:0f1e2d3c4b5a6978"
POLICY_VERSION = "sha256:8899aabbccddeeff"


# ── 替身沙箱 / 上下文 ────────────────────────────────────────────────────


class _StubSandbox:
    """`prepare` 与断言面的替身：只实现本场景真正用到的那几格。

    真沙箱在 Live Gate 运行里由 runner 提供；这里用到的是 `read_text`（前置自查 +
    断言读产物）、`write_text`（"上游就绪"那一次写入）、`workspace_root`。
    """

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self._files = dict(files or {})
        self.written: dict[str, str] = {}
        self.workspace_root = "/workspace"

    def write_text(self, name: str, content: str) -> None:
        self.written[name] = content
        self._files[name] = content

    def read_text(self, name: str) -> str:
        if name not in self._files:
            raise FileNotFoundError(name)
        return self._files[name]


def _settings(**overrides: Any) -> Settings:
    """真实 `Settings`，但**不读仓库 `.env`**（`_env_file=None`）：单测不该把部署机的
    凭证与策略旋钮带进进程。"""
    return Settings(_env_file=None, **overrides)


def _context(tmp_path: Path, *, sandbox: Any | None = None) -> ScenarioContext:
    return ScenarioContext(
        settings=_settings(),
        sandbox=sandbox if sandbox is not None else _StubSandbox(),
        session_root=tmp_path / "sessions",
        session_id=SESSION_ID,
        attempt_index=1,
        injected_failure="",
    )


# ── 合成轨迹（与真实落盘形状同形）────────────────────────────────────────


def _failure_content() -> str:
    """失败 `tool/result.content`：生产 ReadTool 对不存在的文件的原文形状。"""
    return json.dumps({
        "ok": False,
        "error_code": TOOL_ERROR_CODE,
        "message": f"文件 '{AWAITED_FILE}' 不存在。",
        "data": None,
    })


def _read_success_content() -> str:
    """成功 `tool/result.content`：`data.content` 里带着产物的正文。"""
    return json.dumps({
        "ok": True,
        "error_code": None,
        "message": f"已读取 '{AWAITED_FILE}'（{len(AWAITED_CONTENT.splitlines())} 行）。",
        "data": {
            "path": AWAITED_FILE,
            "content": AWAITED_CONTENT,
            "total_lines": len(AWAITED_CONTENT.splitlines()),
        },
    })


def _consumed(facts: Any, **overrides: Any) -> dict[str, Any]:
    """暂停快照的 `consumed`：默认值由**轨迹重算**得出（"如实记账"那一侧永远绿）。"""
    consumed: dict[str, Any] = {
        "agent_turns": facts.turns,
        "model_requests": facts.requests,
        "total_tokens": facts.tokens,
        "cost_usd": None if facts.cost is None else format(facts.cost, "f"),
    }
    consumed.update(overrides)
    return consumed


def _refusal(*, raised: BaseException | None, events_unchanged: bool = True,
             expected: str = "相同") -> _Refusal:
    """一条"恢复必须被拒"的读数（用真实的 `_Refusal`：判据就是它自己的那一份）。"""
    return _Refusal(raised=raised, events_unchanged=events_unchanged, expected=expected)


def _trajectory(
    *,
    failures: int = IDENTICAL_FAILURES,
    soft_guard: bool = True,
    hard_guard: bool = False,
    second_corrective: bool = False,
    consumed_overrides: dict[str, Any] | None = None,
    stuck_overrides: dict[str, Any] | None = None,
    resumed: bool = True,
    resume_read_ok: bool = True,
    resume_evidence_overrides: dict[str, Any] | None = None,
    bad_refusal: str | None = None,
    terminal: bool = True,
    extra_events: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """自洽的事件序列：6 次同动作同失败（第 3 次一条纠正）→ stuck 暂停 → 相关 steer → 同 run 恢复。

    结构逐格对齐生产落盘：`run/started` → `user/message` → 每轮
    `model/request` + `model/completed` + `tool/call` + `tool/result` → 暂停前
    `guard/stuck(level=paused)` → `run/paused` → `steer/requested` → `run/resumed`
    → 恢复那条腿 read 成功 → `run/completed`。seq 由计数器顺序分配。

    反例用例只改"某一处不对"的那一格（见各参数）；`consumed` 与 `stuck` 的默认值由
    轨迹重算 / 常量拼出，所以"自洽"那一侧不靠手抄数字。
    """
    events: list[Any] = []
    seq = 0

    def add(event_type: str, data: dict[str, Any] | None = None) -> None:
        nonlocal seq
        events.append(
            SimpleNamespace(seq=seq, type=event_type, data=data or {}, run_id=RUN_ID, step_id=None)
        )
        seq += 1

    def request(role: str = "primary") -> None:
        add("model/request", {
            "role": role, "outcome": "completed",
            "usage": {"total_tokens": TOKENS_PER_REQUEST},
            "cost_usd": COST_PER_REQUEST,
        })

    def decide(round_index: int, *, args: dict[str, Any], tool_call_id: str) -> None:
        request()
        add("model/completed", {
            "content": f"第 {round_index} 轮：读取 {AWAITED_FILE}",
            "tool_calls": [{"id": tool_call_id, "name": FAILED_TOOL, "args": args}],
        })

    add("session/started")
    add("run/started", {"turn_index": 1, "budget": {"run": {"max_agent_turns_total": 500}}})
    add("user/message", {"content": TASK})
    for index in range(1, failures + 1):
        call_id = f"call-{index}"
        decide(index, args={"path": AWAITED_FILE}, tool_call_id=call_id)
        add("tool/call", {
            "tool_call_id": call_id, "tool_name": FAILED_TOOL,
            "args": {"path": AWAITED_FILE},
        })
        add("tool/result", {"tool_call_id": call_id, "content": _failure_content()})
        if index == 3 and soft_guard:
            add("tool/failure-guard", {
                "level": "soft", "pattern": STUCK_PATTERN_TOOL_FAILURE, "count": 3,
                "threshold": 3, "tool_name": FAILED_TOOL,
            })
            add("user/message", {
                "content": "不要再以相同方式重试同一个工具调用。",
                "injected_by": "tool_failure_guard",
            })
    if second_corrective:
        # 第二个模式在同一次检测里也要一条纠正：契约只允许一条（ADR-0048 D5）。
        add("guard/stuck", {
            "level": "replan", "pattern": STUCK_PATTERN_TOOL_FAILURE, "count": failures,
            "threshold": 3, "replan_count": 1,
        })
        add("user/message", {
            "content": "换个做法。", "injected_by": "stuck_guard",
        })
    if hard_guard:
        add("tool/failure-guard", {
            "level": "hard", "pattern": STUCK_PATTERN_TOOL_FAILURE, "count": failures,
            "threshold": 3, "tool_name": FAILED_TOOL,
        })
    stuck = {
        "pattern": STUCK_PATTERN_TOOL_FAILURE,
        "threshold": 3,
        "count": failures,
        "replan_count": 1,
        "fingerprint": "sha256:deadbeefcafe0001",
        "environment_revision": ENVIRONMENT_REVISION,
        "policy_version": POLICY_VERSION,
    }
    stuck.update(stuck_overrides or {})
    add("guard/stuck", {"level": "paused", **stuck, "tool_name": FAILED_TOOL})
    pause_index = len(events)
    add("run/paused", {})  # 占位：`consumed` 要用暂停**之前**的轨迹重算，见下

    steer_seq: int | None = None
    if resumed:
        add("steer/requested", {
            "steer_id": "steer-live-gate", "content": STEER_TEXT,
            "created_at": "2026-09-27T00:00:00Z",
        })
        steer_seq = events[-1].seq
        evidence: dict[str, Any] = {
            "basis": "relevant_steer",
            "steer_seq": steer_seq,
            "pause_seq": pause_index,
            "environment_revision": ENVIRONMENT_REVISION,
            "policy_version": POLICY_VERSION,
        }
        evidence.update(resume_evidence_overrides or {})
        add("run/resumed", {
            "run_id": RUN_ID, "from_pause_seq": pause_index, "version": 2,
            "resume_basis": "relevant_steer", "resume_evidence": evidence,
        })
        decide(7, args={"path": AWAITED_FILE}, tool_call_id="call-7")
        add("tool/call", {
            "tool_call_id": "call-7", "tool_name": FAILED_TOOL,
            "args": {"path": AWAITED_FILE},
        })
        add("tool/result", {
            "tool_call_id": "call-7",
            "content": _read_success_content() if resume_read_ok else _failure_content(),
        })
        request()
        add("model/completed", {"content": f"读到 {AWAITED_CONTENT!r}", "tool_calls": []})
    for event_type, data in extra_events or []:
        add(event_type, data)
    if terminal and not any(event.type == "run/completed" for event in events):
        add("run/completed", {"final_text": "报告完成"})

    facts = request_accounting(events[: pause_index + 1])
    events[pause_index] = SimpleNamespace(
        seq=pause_index, type="run/paused", run_id=RUN_ID, step_id=None,
        data={
            "reason": "stuck",
            "trigger_dimension": STUCK_PATTERN_TOOL_FAILURE,
            "budget_version": 1,
            "consumed": _consumed(facts, **(consumed_overrides or {})),
            "limits": {"run": {"max_agent_turns_total": 500}},
            "continuation": {
                "completed": ["6 次重试"], "remaining": ["上游就绪后读上游产物"],
                "blockers": [], "next_safe_action": "上游就绪后带相关 steer 以同一 run_id 恢复",
            },
            "closeout_source": "model",
            "resume_requirements": list(STUCK_RESUME_REQUIREMENTS),
            "stuck": dict(stuck),
        },
    )
    refusal = _refusal(
        raised=(
            None if bad_refusal == "unraised"
            else BudgetConflict("暂停快照的 environment_revision 与现场相同，无法作为依据")
        ),
        events_unchanged=bad_refusal != "changed",
    )
    return events, {
        "pause": events[pause_index],
        "pause_seq": pause_index,
        "steer_seq": steer_seq,
        "refusals": {
            "budget_increase": _refusal(
                raised=BudgetConflict("stuck 暂停不接受 resume_basis=budget_increase"),
                expected="budget_increase",
            ),
            "relevant_steer_without_a_steer": _refusal(
                raised=BudgetConflict("没有比暂停快照更新的相关 steer"), expected="没有",
            ),
            "environment_change_unchanged": refusal,
            "policy_change_unchanged": refusal,
            "environment_change_outside_the_workspace": refusal,
        },
    }


def _assertions(
    tmp_path: Path,
    events: list[Any],
    legs: dict[str, Any] | None = None,
    *,
    replayed: list[Any] | None = None,
    projection: dict[str, Any] | None = None,
    sandbox: Any | None = None,
) -> list[Any]:
    ctx = _context(
        tmp_path,
        sandbox=sandbox if sandbox is not None else _StubSandbox({AWAITED_FILE: AWAITED_CONTENT}),
    )
    return SCENARIO._assertions(
        ctx=ctx,
        events=events,
        replayed=list(events) if replayed is None else replayed,
        tool_calls=[FAILED_TOOL] * len(events),
        projection={"reason": None} if projection is None else projection,
        operations=[],
        legs=legs if legs is not None else {},
    )


def _legs(legs: dict[str, Any]) -> dict[str, Any]:
    """断言面读的 `legs`（真实运行由 `run()` 填；这里只放断言真正消费的几格）。"""
    return {
        "refusals": legs["refusals"],
        "steer_seq": legs["steer_seq"],
        "awaited_file_written": AWAITED_CONTENT,
    }


def _failed(assertions: list[Any]) -> set[str]:
    return {item.name for item in assertions if not item.ok}


# ── prepare：失败面的前提 ───────────────────────────────────────────────


def test_prepare_passes_when_the_awaited_file_is_absent(tmp_path):
    """上游产物此刻不存在 = 本场景的前提成立（第一次 read 必然失败）。"""
    sandbox = _StubSandbox()
    unmet = asyncio.run(SCENARIO.prepare(_context(tmp_path, sandbox=sandbox)))
    assert unmet == []
    assert sandbox.written == {}


def test_prepare_reports_an_already_present_awaited_file(tmp_path):
    """产物已经在了 ⇒ 第一次 read 就会成功、① 永远数不到 ⇒ BLOCKED（不是 FAIL）。"""
    sandbox = _StubSandbox({AWAITED_FILE: AWAITED_CONTENT})
    unmet = asyncio.run(SCENARIO.prepare(_context(tmp_path, sandbox=sandbox)))
    assert len(unmet) == 1 and AWAITED_FILE in unmet[0]


def test_runtime_dirs_are_redirected_into_the_attempt_root(tmp_path):
    """`workspace_dir` / `artifact_dir` 落在一次性根内；其余字段逐字沿用部署配置。

    轨迹落在 `<workspace_dir>/sessions/<session_id>/` 是 runner 归档证据的前提，而相对
    默认值会按进程 CWD 落到开发仓库 —— 两条都要机械钉住。
    """
    original = _settings()
    ctx = _context(tmp_path)
    settings = _scenario_settings(ctx)
    root = ctx.session_root.parent
    assert Path(settings.workspace_dir) == root
    assert Path(settings.workspace_dir) / "sessions" == ctx.session_root
    assert Path(settings.artifact_dir) == root / "artifacts"
    # 其余字段逐字沿用部署配置（模型链 / 策略旋钮一个都没动，原对象也不被改写）。
    assert settings.model_provider == original.model_provider
    assert settings.model_name == original.model_name
    assert settings.local_max_agent_turns == original.local_max_agent_turns
    assert settings.capabilities == original.capabilities
    assert original.workspace_dir == Settings(_env_file=None).workspace_dir


# ── 任务文本与独立重算的小工具 ──────────────────────────────────────────


def test_task_text_names_the_action_and_the_repeat_count(tmp_path):
    """任务文本必须把"哪个工具、哪个路径、读几次、别换招"写全——这是场景的**前提**。"""
    assert IDENTICAL_FAILURES == 6
    assert FAILED_TOOL in TASK and AWAITED_FILE in TASK
    assert f"读 {IDENTICAL_FAILURES} 次" in TASK
    assert "bash" in TASK and "不要换别的工具" in TASK
    assert AWAITED_FILE in STEER_TEXT and FAILED_TOOL in STEER_TEXT


def test_failure_marker_matches_the_production_read_tool_message():
    """判据子串必须出现在生产读工具对缺失文件的原文里（形状不是场景自己编的）。

    真源：`tools/read.py` 的 `FileNotFoundError` 分支
    （`ToolResult.failure(message=f"文件 '{path}' 不存在。", TOOL_EXECUTION_ERROR)`）。
    """
    from agent_harness.tools.read import ReadTool

    class _Missing:
        def read_text(self, name: str) -> str:
            raise FileNotFoundError(name)

    args = ReadTool(_Missing()).args_schema(path=AWAITED_FILE)
    result = asyncio.run(ReadTool(_Missing()).execute(args))
    assert result.ok is False
    assert result.error_code == "TOOL_EXECUTION_ERROR"
    assert FAILURE_MARKER in (result.message or "")


def test_streak_helper_keys_on_the_tool_and_the_args():
    """场景侧的独立重算：换了参数就算另一条动作（与产品的 action_fingerprint 同口径）。"""
    events: list[Any] = []
    seq = 0

    def add(event_type: str, **data: Any) -> None:
        nonlocal seq
        events.append(SimpleNamespace(seq=seq, type=event_type, data=data, run_id=RUN_ID))
        seq += 1

    def call_and_fail(call_id: str, path: str) -> None:
        add("tool/call", tool_call_id=call_id, tool_name=FAILED_TOOL, args={"path": path})
        add("tool/result", tool_call_id=call_id, content=_failure_content())

    call_and_fail("c1", AWAITED_FILE)
    call_and_fail("c2", AWAITED_FILE)
    call_and_fail("c3", AWAITED_FILE)
    call_and_fail("c4", "other.txt")  # 换路径 ⇒ 断开这条串
    call_and_fail("c5", "other.txt")
    streak, label = _identical_failure_streak(events, cutoff_seq=None)
    assert streak == 3
    assert AWAITED_FILE in label


# ── 断言集：自洽终态全绿 ────────────────────────────────────────────────


def test_assertions_pass_on_a_self_consistent_trajectory(tmp_path):
    events, legs = _trajectory()
    assertions = _assertions(tmp_path, events, _legs(legs))
    assert _failed(assertions) == set()
    names = {item.name for item in assertions}
    assert {
        "the_model_really_repeated_one_identical_action",
        "every_repeat_failed_the_same_deterministic_way",
        "exactly_one_corrective_replan",
        "stuck_pause_is_recorded_with_the_evidence_snapshot",
        "pause_is_not_a_terminal_event",
        "replay_reconstructs_the_same_verdict",
        "resume_records_the_accepted_basis",
        "resumed_leg_read_the_awaited_artifact",
        "stuck 暂停快照.request_count",
    } <= names


def test_replay_verdict_is_green_on_the_self_consistent_trajectory(tmp_path):
    events, legs = _trajectory()
    pause = legs["pause"]
    stuck = pause.data["stuck"]
    result = SCENARIO._replay_verdict(
        events=events, pause=pause, run_id=RUN_ID,
        pattern=stuck["pattern"], count=stuck["count"], threshold=stuck["threshold"],
    )
    assert result.ok, result.detail
    assert "paused 信号=[('stuck.tool_failure_loop', 6, 3)]" in result.detail


def test_replay_is_filtered_by_run_id(tmp_path):
    """重放必须按 run_id 过滤：换了 id 就重建不出判定（否则"重建"是空话）。"""
    events, legs = _trajectory()
    pause = legs["pause"]
    stuck = pause.data["stuck"]
    result = SCENARIO._replay_verdict(
        events=events, pause=pause, run_id="run-other",
        pattern=stuck["pattern"], count=stuck["count"], threshold=stuck["threshold"],
    )
    assert not result.ok


# ── 断言集：每一处不对都要判红 ──────────────────────────────────────────


def test_fewer_repeats_than_two_thresholds_is_red(tmp_path):
    events, legs = _trajectory(failures=5)
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "the_model_really_repeated_one_identical_action" in failed


def test_count_mismatching_the_trajectory_is_red(tmp_path):
    events, legs = _trajectory(stuck_overrides={"count": 7})
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "stuck_pause_is_recorded_with_the_evidence_snapshot" in failed


def test_missing_environment_snapshot_is_red(tmp_path):
    events, legs = _trajectory(stuck_overrides={"environment_revision": None})
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "stuck_pause_is_recorded_with_the_evidence_snapshot" in failed


def test_old_hard_terminal_shape_is_red(tmp_path):
    """旧 HARD 臂（`run/failed(identical_tool_failure_loop)`）已从生产路径移除。"""
    events, legs = _trajectory(
        extra_events=[("run/failed", {"reason": "identical_tool_failure_loop"})],
    )
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "pause_is_not_a_terminal_event" in failed


def test_two_correctives_are_red(tmp_path):
    events, legs = _trajectory(second_corrective=True)
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "exactly_one_corrective_replan" in failed


def test_hard_guard_event_is_red(tmp_path):
    events, legs = _trajectory(hard_guard=True)
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "exactly_one_corrective_replan" in failed


def test_consumed_snapshot_off_by_one_is_red(tmp_path):
    events, legs = _trajectory(consumed_overrides={"model_requests": 99})
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "stuck 暂停快照.request_count" in failed


def test_a_refusal_that_did_not_raise_is_red(tmp_path):
    events, legs = _trajectory(bad_refusal="unraised")
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "every_resume_without_evidence_is_a_409_with_zero_side_effects" in failed


def test_a_refusal_that_wrote_events_is_red(tmp_path):
    events, legs = _trajectory(bad_refusal="changed")
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "every_resume_without_evidence_is_a_409_with_zero_side_effects" in failed


def test_missing_refusals_are_red(tmp_path):
    events, _ = _trajectory()
    failed = _failed(_assertions(tmp_path, events, {"refusals": {}, "steer_seq": None}))
    assert "every_resume_without_evidence_is_a_409_with_zero_side_effects" in failed


def test_resume_without_a_resumed_event_is_red(tmp_path):
    events, legs = _trajectory(resumed=False)
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "resume_records_the_accepted_basis" in failed


def test_resume_basis_mismatch_is_red(tmp_path):
    events, legs = _trajectory(resume_evidence_overrides={"basis": "budget_increase"})
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "resume_records_the_accepted_basis" in failed


def test_steer_before_the_pause_is_red(tmp_path):
    """相关 = 暂停之后的 steer：seq 不晚于暂停点的 steer 不算依据。"""
    events, legs = _trajectory()
    legs = dict(legs)
    legs["steer_seq"] = 0
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "resume_records_the_accepted_basis" in failed


def test_resumed_leg_that_never_read_the_artifact_is_red(tmp_path):
    """恢复那条腿"多打了一次请求"不算进展：产物内容真的读到才算（AC 的"继续干活"）。"""
    events, legs = _trajectory(resume_read_ok=False)
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert failed == {"resumed_leg_read_the_awaited_artifact"}


def test_local_fuse_terminal_is_red(tmp_path):
    events, legs = _trajectory()
    events = [
        SimpleNamespace(
            seq=event.seq, type=event.type, run_id=event.run_id, step_id=event.step_id,
            data={"reason": "max_steps_exceeded"},
        ) if event.type == "run/completed" else event
        for event in events
    ]
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "no_local_fuse_terminal" in failed


def test_dangling_tool_call_at_the_pause_is_red(tmp_path):
    """暂停那一刻还有"有调用无结果"的 id ⇒ 不是安全的暂停点（12 §9 的初始 Gate）。"""
    events, legs = _trajectory()
    at = legs["pause_seq"]
    events.insert(at, SimpleNamespace(
        seq=at, type="tool/call", run_id=RUN_ID, step_id=None,
        data={"tool_call_id": "call-dangling", "tool_name": FAILED_TOOL,
              "args": {"path": AWAITED_FILE}},
    ))
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert failed == {"no_dangling_tool_call_at_the_pause"}


def test_resumed_leg_may_end_in_a_pause_without_a_crash(tmp_path):
    """恢复那条腿若又走成暂停（没有 `run/completed`），判定照旧给结论、不抛异常。

    这一条钉的是一个真实踩过的缺陷：判据里引用了未定义的局部名，只有在"完成了"那一
    侧才被短路掩盖（`or` 的右侧不求值）——所以两条路都要有用例。
    """
    events, legs = _trajectory(terminal=False)
    assertions = _assertions(tmp_path, events, _legs(legs))
    assert _failed(assertions) == set()


def test_missing_terminal_event_is_red_when_there_is_no_pause(tmp_path):
    """连暂停都没有（既没完成也没暂停）⇒ 不能算"安全收口"。"""
    events, legs = _trajectory(terminal=False)
    events = [
        event for event in events
        if event.type not in ("run/paused", "guard/stuck")
    ]
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert "resumed_leg_ended_safely" in failed


def test_replay_rejects_a_verdict_that_does_not_match_the_payload(tmp_path):
    """重放出的计数与载荷不一致 ⇒ 红（"重建"必须与当时落盘的那份判定逐项相同）。"""
    events, legs = _trajectory(soft_guard=False, failures=6)
    pause = legs["pause"]
    stuck = pause.data["stuck"]
    result = SCENARIO._replay_verdict(
        events=events, pause=pause, run_id=RUN_ID,
        pattern=stuck["pattern"], count=stuck["count"], threshold=stuck["threshold"],
    )
    # 没有 durable 的纠正事件时，重放**仍**能从计数重建"已纠正过"（计数越过 T 那次
    # 自己的信号也会置闩）——所以这一条本身不判红；判红的是口径不一致的那一种。
    assert result.ok

    broken = SCENARIO._replay_verdict(
        events=events, pause=pause, run_id=RUN_ID,
        pattern=stuck["pattern"], count=stuck["count"] + 1, threshold=stuck["threshold"],
    )
    assert not broken.ok


def test_malformed_pause_payload_is_red_not_crash(tmp_path):
    events, legs = _trajectory()
    events = [
        SimpleNamespace(
            seq=event.seq, type=event.type, run_id=event.run_id, step_id=event.step_id,
            data={},
        ) if event.type == "run/paused" else event
        for event in events
    ]
    failed = _failed(_assertions(tmp_path, events, _legs(legs)))
    assert {
        "stuck_pause_is_recorded_with_the_evidence_snapshot",
        "stuck 暂停快照.request_count",
    } <= failed


# ── 场景对象自身 ────────────────────────────────────────────────────────


def test_scenario_identity_and_description():
    assert isinstance(SCENARIO, StuckToolFailurePauseScenario)
    assert SCENARIO.id == "stuck-tool-failure-pause"
    assert SCENARIO.version == 2
    assert "stuck" in SCENARIO.description and "409" in SCENARIO.description
