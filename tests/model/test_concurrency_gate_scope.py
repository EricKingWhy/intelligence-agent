"""#559（M10-6）：模型并发闸的进程级作用域合同。

`model/concurrency.py` 的 docstring 合同与 PHASE_STATUS Phase12 加固记录都是
「进程内共享一个实例——全局在飞模型调用数」；缺陷（M10-6 实测）是
`assembly.build_runtime` 每次新建实例，实际作用域=会话——跨会话并发完全不受限。

本文件钉装配面合同（修复 = 闸归属装配生命周期）：
- 闸实例归 wiring（CapabilityWiring）所有，同一装配内所有 build_runtime 共享；
- 跨两个 Session 的真实峰值在飞 ≤ limit（屏障 + 峰值计数，不是 sleep 总耗时）；
- child factory 与 per-run fallback coordinator 拿同一实例；
- 摘要（ContextCompactor）的模型调用占同一闸的槽位；
- 取消路径归还 permit（不泄漏）。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
)

from agent_harness.agent.profiles import BUILTIN_PROFILES
from agent_harness.assembly import (
    assemble_wiring,
    build_runtime,
    initialize_stores,
    recovery_stores,
)
from agent_harness.config import Settings
from agent_harness.context.compactor import ContextCompactor
from agent_harness.model.concurrency import ModelCallGate
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling.contract import PermissionPolicy


def _settings(tmp_path, *, concurrency: int = 2, capabilities: dict | None = None) -> Settings:
    kwargs: dict = {
        "_env_file": None, "workspace_dir": str(tmp_path), "model_api_key": "sk-test",
        "model_max_concurrency": concurrency,
    }
    if capabilities is not None:
        kwargs["capabilities"] = json.dumps(capabilities)
    return Settings(**kwargs)


class _HeldStreamProbe:
    """astream 峰值计数探针：进入即计数，停在屏障上等 release（真实峰值口径）。"""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self._release = asyncio.Event()

    def bind_tools(self, tools, **kwargs):
        return self

    def release(self) -> None:
        self._release.set()

    async def astream(self, messages, **kwargs):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.wait_for(self._release.wait(), timeout=10.0)
            yield AIMessageChunk(content="ok")
        finally:
            self.in_flight -= 1


class _HeldSummaryProbe:
    """ainvoke 峰值计数探针（摘要模型接缝）：返回合法四节摘要。"""

    MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self._release = asyncio.Event()

    def release(self) -> None:
        self._release.set()

    async def ainvoke(self, messages, **kwargs):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.wait_for(self._release.wait(), timeout=10.0)
            return AIMessage(content=self.MODEL_SECTIONS)
        finally:
            self.in_flight -= 1


def _compact_messages() -> list:
    """可压缩投影：leading System + 旧轮（大 AIMessage）+ 当前请求。"""
    return [
        SystemMessage(content="system policy"),
        HumanMessage(content="读取旧记录并继续。"),
        AIMessage(content="历史分析 " * 800),
        HumanMessage(content="current request"),
    ]


async def _build_runtimes(tmp_path, settings, wiring, model, count, session_store=None):
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    workspace_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    runtimes = []
    with patch("agent_harness.assembly.create_chat_model", return_value=model):
        for index in range(count):
            runtimes.append(await build_runtime(
                settings=settings, wiring=wiring, stores=stores,
                workspace_registry=workspace_registry,
                session_id=f"sess-gate-{index}",
                workspace=tmp_path / "workspaces" / f"sess-gate-{index}",
                max_agent_turns=10,
                permission_mode=PermissionPolicy.WORKSPACE_WRITE,
                session_store=session_store,
            ))
    return runtimes


@pytest.mark.asyncio
async def test_gate_owned_by_wiring_and_shared_across_sessions(tmp_path):
    """闸实例归属装配生命周期：同一 wiring 的两个 Session 共享同一实例。"""
    settings = _settings(tmp_path, concurrency=2)
    _, wiring = await assemble_wiring(settings)
    gate = getattr(wiring, "model_call_gate", None)
    assert gate is not None, "wiring 应持有进程级共享闸（model_call_gate）"
    assert gate.limit == 2
    runtimes = await _build_runtimes(tmp_path, settings, wiring, _HeldStreamProbe(), count=2)
    assert runtimes[0]._model_call_gate is gate
    assert runtimes[1]._model_call_gate is gate, "跨会话必须共享同一闸实例（M10-6 根因）"


@pytest.mark.asyncio
async def test_cross_session_in_flight_peak_capped(tmp_path):
    """6 个跨两 Session 的并发模型调用（limit=2）：真实峰值在飞 ≤ 2，排队不丢。"""
    settings = _settings(tmp_path, concurrency=2)
    _, wiring = await assemble_wiring(settings)
    model = _HeldStreamProbe()
    runtimes = await _build_runtimes(tmp_path, settings, wiring, model, count=2)

    async def consume(runtime):
        agen = runtime._model_call_gate.wrap(model.astream([]))
        return [chunk async for chunk in agen]

    tasks = [asyncio.create_task(consume(runtime))
             for runtime in runtimes for _ in range(3)]
    await asyncio.sleep(0.3)  # 让 6 个调用全部发起、闸队列稳定
    model.release()
    results = await asyncio.gather(*tasks)
    assert all(len(r) == 1 for r in results), "排队不丢调用"
    assert model.peak <= 2, f"跨会话峰值在飞 {model.peak} 超过上限 2（会话隔离闸=2x2）"


@pytest.mark.asyncio
async def test_child_factory_and_fallback_coordinator_share_gate(tmp_path):
    """child factory 与 per-run fallback coordinator 拿的是 wiring 的同一闸实例。"""
    caps = {"multiagent": {"provider": "builtin", "enabled": True, "options": {}}}
    settings = _settings(tmp_path, concurrency=2, capabilities=caps)
    _, wiring = await assemble_wiring(settings)
    gate = getattr(wiring, "model_call_gate", None)
    assert gate is not None, "wiring 应持有进程级共享闸"
    store = JsonlSessionStore(root=tmp_path / "sessions")
    runtimes = await _build_runtimes(
        tmp_path, settings, wiring, _HeldStreamProbe(), count=1, session_store=store,
    )
    runtime = runtimes[0]
    # multiagent 的 factory 激活发生在 per-runtime provider 实例上（wiring 上
    # 只是原型）；该实例挂在 registry 的 delegate 工具里。
    delegate = next(t for t in runtime.registry.list() if t.name == "delegate")
    factory = delegate._provider._factory
    assert factory is not None, "session_store 在场 → delegate 激活，factory 应在场"
    assert factory._model_call_gate is gate
    child = factory.create(
        BUILTIN_PROFILES["coding"], source_registry=runtime.registry,
        grantable={tool.name for tool in runtime.registry.list()},
    )
    assert child._model_call_gate is gate, "child runtime 与根共享同一闸"
    # child 的内建 ContextBuilder（factory 未注入 context_builder 时走 runtime
    # 兜底构造）也必须接同一闸——否则 child 摘要绕过进程级在飞上限。
    assert child._context_builder.model_call_gate is gate, (
        "child 内建 ContextBuilder 与根共享同一闸"
    )
    assert runtime._new_coordinator()._gate is gate, "per-run coordinator 与根共享同一闸"


@pytest.mark.asyncio
async def test_summary_wiring_shares_process_gate(tmp_path):
    """装配面：ContextBuilder 携带 wiring 的闸 → 摘要与主循环同闸。"""
    settings = _settings(tmp_path, concurrency=2)
    _, wiring = await assemble_wiring(settings)
    runtimes = await _build_runtimes(tmp_path, settings, wiring, _HeldStreamProbe(), count=1)
    builder_gate = getattr(runtimes[0]._context_builder, "model_call_gate", None)
    assert builder_gate is wiring.model_call_gate, "摘要调用必须走进程级共享闸"


@pytest.mark.asyncio
async def test_summary_call_consumes_shared_gate_slot():
    """摘要调用与其它模型调用同闸：limit=1 时三个并发 compact 的真实峰值 ≤1。"""
    gate = ModelCallGate(1)
    model = _HeldSummaryProbe()
    compactors = [ContextCompactor(
        model, max_context_tokens=100_000, auto_compact_threshold=0.3,
        model_call_gate=gate,
    ) for _ in range(3)]
    messages = _compact_messages()

    async def compact_one(compactor):
        return await compactor.compact(list(messages), token_estimate=80_000)

    tasks = [asyncio.create_task(compact_one(c)) for c in compactors]
    await asyncio.sleep(0.3)
    model.release()
    results = await asyncio.gather(*tasks)
    assert all(r.compacted_turn_count > 0 for r in results), "三个 compact 都应压缩成功"
    assert model.peak <= 1, f"摘要峰值在飞 {model.peak} 超过闸上限 1（摘要此前完全不过闸）"


@pytest.mark.asyncio
async def test_cancelled_summary_returns_permit_to_shared_gate():
    """取消路径：占槽中的摘要被取消 → permit 归还，后续调用不被卡死。"""
    gate = ModelCallGate(1)
    model = _HeldSummaryProbe()
    compactor = ContextCompactor(
        model, max_context_tokens=100_000, auto_compact_threshold=0.3,
        model_call_gate=gate,
    )
    messages = _compact_messages()
    task = asyncio.create_task(compactor.compact(list(messages), token_estimate=80_000))
    for _ in range(200):
        if model.in_flight:
            break
        await asyncio.sleep(0.01)
    assert model.in_flight == 1, "摘要调用应已进入在飞状态"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert model.in_flight == 0
    task2 = asyncio.create_task(compactor.compact(list(messages), token_estimate=80_000))
    entered = False
    for _ in range(200):
        if model.in_flight:
            entered = True
            break
        await asyncio.sleep(0.01)
    assert entered, "取消后 permit 应已归还：第二个 compact 应立即拿到槽位"
    model.release()
    result = await task2
    assert result.compacted_turn_count > 0
