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

    @pytest.mark.parametrize(
        "command",
        [
            "find . -exec echo {} +",
            "find . -execdir echo {} +",
            "find . -ok echo {} +",
            "find . -okdir echo {} +",
            "find . -delete",
            "find . -fprint result.txt",
            "find . -fprint0 result.txt",
            "find . -fprintf result.txt %p",
            "find . -fls result.txt",
            "find . -name x -delete",
            "cat local.txt > result.txt",
            "cat ../outside.txt",
            "cat ./../outside.txt",
            "cat /etc/passwd",
            "cat ~",
            "cat %TEMP%",
            "cat $HOME/x",
            "cat C:secret.txt",
            "cat {/etc,}/passwd",
            "cat %~dp0secret.txt",
            r"cat C:\outside.txt",
            r"cat \outside.txt",
            "cat local.txt\nrm local.txt",
            "tree -o result.txt",
            "file -C",
        ],
    )
    def test_readonly_subset_rejects_actions_and_outside_paths(
        self, command: str,
    ) -> None:
        """Unsafe actions, paths, shell syntax, and writer flags must not bypass approval."""
        assert classify_bash_command(command) is RuleVerdict.ASK

    @pytest.mark.parametrize(
        "command",
        [
            "wc -c --files0-from=/tmp/rev1484-out/list.txt",
            "grep -f/etc/passwd .",
            "grep --file=/etc/passwd .",
            "grep --exclude-from=/etc/passwd .",
            "file -f/etc/passwd",
            "file --files-from=/etc/passwd",
            "head --files0-from=x",
            "diff --from-file=/etc/passwd",
            "date -f/etc/passwd",
        ],
    )
    def test_file_consuming_options_require_approval(self, command: str) -> None:
        """文件承载选项不能进入可证明安全的免问子集。"""
        assert classify_bash_command(command) is RuleVerdict.ASK

    @pytest.mark.parametrize(
        "command",
        [
            # GNU getopt_long 接受无歧义前缀；`--files0` 就是 `--files0-from`。
            "wc -c --files0=list.txt",
            "wc -c --files0-fro=list.txt",
            "wc -c --files0-fro list.txt",
        ],
    )
    def test_long_option_abbreviations_require_approval(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ASK

    @pytest.mark.parametrize(
        "command",
        [
            # 短旗附着值与空格形一样能承载工作区外路径。
            "diff -X/etc/passwd a.txt b.txt",
            "file -m/etc/passwd README.md",
            "diff --exclude-from=/etc/passwd a.txt b.txt",
            "file --magic-file=/etc/passwd README.md",
            "tree --fromfile=/etc/passwd",
            "tree --fromfile /etc/passwd",
            # 相对值：路径判定看不出越界，只有选项表本身能拦（文件内容可再指向外部）。
            "diff -Xexcl.txt a.txt b.txt",
            "file -mmagic.mgc README.md",
            "tree --fromfile=list.txt",
        ],
    )
    def test_attached_file_option_values_require_approval(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ASK

    @pytest.mark.parametrize(
        "command",
        [
            # 票面要求的两条之一：`file --magic-file=` 现已进表（P1-3）由表拦，
            # `cat/stat --nope=` 是真正的表外选项，只由 `=` 提取段拦住。
            "file --magic-file=/etc/passwd README.md",
            "cat --nope=/etc/passwd",
            "stat --nope=/etc/passwd",
        ],
    )
    def test_equals_attached_value_is_a_path_candidate(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ASK

    @pytest.mark.parametrize(
        "command",
        [
            # 盘符判定只看 token 开头：参数中间的冒号不是盘符。
            "cat notes:2026.txt",
            "grep -n TODO: src/app.py",
            'grep -rn "key:" .',
            "echo done:",
        ],
    )
    def test_colon_inside_argument_is_not_a_drive_letter(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ALLOW

    @pytest.mark.parametrize(
        "command",
        [
            # GNU getopt_long 只把「输入 token 是选项名前缀」当缩写；反向（token 更长）
            # 是另一个选项，不是缩写。`--` 是选项终止符，不是长选项本身。
            "grep -rn --files-with-matches TODO src/",
            "grep -- files",
        ],
    )
    def test_reverse_long_prefix_and_terminator_are_allowed(self, command: str) -> None:
        """反向长选项前缀与 `--` 终止符不得被文件承载选项表误伤（P2-7）。"""
        assert classify_bash_command(command) is RuleVerdict.ALLOW

    def test_pattern_file_attached_value_inside_cluster_remains_allowed(self) -> None:
        """集内：簇内前置了其它旗的 `-f` 附着值（`-ifile` = `-i -f ile`）仍 ALLOW。"""
        assert classify_bash_command("grep -ifile README.md") is RuleVerdict.ALLOW

    def test_pattern_file_attached_value_at_cluster_start_is_asked(self) -> None:
        """集外：簇首 `-f<相对值>` 与空格形同语义，恢复 ASK（P2-8）。"""
        assert classify_bash_command("grep -fpatterns.txt src/app.py") is RuleVerdict.ASK

    @pytest.mark.parametrize(
        "command",
        [
            # 短旗簇逐字符扫描：`-e`/`-i` 的取值不是文件承载选项；
            # `-if` 仍在簇内触达 `-f`，附着值走路径判定。
            "grep -efile README.md",
            "grep -ifile README.md",
            "date -Iseconds",
            "date -Idate",
        ],
    )
    def test_short_flag_values_remain_allowed(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ALLOW

    @pytest.mark.parametrize("command", ["grep -if/etc/passwd .", "file -Nm/etc/passwd"])
    def test_attached_value_inside_cluster_is_path_checked(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ASK

    @pytest.mark.parametrize(
        "command",
        [
            "find .",
            "find . -maxdepth 2 -type f -print",
            "find . -name '*.py' -print0",
            "find . -printf '%p'",
            "date +%Y",
        ],
    )
    def test_safe_find_queries_remain_allowed(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ALLOW

    def test_find_unknown_expression_is_asked(self) -> None:
        assert classify_bash_command("find . -unknown-action") is RuleVerdict.ASK

    @pytest.mark.parametrize(
        "command",
        [
            "date 2026-01-01",
            "date --set=2026-01-01",
            "date -s2026-01-01",
            "date -us20260101",
            "date -u -s20260101",
        ],
    )
    def test_date_setter_is_asked(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ASK

    @pytest.mark.parametrize(
        "command",
        [
            "date",
            "date +%Y",
            "date -u",
            "date -R",
            "date -I",
            "date -u +%Y",
            "hostname",
            "hostname -a",
            "hostname -A",
            "hostname -d",
            "hostname -f",
            "hostname -i",
            "hostname -I",
            "hostname -s",
            "hostname -y",
        ],
    )
    def test_query_only_date_and_hostname_flags_remain_allowed(
        self, command: str,
    ) -> None:
        assert classify_bash_command(command) is RuleVerdict.ALLOW

    @pytest.mark.parametrize(
        "command",
        [
            "hostname NAME",
            "hostname -F FILE",
            "hostname -F/etc/hostname",
            "hostname -bF/etc/hostname",
            "hostname --file=/etc/hostname",
        ],
    )
    def test_hostname_mutating_forms_are_asked(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ASK

    @pytest.mark.parametrize("command", ["find . -maxdepth", "find . -type"])
    def test_incomplete_find_expression_is_asked(self, command: str) -> None:
        assert classify_bash_command(command) is RuleVerdict.ASK

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
