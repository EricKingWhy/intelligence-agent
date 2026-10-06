"""#684 Phase 2 CLI 面：`agent-harness approvals policy list|remove <id> [--yes]`。

黑盒：经 `cli._main_dispatch()` + monkeypatch `sys.argv` 驱动（同 #616 的
`tests/test_cli_purge_stale_tools.py` 形态），只读 `.agent-harness/approve-policy.json`
验证**零改动 / 真删除**，不 import 存储内部符号。

项目根 = 进程当前工作目录（与 `ToolExecutor.__init__` 的 `project_root` 回落口径逐字一致
——管理面与执行域读写同一份文件）；测试用 `monkeypatch.chdir(tmp_path)` 把它钉到临时目录。

钉住的命令契约：

- `approvals policy list`：列出 id/tool/key/granularity/created_at；无规则时友好提示；
- `approvals policy remove <id>`（无 `--yes`）：规则存在 ⇒ 只打印将删规则并
  `SystemExit(2)` 拒绝，**文件零改动**；
- `approvals policy remove <id> --yes`：真删；
- id 不存在：**幂等成功**（不报错、不改文件）。

安全约束：F21（显式确认，禁止静默创建/删除）/ F22（纯 id 精确匹配）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_harness import cli
from agent_harness.config import Settings
from agent_harness.tooling.approve_policy import (
    ApprovePolicyRule,
    ApprovePolicyStore,
    PolicyGranularity,
)
from agent_harness.tooling.contract import ToolPermission


def _rule(rule_id: str = "r1", *, key: str = "npm run build") -> ApprovePolicyRule:
    return ApprovePolicyRule(
        id=rule_id,
        tool="bash",
        key=key,
        granularity=PolicyGranularity.COMMAND,
        permission_at_approval=ToolPermission.DANGER,
        created_at="2026-10-05T12:00:00+00:00",
    )


def _seed(tmp_path: Path, *rules: ApprovePolicyRule) -> None:
    ApprovePolicyStore(tmp_path).save(list(rules))


def _ids(tmp_path: Path) -> list[str]:
    return [rule.id for rule in ApprovePolicyStore(tmp_path).load()]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        model_api_key="sk-test",
        workspace_dir=str(tmp_path / "workspace"),
        _env_file=None,
    )


def _run_cli(monkeypatch, tmp_path: Path, argv: list[str]) -> None:
    """经 `_main_dispatch` 驱动，并把 cwd / Settings 钉到 tmp_path（项目根隔离）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(sys, "argv", ["agent-harness", *argv])
    cli._main_dispatch()


# ── list ───────────────────────────────────────────────────────────────────


def test_list_empty_is_friendly(tmp_path: Path) -> None:
    output = cli.approvals_policy_list_command(project_root=tmp_path)
    assert "暂无持久审批规则" in output
    assert output.strip() != ""


def test_list_renders_all_fields(tmp_path: Path) -> None:
    _seed(tmp_path, _rule())
    output = cli.approvals_policy_list_command(project_root=tmp_path)
    assert "r1" in output
    assert "bash" in output
    assert "npm run build" in output
    assert "command" in output
    assert "2026-10-05T12:00:00+00:00" in output


def test_list_via_dispatch(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed(tmp_path, _rule())
    _run_cli(monkeypatch, tmp_path, ["approvals", "policy", "list"])
    captured = capsys.readouterr()
    assert "r1" in captured.out
    assert _ids(tmp_path) == ["r1"], "list 不得改动文件"


# ── remove：无 --yes ⇒ 拒绝 + 零改动 ────────────────────────────────────────


def test_remove_without_yes_refuses_and_zero_changes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _seed(tmp_path, _rule())
    with pytest.raises(SystemExit) as excinfo:
        _run_cli(monkeypatch, tmp_path, ["approvals", "policy", "remove", "r1"])
    assert excinfo.value.code == 2, "无 --yes 的删除必须拒绝执行（exit 2）"
    captured = capsys.readouterr()
    assert "r1" in (captured.out + captured.err), "必须打印将删规则"
    assert _ids(tmp_path) == ["r1"], "未确认 ⇒ 规则文件一个字节都不许改"


# ── remove --yes ⇒ 真删 ────────────────────────────────────────────────────


def test_remove_with_yes_deletes(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed(tmp_path, _rule())
    try:
        _run_cli(
            monkeypatch, tmp_path, ["approvals", "policy", "remove", "r1", "--yes"]
        )
    except SystemExit as exc:  # 成功路径允许显式 exit 0
        assert exc.code in (0, None), f"带 --yes 的删除不该被拒绝：exit={exc.code}"
    captured = capsys.readouterr()
    assert "已删除" in captured.out
    assert _ids(tmp_path) == []


def test_remove_only_named_rule(tmp_path: Path) -> None:
    _seed(tmp_path, _rule("r1"), _rule("r2"))
    output, confirm_required = cli.approvals_policy_remove_command(
        "r1", yes=True, project_root=tmp_path
    )
    assert confirm_required is False
    assert "已删除" in output
    assert _ids(tmp_path) == ["r2"]


# ── id 不存在 ⇒ 幂等成功 ────────────────────────────────────────────────────


def test_remove_missing_without_yes_is_idempotent(tmp_path: Path) -> None:
    output, confirm_required = cli.approvals_policy_remove_command(
        "missing", yes=False, project_root=tmp_path
    )
    assert confirm_required is False, "删除一个不存在的 id 无需二次确认"
    assert "不存在" in output
    assert _ids(tmp_path) == []


def test_remove_missing_with_yes_is_idempotent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    try:
        _run_cli(
            monkeypatch,
            tmp_path,
            ["approvals", "policy", "remove", "missing", "--yes"],
        )
    except SystemExit as exc:
        assert exc.code in (0, None), f"幂等删除不该拒绝：exit={exc.code}"
    captured = capsys.readouterr()
    assert "不存在" in captured.out


def test_list_no_rules_still_no_file(tmp_path: Path) -> None:
    # 只读命令不得顺手创建空文件（零副作用）。
    cli.approvals_policy_list_command(project_root=tmp_path)
    assert not (tmp_path / ".agent-harness").exists()
