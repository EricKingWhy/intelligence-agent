"""`session/derive.py` 的委派标记判据：`session_is_delegated_child`。

#1085 修回（P3-1/P3-2）：helper 从 `session/context.py`（ContextVar 管道层）
搬到 derive（"事件流派生集散地"）；委派标记判据提为共享谓词
`_has_delegation_marker`，`service._delegated_child_agent_profile` 与本函数
同源（分歧语义保留在各自调用点：响亮 raise vs fail-closed 判 True）。
"""

from __future__ import annotations

from agent_harness.session import SESSION_STARTED, USER_MESSAGE, SessionEvent
from agent_harness.session.derive import session_is_delegated_child


def _started(data: dict | None, agent_id: str | None = "coding") -> SessionEvent:
    return SessionEvent(type=SESSION_STARTED, data=data or {}, agent_id=agent_id)


def test_empty_events_is_not_delegated() -> None:
    assert session_is_delegated_child([]) is False


def test_first_event_not_started_is_not_delegated() -> None:
    events = [SessionEvent(type=USER_MESSAGE, data={"content": "hi"})]
    assert session_is_delegated_child(events) is False


def test_started_without_marker_is_not_delegated() -> None:
    assert session_is_delegated_child([_started({})]) is False


def test_started_with_marker_is_delegated() -> None:
    assert session_is_delegated_child([_started({"delegation_tree_id": "tree-1"})]) is True


def test_marker_on_later_event_does_not_count() -> None:
    # 判据只看首事件：标记出现在后面的事件上不算委派子会话。
    events = [
        _started({}),
        SessionEvent(type=USER_MESSAGE, data={"delegation_tree_id": "tree-1"}),
    ]
    assert session_is_delegated_child(events) is False


def test_unparseable_agent_id_is_still_delegated_fail_closed() -> None:
    # fail-closed：标记在但 agent_id 不可解析 → 不抛错，直接判 True。
    assert (
        session_is_delegated_child([_started({"delegation_tree_id": "tree-1"}, agent_id="bogus")])
        is True
    )
