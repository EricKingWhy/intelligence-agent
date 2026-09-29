"""ContextCompactor/Builder 的压缩与恢复不变量。"""

import asyncio
import json
import math

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import ContextCompactor, ContextWindowExceededError
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    CONTEXT_COMPACTION_FAILED,
    MODEL_COMPLETED,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.derive import derive_protected_facts
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

SUMMARY = {
    "facts": ["read completed"], "decisions": [], "constraints": ["keep Chinese"],
    "failed_attempts": [], "unresolved": [], "artifact_refs": ["abc123"],
    "citations": [], "tool_outcomes": ["call-1 succeeded"],
}

#: 模型撰写的四节；其余四节由 compactor 从消息投影生成。
MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


@pytest.mark.asyncio
async def test_builder_compacts_old_turn_and_preserves_persistent_history(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
    session.append(USER_MESSAGE, {"content": "current request"})
    before = session.events
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content.startswith("## Protected task facts\n")
    assert isinstance(messages[1], SystemMessage)
    assert "## 原始目标与用户约束" in messages[1].content
    assert "读取旧记录并继续。" in messages[1].content
    assert messages[2:] == [HumanMessage(content="current request")]
    assert estimate_message_tokens(messages) < 5600
    assert session.events[:-3] == before  # 3 new events: START, COMPACTED, END
    # Verify all 3 bracket events were written
    new_events = session.events[len(before):]
    event_types = [e.type for e in new_events]
    assert "compaction/start" in event_types
    assert "context/compacted" in event_types
    assert "compaction/end" in event_types
    assert len(model.snapshots) == 1


@pytest.mark.asyncio
async def test_summary_failure_keeps_constraints_and_tool_pair_after_store_reload(tmp_path):
    """timeout ×2：低估值保留原投影；每次失败落一条任务可见状态（W-04 #348）。"""
    class FailingModel:
        calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            raise TimeoutError("summary timed out")

    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    exact_constraint = "不得删除 old_rows；精确 ID 是 R-042"
    large_result = "历史工具输出 " * 8000
    session.append(USER_MESSAGE, {"content": exact_constraint})
    session.append(MODEL_COMPLETED, {
        "content": "正在读取指定记录。",
        "tool_calls": [{
            "id": "call-r-042", "name": "read_rows", "args": {"id": "R-042"},
        }],
    })
    session.append(TOOL_RESULT, {
        "tool_call_id": "call-r-042", "content": large_result,
    })
    session.append(MODEL_COMPLETED, {"content": "已读取记录 R-042。"})
    session.append(USER_MESSAGE, {"content": "继续处理。"})
    original_events = list(session.events)
    model = FailingModel()

    await ContextBuilder(
        model, max_context_tokens=100_000, auto_compact_threshold=0.05,
    ).build(session)

    # W-04 (#348)：失败至多重试一次（两次调用），且每次失败留任务可见状态。
    assert model.calls == 2
    failure_events = [e for e in session.events
                      if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["attempt"] for e in failure_events] == [1, 2]
    assert all(e.data["error_class"] == "timeout" for e in failure_events)
    assert all(e.data["message"] == "TimeoutError" for e in failure_events)
    # 低估（< hard guard）：安全继续，原投影保留、无压缩 bracket。
    assert not any(event.type in {
        COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END,
    } for event in session.events)

    reloaded = Session.load(JsonlSessionStore(root=tmp_path), session.session_id)
    # 失败事件先于压缩事实持久化：重载后原事件 + 两条失败记录原样在场
    #（重放不丢、不重复、不伪造）。
    persisted = store.read_events(session.session_id)
    assert persisted[:len(original_events)] == original_events
    assert [e.data["attempt"] for e in persisted
            if e.type == CONTEXT_COMPACTION_FAILED] == [1, 2]
    rebuilt = await ContextBuilder(
        ScriptedModel([]), max_context_tokens=100_000,
    ).build(reloaded)

    assert any(isinstance(message, HumanMessage)
               and message.content == exact_constraint for message in rebuilt)
    tool_call = next(message for message in rebuilt
                     if isinstance(message, AIMessage) and message.tool_calls)
    assert tool_call.tool_calls[0]["id"] == "call-r-042"
    tool_result = next(message for message in rebuilt if isinstance(message, ToolMessage))
    assert tool_result.tool_call_id == "call-r-042"
    assert tool_result.content == large_result
    assert not any(event.type in {
        COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END,
    } for event in store.read_events(session.session_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "timeout", "empty", "tool_call"])
async def test_summary_failure_keeps_original_projection(failure):
    class Model:
        async def ainvoke(self, messages):
            if failure == "exception":
                raise ConnectionError("offline")
            if failure == "timeout":
                await asyncio.Event().wait()
            if failure == "empty":
                return AIMessage(content="")
            if failure == "tool_call":
                return AIMessage(content=MODEL_SECTIONS, tool_calls=[
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
    assert not result.fallback_used
    assert result.compacted_turn_count == 0
    assert result.summary is None
    assert result.messages == messages


@pytest.mark.asyncio
async def test_persistence_failure_does_not_shadow_original_tool_context(tmp_path, monkeypatch):
    session = make_session(tmp_path)
    constraint = "不得删除 old_rows；精确 ID 是 R-042"
    large_result = "历史工具输出 " * 8000
    session.append(USER_MESSAGE, {"content": constraint})
    session.append(MODEL_COMPLETED, {
        "content": "正在读取指定记录。",
        "tool_calls": [{
            "id": "call-r-042", "name": "read_rows", "args": {"id": "R-042"},
        }],
    })
    session.append(TOOL_RESULT, {
        "tool_call_id": "call-r-042", "content": large_result,
    })
    session.append(MODEL_COMPLETED, {"content": "已读取记录 R-042。"})
    session.append(USER_MESSAGE, {"content": "继续处理。"})
    original_events = list(session.events)
    append_event = session._store.append_event

    def fail_before_end(session_id, event):
        if event.type == COMPACTION_END:
            raise OSError("injected append failure")
        append_event(session_id, event)

    monkeypatch.setattr(session._store, "append_event", fail_before_end)
    builder = ContextBuilder(
        ScriptedModel([AIMessage(content=MODEL_SECTIONS)]),
        max_context_tokens=100_000, auto_compact_threshold=0.05,
    )
    with pytest.raises(OSError, match="injected append failure"):
        await builder.build(session)

    reloaded = Session.load(JsonlSessionStore(root=tmp_path), session.session_id)
    rebuilt = await ContextBuilder(
        ScriptedModel([]), max_context_tokens=100_000,
    ).build(reloaded)

    assert next(message.content for message in rebuilt
                if isinstance(message, HumanMessage)) == constraint
    call = next(message for message in rebuilt
                if isinstance(message, AIMessage) and message.tool_calls)
    assert call.tool_calls[0]["id"] == "call-r-042"
    result = next(message for message in rebuilt if isinstance(message, ToolMessage))
    assert result.tool_call_id == "call-r-042"
    assert result.content == large_result
    stored = JsonlSessionStore(root=tmp_path).read_events(session.session_id)
    assert stored[:len(original_events)] == original_events
    assert not any(
        event.type == COMPACTION_END and event.data.get("bracket_id")
        for event in stored[len(original_events):]
    )


@pytest.mark.asyncio
async def test_hard_guard_rejects_when_recent_turn_cannot_fit(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "old"})
    session.append(MODEL_COMPLETED, {"content": "done"})
    session.append(USER_MESSAGE, {"content": "current " * 2000})
    before = session.events
    with pytest.raises(ContextWindowExceededError):
        await ContextBuilder(ScriptedModel([]), max_context_tokens=1000).build(session)
    # W-04 (#348)：双次摘要尝试的失败记录先于拒绝落盘（任务可见状态），
    # 除此之外不新增任何事件（无 bracket、无改写）。
    assert session.events[:len(before)] == before
    failure_events = [e for e in session.events
                      if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["attempt"] for e in failure_events] == [1, 2]
    assert all(e.data["error_class"] == "transport_error" for e in failure_events)
    assert len(session.events) == len(before) + 2


@pytest.mark.asyncio
async def test_invalid_summary_is_rejected_and_system_constraints_survive():
    huge = {**SUMMARY, "facts": ["huge " * 4000]}
    model = ScriptedModel([AIMessage(content=json.dumps(huge))])
    messages = [SystemMessage(content="Never modify protected files"),
                HumanMessage(content="old"), AIMessage(content="done"),
                HumanMessage(content="current")]
    result = await ContextCompactor(model, max_context_tokens=1000).compact(
        messages, estimate_message_tokens(messages),
    )
    assert not result.fallback_used
    assert result.compacted_turn_count == 0
    assert result.messages == messages


@pytest.mark.asyncio
async def test_summary_request_over_budget_keeps_projection_without_events(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "large " * 10000})
    session.append(MODEL_COMPLETED, {"content": "done"})
    session.append(USER_MESSAGE, {"content": "current"})
    model = ScriptedModel([])
    before = list(session.events)
    with pytest.raises(ContextWindowExceededError):
        await ContextBuilder(model, max_context_tokens=1000).build(session)
    assert model.snapshots == []
    assert session.events == before


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
async def test_tool_pair_split_by_user_boundary_is_rejected_before_summary():
    model = ScriptedModel([])
    messages = [
        HumanMessage(content="old request"),
        AIMessage(content="", tool_calls=[
            {"id": "call-1", "name": "read", "args": {}},
        ]),
        HumanMessage(content="current request"),
        ToolMessage(content="late result", tool_call_id="call-1"),
    ]

    with pytest.raises(ContextWindowExceededError, match="tool call/result"):
        await ContextCompactor(model).compact(messages, 100)

    assert model.snapshots == []


@pytest.mark.asyncio
async def test_single_turn_between_auto_and_hard_guard_does_not_fake_compaction(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "single " * 300})
    count = estimate_message_tokens(session.derive_messages())
    facts_message = ContextBuilder._protected_facts_message(
        derive_protected_facts(session.events),
    )
    if facts_message is not None:
        count += estimate_message_tokens([facts_message])
    model = ScriptedModel([])
    before = session.events
    # Keep the original 0.80/0.90 boundary explicit after the defaults changed.
    messages = await ContextBuilder(
        model,
        max_context_tokens=int(count / 0.8),
        auto_compact_threshold=0.8,
        hard_guard_threshold=0.9,
    ).build(session)
    expected = [facts_message] if facts_message is not None else []
    expected.extend([
        HumanMessage(content=DEFAULT_REGISTRY.assemble(
            "frame:context_pressure").meta_user_text),
        *session.derive_messages(),
    ])
    assert messages == expected
    assert session.events == before
    assert model.snapshots == []


@pytest.mark.parametrize("kwargs", [
    {"max_context_tokens": 0}, {"auto_compact_threshold": 0.95},
    {"hard_guard_threshold": 1.5}, {"auto_compact_threshold": float("nan")},
])
def test_invalid_context_budget_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ContextBuilder(ScriptedModel([]), **kwargs)


@pytest.mark.asyncio
async def test_summary_that_misses_auto_target_is_rejected():
    verbose = {**SUMMARY, "facts": ["fact " * 500]}
    response = AIMessage(content=json.dumps(verbose))
    model = ScriptedModel([response])
    messages = [HumanMessage(content="old " * 600), AIMessage(content="done"),
                HumanMessage(content="current")]
    result = await ContextCompactor(
        model, max_context_tokens=1500, auto_compact_threshold=0.3,
    ).compact(messages, estimate_message_tokens(messages))
    assert not result.fallback_used
    assert result.compacted_turn_count == 0
    assert result.messages == messages


# ── 摘要模型失败与结构化证据 ──


@pytest.mark.asyncio
async def test_huge_tool_call_args_and_summary_failure_hit_hard_guard():
    """不安全摘要不能替代原始工具上下文；原文超护栏时必须停止。"""
    class FailingModel:
        async def ainvoke(self, messages):
            raise ConnectionError("offline")

    huge_args = {"path": "big.txt", "content": "y" * 100_000}
    messages = [
        HumanMessage(content="old " * 2000),
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "write", "args": huge_args}]),
        ToolMessage(content="written", tool_call_id="c1"),
        HumanMessage(content="current"),
    ]
    with pytest.raises(ContextWindowExceededError):
        await ContextCompactor(FailingModel(), max_context_tokens=8000).compact(
            messages, estimate_message_tokens(messages),
        )


@pytest.mark.asyncio
async def test_eight_section_summary_passes_shrink_validation():
    """harness 生成四个确定性节后，完整八节摘要通过精确与 shrink 校验。"""
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    messages = [
        HumanMessage(content="不得删除 old_rows；精确 ID 是 R-042"),
        AIMessage(content="old analysis " * 600),
        HumanMessage(content="current"),
    ]
    result = await ContextCompactor(
        model, max_context_tokens=8000,
    ).compact(messages, estimate_message_tokens(messages))
    assert not result.fallback_used
    assert result.compacted_turn_count == 1
    assert result.summary is not None
    assert result.summary.startswith("## 原始目标与用户约束\n")
    assert "不得删除 old_rows；精确 ID 是 R-042" in result.summary
    assert "R-042" in result.summary
    assert "## 文件清单\n(none)" in result.summary
    assert result.bracket_id is not None


def test_programmatic_sections_preserve_exact_command_error_and_path():
    from agent_harness.context.compactor import _programmatic_summary_sections

    constraint = "不得删除 old_rows"
    command = "python -m pytest tests/context/test_compactor.py"
    path = r"C:\work tree\src\agent.py"
    error = r"FileNotFoundError: missing C:\work tree\data.json"
    messages = [
        HumanMessage(content=f"{constraint}；预算为 4096 tokens"),
        AIMessage(content="", tool_calls=[{
            "id": "call-r-042", "name": "run", "args": {
                "command": command, "path": path, "max_tokens": 8192,
            },
        }]),
        ToolMessage(content=error, tool_call_id="call-r-042", status="error"),
    ]

    sections = _programmatic_summary_sections(messages)
    identifiers = json.loads(sections["## 精确标识清单"])

    assert json.loads(sections["## 原始目标与用户约束"]) == [
        f"{constraint}；预算为 4096 tokens",
    ]
    assert command in identifiers
    assert path in identifiers
    assert "call-r-042" in identifiers
    assert error in identifiers
    assert "4096" in identifiers
    assert "8192" in identifiers
    file_paths = json.loads(sections["## 文件清单"])
    assert path in file_paths
    assert "tests/context/test_compactor.py" in file_paths


def test_summary_validator_rejects_tampered_programmatic_sections():
    from agent_harness.context.compactor import (
        _MODEL_SUMMARY_HEADINGS,
        _SUMMARY_HEADINGS,
        _assemble_summary,
        _parse_summary_sections,
        _validate_summary,
    )

    messages = [
        HumanMessage(content="不得删除 old_rows；精确 ID 是 R-042"),
        AIMessage(content="old analysis"),
    ]
    summary = _assemble_summary(
        messages, _parse_summary_sections(MODEL_SECTIONS, _MODEL_SUMMARY_HEADINGS),
    )

    sections = _parse_summary_sections(summary, _SUMMARY_HEADINGS)
    for index in (0, 6):
        tampered_sections = list(sections)
        tampered_sections[index] = f"rewritten\n{tampered_sections[index]}"
        tampered = "\n\n".join(
            f"{heading}\n{body}"
            for heading, body in zip(_SUMMARY_HEADINGS, tampered_sections)
        )
        with pytest.raises(ValueError, match="Programmatic summary section"):
            _validate_summary(tampered, messages)
