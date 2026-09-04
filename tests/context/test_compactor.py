"""ContextCompactor/Builder 的压缩与恢复不变量。"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import ContextCompactor, ContextWindowExceededError
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

SUMMARY = {
    "facts": ["read completed"], "decisions": [], "constraints": ["keep Chinese"],
    "failed_attempts": [], "unresolved": [], "artifact_refs": ["abc123"],
    "citations": [], "tool_outcomes": ["call-1 succeeded"],
}


@pytest.mark.asyncio
async def test_builder_compacts_old_turn_and_preserves_persistent_history(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "old " * 6000})
    session.append(MODEL_COMPLETED, {"content": "finished"})
    session.append(USER_MESSAGE, {"content": "current request"})
    before = session.events
    model = ScriptedModel([AIMessage(content=json.dumps(SUMMARY))])
    messages = await ContextBuilder(model, max_context_tokens=8000).build(session)
    assert isinstance(messages[0], SystemMessage)
    assert json.loads(messages[0].content) == SUMMARY
    assert messages[1:] == [HumanMessage(content="current request")]
    assert estimate_message_tokens(messages) < 5600
    assert session.events[:-1] == before
    event = session.events[-1]
    assert event.type == "context/compacted"
    assert event.data == {"compacted_turn_count": 1, "summary_message_count": 1,
                          "token_estimate": estimate_message_tokens(messages),
                          "fallback_used": False}
    assert len(model.snapshots) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout", "format", "tool_call"])
async def test_summary_failure_falls_back_with_atomic_tool_block(failure):
    class Model:
        async def ainvoke(self, messages):
            if failure == "error":
                raise ConnectionError("offline")
            if failure == "timeout":
                await asyncio.Event().wait()
            if failure == "tool_call":
                return AIMessage(content=json.dumps(SUMMARY), tool_calls=[
                    {"id": "unwanted", "name": "bash", "args": {}},
                ])
            return AIMessage(content="not a summary")

    messages = [
        HumanMessage(content="old " * 1000),
        AIMessage(content="reason", tool_calls=[{"id": "c1", "name": "read", "args": {}}]),
        ToolMessage(content="result " * 1000, tool_call_id="c1"),
        HumanMessage(content="current"),
        AIMessage(content="", tool_calls=[{"id": "c2", "name": "read", "args": {}}]),
        ToolMessage(content="current result", tool_call_id="c2"),
    ]
    result = await ContextCompactor(Model(), max_context_tokens=8000,
                                    summary_timeout_seconds=0.01).compact(
        messages, estimate_message_tokens(messages),
    )
    assert result.fallback_used
    assert result.messages[1:] == messages[3:]
    rows = json.loads(result.messages[0].content)["mechanical_extract"]
    assert rows[0]["content"] == messages[0].content[:200]
    assert rows[1]["tool_calls"] == messages[1].tool_calls
    assert rows[2]["tool_call_id"] == "c1"
    assert rows[2]["content"] == messages[2].content[:100]


@pytest.mark.asyncio
async def test_hard_guard_rejects_when_recent_turn_cannot_fit(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "old"})
    session.append(MODEL_COMPLETED, {"content": "done"})
    session.append(USER_MESSAGE, {"content": "current " * 2000})
    before = session.events
    with pytest.raises(ContextWindowExceededError):
        await ContextBuilder(ScriptedModel([]), max_context_tokens=1000).build(session)
    assert session.events == before


@pytest.mark.asyncio
async def test_oversized_summary_falls_back_and_system_constraints_survive():
    huge = {**SUMMARY, "facts": ["huge " * 4000]}
    model = ScriptedModel([AIMessage(content=json.dumps(huge))])
    messages = [SystemMessage(content="Never modify protected files"),
                HumanMessage(content="old"), AIMessage(content="done"),
                HumanMessage(content="current")]
    result = await ContextCompactor(model, max_context_tokens=1000).compact(
        messages, estimate_message_tokens(messages),
    )
    assert result.fallback_used
    assert result.messages[0] == messages[0]
    assert result.messages[-1] == messages[-1]
    assert result.token_estimate <= 850


@pytest.mark.asyncio
async def test_summary_request_over_budget_skips_model_and_records_fallback(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "large " * 10000})
    session.append(MODEL_COMPLETED, {"content": "done"})
    session.append(USER_MESSAGE, {"content": "current"})
    model = ScriptedModel([])
    messages = await ContextBuilder(model, max_context_tokens=1000).build(session)
    assert model.snapshots == []
    assert session.events[-1].data["fallback_used"] is True
    assert estimate_message_tokens(messages) <= 850


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_as_summary_failure():
    class CancelledModel:
        async def ainvoke(self, messages):
            raise asyncio.CancelledError

    messages = [HumanMessage(content="old"), AIMessage(content="done"),
                HumanMessage(content="current")]
    with pytest.raises(asyncio.CancelledError):
        await ContextCompactor(CancelledModel()).compact(messages, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [[], ["wrong"], ["c1", "c1"]])
async def test_invalid_tool_blocks_rejected_before_model_call(results):
    model = ScriptedModel([])
    messages = [HumanMessage(content="old"), AIMessage(content="", tool_calls=[
        {"id": "c1", "name": "read", "args": {}},
    ]), *[ToolMessage(content="result", tool_call_id=call_id) for call_id in results],
        HumanMessage(content="current")]
    with pytest.raises(ContextWindowExceededError, match="tool"):
        await ContextCompactor(model).compact(messages, 100)
    assert model.snapshots == []


@pytest.mark.asyncio
async def test_single_turn_between_auto_and_hard_guard_does_not_fake_compaction(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "single " * 300})
    count = estimate_message_tokens(session.derive_messages())
    model = ScriptedModel([])
    before = session.events
    messages = await ContextBuilder(model, max_context_tokens=int(count / 0.8)).build(session)
    assert messages == session.derive_messages()
    assert session.events == before
    assert model.snapshots == []


@pytest.mark.parametrize("kwargs", [
    {"max_context_tokens": 0}, {"auto_compact_threshold": 0.9},
    {"hard_guard_threshold": 1.5}, {"auto_compact_threshold": float("nan")},
])
def test_invalid_context_budget_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ContextBuilder(ScriptedModel([]), **kwargs)


@pytest.mark.asyncio
async def test_valid_summary_above_auto_target_uses_smaller_fallback():
    verbose = {**SUMMARY, "facts": ["fact " * 500]}
    response = AIMessage(content=json.dumps(verbose))
    model = ScriptedModel([response])
    messages = [HumanMessage(content="old " * 600), AIMessage(content="done"),
                HumanMessage(content="current")]
    result = await ContextCompactor(
        model, max_context_tokens=1500, auto_compact_threshold=0.3,
    ).compact(messages, estimate_message_tokens(messages))
    assert result.fallback_used
    assert result.token_estimate < 450
