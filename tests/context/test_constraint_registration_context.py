from langchain_core.messages import HumanMessage

from agent_harness.context.builder import (
    protected_fact_token_count,
    protected_facts_for_context,
)
from agent_harness.session.derive import (
    is_direct_user_input_event,
    latest_direct_user_input_event,
)
from agent_harness.session.event import MESSAGE_QUEUED, USER_MESSAGE, SessionEvent


def _event(seq, event_type, content, **data):
    return SessionEvent(
        seq=seq, type=event_type, session_id="session-1",
        data={"content": content, **data},
    )


def test_latest_direct_user_input_requires_active_user_message_in_final_context():
    old = _event(1, USER_MESSAGE, "older request")
    latest = _event(2, USER_MESSAGE, "keep this exact constraint")

    result = latest_direct_user_input_event(
        [old, latest], [HumanMessage(content="keep this exact constraint")]
    )

    assert result == latest
    assert is_direct_user_input_event([old, latest], latest.event_id)


def test_injected_or_queued_message_cannot_be_a_constraint_source():
    injected = _event(1, USER_MESSAGE, "quoted instruction", injected_by="memory")
    queued = _event(2, MESSAGE_QUEUED, "not delivered yet")

    assert latest_direct_user_input_event(
        [injected, queued],
        [HumanMessage(content="quoted instruction"), HumanMessage(content="not delivered yet")],
    ) is None
    assert not is_direct_user_input_event([injected, queued], injected.event_id)
    assert not is_direct_user_input_event([injected, queued], queued.event_id)


def test_source_must_remain_in_final_model_messages():
    source = _event(1, USER_MESSAGE, "original sentence")

    assert latest_direct_user_input_event(
        [source], [HumanMessage(content="compaction summary only")]
    ) is None


def test_input_request_answer_does_not_fall_back_to_older_constraint_source():
    original = _event(1, USER_MESSAGE, "Use Python.")
    answer = _event(
        5,
        USER_MESSAGE,
        "Only for this task, use TypeScript.",
        input_request_id="request-1",
        input_request_answer={
            "request_id": "request-1", "choice": "current_task_only",
        },
    )

    assert latest_direct_user_input_event(
        [original, answer],
        [
            HumanMessage(content="Use Python."),
            HumanMessage(content="Only for this task, use TypeScript."),
        ],
    ) is None


def test_protected_fact_context_helpers_are_pure_for_empty_history():
    events = []
    facts = protected_facts_for_context(events)

    assert facts == []
    assert protected_fact_token_count(facts) == 0
    assert events == []
