"""#372：委派子会话走 `resume_and_launch` 的恢复契约（端到端，真实组件）。

票面成因（ADR-0048 残余 16）：#288 给子会话写了持久 alias 绑定
（`multiagent/provider.py` 的 `bind_alias`），而装配层 `assembly.build_runtime`
对"新会话"与"恢复"共用且无条件 `workspace_registry.create()` ⇒ 子会话恢复必然
`WorkspaceBindingError`（web 层未映射 ⇒ 500）+ 孤儿 `session/resumed`。

修复契约（逐条对应票面验收）：
- AC1：恢复路径**取回**既有 alias 绑定（get 语义）且不改写映射；
  `create()` 对"冲突请求"的响亮拒绝语义保持不变（tests/sandbox/ 已钉）。
- AC2：恢复起的子 runtime 工具面 = 该子会话 AgentSpec 的收窄面（按
  session/started 的 `agent_id` 重建），**不得**等于装配全集
  （`BUILTIN_LOCAL_TOOLS`）——恢复入口不许放大子会话授权（不变量 #19、
  spec 10："恢复、重启子 Agent 都不能…放大"）。
- AC3：workspace 与属主绑定不一致的请求仍然响亮失败（#266 对账，不静默换目录）。
- AC4：装配期失败不再留孤儿 `session/resumed`（ADR-0048 残余 13 同族）。
- AC5：无 cwd 锚的父也在覆盖内：回落目录 `<workspaces_root>/<child_id>` 与
  属主目录不同值，恢复仍按 alias 解析到属主 workspace。

AgentSpec 恢复来源（票面决定性调研点的证据）：子会话 `session/started` 事件的
`agent_id` 信封字段 == spawn 时 `spec.name`（`multiagent/provider.py` 的
`session.append(SESSION_STARTED, {...}, agent_id=spec.name)`；`SessionEvent.agent_id`
随 JSONL 持久化），生产 provider 的 profiles 即 `BUILTIN_PROFILES`
（`capability/wiring.py` 构造 `InProcessSubagentProvider()` 无自定义注入）。

测试基建：真实 `AppState` + 真实 `SessionService` + 真实 `InProcessSubagentProvider`
（经 `DelegateTool` 走生产 spawn 路径，保证子会话事件形状与真机一致），只把
`create_chat_model` 换成 `ScriptedModel`（模式沿自 tests/session/test_multiturn_delivery.py）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent.factory import AgentFactory
from agent_harness.assembly import BUILTIN_LOCAL_TOOLS
from agent_harness.config import Settings
from agent_harness.model.config import ConfigError
from agent_harness.model.scripted import ScriptedModel
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.session import RUN_COMPLETED, RUN_FAILED, Session
from agent_harness.session.amend import AmendOptions
from agent_harness.session.service import WorkspaceBindingConflict
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tools import BashTool, GlobTool, GrepTool, ReadTool, WriteTool
from agent_harness.tools.update_plan import UpdatePlanTool
from agent_harness.web.app import AppState, session_service

PARENT_ID = "t372-parent"


class _Harness:
    """隔离环境：真实 AppState + Service + 事件读取助手。"""

    def __init__(self, state: AppState) -> None:
        self.state = state
        self.service = session_service(state)

    def events(self, session_id: str):
        return self.state.store.read_events(session_id)

    def types(self, session_id: str) -> list[str]:
        return [e.type for e in self.events(session_id)]


def _build_harness(tmp_path: Path, monkeypatch) -> _Harness:
    """AppState 全真件，只把 create_chat_model 换成确定性剧本。"""

    def _factory(config, **kwargs):
        return ScriptedModel([AIMessage(content="child resumed fine")])

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", _factory)
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_name="test-model",
        model_provider="deepseek",
        enable_cors=False,
    )
    return _Harness(AppState(settings))


def _capture_resumed_runtime(monkeypatch) -> list[Any]:
    """捕获恢复路径 build_runtime 产出的 runtime（透传不替换，行为零改动）。"""
    import agent_harness.session.service as service_module

    captured: list[Any] = []
    real_build = service_module.build_runtime  # 补丁前取原函数（防自引用递归）

    async def _spy(**kwargs):
        runtime = await real_build(**kwargs)
        captured.append(runtime)
        return runtime

    monkeypatch.setattr(service_module, "build_runtime", _spy)
    return captured


def _args(target: str, task: str) -> object:
    return type("_Args", (), {"target": target, "task": task,
                              "constraints": []})()


async def _spawn_child(
    harness: _Harness,
    *,
    parent_cwd: Path | None,
    target: str = "research_review",
    spawned_registries: list[ToolRegistry] | None = None,
) -> str:
    """走生产 spawn 路径（DelegateTool → provider → AgentFactory）造一个真实子会话。

    `parent_cwd` 给定 = 有 cwd 锚的父（生产常态）；None = 无 cwd 锚的父（AC5 腿）。
    source registry 按生产 main 父装配（BUILTIN_LOCAL_TOOLS + update_plan + delegate），
    保证 spawn 时的收窄面 = spec.tool_scope ∩ 生产可注册集。
    """
    state = harness.state
    registry = state.workspace_registry
    if parent_cwd is not None:
        registry.create(PARENT_ID, workspace_root=parent_cwd)
        Session.start(state.store, session_id=PARENT_ID, cwd=parent_cwd)
    else:
        registry.create(PARENT_ID)
        Session.start(state.store, session_id=PARENT_ID)

    sandbox = registry.get(PARENT_ID)
    source = ToolRegistry()
    for tool_cls in (ReadTool, WriteTool, BashTool, GlobTool, GrepTool):
        source.register(tool_cls(sandbox))
    source.register(UpdatePlanTool())

    provider_impl = InProcessSubagentProvider()
    delegate = DelegateTool(provider_impl)
    source.register(delegate)

    def _executor_factory(child_registry: ToolRegistry) -> ToolExecutor:
        if spawned_registries is not None:
            spawned_registries.append(child_registry)
        return ToolExecutor(child_registry)

    provider_impl.activate(
        factory=AgentFactory(
            model=ScriptedModel([AIMessage(content="child done")]),
            primary_model_name="main-model",
            executor_factory=_executor_factory,
        ),
        source_registry=source,
        session_store=state.store,
        workspace_registry=registry,
        parent_session_id=PARENT_ID,
    )

    result = await delegate.execute(_args(target, "child task"))
    assert result.ok, f"spawn 失败：{result.message}"
    # child_session_id 不在 tool result payload 里（走 pending_events）；provider
    # 的公开观测挂点（provider.py `last_child_sessions`）是测试取它的既定入口。
    return provider_impl.last_child_sessions[-1].session_id


async def _wait_terminal(harness: _Harness, session_id: str,
                         timeout: float = 15.0) -> None:
    """等恢复起的 run 到终态（ScriptedModel 毫秒级完成；轮询避免猜时序）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        types = harness.types(session_id)
        if RUN_COMPLETED in types:
            return
        if RUN_FAILED in types:
            raise AssertionError(f"run 意外失败：{types}")
        if loop.time() >= deadline:
            raise AssertionError(f"超时 {timeout}s：run 未到终态（{types[-6:]}）")
        await asyncio.sleep(0.02)


# ── AC1：恢复取回既有 alias 绑定，不改写 ──────────────────────────────


@pytest.mark.asyncio
async def test_child_resume_takes_back_alias_binding_without_rewrite(
    tmp_path: Path, monkeypatch,
):
    harness = _build_harness(tmp_path, monkeypatch)
    child_id = await _spawn_child(harness, parent_cwd=tmp_path / "project")

    mapping_path = tmp_path / "workspaces" / f"{child_id}.json"
    before = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert before["workspace_owner_session_id"] == PARENT_ID  # 前置：#288 alias 在

    result = await harness.service.resume_and_launch(
        session_id=child_id, task="继续",
    )
    await _wait_terminal(harness, child_id)

    after = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert after == before, "恢复不得改写既有 alias 绑定"
    owner_root = Path(
        harness.state.workspace_registry.get(PARENT_ID).workspace_root
    )
    assert Path(result.session.sandbox.workspace_root) == owner_root


# ── AC2：恢复 runtime 工具面 = 子会话原有收窄面（按 AgentSpec 重建）────


@pytest.mark.asyncio
async def test_resumed_child_runtime_keeps_agent_spec_narrowed_tools(
    tmp_path: Path, monkeypatch,
):
    harness = _build_harness(tmp_path, monkeypatch)
    captured = _capture_resumed_runtime(monkeypatch)
    spawned: list[ToolRegistry] = []
    child_id = await _spawn_child(
        harness, parent_cwd=tmp_path / "project",
        spawned_registries=spawned,
    )

    await harness.service.resume_and_launch(
        session_id=child_id, task="继续",
    )
    await _wait_terminal(harness, child_id)

    resumed_names = {t.name for t in captured[-1].registry.list()}
    spawn_names = {t.name for t in spawned[-1].list()}
    # research_review 的 AgentSpec 收窄面：本部署（capabilities 空）可注册的只有
    # 本地三件读工具；write/bash 等在装配全集里、被 spec 排除——具体名集合咬死。
    assert resumed_names == {"read", "grep", "glob"}
    # 恢复面 == spawn 时的原有收窄面（按同一 AgentSpec 重建）。
    assert resumed_names == spawn_names
    # 不得等于装配全集（BUILTIN_LOCAL_TOOLS ⊇ {read, write, bash, …}）。
    assert "write" not in resumed_names
    assert "bash" not in resumed_names
    builtin_names = {tool_cls.name for tool_cls in BUILTIN_LOCAL_TOOLS}
    assert resumed_names != builtin_names


# ── AC3：与属主绑定不一致的请求仍然响亮失败（守护：不静默换目录）──────


@pytest.mark.asyncio
async def test_child_resume_with_owner_root_drift_fails_loudly(
    tmp_path: Path, monkeypatch,
):
    harness = _build_harness(tmp_path, monkeypatch)
    child_id = await _spawn_child(harness, parent_cwd=tmp_path / "project")

    # 模拟属主映射漂移：owner 映射的 workspace_root 被改写到别处
    owner_mapping = tmp_path / "workspaces" / f"{PARENT_ID}.json"
    data = json.loads(owner_mapping.read_text(encoding="utf-8"))
    data["workspace_root"] = str(tmp_path / "elsewhere")
    owner_mapping.write_text(json.dumps(data), encoding="utf-8")

    types_before = harness.types(child_id)
    with pytest.raises(WorkspaceBindingConflict):
        await harness.service.resume_and_launch(
            session_id=child_id, task="继续",
        )
    # 响亮失败且零副作用：不静默换目录、不落任何恢复事件。
    assert harness.types(child_id) == types_before


# ── AC4：装配期失败不留孤儿 session/resumed ──────────────────────────


@pytest.mark.asyncio
async def test_assembly_failure_leaves_no_orphan_session_resumed(
    tmp_path: Path, monkeypatch,
):
    harness = _build_harness(tmp_path, monkeypatch)
    child_id = await _spawn_child(harness, parent_cwd=tmp_path / "project")
    types_before = harness.types(child_id)
    assert "session/resumed" not in types_before  # 前置：恢复标记尚不存在

    with pytest.raises(ConfigError):
        await harness.service.resume_and_launch(
            session_id=child_id, task="继续",
            amend=AmendOptions(model="no-such-model-in-catalog"),
        )

    assert harness.types(child_id) == types_before, (
        "装配期失败不得留下孤儿 session/resumed"
    )


# ── AC5：无 cwd 锚的父也在覆盖内（回落目录 ≠ 属主目录）────────────────


@pytest.mark.asyncio
async def test_child_without_cwd_anchor_resolves_to_owner_workspace(
    tmp_path: Path, monkeypatch,
):
    harness = _build_harness(tmp_path, monkeypatch)
    child_id = await _spawn_child(harness, parent_cwd=None)

    started = harness.events(child_id)[0]
    assert "cwd" not in (started.data or {})  # 前置：子会话无 cwd 锚
    fallback_dir = tmp_path / "workspaces" / child_id
    owner_root = Path(
        harness.state.workspace_registry.get(PARENT_ID).workspace_root
    )
    assert fallback_dir != owner_root  # 前置：两腿目录确实不同值

    result = await harness.service.resume_and_launch(
        session_id=child_id, task="继续",
    )
    await _wait_terminal(harness, child_id)

    # 恢复按 alias 解析到属主 workspace（回落目录只是无锚腿的既有 mkdir 产物，
    # 不是恢复落点——注册表登记的仍只有属主目录这一个事实）。
    assert Path(result.session.sandbox.workspace_root) == owner_root
    assert harness.state.workspace_registry.recorded_workspace_roots(child_id) == [
        str(owner_root)
    ]
