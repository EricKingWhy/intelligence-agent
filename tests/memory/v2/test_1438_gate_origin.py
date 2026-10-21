"""#1438 F-02 补实现：origin 打标接线 + `evaluate_gate` 的 origin 分流。

用户裁决（2026-10-21）：补实现，不追认。票面 §5.1 的判据逐条落在这里：

1. 分流矩阵逐格断言：gate 关 × 任一 origin → allow；gate 开 × FOREGROUND → stage；
   gate 开 × BACKGROUND_REVIEW → stage；`evaluate_gate` **任何输入都不产出 blocked**
   （blocked 只由人工 approve/reject 产生，回归既有 AC7 用例覆盖）；
2. `set_origin` 在真实写入入口有 ≥3 处调用点且都有配对 `reset_origin`——前台
   `memory` 工具、后台 review job（`executor._apply` 的写入事务）、垫片
   （`RememberMemoryV2Tool` / `ForgetMemoryV2Tool`）；
3. `_add_soft_check` 的来源守卫不再恒真：后台 origin 下守卫分支被走到。

`APPROVAL_REPLAY` 已按票面 §4.1 的兜底删除（无落点、无消费方），判据见
`test_approval_replay_marker_is_gone_with_rationale` 与 `origin.py` 模块 docstring。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from agent_harness.memory.types import memory_session_var
from agent_harness.memory.v2 import origin
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.jobs import SqliteMemoryV2JobStore
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.memory_tool import MemoryTool, _MemoryArgs
from agent_harness.memory.v2.store import MemorySettingsV2, SqliteMemoryV2Store
from agent_harness.memory.v2.tools import (
    ForgetMemoryV2Tool,
    RememberMemoryV2Tool,
    _ForgetV2Args,
    _RememberV2Args,
)
from agent_harness.memory.v2.types import (
    MemoryKind,
    SemanticCategory,
    SemanticPayload,
)
from agent_harness.memory.v2.write_approval import StagedWriteStore, evaluate_gate
from agent_harness.session import (
    RUN_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)
from tests.memory.v2.test_v2_executor import (
    FakeInvoker,
    _add,
    _adjudication,
    _candidate,
    _formation_candidates,
    _run,
)

#: 票面 §5.1 的分流矩阵（本仓无行内审批通道 ⇒ 两格同值，见 `write_approval` docstring）。
MATRIX = [
    (False, origin.FOREGROUND, "allow"),
    (False, origin.BACKGROUND_REVIEW, "allow"),
    (True, origin.FOREGROUND, "stage"),
    (True, origin.BACKGROUND_REVIEW, "stage"),
]


@pytest.mark.parametrize(("gate_on", "source", "expected"), MATRIX)
def test_gate_routing_matrix(gate_on: bool, source: str, expected: str) -> None:
    decision = evaluate_gate(MemorySettingsV2(write_approval=gate_on), source)
    assert getattr(decision, expected) is True, (gate_on, source, decision)
    assert decision.blocked is False, "blocked 只由人工 approve/reject 产生，门本身绝不产出"


@pytest.mark.parametrize("gate_on", [False, True])
@pytest.mark.parametrize("source", [origin.FOREGROUND, origin.BACKGROUND_REVIEW])
def test_gate_never_blocks_for_any_input(gate_on: bool, source: str) -> None:
    assert evaluate_gate(MemorySettingsV2(write_approval=gate_on), source).blocked is False


def test_gate_defaults_to_the_ambient_origin() -> None:
    """不传 origin 时读 ContextVar——这就是"打标接线"真正生效的地方。"""
    token = origin.set_origin(origin.BACKGROUND_REVIEW)
    try:
        assert evaluate_gate(MemorySettingsV2(write_approval=True)).stage is True
        assert origin.current_origin() == origin.BACKGROUND_REVIEW
    finally:
        origin.reset_origin(token)
    assert origin.current_origin() == origin.FOREGROUND


def test_set_origin_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="unknown memory write origin"):
        origin.set_origin("assistant_tool")  # 上游字面值，不是本仓的取值


def test_gate_rejects_an_unknown_origin_instead_of_silently_staging() -> None:
    """打标写错字必须响亮：静默 stage 会把"来源不明"伪装成"来源合法"。"""
    with pytest.raises(ValueError, match="unknown memory write origin"):
        evaluate_gate(MemorySettingsV2(write_approval=True), "assistant_tool")


def test_approval_replay_marker_is_gone_with_rationale() -> None:
    """§4.1 兜底：重放标没有落点也没有消费方 ⇒ 删除，不留无消费者的标。

    理由记在 `origin.py` 的模块 docstring（代码注记）与本票完工报告。
    """
    assert not hasattr(origin, "APPROVAL_REPLAY")
    assert origin.ORIGINS == {origin.FOREGROUND, origin.BACKGROUND_REVIEW}


async def _write_user_turn(sessions: JsonlSessionStore, session_id: str, text: str) -> None:
    sessions.append_event(session_id, SessionEvent(
        seq=0, type=USER_MESSAGE, session_id=session_id, data={"content": text}))
    sessions.append_event(session_id, SessionEvent(
        seq=1, type=RUN_STARTED, session_id=session_id, run_id="run-1", data={}))


# --------------------------------------------------------------------------------------
# 前台 `memory` 工具：`_gate` 在写入门上打 FOREGROUND
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def foreground_tool(tmp_path):
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    await _write_user_turn(sessions, "session-1", "今天天气不错")
    markdown = MarkdownMemoryStore(tmp_path / "memories")
    markdown.load_from_disk()
    return MemoryTool(markdown, sessions)


@pytest.mark.asyncio
async def test_soft_check_guard_is_reached_for_background_origin(foreground_tool) -> None:
    """`_add_soft_check` 的来源守卫不再恒真（票面 §5.1 第 3 条）。

    同一份"用户消息里找不到依据"的 add：前台 → 软校验拒绝；后台 origin → 守卫
    分支直接放行（后台 review 提炼的内容本来就不逐字来自某条 user message）。
    """
    tool = foreground_tool
    args = _MemoryArgs(action="add", target="memory", content="完全无关的一条内容")
    binding = memory_session_var.set("session-1")
    try:
        denied = await tool.execute(args)
        token = origin.set_origin(origin.BACKGROUND_REVIEW)
        try:
            allowed = await tool.execute(args)
        finally:
            origin.reset_origin(token)
    finally:
        memory_session_var.reset(binding)

    assert denied.ok is False and "软校验" in denied.message
    assert allowed.ok is True, allowed


@pytest.mark.asyncio
async def test_foreground_gate_tags_origin_and_restores_it(tmp_path) -> None:
    """`_gate` 在自己这一段打 FOREGROUND，结束后恢复环境值（配对 reset）。"""
    seen: list[str] = []

    class _Service:
        async def get_settings(self, trusted):
            seen.append(origin.current_origin())
            return MemorySettingsV2(write_approval=True)

    markdown = MarkdownMemoryStore(tmp_path / "memories")
    markdown.load_from_disk()
    staged = StagedWriteStore(tmp_path / "memory-v2.db", markdown)
    await staged.initialize()
    tool = MemoryTool(markdown, service=_Service(), staged=staged)
    payload = {"target": "memory", "action": "add", "content": "一条事实", "old_text": None}

    token = origin.set_origin(origin.BACKGROUND_REVIEW)
    try:
        result = await tool._gate(payload)
        assert origin.current_origin() == origin.BACKGROUND_REVIEW, "退出后必须还原环境值"
    finally:
        origin.reset_origin(token)

    assert seen == [origin.FOREGROUND]
    assert result is not None and result.data["staged"] is True


# --------------------------------------------------------------------------------------
# 后台 review job：`executor._apply` 的写入事务打 BACKGROUND_REVIEW
# --------------------------------------------------------------------------------------


class _OriginRecordingWriter:
    """包住真实 writer，记下写入时看到的 origin（其余行为原样委托）。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.origins: list[str] = []

    async def create_in(self, connection, draft, trusted):
        self.origins.append(origin.current_origin())
        return await self.inner.create_in(connection, draft, trusted)

    async def update_in(self, connection, previous_id, draft, trusted):
        self.origins.append(origin.current_origin())
        return await self.inner.update_in(connection, previous_id, draft, trusted)

    async def invalidate_in(self, connection, memory_id, trusted):
        self.origins.append(origin.current_origin())
        return await self.inner.invalidate_in(connection, memory_id, trusted)

    async def mirror_committed(self, records, *, invalidated=()):
        return await self.inner.mirror_committed(records, invalidated=invalidated)


@pytest_asyncio.fixture()
async def executor_env(tmp_path):
    path = tmp_path / "memory-v2.db"
    store = SqliteMemoryV2Store(path)
    await store.initialize()
    jobs = SqliteMemoryV2JobStore(path)
    await jobs.initialize()
    index = InMemoryMemoryV2Index()
    return SimpleNamespace(
        store=store, jobs=jobs, index=index, service=MemoryV2Service(store, index))


@pytest.mark.asyncio
async def test_background_apply_writes_under_background_review_origin(executor_env) -> None:
    recorder = _OriginRecordingWriter(executor_env.service)
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    await _run(executor_env, invoker, writer=recorder)

    assert recorder.origins == [origin.BACKGROUND_REVIEW], (
        "后台 formation 的落盘必须在 BACKGROUND_REVIEW 下进行，且退出后不得泄漏")
    assert origin.current_origin() == origin.FOREGROUND


# --------------------------------------------------------------------------------------
# 垫片：显式 remember / forget 打 FOREGROUND（即使环境里是后台 origin）
# --------------------------------------------------------------------------------------


class _OriginRecordingMemoryService:
    def __init__(self) -> None:
        self.origins: list[str] = []

    async def create(self, draft, trusted):
        self.origins.append(origin.current_origin())
        return SimpleNamespace(id="mem-1", kind=draft.kind, version=1)

    async def delete(self, memory_id, trusted):
        self.origins.append(origin.current_origin())
        return SimpleNamespace(deleted=True, memories=())


def _semantic_payload(fact: str) -> SemanticPayload:
    return SemanticPayload(subject=fact, fact=fact, category=SemanticCategory.PREFERENCE)


@pytest.mark.asyncio
async def test_shim_remember_and_forget_tag_foreground(tmp_path) -> None:
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    await _write_user_turn(sessions, "remember-session", "Please remember I prefer tea")
    await _write_user_turn(sessions, "forget-session", "Forget the old coffee note")
    service = _OriginRecordingMemoryService()
    remember = RememberMemoryV2Tool(service, sessions)
    forget = ForgetMemoryV2Tool(service, sessions)
    remember_args = _RememberV2Args(
        content="I prefer tea", kind=MemoryKind.SEMANTIC,
        payload=_semantic_payload("I prefer tea"))
    forget_args = _ForgetV2Args(memory_id="the old coffee note")

    token = origin.set_origin(origin.BACKGROUND_REVIEW)
    try:
        binding = memory_session_var.set("remember-session")
        try:
            remembered = await remember.execute(remember_args)
        finally:
            memory_session_var.reset(binding)
        binding = memory_session_var.set("forget-session")
        try:
            forgotten = await forget.execute(forget_args)
        finally:
            memory_session_var.reset(binding)
        assert origin.current_origin() == origin.BACKGROUND_REVIEW, "退出后必须还原环境值"
    finally:
        origin.reset_origin(token)

    assert remembered.ok is True, remembered
    assert forgotten.ok is True, forgotten
    assert service.origins == [origin.FOREGROUND, origin.FOREGROUND]
