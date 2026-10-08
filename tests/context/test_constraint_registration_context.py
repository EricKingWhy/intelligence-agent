from langchain_core.messages import HumanMessage

from agent_harness.context.builder import (
    protected_fact_token_count,
    protected_facts_for_context,
)
from agent_harness.session.derive import (
    derive_messages_with_source_ranges,
    derive_protected_facts,
    is_direct_user_input_event,
    latest_direct_user_input_event,
)
from agent_harness.session.event import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    MESSAGE_QUEUED,
    MESSAGE_SUPERSEDED,
    QUEUE_CANCELLED,
    STEER_APPLIED,
    STEER_REQUESTED,
    USER_MESSAGE,
    SessionEvent,
)


def _event(seq, event_type, content, **data):
    return SessionEvent(
        seq=seq, type=event_type, session_id="session-1",
        data={"content": content, **data},
    )


def _bracket(seq, bracket_id, start, end, summary="compaction summary"):
    """A complete, persisted compaction bracket covering ``[start, end]``."""
    return [
        SessionEvent(
            seq=seq, type=COMPACTION_START, session_id="session-1",
            data={"bracket_id": bracket_id, "source_seq_start": start,
                  "source_seq_end": end},
        ),
        SessionEvent(
            seq=seq + 1, type=CONTEXT_COMPACTED, session_id="session-1",
            data={"bracket_id": bracket_id, "source_seq_start": start,
                  "source_seq_end": end, "summary": summary},
        ),
        SessionEvent(
            seq=seq + 2, type=COMPACTION_END, session_id="session-1",
            data={"bracket_id": bracket_id},
        ),
    ]


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


def test_source_must_remain_a_direct_user_event_after_compaction():
    """来源判据是事件事实，不是"还在不在本次注入的投影里"。

    compaction 只为控预算把原事件收进 summary，从不删原始 session entry（Pi
    `packages/coding-agent/docs/sessions.md:40`："Compaction adds a summary and keeps
    recent messages. It does not delete the original session entries."）。依赖投影
    判来源会让压缩后的 run 再也无法登记约束（本票 P2）。
    """
    source = _event(1, USER_MESSAGE, "本次修改不能新增第三方依赖。")
    events = [source, *_bracket(2, "b1", 1, 1)]

    # 投影里只剩 summary，原文消息不在。
    projected = derive_messages_with_source_ranges(events)
    assert not any(
        isinstance(message, HumanMessage)
        and message.content == source.data["content"]
        for message, _range in projected
    )
    # 但这条事件仍是用户的直接输入 ⇒ 仍是合法来源。
    assert is_direct_user_input_event(events, source.event_id)


def test_compaction_summary_text_is_never_a_constraint_source():
    """summary 是 `context/compacted` / `user/message(replace)`，都不是用户原话。"""
    source = _event(1, USER_MESSAGE, "本次修改不能新增第三方依赖。")
    bracket = _bracket(2, "b1", 1, 1, summary="摘要：用户要求不要新增依赖")
    events = [source, *bracket]

    # 投影里那条 replace 形态的 user message 是摘要替身，不是用户输入。
    projected = derive_messages_with_source_ranges(events)
    replacement = next(
        message for message, source_range in projected if source_range == (1, 1)
    )
    assert isinstance(replacement, HumanMessage)
    assert replacement.content == "摘要：用户要求不要新增依赖"
    assert not is_direct_user_input_event(events, bracket[1].event_id)
    # 压缩后的 run 里，投影里那条摘要替身也不能被当成"最新的用户输入"。
    assert latest_direct_user_input_event(
        events, [message for message, _range in projected]
    ) is None


def test_superseded_user_message_is_not_a_source():
    """ADR-0030：被取代的用户消息退出模型可见投影，也不再是可登记来源。"""
    old = _event(1, USER_MESSAGE, "Use tabs.")
    replacement = _event(2, USER_MESSAGE, "Use spaces instead.")
    marker = SessionEvent(
        seq=3, type=MESSAGE_SUPERSEDED, session_id="session-1",
        data={"superseded_seq": 1},
    )

    assert not is_direct_user_input_event([old, replacement, marker], old.event_id)
    assert is_direct_user_input_event([old, replacement, marker], replacement.event_id)


def test_supersede_with_empty_replacement_slot_keeps_source_active():
    """#614①（Call 3 P3-1）：替换槽空 ⇒ 这次 supersede 没发生过，目标仍是来源。

    `MESSAGE_QUEUED → MESSAGE_SUPERSEDED → QUEUE_CANCELLED` 是可达成形状：排队项本身
    被取消了，没有任何活跃用户事件落进替换槽。投影（`derive_protected_facts`）按
    "标记作废、目标保持 active"处理，事件口径来源校验必须同判——否则同一条事件流上
    投影说"这条事实 active/受保护"，`register_constraint` 的 `SOURCE_NOT_ACTIVE` 却说
    "已不是有效直接输入"，两处自相矛盾。
    """
    old = _event(1, USER_MESSAGE, "Use tabs.")
    queued = _event(2, MESSAGE_QUEUED, "Use spaces", queue_id="q9")
    marker = SessionEvent(
        seq=3, type=MESSAGE_SUPERSEDED, session_id="session-1",
        data={"superseded_seq": 1},
    )
    cancel = SessionEvent(
        seq=4, type=QUEUE_CANCELLED, session_id="session-1",
        data={"queue_id": "q9"},
    )
    events = [old, queued, marker, cancel]

    # 投影口径：唯一 active 事实仍是 seq 1。
    assert [
        (fact.type, fact.source_seq, fact.status)
        for fact in derive_protected_facts(events)
    ] == [("user_goal", 1, "active")]
    # 来源口径必须同判（这正是 Call 3 复现里分叉的那一格）。
    assert is_direct_user_input_event(events, old.event_id)


def test_supersede_with_live_replacement_still_retires_the_target():
    """空槽规则的反例边界：替换槽真被填上时，supersede 照常生效。"""
    old = _event(1, USER_MESSAGE, "Use tabs.")
    replacement = _event(2, USER_MESSAGE, "Use spaces instead.")
    marker = SessionEvent(
        seq=3, type=MESSAGE_SUPERSEDED, session_id="session-1",
        data={"superseded_seq": 1},
    )

    assert not is_direct_user_input_event([old, replacement, marker], old.event_id)


def test_cancelled_queue_item_is_not_a_source():
    """取消掉的排队项从未成为用户输入（AC05）；已投递的仍不是直接 USER_MESSAGE。

    取消规则对 `is_direct_user_input_event` 的作用面是**负向且已被类型检查覆盖**：
    取消标记只落在 `message/queued` 上，而本函数先要求 `type == USER_MESSAGE`，两者无
    交集，所以取消分支不可达（Call 3 P3-2：它曾是死代码，现已删；取消规则的真实作用
    面是投影，由 `user_source_events` 单点承载）。这里两条断言的实际守卫分别是类型检查
    与"从未投递"——不是取消规则本身。
    """
    queued = _event(1, MESSAGE_QUEUED, "remember to use tabs", queue_id="q1")
    cancel = SessionEvent(
        seq=2, type=QUEUE_CANCELLED, session_id="session-1",
        data={"queue_id": "q1"},
    )

    assert not is_direct_user_input_event([queued, cancel], queued.event_id)
    assert not is_direct_user_input_event([queued], queued.event_id)


def test_steer_request_is_not_a_direct_user_message_source():
    """`steer/requested` 是未投递请求，不是 `USER_MESSAGE`（AC05）。"""
    steer = SessionEvent(
        seq=1, type=STEER_REQUESTED, session_id="session-1",
        data={"content": "also use tabs", "steer_id": "s1"},
    )
    applied = SessionEvent(
        seq=2, type=STEER_APPLIED, session_id="session-1",
        data={"steer_id": "s1"},
    )

    assert not is_direct_user_input_event([steer, applied], steer.event_id)


def test_replace_shaped_user_message_is_not_a_source():
    """摘要替身走 `user/message(replace)`——它是 `USER_MESSAGE` 但**不是**用户说的话。

    来源判据改成事件口径之后，这条负例是它的守门人：只排除 `injected_by` 而不排除
    `replace`，模型就能把 compaction 摘要当用户原话登记成约束（比"压缩后漏登记"
    更坏）。`derive_protected_facts` 与它共用同一份判据（`user_source_events`）。
    """
    summary_as_user_message = SessionEvent(
        seq=1, type=USER_MESSAGE, session_id="session-1",
        data={"content": "摘要：用户要求不要新增依赖", "replace": True},
    )
    real = SessionEvent(
        seq=2, type=USER_MESSAGE, session_id="session-1",
        data={"content": "本次修改不能新增第三方依赖。"},
    )

    assert not is_direct_user_input_event(
        [summary_as_user_message, real], summary_as_user_message.event_id
    )
    assert is_direct_user_input_event(
        [summary_as_user_message, real], real.event_id
    )


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
