"""ContextCompactor/Builder 的压缩与恢复不变量。"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import (
    _PROG_SECTION_MAX_ENTRY_CHARS,
    ContextCompactor,
    ContextWindowExceededError,
    _programmatic_summary_sections,
)
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
from agent_harness.session.event import (
    MESSAGE_QUEUED,
    MESSAGE_SUPERSEDED,
    QUEUE_CANCELLED,
    SessionEvent,
)
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
    assert "Runtime permission and approval checks are authoritative" in messages[0].content
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content.startswith("## Protected task facts\n")
    assert isinstance(messages[2], HumanMessage)
    assert messages[2].name == "context_compaction_summary"
    assert "## 原始目标与用户约束" in messages[2].content
    assert "读取旧记录并继续。" in messages[2].content
    assert messages[3:] == [HumanMessage(content="current request")]
    assert estimate_message_tokens(messages) < 5600
    assert session.events[:-3] == before  # 3 new events: START, COMPACTED, END
    # Verify all 3 bracket events were written
    new_events = session.events[len(before):]
    event_types = [e.type for e in new_events]
    assert "compaction/start" in event_types
    assert "context/compacted" in event_types
    assert "compaction/end" in event_types
    assert len(model.snapshots) == 1


def test_user_message_that_looks_like_summary_is_kept_as_user_content():
    """长得像摘要头部的用户消息不得被当旧摘要 decode（双重形态识别的边界）。

    #556 裁决 C 后 [0] 不再逐字携带用户消息——"保留为用户内容"的可观测面变为：
    消息仍经 `visit()` 提取（标识符进 [6]），且目标节不因此出现幽灵内容。
    """
    content = "## 原始目标与用户约束\n用户提供的普通文本 R-042"
    sections = _programmatic_summary_sections([HumanMessage(content=content)], [])

    assert sections["## 原始目标与用户约束"] == "(none)", "无事实源 ⇒ 目标节空"
    assert "R-042" in json.loads(sections["## 精确标识清单"]), (
        "消息仍作为内容被 visit（没有被当摘要吞掉）"
    )


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

    assert any(
        message.content == constraint
        for message in rebuilt
        if isinstance(message, HumanMessage)
    )
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


@pytest.mark.parametrize(
    ("second_failure", "expected_class"),
    [
        ("non_text", "non_text"),
        ("timeout", "timeout"),
        ("provider", "transport_error"),
        ("target", "target_not_reached"),
    ],
)
@pytest.mark.asyncio
async def test_rejected_candidate_is_not_used_when_retry_fails(
    second_failure, expected_class,
):
    class TwoResponseModel:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content=MODEL_SECTIONS)
            if second_failure == "timeout":
                raise TimeoutError("summary timed out")
            if second_failure == "provider":
                raise ConnectionError("provider unavailable")
            if second_failure == "target":
                return AIMessage(content=MODEL_SECTIONS)
            return AIMessage(content=[{"type": "text", "text": "bad"}])

    messages = [
        HumanMessage(content="goal"),
        AIMessage(content="history " * 4_000),
        HumanMessage(content="next"),
    ]
    model = TwoResponseModel()
    result = await ContextCompactor(
        model, max_context_tokens=100_000, auto_compact_threshold=0.30,
    ).compact(
        messages, estimate_message_tokens(messages), reserved_tokens=30_000,
    )

    assert model.calls == 2
    assert [failure.error_class for failure in result.failures] == [
        "target_not_reached", expected_class,
    ]
    assert result.compacted_turn_count == 0
    assert result.summary is None
    assert result.bracket_id is None
    assert result.messages == messages


@pytest.mark.asyncio
async def test_rejected_first_candidate_is_not_used_when_retry_succeeds():
    class TwoResponseModel:
        def __init__(self):
            oversized = MODEL_SECTIONS.replace(
                "已完成读取历史记录，并选择直接展示内容。",
                "已完成读取历史记录。" + "细节 " * 500,
            )
            self.responses = [
                AIMessage(content=oversized), AIMessage(content=MODEL_SECTIONS),
            ]
            self.calls = 0

        async def ainvoke(self, messages):
            response = self.responses[self.calls]
            self.calls += 1
            return response

    messages = [
        HumanMessage(content="goal"),
        AIMessage(content="history " * 4_000),
        HumanMessage(content="next"),
    ]
    model = TwoResponseModel()
    reserved_tokens = 29_000
    result = await ContextCompactor(
        model, max_context_tokens=100_000, auto_compact_threshold=0.30,
    ).compact(
        messages, estimate_message_tokens(messages), reserved_tokens=reserved_tokens,
    )

    assert model.calls == 2
    assert [failure.error_class for failure in result.failures] == [
        "target_not_reached",
    ]
    assert result.compacted_turn_count == 1
    assert result.summary is not None
    assert "细节" not in result.summary
    assert "已完成读取历史记录，并选择直接展示内容。" in result.summary
    assert result.bracket_id is not None
    assert estimate_message_tokens(result.messages) + reserved_tokens < 30_000


@pytest.mark.asyncio
async def test_failed_summaries_over_hard_guard_raise_with_both_failures():
    class TwoResponseModel:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content=MODEL_SECTIONS)
            return AIMessage(content=[{"type": "text", "text": "bad"}])

    messages = [
        HumanMessage(content="goal"),
        AIMessage(content="history " * 4_000),
        HumanMessage(content="next"),
    ]
    model = TwoResponseModel()
    reserved_tokens = 90_000
    token_estimate = estimate_message_tokens(messages) + reserved_tokens

    with pytest.raises(ContextWindowExceededError) as error:
        await ContextCompactor(
            model, max_context_tokens=100_000, auto_compact_threshold=0.30,
        ).compact(
            messages, token_estimate, reserved_tokens=reserved_tokens,
        )

    assert model.calls == 2
    assert [failure.error_class for failure in error.value.failures] == [
        "target_not_reached", "non_text",
    ]
    assert messages == [
        HumanMessage(content="goal"),
        AIMessage(content="history " * 4_000),
        HumanMessage(content="next"),
    ]


@pytest.mark.parametrize(
    ("token_estimate", "should_raise"),
    [(84_999, False), (85_000, False), (85_001, True)],
)
@pytest.mark.asyncio
async def test_failed_summaries_keep_inclusive_hard_guard_boundary(
    token_estimate, should_raise,
):
    class FailingModel:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            raise ConnectionError("provider unavailable")

    messages = [
        HumanMessage(content="goal"),
        AIMessage(content="history"),
        HumanMessage(content="next"),
    ]
    model = FailingModel()
    compactor = ContextCompactor(
        model,
        max_context_tokens=100_000,
        auto_compact_threshold=0.30,
        hard_guard_threshold=0.85,
    )

    if should_raise:
        with pytest.raises(ContextWindowExceededError) as error:
            await compactor.compact(messages, token_estimate)
        assert [failure.error_class for failure in error.value.failures] == [
            "transport_error", "transport_error",
        ]
    else:
        result = await compactor.compact(messages, token_estimate)
        assert result.compacted_turn_count == 0
        assert result.summary is None
        assert result.bracket_id is None
        assert result.messages == messages
        assert [failure.error_class for failure in result.failures] == [
            "transport_error", "transport_error",
        ]

    assert model.calls == 2
    assert messages == [
        HumanMessage(content="goal"),
        AIMessage(content="history"),
        HumanMessage(content="next"),
    ]


@pytest.mark.asyncio
async def test_builder_persists_only_failures_when_both_summary_attempts_fail(tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store)
    session.append(USER_MESSAGE, {"content": "goal"})
    session.append(MODEL_COMPLETED, {"content": "history " * 4000})
    session.append(USER_MESSAGE, {"content": "next"})
    original_events = session.events
    original_projection = session.derive_messages()
    model = ScriptedModel([
        AIMessage(content=MODEL_SECTIONS),
        AIMessage(content=[{"type": "text", "text": "bad"}]),
    ])

    await ContextBuilder(
        model,
        max_context_tokens=20_000,
        auto_compact_threshold=0.30,
        system_prompt="context " * 6_000,
    ).build(session)

    assert len(model.snapshots) == 2
    failures = [event for event in session.events
                if event.type == CONTEXT_COMPACTION_FAILED]
    assert [event.data["error_class"] for event in failures] == [
        "target_not_reached", "non_text",
    ]
    assert not any(event.type in {
        COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END,
    } for event in session.events)
    persisted = store.read_events(session.session_id)
    assert persisted[:len(original_events)] == original_events
    assert [event.data["attempt"] for event in persisted
            if event.type == CONTEXT_COMPACTION_FAILED] == [1, 2]
    reloaded = Session.load(store, session.session_id)
    assert reloaded.derive_messages() == original_projection


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
async def test_compactor_keeps_list_system_prefix_with_a_valid_tool_pair():
    system = SystemMessage(content=[{"type": "text", "text": "sys"}])
    messages = [
        system,
        HumanMessage(content="historical request " * 1200),
        AIMessage(
            content="",
            tool_calls=[{
                "id": "call-1", "name": "read_rows", "args": {"id": "R-042"},
            }],
        ),
        ToolMessage(
            content="historical tool result " * 1200,
            tool_call_id="call-1",
        ),
        AIMessage(content="historical response " * 1200),
        HumanMessage(content="current request"),
    ]
    token_estimate = estimate_message_tokens(messages)
    assert 2000 < token_estimate < 17_000
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])

    result = await ContextCompactor(
        model, max_context_tokens=20_000, auto_compact_threshold=0.1,
    ).compact(messages, token_estimate)

    assert result.compacted_turn_count == 1
    assert result.messages[0] == system
    assert isinstance(result.messages[1], HumanMessage)
    assert result.messages[1].name == "context_compaction_summary"
    assert result.messages[-1] == messages[-1]
    assert result.token_estimate == estimate_message_tokens(result.messages)


@pytest.mark.asyncio
async def test_list_system_prefix_without_early_turn_still_hits_hard_guard():
    messages = [
        SystemMessage(content=[{"type": "text", "text": "sys"}]),
        HumanMessage(content="current request " * 2500),
    ]
    token_estimate = estimate_message_tokens(messages)
    assert token_estimate > 850

    compactor = ContextCompactor(
        None, max_context_tokens=1000, auto_compact_threshold=0.3,
    )
    with pytest.raises(ContextWindowExceededError, match="No complete early turn"):
        await compactor.compact(messages, token_estimate)


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
    facts_messages = ContextBuilder._protected_facts_messages(
        derive_protected_facts(session.events),
    )
    count += estimate_message_tokens(facts_messages)
    model = ScriptedModel([])
    before = session.events
    # Keep the original 0.80/0.90 boundary explicit after the defaults changed.
    messages = await ContextBuilder(
        model,
        max_context_tokens=int(count / 0.8) - 1,
        auto_compact_threshold=0.8,
        hard_guard_threshold=0.9,
    ).build(session)
    expected = list(facts_messages)
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
async def test_eight_section_summary_passes_shrink_validation(monkeypatch):
    """harness 生成四个确定性节后，完整八节摘要通过精确与 shrink 校验。"""
    import agent_harness.context.compactor as compactor_module

    estimate = compactor_module.estimate_message_tokens
    shrink_candidates = []

    def track_shrink_candidate(messages):
        if (
            len(messages) == 1
            and isinstance(messages[0], HumanMessage)
            and messages[0].name == "context_compaction_summary"
        ):
            shrink_candidates.append(messages[0])
        return estimate(messages)

    monkeypatch.setattr(
        compactor_module, "estimate_message_tokens", track_shrink_candidate,
    )
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    # #556 裁决 C：目标节内容由 protected_facts 通道承载（与 builder 同一通路：
    # 全量 events 派生）；直连 compactor 的用例显式传入同一份产物。
    facts = derive_protected_facts([
        SessionEvent(seq=1, type=USER_MESSAGE, session_id="s",
                     data={"content": "不得删除 old_rows；精确 ID 是 R-042"}),
    ])
    messages = [
        HumanMessage(content="不得删除 old_rows；精确 ID 是 R-042"),
        AIMessage(content="old analysis " * 600),
        HumanMessage(content="current"),
    ]
    result = await ContextCompactor(
        model, max_context_tokens=8000,
    ).compact(messages, estimate_message_tokens(messages), protected_facts=facts)
    assert not result.fallback_used
    assert result.compacted_turn_count == 1
    assert result.summary is not None
    assert result.summary.startswith("## 原始目标与用户约束\n")
    assert "不得删除 old_rows；精确 ID 是 R-042" in result.summary
    assert "R-042" in result.summary
    assert "## 文件清单\n(none)" in result.summary
    assert result.bracket_id is not None
    assert any(candidate is result.messages[0] for candidate in shrink_candidates)


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

    sections = _programmatic_summary_sections(messages, [])
    identifiers = json.loads(sections["## 精确标识清单"])

    # #556 裁决 C：用户消息原文不再逐字进目标节（该节由 protected_facts 承载，
    # 无事实源时 (none)）；命令/错误/路径的精确读回仍由标识节（有界）承担。
    assert sections["## 原始目标与用户约束"] == "(none)"
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


# ── #556 裁决 C（分流承载）：程序化节的确定性有界化 ─────────────────────────


def _goal_events():
    """两条 distinct 目标 + supersede：goal sources = 首条 + supersede 前最近一条。"""
    return [
        SessionEvent(seq=1, type=USER_MESSAGE, session_id="s1",
                     data={"content": "目标：把 A 改成 B"}),
        SessionEvent(seq=2, type=USER_MESSAGE, session_id="s1",
                     data={"content": "改目标：把 A 改成 C，其余不变"}),
        SessionEvent(seq=3, type=MESSAGE_SUPERSEDED, session_id="s1",
                     data={"superseded_seq": 1}),
    ]


def test_target_section_carries_only_current_effective_goal():
    """C：目标节只保留**当前生效目标**原文 + 归档计数行，历史逐字不再累积。

    旧实现把全部 HumanMessage 逐字 extend（无界）；C 之后历史目标靠
    SessionEvent 回读，叙述性消息不再逐字进程序化节。
    """
    facts = derive_protected_facts(_goal_events())
    sections = _programmatic_summary_sections(
        [HumanMessage(content="改目标：把 A 改成 C，其余不变")], facts,
    )
    target = sections["## 原始目标与用户约束"]
    assert "把 A 改成 C" in target, "当前生效目标必须逐字在场"
    assert "把 A 改成 B" not in target, "历史目标逐字不得再进目标节"
    assert "更早 1 条历史目标已归档" in target, "归档计数行必须在场（回读指针）"


def test_large_first_goal_folds_to_readback_pointer():
    """C 矩阵「大首消息」：超长目标不再逐字撑爆目标节，靠 #430 截断标记回读。"""
    huge = "目标头" + "很长的正文" * 399 + "尾缀SENTINEL-981"  # > 2000 字符
    events = [SessionEvent(seq=1, type=USER_MESSAGE, session_id="s1",
                           data={"content": huge})]
    facts = derive_protected_facts(events)
    sections = _programmatic_summary_sections([HumanMessage(content=huge)], facts)
    target = sections["## 原始目标与用户约束"]
    assert "source_event_id=" in target, "截断标记必须携带回读指针"
    assert "尾缀SENTINEL-981" not in target, "目标节不得逐字携带全文尾部"


def test_identifier_section_converges_under_cap():
    """C 同票处理：标识清单加确定性上限（保留最近 N 条），不再无界累积。"""
    messages = [
        AIMessage(content=f"处理 TSK-{i:04d} 号任务") for i in range(60)
    ]
    sections = _programmatic_summary_sections(messages, [])
    identifiers = json.loads(sections["## 精确标识清单"])
    assert len(identifiers) == 50, "必须收敛到上限而不是无界增长"
    assert "TSK-0000" not in identifiers, "最旧的标识被窗口淘汰"
    assert "TSK-0059" in identifiers, "最新的标识保留（最近偏置）"


def test_long_error_entry_is_truncated_per_entry():
    """C：单条巨串（如全文错误）不再撑爆标识节——逐条截断 + 省略号。"""
    huge_error = "FileNotFoundError: " + "细节" * 1500
    messages = [
        AIMessage(content="", tool_calls=[{
            "id": "call-huge", "name": "run", "args": {"command": "ls"},
        }]),
        ToolMessage(content=huge_error, tool_call_id="call-huge", status="error"),
    ]
    sections = _programmatic_summary_sections(messages, [])
    identifiers = json.loads(sections["## 精确标识清单"])
    assert max(len(entry) for entry in identifiers) <= 201, "单条上限 200 字符 + 省略号"


def test_target_section_none_without_goal_facts():
    """无 user_goal facts ⇒ 目标节 (none)——与八节摘要的空节约定一致。"""
    sections = _programmatic_summary_sections(
        [HumanMessage(content="普通消息，不是任何事实源")], [],
    )
    assert sections["## 原始目标与用户约束"] == "(none)"


def test_cancelled_queued_replacement_keeps_sections_consistent():
    """#614①：取消的替换 ⇒ 标记作废 ⇒ §1（目标节）与 §2（保护事实表）同判。

    S2 序列（修复前实测矛盾）：USER → MESSAGE_QUEUED(新任务) →
    MESSAGE_SUPERSEDED → QUEUE_CANCELLED。修复前 §1 把旧任务当当前生效目标，
    §2 却把旧任务标 superseded——两节自相矛盾且 §2 丢失唯一 active 目标。
    修复后：标记作废，旧目标保持 active，两节一致；被取消的替换既不进
    目标节也不进保护事实节。
    """
    events = [
        SessionEvent(seq=1, type=USER_MESSAGE, session_id="s1",
                     data={"content": "旧任务 ORD-100。"}),
        SessionEvent(seq=2, type=MESSAGE_QUEUED, session_id="s1",
                     data={"queue_id": "queued-1", "content": "新任务 ORD-200。"}),
        SessionEvent(seq=3, type=MESSAGE_SUPERSEDED, session_id="s1",
                     data={"superseded_seq": 1}),
        SessionEvent(seq=4, type=QUEUE_CANCELLED, session_id="s1",
                     data={"queue_id": "queued-1"}),
    ]
    facts = derive_protected_facts(events)
    sections = _programmatic_summary_sections(
        [HumanMessage(content="旧任务 ORD-100。")], facts,
    )

    target = sections["## 原始目标与用户约束"]
    assert "旧任务 ORD-100。" in target, "取消替换 ⇒ 原目标仍是当前生效目标（§1）"
    assert "新任务 ORD-200。" not in target, "被取消的替换不得进目标节"

    protected = json.loads(sections["## 保护事实表"])
    assert protected, "§2 不得为空：原目标必须仍以 active 投影"
    original = next(
        fact for fact in protected if fact["value"] == "旧任务 ORD-100。"
    )
    assert original["status"] == "active", "§2 与 §1 同判：原目标 active"
    assert all(fact["value"] != "新任务 ORD-200。" for fact in protected), (
        "被取消的替换不得以 active 投影"
    )


def test_trimmed_same_prefix_entries_dedup_after_truncation():
    """#614②：去重必须发生在截断**之后**——同前缀超长条目不得以截断值重复。

    旧实现先按全文 `add_once` 去重、后截断：两条仅在 200 字符之后分叉的
    超长条目全文不同、双双入列，截断后收敛为同一个值 ⇒ §6/§7 投影出现
    重复条目。修复：先截断、再按截断值去重（保留最后出现者，与窗口的
    「最近偏置」一致）、最后套数量上限。
    """
    prefix = "同一前缀" + "很长的细节" * 40  # 前 200 字符完全一致
    assert len(prefix) >= _PROG_SECTION_MAX_ENTRY_CHARS
    messages = [
        ToolMessage(content=prefix + "TAIL-A-777", tool_call_id="call-a", status="error"),
        ToolMessage(content=prefix + "TAIL-B-888", tool_call_id="call-b", status="error"),
    ]
    sections = _programmatic_summary_sections(messages, [])
    identifiers = json.loads(sections["## 精确标识清单"])
    assert len(identifiers) == len(set(identifiers)), (
        "截断后同值的条目不得在投影中重复（#614②）"
    )
