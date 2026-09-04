"""T4：SkillCapability + 目录注入 + load_skill（spec 09 §2 闭环，ADR-0011 Q3/Q5，Gate 2）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.capability.base import (
    CapabilityError,
    CapabilityRegistry,
    Degradation,
)
from agent_harness.capability.wiring import wire_capabilities
from agent_harness.config import Settings
from agent_harness.session import Session
from agent_harness.skills.capability import SkillCapability
from agent_harness.skills.context_provider import SkillCatalogContextProvider
from agent_harness.skills.discovery import SkillCatalog, SkillCatalogEntry
from agent_harness.skills.tool import LoadSkillTool
from agent_harness.tooling.contract import ToolSideEffect

BODY = "第一步：读取模板\n第二步：导出 PDF"


def _entry(name: str, description: str, body: str, tmp_path: Path) -> SkillCatalogEntry:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    from agent_harness.skills.discovery import parse_skill_markdown
    entry, errors = parse_skill_markdown(skill_file)
    assert errors == []
    return entry


def _capability(tmp_path: Path) -> SkillCapability:
    catalog = SkillCatalog(entries=[
        _entry("pdf-export", "导出 PDF 报告", BODY, tmp_path),
        _entry("code-review", "审查代码", "审查正文", tmp_path),
    ])
    return SkillCapability(catalog)


class TestSkillCapability:
    def test_catalog_lists_entries_without_body(self, tmp_path):
        capability = _capability(tmp_path)
        names = [e.name for e in capability.catalog()]
        assert names == ["pdf-export", "code-review"]
        # 目录条目不携带正文（渐进披露）：
        assert all(not hasattr(e, "_body") for e in capability.catalog())

    def test_load_known_returns_body(self, tmp_path):
        assert _capability(tmp_path).load("pdf-export") == BODY

    def test_load_unknown_raises_not_found(self, tmp_path):
        with pytest.raises(CapabilityError) as err:
            _capability(tmp_path).load("ghost")
        assert err.value.code == "not_found"


class TestSkillCatalogContextProvider:
    @pytest.mark.asyncio
    async def test_when_to_use_goes_into_catalog_line(self, tmp_path):
        """可选 when_to_use（ADR-0011 grill 批准的扩展）进目录行；Gate 2 仍成立。"""
        catalog = SkillCatalog(entries=[
            _entry("with-when", "有触发场景", "正文A", tmp_path),
        ])
        # 给条目补 when_to_use（经真实解析路径）：
        skill_file = catalog.entries[0].source_path
        skill_file.write_text(
            "---\nname: with-when\ndescription: 有触发场景\nwhen_to_use: 需要导出 PDF 时\n---\n\n正文A\n",
            encoding="utf-8",
        )
        from agent_harness.skills.discovery import parse_skill_markdown
        entry, errors = parse_skill_markdown(skill_file)
        assert errors == [] and entry.when_to_use == "需要导出 PDF 时"
        capability = SkillCapability(SkillCatalog(entries=[entry]))
        session = Session.__new__(Session)
        content = (await SkillCatalogContextProvider(capability).select(session, 1000))[0].content
        assert "何时用：需要导出 PDF 时" in content
        assert "正文A" not in content  # Gate 2 不变

    @pytest.mark.asyncio
    async def test_catalog_injected_as_single_system_message(self, tmp_path):
        session = Session.__new__(Session)  # provider 不消费事件，最小实例即可
        provider = SkillCatalogContextProvider(_capability(tmp_path))
        messages = await provider.select(session, 1000)
        assert len(messages) == 1
        content = messages[0].content
        assert "- pdf-export: 导出 PDF 报告" in content
        assert "- code-review: 审查代码" in content
        assert "数据" in content and "load_skill" in content
        # Gate 2：目录注入绝不携带 skill 正文。
        assert BODY not in content and "审查正文" not in content

    @pytest.mark.asyncio
    async def test_empty_catalog_is_zero_noise(self, tmp_path):
        provider = SkillCatalogContextProvider(SkillCapability(SkillCatalog()))
        session = Session.__new__(Session)
        assert await provider.select(session, 1000) == []

    @pytest.mark.asyncio
    async def test_zero_budget_returns_empty(self, tmp_path):
        session = Session.__new__(Session)
        assert await SkillCatalogContextProvider(_capability(tmp_path)).select(session, 0) == []

    @pytest.mark.asyncio
    async def test_tiny_budget_truncates_lines_never_body(self, tmp_path):
        session = Session.__new__(Session)
        provider = SkillCatalogContextProvider(_capability(tmp_path))
        messages = await provider.select(session, 40)
        assert len(messages) <= 1
        if messages:
            assert BODY not in messages[0].content


class TestLoadSkillTool:
    def test_contract_is_readonly_context_tool(self, tmp_path):
        tool = LoadSkillTool(_capability(tmp_path))
        assert tool.name == "load_skill"
        assert tool.side_effect is ToolSideEffect.READ_ONLY

    @pytest.mark.asyncio
    async def test_known_skill_returns_body_with_data_frame(self, tmp_path):
        tool = LoadSkillTool(_capability(tmp_path))
        result = await tool.execute(tool.args_schema(name="pdf-export"))
        assert result.ok is True
        assert BODY in result.data["content"]
        assert "数据" in result.data["content"]

    @pytest.mark.asyncio
    async def test_unknown_skill_fails_without_fabrication(self, tmp_path):
        tool = LoadSkillTool(_capability(tmp_path))
        result = await tool.execute(tool.args_schema(name="ghost"))
        assert result.ok is False
        assert result.error_code is not None
        assert "ghost" in result.message


class TestWiring:
    @pytest.mark.asyncio
    async def test_skills_config_wires_capability_provider_and_tool(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "pdf-export").mkdir()
        (skills_dir / "pdf-export" / "SKILL.md").write_text(
            f"---\nname: pdf-export\ndescription: 导出\n---\n\n{BODY}\n", encoding="utf-8",
        )
        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path),
            skill_global_dir=str(tmp_path / "no-global"),
        )
        registry = CapabilityRegistry()
        from agent_harness.capability.config import parse_capabilities_config
        wiring = await wire_capabilities(
            registry, parse_capabilities_config('{"skills": {}}'), settings=settings,
        )
        assert registry.descriptor("skills").degradation is Degradation.OPTIONAL_RUNTIME
        capability = registry.get("skills")
        assert [e.name for e in capability.catalog()] == ["pdf-export"]
        assert any(isinstance(p, SkillCatalogContextProvider) for p in wiring.context_providers)
        assert any(isinstance(t, LoadSkillTool) for t in wiring.tools)

    @pytest.mark.asyncio
    async def test_skills_disabled_is_skipped(self, tmp_path):
        settings = Settings(_env_file=None, workspace_dir=str(tmp_path))
        registry = CapabilityRegistry()
        from agent_harness.capability.config import parse_capabilities_config
        wiring = await wire_capabilities(
            registry, parse_capabilities_config('{"skills": {"enabled": false}}'), settings=settings,
        )
        assert registry.available() == []
        assert wiring.tools == [] and wiring.context_providers == []
