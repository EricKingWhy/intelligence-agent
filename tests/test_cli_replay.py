"""CLI replay 命令（Phase 14 T8, #114, ADR-0017 决策 4）。

逻辑回放契约（spec 03 §6）：从已持久化事件重新派生视图，tool result 一律
冻结终态；**零副作用**——不触发模型调用、工具执行、workspace 写、新事件
（连 Session.resume 都不走，那会追加 session/resumed）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.cli import render_replay_event, replay_command
from agent_harness.cli_theme import Theme
from agent_harness.session import Session
from agent_harness.session.event import (
    AGENT_DELEGATION_FINISHED,
    AGENT_DELEGATION_STARTED,
    ARTIFACT_CREATED,
    ARTIFACT_EXTERNALIZED,
    CONTEXT_COMPACTED,
    GUARD_STUCK,
    MODEL_COMPLETED,
    MODEL_FAILED,
    MODEL_FALLBACK,
    OPERATION_RECONCILE_REQUIRED,
    RUN_FAILED,
    SESSION_FORKED,
    TOOL_CALL,
    TOOL_FAILURE_GUARD,
    TOOL_RESULT,
    USER_MESSAGE,
)
from agent_harness.session.store import JsonlSessionStore


def _session_with_tools(tmp_path: Path) -> Session:
    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id="hist")
    s.append(USER_MESSAGE, {"content": "列出文件"})
    s.append(TOOL_CALL, {"tool_call_id": "c1", "tool_name": "bash",
                         "args": {"command": "ls"}})
    s.append(TOOL_RESULT, {"tool_call_id": "c1", "content": "a.txt\nb.txt"})
    s.append(MODEL_COMPLETED, {"content": "有两个文件"})
    return s


@pytest.mark.asyncio
async def test_replay_renders_frozen_history(tmp_path: Path) -> None:
    _session_with_tools(tmp_path)
    out = await replay_command("hist", workspace_dir=str(tmp_path))
    assert "[user]" in out and "列出文件" in out
    assert "[assistant]" in out and "有两个文件" in out
    assert "bash" in out
    assert "a.txt" in out  # 冻结终态的 tool result 可见


@pytest.mark.asyncio
async def test_replay_zero_side_effects(tmp_path: Path) -> None:
    """回放后：事件一字不变、无 workspace 写、无模型调用（结构保证）。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id="hist")
    s.append(USER_MESSAGE, {"content": "列出文件"})
    before = [e.to_dict() for e in store.read_events("hist")]

    await replay_command("hist", workspace_dir=str(tmp_path))

    after = [e.to_dict() for e in store.read_events("hist")]
    assert after == before  # 连 session/resumed 都没有追加


@pytest.mark.asyncio
async def test_replay_missing_session(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不存在"):
        await replay_command("ghost", workspace_dir=str(tmp_path))


def test_render_replay_event_delegation_and_failures(tmp_path: Path) -> None:
    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id="d")
    s.append(USER_MESSAGE, {"content": "x"})
    s.append(AGENT_DELEGATION_STARTED, {"target": "coding", "task": "t",
                                        "child_session_id": "c1"})
    s.append(AGENT_DELEGATION_FINISHED, {"target": "coding", "task": "t",
                                         "child_session_id": "c1",
                                         "status": "completed", "summary": "写好了"})
    s.append(RUN_FAILED, {"reason": "identical_tool_failure_loop"})
    events = store.read_events("d")

    lines = [render_replay_event(e) for e in events]
    joined = "\n".join(line for line in lines if line)
    assert "[delegate→coding]" in joined and "c1" in joined
    assert "completed" in joined and "写好了" in joined
    assert "[run failed]" in joined and "identical_tool_failure_loop" in joined
    # session/started 等生命周期事件不渲染
    assert all("session/started" not in (line or "") for line in lines)


def test_render_replay_event_stuck_guard(tmp_path: Path) -> None:
    """`#317`：stuck 护栏的两种动作都要有行——只渲染 run/paused 会让回放看不出"纠正过"。

    `guard/stuck` 不在生命周期噪音里（它是**动作**，不是过程记录）：`replan` 是
    `run/paused.stuck.replan_count` 那一格的对账依据，`paused` 是暂停前的最后一步。
    """
    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id="st")
    s.append(GUARD_STUCK, {"level": "replan", "pattern": "stuck.tool_failure_loop",
                           "count": 3, "threshold": 3, "replan_count": 1})
    s.append(GUARD_STUCK, {"level": "paused", "pattern": "stuck.tool_failure_loop",
                           "count": 6, "threshold": 3, "replan_count": 1})
    events = store.read_events("st")[-2:]

    first, second = (render_replay_event(event) for event in events)
    assert "[stuck]" in first and "level=replan" in first
    assert "stuck.tool_failure_loop" in first and "count=3" in first
    assert "[stuck]" in second and "level=paused" in second and "count=6" in second


def test_render_replay_event_session_forked(tmp_path: Path) -> None:
    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id="fk")
    s.append(SESSION_FORKED, {"parent_session_id": "p", "fork_point_seq": 3,
                              "boundary_user_message_seq": 4})
    event = store.read_events("fk")[-1]
    line = render_replay_event(event)
    assert "[fork]" in line and "p" in line and "@3" in line
    assert "from" in line


def test_render_replay_event_tool_result_tail_preview(tmp_path: Path) -> None:
    """`#736`：回放 TOOL_RESULT 预览取尾部 5 行；hint 在保留行之前（更早的行在上方）。

    只改取行方向与截断文案：`│` 前缀与 `→ result (frozen):` 外层标签（#740 已统一为
    全英文小写）保持不变（着色归 P0-7）。
    """
    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id="tp")
    s.append(TOOL_RESULT, {"tool_call_id": "c1",
                           "content": "\n".join(f"l{i}" for i in range(1, 9))})
    event = store.read_events("tp")[-1]
    line = render_replay_event(event)
    assert line == ("  → result (frozen):\n  │ … (3 earlier lines)\n"
                    "  │ l4\n  │ l5\n  │ l6\n  │ l7\n  │ l8")


def test_render_replay_event_ignores_lifecycle(tmp_path: Path) -> None:

    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id="lc")
    s.append(USER_MESSAGE, {"content": "hi"})
    s.append(MODEL_FAILED, {"message": "boom"})
    events = store.read_events("lc")
    assert render_replay_event(events[0]) is None  # session/started
    # model/failed 渲染为失败行（replay 要如实呈现失败事实）
    assert render_replay_event(events[2]) is not None


def test_render_replay_event_labels_all_english(tmp_path: Path) -> None:
    """`#740`：replay 渲染标签统一为全英文小写；整函数输出无 CJK。

    逐个覆盖 16 个可渲染分支（不含 run/paused、run/resumed——那是 `#648` 故意的
    中文恢复指引，不在本票范围）。全函数输出无 CJK 的断言只针对输出字符串，
    docstring / 注释里的中文豁免。
    """
    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id="labels")
    s.append(USER_MESSAGE, {"content": "hi"})
    s.append(MODEL_COMPLETED, {"content": "hello"})
    s.append(TOOL_CALL, {"tool_call_id": "c1", "tool_name": "bash",
                         "args": {"command": "ls"}})
    s.append(TOOL_RESULT, {"tool_call_id": "c1", "content": "out"})
    s.append(RUN_FAILED, {"reason": "boom"})
    s.append(MODEL_FAILED, {"message": "model boom"})
    s.append(TOOL_FAILURE_GUARD, {"level": "warn", "consecutive_failures": 3})
    s.append(GUARD_STUCK, {"level": "replan", "pattern": "stuck.tool_failure_loop",
                           "count": 3, "threshold": 3, "replan_count": 1})
    s.append(MODEL_FALLBACK, {"from_model": "a", "to_model": "b", "reason": "busy"})
    s.append(AGENT_DELEGATION_STARTED, {"target": "coding", "task": "t",
                                        "child_session_id": "c1"})
    s.append(AGENT_DELEGATION_FINISHED, {"target": "coding", "task": "t",
                                         "child_session_id": "c1",
                                         "status": "completed", "summary": "done"})
    s.append(ARTIFACT_CREATED, {"artifact_id": "a1"})
    s.append(ARTIFACT_EXTERNALIZED, {"artifact_id": "a2", "size": 10})
    s.append(SESSION_FORKED, {"parent_session_id": "p", "fork_point_seq": 3,
                              "boundary_user_message_seq": 4})
    s.append(CONTEXT_COMPACTED, {})
    s.append(OPERATION_RECONCILE_REQUIRED, {"op": "x"})
    events = store.read_events("labels")

    lines = [render_replay_event(e) for e in events]
    joined = "\n".join(line for line in lines if line)
    for label in (
        "[user]",
        "[assistant]",
        "[tool]",
        "→ result (frozen):",
        "[run failed]",
        "[model failed]",
        "[guard]",
        "[stuck]",
        "[fallback]",
        "[delegate→coding]",
        "[delegate done→coding]",
        "[artifact]",
        "[artifact externalized]",
        "[fork] from",
        "[context compacted]",
        "[reconcile required]",
    ):
        assert label in joined, f"missing label: {label}"

    # `[assistant]` 内容为空时返回 None 的分支一并覆盖
    empty = Session.start(store, session_id="empty")
    empty.append(MODEL_COMPLETED, {"content": ""})
    assert render_replay_event(store.read_events("empty")[-1]) is None

    # 全函数输出无 CJK（注释里的中文不管——只断言输出字符串）
    assert not any(
        "\u4e00" <= ch <= "\u9fff" for line in lines if line for ch in line
    )


def test_render_replay_event_failure_coloring(tmp_path: Path) -> None:
    """P0-7（AC7）：失败三行（run/failed、model/failed、guard）可着 err 色；只着色不改文本。

    `theme=None` ⇒ 纯文本（旧调用方零行为变化）；真彩含 err 转义 `215;95;95`；
    `[stuck]` / `[fallback]` 不染（非失败语义，#317/#312 纪律）。
    """
    store = JsonlSessionStore(root=tmp_path / "sessions")
    s = Session.start(store, session_id="fc")
    s.append(RUN_FAILED, {"reason": "cancelled"})
    s.append(MODEL_FAILED, {"message": "boom"})
    s.append(TOOL_FAILURE_GUARD, {"level": "hard", "consecutive_failures": 3})
    run_failed, model_failed, guard = store.read_events("fc")[-3:]

    # theme=None ⇒ 纯文本，零 ANSI（旧行为）
    assert render_replay_event(run_failed) == "[run failed] cancelled"
    assert "\x1b[" not in (render_replay_event(model_failed) or "")

    err = "\x1b[38;2;215;95;95m"
    theme = Theme(color="truecolor")
    assert render_replay_event(run_failed, theme=theme) == err + "[run failed] cancelled\x1b[0m"
    assert render_replay_event(model_failed, theme=theme).startswith(err)
    assert render_replay_event(guard, theme=theme).startswith(err)

    # nocolor ⇒ 原样（只着色，文本不变）
    plain = Theme(color="nocolor")
    assert render_replay_event(run_failed, theme=plain) == "[run failed] cancelled"
    assert render_replay_event(guard, theme=plain).startswith("[guard] level=hard")
