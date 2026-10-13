"""#1093：所选 reasoning_effort 落 run/started + model/fallback（Inspector 可回溯）。

票面（issue #1093）钉三条契约：

  T1：`run/started.data.reasoning_effort` = 本轮**请求侧**选中的推理档位（harness
      语义档位，非 provider wire 值）；**无声明 ⇒ 不落键**——与 `model` / `budget`
      同一「不可得 ≠ 默认值」口径（`11 §6.1`：账目缺失是 unavailable，不是默认档）。
      web 的「默认」位置正是「请求省略该字段」（`ReasoningEffortSlider.tsx`），落到
      后端就是 None ⇒ 键缺席。
  T2：`model/fallback.data.reasoning_effort` = 切换后生效的**备用模型**档位；无声明 ⇒
      不落键（与 #226 的 `model` 口径同构：切换事实另记）。
  T3：加法不破坏旧事件形状——两处 None 时 data 键与改动前逐字一致。

成熟产品依据（判定 ADAPT）：LangSmith `ls_invocation_params`——run metadata 记录每次
调用的 provider 参数（top_p/seed 等）供事后按配置过滤/对比。落点抄仓内 #198
（`agent_profile` 总是写进 `run/started.data`）的加法模式。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.assembly import build_runtime, initialize_stores, recovery_stores
from agent_harness.capability.wiring import CapabilityWiring
from agent_harness.config import Settings
from agent_harness.model.fallback import (
    FallbackTransition,
    ModelFallbackCoordinator,
    TwoLevelFallbackPolicy,
)
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import MODEL_FALLBACK, RUN_STARTED
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _FailOnceModel:
    """第一次 ainvoke 抛指定异常，之后委托 inner（模拟瞬时故障 → 切 fallback）。"""

    def __init__(self, inner: Any, error: Exception) -> None:
        self._inner = inner
        self._error = error
        self._failed = False

    def bind_tools(self, tools: list, **kwargs: Any) -> _FailOnceModel:
        self._inner.bind_tools(tools, **kwargs)
        return self

    async def ainvoke(self, messages: list[AnyMessage], **kwargs: Any) -> Any:
        if not self._failed:
            self._failed = True
            raise self._error
        return await self._inner.ainvoke(messages, **kwargs)


def _runtime(
    *,
    reasoning_effort: str | None = None,
    fallback_reasoning_effort: str | None = None,
    primary: Any | None = None,
    fallback: Any | None = None,
) -> AgentRuntime:
    registry = ToolRegistry()
    model = primary if primary is not None else ScriptedModel([AIMessage(content="done")])
    return AgentRuntime(
        model=model, registry=registry, executor=ToolExecutor(registry),
        max_agent_turns=10,
        fallback_model=fallback,
        fallback_policy=TwoLevelFallbackPolicy() if fallback is not None else None,
        primary_model_name="primary-model",
        fallback_model_name="fallback-model",
        reasoning_effort=reasoning_effort,
        fallback_reasoning_effort=fallback_reasoning_effort,
    )


# ── T1：begin_run / run/started.data.reasoning_effort ────────────────


@pytest.mark.parametrize("effort", ["minimal", "standard", "deep"])
def test_begin_run_records_reasoning_effort_when_provided(tmp_path, effort):
    """有值落键（harness 档位名原样写，不做任何默认化/翻译）。"""
    session = make_session(tmp_path)
    session.begin_run(agent_id="default", reasoning_effort=effort)
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert started.data["reasoning_effort"] == effort


def test_begin_run_omits_reasoning_effort_when_none(tmp_path):
    """无值**不落键**——不是 null，也不是 "default"（不可得 ≠ 默认值）。"""
    session = make_session(tmp_path)
    session.begin_run(agent_id="default", reasoning_effort=None)
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert "reasoning_effort" not in started.data


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["minimal", "standard", "deep"])
async def test_run_started_carries_reasoning_effort(tmp_path, effort):
    """Runtime 接线：构造期给的档位落进 run/started.data（durable）。"""
    runtime = _runtime(reasoning_effort=effort)
    session = make_session(tmp_path)
    await runtime.run(session, "hi")
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert started.data["reasoning_effort"] == effort


@pytest.mark.asyncio
async def test_run_started_omits_reasoning_effort_when_unset(tmp_path):
    """未声明档位（模型无能力声明 / 默认位置）⇒ 键缺席。"""
    runtime = _runtime()
    session = make_session(tmp_path)
    await runtime.run(session, "hi")
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert "reasoning_effort" not in started.data


@pytest.mark.asyncio
async def test_assembly_threads_reasoning_effort_to_run_started(tmp_path):
    """装配路径：build_runtime(reasoning_effort=...) 透传进 run/started。"""
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
    )
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    workspace_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    with patch(
        "agent_harness.assembly.create_chat_model",
        return_value=ScriptedModel([AIMessage(content="ok")]),
    ):
        runtime = await build_runtime(
            settings=settings, wiring=CapabilityWiring(), stores=stores,
            workspace_registry=workspace_registry,
            session_id="sess-1093",
            workspace=tmp_path / "workspaces" / "sess-1093",
            max_agent_turns=10,
            reasoning_effort="deep",
        )
    session = make_session(tmp_path)
    await runtime.run(session, "hi")
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert started.data["reasoning_effort"] == "deep"


# ── T2：model/fallback.data.reasoning_effort ─────────────────────────


def test_fallback_event_data_includes_reasoning_effort():
    transition = FallbackTransition(
        from_model="a", to_model="b", reason="TimeoutError",
        reasoning_effort="minimal",
    )
    assert transition.event_data()["reasoning_effort"] == "minimal"


def test_fallback_event_data_omits_reasoning_effort_when_none():
    """T3：无档位时 data 形状与改动前逐字一致（加法不破坏旧事件）。"""
    transition = FallbackTransition(from_model="a", to_model="b", reason="TimeoutError")
    assert transition.event_data() == {
        "from_model": "a", "to_model": "b", "reason": "TimeoutError",
    }


@pytest.mark.asyncio
async def test_coordinator_records_fallback_reasoning_effort():
    """coordinator 把备用模型档位记进切换事实（drain 语义，Runtime 落盘用）。"""
    primary = _FailOnceModel(
        ScriptedModel([AIMessage(content="unused")]), TimeoutError("down"),
    )
    coordinator = ModelFallbackCoordinator(
        primary=primary, fallback=ScriptedModel([AIMessage(content="from fallback")]),
        primary_name="primary-model", fallback_name="fallback-model",
        fallback_reasoning_effort="standard",
    )
    await coordinator.ainvoke([])
    [transition] = coordinator.drain_transitions()
    assert transition.reasoning_effort == "standard"


@pytest.mark.asyncio
async def test_model_fallback_event_carries_fallback_effort(tmp_path):
    """Runtime 循环：切换后 model/fallback.data 记备用档位，run/started 记主档位。"""
    primary = _FailOnceModel(
        ScriptedModel([AIMessage(content="unused")]), TimeoutError("primary down"),
    )
    runtime = _runtime(
        reasoning_effort="deep", fallback_reasoning_effort="minimal",
        primary=primary, fallback=ScriptedModel([AIMessage(content="ok")]),
    )
    session = make_session(tmp_path)
    result = await runtime.run(session, "hi")
    assert result.status == "completed"
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert started.data["reasoning_effort"] == "deep"
    [fallback_event] = [e for e in session.events if e.type == MODEL_FALLBACK]
    assert fallback_event.data["reasoning_effort"] == "minimal"


@pytest.mark.asyncio
async def test_model_fallback_event_omits_effort_when_unset(tmp_path):
    """备用模型无档位声明 ⇒ model/fallback 键缺席（旧形状不变）。"""
    primary = _FailOnceModel(
        ScriptedModel([AIMessage(content="unused")]), TimeoutError("primary down"),
    )
    runtime = _runtime(primary=primary, fallback=ScriptedModel([AIMessage(content="ok")]))
    session = make_session(tmp_path)
    await runtime.run(session, "hi")
    [fallback_event] = [e for e in session.events if e.type == MODEL_FALLBACK]
    assert "reasoning_effort" not in fallback_event.data
