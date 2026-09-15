"""#202 / ADR-0031：retrieve_memory / remember_this 的契约测试。

本文件钉 ADR-0031 §7 的 T1–T12。断言打在**真实执行路径**（ToolExecutor）与
真实 provider 写入路径（consolidate）上；排序同源（T10）直接断言两处调用
同一个 `rank_entries` 纯函数。
"""

import pytest
from pydantic import ValidationError

from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.fake_capability import FakeMemoryCapability
from agent_harness.memory.rank import rank_entries
from agent_harness.memory.tools import (
    ForgetMemoryTool,
    RememberThisTool,
    RetrieveMemoryTool,
    _RememberThisArgs,
    _RetrieveMemoryArgs,
)
from agent_harness.memory.types import MemoryScope
from agent_harness.session import memory_injected_ids_var
from agent_harness.tooling import (
    ErrorCode,
    PermissionPolicy,
    ToolExecutor,
    ToolPermission,
    ToolRegistry,
    ToolSideEffect,
)

ALICE = IdentityContext("acme", "alice", ["user"])


async def _seed(capability: FakeMemoryCapability, *contents: str) -> list[str]:
    ids = []
    for content in contents:
        token = set_identity_context(ALICE)
        try:
            ids.append(await capability.store(MemoryScope.USER, content, {}))
        finally:
            identity_context_var.reset(token)
    return ids


async def _run(tool, args):
    """工具执行包在身份上下文里（生产由 run 任务绑定；测试显式绑定）。"""
    token = set_identity_context(ALICE)
    try:
        return await tool.execute(args)
    finally:
        identity_context_var.reset(token)


def _tc(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"id": call_id, "name": name, "args": args}


def _executor(*tools, **kwargs) -> ToolExecutor:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolExecutor(registry, **kwargs)


# ── T1：工具注册随 capability（接线层） ──────────────────────────────


def test_wiring_registers_all_three_memory_tools():
    """memory capability 接线 ⇒ 三个记忆工具都经 contributes_tools 注册。"""
    from agent_harness.capability.wiring import _MemoryCapabilityProvider

    provider = _MemoryCapabilityProvider(FakeMemoryCapability())
    names = {type(t).name.fget(t) for t in provider.contributes_tools()}
    assert {"retrieve_memory", "remember_this", "forget_memory"} <= names


# ── T2：去重 + injected 标记 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_injected_flag_and_dedup():
    capability = FakeMemoryCapability()
    ids = await _seed(capability, "偏好 A", "偏好 B", "偏好 C")
    a, b, c = ids

    # provider 已把 A、B 注入本 run 上下文（注册表含其 id）。
    memory_injected_ids_var.set(frozenset({a, b}))
    try:
        tool = RetrieveMemoryTool(capability)
        result = await _run(tool, _RetrieveMemoryArgs(query="偏好"))
        memories = result.data["memories"]
        by_id = {m["id"]: m for m in memories}
        assert by_id[a]["injected"] is True
        assert by_id[b]["injected"] is True
        assert by_id[c]["injected"] is False
        assert result.data["already_injected_count"] == 2
        # 同一 id 只出现一次（去重）。
        assert len([m for m in memories if m["id"] == a]) == 1
    finally:
        memory_injected_ids_var.set(frozenset())


@pytest.mark.asyncio
async def test_duplicate_ids_collapse_to_one():
    """T2 去重：检索返回重复 id ⇒ 只出现一次（ADR-0031 §3.1）。

    FakeMemoryCapability 从不返回重复 id，用专用替身真正覆盖这条。"""
    from datetime import UTC, datetime

    from agent_harness.memory.types import MemoryEntry

    class _Duplicate(FakeMemoryCapability):
        async def search(self, scope, query, limit):
            entry = MemoryEntry(id="dup-1", content="重复条目", metadata={},
                                created_at=datetime.now(UTC).isoformat(), scope=scope)
            return [entry.model_copy(), entry.model_copy()]

    result = await _run(RetrieveMemoryTool(_Duplicate()), _RetrieveMemoryArgs(query="重复"))
    assert [m["id"] for m in result.data["memories"]] == ["dup-1"]


# ── T3：无分数（键集合恰好为四个） ───────────────────────────────────


@pytest.mark.asyncio
async def test_memory_keys_are_exactly_the_contract_set():
    capability = FakeMemoryCapability()
    await _seed(capability, "稳定事实")
    result = await _run(RetrieveMemoryTool(capability), _RetrieveMemoryArgs(query="稳定"))
    assert result.data["memories"], "search 应命中"
    assert set(result.data["memories"][0]) == {"id", "content", "injected", "created_at"}


# ── T4：失败 ≠ 没有记忆 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_failure_is_transient_not_empty_truth():
    class _Broken(FakeMemoryCapability):
        async def search(self, scope, query, limit):
            raise RuntimeError("milvus down")

    result = await _run(RetrieveMemoryTool(_Broken()), _RetrieveMemoryArgs(query="x"))
    assert result.ok is False
    assert result.error_code is ErrorCode.TRANSIENT_ERROR
    assert "不代表没有相关记忆" in result.message


# ── T5：空结果 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_search_is_a_factual_success():
    result = await _run(RetrieveMemoryTool(FakeMemoryCapability()), _RetrieveMemoryArgs(query="不存在的东西"))
    assert result.ok is True
    assert result.data == {"memories": [], "already_injected_count": 0}
    assert "没有检索到" in result.message


# ── T6：写入走 consolidate ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_remember_this_goes_through_consolidate():
    capability = FakeMemoryCapability()
    calls: list[str] = []
    original = capability.consolidate

    async def spy(*a, **kw):
        calls.append("consolidate")
        return await original(*a, **kw)

    capability.consolidate = spy  # type: ignore[method-assign]
    result = await _run(RememberThisTool(capability), _RememberThisArgs(content="用户偏好暗色主题"))

    assert calls == ["consolidate"]
    assert result.ok is True
    assert result.data["degraded"] is False
    assert result.data["memory_id"]
    # 真的落库了。
    token = set_identity_context(ALICE)
    try:
        entries = await capability.list_entries(MemoryScope.USER, 10)
    finally:
        identity_context_var.reset(token)
    assert any(e.content == "用户偏好暗色主题" for e in entries)


# ── T7：写入异常不穿透 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consolidate_failure_becomes_tool_failure():
    class _Broken(FakeMemoryCapability):
        async def consolidate(self, *a, **kw):
            raise RuntimeError("store down")

    result = await _run(RememberThisTool(_Broken()), _RememberThisArgs(content="x"))
    assert result.ok is False
    assert result.error_code is ErrorCode.TRANSIENT_ERROR


# ── T8：参数安全 ─────────────────────────────────────────────────────


def test_remember_this_args_reject_metadata_and_oversize():
    assert set(_RememberThisArgs.model_fields) == {"content"}
    with pytest.raises(ValidationError):
        _RememberThisArgs(content="x", metadata={"k": 1})
    with pytest.raises(ValidationError):
        _RememberThisArgs(content="x" * 2001)


def test_retrieve_args_reject_extra_and_empty_query():
    assert set(_RetrieveMemoryArgs.model_fields) == {"query", "limit"}
    with pytest.raises(ValidationError):
        _RetrieveMemoryArgs(query="x", scope="tenant")
    with pytest.raises(ValidationError):
        _RetrieveMemoryArgs(query="  ")
    with pytest.raises(ValidationError):
        _RetrieveMemoryArgs(query="x", limit=21)


# ── T9：注册表生命周期 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_run_context_means_all_false():
    """无 run 上下文直接调工具 ⇒ 可用且 injected 全 false，不抛异常。"""
    memory_injected_ids_var.set(frozenset())
    capability = FakeMemoryCapability()
    await _seed(capability, "事实")
    result = await _run(RetrieveMemoryTool(capability), _RetrieveMemoryArgs(query="事实"))
    assert result.ok is True
    assert all(m["injected"] is False for m in result.data["memories"])


# ── T10：排序口径同源 ────────────────────────────────────────────────


def test_provider_and_tool_share_rank_entries():
    """provider 的闭包 rank 与模块级 rank_entries 对同一批 entries 输出一致。"""
    from datetime import UTC, datetime

    from agent_harness.memory.types import MemoryEntry

    entries = [
        MemoryEntry(id=str(i), content=f"c{i}", score=s,
                    created_at=datetime(2026, 1, 1 + i, tzinfo=UTC).isoformat(),
                    scope=MemoryScope.USER)
        for i, s in enumerate([0.9, 0.5, 0.7])
    ]
    now = datetime(2026, 9, 15, tzinfo=UTC)
    ranked = rank_entries(entries, now=now)

    # 复刻 provider 内原闭包的公式，核对顺序一致（同源回归）。
    def expected_key(entry):
        created = datetime.fromisoformat(entry.created_at)
        created = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
        age_days = max(0, (now - created).total_seconds() / 86400)
        importance = max(0, min(1, float(entry.metadata.get("importance", 0.5))))
        return 0.7 * (entry.score or 0) + 0.2 * importance + 0.1 / (1 + age_days)

    assert [e.id for e in ranked] == [e.id for e in sorted(entries, key=expected_key, reverse=True)]


# ── T9 补：runtime set/reset 生命周期 ────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_resets_injected_registry_after_run(tmp_path):
    """T9 前半：runtime run 开始设空集合、provider 注入写入、收尾 reset。

    驱动真实 run 生命周期（AgentRuntime + MemoryContextProvider），断言 run 结束
    后注册表回到空集合（下一 run injected 全 false）。"""
    from langchain_core.messages import AIMessage

    from agent_harness.agent import AgentRuntime
    from agent_harness.memory.context_provider import MemoryContextProvider
    from tests.conftest import make_session
    from tests.scripted_model import ScriptedModel

    capability = FakeMemoryCapability()
    await _seed(capability, "runtime 生命周期记忆")

    class _Static:
        async def search(self, scope, query, limit):
            token = set_identity_context(ALICE)
            try:
                return await capability.search(scope, query, limit)
            finally:
                identity_context_var.reset(token)

    provider = MemoryContextProvider(_Static(), timeout_seconds=5.0)
    registry = ToolRegistry()
    runtime = AgentRuntime(ScriptedModel([AIMessage(content="完成")]), registry,
                           ToolExecutor(registry), context_providers=[provider])
    session = make_session(tmp_path)

    assert memory_injected_ids_var.get() == frozenset()  # run 前：空
    result = await runtime.run(session, "runtime 生命周期记忆")
    assert result.final_text == "完成"
    # run 内 provider 真的注入了（注册表含注入 id）；run 收尾后 reset 回空集合。
    assert memory_injected_ids_var.get() == frozenset()


# ── T11：tool_scope（同时防 #198 回归） ──────────────────────────────


def test_tool_scope_covers_new_memory_tools():
    from agent_harness.agent.profiles import BUILTIN_PROFILES

    research = BUILTIN_PROFILES["research_review"].tool_scope
    assert "retrieve_memory" in research
    assert "remember_this" not in research
    assert "forget_memory" not in research

    main = BUILTIN_PROFILES["main"].tool_scope
    assert {"retrieve_memory", "remember_this", "forget_memory"} <= main

    coding = BUILTIN_PROFILES["coding"].tool_scope
    assert "retrieve_memory" in coding
    assert "remember_this" in coding
    assert "forget_memory" in coding


# ── T12：描述区分度（防退化） ────────────────────────────────────────


def test_descriptions_are_distinct_and_accurate():
    descriptions = {
        "retrieve_memory": RetrieveMemoryTool.description.fget(RetrieveMemoryTool(FakeMemoryCapability())),
        "remember_this": RememberThisTool.description.fget(RememberThisTool(FakeMemoryCapability())),
        "forget_memory": ForgetMemoryTool.description.fget(ForgetMemoryTool(FakeMemoryCapability())),
    }
    values = list(descriptions.values())
    assert len(set(values)) == 3
    assert any(k in descriptions["retrieve_memory"] for k in ("检索",))
    assert any(k in descriptions["remember_this"] for k in ("记住", "写入"))
    assert any(k in descriptions["forget_memory"] for k in ("删除", "遗忘"))
    # forget_memory 描述必须指向真实工具名（#202 派生缺陷闭环）。
    assert "retrieve_memory" in descriptions["forget_memory"]


# ── 权限声明（ADR-0031 §3） ─────────────────────────────────────────


def test_permission_declarations():
    retrieve = RetrieveMemoryTool(FakeMemoryCapability())
    assert retrieve.permission is ToolPermission.READ_ONLY
    assert retrieve.side_effect is ToolSideEffect.READ_ONLY

    remember = RememberThisTool(FakeMemoryCapability())
    assert remember.permission is ToolPermission.WORKSPACE_WRITE
    assert remember.side_effect is ToolSideEffect.MUTATING


@pytest.mark.asyncio
async def test_remember_this_denied_under_readonly_policy():
    """只读策略下 remember_this 被执行器拒绝（不变量 #11 的期望行为）。"""
    capability = FakeMemoryCapability()
    executor = _executor(RememberThisTool(capability), policy=PermissionPolicy.READ_ONLY)
    result = (await executor.execute(_tc("remember_this", {"content": "x"}))).result
    assert result.ok is False
    assert result.error_code is ErrorCode.PERMISSION_DENIED
