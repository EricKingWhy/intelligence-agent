"""#1359 阶段 0：目标事实源收敛（读侧三处同源）。

`derive_protected_facts`（user_goal fact）/ compactor §0 / progress.md 都以
`effective_goal` 为准；`clear` 墓碑 = 明确"无目标"，不回落旧文本。没有目标事件
的存量会话保持原口径（首条用户消息 / `task_text`）。
"""

from __future__ import annotations

from agent_harness.context.compactor import _current_goal_body
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.derive import (
    PROGRESS_CONFIRM_ORIGIN,
    derive_messages,
    derive_protected_facts,
)
from agent_harness.session.goal import append_goal_change
from agent_harness.session.progress import (
    derive_progress_document,
    render_progress_brief_lines,
    render_progress_markdown,
)
from agent_harness.session.task import apply_task_definition

_GOAL = "## Objective\n修登录页\n\n## Success criteria\n跳转成功\n\n## Verification\npytest tests/auth\n"


def _session(tmp_path) -> Session:
    return Session.start(JsonlSessionStore(root=tmp_path), cwd=str(tmp_path))


def _active_goals(session: Session):
    return [
        fact for fact in derive_protected_facts(session.events)
        if fact.type == "user_goal" and fact.status == "active"
    ]


def test_goal_event_replaces_first_message_as_goal_source(tmp_path) -> None:
    session = _session(tmp_path)
    session.append("user/message", {"content": "旧目标：随便看看"})
    created = append_goal_change(session, "create", objective=_GOAL)
    assert created.ok, created.reason

    goals = _active_goals(session)
    assert len(goals) == 1
    assert goals[0].value == _GOAL.strip()
    assert goals[0].source_event_id == session.events[-1].event_id
    # 旧 fact 不迁移：首条消息那条 source 不再 active（换源 = 换 fact id）。
    assert all(fact.source_event_id != session.events[0].event_id for fact in goals)


def test_clear_tombstone_means_no_goal_not_old_text(tmp_path) -> None:
    session = _session(tmp_path)
    session.append("user/message", {"content": "旧目标：随便看看"})
    append_goal_change(session, "create", objective=_GOAL)
    append_goal_change(session, "clear")

    assert _active_goals(session) == []
    assert _current_goal_body(session.events, derive_protected_facts(session.events)) == "(none)"
    doc = derive_progress_document(session.events, session_id=session.session_id)
    assert doc.goal is None


def test_three_readers_agree_on_goal_objective(tmp_path) -> None:
    """AC4：compactor §0 / progress.md / `/goal` 读同一份 objective。"""
    session = _session(tmp_path)
    session.append("user/message", {"content": "旧目标"})
    append_goal_change(session, "create", objective=_GOAL)

    facts = derive_protected_facts(session.events)
    section = _current_goal_body(session.events, facts)
    assert section.startswith('"## Objective')
    assert "修登录页" in section
    doc = derive_progress_document(session.events, session_id=session.session_id)
    assert doc.goal == _GOAL.strip()


def test_legacy_sessions_still_fall_back(tmp_path) -> None:
    """没有 goal/changed 的存量会话：derive 取首条消息，progress 取 task_text。"""
    session = _session(tmp_path)
    session.append("user/message", {"content": "旧目标：随便看看"})
    apply_task_definition(session, "结构化任务目标")

    goals = _active_goals(session)
    assert len(goals) == 1
    assert goals[0].value == "旧目标：随便看看"
    doc = derive_progress_document(session.events, session_id=session.session_id)
    assert doc.goal == "结构化任务目标"


def test_active_goal_keeps_w06_edits_visible_but_out_of_user_goal(tmp_path) -> None:
    """P2-4：目标权威下的 W-06 取舍——外部编辑不进 user_goal，但仍可见。

    没有 goal 时，确认后的外部编辑以 user_goal 身份进投影（W-06 原行为）；
    有 active goal 后只认 goal。**不变量**：确认事件没有消失（事件流 + 模型
    历史），消失的只是"它也算目标"这条派生。
    """
    confirmed = "外部手改：迁移脚本先备份"
    legacy = _session(tmp_path)
    legacy.append("user/message", {"content": "旧目标：随便看看"})
    legacy.append(
        "user/message", {"content": confirmed, "origin": PROGRESS_CONFIRM_ORIGIN},
    )
    values = {fact.value for fact in _active_goals(legacy)}
    assert values == {"旧目标：随便看看", confirmed}

    session = _session(tmp_path / "with-goal")
    session.append("user/message", {"content": "旧目标：随便看看"})
    append_goal_change(session, "create", objective=_GOAL)
    session.append(
        "user/message", {"content": confirmed, "origin": PROGRESS_CONFIRM_ORIGIN},
    )

    assert [fact.value for fact in _active_goals(session)] == [_GOAL.strip()]
    doc = derive_progress_document(session.events, session_id=session.session_id)
    assert doc.goal == _GOAL.strip()
    # 可见性：确认事件仍在流里，仍进模型历史（模型没瞎，只是它不再改写目标）。
    assert any(confirmed in e.data.get("content", "") for e in session.events)
    assert any(
        confirmed in str(getattr(message, "content", ""))
        for message in derive_messages(session.events)
    )


def test_brief_injection_bounds_a_huge_goal_but_markdown_keeps_it_whole(tmp_path) -> None:
    """P2-5：完整保存 ≠ 完整注入——模型上下文按 2000 字符口径截断并留回读指针。"""
    session = _session(tmp_path)
    huge = "## Objective\n" + "修登录页." * 700  # ≈2800 字符
    created = append_goal_change(session, "create", objective=huge)
    assert created.ok, created.reason

    doc = derive_progress_document(session.events, session_id=session.session_id)
    brief = "\n".join(render_progress_brief_lines(doc))
    assert "…［已截断：" in brief
    assert f"source_event_id={doc.goal_source_event_id}" in brief
    assert len(brief) < len(huge)
    # progress.md 正文仍是全文。
    markdown = render_progress_markdown(doc, generated_at="2026-10-20T00:00:00Z")
    assert huge.strip() in markdown
