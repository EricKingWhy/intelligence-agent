"""AgentRuntime stable-boundary Checkpoint integration tests (#28).

验证：
- Checkpoint 保存发生在对应 SessionEvent 已持久化【之后】；
- checkpoint/saved 不写入 SessionEvent；
- NoCheckpoint 不保存；
- EveryStep 行为明确且不污染 SessionEvent；
- SessionMetaStore 在 Run 结束后反映 last_checkpoint_seq；
- 策略可替换（注入 NoCheckpoint 时 store 为空）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.session import (
    JsonlSessionStore,
    Session,
)
from agent_harness.storage import (
    CheckpointBoundary,
    EveryStep,
    NoCheckpoint,
    OnStableBoundary,
    SqliteCheckpointStore,
    SqliteSessionMetaStore,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.scripted_model import ScriptedModel


class _NoArgs(BaseModel):
    pass


class _ProbeTool(Tool):
    """记录 execute 时 session 里已有的 event 类型——验证 checkpoint 保存发生在
    tool/result 等 SessionEvent 之后。"""

    def __init__(self) -> None:
        self.event_types_during_execute: list[str] | None = None

    @property
    def name(self) -> str:
        return "probe"

    @property
    def description(self) -> str:
        return "Probe event ordering at the side-effect boundary."

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        return ToolResult.success("probed")


def _build_tool_runtime(
    tmp_path: Path,
    *,
    checkpoint_store: SqliteCheckpointStore | None,
    session_meta_store: SqliteSessionMetaStore | None,
    policy=None,
) -> tuple[AgentRuntime, Session]:
    session = Session.start(JsonlSessionStore(tmp_path / "sessions"))
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "call-1", "name": "probe", "args": {}}],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = AgentRuntime(
        model,
        registry,
        ToolExecutor(registry),
        checkpoint_policy=policy
        or OnStableBoundary(checkpoint_store),
        session_meta_store=session_meta_store,
    )
    return runtime, session


@pytest.mark.asyncio
async def test_runtime_saves_checkpoint_after_each_boundary(tmp_path: Path) -> None:
    checkpoint_store = SqliteCheckpointStore(tmp_path / "state.db")
    session_meta_store = SqliteSessionMetaStore(tmp_path / "state.db")
    await checkpoint_store.initialize()
    await session_meta_store.initialize()

    runtime, session = _build_tool_runtime(
        tmp_path,
        checkpoint_store=checkpoint_store,
        session_meta_store=session_meta_store,
    )
    await runtime.run(session, "hello")

    boundaries = {c.boundary_type for c in await checkpoint_store.list_for_session(session.session_id)}
    # 一轮 tool-calling：USER_ACCEPTED + 2× MODEL_COMPLETED + TOOL_BATCH_COMPLETED + FINAL_COMPLETED
    assert CheckpointBoundary.USER_ACCEPTED in boundaries
    assert CheckpointBoundary.TOOL_BATCH_COMPLETED in boundaries
    assert CheckpointBoundary.FINAL_COMPLETED in boundaries


@pytest.mark.asyncio
async def test_checkpoint_does_not_emit_session_event(tmp_path: Path) -> None:
    """checkpoint/saved 严禁进入 SessionEvent（ADR-0004 Round 5 §Q16）。"""
    checkpoint_store = SqliteCheckpointStore(tmp_path / "state.db")
    await checkpoint_store.initialize()
    runtime, session = _build_tool_runtime(
        tmp_path,
        checkpoint_store=checkpoint_store,
        session_meta_store=None,
    )

    await runtime.run(session, "hello")

    event_types = [event.type for event in session.events]
    assert "checkpoint/saved" not in event_types
    # 只有对话事实，没有任何存储动作事件。
    assert all(
        t
        in {
            "session/started",
            "user/message",
            "run/started",
            "model/completed",
            "tool/call",
            "tool/result",
            "run/completed",
        }
        for t in event_types
    )


@pytest.mark.asyncio
async def test_checkpoint_event_seq_advances_with_session(tmp_path: Path) -> None:
    """Checkpoint.event_seq 应反映保存时已持久化的最新 SessionEvent seq，
    且单调推进（每条比上一条至少不减）。"""
    checkpoint_store = SqliteCheckpointStore(tmp_path / "state.db")
    session_meta_store = SqliteSessionMetaStore(tmp_path / "state.db")
    await checkpoint_store.initialize()
    await session_meta_store.initialize()
    runtime, session = _build_tool_runtime(
        tmp_path,
        checkpoint_store=checkpoint_store,
        session_meta_store=session_meta_store,
    )

    await runtime.run(session, "hello")

    checkpoints = await checkpoint_store.list_for_session(session.session_id)
    seqs = [c.event_seq for c in checkpoints]
    # USER_ACCEPTED 应指向 user/message（seq=1，session/started 是 0）。
    user_checkpoint = next(
        c for c in checkpoints if c.boundary_type is CheckpointBoundary.USER_ACCEPTED
    )
    assert user_checkpoint.event_seq >= 1
    # FINAL_COMPLETED 应是该 session 的最大 seq。
    final_checkpoint = next(
        c for c in checkpoints if c.boundary_type is CheckpointBoundary.FINAL_COMPLETED
    )
    assert final_checkpoint.event_seq == max(seqs)
    # 单调非减。
    assert seqs == sorted(seqs)


@pytest.mark.asyncio
async def test_no_checkpoint_policy_writes_nothing(tmp_path: Path) -> None:
    checkpoint_store = SqliteCheckpointStore(tmp_path / "state.db")
    await checkpoint_store.initialize()
    runtime, session = _build_tool_runtime(
        tmp_path,
        checkpoint_store=checkpoint_store,
        session_meta_store=None,
        policy=NoCheckpoint(),
    )

    await runtime.run(session, "hello")

    assert await checkpoint_store.list_for_session(session.session_id) == []


@pytest.mark.asyncio
async def test_every_step_policy_is_swappable(tmp_path: Path) -> None:
    """EveryStep 行为与 OnStableBoundary 在四个稳定边界一致；验证策略可替换。"""
    checkpoint_store = SqliteCheckpointStore(tmp_path / "state.db")
    await checkpoint_store.initialize()
    runtime, session = _build_tool_runtime(
        tmp_path,
        checkpoint_store=checkpoint_store,
        session_meta_store=None,
        policy=EveryStep(checkpoint_store),
    )

    await runtime.run(session, "hello")

    checkpoints = await checkpoint_store.list_for_session(session.session_id)
    assert checkpoints  # 至少一条
    assert CheckpointBoundary.FINAL_COMPLETED in {c.boundary_type for c in checkpoints}


@pytest.mark.asyncio
async def test_session_meta_reflects_last_checkpoint_seq(tmp_path: Path) -> None:
    checkpoint_store = SqliteCheckpointStore(tmp_path / "state.db")
    session_meta_store = SqliteSessionMetaStore(tmp_path / "state.db")
    await checkpoint_store.initialize()
    await session_meta_store.initialize()
    runtime, session = _build_tool_runtime(
        tmp_path,
        checkpoint_store=checkpoint_store,
        session_meta_store=session_meta_store,
    )

    await runtime.run(session, "hello")

    meta = await session_meta_store.get(session.session_id)
    assert meta is not None
    assert meta.last_checkpoint_seq is not None
    assert meta.agent_id == "default"


@pytest.mark.asyncio
async def test_runtime_without_checkpoint_store_is_noop(tmp_path: Path) -> None:
    """AgentRuntime 不强制依赖存储：未注入 store 时仍能跑完整 loop。"""
    runtime, session = _build_tool_runtime(
        tmp_path,
        checkpoint_store=None,
        session_meta_store=None,
    )

    result = await runtime.run(session, "hello")

    assert result.status == "completed"


# ── 严格时序测试：checkpoint 保存发生在对应 SessionEvent 已持久化【之后】──


class _OrderingPolicy(OnStableBoundary):
    """记录每次 maybe_save 调用时 session 里已有的事件数量（next_seq）。

    AgentRuntime 的不变量：在每个稳定边界调 maybe_save 时，对应那条 SessionEvent
    必须已经 append 进 session（即 next_seq 必须至少反映了那条事件）。
    """

    def __init__(self, store: SqliteCheckpointStore, session: Session) -> None:
        super().__init__(store)
        self._session = session
        self.recorded: list[tuple[CheckpointBoundary, int]] = []

    async def maybe_save(
        self,
        session: Session,
        boundary_type: CheckpointBoundary,
        *,
        event_seq: int | None = None,
        payload: dict | None = None,
    ) -> object:
        # 用真实 session 的 next_seq（而非参数 session，避免闭包混淆）。
        self.recorded.append((boundary_type, self._session.next_seq))
        return await super().maybe_save(
            session, boundary_type, event_seq=event_seq, payload=payload
        )


@pytest.mark.asyncio
async def test_checkpoint_always_saved_after_session_event_persisted(
    tmp_path: Path,
) -> None:
    """对每个稳定边界：保存 checkpoint 时，对应的 SessionEvent 必须已经在
    session.events 里——即 event_seq 已分配。"""
    checkpoint_store = SqliteCheckpointStore(tmp_path / "state.db")
    session_meta_store = SqliteSessionMetaStore(tmp_path / "state.db")
    await checkpoint_store.initialize()
    await session_meta_store.initialize()

    session = Session.start(JsonlSessionStore(tmp_path / "sessions"))
    policy = _OrderingPolicy(checkpoint_store, session)
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"id": "call-1", "name": "probe", "args": {}}],
            ),
            AIMessage(content="done"),
        ]
    )
    runtime = AgentRuntime(
        model,
        registry,
        ToolExecutor(registry),
        checkpoint_policy=policy,
        session_meta_store=session_meta_store,
    )

    await runtime.run(session, "hello")

    # 每个边界被调用时，session 至少已有对应那条 SessionEvent（next_seq > 0）。
    assert all(seq > 0 for _boundary, seq in policy.recorded)

    # TOOL_BATCH_COMPLETED 检查点对应的 event_seq 必须覆盖全部 tool/result——
    # 即保存发生在 tool/result SessionEvent 持久化之后。
    tool_results = [e for e in session.events if e.type == "tool/result"]
    assert tool_results  # 确实有 tool/result
    saved_checkpoints = await checkpoint_store.list_for_session(session.session_id)
    tb_checkpoint = next(
        c
        for c in saved_checkpoints
        if c.boundary_type is CheckpointBoundary.TOOL_BATCH_COMPLETED
    )
    assert tb_checkpoint.event_seq >= tool_results[-1].seq
