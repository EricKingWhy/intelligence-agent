import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_harness.context.builder import ContextBuilder, ContextWindowExceededError
from agent_harness.context.compactor import (
    ContextCompactor,
    _programmatic_summary_sections,
)
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    Session,
)
from agent_harness.session.derive import ProtectedFact
from agent_harness.session.fork import fork_session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage.sqlite import SqliteSessionMetaStore


@pytest.mark.asyncio
async def test_context_builder_injects_exact_facts_and_accounts_for_their_budget(
    tmp_path,
):
    session = Session.start(JsonlSessionStore(tmp_path), session_id="context")
    user_text = "完成 W-02；禁止写入 main；订单号 ORD-84721 不得改写。"
    session.append(USER_MESSAGE, {"content": user_text})
    before = session.events

    builder = ContextBuilder(None, max_context_tokens=4_000)
    messages = await builder.build(session)

    assert isinstance(messages[0], SystemMessage)
    assert user_text in messages[0].content
    assert "ORD-84721" in messages[0].content
    assert session.events == before
    assert builder.usage_snapshot(session)["other"] > 0


@pytest.mark.asyncio
async def test_fact_registry_does_not_duplicate_every_user_turn(tmp_path):
    session = Session.start(JsonlSessionStore(tmp_path), session_id="raw-turns")
    initial_goal = "完成 W-02，并保留订单号 ORD-84721。"
    later_instruction = "补充：测试通过后再整理结果。"
    session.append(USER_MESSAGE, {"content": initial_goal})
    session.append(USER_MESSAGE, {"content": later_instruction})

    messages = await ContextBuilder(None, max_context_tokens=4_000).build(session)
    facts_message = next(
        message
        for message in messages
        if isinstance(message, SystemMessage)
        and "Protected task facts" in message.content
    )
    facts = json.loads(facts_message.content.rsplit("\n", 1)[1])

    assert len(facts) == 1
    assert facts[0]["type"] == "user_goal"
    assert facts[0]["value"] == initial_goal
    assert later_instruction not in facts_message.content
    assert [
        message.content for message in messages if isinstance(message, HumanMessage)
    ] == [initial_goal, later_instruction]


def test_compaction_keeps_raw_user_turns_in_section_one_and_registry_minimal():
    first = "完成 W-02，保留 ID ORD-84721。"
    second = "后来补充：先跑测试。"
    sections = _programmatic_summary_sections(
        [HumanMessage(content=first), HumanMessage(content=second)]
    )

    raw_user_turns = json.loads(sections["## 原始目标与用户约束"])

    assert raw_user_turns == [first, second]
    assert sections["## 保护事实表"] == "(none)"


@pytest.mark.asyncio
async def test_protected_facts_survive_optional_context_provider_failure(tmp_path):
    class FailingProvider:
        name = "memory"

        async def select(self, _session, _remaining):
            raise RuntimeError("memory unavailable")

    session = Session.start(JsonlSessionStore(tmp_path), session_id="provider-failure")
    exact_fact = "保留精确 ID ORD-84721。"
    session.append(USER_MESSAGE, {"content": exact_fact})

    messages = await ContextBuilder(
        None, context_providers=[FailingProvider()], max_context_tokens=4_000,
    ).build(session)

    protected = next(
        message for message in messages
        if isinstance(message, SystemMessage) and "Protected task facts" in message.content
    )
    assert exact_fact in protected.content


@pytest.mark.asyncio
async def test_protected_facts_over_budget_stop_before_any_fact_is_truncated(tmp_path):
    session = Session.start(JsonlSessionStore(tmp_path), session_id="too-large")
    session.append(USER_MESSAGE, {"content": "必须保留精确编号 ORD-123456789。"})
    builder = ContextBuilder(
        None,
        max_context_tokens=4_000,
        protected_fact_token_budget=1,
    )

    with pytest.raises(ContextWindowExceededError, match="facts withheld"):
        await builder.build(session)


@pytest.mark.asyncio
async def test_protected_facts_survive_build_compaction_restart_and_fork(tmp_path):
    store = JsonlSessionStore(tmp_path / "sessions")
    meta = SqliteSessionMetaStore(tmp_path / "meta.db")
    await meta.initialize()
    session = Session.start(store, session_id="protected-lifecycle")
    goal_text = "完成 W-02；禁止写入 main；精确订单号 ORD-84721。"
    goal = session.append(USER_MESSAGE, {"content": goal_text})
    session.register_protected_fact(
        fact_type="user_goal", value=goal_text, source_event_id=goal.event_id,
    )
    session.append(
        MODEL_COMPLETED,
        {
            "content": "Long historical analysis with no durable outcome. " * 5_000,
            "tool_calls": [
                {
                    "id": "failed-call",
                    "name": "run_tests",
                    "args": {"command": "pytest -q tests/context"},
                }
            ],
        },
    )
    attempt = session.append(
        TOOL_CALL,
        {
            "tool_call_id": "failed-call",
            "tool_name": "run_tests",
            "args": {"command": "pytest -q tests/context"},
        },
    )
    refusal = session.append(
        TOOL_RESULT,
        {
            "tool_call_id": "failed-call",
            "content": (
                '{"ok":false,"message":"tests failed",'
                '"error_code":"TOOL_EXECUTION_ERROR"}'
            ),
        },
    )
    boundary = session.append(USER_MESSAGE, {"content": "继续处理剩余验收项。"})

    messages = await ContextBuilder(
        _SummaryModel(), max_context_tokens=50_000, auto_compact_threshold=0.3,
    ).build(session)

    compacted = next(
        event for event in session.events if event.type == "context/compacted"
    )
    protected_table = compacted.data["summary"].split(
        "## 保护事实表\n", 1,
    )[1].split("\n\n## ", 1)[0]
    assert any(
        fact["value"] == goal_text for fact in json.loads(protected_table)
    )
    failure_record = next(
        fact for fact in json.loads(protected_table)
        if fact["type"] == "failed_approach"
    )
    assert failure_record["source_event_id"] == attempt.event_id
    assert failure_record["evidence_event_id"] == refusal.event_id
    assert failure_record["value"]["args"]["command"] == "pytest -q tests/context"
    assert any(
        isinstance(message, SystemMessage) and "ORD-84721" in message.content
        for message in messages
    )
    reloaded = Session.load(store, session.session_id)
    rebuilt = await ContextBuilder(None, max_context_tokens=50_000).build(reloaded)
    protected_message = next(
        message for message in rebuilt
        if isinstance(message, SystemMessage) and "Protected task facts" in message.content
    )
    assert goal_text in protected_message.content
    assert "ORD-84721" in protected_message.content
    assert "failed_approach" in protected_message.content
    assert "pytest -q tests/context" in protected_message.content

    child = await fork_session(
        store, meta, session.session_id,
        boundary_user_message_seq=boundary.seq,
        child_session_id="protected-child",
    )
    child_messages = await ContextBuilder(None, max_context_tokens=50_000).build(child)
    child_fact_message = next(
        message for message in child_messages
        if isinstance(message, SystemMessage) and "Protected task facts" in message.content
    )
    assert goal_text in child_fact_message.content
    assert "ORD-84721" in child_fact_message.content
    assert "pytest -q tests/context" in child_fact_message.content


class _SummaryModel:
    async def ainvoke(self, _messages):
        return AIMessage(
            content=(
                "## 已完成工作与关键决策\n摘要模型把订单号改成 ORD-99999\n\n"
                "## 失败方案\n(none)\n\n"
                "## 当前进行中状态\n(none)\n\n"
                "## Next Step\n(none)"
            )
        )


@pytest.mark.asyncio
async def test_compaction_writes_protected_fact_table_from_projection_not_summary_model():
    fact = ProtectedFact(
        fact_id="user:fact-1",
        type="exact_identifier",
        value="ORD-84721",
        source_event_id="event-1",
        source_seq=1,
        status="active",
        session_id="s1",
    )
    messages = [
        HumanMessage(content="订单号 ORD-84721。" * 120),
        AIMessage(content="已记录。" * 120),
        HumanMessage(content="继续处理。"),
    ]
    result = await ContextCompactor(
        _SummaryModel(),
        max_context_tokens=10_000,
        auto_compact_threshold=0.8,
        hard_guard_threshold=0.9,
    ).compact(
        messages,
        estimate_message_tokens(messages) + 100,
        protected_facts=[fact],
        reserved_tokens=100,
    )

    assert result.summary is not None
    section = result.summary.split("## 保护事实表\n", 1)[1].split("\n\n## ", 1)[0]
    records = json.loads(section)
    assert records == [fact.to_dict()]
    assert "ORD-99999" in result.summary
    assert "ORD-84721" in section
