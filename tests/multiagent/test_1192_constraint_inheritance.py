"""#1192（#1176 Phase 3）Subagent 约束继承单测。

覆盖：
- 继承集计算：项目/全局 active 注入、会话级 active 授权注入、会话专属事实不注入、
  omit_constraints 干净启动、非 active（revoked/superseded）与同文覆盖排除；
- 快照序列化往返；
- 注入体（`inherited_protected_facts`）来源口径；
- Runtime 注入缝（`inherited_facts` → ContextBuilder，AC1 上下文含约束原文）；
- provider 端到端：子会话落 `agent/constraint-inheritance` 快照事件（AC5），
  omit_constraints=true 时 inherited 为空（AC4）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent.factory import AgentFactory
from agent_harness.agent.runtime import AgentRuntime
from agent_harness.multiagent.constraint_inheritance import (
    CONSTRAINT_INHERITANCE_EVENT,
    InheritanceSnapshot,
    compute_inheritance,
    inherited_protected_facts,
)
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session.derive import ProtectedFact
from agent_harness.session.event import (
    SESSION_STARTED,
    SessionEvent,
)
from agent_harness.session.scoped_constraints import ScopedConstraintStore
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

_REGISTERED_AT = "2026-10-16T00:00:00.000+00:00"


def _started(session_id: str = "parent-0", **data) -> SessionEvent:
    return SessionEvent(type=SESSION_STARTED, session_id=session_id, data=dict(data))


# ── 继承集计算 ────────────────────────────────────────────────────────────


def test_compute_inheritance_project_global_and_authorization(tmp_path: Path) -> None:
    store = ScopedConstraintStore(tmp_path / "c")
    key = "/repos/demo"
    project = store.register(
        "project", project_key=key, fact_type="constraint",
        value="所有 Python 改动先写 failing test。", registered_at=_REGISTERED_AT,
    )
    glob = store.register(
        "global", fact_type="constraint", value="说话温柔一点。",
        registered_at=_REGISTERED_AT,
    )

    # 父会话：session/started 带 permission_mode → 一条会话级 authorization fact。
    parent_events = [
        _started("parent-0", permission_mode="workspace-write", auto_approve=False),
    ]
    snap = compute_inheritance(
        parent_events, store=store, project_key=key, session_id="parent-0",
    )

    assert snap.omit_constraints is False
    ids = {row["fact_id"] for row in snap.inherited}
    assert {project.fact_id, glob.fact_id} <= ids
    scopes = {row["fact_id"]: row["scope"] for row in snap.inherited}
    assert scopes[project.fact_id] == "project"
    assert scopes[glob.fact_id] == "global"
    # 会话级 authorization 也继承（scope=session）。
    auth_rows = [row for row in snap.inherited if row["type"] == "authorization"]
    assert len(auth_rows) == 1 and auth_rows[0]["scope"] == "session"


def test_compute_inheritance_excludes_session_facts_and_revoked(tmp_path: Path) -> None:
    store = ScopedConstraintStore(tmp_path / "c")
    revoked = store.register(
        "global", fact_type="constraint", value="已撤销。", registered_at=_REGISTERED_AT,
    )
    store.revoke("global", revoked.fact_id, revoked_at=_REGISTERED_AT)
    active = store.register(
        "global", fact_type="constraint", value="仍生效。", registered_at=_REGISTERED_AT,
    )

    # 会话级非授权事实（user_goal）→ 只进 not_inherited，不注入。
    parent_events = [
        _started("parent-0"),
        SessionEvent(
            type="user/message", session_id="parent-0",
            data={"content": "本会话专属：先给结论。"},
        ),
    ]
    snap = compute_inheritance(
        parent_events, store=store, project_key=None, session_id="parent-0",
    )
    inherited_ids = {row["fact_id"] for row in snap.inherited}
    assert active.fact_id in inherited_ids
    assert revoked.fact_id not in inherited_ids
    # 会话专属事实落在 not_inherited（scope=session），且不在 inherited 里。
    not_inherited_types = {row["type"] for row in snap.not_inherited}
    assert "user_goal" in not_inherited_types
    assert all(row["scope"] == "session" for row in snap.not_inherited)


def test_compute_inheritance_omit_is_clean_start(tmp_path: Path) -> None:
    store = ScopedConstraintStore(tmp_path / "c")
    store.register(
        "global", fact_type="constraint", value="不应继承。", registered_at=_REGISTERED_AT,
    )
    parent_events = [_started("parent-0", permission_mode="workspace-write")]
    snap = compute_inheritance(
        parent_events, store=store, project_key=None, session_id="parent-0",
        omit_constraints=True,
    )
    assert snap.omit_constraints is True
    assert snap.inherited == ()
    # 未继承组仍如实列出（展示层据它渲染 NOT INHERITED）。


def test_snapshot_roundtrip() -> None:
    snap = InheritanceSnapshot(
        omit_constraints=False,
        inherited=({"fact_id": "f1", "type": "constraint", "value": "x", "scope": "global"},),
        not_inherited=({"fact_id": "s1", "type": "user_goal", "value": "y", "scope": "session"},),
    )
    again = InheritanceSnapshot.from_event_data(snap.to_event_data())
    assert again == snap


def test_inherited_protected_facts_source_and_session_id(tmp_path: Path) -> None:
    store = ScopedConstraintStore(tmp_path / "c")
    g = store.register(
        "global", fact_type="constraint", value="v", registered_at=_REGISTERED_AT,
    )
    parent_events = [_started("parent-0", permission_mode="workspace-write")]
    snap = compute_inheritance(
        parent_events, store=store, project_key=None, session_id="parent-0",
    )
    facts = inherited_protected_facts(snap, session_id="child-9")
    by_id = {f.fact_id: f for f in facts}
    assert by_id[g.fact_id].source_event_id == f"scoped:{g.fact_id}"
    assert by_id[g.fact_id].source_seq == 0
    assert all(f.session_id == "child-9" for f in facts)
    auth = next(f for f in facts if f.type == "authorization")
    # 会话级授权保留父会话真实来源 id（审计可回溯）。
    assert auth.source_event_id == parent_events[0].event_id


def test_inherited_protected_facts_bad_data_tolerance() -> None:
    """坏数据容错（审查 Finding-3/5）：空 fact_id 行跳过不注入；会话级条目缺
    来源指针时以 unresolved: 前缀显式标记，不伪装成真实事件 id。"""
    snap = InheritanceSnapshot(
        omit_constraints=False,
        inherited=(
            {"fact_id": "", "type": "constraint", "value": "无 id", "scope": "project"},
            {"fact_id": "p1", "type": "constraint", "value": "v1", "scope": "project"},
            {"fact_id": "s1", "type": "authorization", "value": "v2", "scope": "session"},
        ),
        not_inherited=(),
    )
    facts = inherited_protected_facts(snap, session_id="child-1")
    by_id = {f.fact_id: f for f in facts}
    assert len(facts) == 2 and "" not in by_id
    assert by_id["p1"].source_event_id == "scoped:p1"
    assert by_id["s1"].source_event_id == "unresolved:s1"


def test_compute_inheritance_session_shadow_does_not_hole_project(
    tmp_path: Path,
) -> None:
    """同文覆盖空洞（审查 Correctness Finding-1）：会话级非授权同文行不进覆盖
    候选——否则项目行被覆盖跳过、session 行又不继承，子上下文两头落空。"""
    store = ScopedConstraintStore(tmp_path / "c")
    project = store.register(
        "project", project_key="/repos/demo", fact_type="constraint",
        value="同文X。", registered_at=_REGISTERED_AT,
    )
    parent_events = [
        _started("parent-0"),
        SessionEvent(
            type="user/message", session_id="parent-0",
            data={
                "content": "同文X。",
                "protected_facts": [
                    {"fact_type": "constraint", "value": "同文X。", "scope": "session"},
                ],
            },
        ),
    ]
    snap = compute_inheritance(
        parent_events, store=store, project_key="/repos/demo", session_id="parent-0",
    )
    inherited_ids = {row["fact_id"] for row in snap.inherited}
    assert project.fact_id in inherited_ids
    # 会话级行仍如实进 not_inherited（展示组），但不再吞掉项目行。
    assert ("constraint", "同文X。") in {
        (row["type"], row["value"]) for row in snap.not_inherited
    }


# ── Runtime 注入缝（AC1）──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_injects_inherited_facts_into_context(tmp_path: Path) -> None:

    model = ScriptedModel([AIMessage(content="done")])
    registry = ToolRegistry()
    fact = ProtectedFact(
        fact_id="prj-abc", type="constraint", value="所有 Python 改动先写 failing test。",
        source_event_id="scoped:prj-abc", source_seq=0, status="active",
        session_id="child-1", scope="project",
    )
    runtime = AgentRuntime(
        model, registry, ToolExecutor(registry), inherited_facts=[fact],
    )
    session = make_session(tmp_path)
    result = await runtime.run(session, "hello")
    assert result.status == "completed"

    rendered = "\n".join(
        str(getattr(m, "content", "")) for m in model.snapshots[0].messages
    )
    assert "所有 Python 改动先写 failing test。" in rendered


@pytest.mark.asyncio
async def test_runtime_without_inherited_facts_injects_nothing(tmp_path: Path) -> None:
    model = ScriptedModel([AIMessage(content="done")])
    registry = ToolRegistry()
    runtime = AgentRuntime(model, registry, ToolExecutor(registry))
    session = make_session(tmp_path)
    await runtime.run(session, "hello")
    rendered = "\n".join(
        str(getattr(m, "content", "")) for m in model.snapshots[0].messages
    )
    # 无 inherited_facts ⇒ 不注入任何 scoped 条目（注入体脚手架恒在，判据是条目缺席）。
    assert "scoped:" not in rendered


# ── provider 端到端（AC4/AC5）─────────────────────────────────────────────


class _EchoArgs:
    pass


class _EchoTool:
    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "read"

    @property
    def args_schema(self):
        from pydantic import BaseModel, Field

        class _A(BaseModel):
            text: str = Field(default="x")

        return _A

    async def execute(self, args):
        from agent_harness.tooling import ToolResult

        return ToolResult.success(message="ok")


def _activated(tmp_path: Path, inheritance_provider):
    store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    parent_session_id = "parent-1192"
    workspace_registry.create(parent_session_id, workspace_root=tmp_path / "ws")

    provider_impl = InProcessSubagentProvider()
    tool = DelegateTool(provider_impl)
    child_model = ScriptedModel([AIMessage(content="child 完成")])
    factory = AgentFactory(
        model=child_model, primary_model_name="main-model",
        executor_factory=lambda registry: ToolExecutor(registry),
    )
    source_registry = ToolRegistry()
    source_registry.register(_EchoTool())
    source_registry.register(tool)
    provider_impl.activate(
        factory=factory,
        source_registry=source_registry,
        session_store=store,
        workspace_registry=workspace_registry,
        parent_session_id=parent_session_id,
        constraint_inheritance=inheritance_provider,
    )
    return tool, store, parent_session_id


def _args(target: str, task: str, *, omit: bool = False):
    return type("_A", (), {
        "target": target, "task": task, "constraints": [], "output_schema": None,
        "omit_constraints": omit,
    })()


@pytest.mark.asyncio
async def test_delegate_persists_inheritance_snapshot_event(tmp_path: Path) -> None:
    snap = InheritanceSnapshot(
        omit_constraints=False,
        inherited=({"fact_id": "glb-1", "type": "constraint",
                    "value": "全局：说话温柔一点。", "scope": "global"},),
        not_inherited=(),
    )
    tool, store, _ = _activated(tmp_path, lambda events, omit: snap)

    result = await tool.execute(_args("coding", "跑测试"))
    assert result.ok

    child_ids = [cid for cid in store.list_session_ids() if cid != "parent-1192"]
    assert child_ids, "子会话必须独立落盘"
    events = store.read_events(child_ids[0])
    snaps = [e for e in events if e.type == CONSTRAINT_INHERITANCE_EVENT]
    assert len(snaps) == 1
    assert snaps[0].data == {
        "omit_constraints": False,
        "inherited": [{"fact_id": "glb-1", "type": "constraint",
                       "value": "全局：说话温柔一点。", "scope": "global"}],
        "not_inherited": [],
    }


@pytest.mark.asyncio
async def test_delegate_omit_clean_start_snapshot(tmp_path: Path) -> None:
    seen: list[bool] = []

    def provider(events, omit):
        seen.append(omit)
        return InheritanceSnapshot(True, (), ()) if omit else InheritanceSnapshot(False, (), ())

    tool, store, _ = _activated(tmp_path, provider)
    result = await tool.execute(_args("coding", "跑测试", omit=True))
    assert result.ok
    assert seen == [True]
    child_ids = [cid for cid in store.list_session_ids() if cid != "parent-1192"]
    events = store.read_events(child_ids[0])
    snap = next(e for e in events if e.type == CONSTRAINT_INHERITANCE_EVENT)
    assert snap.data["omit_constraints"] is True
    assert snap.data["inherited"] == []
