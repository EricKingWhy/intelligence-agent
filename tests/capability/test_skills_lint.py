"""T-529-2：SKILL.md 静态 lint（#529 §5.1，每条规则一测）。

六条规则（§5.1）：
1. frontmatter 可解析且 name/description 非空（复用 parse_skill_markdown，单真相）；
2. 触发条件非空（when_to_use 或 description 含触发语）；
3. 危险动作（删库/删文件/外发网络）必须显式声明 allowed-tools；
4. 超过 64KB（MAX_SKILL_BYTES，写入侧语义）失败；
5. name 冲突 → 提示走更新分支（阻断注册，不静默 shadow）；
6. ALWAYS/NEVER 全大写 → 警告（不阻断，ADAPT 依据 C 反僵化子集）。
"""

from __future__ import annotations

from pathlib import Path

from agent_harness.skills.discovery import MAX_SKILL_BYTES
from agent_harness.skills.lint import LintResult, lint_skill


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "正文内容") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return skill_file


VALID_FM = 'name: pdf-export\ndescription: "当用户需要导出 PDF 报告时使用"'


class TestRule1Frontmatter:
    def test_parse_failure_reuses_parse_skill_markdown_errors(self, tmp_path):
        """规则 1：解析失败进 errors——错误文案来自 parse_skill_markdown（单真相，不写第二套校验）。"""
        path = _write_skill(tmp_path, "broken", "name: broken")  # 缺 description
        result = lint_skill(path)
        assert result.ok is False
        assert any("description" in e for e in result.errors)

    def test_invalid_yaml_is_error(self, tmp_path):
        path = _write_skill(tmp_path, "badyaml", "name: [unclosed")
        result = lint_skill(path)
        assert any("YAML" in e or "frontmatter" in e for e in result.errors)

    def test_missing_frontmatter_fence_is_error(self, tmp_path):
        path = tmp_path / "plain" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("没有 frontmatter 的文件", encoding="utf-8")
        result = lint_skill(path)
        assert result.ok is False and result.errors


class TestRule2TriggerCondition:
    def test_no_trigger_condition_is_error(self, tmp_path):
        """规则 2：when_to_use 缺席且 description 无触发语 → 阻断。"""
        path = _write_skill(tmp_path, "no-trigger", 'name: no-trigger\ndescription: "导出 PDF 报告"')
        result = lint_skill(path)
        assert any("触发" in e for e in result.errors)

    def test_when_to_use_satisfies_trigger_rule(self, tmp_path):
        path = _write_skill(
            tmp_path, "with-when",
            'name: with-when\ndescription: "导出 PDF 报告"\nwhen_to_use: 需要导出 PDF 时',
        )
        result = lint_skill(path)
        assert result.errors == []

    def test_description_trigger_hint_satisfies_rule(self, tmp_path):
        path = _write_skill(tmp_path, "hinted", 'name: hinted\ndescription: "Use when exporting PDF reports"')
        assert lint_skill(path).errors == []


class TestRule3DangerousActions:
    def test_dangerous_body_without_allowed_tools_fails(self, tmp_path):
        """规则 3：正文含危险动作模式但未声明 allowed-tools → 阻断。"""
        path = _write_skill(
            tmp_path, "dangerous", VALID_FM,
            body="清理时执行 rm -rf /tmp/cache 即可",
        )
        result = lint_skill(path)
        assert any("allowed-tools" in e for e in result.errors)

    def test_dangerous_body_with_allowed_tools_passes(self, tmp_path):
        path = _write_skill(
            tmp_path, "declared",
            VALID_FM + '\nallowed-tools: ["Bash"]',
            body="清理时执行 rm -rf /tmp/cache 即可",
        )
        result = lint_skill(path)
        assert result.errors == []

    def test_external_network_pattern_requires_declaration(self, tmp_path):
        """外发网络（curl/wget）同属危险动作模式表。"""
        path = _write_skill(tmp_path, "net", VALID_FM, body="curl https://example.com/api 上报结果")
        result = lint_skill(path)
        assert any("allowed-tools" in e for e in result.errors)

    def test_dangerous_pattern_table_is_configurable(self, tmp_path):
        """模式表可配置（§5.1）：调用方传入自定义模式集替换默认表。"""
        path = _write_skill(tmp_path, "custom", VALID_FM, body="curl https://example.com/api")
        import re

        result = lint_skill(path, dangerous_patterns=(re.compile(r"自定义危险"),))
        assert result.errors == []  # 默认表被替换：curl 不再命中


class TestRule4SizeLimit:
    def test_oversized_skill_fails(self, tmp_path):
        """规则 4：整个 SKILL.md 序列化后超 64KB → 阻断（MAX_SKILL_BYTES 语义）。"""
        assert MAX_SKILL_BYTES == 64_000
        path = _write_skill(tmp_path, "big", VALID_FM, body="x" * (MAX_SKILL_BYTES + 1))
        result = lint_skill(path)
        assert any("too large" in e for e in result.errors)

    def test_just_under_limit_passes(self, tmp_path):
        path = _write_skill(tmp_path, "ok-size", VALID_FM, body="x" * (MAX_SKILL_BYTES - 4096))
        assert lint_skill(path).errors == []


class TestRule5NameConflict:
    def test_conflicting_name_blocks_with_update_hint(self, tmp_path):
        """规则 5：与既有 catalog 名字冲突 → 阻断并提示走更新分支（shadowed 语义显式化）。"""
        path = _write_skill(tmp_path, "pdf-export", VALID_FM)
        result = lint_skill(path, existing_names={"pdf-export"})
        assert result.ok is False
        assert any("update" in e or "更新" in e for e in result.errors)

    def test_no_conflict_passes(self, tmp_path):
        path = _write_skill(tmp_path, "fresh-name", VALID_FM)
        assert lint_skill(path, existing_names={"other"}).errors == []


class TestRule6AntiRigidityWarning:
    def test_uppercase_always_never_warns_without_blocking(self, tmp_path):
        """规则 6：正文 ALWAYS/NEVER 全大写指令词 → 警告不阻断（依据 C 反僵化）。"""
        path = _write_skill(
            tmp_path, "rigid", VALID_FM,
            body="ALWAYS 先备份。\nNEVER 跳过校验。",
        )
        result = lint_skill(path)
        assert result.ok is True  # 警告不阻断
        assert result.warnings and any("ALWAYS" in w for w in result.warnings)
        assert any("NEVER" in w for w in result.warnings)

    def test_lowercase_usage_does_not_warn(self, tmp_path):
        path = _write_skill(tmp_path, "soft", VALID_FM, body="Always backup first — 警告只盯全大写。")
        result = lint_skill(path)
        assert result.warnings == []


class TestLintResultShape:
    def test_clean_skill_is_ok_with_no_findings(self, tmp_path):
        path = _write_skill(
            tmp_path, "clean",
            'name: clean\ndescription: "当用户要求生成周报时使用"\nwhen_to_use: 每周五',
        )
        result = lint_skill(path, existing_names=set())
        assert isinstance(result, LintResult)
        assert result.ok is True
        assert result.errors == [] and result.warnings == []

    def test_errors_and_warnings_are_independent_lists(self, tmp_path):
        """同时命中 error（危险未声明）与 warning（ALWAYS）→ 两列各自就位。"""
        path = _write_skill(
            tmp_path, "mixed", 'name: mixed\ndescription: "导出报告"',
            body="ALWAYS rm -rf /tmp/x",
        )
        result = lint_skill(path)
        assert result.ok is False
        assert any("allowed-tools" in e for e in result.errors)
        assert result.warnings
