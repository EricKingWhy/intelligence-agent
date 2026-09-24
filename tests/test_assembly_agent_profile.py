"""build_runtime agent_profile 运行时消费（ADR-0020a，RUNTIME 子批次）。

覆盖五条契约：
  C1：agent_profile="coding" → registry 只含 _CODING_TOOLS（收窄）；
  C2：agent_profile="research_review" → registry 只含 _RESEARCH_TOOLS；
  C3：agent_profile="main" → registry 全量（不过滤，向后兼容）；
  C4：agent_profile=None（默认）→ registry 全量 + 无 system_prompt（向后兼容）；
  C5：任何非 None profile → ContextBuilder.system_prompt == profile 文本。

#238 追加两条对账契约（声明面 vs 注册面，AC2/AC3）：
  - optional capability 缺席 ⇒ effective = declared ∩ registered，缺席不进 dropped；
  - 注册了但未声明的工具（真实例 `read_artifact`）必须出现在 `dropped_tools`。

测试沿用 test_assembly.py 的装配模式：patch create_chat_model + 最小 CapabilityWiring。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk

from agent_harness.agent.profiles import BUILTIN_PROFILES, declared_tool_universe
from agent_harness.assembly import (
    BUILTIN_LOCAL_TOOLS,
    build_runtime,
    initialize_stores,
    recovery_stores,
)
from agent_harness.capability.wiring import CapabilityWiring
from agent_harness.config import Settings
from agent_harness.sandbox import WorkspaceRegistry


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
                    **overrides)


class ScriptedModelFactory:
    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(content="ok")


async def _build_runtime(tmp_path: Path, agent_profile: str | None, **settings_overrides):
    """装配一个 runtime（最小 wiring），返回 runtime 供断言 registry/system_prompt。"""
    settings = _settings(tmp_path, **settings_overrides)
    wiring = CapabilityWiring()
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    workspace_registry = WorkspaceRegistry(root=tmp_path, backend="local")

    with patch("agent_harness.assembly.create_chat_model", return_value=ScriptedModelFactory()):
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspace_registry,
            session_id="sess-profile",
            workspace=tmp_path / "workspaces" / "sess-profile",
            max_steps=10,
            agent_profile=agent_profile,
        )
    return runtime


@pytest.mark.asyncio
async def test_build_runtime_coding_profile_filters_registry(tmp_path):
    """C1：agent_profile="coding" → registry 只含 _CODING_TOOLS（无 delegate、无 research-only）。"""
    runtime = await _build_runtime(tmp_path, agent_profile="coding")
    tool_names = {tool.name for tool in runtime.registry.list()}

    coding_scope = BUILTIN_PROFILES["coding"].tool_scope
    assert tool_names == (coding_scope & tool_names), (
        f"coding profile registry 应只含 _CODING_TOOLS，实际：{tool_names}"
    )
    # 关键工具在
    assert {"read", "write", "bash", "edit", "apply_patch"} <= tool_names
    # delegate 不在（coding 不能委派）
    assert "delegate" not in tool_names


@pytest.mark.asyncio
async def test_build_runtime_research_profile_filters_registry(tmp_path):
    """C2：agent_profile="research_review" → registry 只含 _RESEARCH_TOOLS（无 write/bash）。"""
    runtime = await _build_runtime(tmp_path, agent_profile="research_review")
    tool_names = {tool.name for tool in runtime.registry.list()}

    research_scope = BUILTIN_PROFILES["research_review"].tool_scope
    assert tool_names == (research_scope & tool_names), (
        f"research_review profile registry 应只含 _RESEARCH_TOOLS，实际：{tool_names}"
    )
    # 只读工具在
    assert {"read", "grep", "glob"} <= tool_names
    # 写/执行工具不在（research 是只读角色）
    assert "write" not in tool_names
    assert "bash" not in tool_names


@pytest.mark.asyncio
async def test_build_runtime_main_profile_keeps_full_registry(tmp_path):
    """C3：agent_profile="main" → registry 含全量 coding 工具（不过滤）。"""
    runtime = await _build_runtime(tmp_path, agent_profile="main")
    tool_names = {tool.name for tool in runtime.registry.list()}

    # main 的 _MAIN_TOOLS 含全部 coding + research + delegate + inspect_artifact；
    # 无 capability 配置时实际注册的是 9 个 coding 工具（research/delegate 靠 wiring 注入）
    assert {"read", "write", "bash", "edit", "apply_patch",
            "grep", "glob", "git_status", "git_diff"} <= tool_names


@pytest.mark.asyncio
async def test_build_runtime_none_profile_keeps_full_registry(tmp_path):
    """C4：agent_profile=None（默认）→ registry 全量（向后兼容，不过滤）。"""
    runtime = await _build_runtime(tmp_path, agent_profile=None)
    tool_names = {tool.name for tool in runtime.registry.list()}

    # 默认行为不变：全部 coding 工具在册
    assert {"read", "write", "bash", "edit", "apply_patch",
            "grep", "glob", "git_status", "git_diff"} <= tool_names


@pytest.mark.asyncio
async def test_build_runtime_coding_profile_injects_system_prompt(tmp_path):
    """C5：agent_profile="coding" → runtime 的 ContextBuilder.system_prompt == coding 文本。"""
    runtime = await _build_runtime(tmp_path, agent_profile="coding")
    assert runtime._context_builder.system_prompt == BUILTIN_PROFILES["coding"].system_prompt


@pytest.mark.asyncio
async def test_build_runtime_none_profile_no_system_prompt(tmp_path):
    """agent_profile=None → ContextBuilder.system_prompt 是 None（向后兼容）。"""
    runtime = await _build_runtime(tmp_path, agent_profile=None)
    assert runtime._context_builder.system_prompt is None


@pytest.mark.asyncio
async def test_build_runtime_main_profile_injects_system_prompt(tmp_path):
    """agent_profile="main" → 注入 main system_prompt（显式选 main 有角色提示）。"""
    runtime = await _build_runtime(tmp_path, agent_profile="main")
    assert runtime._context_builder.system_prompt == BUILTIN_PROFILES["main"].system_prompt


# ── #238：声明面 / 注册面对账（AC2 交集、AC3 dropped） ──────────────────

#: `assembly.BUILTIN_LOCAL_TOOLS` 无条件注册的本地工具名——从常量**取**（`cls(None)`
#: 只读 `.name`，构造器不碰沙箱），不再手抄第三份：手抄的那份不会随常量漂移，
#: 而常量本身由 `tests/agent/test_tool_scope_reconciliation.py` 的 AST 闸看住。
_LOCAL_TOOL_NAMES = frozenset(cls(None).name for cls in BUILTIN_LOCAL_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["main", "coding", "research_review"])
async def test_effective_scope_is_intersection_when_capabilities_absent(tmp_path, profile):
    """#238 AC2：optional capability 缺席 ⇒ effective = declared ∩ registered。

    `artifact_dir=""` 关掉本地 artifact store（合法配置：写了就是"别落盘"），注册面只剩
    9 个本地工具，capability 工具（knowledge / websearch / memory / delegate）全部缺席。
    缺席**不是**被剔除：它们不进 registry，也**不**进 `dropped_tools`——后者只装"注册了
    但未声明"的名字。声明数（18/13/8）与实际数（9/9/3）在这里必然不等，这正是
    `tool_scope_summary` docstring 说的"这两个数不能读作实际工具数"。
    """
    runtime = await _build_runtime(tmp_path, agent_profile=profile, artifact_dir="")
    registered = {tool.name for tool in runtime.registry.list()}
    declared = BUILTIN_PROFILES[profile].tool_scope

    assert registered == declared & _LOCAL_TOOL_NAMES  # 交集，不多不少
    assert registered < declared                       # 缺席 ⇒ 严格子集（不虚报实际数）
    assert set(runtime.dropped_tools) == _LOCAL_TOOL_NAMES - declared
    # 缺席的名字一个都不许出现在 dropped 里（否则就是把"没配"报成"被剔除"）
    assert not (declared - registered) & set(runtime.dropped_tools)


@pytest.mark.asyncio
async def test_registered_but_undeclared_tool_lands_in_dropped_tools(tmp_path):
    """#238 AC3：注册了但未声明的工具必须出现在 `dropped_tools`（真实例 `read_artifact`）。

    本地 artifact store（默认 Provider）注册的读回工具是 `read_artifact`，而声明面只声明
    了 S3 配对的 `inspect_artifact`——审计 §5.7 实测：这是 coding 收窄后**唯一**被剔除的
    工具，也是前端 tooltip 列不出来的名字。main 不过滤 ⇒ 同一工具在册、dropped 为空。
    """
    artifact_dir = str(tmp_path / "artifacts")
    universe = declared_tool_universe()

    coding = await _build_runtime(tmp_path, agent_profile="coding",
                                  artifact_dir=artifact_dir)
    # 声明面只有 S3 配对的 `inspect_artifact`，没有本地链路的 `read_artifact`
    assert "inspect_artifact" in universe and "read_artifact" not in universe
    assert "read_artifact" not in {tool.name for tool in coding.registry.list()}
    assert coding.dropped_tools == ("read_artifact",)

    main = await _build_runtime(tmp_path, agent_profile="main",
                                artifact_dir=artifact_dir)
    assert "read_artifact" in {tool.name for tool in main.registry.list()}
    assert main.dropped_tools == ()
