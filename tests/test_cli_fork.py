"""CLI fork 命令（Phase 14 T5, #111, ADR-0017 决策 6/10）。

全链串联：boundary 校验 + seed + session/forked + meta（T2）→ copy-on-fork
（T3）→ tail summary 可选（T4，--no-summary 关）。fork 是 CLI 动作，模型
不可见——不注册任何工具。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.cli import _parse_fork_args, fork_command
from agent_harness.session import Session
from agent_harness.session.event import USER_MESSAGE
from agent_harness.session.store import JsonlSessionStore


def _prepare(tmp_path: Path) -> JsonlSessionStore:
    store = JsonlSessionStore(root=tmp_path / "sessions")
    parent = Session.start(store, session_id="parent")
    parent.append(USER_MESSAGE, {"content": "第一条"})
    parent.append(USER_MESSAGE, {"content": "第二条"})
    return store


@pytest.mark.asyncio
async def test_fork_command_end_to_end(tmp_path: Path) -> None:
    store = _prepare(tmp_path)
    child_id = await fork_command(
        "parent", from_message=2, no_summary=True,
        workspace_dir=str(tmp_path), write=lambda _line: None,
    )
    # child JSONL 真实落盘且可 resume（resume 自身会追加 session/resumed）
    resumed = Session.resume(store, child_id)
    types = [e.type for e in resumed.events]
    assert "session/forked" in types
    assert resumed.events[-1].type == "session/resumed"
    # 输出指向 child（resume 提示里带 id）
    # —— child_id 即返回值，resume 成功本身就是端到端证明


@pytest.mark.asyncio
async def test_fork_command_copies_workspace(tmp_path: Path) -> None:
    from agent_harness.sandbox import WorkspaceRegistry

    registry = WorkspaceRegistry(root=tmp_path)
    store = JsonlSessionStore(root=tmp_path / "sessions")
    parent = Session.start(
        store, session_id="wsparent", workspace_registry=registry
    )
    parent.sandbox.write_text("hello.txt", "world")
    parent.append(USER_MESSAGE, {"content": "分叉点"})

    child_id = await fork_command(
        "wsparent", from_message=parent.events[-1].seq, no_summary=True,
        workspace_dir=str(tmp_path), write=lambda _line: None,
    )
    child_registry = WorkspaceRegistry(root=tmp_path)
    child_sandbox = child_registry.get(child_id)
    assert child_sandbox.read_text("hello.txt") == "world"


@pytest.mark.asyncio
async def test_fork_command_bad_boundary_raises(tmp_path: Path) -> None:
    """命令核心抛领域错误；SystemExit 转换在 _main_fork（CLI 边界）。"""
    _prepare(tmp_path)
    from agent_harness.session.fork import ForkBoundaryError

    with pytest.raises(ForkBoundaryError):
        await fork_command(
            "parent", from_message=999, no_summary=True,
            workspace_dir=str(tmp_path), write=lambda _line: None,
        )


@pytest.mark.asyncio
async def test_fork_command_child_inherits_parent_model(tmp_path: Path) -> None:
    """T7 #137：CLI fork 的 child 继承父当前模型（与 Web / demo 同语义）。"""
    from agent_harness.session.service import current_model_selection

    store = JsonlSessionStore(root=tmp_path / "sessions")
    parent = Session.start(
        store, session_id="parent",
        started_data={"provider": "deepseek", "model_id": "gpt-4o"},
    )
    parent.append(USER_MESSAGE, {"content": "第一条"})
    parent.append(USER_MESSAGE, {"content": "第二条"})

    child_id = await fork_command(
        "parent", from_message=2, no_summary=True,
        workspace_dir=str(tmp_path), write=lambda _line: None,
    )

    assert current_model_selection(
        store.read_events(child_id)
    ) == ("deepseek", "gpt-4o")


# ── #424：--from-message 承诺的是**序数**（第 N 条用户消息）───────────
#
# 历史行为把 N 当事件 seq 直传 fork_session。纯消息会话里二者恰好重合
# （session/started 占 seq 0，用户消息 seq 1,2,…——ordinal N == seq N），
# 掩盖了语义错位；真实会话里有 run/model/tool 事件插在消息之间，seq 与
# 序数立刻分叉：要么报错只给 seq 清单，要么 fork 到错误的点。


def _prepare_with_gap(tmp_path: Path) -> JsonlSessionStore:
    """started(0) + user#1(1) + model/completed(2) + user#2(3)：序数 ≠ seq。"""
    from agent_harness.session.event import MODEL_COMPLETED

    store = JsonlSessionStore(root=tmp_path / "sessions")
    parent = Session.start(store, session_id="parent")
    parent.append(USER_MESSAGE, {"content": "第一条"})
    parent.append(MODEL_COMPLETED, {"content": "ok"})
    parent.append(USER_MESSAGE, {"content": "第二条"})
    return store


@pytest.mark.asyncio
async def test_from_message_is_ordinal_not_seq(tmp_path: Path) -> None:
    """--from-message 2 = 第 2 条用户消息（seq 3），不是事件 seq 2。

    seq 2 是 model/completed——旧语义在此直接抛 ForkBoundaryError，
    或（消息恰在 seq N 时）静默 fork 到错误的点。
    """
    store = _prepare_with_gap(tmp_path)

    child_id = await fork_command(
        "parent", from_message=2, no_summary=True,
        workspace_dir=str(tmp_path), write=lambda _line: None,
    )

    seed_user_messages = [
        e.data.get("content") for e in store.read_events(child_id)
        if e.type == USER_MESSAGE
    ]
    assert seed_user_messages == ["第一条"], (
        "序数 2 应锚定第二条用户消息（其本身不进 seed），seed 只留第一条"
    )


@pytest.mark.asyncio
async def test_from_message_out_of_range_error_is_dual_annotated(
    tmp_path: Path,
) -> None:
    """越界报错同时给序数范围与底层 seq 清单（用户按序数提问，按 seq 对账）。"""
    _prepare_with_gap(tmp_path)
    from agent_harness.session.fork import ForkBoundaryError

    with pytest.raises(ForkBoundaryError) as exc_info:
        await fork_command(
            "parent", from_message=3, no_summary=True,
            workspace_dir=str(tmp_path), write=lambda _line: None,
        )
    message = str(exc_info.value)
    assert "3" in message, message
    assert "[1, 3]" in message, f"错误信息应列出合法锚点 seq 清单：{message}"


# ── #445：不存在 / 空日志与序数越界分开报 ────────────────────────────
#
# JsonlSessionStore.read_events 对不存在的会话返回 []（不抛 SessionNotFound），
# 解析器先于 fork_session 运行——原「Session 'x' 不存在或事件日志为空」措辞
# 不可达，缺会话被误报成「--from-message 超出范围」。两类不可分叉事实
# （会话不在 / 一条事件都没有）都该按本来面目报，而不是伪装成越界。


@pytest.mark.asyncio
async def test_fork_command_missing_session_reports_not_found(
    tmp_path: Path,
) -> None:
    """不存在的会话 id → 「不存在或事件日志为空」，不是序数越界。"""
    from agent_harness.session.fork import ForkBoundaryError

    with pytest.raises(ForkBoundaryError, match="不存在或事件日志为空"):
        await fork_command(
            "nope", from_message=1, no_summary=True,
            workspace_dir=str(tmp_path), write=lambda _line: None,
        )


@pytest.mark.asyncio
async def test_fork_command_empty_session_same_message(
    tmp_path: Path,
) -> None:
    """存在但 0 事件的会话与不存在同一措辞（同一不可分叉事实）。"""
    from agent_harness.session.fork import ForkBoundaryError

    empty_log = tmp_path / "sessions" / "empty" / "events.jsonl"
    empty_log.parent.mkdir(parents=True)
    empty_log.touch()

    with pytest.raises(ForkBoundaryError, match="不存在或事件日志为空"):
        await fork_command(
            "empty", from_message=1, no_summary=True,
            workspace_dir=str(tmp_path), write=lambda _line: None,
        )


def test_parse_fork_args() -> None:
    args = _parse_fork_args(["sess-1", "--from-message", "3"])
    assert (args.session_id, args.from_message, args.no_summary) == (
        "sess-1", 3, False
    )
    args = _parse_fork_args(
        ["sess-1", "--from-message", "3", "--no-summary"]
    )
    assert args.no_summary is True
    with pytest.raises(SystemExit):
        _parse_fork_args(["sess-1"])  # --from-message 必填
