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

from agent_harness.agent.budget import BudgetConflict
from agent_harness.agent.factory import AgentFactory
from agent_harness.agent.resume_evidence import (
    digest_policy_inputs,
    environment_revision,
)
from agent_harness.agent.run_budget import (
    REASON_STUCK,
    RESUME_BASIS_ENVIRONMENT_CHANGE,
    RESUME_BASIS_POLICY_CHANGE,
    RESUME_BASIS_RELEVANT_STEER,
    latest_paused_run,
)
from agent_harness.assembly import BUILTIN_LOCAL_TOOLS
from agent_harness.config import Settings
from agent_harness.model.config import ConfigError
from agent_harness.model.scripted import ScriptedModel
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.session import RUN_COMPLETED, RUN_FAILED, RUN_PAUSED, Session
from agent_harness.session.amend import AmendOptions
from agent_harness.session.model_switch import ModelTarget, append_model_change
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
    child_model: ScriptedModel | None = None,
    expect_ok: bool = True,
) -> str:
    """走生产 spawn 路径（DelegateTool → provider → AgentFactory）造一个真实子会话。

    `parent_cwd` 给定 = 有 cwd 锚的父（生产常态）；None = 无 cwd 锚的父（AC5 腿）。
    source registry 按生产 main 父装配（BUILTIN_LOCAL_TOOLS + update_plan + delegate），
    保证 spawn 时的收窄面 = spec.tool_scope ∩ 生产可注册集。
    `child_model`（#370）：给 child 换剧本（如必然 stuck 的失败循环）；子代理非
    completed ⇒ delegate 返回 failure，用 `expect_ok=False` 放行该形状。
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
            model=child_model or ScriptedModel([AIMessage(content="child done")]),
            primary_model_name="main-model",
            executor_factory=_executor_factory,
        ),
        source_registry=source,
        session_store=state.store,
        workspace_registry=registry,
        parent_session_id=PARENT_ID,
    )

    result = await delegate.execute(_args(target, "child task"))
    if expect_ok:
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


# ── AC2 补强：委派标记在而 agent_id 不可解析 ⇒ 响亮失败（fail-closed）──────


def _corrupt_started_agent_id(
    harness: _Harness, session_id: str, new_agent_id: str | None
) -> None:
    """篡改 session/started 信封的 agent_id（模拟损坏 / 历史遗留数据）。"""
    path = harness.state.sessions_root / session_id / "events.jsonl"
    rewritten: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("type") == "session/started":
            if new_agent_id is None:
                obj.pop("agent_id", None)
            else:
                obj["agent_id"] = new_agent_id
        rewritten.append(json.dumps(obj, ensure_ascii=False))
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


@pytest.mark.parametrize("broken_agent_id", ["ghost-profile", None])
@pytest.mark.asyncio
async def test_child_resume_with_unresolvable_agent_id_refuses_full_surface(
    tmp_path: Path, monkeypatch, broken_agent_id,
):
    """委派标记在而 agent_id 缺失/未知 ⇒ 拒绝恢复，不静默回落全集工具面。

    `_delegated_child_agent_profile` 的 fail-closed 分支：无法按子会话自己的
    AgentSpec 重建授权时，唯一安全的动作是响亮失败——静默返回 None 等于把
    恢复入口开成 BUILTIN_LOCAL_TOOLS 全集（变异实证：把 raise 改成
    `return None` 后本测试是唯一转红者）。
    """
    harness = _build_harness(tmp_path, monkeypatch)
    child_id = await _spawn_child(harness, parent_cwd=tmp_path / "project")
    _corrupt_started_agent_id(harness, child_id, broken_agent_id)

    types_before = harness.types(child_id)
    with pytest.raises(ValueError):
        await harness.service.resume_and_launch(session_id=child_id, task="继续")
    # 响亮失败且零副作用：不落 session/resumed、不建 run、不改注册表。
    assert harness.types(child_id) == types_before


# ── #370（ADR-0048 残余 15）：child stuck 暂停带**它自己的**生效策略面 ──────
#
# #372 修好了恢复入口（alias get + 按 AgentSpec 重建档位），本票把快照的策略那一半
# 补上：`AgentFactory.create` 经 `delegated_child_evidence_port`（唯一口径）给 child
# 注入证据端口。下面用 #372 的交付入口验证票面 AC：
# - AC1：child 暂停载荷的 policy 格 = child 自己的面（默认档 + spec 档位 +
#   无独立模型/effort/providers 声明），逐维值与摘要同源；
# - AC2：`policy_change` 恢复——什么都不声明（字段省略不是变更）⇒ 409「相同」；
#   声明 child 自己的一维 ⇒ 依据成立，恢复腿真按新档跑（第二次暂停的快照记着新值）；
# - AC3：**父**会话的策略漂移（模型切换）打不开 child 的 `policy_change`。
# （AC4 环境格的钉子：无锚臂在 tests/agent/test_stuck_runtime.py 的策略面测试里；
#   带锚点亮 = #608，见本文件末尾。）


def _failing_round(index: int) -> AIMessage:
    """同一个未注册工具、同一份参数：动作指纹恒定 ⇒ 连续失败累积（同 CLI 用例）。"""
    return AIMessage(
        content="",
        tool_calls=[{
            "id": f"call_{index:04d}", "name": "no_such_tool", "args": {"command": "ls"},
        }],
    )


def _stuck_child_model() -> ScriptedModel:
    """必然撞满 stuck 阈值的 child 剧本；closeout 由确定性 continuation 兜底。"""
    return ScriptedModel(responses=[_failing_round(index) for index in range(6)])


def _paused_runs(harness: _Harness, session_id: str) -> list:
    return [e for e in harness.events(session_id) if e.type == RUN_PAUSED]


async def _wait_paused_runs(harness: _Harness, session_id: str, *,
                            count: int, timeout: float = 15.0) -> None:
    """等第 count 次 `run/paused` 落盘（ScriptedModel 毫秒级；轮询避免猜时序）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(_paused_runs(harness, session_id)) < count:
        if loop.time() >= deadline:
            raise AssertionError(
                f"超时 {timeout}s：run/paused 未到 {count} 条"
                f"（{harness.types(session_id)[-6:]}）"
            )
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_child_policy_change_resume_gated_by_its_own_face(
    tmp_path: Path, monkeypatch,
):
    harness = _build_harness(tmp_path, monkeypatch)
    child_id = await _spawn_child(
        harness, parent_cwd=tmp_path / "project",
        child_model=_stuck_child_model(), expect_ok=False,
    )
    paused = latest_paused_run(harness.events(child_id))
    assert paused is not None and paused.reason == REASON_STUCK

    # AC1：child 自己的面（不是父的面照抄一份），逐维值与摘要同源。
    recorded = (paused.stuck or {})["policy_inputs"]
    assert recorded == {
        "permission_mode": "workspace-write",
        "model": None,
        "agent_profile": "research_review",
        "reasoning_effort": None,
        "context_providers": None,
    }
    assert (paused.stuck or {})["policy_version"] == digest_policy_inputs(recorded)

    # AC2b：什么都不声明 ⇒ 恢复侧还原快照再比 ⇒ 409「相同」，零副作用。
    types_before = harness.types(child_id)
    with pytest.raises(BudgetConflict, match="相同"):
        await harness.service.resume_and_launch(
            session_id=child_id, task=None, resume_run_id=paused.run_id,
            resume_basis=RESUME_BASIS_POLICY_CHANGE, expected_version=paused.version,
        )
    assert harness.types(child_id) == types_before

    # AC2a：声明 child 自己的一维（reasoning_effort）⇒ 依据成立；恢复腿撞上同一个
    # 循环再次暂停，第二次快照记着新档——"采纳"的机械证据（不是只把闸门放开）。
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(
            responses=[_failing_round(index) for index in range(6, 12)],
        ),
    )
    resumed = await harness.service.resume_and_launch(
        session_id=child_id, task=None, resume_run_id=paused.run_id,
        resume_basis=RESUME_BASIS_POLICY_CHANGE, expected_version=paused.version,
        amend=AmendOptions(reasoning_effort="deep"),
    )
    await resumed.run.task
    await _wait_paused_runs(harness, child_id, count=2)
    second = latest_paused_run(harness.events(child_id))
    assert second.run_id == paused.run_id  # 同 run 续跑：同一条逻辑 run
    assert second.stuck["policy_inputs"]["reasoning_effort"] == "deep"


@pytest.mark.asyncio
async def test_parent_face_drift_does_not_open_child_policy_change(
    tmp_path: Path, monkeypatch,
):
    harness = _build_harness(tmp_path, monkeypatch)
    child_id = await _spawn_child(
        harness, parent_cwd=tmp_path / "project",
        child_model=_stuck_child_model(), expect_ok=False,
    )
    paused = latest_paused_run(harness.events(child_id))
    assert paused is not None and paused.reason == REASON_STUCK

    # 父会话真实的策略漂移（MODEL_CHANGED 唯一写入口）：父切了模型。
    parent = Session.load(harness.state.store, PARENT_ID)
    append_model_change(parent, ModelTarget(
        provider="deepseek", model_id="other-model", effective_model_id="other-model",
    ))

    # 父的面变了 ≠ child 的依据：恢复侧重算只读 child 自己的事件流 +
    # 强制的 child 档位，父级漂移到不了 child 的摘要 ⇒ 仍然 409「相同」。
    with pytest.raises(BudgetConflict, match="相同"):
        await harness.service.resume_and_launch(
            session_id=child_id, task=None, resume_run_id=paused.run_id,
            resume_basis=RESUME_BASIS_POLICY_CHANGE, expected_version=paused.version,
        )


# ── #608（ADR-0048 残余 15 环境半）：child 暂停快照的环境格 = 父锚同源 ──────
#
# D8 勘误注记预写的后续票：恢复入口（#372）与 child 策略面（#370）都已交付 ⇒
# 环境那一半按 D8 规则注入。同源链：provider._parent_cwd() → factory.create(
# workspace=…) → 端口 environment_revision；恢复侧 session_cwd(子事件流) →
# Path(persisted_cwd)——读回的是**同一事件字段**（子 SESSION_STARTED 的 cwd），
# 同源性是构造保证（#317 T9 审查 P1 的"两侧同一套输入"纪律），不靠数值巧合。


@pytest.mark.asyncio
async def test_child_env_cell_lights_up_same_source_as_resume_side(
    tmp_path: Path, monkeypatch,
):
    harness = _build_harness(tmp_path, monkeypatch)
    anchor = tmp_path / "project"
    child_id = await _spawn_child(
        harness, parent_cwd=anchor,
        child_model=_stuck_child_model(), expect_ok=False,
    )
    paused = latest_paused_run(harness.events(child_id))
    assert paused is not None and paused.reason == REASON_STUCK
    recorded = (paused.stuck or {})["environment_revision"]

    # AC1：环境格点亮（#370 时恒 None），且 = 父锚目录的暂停时刻观测值。
    assert recorded is not None
    assert recorded == environment_revision(anchor)
    # 格点亮 ⇒ environment_change 进入可用依据清单（顺序 = run_budget 的清单序）。
    assert paused.resume_requirements == (
        RESUME_BASIS_RELEVANT_STEER, RESUME_BASIS_ENVIRONMENT_CHANGE,
        RESUME_BASIS_POLICY_CHANGE,
    )

    # AC2 同源双向·腿一：树未变 ⇒ 恢复侧重算（Path(persisted_cwd) 同一目录）得
    # **相同**摘要 ⇒ 409「相同」——不造假变更（fail-open 防线），零副作用。
    types_before = harness.types(child_id)
    with pytest.raises(BudgetConflict, match="相同"):
        await harness.service.resume_and_launch(
            session_id=child_id, task=None, resume_run_id=paused.run_id,
            resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
            expected_version=paused.version,
        )
    assert harness.types(child_id) == types_before

    # AC2 腿二：树变了 ⇒ 摘要不同 ⇒ 依据成立，恢复腿真跑到终态（机械证据）。
    (anchor / "changed.txt").write_text("环境变了", encoding="utf-8")
    resumed = await harness.service.resume_and_launch(
        session_id=child_id, task=None, resume_run_id=paused.run_id,
        resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
        expected_version=paused.version,
    )
    await resumed.run.task
    await _wait_terminal(harness, child_id)


@pytest.mark.asyncio
async def test_child_without_parent_anchor_keeps_env_cell_fail_closed(
    tmp_path: Path, monkeypatch,
):
    """AC3 无锚臂：父无 cwd 锚 ⇒ 环境格如实缺席，environment_change fail-closed。

    恢复侧即便 fallback 到 `<workspaces_root>/<child_id>` 观测到某个目录，
    `recorded_environment_revision=None` ⇒ 判据仍 409「无快照可比」——现有判据
    零改动，不放行。（本测试修复前后都应绿：它是保留钉，不是红测。）
    """
    harness = _build_harness(tmp_path, monkeypatch)
    child_id = await _spawn_child(
        harness, parent_cwd=None,
        child_model=_stuck_child_model(), expect_ok=False,
    )
    paused = latest_paused_run(harness.events(child_id))
    assert paused is not None and paused.reason == REASON_STUCK
    assert (paused.stuck or {})["environment_revision"] is None
    assert RESUME_BASIS_ENVIRONMENT_CHANGE not in paused.resume_requirements
    with pytest.raises(BudgetConflict, match="无快照可比"):
        await harness.service.resume_and_launch(
            session_id=child_id, task=None, resume_run_id=paused.run_id,
            resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
            expected_version=paused.version,
        )



# ── P3（#607 批审查登记）：父 cwd 读失败的两个失败臂 ──
#
# 点亮/无锚两臂只覆盖「读到 / 根本没有锚」；_read_parent_cwd 的**读失败路径**
# （store 抛异常 → warning → (None, True)，失败不缓存「换一次 spawn 再试」）无钉。
# 两腿分别钉：瞬时失败不缓存（provider 级契约）、持久失败不拖垮委派且恢复
# fail-closed（spawn 级，与无锚臂同一收口——成因不同：有锚但读不到）。


def test_parent_cwd_transient_failure_is_not_cached(tmp_path: Path, monkeypatch):
    """失败不缓存 ⇒ 下一次调用必须**重读**（不能把 None 钉死）；成功后恢复缓存语义。

    断言落在可观察的**读取次数**上，不依赖 provider 的私有缓存旗标。
    """
    harness = _build_harness(tmp_path, monkeypatch)
    anchor = tmp_path / "project"
    Session.start(harness.state.store, session_id=PARENT_ID, cwd=anchor)
    real_read = harness.state.store.read_events
    reads = {"n": 0, "failed_once": False}

    def flaky(session_id, *args, **kwargs):
        if session_id == PARENT_ID:
            reads["n"] += 1
            if not reads["failed_once"]:
                reads["failed_once"] = True
                raise OSError("transient store failure")
        return real_read(session_id, *args, **kwargs)

    monkeypatch.setattr(harness.state.store, "read_events", flaky)
    provider = InProcessSubagentProvider()
    provider._session_store = harness.state.store
    provider._parent_session_id = PARENT_ID

    assert provider._parent_cwd() is None    # 失败 → None（按未分组处理）
    assert reads["n"] == 1                   # 失败没有被缓存
    value = provider._parent_cwd()           # 重读成功（不是拿缓存的 None）
    assert reads["n"] == 2
    assert value is not None and Path(value).resolve() == anchor.resolve()
    assert provider._parent_cwd() == value   # 缓存命中
    assert reads["n"] == 2                   # 成功后才缓存：不再重读


@pytest.mark.asyncio
async def test_parent_store_unreadable_delegation_fails_closed_loudly(
    tmp_path: Path, monkeypatch,
):
    """持久读失败臂：activate 预读（provider.py L256-307）也读不到 ⇒
    _tree_metadata_error=True ⇒ reserve_delegation 响亮拒绝（不发新树预算），
    不开幽灵子会话。父锚真实存在（有锚但读不到），成因与无锚臂不同。"""
    harness = _build_harness(tmp_path, monkeypatch)
    anchor = tmp_path / "project"
    real_read = harness.state.store.read_events

    def always_fail(session_id, *args, **kwargs):
        if session_id == PARENT_ID:
            raise OSError("transient store failure")
        return real_read(session_id, *args, **kwargs)

    monkeypatch.setattr(harness.state.store, "read_events", always_fail)
    harness.state.workspace_registry.create(PARENT_ID, workspace_root=anchor)
    Session.start(harness.state.store, session_id=PARENT_ID, cwd=anchor)

    provider_impl = InProcessSubagentProvider()
    provider_impl.activate(
        factory=AgentFactory(
            model=ScriptedModel([AIMessage(content="child done")]),
            primary_model_name="main-model",
            executor_factory=lambda child_registry: ToolExecutor(child_registry),
        ),
        source_registry=ToolRegistry(),
        session_store=harness.state.store,
        workspace_registry=harness.state.workspace_registry,
        parent_session_id=PARENT_ID,
    )
    delegate = DelegateTool(provider_impl)
    result = await delegate.execute(_args("research_review", "child task"))
    assert result.ok is False
    assert "无法确认委派树预算" in result.message  # DelegateTool 对 reserve 拒绝的包装语
    assert provider_impl.last_child_sessions == []  # 无幽灵子会话
