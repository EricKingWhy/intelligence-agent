"""#616 CLI 面：`agent-harness budgets clear-stale-tools --session <id> [--tool X] [--yes]`。

黑盒：经 `cli._main_dispatch()` + monkeypatch `sys.argv` 驱动，不 import 服务/存储内部符号；
只读 `harness.db` 与 `workspace/sessions/<id>/events.jsonl` 验证零改动 / 真执行。

**本文件钉的命令契约（#616 第一轮，用户已按推荐拍板）**：

- `budgets clear-stale-tools --session <id>`（**整会话重整**）：无 `--yes` 时**只打印将清名单**
  （陈旧账行名）并**拒绝执行**——`SystemExit` 非零、账行零改动、不落事件；
- `budgets clear-stale-tools --session <id> --tool X`（**单名收窄**）：无需 `--yes` 即执行
  （服务端仍按根 registry 判 stale，点名正常/未知名是安全 no-op）；
- `budgets clear-stale-tools --session <id> --yes`：整会话重整真执行。

CLI 是同一个 `SessionService` 方法的瘦客户端（ADR-0045 D8）：判定与落盘在服务/存储层，
本层只负责确认面与人类可读输出。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_harness import cli
from agent_harness.config import Settings
from tests.scripted_model import ScriptedModel

_REGISTERED = "glob"
_STALE = "ghost-tool"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        model_api_key="sk-test",
        workspace_dir=str(tmp_path / "workspace"),
        _env_file=None,
    )


def _sessions_root(settings: Settings) -> Path:
    return Path(settings.workspace_dir) / "sessions"


def _only_session_id(settings: Settings) -> str:
    ids = [p.parent.name for p in _sessions_root(settings).glob("*/events.jsonl")]
    assert len(ids) == 1, f"应有且仅有一个会话，实际 {ids}"
    return ids[0]


def _seed(settings: Settings, session_id: str, limits: dict[str, int], **maps) -> None:
    db = Path(settings.workspace_dir) / "harness.db"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO session_budgets (budget_key, root_session_id) VALUES (?, ?)",
            (session_id, session_id),
        )
        connection.execute(
            "UPDATE session_budgets SET tool_call_limits = ?, tool_calls_by_tool = ?, "
            "tool_attempts_by_tool = ? WHERE budget_key = ?",
            (
                json.dumps(limits, sort_keys=True),
                json.dumps(maps.get("calls", {}), sort_keys=True),
                json.dumps(maps.get("attempts", {}), sort_keys=True),
                session_id,
            ),
        )
        connection.commit()


def _state(settings: Settings, session_id: str) -> dict:
    db = Path(settings.workspace_dir) / "harness.db"
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM session_budgets WHERE budget_key = ?", (session_id,)
        ).fetchone()
        events = connection.execute(
            "SELECT kind, version FROM session_budget_events WHERE budget_key = ? "
            "ORDER BY rowid",
            (session_id,),
        ).fetchall()
    assert row is not None
    return {
        "limits": json.loads(row["tool_call_limits"]),
        "version": int(row["version"]),
        "events": [tuple(e) for e in events],
    }


def _make_session(settings: Settings) -> str:
    """用替身模型跑出一个真实完成的会话（events.jsonl + harness.db 由 CLI 装配建出）。"""
    asyncio.run(cli.run("建立一个会话", write=lambda _t: None))
    return _only_session_id(settings)


def _run_cli(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["agent-harness", *argv])
    cli._main_dispatch()


@pytest.fixture()
def _prepared(monkeypatch, tmp_path):
    """公共前置：settings 注入 + 替身模型 + 一个已建会话 + 预置陈旧账行。"""
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[AIMessage(content="ok")]),
    )
    session_id = _make_session(settings)
    _seed(settings, session_id, {_STALE: 5, _REGISTERED: 9})
    return settings, session_id


# ── 整会话重整：无 --yes ⇒ 打印将清名单 + 拒绝执行 + 零改动 ──────────────


def test_whole_session_without_yes_prints_and_refuses(
    _prepared, monkeypatch, capsys
):
    settings, session_id = _prepared
    before = _state(settings, session_id)

    with pytest.raises(SystemExit) as excinfo:
        _run_cli(monkeypatch, ["budgets", "clear-stale-tools", "--session", session_id])

    assert excinfo.value.code not in (0, None), "无 --yes 的整会话重整必须拒绝执行"
    captured = capsys.readouterr()
    assert _STALE in (captured.out + captured.err), "必须打印**将清名单**（陈旧名）"

    after = _state(settings, session_id)
    assert after == before, "未确认 ⇒ 账行、version、审计事件一个字节都不许改"


# ── 单名收窄：无需 --yes 直接执行 ───────────────────────────────────────


def test_tool_narrowing_executes_without_yes(_prepared, monkeypatch):
    settings, session_id = _prepared
    before_version = _state(settings, session_id)["version"]

    try:
        _run_cli(
            monkeypatch,
            ["budgets", "clear-stale-tools", "--session", session_id, "--tool", _STALE],
        )
    except SystemExit as exc:  # 成功路径允许显式 exit 0
        assert exc.code in (0, None), f"单名清除不该被拒绝：exit={exc.code}"

    state = _state(settings, session_id)
    assert state["limits"] == {_REGISTERED: 9}, "点名陈旧名被清、正常名保留"
    assert state["version"] == before_version + 1
    assert state["events"][-1][0] == "tool_limits_purged"


# ── 整会话重整 + --yes：真执行 ──────────────────────────────────────────


def test_whole_session_with_yes_executes(_prepared, monkeypatch):
    settings, session_id = _prepared
    before_version = _state(settings, session_id)["version"]

    try:
        _run_cli(
            monkeypatch,
            ["budgets", "clear-stale-tools", "--session", session_id, "--yes"],
        )
    except SystemExit as exc:
        assert exc.code in (0, None), f"带 --yes 的整会话重整不该被拒绝：exit={exc.code}"

    state = _state(settings, session_id)
    assert state["limits"] == {_REGISTERED: 9}
    assert state["version"] == before_version + 1
    assert state["events"][-1][0] == "tool_limits_purged"
