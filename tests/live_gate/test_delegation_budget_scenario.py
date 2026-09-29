"""#396 delegation-budget-tree-wide Live Gate 场景的**确定性**专项单测（不烧真实模型调用）。

真实 3/3 证据由 `python scripts/live_gate.py run --scenario delegation-budget-tree-wide`
产出并落进 `docs/live_gate/**`；本文件只钉**场景自身**的可机检事实，避免"跑了才知道
场景是不是恒 PASS / 恒 FAIL"：

1. **断言集是诚实的**：受纳数错（如 7 受纳）/ 尝试不足 9 次 / run 提前 failed / 暂停
   reason·trigger 改错 / 恢复接错暂停 / 投影与事件面不符 / child 未完成 / 悬空
   delegate 调用 / 重读不一致 / live 流缺帧 —— 每一种都要判不通过；并且在一个自洽
   轨迹上全部通过（断言集一条不多一条不少）；
2. **端到端（替身只替模型客户端一处接缝）**：脚本化模型经生产 `resume_and_launch` →
   `assembly.build_runtime` 装配真实父子委派（真 SQLite 树账、真 delegate 工具、真
   InProcess 子代理）：低 ceiling 结构性暂停一次 → 续跑，恰好 8 次受纳、第 9 次在
   子代理执行之前被拒（拒绝回填带「预算耗尽 8/8」）⇒ 断言集全绿；模型只发起 8 次
   尝试就收尾 ⇒ 场景必须判红（破坏契约即红）；
3. **registry 路径**：`delegation-budget-tree-wide` 已注册为 builtin（`_is_shipped`
   核实过定义文件在 `scenarios/` 下）。

（替身模型跑场景**不算** Live Gate 证据 —— runner 的 `seams` 把那种运行锁到 FAIL；
本文件不落盘证据，registry 用例只做幂等注册，与 `test_live_gate_registry.py` 同规。）

## 与票面设想的两处偏差（起草时核实）

- **prepare 没有"前置不满足"分支**：本场景 `prepare` 恒返回 `[]`（无 seed 文件、无
  沙箱自查），"BLOCKED 语义"用例只能钉"恒空、不碰沙箱"，不存在反例分支；
- **`injected_failure` 无注入面**：本场景不读 `ctx.injected_failure`（对照
  pause_resume 的 `PRIMARY_FAILURE_INJECTION`），草稿不为其造用例。

## 替身接缝的位置（与 test_completion 的差别）

completion 场景自己手搭 Runtime（惰性 `from agent_harness.model.provider import
create_chat_model`，接缝在任何 import 顺序下都有效）；本场景走 `service.
resume_and_launch` → `assembly.build_runtime` ⇒ 接缝必须打在
**`agent_harness.assembly.create_chat_model`**（模块级绑定，import 顺序无关）——
只 patch provider 模块属性仅在"assembly 尚未被 import"时碰巧生效（full-suite 里
`tests/test_assembly.py` / `tests/test_web_api.py` 的收集就会提前 import assembly）。
脚本化模型按**历史计数**决策（数消息里 delegate tool_calls），跨暂停/恢复的两个
launch 各建一个实例也天然连续；closeout（`_raw_model.ainvoke` + 指令 user 消息）
与子代理任务（`子任务 N：…`）按最后一条 human 消息路由。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent_harness.agent.run_budget import (
    REASON_BUDGET_EXHAUSTED,
    RESUME_BASIS_BUDGET_INCREASE,
    TRIGGER_RUN_TURNS,
)
from agent_harness.config import Settings
from agent_harness.sandbox.local import LocalSubprocessSandbox
from agent_harness.session.store import JsonlSessionStore
from evaluation.live_gate.registry import ScenarioContext, get_scenario, is_builtin
from evaluation.live_gate.scenarios import BUILTIN_SCENARIOS, register_builtin_scenarios
from evaluation.live_gate.scenarios.delegation_budget import (
    BUDGET_EXHAUSTED_MARKER,
    DELEGATE_TOOL,
    LOW_CEILING,
    RESUME_CEILING,
    SCENARIO,
    SCENARIO_ID,
    SESSION_MAX_DELEGATIONS,
    SUBTASKS,
    TARGET_PROFILE,
    TASK,
    _scenario_settings,
)

RUN_ID = "run-delegation-1"
SESSION_ID = "live-gate-delegation-a1"

#: 断言名清单（多一条 / 少一条都要被发现：断言集变了就是判据变了）。
EXPECTED_ASSERTIONS = {
    "pause_exactly_once_structural",
    "resume_same_run_without_session_dims",
    "run_completed_without_terminal_noise",
    "all_subtask_attempts_made",
    "accepted_delegations_capped_across_runs",
    "over_budget_rejection_before_child_execution",
    "children_are_real_and_completed",
    "session_row_projection_agrees",
    "stream_mirrors_pause_and_completion",
    "durable_replay_matches_live",
    "no_dangling_tool_calls",
    "session_identity_present",
}


def _settings(**overrides: Any) -> Settings:
    """真实 `Settings`，但**不读仓库 `.env`**（单测不把部署机凭证带进进程）。"""
    return Settings(_env_file=None, **overrides)


def _context(tmp_path: Path, *, sandbox: Any | None = None,
             settings: Settings | None = None) -> ScenarioContext:
    return ScenarioContext(
        settings=settings if settings is not None else _settings(),
        sandbox=sandbox if sandbox is not None else _StubSandbox({}),
        session_root=tmp_path / "sessions",
        session_id=SESSION_ID,
        attempt_index=1,
        injected_failure="",
    )


class _StubSandbox:
    """断言面替身：本场景的 `_assertions` 不读沙箱，这里只占位（并守"prepare 不写盘"）。"""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.workspace_root = "/workspace"

    def write_text(self, name: str, content: str) -> None:  # pragma: no cover
        raise AssertionError(f"prepare 不该写任何文件（收到 {name!r}）")

    def exec(self, command: str) -> Any:  # pragma: no cover
        raise AssertionError(f"prepare 不该执行命令（收到 {command!r}）")

    def read_text(self, name: str) -> str:
        if name not in self._files:
            raise FileNotFoundError(name)
        return self._files[name]


# ── 场景对象 / 任务文本 / 常数关系 ────────────────────────────────────────


def test_scenario_identity():
    """场景 id / 版本 / 描述稳定：id 是 runner 的 `--scenario` 锚点。"""
    assert SCENARIO.id == SCENARIO_ID == "delegation-budget-tree-wide"
    assert isinstance(SCENARIO.version, int) and SCENARIO.version >= 1
    assert "8" in SCENARIO.description and "第 9 次" in SCENARIO.description


def test_task_text_names_the_audit_contract():
    """任务书把"逐个实际发起全部 9 次委派尝试"写成审计要求——拒绝证据的来源。"""
    assert DELEGATE_TOOL in TASK
    assert TARGET_PROFILE in TASK
    assert "9 个相互独立的子任务" in TASK
    assert "全部 9 次委派尝试" in TASK, "没有审计要求，守规矩的模型 8 次后停手 ⇒ 采集不到拒绝"
    assert "预算耗尽" in TASK, "被拒后的处置（记录、跳过、继续）要写进任务书"


def test_constants_relate_structurally():
    """常数关系即结构论证：首轮必暂停、第 9 次必越线、续跑装得下剩余轮次。"""
    assert LOW_CEILING == 2, "consumed+1>=ceiling ⇒ 首轮之后结构性暂停"
    assert SUBTASKS == SESSION_MAX_DELEGATIONS + 1, "第 9 次尝试必然越线（拒绝是结构结果）"
    assert RESUME_CEILING >= SUBTASKS + 1, "首轮 1 轮 + 剩余 8 次尝试 + 收尾 ≥ SUBTASKS+1 轮"


# ── prepare 与取证卫生 ───────────────────────────────────────────────────


def test_prepare_needs_no_seeds_and_touches_nothing(tmp_path):
    """本场景无 seed 文件 ⇒ prepare 恒返回 []，且不写沙盘、不执行命令（无 BLOCKED 分支）。"""
    assert asyncio.run(SCENARIO.prepare(_context(tmp_path))) == []


def test_runtime_dirs_are_redirected_and_multiagent_is_declared(tmp_path):
    """`workspace_dir`/`artifact_dir` 落进一次性根；multiagent 显式并进 CAPABILITIES。

    multiagent 是本场景的受试面：部署没配时场景自己声明（provider=builtin）；其余
    能力逐字保留；模型链字段逐字沿用部署配置；原 settings 对象不被改写。
    """
    original = _settings()
    ctx = _context(tmp_path, settings=original)
    effective = _scenario_settings(ctx)
    root = ctx.session_root.parent

    assert Path(effective.workspace_dir) == root
    assert Path(effective.workspace_dir) / "sessions" == ctx.session_root
    assert Path(effective.artifact_dir) == root / "artifacts"
    from agent_harness.capability.config import parse_capabilities_config

    config = parse_capabilities_config(effective.capabilities)
    assert "multiagent" in config, "受试面必须显式在场"
    assert config["multiagent"].provider == "builtin"
    assert config["multiagent"].enabled is True
    # 模型链 / 策略旋钮一个都没动
    assert effective.model_provider == original.model_provider
    assert effective.model_name == original.model_name
    assert effective.local_max_agent_turns == original.local_max_agent_turns
    assert original.workspace_dir == Settings(_env_file=None).workspace_dir, "不得改到原对象"


# ── 合成轨迹（与真实落盘同形）与投影 ─────────────────────────────────────


def _success_content(summary: str) -> str:
    """成功 `tool/result.content`：executor 落盘的是 ToolResult JSON（含 message）。"""
    return json.dumps({
        "ok": True, "error_code": None,
        "message": f"子代理 '{TARGET_PROFILE}' 完成：{summary}",
        "data": {"output": json.dumps(
            {"agent_id": TARGET_PROFILE, "status": "completed", "summary": summary},
            ensure_ascii=False,
        )},
    }, ensure_ascii=False)


def _rejected_content() -> str:
    """配额拒绝的 `tool/result.content`：DelegateTool 拒绝分支的生产措辞（used/limit）。"""
    return json.dumps({
        "ok": False, "error_code": "INVALID_ARGUMENT",
        "message": (
            f"delegation 预算耗尽（已用 {SESSION_MAX_DELEGATIONS}/{SESSION_MAX_DELEGATIONS}）。"
            "请综合已有结果直接收尾，或改变策略，不要再委派。"
        ),
        "data": None,
    }, ensure_ascii=False)


def _pause_payload(*, version: int = 1) -> dict[str, Any]:
    """`run/paused.data`（`03 §3.4` 契约形状；断言只读 reason / trigger_dimension）。"""
    return {
        "reason": REASON_BUDGET_EXHAUSTED,
        "trigger_dimension": TRIGGER_RUN_TURNS,
        "budget_version": version,
        "consumed": {"agent_turns": 1, "model_requests": 1, "total_tokens": 11,
                     "cost_usd": None},
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": LOW_CEILING},
        },
        "continuation": {
            "completed": ["子任务 1 已完成"],
            "remaining": [f"子任务 2-{SUBTASKS} 待逐个委派"],
            "blockers": [],
            "next_safe_action": "抬高 run ceiling 后以同一 run_id 恢复",
        },
        "closeout_source": "model",
        "resume_requirements": [],
    }


def _resume_payload(*, pause_seq: int, version: int = 2) -> dict[str, Any]:
    """`run/resumed.data`：同 run 续跑，不点名任何 session 维（账行现值沿用）。"""
    return {
        "from_pause_seq": pause_seq,
        "previous_budget_version": version - 1,
        "budget_version": version,
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": RESUME_CEILING},
        },
        "consumed": {"agent_turns": 1, "model_requests": 1, "total_tokens": 11,
                     "cost_usd": None},
        "resume_basis": RESUME_BASIS_BUDGET_INCREASE,
    }


def _projection(
    *, delegations: int = SESSION_MAX_DELEGATIONS,
    max_delegations: int = SESSION_MAX_DELEGATIONS, version: int = 2,
) -> dict[str, Any]:
    """`SessionBudgetSnapshot.as_projection()` 的 session 段形状（断言读三格）。"""
    return {
        "session": {
            "version": version,
            "limits": {"max_delegations": max_delegations},
            "consumed": {"delegations": delegations},
            "remaining": {"delegations": max_delegations - delegations},
        },
    }


def _events(
    *, attempts: int = SUBTASKS, accepted: int = SESSION_MAX_DELEGATIONS,
    ending: str = "completed", last_child_status: str = "completed",
    pause_overrides: dict[str, Any] | None = None,
    resume_overrides: dict[str, Any] | None = None,
) -> list[Any]:
    """自洽轨迹：低 ceiling 执行（1 轮）⇒ 暂停 ⇒ 同 run 续跑 ⇒ 恰好 `accepted` 次受纳。

    尝试 i <= accepted：tool/call → delegation-started → delegation-finished →
    tool/result（pending_events 落在 call 与 result 之间，与 executor 同序）；其余
    尝试：tool/call → 拒绝回填（带「预算耗尽 8/8」）。反例只改"某一处不对"的那一格。
    """
    events: list[Any] = []
    seq = 0

    def add(event_type: str, data: dict | None = None) -> None:
        nonlocal seq
        events.append(SimpleNamespace(
            seq=seq, type=event_type, data=data or {}, run_id=RUN_ID, step_id=None,
        ))
        seq += 1

    def attempt(index: int, *, accepted_now: bool) -> str:
        call_id = f"call-delegate-{index}"
        child = f"child-session-{index}"
        task_text = f"子任务 {index}：请原样返回一行文本 subtask-{index}-done"
        add("model/request", {"role": "primary", "outcome": "completed",
                              "usage": {"total_tokens": 11}})
        add("model/completed", {"content": "", "tool_calls": [
            {"id": call_id, "name": DELEGATE_TOOL,
             "args": {"target": TARGET_PROFILE, "task": task_text}},
        ]})
        add("tool/call", {"tool_call_id": call_id, "tool_name": DELEGATE_TOOL,
                          "args": {"target": TARGET_PROFILE, "task": task_text}})
        if accepted_now:
            add("agent/delegation-started", {
                "target": TARGET_PROFILE, "task": task_text, "child_session_id": child,
            })
            status = last_child_status if index == accepted else "completed"
            add("agent/delegation-finished", {
                "target": TARGET_PROFILE, "child_session_id": child, "status": status,
                "summary": f"subtask-{index}-done",
            })
            add("tool/result", {"tool_call_id": call_id,
                                "content": _success_content(f"subtask-{index}-done")})
        else:
            add("tool/result", {"tool_call_id": call_id, "content": _rejected_content()})
        return call_id

    add("session/started")
    add("run/started", {"turn_index": 1,
                        "budget": {"run": {"max_agent_turns_total": LOW_CEILING}}})
    add("user/message", {"content": TASK})
    attempt(1, accepted_now=1 <= accepted)
    pause_seq = seq
    add("run/paused", {**_pause_payload(), **(pause_overrides or {})})
    add("run/resumed", {**_resume_payload(pause_seq=pause_seq), **(resume_overrides or {})})
    for index in range(2, attempts + 1):
        attempt(index, accepted_now=index <= accepted)
    if ending == "completed":
        add("run/completed", {
            "final_text": (f"子任务 1-{accepted} 已由子代理完成，"
                           f"第 {accepted + 1} 次委派因预算耗尽被拒（已用 "
                           f"{SESSION_MAX_DELEGATIONS}/{SESSION_MAX_DELEGATIONS}）。"),
        })
    else:
        add("run/failed", {"error": "恢复后崩了（反例）"})
    return events


def _default_streams(*, resume_completed: bool = True) -> dict[str, list[str]]:
    """live 流替身：pause 流以 run/paused 收口、resume 流以 run/completed 收口。"""
    return {
        "pause": ["run/started", "model/completed", "run/paused"],
        "resume": ["model/completed", "run/completed"] if resume_completed
        else ["model/completed"],
    }


def _run_assertions(
    tmp_path: Path, events: list[Any], *, projection: dict[str, Any] | None = None,
    streams: dict[str, list[str]] | None = None, replayed: list[Any] | None = None,
) -> list[Any]:
    ctx = _context(tmp_path)
    return SCENARIO._assertions(
        ctx=ctx, events=events,
        replayed=list(events) if replayed is None else replayed,
        projection=_projection() if projection is None else projection,
        streams=streams if streams is not None else _default_streams(),
    )


def _failed(assertions: list[Any]) -> set[str]:
    return {item.name for item in assertions if not item.ok}


# ── 断言面：自洽轨迹全绿 ─────────────────────────────────────────────────


def test_assertions_pass_on_a_self_consistent_trajectory(tmp_path):
    """9 尝试 / 8 受纳 / 第 9 次被拒 / 暂停一次 / 同 run 续跑完成：全绿且断言集恰为 12 条。"""
    assertions = _run_assertions(tmp_path, _events())
    assert {item.name for item in assertions} == EXPECTED_ASSERTIONS
    assert _failed(assertions) == set(), _failed(assertions)


# ── 断言面：每一处"走错路"都要判红 ───────────────────────────────────────


def test_red_only_seven_delegations_accepted(tmp_path):
    """只受纳 7 次（票面点名的反例）⇒ 受纳上限 + child 完成面两条判红。"""
    failed = _failed(_run_assertions(tmp_path, _events(accepted=7)))
    assert failed == {
        "accepted_delegations_capped_across_runs",
        "children_are_real_and_completed",
    }, failed


def test_red_fewer_attempts_than_the_audit_requires(tmp_path):
    """模型只发起 8 次尝试（8 次全受纳、无拒绝）⇒ 尝试面 + 拒绝面两条判红。"""
    failed = _failed(_run_assertions(tmp_path, _events(attempts=8, accepted=8)))
    assert failed == {
        "all_subtask_attempts_made",
        "over_budget_rejection_before_child_execution",
    }, failed


def test_red_run_fails_instead_of_completing(tmp_path):
    """续跑以 run/failed 收口 ⇒ 单终态判红；live 流如实镜像（也不再有 completed 帧）。"""
    events = _events(ending="failed")
    failed = _failed(_run_assertions(
        tmp_path, events, streams=_default_streams(resume_completed=False),
    ))
    assert failed == {"run_completed_without_terminal_noise",
                      "stream_mirrors_pause_and_completion"}, failed


def test_red_pause_payload_mutated(tmp_path):
    """暂停 reason / trigger_dimension 改错 ⇒ 结构暂停判据红（预算暂停不是别的暂停）。"""
    mutations = {
        "reason": {"reason": "deadline"},
        "trigger": {"trigger_dimension": "run.deadline_at"},
    }
    for label, patch in mutations.items():
        failed = _failed(_run_assertions(tmp_path, _events(pause_overrides=patch)))
        assert failed == {"pause_exactly_once_structural"}, label


def test_red_resume_detached_from_the_pause(tmp_path):
    """`from_pause_seq` 接错暂停 ⇒ 同 run 恢复判据红（恢复必须接上那一条暂停）。"""
    failed = _failed(_run_assertions(
        tmp_path, _events(resume_overrides={"from_pause_seq": 99}),
    ))
    assert failed == {"resume_same_run_without_session_dims"}, failed


def test_red_projection_disagrees_with_the_events(tmp_path):
    """投影三格各自改错（少报 / 抬高 ceiling / 版本归零）⇒ 投影互证判红。"""
    mutations = {
        "少报": _projection(delegations=7),
        "ceiling 抬高": _projection(max_delegations=9),
        "版本归零": _projection(version=0),
    }
    for label, projection in mutations.items():
        failed = _failed(_run_assertions(tmp_path, _events(), projection=projection))
        assert failed == {"session_row_projection_agrees"}, label


def test_red_child_finished_but_not_completed(tmp_path):
    """有 child 以非 completed 收场 ⇒ "子代理是真的跑完"判据红（不是空账也不能有失败账）。"""
    failed = _failed(_run_assertions(tmp_path, _events(last_child_status="failed")))
    assert failed == {"children_are_real_and_completed"}, failed


def test_red_dangling_delegate_call(tmp_path):
    """某次受纳尝试的 tool/result 被抽掉（有 call 无 result）⇒ 悬空调用判红。

    抽的是**受纳**那次的结果：抽拒绝那次会连拒绝回填一起抽掉，红就没有分辨力。
    """
    events = _events()
    dangling_id = str(next(e for e in events if e.type == "tool/call")
                      .data.get("tool_call_id"))
    trimmed = [
        e for e in events
        if not (e.type == "tool/result"
                and str(e.data.get("tool_call_id")) == dangling_id)
    ]
    failed = _failed(_run_assertions(tmp_path, trimmed))
    assert failed == {"no_dangling_tool_calls"}, failed


def test_red_durable_replay_mismatch(tmp_path):
    """重新读盘比内存态少一条 ⇒ durable 重放判据红。"""
    events = _events()
    failed = _failed(_run_assertions(tmp_path, events, replayed=events[:-1]))
    assert failed == {"durable_replay_matches_live"}, failed


def test_red_stream_without_the_pause_frame(tmp_path):
    """pause 流缺 `run/paused` 帧（落盘有、live 镜像没有）⇒ 流镜像判据红。"""
    failed = _failed(_run_assertions(
        tmp_path, _events(),
        streams={"pause": ["run/started", "model/completed"],
                 "resume": _default_streams()["resume"]},
    ))
    assert failed == {"stream_mirrors_pause_and_completion"}, failed


# ── 端到端：替身只替模型客户端一处接缝 ────────────────────────────────────

_CHILD_TASK = re.compile(r"^子任务 (\d+)：请原样返回一行文本 subtask-\1-done$")
_CLOSEOUT_MARK = "运行即将因回合预算到顶而暂停"


class _DelegatingModel:
    """按**历史计数**决策的脚本化模型（无状态 ⇒ 跨暂停/续跑的两个 launch 天然连续）。

    路由按最后一条 human 消息：closeout 指令 ⇒ 续跑说明 JSON；子任务契约 ⇒ 一行
    subtask-<N>-done；父任务 ⇒ 发起下一次 delegate（数过历史里的 delegate
    tool_calls），发起满 `attempts` 次后给收尾总结。
    """

    def __init__(self, *, attempts: int = SUBTASKS) -> None:
        self._attempts = attempts
        self.bound_tools: list | None = None
        self.snapshots: list[list[Any]] = []

    def bind_tools(self, tools: list, **kwargs: Any) -> _DelegatingModel:
        self.bound_tools = tools
        return self

    @staticmethod
    def _delegate_attempts(messages: list[Any]) -> int:
        count = 0
        for message in messages:
            for call in getattr(message, "tool_calls", None) or []:
                if isinstance(call, dict) and call.get("name") == DELEGATE_TOOL:
                    count += 1
        return count

    def _next(self, messages: list[Any]) -> AIMessage:
        last_human = ""
        for message in reversed(messages):
            if getattr(message, "type", "") == "human":
                last_human = str(getattr(message, "content", ""))
                break
        if _CLOSEOUT_MARK in last_human:
            return AIMessage(content=json.dumps({
                "completed": ["已接纳的子任务已完成"],
                "remaining": ["继续逐个发起剩余委派尝试"],
                "blockers": [],
                "next_safe_action": "抬高 run ceiling 后以同一 run_id 恢复",
            }, ensure_ascii=False))
        found = _CHILD_TASK.match(last_human.strip())
        if found is not None:
            return AIMessage(content=f"subtask-{found.group(1)}-done")
        attempts = self._delegate_attempts(messages)
        if attempts >= self._attempts:
            return AIMessage(content=(
                f"子任务 1-{min(attempts, SESSION_MAX_DELEGATIONS)} 已由子代理完成；"
                f"超出部分因委派预算耗尽被拒（已用 "
                f"{SESSION_MAX_DELEGATIONS}/{SESSION_MAX_DELEGATIONS}）。"
            ))
        next_index = attempts + 1
        return AIMessage(content="", tool_calls=[{
            "id": f"call-delegate-{next_index}", "name": DELEGATE_TOOL,
            "args": {
                "target": TARGET_PROFILE,
                "task": f"子任务 {next_index}：请原样返回一行文本 subtask-{next_index}-done",
            },
        }])

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self.snapshots.append(list(messages))
        return self._next(messages)

    async def astream(self, messages: list[Any], **kwargs: Any):
        self.snapshots.append(list(messages))
        response = self._next(messages)
        yield AIMessageChunk(content=response.content, tool_calls=response.tool_calls or [])


def _patch_model(monkeypatch: pytest.MonkeyPatch, *, attempts: int = SUBTASKS
                 ) -> list[_DelegatingModel]:
    """把**模型客户端**接缝换成离线替身（工具 / 执行器 / 账本 / 运行时全是生产实现）。

    接缝打在 `agent_harness.assembly.create_chat_model`（`resume_and_launch` 走
    `build_runtime` 的模块级绑定，import 顺序无关）；`ModelConfig.from_settings`
    一并打掉（不让 Settings 解析碰真实供应商目录）。
    """
    from agent_harness.model.config import ModelConfig

    monkeypatch.setattr(
        ModelConfig, "from_settings",
        classmethod(lambda cls, settings: SimpleNamespace(
            model_name="offline-scripted", provider_id="offline", fallback=None,
        )),
    )
    created: list[_DelegatingModel] = []

    def _factory(config: Any, request_timeout: Any = None,
                 reasoning_effort: Any = None) -> _DelegatingModel:
        model = _DelegatingModel(attempts=attempts)
        created.append(model)
        return model

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", _factory)
    return created


def test_scenario_passes_end_to_end_with_the_model_seam_stubbed(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    """绿路：低 ceiling 暂停 → 续跑，恰好 8 受纳 + 第 9 次被拒 ⇒ AttemptOutcome.ok 全绿。"""
    created = _patch_model(monkeypatch)
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    ctx = ScenarioContext(
        settings=_settings(), sandbox=sandbox,
        session_root=tmp_path / "sessions", session_id=SESSION_ID, attempt_index=1,
    )
    outcome = asyncio.run(SCENARIO.run(ctx))

    assert outcome.ok is True, {item.name: item.detail for item in outcome.assertions
                                if not item.ok}
    assert {item.name for item in outcome.assertions} == EXPECTED_ASSERTIONS
    assert outcome.session_id == ctx.session_id
    assert outcome.run_id and outcome.run_status == "completed"
    assert outcome.steps >= SUBTASKS, "父 run 至少 SUBTASKS+1 轮决策"
    assert outcome.tool_calls.count(DELEGATE_TOOL) == SUBTASKS, "恰好 9 次 delegate 尝试"
    assert f"{SESSION_MAX_DELEGATIONS}/{SESSION_MAX_DELEGATIONS}" in outcome.output_tail
    assert created, "场景必须真的造过模型客户端（每次 launch 一个）"

    # durable 面独立复读（不看场景给的读数）：8 受纳 / 8 完成 / 9 尝试 / 1 拒绝回填。
    events = JsonlSessionStore(root=ctx.session_root).read_events(ctx.session_id)
    started = [e for e in events if e.type == "agent/delegation-started"]
    finished = [e for e in events if e.type == "agent/delegation-finished"]
    calls = [e for e in events if e.type == "tool/call"
             and str(e.data.get("tool_name")) == DELEGATE_TOOL]
    rejected = [
        e for e in events
        if e.type == "tool/result"
        and BUDGET_EXHAUSTED_MARKER in str(e.data.get("content", ""))
        and f"{SESSION_MAX_DELEGATIONS}/{SESSION_MAX_DELEGATIONS}"
        in str(e.data.get("content", ""))
    ]
    assert len(calls) == SUBTASKS and len(started) == SESSION_MAX_DELEGATIONS
    assert len(finished) == SESSION_MAX_DELEGATIONS and len(rejected) == 1
    assert len({str(e.data.get("child_session_id")) for e in started}) \
        == SESSION_MAX_DELEGATIONS, "跨 run 聚合：每次受纳一个独立 child 会话"
    paused = [e for e in events if e.type == "run/paused"]
    assert len(paused) == 1 and paused[0].data.get("reason") == REASON_BUDGET_EXHAUSTED


def test_scenario_is_red_when_the_model_stops_at_the_quota(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    """破坏契约即红：模型 8 次后主动停手（审计要求没满足）⇒ ok=False，红的落点要对得上。

    配额没被击穿（8/8 恰好用满、无拒绝回填），红的只能是"尝试面 + 拒绝面"两条——
    场景不会把"配额没满但模型守规矩"洗成通过。
    """
    _patch_model(monkeypatch, attempts=SESSION_MAX_DELEGATIONS)
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace")
    ctx = ScenarioContext(
        settings=_settings(), sandbox=sandbox,
        session_root=tmp_path / "sessions", session_id=SESSION_ID, attempt_index=1,
    )
    outcome = asyncio.run(SCENARIO.run(ctx))

    assert outcome.ok is False
    failed = {item.name for item in outcome.assertions if not item.ok}
    assert failed == {
        "all_subtask_attempts_made",
        "over_budget_rejection_before_child_execution",
    }, failed


# ── registry：builtin 注册路径 ───────────────────────────────────────────


def test_registered_as_builtin_scenario():
    """`delegation-budget-tree-wide` 随 harness 发布：幂等注册后 builtin 核实为真。"""
    assert SCENARIO_ID in [item.id for item in BUILTIN_SCENARIOS]
    register_builtin_scenarios()
    register_builtin_scenarios()  # 幂等：重复调用不抛、不产生第二份
    assert get_scenario(SCENARIO_ID) is SCENARIO
    assert is_builtin(SCENARIO_ID) is True, "要能被 runner 认出（否则产出会被锁 FAIL）"
