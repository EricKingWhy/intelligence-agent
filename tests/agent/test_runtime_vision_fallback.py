"""#823 / MM-02（A2）：投影视觉判定跟随**当前请求模型**（PRD D6 fallback 降级）。"""

from __future__ import annotations

from agent_harness.agent import AgentRuntime
from agent_harness.context.builder import ContextBuilder
from agent_harness.model.fallback import ModelFallbackCoordinator
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.scripted_model import ScriptedModel


def _runtime(builder: ContextBuilder, **kwargs) -> AgentRuntime:
    registry = ToolRegistry()
    return AgentRuntime(
        ScriptedModel([]), registry, ToolExecutor(registry),
        context_builder=builder, **kwargs,
    )


def test_builder_set_supports_vision_toggles_flag() -> None:
    builder = ContextBuilder(ScriptedModel([]))
    assert builder._supports_vision is False
    builder.set_supports_vision(True)
    assert builder._supports_vision is True


def test_runtime_syncs_builder_vision_by_role() -> None:
    builder = ContextBuilder(ScriptedModel([]))
    runtime = _runtime(
        builder, vision_by_model_role={"primary": True, "fallback": False},
    )
    runtime._sync_context_vision("primary")
    assert builder._supports_vision is True
    runtime._sync_context_vision("fallback")
    assert builder._supports_vision is False


def test_runtime_without_mapping_does_not_touch_builder() -> None:
    """未注入映射（既有调用方/测试）⇒ 不介入，builder 视觉位保持构造期值。"""
    builder = ContextBuilder(ScriptedModel([]), model_supports_vision=True)
    runtime = _runtime(builder)
    runtime._sync_context_vision("fallback")
    assert builder._supports_vision is True


def test_sync_is_noop_for_builder_without_seam() -> None:
    """非 ContextBuilder 的测试替身没有该接缝 ⇒ 静默跳过，不中断 run。"""

    class _StubBuilder:
        pass

    runtime = _runtime(_StubBuilder())  # type: ignore[arg-type]
    runtime._vision_by_model_role = {"primary": True, "fallback": False}
    runtime._sync_context_vision("fallback")  # 不应抛错


def test_coordinator_current_role_reflects_switch() -> None:
    primary = ScriptedModel([])
    fallback = ScriptedModel([])
    coord = ModelFallbackCoordinator(primary=primary, fallback=fallback)
    assert coord.current_role == "primary"
    # 模拟 `_try_switch` 的效果（never 切回）：current 指向 fallback。
    coord.current = fallback
    assert coord.current_role == "fallback"
