"""#1323：用户显式「作为可复用规则」手势的确定性会话级登记（单元面）。

覆盖判据本身（信号 + scope 排除）、逐字登记、同值去重、预算闸门与无效来源的
失败方向。HTTP 全链路（勾选 → user/message → SESSION 组）在
`tests/web/test_1323_explicit_constraint_registration.py`。
"""

from __future__ import annotations

from agent_harness.session import TASK_PROTECTED_FACT, USER_MESSAGE, Session
from agent_harness.session.derive import derive_protected_facts
from agent_harness.session.explicit_constraint import (
    is_session_explicit_rule,
    register_explicit_constraint,
)
from agent_harness.session.store import JsonlSessionStore


def _session(tmp_path) -> Session:
    return Session.start(JsonlSessionStore(tmp_path), session_id="s-1323")


def _constraints(session: Session):
    return [
        fact for fact in derive_protected_facts(session.events)
        if fact.type == "constraint" and fact.status == "active"
    ]


def test_rule_signal_is_session_scoped_only() -> None:
    assert is_session_explicit_rule({}) is False
    assert is_session_explicit_rule({"remember_as_procedural_rule": False}) is False
    assert is_session_explicit_rule({"remember_as_procedural_rule": True}) is True
    # 会话级注解（API 可显式带）不改变结论。
    assert is_session_explicit_rule(
        {
            "remember_as_procedural_rule": True,
            "protected_facts": [{"fact_type": "constraint", "scope": "session"}],
        }
    ) is True


def test_project_or_global_annotation_is_not_a_session_rule() -> None:
    """三档都带 remember 信号（App.tsx）；非会话档不得在 SESSION 组多登记一条。"""
    for scope in ("project", "global"):
        assert is_session_explicit_rule(
            {
                "remember_as_procedural_rule": True,
                "protected_facts": [
                    {"fact_type": "constraint", "scope": scope},
                ],
            }
        ) is False


def test_registers_the_message_verbatim(tmp_path) -> None:
    session = _session(tmp_path)
    content = "合并前必须跑完所有测试；失败就停下。"
    event = session.append(
        USER_MESSAGE, {"content": content, "remember_as_procedural_rule": True},
    )

    registered = register_explicit_constraint(
        session,
        content=content,
        source_event_id=event.event_id,
        token_budget=8192,
    )

    assert registered is not None
    assert registered.type == TASK_PROTECTED_FACT
    assert registered.data["value"] == content
    facts = _constraints(session)
    assert [fact.value for fact in facts] == [content]
    assert facts[0].source_event_id == event.event_id


def test_duplicate_value_is_not_registered_twice(tmp_path) -> None:
    session = _session(tmp_path)
    content = "回复先给结论。"
    first = session.append(
        USER_MESSAGE, {"content": content, "remember_as_procedural_rule": True},
    )
    second = session.append(
        USER_MESSAGE, {"content": content, "remember_as_procedural_rule": True},
    )

    assert register_explicit_constraint(
        session, content=content, source_event_id=first.event_id, token_budget=8192,
    ) is not None
    assert register_explicit_constraint(
        session, content=content, source_event_id=second.event_id, token_budget=8192,
    ) is None
    assert [fact.value for fact in _constraints(session)] == [content]


def test_budget_exceeded_does_not_register(tmp_path) -> None:
    session = _session(tmp_path)
    content = "提交前必须跑完所有测试。"
    event = session.append(
        USER_MESSAGE, {"content": content, "remember_as_procedural_rule": True},
    )

    assert register_explicit_constraint(
        session, content=content, source_event_id=event.event_id, token_budget=0,
    ) is None
    assert _constraints(session) == []


def test_invalid_source_does_not_raise_or_register(tmp_path) -> None:
    session = _session(tmp_path)

    assert register_explicit_constraint(
        session, content="规则", source_event_id="missing-event", token_budget=8192,
    ) is None
    assert _constraints(session) == []
