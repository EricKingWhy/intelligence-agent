"""W-12（#356）· 第二阶段 TDD 红测：明确退出信号 → 安全暂停（选项 B）。

判据来源：`docs/agents/356-design.md`（§2 contract、§3 quit-inspection、§4 W-05 严格
写 fail-closed、§5 无信号断线继续跑、§8 测试计划 T1–T15）；上游 `02 §5.2.1`、
`03 §3.4/§5`、`11 §6.2`、ADR-0046。

**本步只写测试（红），不写产品代码、不改 `src/`、不改现有测试。**

新面（设计 §1.3 / §2.2 权威）：
- `RunManager.signal_client_exit(session_id, *, client_id) -> ClientExitOutcome`（async）
- `RunManager.inspect_exit_impact(session_id) -> ExitImpact`（async，只读）
- 类型 `ExitImpact` / `ClientExitOutcome` / `ClientExitError` + 三个 status 常量
- `_reap_if_orphaned` 的 `presence_managed` 分支改写（宽限到期只记日志、继续跑）

红态约定：所有新符号经 `runmanager` 模块**运行时取属性**（`runmanager.CLIENT_EXIT_*`
/ `runmanager.ClientExitError`），这样红是逐用例的 `AttributeError`（"缺能力"），
而不是收集期的 `ImportError`。用例编号 `[W-12 Tn]` = 用户派单编号；`[design Tn]`
= 设计稿 §8 编号。

Fixture 复用 `tests/agent/test_client_absent_runtime.py` 的 fake runtime / ScriptedModel
模式与 `JsonlSessionStore`，不自造第二套 harness。`signal_client_exit` 的返回可能早于
在途 Tool 收口（设计 §2.1 第③步"立即置缺席、之后由既有链收口"），故对"在途信号"类
用例用 `_signal_while_releasing`：置缺席后放行在途工具，兼容"signal 是否等待暂停"两种
实现，红态仍是干净的 `AttributeError`。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.agent.run_budget import derive_run_budget, project_budget
from agent_harness.model.accounting import HARNESS_MODEL_ACCOUNTING
from agent_harness.session import (
    OPERATION_RECONCILE_REQUIRED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    Session,
    runmanager,
)
from agent_harness.session.progress import ProgressWriteOutcome
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage import (
    OperationState,
    SqliteOperationLedger,
    has_unproven_side_effect,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.scripted_model import ScriptedModel
from tests.tooling.test_deadline_admission import _SlowMutatingTool

# ── fixtures ────────────────────────────────────────────────────────────────


class _GateArgs(BaseModel):
    text: str = "g"


class _GateTool(Tool):
    """在途 Tool 探针：execute 中段挂起等测试放行。

    执行次数进 `calls`——钉"退出路径不取消、不盲重试、恰好一次"（设计 decision 5）。
    """

    def __init__(self, released: asyncio.Event, calls: list[int]) -> None:
        self._released = released
        self._calls = calls

    @property
    def name(self) -> str:
        return "gate"

    @property
    def description(self) -> str:
        return "挂起直到测试放行。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _GateArgs

    async def execute(self, args: _GateArgs) -> ToolResult:
        self._calls.append(1)
        await self._released.wait()
        return ToolResult.success(message=args.text)


def _gate_round() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": "call_exit", "name": "gate", "args": {"text": "g"}}],
    )


def _session(tmp_path: Path, *, cwd: Path | None) -> Session:
    """临时 Session（JsonlSessionStore 在 tmp 下）；cwd 决定 W-05 是否有项目根锚。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    return Session.start(store, cwd=str(cwd) if cwd is not None else None)


def _runtime(
    model, *, tools: Sequence[Tool] = (), operation_ledger=None,
) -> AgentRuntime:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentRuntime(
        model=model, registry=registry,
        executor=ToolExecutor(registry, operation_ledger=operation_ledger),
        max_agent_turns=5,
    )


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0, interval: float = 0.005,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)
    return True


async def _signal_while_releasing(
    manager, session_id: str, runtime: AgentRuntime,
    released: asyncio.Event, *, client_id: str = "cli", timeout: float = 5.0,
) -> object:
    """发退出信号，并在置缺席后放行在途工具；返回 signal 的 await 结果。

    置缺席由 `runtime.client_presence.absent`（既有 W-22 面）如实观测——不假设
    signal 是否等待暂停：先起 signal task，轮询到位后放行工具，再 await signal。
    """
    task = asyncio.create_task(
        manager.signal_client_exit(session_id, client_id=client_id)
    )
    ok = await _wait_until(lambda: runtime.client_presence.absent)
    assert ok, "发信号后应在宽限前立即置缺席（decision 3）"
    released.set()
    return await asyncio.wait_for(task, timeout=timeout)


def _paused_events(session: Session) -> list:
    return [e for e in session.events if e.type == RUN_PAUSED]


def _patch_progress_write_failure(monkeypatch: pytest.MonkeyPatch, outcome_factory):
    """把退出路径的 W-05 严格写打点成失败（返回 ok=False）。

    同时打 `runmanager`（from-import 绑定名）与 `progress`（module-attr 取法）两处，
    `raising=False`——红态下 `runmanager.write_progress_file` 尚不存在，不能提前报错。
    """

    def boom(*_args, **_kwargs):
        return outcome_factory()

    for target in (
        "agent_harness.session.runmanager.write_progress_file",
        "agent_harness.session.progress.write_progress_file",
    ):
        monkeypatch.setattr(target, boom, raising=False)


# ── T1：未登记 run → ignored_not_managed，信号零副作用 ─────────────────────


@pytest.mark.asyncio
async def test_unmanaged_run_signal_is_ignored_not_managed(tmp_path) -> None:
    """[W-12 T1 / design T1] `presence_managed=False` 的 run 收到信号：
    返回 `ignored_not_managed`，不置闸门、不暂停；run 行为逐字不变（decision 7）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="done")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi")  # 未登记
        assert await _wait_until(lambda: calls == [1]), "工具已在途"
        assert runtime.client_presence.managed is False

        outcome = await manager.signal_client_exit(
            session.session_id, client_id="cli"
        )

        assert outcome.status == runmanager.CLIENT_EXIT_IGNORED_NOT_MANAGED
        assert runtime.client_presence.managed is False
        assert runtime.client_presence.absent is False, "未登记不得置缺席"
        assert _paused_events(session) == [], "未登记 run 不得被暂停"
        assert run.reap_requested is False

        released.set()
        await asyncio.wait_for(run.task, timeout=5)
        types = [e.type for e in session.events]
        assert RUN_PAUSED not in types, "信号无副作用：run 自然完成，不暂停"
        assert RUN_COMPLETED in types
    finally:
        await manager.aclose()


# ── T2：已收口 run → ignored_already_settled，不双写 ────────────────────────


@pytest.mark.asyncio
async def test_signal_after_terminal_run_is_ignored_already_settled(tmp_path) -> None:
    """[W-12 T2 / design T2] run 已终态（run/completed 已落）→ 信号返回
    `ignored_already_settled`，不新增任何 `run/paused`（decision 7）。"""
    session = _session(tmp_path, cwd=tmp_path)
    runtime = _runtime(ScriptedModel([AIMessage(content="done")]))
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        await asyncio.wait_for(run.task, timeout=5)
        assert run.terminal is True
        before = len(_paused_events(session))

        outcome = await manager.signal_client_exit(
            session.session_id, client_id="cli"
        )

        assert outcome.status == runmanager.CLIENT_EXIT_IGNORED_ALREADY_SETTLED
        assert len(_paused_events(session)) == before == 0, "终态不双写、不暂停"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_signal_after_client_absent_pause_does_not_double_write(tmp_path) -> None:
    """[W-12 T2 / design T3] run 已 paused（已置缺席并落 run/paused）→ 信号返回
    `ignored_already_settled`，`run/paused` 恰好仍 1 条（W-22「已 paused 不双写」）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="never")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        # 用既有 W-22 面（直接置缺席）造成一次 client_absent 暂停，作为"已 paused"前置。
        runtime.client_presence.mark_absent()
        released.set()
        await asyncio.wait_for(run.task, timeout=5)
        assert run.paused is True
        assert len(_paused_events(session)) == 1

        outcome = await manager.signal_client_exit(
            session.session_id, client_id="cli"
        )

        assert outcome.status == runmanager.CLIENT_EXIT_IGNORED_ALREADY_SETTLED
        assert len(_paused_events(session)) == 1, "已 paused 不双写"
        assert RUN_FAILED not in [e.type for e in session.events]
    finally:
        await manager.aclose()


# ── T2b：await 间隙 run 收口 → 不谎报 paused（竞态回归）────────────────────


@pytest.mark.asyncio
async def test_signal_race_settlement_between_awaits_is_not_reported_paused(
    tmp_path, monkeypatch,
) -> None:
    """[W-12 race / decision 7] 幂等检查通过后、第③步身份校验前还有两次
    await（只读 quit-inspection → 严格 W-05 写）。若 run 恰好在此间隙**自然收口**
    （`_drive` 的 finally 释放 runtime），身份校验的 `run.runtime is None` 命中，
    outcome 必须如实报 `ignored_already_settled`——不得谎报 `paused` / "已立即置缺席"。

    竞态用**真实语义**制造：mock `inspect_exit_impact`，在其 await 期间放行在途
    工具并等 `run.task` 自然结束（走真实 `_drive` finally，不手造 terminal /
    absent 旗标）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="done")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        original_inspect = manager.inspect_exit_impact

        async def inspect_that_settles(session_id: str):
            # await 间隙：放行在途工具 → run 跑到自然终态 → _drive finally 释放
            # runtime（`run.runtime = None`）。
            released.set()
            await asyncio.wait_for(run.task, timeout=5)
            return await original_inspect(session_id)

        monkeypatch.setattr(manager, "inspect_exit_impact", inspect_that_settles)

        outcome = await manager.signal_client_exit(
            session.session_id, client_id="cli",
        )

        assert outcome.status == runmanager.CLIENT_EXIT_IGNORED_ALREADY_SETTLED, (
            "await 间隙 run 已收口，不得谎报 paused"
        )
        assert runtime.client_presence.absent is False, (
            "未真正置缺席，不得谎报已立即置缺席"
        )
    finally:
        await manager.aclose()


# ── T2c：TOCTOU —— 旧 run 在途时被替换，不得错靶到新 run（B-P1 回归）────────


@pytest.mark.asyncio
async def test_signal_does_not_mark_replacement_run_absent(
    tmp_path, monkeypatch,
) -> None:
    """[W-12 B-P1 回归 / TOCTOU] 旧 run R1 在途时发信号；在 `inspect_exit_impact`
    的 await 间隙把 `_runs[session_id]` 换成已登记的新 run R2。信号必须做身份校验后
    放弃置位（不对 R2 置缺席），返回 `ignored_already_settled` 且 R2 的 gate.absent
    仍为 False（旧实现按 session_id 重查会把信号错靶到 R2 并置其缺席）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="never")]),
        tools=[_GateTool(released, calls)],
    )
    runtime2 = _runtime(ScriptedModel([AIMessage(content="never")]))
    runtime2.client_presence.enroll()
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        _run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        original_inspect = manager.inspect_exit_impact

        async def inspect_that_swaps(session_id: str):
            # await 间隙：同会话登记一个新 run R2（旧 R1 仍在途）。R2 已纳入在场
            # 协议，其 gate 可被观测——若信号错靶到它，R2.gate.absent 会被置真。
            replacement = runmanager.ManagedRun(session, manager)
            replacement.presence_managed = True
            replacement.runtime = runtime2
            manager._runs[session_id] = replacement
            return await original_inspect(session_id)

        monkeypatch.setattr(manager, "inspect_exit_impact", inspect_that_swaps)

        outcome = await manager.signal_client_exit(
            session.session_id, client_id="cli",
        )

        assert outcome.status == runmanager.CLIENT_EXIT_IGNORED_ALREADY_SETTLED, (
            "await 间隙 run 已被替换——不得当作正常信号收口"
        )
        assert runtime2.client_presence.absent is False, (
            "TOCTOU 修复：不得把退出信号错靶到新 run R2 并置其缺席"
        )
    finally:
        released.set()
        await manager.aclose()


# ── T3：明确退出信号 → 恰好一条 run/paused(client_absent) ───────────────────


@pytest.mark.asyncio
async def test_clear_exit_signal_pauses_with_client_absent_shape(tmp_path) -> None:
    """[W-12 T3 / design T5] 正常在途信号：恰好一条
    `run/paused(reason=client_absent, trigger_dimension=client_presence,
    closeout_source=deterministic)`，同 run_id，不出现 `run/failed`（decision 1/3/5）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    scripted = ScriptedModel([_gate_round(), AIMessage(content="never reached")])
    runtime = _runtime(scripted, tools=[_GateTool(released, calls)])
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        # A-P3-5：信号前记下 model / tool 两维的实际接纳计数。循环顶准入点对
        # model / tool / child **三维同一处**闸门（`runtime.py` 只认
        # `client_presence.absent`，不按维度分叉），故 child 维与 model / tool 共用
        # 同一准入点，用这两维的实际计数即可钉"离开后零新增接纳"，不另造 child 探针。
        calls_before = list(calls)
        snapshots_before = len(scripted.snapshots)

        outcome = await _signal_while_releasing(
            manager, session.session_id, runtime, released,
        )
        await asyncio.wait_for(run.task, timeout=5)

        assert outcome.status == runmanager.CLIENT_EXIT_PAUSED
        paused_events = _paused_events(session)
        assert len(paused_events) == 1, "恰好一条 run/paused（不双写）"
        data = paused_events[0].data
        assert data["reason"] == "client_absent"
        assert data["trigger_dimension"] == "client_presence"
        assert data["closeout_source"] == "deterministic"
        assert RUN_FAILED not in [e.type for e in session.events]
        assert outcome.run_id == paused_events[0].run_id, "收口在同一 run_id"
        assert calls == calls_before == [1], (
            "离开后零新增工具接纳（在途工具恰好执行一次、不重跑、不盲重试）"
        )
        # AC「离开后新请求数 0」：缺席后再无模型步骤，收口也不发总结请求。
        assert len(scripted.snapshots) == snapshots_before == 1, "离开后零新增模型请求"
    finally:
        await manager.aclose()


# ── T4：无信号断线 → 继续跑（decision 8 行为改写）──────────────────────────


@pytest.mark.asyncio
async def test_no_signal_disconnect_keeps_run_running(tmp_path) -> None:
    """[W-12 T4 / design T6] 最后一个订阅者离开、宽限到期：**不置缺席、不暂停、
    不失败**，run 继续跑直到自然完成（decision 8；旧 `_reap_if_orphaned` 会在到期
    `mark_absent` ⇒ 本用例红）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="done")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=0.2)
    try:
        run, subscriber = manager.launch(
            session, runtime, "hi", presence_managed=True,
        )
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        run.unsubscribe(subscriber)          # 零订阅者 → 宽限计时
        await asyncio.sleep(0.4)             # 宽限到期

        assert runtime.client_presence.absent is False, (
            "无信号断线宽限到期不得置缺席（选项 B：继续跑）"
        )
        assert run.task is not None and not run.task.done(), "run 仍在跑"
        assert _paused_events(session) == []
        assert RUN_FAILED not in [e.type for e in session.events]

        released.set()                       # 自然收尾
        await asyncio.wait_for(run.task, timeout=5)
        types = [e.type for e in session.events]
        assert RUN_COMPLETED in types, "断线不阻断：run 自然完成"
        assert RUN_PAUSED not in types
        assert RUN_FAILED not in types
    finally:
        await manager.aclose()


# ── T5：在途 Tool 不被取消 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inflight_tool_is_not_cancelled_by_signal(tmp_path) -> None:
    """[W-12 T5 / design T7] 信号不注入 `task.cancel()`：在途工具跑到自然稳定边界、
    恰好执行一次，之后才收口暂停（decision 5）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="done")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        await _signal_while_releasing(
            manager, session.session_id, runtime, released,
        )
        await asyncio.wait_for(run.task, timeout=5)

        assert run.task.cancelled() is False, "退出路径不得取消 run task"
        assert run.reap_requested is False, "不得走 orphaned 取消臂"
        assert calls == [1], "工具恰好执行一次（不盲重试、不中途取消）"
        assert len(_paused_events(session)) == 1, "自然收尾后以一条 paused 收口"
    finally:
        await manager.aclose()


# ── T6：W-05 严格写失败 → fail-closed ──────────────────────────────────────


@pytest.mark.asyncio
async def test_strict_progress_write_failure_raises_client_exit_error(
    tmp_path, monkeypatch,
) -> None:
    """[W-12 T6 / design T8] 有 cwd 锚但严格写失败（ok=False）→ 抛
    `ClientExitError`，不置缺席、不暂停，run 继续跑（decision 4 fail-closed）。"""
    _patch_progress_write_failure(
        monkeypatch,
        lambda: ProgressWriteOutcome(
            ok=False, skipped=False, path=tmp_path / "progress.md",
            source_event_seq=0, error_kind="locked", reason="test-locked",
        ),
    )
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="done")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        with pytest.raises(runmanager.ClientExitError):
            await manager.signal_client_exit(session.session_id, client_id="cli")

        assert runtime.client_presence.absent is False, "严格写失败不得置缺席"
        assert _paused_events(session) == [], "失败即中止，不暂停"

        released.set()
        await asyncio.wait_for(run.task, timeout=5)
        assert RUN_PAUSED not in [e.type for e in session.events]
    finally:
        await manager.aclose()


# ── T6b：严格写抛非 outcome 异常 → 仍收敛为 ClientExitError（F2 回归）────────


@pytest.mark.asyncio
async def test_unexpected_progress_write_exception_raises_client_exit_error(
    tmp_path, monkeypatch,
) -> None:
    """[W-12 F2 回归 / design §4.4] 严格写抛**非 outcome** 异常（RuntimeError）→
    收敛为 `ClientExitError`（不是 RuntimeError），gate 未置缺席、run 继续跑；
    `ClientExitError.progress` 如实带 `error_kind='unexpected_exception'`。"""
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "agent_harness.session.runmanager.write_progress_file", boom, raising=False,
    )
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="done")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        with pytest.raises(runmanager.ClientExitError) as excinfo:
            await manager.signal_client_exit(session.session_id, client_id="cli")

        assert excinfo.value.progress.ok is False
        assert excinfo.value.progress.error_kind == "unexpected_exception"
        assert "boom" in excinfo.value.progress.reason
        assert runtime.client_presence.absent is False, "异常收敛后不得置缺席"
        assert _paused_events(session) == [], "失败即中止，不暂停"

        released.set()
        await asyncio.wait_for(run.task, timeout=5)
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_no_cwd_anchor_progress_write_is_na_and_still_pauses(tmp_path) -> None:
    """[design T9 / decision 4] 无 cwd 锚 → W-05 判 **N/A**（不失败、不抛错），
    照常暂停；`progress is None`、`detail` 如实注明 N/A。"""
    session = _session(tmp_path, cwd=None)  # 无项目根锚
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="never")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        outcome = await _signal_while_releasing(
            manager, session.session_id, runtime, released,
        )
        await asyncio.wait_for(run.task, timeout=5)

        assert outcome.status == runmanager.CLIENT_EXIT_PAUSED
        assert outcome.progress is None, "无锚 = N/A，不是写失败"
        assert "N/A" in outcome.detail
        assert len(_paused_events(session)) == 1
    finally:
        await manager.aclose()


# ── T7：quit-inspection 读失败 → uncertain=True，仍暂停 ──────────────────────


class _FaultInjectingLedger:
    """装饰真实账本：`fault=True` 时 `list_for_session` 抛错。

    只用于模拟"退出前只读查询读失败"（decision 6）。放行在途工具前把 `fault`
    复位，避免暂停臂（既有 `_raise_reconcile_required`）读到同一个装饰器而失败。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.fault = False

    async def list_for_session(self, session_id: str):
        if self.fault:
            raise RuntimeError("operation_ledger 读取失败（测试注入）")
        return await self._inner.list_for_session(session_id)

    async def create(self, operation) -> None:
        return await self._inner.create(operation)

    async def get(self, session_id: str, tool_call_id: str):
        return await self._inner.get(session_id, tool_call_id)

    async def update_state(self, *args, **kwargs) -> None:
        return await self._inner.update_state(*args, **kwargs)

    async def initialize(self) -> None:
        return await self._inner.initialize()


@pytest.mark.asyncio
async def test_quit_inspection_read_failure_is_uncertain_but_still_pauses(
    tmp_path,
) -> None:
    """[W-12 T7 / design T10] 账本读失败 → `impact.uncertain is True` 偏 busy，
    但信号权威仍暂停，并如实记录失败原因（decision 6）。"""
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    faulty = _FaultInjectingLedger(ledger)
    faulty.fault = True  # 只读查询读失败（decision 6 的场景）

    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="never")]),
        tools=[_GateTool(released, calls)],
        operation_ledger=faulty,
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1]), "工具已在途"

        task = asyncio.create_task(
            manager.signal_client_exit(session.session_id, client_id="cli")
        )
        assert await _wait_until(lambda: runtime.client_presence.absent), (
            "发信号后应立即置缺席"
        )
        faulty.fault = False  # 只读查询已完成；放行暂停臂
        released.set()
        outcome = await asyncio.wait_for(task, timeout=5)
        await asyncio.wait_for(run.task, timeout=5)

        assert outcome.status == runmanager.CLIENT_EXIT_PAUSED
        assert outcome.impact is not None
        assert outcome.impact.uncertain is True, "读失败必须偏 busy（不谎报 clean）"
        assert outcome.impact.busy is True
        assert outcome.uncertain is True
        assert "operation_ledger" in outcome.detail
        assert len(_paused_events(session)) == 1, "仍按信号权威暂停"
    finally:
        await manager.aclose()


# ── T8：在途 UNKNOWN 副作用 → 对账优先于暂停 ────────────────────────────────


class _WideSlowWrite(_SlowMutatingTool):
    """MUTATING 慢写，放宽 `timeout_seconds` 到 0.2s，给"在途时发信号"留出稳定窗口。"""

    @property
    def timeout_seconds(self) -> float:
        return 0.2


@pytest.mark.asyncio
async def test_inflight_unknown_side_effect_becomes_needs_reconcile_before_pause(
    tmp_path,
) -> None:
    """[W-12 T8 / design T11] 信号时在途 UNKNOWN 副作用：暂停臂复用
    `_raise_reconcile_required`，`operation/reconcile-required` 先于 `run/paused`，
    op 升到 NEED_RECONCILE（decision 5；`03 §5`）。"""
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    tool = _WideSlowWrite()
    scripted = ScriptedModel([
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_exit_unk", "name": "slow_write", "args": {}},
            ],
        ),
        AIMessage(content="never reached"),
    ])
    runtime = _runtime(scripted, tools=[tool], operation_ledger=ledger)
    session = _session(tmp_path, cwd=tmp_path)
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: tool.call_count >= 1), "慢写在途"

        outcome = await manager.signal_client_exit(
            session.session_id, client_id="cli",
        )
        await asyncio.wait_for(run.task, timeout=5)

        assert outcome.status == runmanager.CLIENT_EXIT_PAUSED
        paused = next(e for e in session.events if e.type == RUN_PAUSED)
        assert paused.data["reason"] == "client_absent"

        operation = await ledger.get(session.session_id, "call_exit_unk")
        assert operation is not None
        assert has_unproven_side_effect(operation) is True
        assert operation.state is OperationState.NEED_RECONCILE

        reconcile_events = [
            e for e in session.events if e.type == OPERATION_RECONCILE_REQUIRED
        ]
        assert len(reconcile_events) == 1
        types = [e.type for e in session.events]
        assert types.index(OPERATION_RECONCILE_REQUIRED) < types.index(RUN_PAUSED), (
            "对账优先于恢复：先 NEED_RECONCILE，再 paused"
        )
    finally:
        await manager.aclose()


# ── T9：三态在事件层面可区分 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_outcomes_distinguishable_at_event_level(tmp_path) -> None:
    """[W-12 T9 / design T12] `paused` / `failed` / `needs_reconcile` 三态可区分：
    信号路径只落 `run/paused(reason=client_absent, trigger=client_presence)`；旧孤儿
    路径落 `run/failed(reason=orphaned)`。T12 的真断言落在**投影**上（`03 §5`）：干净
    信号暂停投影为 `paused`；同一暂停若账本欠对账则状态词被覆盖为 `needs_reconcile`
    （原因照旧可读，覆盖而非替换）——不再只断言事件常量两两不同。"""
    # 态一：明确退出 → run/paused(client_absent)，且投影如实报 paused。
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="never")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1])
        await _signal_while_releasing(
            manager, session.session_id, runtime, released,
        )
        await asyncio.wait_for(run.task, timeout=5)
        paused = next(e for e in session.events if e.type == RUN_PAUSED)
        assert paused.data["reason"] == "client_absent"
        assert paused.data["trigger_dimension"] == "client_presence"

        # 投影入口（`03 §5`，`project_budget` + `reconcile_pending`）：干净信号暂停
        # → paused；带 UNKNOWN op 欠对账（reconcile_pending 非空）→ 同一暂停的 state
        # 被覆盖为 needs_reconcile。这把"投影可区分"钉成真断言（T12 A-P2-4）。
        state = derive_run_budget(session.events, paused.run_id)
        clean = project_budget(state, accounting=HARNESS_MODEL_ACCOUNTING)
        assert clean["state"] == "paused", "干净信号暂停投影为 paused"
        assert clean["reason"] == "client_absent"
        owing = project_budget(
            state, accounting=HARNESS_MODEL_ACCOUNTING,
            reconcile_pending=["call_exit_unk"],
        )
        assert owing["state"] == "needs_reconcile", (
            "带 UNKNOWN op 的暂停投影为 needs_reconcile（不是 paused）"
        )
        assert owing["reason"] == "client_absent", "覆盖而非替换：暂停原因仍可读"
    finally:
        await manager.aclose()

    # 态二：未登记 + 无信号断线 → run/failed(orphaned)。
    session2 = _session(tmp_path / "s2", cwd=tmp_path)
    slow = asyncio.Event()
    calls2: list[int] = []
    runtime2 = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="never")]),
        tools=[_GateTool(slow, calls2)],
    )
    manager2 = runmanager.RunManager(disconnect_grace_seconds=0.2)
    try:
        run2, sub2 = manager2.launch(session2, runtime2, "hi")  # 未登记
        assert await _wait_until(lambda: calls2 == [1])
        run2.unsubscribe(sub2)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(run2.task, timeout=5)
        failed = next(e for e in session2.events if e.type == RUN_FAILED)
        assert failed.data["reason"] == "orphaned"
        assert failed.data.get("trigger_dimension") != "client_presence"
        assert paused.data["reason"] != failed.data["reason"], "两态 reason 可辨"
    finally:
        await manager2.aclose()


# ── T10：幂等——同一信号发两次 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_signal_is_idempotent(tmp_path) -> None:
    """[W-12 T10 / design T4/T13] 连续两次信号：第一次生效（paused），第二次
    `ignored_already_settled`，`run/paused` 合计恰好一条（decision 7）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="never")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1])

        first = asyncio.create_task(
            manager.signal_client_exit(session.session_id, client_id="c1")
        )
        assert await _wait_until(lambda: runtime.client_presence.absent)

        second = await manager.signal_client_exit(
            session.session_id, client_id="c2",
        )
        assert second.status == runmanager.CLIENT_EXIT_IGNORED_ALREADY_SETTLED

        released.set()
        first_outcome = await asyncio.wait_for(first, timeout=5)
        await asyncio.wait_for(run.task, timeout=5)

        assert first_outcome.status == runmanager.CLIENT_EXIT_PAUSED
        assert len(_paused_events(session)) == 1, "合计恰好一条 run/paused"
    finally:
        await manager.aclose()


# ── T11：信号跳过宽限 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signal_pauses_without_waiting_for_grace(tmp_path) -> None:
    """[W-12 T11 / decision 3 第③步] 明确退出信号**立即**置缺席、不等宽限：置缺席与
    暂停都发生在宽限时长之前（此处 grace=10s，断言 < 1s）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="never")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=10.0)
    try:
        run, _sub = manager.launch(session, runtime, "hi", presence_managed=True)
        assert await _wait_until(lambda: calls == [1])

        loop = asyncio.get_running_loop()
        started = loop.time()
        task = asyncio.create_task(
            manager.signal_client_exit(session.session_id, client_id="cli")
        )
        assert await _wait_until(lambda: runtime.client_presence.absent, timeout=1.0), (
            "置缺席必须立即，不等宽限"
        )
        assert loop.time() - started < 1.0, "远早于 grace=10s"
        released.set()
        await asyncio.wait_for(task, timeout=5)
        await asyncio.wait_for(run.task, timeout=5)
        assert loop.time() - started < 10.0, "暂停不等待宽限到期"
        assert run.paused is True
    finally:
        await manager.aclose()


# ── T12：连接级重连撤销宽限计时（pin 现有行为）─────────────────────────────


@pytest.mark.asyncio
async def test_reconnect_within_grace_cancels_orphan_timer(tmp_path) -> None:
    """[W-12 T12] 断线后宽限内重新 subscribe：孤儿计时被撤销，宽限到期不置缺席、
    不暂停（pin 现有行为，不断言改写）。"""
    session = _session(tmp_path, cwd=tmp_path)
    released = asyncio.Event()
    calls: list[int] = []
    runtime = _runtime(
        ScriptedModel([_gate_round(), AIMessage(content="done")]),
        tools=[_GateTool(released, calls)],
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=0.3)
    try:
        run, subscriber = manager.launch(
            session, runtime, "hi", presence_managed=True,
        )
        assert await _wait_until(lambda: calls == [1])

        run.unsubscribe(subscriber)          # 计时起
        await asyncio.sleep(0.1)             # < grace
        _resub = run.subscribe()             # 重连 → 撤销计时
        await asyncio.sleep(0.5)             # > grace

        assert runtime.client_presence.absent is False, "重连撤销计时，不得置缺席"
        assert _paused_events(session) == []

        released.set()
        await asyncio.wait_for(run.task, timeout=5)
        assert RUN_COMPLETED in [e.type for e in session.events]
    finally:
        await manager.aclose()


# ── 纯单元：ExitImpact 偏 busy（A-P3-3）+ quit-inspection 的 steer seam（A-P3-4）──


def test_exit_impact_uncertain_alone_is_busy() -> None:
    """[W-12 A-P3-3] 仅 `uncertain=True`（其余维全 False）时 `busy` 仍为 True——
    "判不准一律按有活处理"的偏置不是空话。"""
    impact = runmanager.ExitImpact(
        session_id="s",
        has_inflight_tool=False,
        has_inflight_child=False,
        has_pending_operation=False,
        needs_reconcile=False,
        has_queued_input=False,
        uncertain=True,
        detail=(),
    )
    assert impact.busy is True


@pytest.mark.asyncio
async def test_inspect_exit_impact_reads_steer_source_pending_count(tmp_path) -> None:
    """[W-12 A-P3-4] `inspect_exit_impact` 经 `runtime._steer_source.pending_count`
    只读 duck-type seam 统计排队输入：pending_count=2 ⇒ `has_queued_input is True`。
    钉住这个私有 seam（rename 会让本用例变红），防止"有排队输入"这一维悄悄失效。"""
    session = _session(tmp_path, cwd=tmp_path)
    runtime = _runtime(ScriptedModel([AIMessage(content="never")]))
    runtime._steer_source = SimpleNamespace(  # type: ignore[attr-defined]
        pending_count=AsyncMock(return_value=2)
    )
    manager = runmanager.RunManager(disconnect_grace_seconds=60.0)
    try:
        run = runmanager.ManagedRun(session, manager)
        run.presence_managed = True
        run.runtime = runtime
        manager._runs[session.session_id] = run

        impact = await manager.inspect_exit_impact(session.session_id)

        assert impact.has_queued_input is True
        runtime._steer_source.pending_count.assert_awaited_once_with(
            session.session_id
        )
    finally:
        await manager.aclose()
