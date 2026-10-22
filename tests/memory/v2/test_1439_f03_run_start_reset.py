"""#1439 F-03：consolidation 失败预算必须按 **run** 归零（恢复上游的 per-turn 语义）。

# 钉的是什么

`MAX_CONSOLIDATION_FAILURES_PER_TURN` / `reset_consolidation_failures()` 从上游
`tools/memory_tool_store.py:97,113-131` 照搬，但移植时**没有调用点**（只有「写成功」
归零）。markdown store 是进程级单例（`build_memory_v2_service` 建一次，跨 run 复用），
于是计数跨 run 累积：长进程里第 N 个 run 第一次失败就可能直接 TERMINAL，自纠机会被
提前掐断。上游把重置放在 turn 边界（`agent/turn_context.py:619`
`_reset_per_turn_agent_state`），本仓对应点是 run 开始边界
（`AgentRuntime._drive` → 宿主的 `notify_run_started`，端口外的可选扩展）。

# 三层判据

- **接缝层**（真 Runtime + 真 `MemoryTool` + 真 store）：一次 run 内 3 次失败仍可自纠、
  第 4 次 TERMINAL；下一个 run 的第 1 次失败又可自纠。对照组用**不实现**
  `notify_run_started` 的宿主——那正是修复前生产宿主的形状，新 run 第 1 次失败即
  TERMINAL，这条对照就是本 bug 的回归断言。
- **续跑层**：同 run 续跑（`LaunchRunBudget(run_id=...)`）**不**归零——续跑是同一逻辑
  run 的继续，`#312` 要求计数器与 stuck 指纹都不重置。
- **宿主层**（`MemoryJobRunner.notify_run_started`）：它把调用转给
  `memory_v2.markdown`；没有 markdown 面时是 no-op，store 抛异常时吞掉。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.agent.run_budget import LaunchRunBudget
from agent_harness.memory.v2.markdown_store import MAX_CONSOLIDATION_FAILURES_PER_TURN
from agent_harness.memory.v2.memory_tool import MemoryTool
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.memory.v2.runner import MemoryJobRunner
from agent_harness.session import RUN_STARTED, TOOL_RESULT
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.memory.v2._markdown_memory_1353 import build_store, call
from tests.scripted_model import ScriptedModel

pytestmark = pytest.mark.asyncio

CAP = MAX_CONSOLIDATION_FAILURES_PER_TURN


class _RunStartSpy:
    """真实记忆宿主的最小替身：`notify_run_started` 直接落到真 store 上。

    `seam=False` 时不实现 `notify_run_started`——那是修复前生产宿主的形状（Runtime
    用 `getattr` 探测，缺席即不重置），对照组的 bug 就靠它复现。
    """

    def __init__(self, store: Any, *, seam: bool = True) -> None:
        self._store = store
        self.started = 0
        if seam:
            self.notify_run_started = self._notify_run_started  # type: ignore[method-assign]

    def _notify_run_started(self) -> None:
        self.started += 1
        self._store.reset_consolidation_failures()

    async def notify_run_finished(self, **_kwargs: Any) -> None:
        return None


def _runtime(store: Any, notifier: Any, model: Any, *, run_budget: Any = None) -> AgentRuntime:
    """真 Runtime + 真 `MemoryTool`；**每个 run 建一个**——与生产同形
    （`build_runtime` 每轮调用），而 store / 宿主是跨 run 的进程级单例。

    `run_budget` 带 `run_id` 时是**同 run 续跑**（`#312`），Runtime 不再 `begin_run`。
    """
    registry = ToolRegistry()
    registry.register(MemoryTool(store))
    return AgentRuntime(
        model=model, registry=registry, executor=ToolExecutor(registry),
        max_agent_turns=20, memory_formation=notifier, run_budget=run_budget,
    )


def _fail_round(run_tag: str, index: int) -> AIMessage:
    """一次零命中的 replace（`old_text` 每轮不同，避免撞上同错熔断的指纹）。"""
    return AIMessage(
        content="",
        tool_calls=[{
            "id": f"call_{run_tag}_{index}", "name": "memory",
            "args": {
                "target": "memory", "action": "replace",
                "old_text": f"no-such-entry-{run_tag}-{index}", "content": "x",
            },
            "type": "tool_call",
        }],
    )


async def _run(
    store: Any, notifier: Any, session: Any, *, run_tag: str, failures: int,
    run_budget: Any = None,
) -> list[dict]:
    """跑一个 run：`failures` 次零命中 replace，然后正常收尾。返回本 run 的工具结果。"""
    model = ScriptedModel(
        [_fail_round(run_tag, i) for i in range(failures)] + [AIMessage(content="好的。")]
    )
    before = len(session.events)
    result = await _runtime(store, notifier, model, run_budget=run_budget).run(
        session, None if run_budget is not None else f"run {run_tag}",
    )
    assert result.status == "completed", f"run {run_tag} 没有正常收口：{result.status}"
    return [
        json.loads(event.data["content"])
        for event in session.events[before:]
        if event.type == TOOL_RESULT
    ]


def _self_correctable(payload: dict) -> bool:
    """cap 之内：失败响应仍带 `current_entries`，模型能就地自纠。"""
    response = (payload.get("metadata") or {}).get("response") or {}
    return payload.get("ok") is False and "current_entries" in response


def _terminal(payload: dict) -> bool:
    """cap+1：TERMINAL（`done=True`）且不再回显条目。"""
    response = (payload.get("metadata") or {}).get("response") or {}
    return payload.get("ok") is False and response.get("done") is True and (
        "current_entries" not in response
    )


# --------------------------------------------------------------------------------------
# 接缝层：真 Runtime → 宿主 → store
# --------------------------------------------------------------------------------------


async def test_three_failures_self_correct_then_the_fourth_is_terminal(tmp_path) -> None:
    store = build_store(tmp_path)
    notifier = _RunStartSpy(store)
    session = make_session(tmp_path / "sessions")

    results = await _run(store, notifier, session, run_tag="a", failures=CAP + 1)

    assert len(results) == CAP + 1
    assert all(_self_correctable(payload) for payload in results[:CAP]), (
        f"cap 之内（{CAP} 次）必须还能自纠"
    )
    assert _terminal(results[CAP]), f"第 {CAP + 1} 次必须 TERMINAL"
    assert notifier.started == 1, "每个 run 恰好一次 run-start 重置"


async def test_a_new_run_gets_a_fresh_budget(tmp_path) -> None:
    """跨 run 归零：run 1 用满 3 次自纠额度后，run 2 的第一次失败仍可自纠。"""
    store = build_store(tmp_path)
    notifier = _RunStartSpy(store)
    session = make_session(tmp_path / "sessions")

    first = await _run(store, notifier, session, run_tag="a", failures=CAP)
    assert all(_self_correctable(payload) for payload in first)

    second = await _run(store, notifier, session, run_tag="b", failures=1)
    assert _self_correctable(second[0]), (
        "新 run 的第 1 次失败被判 TERMINAL —— run-start 没有归零，计数跨 run 累积了"
    )
    assert notifier.started == 2, "两个 run 各一次 run-start 重置"


async def test_without_the_run_start_seam_the_new_run_fails_immediately(tmp_path) -> None:
    """回归对照（修复前的形状）：不调 `notify_run_started` ⇒ 新 run 第 1 次失败即 TERMINAL。

    这条把"接缝是承重的"变成可执行判据：删掉 `_drive` 里的 run-start 调用、或删掉宿主
    的重置，本用例的断言方向会反转。
    """
    store = build_store(tmp_path)
    notifier = _RunStartSpy(store, seam=False)
    session = make_session(tmp_path / "sessions")

    first = await _run(store, notifier, session, run_tag="a", failures=CAP)
    assert all(_self_correctable(payload) for payload in first)

    second = await _run(store, notifier, session, run_tag="b", failures=1)
    assert _terminal(second[0]), (
        "没有 run-start 重置时，新 run 的第 1 次失败应当是第 4 次（TERMINAL）"
    )
    assert notifier.started == 0


async def test_a_resumed_run_does_not_get_a_fresh_budget(tmp_path) -> None:
    """同 run 续跑**不**归零（`#312` 不变量）：续跑是同一逻辑 run 的继续。

    续跑的输入是 `LaunchRunBudget(run_id=...)`，Runtime 因此不再 `begin_run`。记忆预算
    若在续跑时也归零，同一个逻辑 run 就凭空多出 3 次自纠机会——与"计数器与 stuck
    指纹都不重置"（`runtime.py` 的 `#312` 注释）字面冲突。本用例把这条钉住：run 1
    用满额度后，以同一 run_id 续跑，续跑的第 1 次失败必须是第 4 次（TERMINAL）。
    """
    store = build_store(tmp_path)
    notifier = _RunStartSpy(store)
    session = make_session(tmp_path / "sessions")

    first = await _run(store, notifier, session, run_tag="a", failures=CAP)
    assert all(_self_correctable(payload) for payload in first)
    assert notifier.started == 1
    run_id = next(e.run_id for e in session.events if e.type == RUN_STARTED)

    resumed = await _run(
        store, notifier, session, run_tag="b", failures=1,
        run_budget=LaunchRunBudget(run_id=run_id, turn_index=1),
    )

    assert _terminal(resumed[0]), (
        "续跑第 1 次失败被判可自纠 —— 续跑时归零了预算，违反 `#312` 不重置不变量"
    )
    assert notifier.started == 1, "同 run 续跑不得再发一次 run-start 重置"


# --------------------------------------------------------------------------------------
# 宿主层：MemoryJobRunner 把 run-start 转给 markdown 面
# --------------------------------------------------------------------------------------


def _runner(memory_v2: Any) -> MemoryJobRunner:
    return MemoryJobRunner(
        jobs=object(), sessions=object(), executor=object(),
        roles=MemoryModelRoles(primary=None, fallback=None), memory_v2=memory_v2,
    )


async def test_runner_run_start_resets_the_markdown_budget(tmp_path) -> None:
    store = build_store(tmp_path)
    runner = _runner(type("_V2", (), {"markdown": store})())

    for _ in range(CAP):
        assert (await call(store.replace, "memory", "missing", "x"))["success"] is False
    assert (await call(store.replace, "memory", "missing", "x"))["done"] is True, (
        "前提：不重置时第 4 次已经 TERMINAL"
    )

    runner.notify_run_started()
    assert (await call(store.replace, "memory", "missing", "x"))["success"] is False
    assert "current_entries" in (await call(store.replace, "memory", "missing", "x")), (
        "重置后必须重新拿到自纠响应"
    )


async def test_runner_run_start_is_a_noop_without_markdown() -> None:
    """纯 SQLite 部署 / 未接 markdown：没有可重置的东西，且不得抛。"""
    _runner(None).notify_run_started()
    _runner(type("_V2", (), {"markdown": None})()).notify_run_started()


async def test_runner_run_start_swallows_store_failures() -> None:
    """store 侧抛异常也必须吞掉：run 还没开始，记忆是旁路（`#1439` C-03）。"""
    class _Boom:
        def reset_consolidation_failures(self) -> None:
            raise RuntimeError("disk on fire")

    _runner(type("_V2", (), {"markdown": _Boom()})()).notify_run_started()
