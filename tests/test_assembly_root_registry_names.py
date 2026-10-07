"""#564 裁决 (a)：service 层 pre-CAS 校验用的**根 registry 名字集**与真实装配一致。

`assembly._build_tooling` 是 build_runtime 与 service 注册名校验共用的唯一构造源；
一旦有人在 build_runtime 的 registry 构造里加新的工具来源而忘了它经过这里，
本文件的红灯就是漂移警报（而不是静默放行一个永远不触发的配额名）。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk

from agent_harness.assembly import (
    _build_tooling,
    assemble_wiring,
    build_runtime,
    initialize_stores,
    recovery_stores,
    root_registry_tool_names,
)
from agent_harness.config import Settings
from agent_harness.prompt.tool_sections import join_guidance
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.tools.request_constraint_resolution import (
    REGISTER_CONSTRAINT_HANDOFF,
)


class ScriptedModelFactory:
    """astream 可用的替身模型（装配可构造即可，不真调用）。"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(content="ok")


def _settings(tmp_path: Path, *, multiagent: bool) -> Settings:
    overrides: dict[str, object] = {
        "_env_file": None,
        "workspace_dir": str(tmp_path),
        "model_api_key": "sk-test",
    }
    if multiagent:
        overrides["capabilities"] = json.dumps(
            {"multiagent": {"provider": "builtin", "enabled": True, "options": {}}}
        )
    return Settings(**overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize("multiagent", [False, True], ids=["no-ma", "ma"])
@pytest.mark.parametrize(
    ("include_constraint_tools", "include_constraint_resolution_tool"),
    [(False, None), (True, None), (True, False)],
    ids=["none", "both", "registration-only"],
)
async def test_pre_cas_name_set_matches_real_root_registry(
    tmp_path, multiagent, include_constraint_tools,
    include_constraint_resolution_tool,
):
    """三方对账：`root_registry_tool_names`（零副作用）== `_build_tooling` == 真实装配。"""
    settings = _settings(tmp_path, multiagent=multiagent)
    _, wiring = await assemble_wiring(settings)
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    workspace_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    session_id = "sess-root-names"
    workspace = tmp_path / "workspaces" / session_id
    session_store = JsonlSessionStore(root=tmp_path / "sessions")

    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModelFactory()):
        runtime = await build_runtime(
            settings=settings,
            wiring=wiring,
            stores=stores,
            workspace_registry=workspace_registry,
            session_id=session_id,
            workspace=workspace,
            session_store=session_store,
            max_agent_turns=10,
            permission_mode=PermissionPolicy.WORKSPACE_WRITE,
            include_constraint_tools=include_constraint_tools,
            include_constraint_resolution_tool=include_constraint_resolution_tool,
        )

    tooling = _build_tooling(
        settings, wiring,
        session_id=session_id, workspace=workspace,
        workspace_registry=workspace_registry, session_store=session_store,
        agent_profile=None,
        include_constraint_tools=include_constraint_tools,
        include_constraint_resolution_tool=include_constraint_resolution_tool,
    )
    helper_names = {tool.name for tool in tooling.registry.list()}
    runtime_names = {tool.name for tool in runtime.registry.list()}
    # 第三方：pre-CAS validator 实际用的**零副作用**投影（审查 P2-1：不构造
    # sandbox，否则 #266 归属对账前就 mkdir workspace）。它与两份真实构造必须
    # 逐名一致——漂移 = 校验判据与真实工具面分叉。
    zeronames = root_registry_tool_names(
        settings, wiring, session_id=session_id, session_store=session_store,
        include_constraint_tools=include_constraint_tools,
        include_constraint_resolution_tool=include_constraint_resolution_tool,
    )
    assert helper_names == runtime_names == zeronames, (
        "pre-CAS 校验的名字集与真实根 registry 漂移（main 档位不收窄，三方必须相等）"
    )
    if multiagent:
        assert "delegate" in helper_names
    else:
        assert "delegate" not in helper_names
    assert ("register_constraint" in helper_names) == include_constraint_tools
    include_resolution_tool = (
        include_constraint_tools
        if include_constraint_resolution_tool is None
        else include_constraint_resolution_tool
    )
    assert ("request_constraint_resolution" in helper_names) == include_resolution_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("registration_only", [True, False], ids=["registration-only", "both"])
async def test_registered_register_constraint_guidance_matches_its_registry(tmp_path, registration_only):
    """#663 P2：装配出的 `register_constraint` 指引必须与**同一 registry** 的实情一致。

    工具指引点名 `request_constraint_resolution` 却不注册它 = 一条照做必然报错的假指令
    （CLI 入口就是 `include_constraint_resolution_tool=False`）。这段注入发生在
    `_build_tooling`，所以只有**穿过装配**取到那个工具实例才能钉住它——默认构造的
    guidance 断不到这条接线（`tests/tools/test_register_constraint.py` 只钉两端）。
    """
    settings = _settings(tmp_path, multiagent=False)
    _, wiring = await assemble_wiring(settings)
    session_id = "sess-register-guidance"
    tooling = _build_tooling(
        settings, wiring,
        session_id=session_id, workspace=tmp_path / "workspaces" / session_id,
        workspace_registry=WorkspaceRegistry(root=tmp_path, backend="local"),
        session_store=JsonlSessionStore(root=tmp_path / "sessions"),
        agent_profile=None,
        include_constraint_tools=True,
        include_constraint_resolution_tool=not registration_only,
    )
    names = {tool.name for tool in tooling.registry.list()}
    guidance = tooling.registry.get("register_constraint").prompt_guidance

    # 指引说的每件事，registry 里都必须真的在册（反过来的方向由名字集用例钉）。
    assert ("request_constraint_resolution" in guidance) == (
        "request_constraint_resolution" in names
    )
    # 注入的是**增量**转接句，不是 resolver guidance 的副本（Call 3 P2-1）：
    # resolver 独有的话只在它自己的 `tool:` section 里，不在 register 的 guidance 里。
    assert (REGISTER_CONSTRAINT_HANDOFF in guidance) == (
        "request_constraint_resolution" in names
    )
    assert "a pending card does not authorize the conflicting action" not in guidance.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("registration_only", [True, False], ids=["registration-only", "both"])
async def test_root_prompt_has_no_duplicated_register_and_resolver_guidance(tmp_path, registration_only):
    """Call 3 P2-1：拼装后的 system prompt 里 resolver 的行为指导只出现一次。

    resolver 的全文经 `tool:request_constraint_resolution` 独立进 system prompt。把全文
    再嵌进 `register_constraint` 的 guidance，等于同一份指导每次请求下发两遍（实测净增
    1403 字符）。这里在**真实装配**的 registry 上取两份 guidance 拼装对照——默认构造
    断不到这条接线。
    """
    settings = _settings(tmp_path, multiagent=False)
    _, wiring = await assemble_wiring(settings)
    session_id = "sess-guidance-dedup"
    tooling = _build_tooling(
        settings, wiring,
        session_id=session_id, workspace=tmp_path / "workspaces" / session_id,
        workspace_registry=WorkspaceRegistry(root=tmp_path, backend="local"),
        session_store=JsonlSessionStore(root=tmp_path / "sessions"),
        agent_profile=None,
        include_constraint_tools=True,
        include_constraint_resolution_tool=not registration_only,
    )
    registry = tooling.registry

    resolver_only = "a pending card does not authorize the conflicting action"
    if registration_only:
        # resolver 不在册：连它的句子都不该出现（点名不存在的工具）。
        assert resolver_only not in registry.get("register_constraint").prompt_guidance.lower()
        return
    joined = join_guidance([
        registry.get("register_constraint"), registry.get("request_constraint_resolution"),
    ]).lower()
    assert joined.count(resolver_only) == 1


@pytest.mark.asyncio
async def test_root_reconcile_info_projection_matches_real_root_registry(tmp_path):
    """#357 W-13 R12-R14：`root_registry_reconcile_info`（零副作用）与真实装配对账。

    恢复裁决展示字段（default_action/probe）的判据必须与真实根 registry 的
    工具面同源——键集漂移 = 展示字段对真实工具说谎（fail-closed 伪装成已知）。
    """
    from agent_harness.assembly import root_registry_reconcile_info
    from agent_harness.tooling.contract import ToolReconcileInfo
    from agent_harness.tools.bash import BashTool
    from agent_harness.tools.read import ReadTool

    settings = _settings(tmp_path, multiagent=False)
    _, wiring = await assemble_wiring(settings)
    session_id = "sess-reconcile-info"
    workspace = tmp_path / "workspaces" / session_id
    session_store = JsonlSessionStore(tmp_path / "sessions")

    tooling = _build_tooling(
        settings, wiring,
        session_id=session_id, workspace=workspace,
        workspace_registry=WorkspaceRegistry(tmp_path, backend="local"),
        session_store=session_store,
        agent_profile=None,
    )
    helper_names = {tool.name for tool in tooling.registry.list()}

    info = root_registry_reconcile_info(
        settings, wiring, session_id=session_id, session_store=session_store,
    )
    assert set(info) == helper_names == root_registry_tool_names(
        settings, wiring, session_id=session_id, session_store=session_store,
    ), "reconcile info 投影与真实根 registry 漂移"

    assert all(isinstance(v, ToolReconcileInfo) for v in info.values())
    # R13/R14 判据逐字对账：read（replay_safe=True）与 bash（默认 unsafe）。
    assert info[ReadTool(None).name].replay_safe is True
    assert info[ReadTool(None).name].verifiable is (
        ReadTool(None).reconcile_hint.verifiable
    )
    assert info[ReadTool(None).name].suggested_action == (
        ReadTool(None).reconcile_hint.suggested_action
    )
    assert info[BashTool(None).name].replay_safe is False
    assert info[BashTool(None).name].verifiable is (
        BashTool(None).reconcile_hint.verifiable
    )


def test_root_profile_spec_resolves_the_declared_profile():
    """#615②：根配额取用点的判定表——None/main 落 main，具名档位返回自身。"""
    from agent_harness.agent.profiles import BUILTIN_PROFILES
    from agent_harness.assembly import _root_profile_spec

    assert _root_profile_spec(None) is BUILTIN_PROFILES["main"]
    assert _root_profile_spec("main") is BUILTIN_PROFILES["main"]
    assert _root_profile_spec("coding") is BUILTIN_PROFILES["coding"]


@pytest.mark.asyncio
async def test_delegate_tree_quotas_are_single_sourced(tmp_path):
    """#615②：DelegateTool 树配额 == provider.activate 树账 == 档位声明。

    此前 `_build_tooling` 与 `build_runtime` 各写一遍取用式（双算）：漂移时
    工具面文案里的"整棵委派树最多 N 次"与树账的 max_delegations 各说各话，
    没有任何测试会红。双方现在都从 `_root_profile_spec` 取；本测试在真实装配
    上同时钉住两条消费面（`agent_profile=None` ⇒ main 档位）。
    """
    settings = _settings(tmp_path, multiagent=True)
    _, wiring = await assemble_wiring(settings)
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    workspace_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    session_id = "sess-quotas"
    workspace = tmp_path / "workspaces" / session_id
    session_store = JsonlSessionStore(root=tmp_path / "sessions")
    with patch("agent_harness.assembly.create_chat_model",
               return_value=ScriptedModelFactory()):
        runtime = await build_runtime(
            settings=settings,
            wiring=wiring,
            stores=stores,
            workspace_registry=workspace_registry,
            session_id=session_id,
            workspace=workspace,
            session_store=session_store,
            max_agent_turns=10,
            permission_mode=PermissionPolicy.WORKSPACE_WRITE,
        )

    from agent_harness.agent.profiles import BUILTIN_PROFILES
    from agent_harness.multiagent.tools import DelegateTool

    expected = BUILTIN_PROFILES["main"].max_delegations
    delegate = next(
        tool for tool in runtime.registry.list() if isinstance(tool, DelegateTool)
    )
    # 工具面消费点（_build_tooling）：工具描述里的"整棵委派树最多 N 次"。
    assert delegate._max_delegations == expected
    # 树账消费点（build_runtime → provider.activate）：委派计数判超的那份。
    assert delegate._provider._max_delegations == expected
