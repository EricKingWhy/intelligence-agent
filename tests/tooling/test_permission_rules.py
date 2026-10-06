"""#358 [W-14] 审批层权限规则引擎（permission_rules）单元测试。

覆盖：
- 求值顺序 deny → ask → allow，first match 胜出；deny 永远赢（跨层 union）。
- bash 命令分类：只读子集 ALLOW / 其余 ASK / 破坏性 DENY（D2 默认矩阵 R3/R4/R1）。
- 路径规则 R2：workspace 外路径 → ASK（reason 含「越出工作区」）；工作区内 → NO_MATCH。
- default_rule_set() 单例对 R1–R6 的冒烟覆盖。

性质：规则引擎是**审批层**的路由启发式，不是安全边界（见模块 docstring / ADR-0051）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.tooling.permission_rules import (
    PermissionRule,
    PermissionRuleSet,
    RuleVerdict,
    classify_bash_command,
    default_rule_set,
)

# ============================================================================
# 求值顺序 / deny 永远赢
# ============================================================================


class TestEvaluationOrder:
    def test_deny_wins_even_when_allow_also_matches(self) -> None:
        """deny 命中时即使 allow 也命中 → DENY（deny 优先）。"""
        rule_set = PermissionRuleSet(
            allow=(PermissionRule(name="a", tools=frozenset({"x"}),
                                  verdict=RuleVerdict.ALLOW, reason="allow", source="t"),),
            deny=(PermissionRule(name="d", tools=frozenset({"x"}),
                                 verdict=RuleVerdict.DENY, reason="deny", source="t"),),
        )
        verdict, _reason, rule_name = rule_set.evaluate("x", {})
        assert verdict is RuleVerdict.DENY
        assert rule_name == "d"

    def test_narrow_allow_cannot_dig_wide_deny(self) -> None:
        """跨层 union：宽 deny（*）+ 窄 allow（x）→ 仍是 DENY。"""
        rule_set = PermissionRuleSet(
            allow=(PermissionRule(name="narrow", tools=frozenset({"x"}),
                                  verdict=RuleVerdict.ALLOW, reason="allow", source="t"),),
            deny=(PermissionRule(name="wide", tools=frozenset({"*"}),
                                 verdict=RuleVerdict.DENY, reason="deny", source="t"),),
        )
        verdict, _reason, _name = rule_set.evaluate("x", {})
        assert verdict is RuleVerdict.DENY

    def test_ask_before_allow(self) -> None:
        """deny 无命中时 ask 层先于 allow 层求值。"""
        rule_set = PermissionRuleSet(
            allow=(PermissionRule(name="a", tools=frozenset({"x"}),
                                  verdict=RuleVerdict.ALLOW, reason="allow", source="t"),),
            ask=(PermissionRule(name="q", tools=frozenset({"x"}),
                                verdict=RuleVerdict.ASK, reason="ask", source="t"),),
        )
        verdict, _reason, rule_name = rule_set.evaluate("x", {})
        assert verdict is RuleVerdict.ASK
        assert rule_name == "q"

    def test_no_match_returns_sentinel(self) -> None:
        verdict, reason, rule_name = default_rule_set().evaluate("unknown-tool", {})
        assert verdict is RuleVerdict.NO_MATCH
        assert reason == ""
        assert rule_name is None


# ============================================================================
# bash 命令分类（R1/R3/R4）
# ============================================================================


class TestBashClassification:
    @pytest.mark.parametrize("command", ["ls -la", "cat f.txt", "grep -n x f.txt"])
    def test_readonly_subset_allowed(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ALLOW

    def test_pipe_connector_not_readonly(self) -> None:
        """含 `|` 连接符 → 不算只读（ASK）。"""
        assert classify_bash_command("ls -la | grep x") is RuleVerdict.ASK

    def test_destructive_rm_recursive_denied(self) -> None:
        assert classify_bash_command("rm -rf /") is RuleVerdict.DENY

    def test_non_recursive_rm_is_ask(self) -> None:
        assert classify_bash_command("rm file.txt") is RuleVerdict.ASK

    def test_sudo_denied(self) -> None:
        assert classify_bash_command("sudo apt update") is RuleVerdict.DENY

    def test_git_status_is_ask(self) -> None:
        """git 不在只读白名单（子命令读写难分）→ ASK。"""
        assert classify_bash_command("git status") is RuleVerdict.ASK

    def test_mkfs_denied(self) -> None:
        assert classify_bash_command("mkfs.ext4 /dev/sda1") is RuleVerdict.DENY

    def test_empty_command_is_ask(self) -> None:
        assert classify_bash_command("") is RuleVerdict.ASK

    def test_unparseable_command_is_ask(self) -> None:
        """shlex 解析失败 → 保守按「其余」处理（ASK）。"""
        assert classify_bash_command("echo 'unclosed") is RuleVerdict.ASK

    def test_evaluate_bash_surfaces_verdict(self) -> None:
        rule_set = default_rule_set()
        assert rule_set.evaluate("bash", {"command": "ls -la"})[0] is RuleVerdict.ALLOW
        assert rule_set.evaluate("bash", {"command": "rm -rf /"})[0] is RuleVerdict.DENY
        assert rule_set.evaluate("bash", {"command": "git status"})[0] is RuleVerdict.ASK


# ============================================================================
# 路径规则 R2
# ============================================================================


class TestPathRule:
    def test_path_outside_workspace_is_ask(self, tmp_path: Path) -> None:
        verdict, reason, rule_name = default_rule_set().evaluate(
            "read", {"path": "/etc/passwd"}, workspace_root=tmp_path,
        )
        assert verdict is RuleVerdict.ASK
        assert "越出工作区" in reason
        assert rule_name == "ask-path-outside-workspace"

    def test_path_inside_workspace_is_no_match(self, tmp_path: Path) -> None:
        """工作区内读 → R2 不命中（交给 R6 的 ALLOW）。"""
        verdict, _reason, _name = default_rule_set().evaluate(
            "read", {"path": "src/a.py"}, workspace_root=tmp_path,
        )
        assert verdict is RuleVerdict.ALLOW

    def test_write_outside_workspace_is_ask(self, tmp_path: Path) -> None:
        verdict, reason, _name = default_rule_set().evaluate(
            "write", {"path": "../escape.txt", "content": "x"}, workspace_root=tmp_path,
        )
        assert verdict is RuleVerdict.ASK
        assert "越出工作区" in reason

    def test_no_workspace_root_skips_path_rule(self) -> None:
        """无 workspace_root → 跳过 R2（防御性），回落到工具名规则。"""
        verdict, _reason, _name = default_rule_set().evaluate("read", {"path": "/etc/passwd"})
        assert verdict is RuleVerdict.ALLOW


# ============================================================================
# default 矩阵 R1–R6 冒烟
# ============================================================================


class TestDefaultMatrixSmoke:
    def test_read_inside_allowed(self, tmp_path: Path) -> None:
        assert default_rule_set().evaluate(
            "read", {"path": "a.py"}, workspace_root=tmp_path,
        )[0] is RuleVerdict.ALLOW

    def test_write_inside_asked(self, tmp_path: Path) -> None:
        assert default_rule_set().evaluate(
            "write", {"path": "a.py", "content": "x"}, workspace_root=tmp_path,
        )[0] is RuleVerdict.ASK

    def test_edit_inside_asked(self, tmp_path: Path) -> None:
        assert default_rule_set().evaluate(
            "edit", {"path": "a.py", "old_string": "a", "new_string": "b"},
            workspace_root=tmp_path,
        )[0] is RuleVerdict.ASK

    def test_bash_other_asked(self) -> None:
        assert default_rule_set().evaluate(
            "bash", {"command": "python build.py"},
        )[0] is RuleVerdict.ASK

    def test_singleton_is_reused(self) -> None:
        assert default_rule_set() is default_rule_set()
