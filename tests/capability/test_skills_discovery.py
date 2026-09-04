"""T3：SKILL.md 解析 + SkillDiscovery（spec 09 §2，ADR-0011 Q1/Q2/Q4）。"""

from __future__ import annotations

from pathlib import Path

from agent_harness.skills.discovery import SkillDiscovery, parse_skill_markdown


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "正文内容") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return skill_file


VALID_FM = 'name: pdf-export\ndescription: "导出 PDF 报告"'


def _fm(name: str, description: str = "示例技能") -> str:
    return f'name: {name}\ndescription: "{description}"'


class TestParseSkillMarkdown:
    def test_valid_frontmatter_and_body(self, tmp_path):
        path = _write_skill(tmp_path, "pdf-export", VALID_FM, "步骤一：选择文件")
        entry, errors = parse_skill_markdown(path)
        assert errors == []
        assert entry.name == "pdf-export"
        assert entry.description == "导出 PDF 报告"
        assert entry.source_path == path
        # 渐进披露的物理前提：发现阶段不读正文——body 延迟加载。
        assert entry.load_body() == "步骤一：选择文件"

    def test_missing_name_or_description_is_error_not_crash(self, tmp_path):
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "SKILL.md").write_text("---\nname: broken\n---\nbody", encoding="utf-8")
        entry, errors = parse_skill_markdown(tmp_path / "broken" / "SKILL.md")
        assert entry is None
        assert errors and "description" in errors[0]

    def test_no_frontmatter_is_error(self, tmp_path):
        (tmp_path / "plain").mkdir()
        (tmp_path / "plain" / "SKILL.md").write_text("无 frontmatter 内容", encoding="utf-8")
        entry, errors = parse_skill_markdown(tmp_path / "plain" / "SKILL.md")
        assert entry is None and errors

    def test_unknown_frontmatter_fields_preserved(self, tmp_path):
        path = _write_skill(tmp_path, "x", VALID_FM + "\nversion: 2")
        entry, errors = parse_skill_markdown(path)
        assert errors == []
        assert entry.meta.get("version") == 2

    def test_body_lazily_read_each_call(self, tmp_path):
        path = _write_skill(tmp_path, "lazy", VALID_FM, "v1")
        entry, _ = parse_skill_markdown(path)
        path.write_text(path.read_text(encoding="utf-8").replace("v1", "v2"), encoding="utf-8")
        assert entry.load_body() == "v2"

    def test_utf8_bom_is_stripped(self, tmp_path):
        """Windows 记事本默认写 BOM：按 utf-8 读会残留 \\ufeff，首行 '---' 校验失败 → 技能全灭。"""
        skill_dir = tmp_path / "bom"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_bytes(
            b"\xef\xbb\xbf" + "---\nname: bom-skill\ndescription: 带 BOM\n---\n\nBOM 正文\n".encode(),
        )
        entry, errors = parse_skill_markdown(skill_file)
        assert errors == []
        assert entry.name == "bom-skill"
        assert entry.description == "带 BOM"
        assert entry.load_body() == "BOM 正文"

    def test_crlf_line_endings_frontmatter_ok(self, tmp_path):
        """CRLF（Windows 换行）：splitlines 已能处理——pin 该行为防回归。"""
        skill_dir = tmp_path / "crlf"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_bytes(
            "---\r\nname: crlf-skill\r\ndescription: CRLF 换行\r\n---\r\n\r\nCRLF 正文\r\n".encode(),
        )
        entry, errors = parse_skill_markdown(skill_file)
        assert errors == []
        assert entry.name == "crlf-skill"
        assert entry.load_body() == "CRLF 正文"


class TestSkillDiscovery:
    def test_scans_global_then_project_single_level(self, tmp_path):
        global_dir, project_dir = tmp_path / "global", tmp_path / "project"
        _write_skill(global_dir, "alpha", _fm("alpha", "A"))
        _write_skill(project_dir, "beta", _fm("beta", "B"))
        discovery = SkillDiscovery(directories=[global_dir, project_dir])
        catalog = discovery.discover()
        assert [e.name for e in catalog.entries] == ["alpha", "beta"]
        assert catalog.errors == []

    def test_duplicate_name_first_wins_and_conflict_visible(self, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        _write_skill(first, "dup", _fm("dup", "first"))
        _write_skill(second, "dup", _fm("dup", "second"))
        discovery = SkillDiscovery(directories=[first, second])
        catalog = discovery.discover()
        assert [e.description for e in catalog.entries] == ["first"]
        assert any("dup" in c for c in catalog.conflicts)

    def test_missing_directory_is_empty_catalog_not_error(self, tmp_path):
        catalog = SkillDiscovery(directories=[tmp_path / "ghost"]).discover()
        assert catalog.entries == [] and catalog.errors == []

    def test_manual_paths_are_included(self, tmp_path):
        skill = _write_skill(tmp_path / "anywhere", "manual", _fm("manual", "手动指定"))
        catalog = SkillDiscovery(directories=[], manual_paths=[skill]).discover()
        assert [e.name for e in catalog.entries] == ["manual"]

    def test_manual_missing_file_is_error(self, tmp_path):
        catalog = SkillDiscovery(directories=[], manual_paths=[tmp_path / "ghost.md"]).discover()
        assert catalog.entries == [] and catalog.errors

    def test_resolve_within_rejects_escape(self, tmp_path):
        """symlink/相对路径逃逸防线的纯函数测试（跨平台不依赖 symlink 权限）。"""
        from agent_harness.skills.discovery import resolve_within

        root = tmp_path / "root"
        root.mkdir()
        assert resolve_within(root / "a" / "SKILL.md", root) is True
        assert resolve_within(root, root) is True
        assert resolve_within(tmp_path / "outside" / "SKILL.md", root) is False
        assert resolve_within(root.parent / "SKILL.md", root) is False

    def test_nested_directories_ignored(self, tmp_path):
        deep = tmp_path / "skills" / "nested" / "deeper"
        deep.mkdir(parents=True)
        (deep / "SKILL.md").write_text("---\nname: deep\ndescription: x\n---\n", encoding="utf-8")
        catalog = SkillDiscovery(directories=[tmp_path / "skills"]).discover()
        assert catalog.entries == []
