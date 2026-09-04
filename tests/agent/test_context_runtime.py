"""Runtime 消费 ContextBuilder 并镜像压缩事实。"""

import asyncio
import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from agent_harness.agent import AgentRuntime
from agent_harness.context.builder import ContextBuilder
from agent_harness.sandbox.base import ExecResult, Sandbox
from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE
from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.tooling import (
    PermissionPolicy,
    ToolExecutor,
    ToolRegistry,
    ToolSideEffect,
)
from agent_harness.tooling.overflow import ArtifactOverflowHandler
from agent_harness.tools import BashTool
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_runtime_stops_before_model_request_when_context_exceeds_guard(tmp_path, stream):
    model = ScriptedModel([])
    registry = ToolRegistry()
    runtime = AgentRuntime(model, registry, ToolExecutor(registry),
                           context_builder=ContextBuilder(model, max_context_tokens=100))
    session = make_session(tmp_path)
    if stream:
        events = [event async for event in runtime.run_stream(session, "huge " * 200)]
        assert events[-1].type == "run/failed"
        assert events[-1].data["reason"] == "context_window_exceeded"
    else:
        result = await runtime.run(session, "huge " * 200)
        assert result.status == "context_window_exceeded"
        assert result.steps == 0 and not result.completed
    assert model.snapshots == []
    assert session.events[-1].type == "run/failed"


@pytest.mark.asyncio
async def test_compaction_event_stream_matches_persistence_and_model_sees_summary(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "old " * 6000})
    session.append(MODEL_COMPLETED, {"content": "done"})
    before = session.events
    summary = {key: [] for key in ("facts", "decisions", "constraints", "failed_attempts",
                                   "unresolved", "artifact_refs", "citations", "tool_outcomes")}
    model = ScriptedModel([AIMessage(content=json.dumps(summary)), AIMessage(content="answer")])
    registry = ToolRegistry()
    runtime = AgentRuntime(model, registry, ToolExecutor(registry),
                           context_builder=ContextBuilder(model, max_context_tokens=8000))
    emitted = [e async for e in runtime.run_stream(session, "current")]
    persisted = session.events[len(before):]
    assert [(e.type, e.seq, e.data) for e in emitted if e.is_durable] == [
        (e.type, e.seq, e.data) for e in persisted
    ]
    assert any(e.type == "context/compacted" for e in emitted)
    assert isinstance(model.snapshots[-1].messages[0], SystemMessage)
    assert session.events[:len(before)] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_partial_batch_failure_still_emits_committed_artifact(tmp_path, parallel):
    class Store(FakeArtifactStore):
        async def save(self, *args, **kwargs):
            if kwargs["tool_call_id"] == "second":
                raise ConnectionError("storage offline")
            if parallel:
                await asyncio.sleep(0.01)
            return await super().save(*args, **kwargs)

    class TestTool(BashTool):
        @property
        def side_effect(self):
            return ToolSideEffect.READ_ONLY if parallel else ToolSideEffect.MUTATING

    session = make_session(tmp_path)
    before = len(session.events)
    sandbox = Mock(spec=Sandbox)
    sandbox.exec.return_value = ExecResult(exit_code=0, stdout="long " * 1000, stderr="")
    registry = ToolRegistry()
    registry.register(TestTool(sandbox))
    model = ScriptedModel([AIMessage(content="", tool_calls=[
        {"id": name, "name": "bash", "args": {"command": "test"}}
        for name in ("first", "second")
    ])])
    runtime = AgentRuntime(model, registry, ToolExecutor(
        registry, policy=PermissionPolicy.DANGER_FULL_ACCESS,
        overflow_handler=ArtifactOverflowHandler(Store()),
    ))
    emitted = []
    with pytest.raises(ConnectionError):
        async for event in runtime.run_stream(session, "run"):
            emitted.append(event)
    if parallel:
        await asyncio.sleep(0.02)
    assert [(e.type, e.seq) for e in emitted if e.is_durable] == [
        (e.type, e.seq) for e in session.events[before:]
    ]
    assert emitted[-1].type == "artifact/created"
    assert sandbox.exec.call_count == 2
