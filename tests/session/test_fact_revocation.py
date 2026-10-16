"""#1190：受保护事实的**通用主动撤销**（`task/fact_revocation`）投影语义。

覆盖：
- 撤销把 active fact 置 `status="revoked"`（区别于被动 supersede 的 `"superseded"`），
  并从撤销事件派生 `revoked_at` / `revoked_by`；`superseded_by_fact_id` 指向撤销身份；
- 被撤销 fact 不再进注入体（`serialize_protected_facts` 只投影 active）；
- 引用未知 fact 的撤销事件被忽略（坏数据只损失该条，不崩）；
- 主动撤销优先于被动取代（同一 fact 两者都命中时）。
"""

from __future__ import annotations

import pytest

from agent_harness.session import (
    TASK_FACT_REVOCATION,
    USER_MESSAGE,
    Session,
)
from agent_harness.session.derive import (
    _fact_revocation_id,
    derive_protected_facts,
    serialize_protected_facts,
)
from agent_harness.session.fork import fork_session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage.sqlite import SqliteSessionMetaStore


def _session(tmp_path, session_id="s-1190"):
    return Session.start(JsonlSessionStore(tmp_path), session_id=session_id)


def _register_constraint(session, content, value=None):
    event = session.append(USER_MESSAGE, {"content": content})
    fact = session.register_protected_fact(
        fact_type="constraint",
        value=value or content,
        source_event_id=event.event_id,
    )
    return event, fact.data["fact_id"]


def test_active_fact_not_revoked_by_default(tmp_path):
    session = _session(tmp_path)
    _, fact_id = _register_constraint(session, "回复代码审查意见时先给结论。")
    fact = next(f for f in derive_protected_facts(session.events) if f.fact_id == fact_id)
    assert fact.status == "active"
    assert fact.revoked_at is None
    assert fact.revoked_by is None


def test_fact_revocation_event_marks_fact_revoked(tmp_path):
    session = _session(tmp_path)
    _, fact_id = _register_constraint(session, "回复代码审查意见时先给结论。")

    revoke_event = session.append(
        TASK_FACT_REVOCATION,
        {
            "revoke_fact_id": fact_id,
            "revoked_by": "local",
            "revoked_at": "2026-10-16T03:00:00.000+00:00",
        },
    )

    fact = next(f for f in derive_protected_facts(session.events) if f.fact_id == fact_id)
    assert fact.status == "revoked"
    assert fact.revoked_by == "local"
    assert fact.revoked_at == "2026-10-16T03:00:00.000+00:00"
    # superseded_by 指向撤销身份（沿用 derive 现有 supersede 链机制）
    assert fact.superseded_by_fact_id == _fact_revocation_id(revoke_event.event_id)
    # 状态值域与被动取代区分
    assert fact.status != "superseded"


def test_revoked_at_falls_back_to_event_time(tmp_path):
    session = _session(tmp_path)
    _, fact_id = _register_constraint(session, "别在周末推送早报。")
    revoke_event = session.append(
        TASK_FACT_REVOCATION, {"revoke_fact_id": fact_id, "revoked_by": "local"}
    )
    fact = next(f for f in derive_protected_facts(session.events) if f.fact_id == fact_id)
    assert fact.status == "revoked"
    assert fact.revoked_at == revoke_event.time


def test_revoked_fact_is_not_serialized_for_injection(tmp_path):
    session = _session(tmp_path)
    _, fact_id = _register_constraint(session, "回复代码审查意见时先给结论。")
    session.append(
        TASK_FACT_REVOCATION, {"revoke_fact_id": fact_id, "revoked_by": "local"}
    )
    facts = derive_protected_facts(session.events)
    serialized = serialize_protected_facts(facts)
    assert fact_id not in serialized


def test_revocation_of_unknown_fact_is_ignored(tmp_path):
    session = _session(tmp_path)
    _, fact_id = _register_constraint(session, "回复代码审查意见时先给结论。")
    session.append(
        TASK_FACT_REVOCATION, {"revoke_fact_id": "pf-does-not-exist", "revoked_by": "local"}
    )
    fact = next(f for f in derive_protected_facts(session.events) if f.fact_id == fact_id)
    assert fact.status == "active"


def test_revocation_of_superseded_fact_wins_over_supersede(tmp_path):
    session = _session(tmp_path)
    ev1 = session.append(USER_MESSAGE, {"content": "用 Python 写。"})
    first = session.register_protected_fact(
        fact_type="constraint", value="用 Python 写。", source_event_id=ev1.event_id,
    )
    first_id = first.data["fact_id"]
    # 同类型新事实显式取代旧事实（被动 supersede）
    ev2 = session.append(USER_MESSAGE, {"content": "改用 TypeScript 写。"})
    session.register_protected_fact(
        fact_type="constraint",
        value="改用 TypeScript 写。",
        source_event_id=ev2.event_id,
        supersedes_fact_id=first_id,
    )
    first_fact = next(f for f in derive_protected_facts(session.events) if f.fact_id == first_id)
    assert first_fact.status == "superseded"

    # 再对该 fact 主动撤销：撤销优先
    session.append(
        TASK_FACT_REVOCATION, {"revoke_fact_id": first_id, "revoked_by": "local"}
    )
    first_fact = next(
        f for f in derive_protected_facts(session.events) if f.fact_id == first_id
    )
    assert first_fact.status == "revoked"


@pytest.mark.asyncio
async def test_fork_descendant_does_not_inject_revoked_fact(tmp_path):
    """AC2：撤销在该会话及其 fork 后代新开会话都不再注入（抽查注入序列化输出）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    meta = SqliteSessionMetaStore(tmp_path / "meta.db")
    await meta.initialize()
    parent = Session.start(store, session_id="parent")
    goal = "完成 W-02，精确保留单号 ORD-84721。"
    goal_event = parent.append(USER_MESSAGE, {"content": goal})
    fact = parent.register_protected_fact(
        fact_type="constraint", value=goal, source_event_id=goal_event.event_id,
    )
    fact_id = fact.data["fact_id"]
    parent.append(
        TASK_FACT_REVOCATION, {"revoke_fact_id": fact_id, "revoked_by": "local"}
    )
    boundary = parent.append(USER_MESSAGE, {"content": "随后完成 W-03。"})

    # 父会话注入体不含该 fact
    assert fact_id not in serialize_protected_facts(derive_protected_facts(parent.events))

    child = await fork_session(
        store,
        meta,
        parent.session_id,
        boundary_user_message_seq=boundary.seq,
        child_session_id="child",
    )
    child_facts = derive_protected_facts(child.events)
    child_fact = next(f for f in child_facts if f.fact_id == fact_id)
    assert child_fact.status == "revoked"
    assert fact_id not in serialize_protected_facts(child_facts)
