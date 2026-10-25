"""#1493 CLI 入口（`agent-harness memory identity ...`）：盘点/逐项批准/裁决/切换。

与 `test_1493_identity_mapping.py` 同一批夹具，单独成文件是因为 `_main_memory`
自己 `asyncio.run`（真实进程就是这样），在模块级 `pytest.mark.asyncio` 的用例里
调用会直接撞 "cannot be called from a running event loop"。
"""

from __future__ import annotations

import asyncio

from agent_harness.config import Settings
from agent_harness.memory.v2.identity_mapping import (
    MANIFEST_NAME,
    load_manifest,
)
from tests.memory.v2.test_1493_identity_mapping import (
    _SHARED_TEXT,
    _entries,
    _items,
    _seed,
)


def _memory_cli(monkeypatch, settings, argv: list[str], capsys) -> tuple[int, str]:
    """跑真实 `memory` 子命令入口；返回 `(退出码, 输出)`。"""
    from agent_harness import cli

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "setup_logging", lambda *args, **kwargs: None)
    code = 0
    try:
        cli._main_memory(argv)
    except SystemExit as exit_request:
        code = int(exit_request.code or 0)
    return code, capsys.readouterr().out


def test_cli_plan_is_read_only_and_apply_requires_every_item(tmp_path, monkeypatch, capsys) -> None:
    """plan 只盘点（不建 memories/）；未逐项批准时 apply 退 2 且零落盘；批完才写。"""
    workspace, database = asyncio.run(_seed(tmp_path))
    assert database.name == "memory-v2.db"
    settings = Settings(_env_file=None, workspace_dir=str(workspace), model_api_key="sk-test")

    code, out = _memory_cli(monkeypatch, settings, ["identity", "plan", "--conflict", "m6,m7"], capsys)
    assert code == 0, out
    assert "待逐项批准" in out, out
    assert not (workspace / "memories").exists(), "plan 不得建 memories/ 目录（只读盘点）"

    code, out = _memory_cli(monkeypatch, settings, ["identity", "apply"], capsys)
    assert code == 2, out
    assert "未获批" in out, out
    assert not (workspace / "memories").exists(), "被阻断时零落盘"

    manifest = load_manifest(workspace / MANIFEST_NAME)
    for item in _items(manifest, disposition="map"):
        code, out = _memory_cli(monkeypatch, settings, ["identity", "approve", item["id"]], capsys)
        assert code == 0, out
    for item in _items(manifest, kind="entry_conflict"):
        code, out = _memory_cli(
            monkeypatch, settings, ["identity", "resolve", item["id"], "--merge", "m7"], capsys)
        assert code == 0, out

    code, out = _memory_cli(monkeypatch, settings, ["identity", "list"], capsys)
    assert code == 0 and "entry:memory:" in out, out

    code, out = _memory_cli(monkeypatch, settings, ["identity", "apply"], capsys)
    assert code == 0, out
    assert "旧数据销毁：not_authorized" in out, out
    assert _SHARED_TEXT in _entries(workspace / "memories" / "MEMORY.md")


def test_cli_rejects_a_declared_conflict_without_a_merge_source(tmp_path, monkeypatch, capsys) -> None:
    """裁决必须指名赢家：不给 --merge 时退 2 并说清要什么，不替人默认。"""
    workspace, _ = asyncio.run(_seed(tmp_path))
    settings = Settings(_env_file=None, workspace_dir=str(workspace), model_api_key="sk-test")
    assert _memory_cli(
        monkeypatch, settings, ["identity", "plan", "--conflict", "m6,m7"], capsys)[0] == 0

    manifest = load_manifest(workspace / MANIFEST_NAME)
    conflict = _items(manifest, kind="entry_conflict")[0]
    code, out = _memory_cli(
        monkeypatch, settings, ["identity", "resolve", conflict["id"]], capsys)
    assert code == 2, out
    assert "--merge" in out, out
    assert _items(load_manifest(workspace / MANIFEST_NAME), kind="entry_conflict")[0][
        "disposition"] == "conflict", "被拒的裁决不得改动清单"


def test_cli_rejects_a_budget_flag_above_the_deployment_setting(tmp_path, monkeypatch, capsys) -> None:
    """`--memory-limit` 高于 `Settings.memory_char_limit` 时退 2，且不落任何清单。

    复核 probe26 的形状：Settings 默认 2200，flag 抬到 22000 后 apply 写出 3007 字的
    MEMORY.md 且 `skipped=[]`——运行时读不进来的文件被静默产出。
    """
    workspace, _ = asyncio.run(_seed(tmp_path))
    settings = Settings(_env_file=None, workspace_dir=str(workspace), model_api_key="sk-test")
    assert settings.memory_char_limit == 2200, settings.memory_char_limit

    code, out = _memory_cli(
        monkeypatch, settings, ["identity", "plan", "--memory-limit", "22000"], capsys)
    assert code == 2, out
    assert "不得扩大预算" in out, out
    assert not (workspace / MANIFEST_NAME).exists(), "被拒的 plan 不得留下清单"


def test_cli_accepts_a_lower_budget_flag_and_records_the_deployment_ceiling(
    tmp_path, monkeypatch, capsys,
) -> None:
    """压低预算合法；清单同时记下部署上限（apply 靠它封顶，挡住手改）。"""
    workspace, _ = asyncio.run(_seed(tmp_path))
    settings = Settings(_env_file=None, workspace_dir=str(workspace), model_api_key="sk-test")
    code, out = _memory_cli(
        monkeypatch, settings, ["identity", "plan", "--memory-limit", "900"], capsys)
    assert code == 0, out
    manifest = load_manifest(workspace / MANIFEST_NAME)
    assert manifest["limits"]["memory"] == 900
    assert manifest["deployment_limits"]["memory"] == settings.memory_char_limit
    assert manifest["deployment_limits"]["user"] == settings.user_char_limit
