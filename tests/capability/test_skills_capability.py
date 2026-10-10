"""T4：SkillCapability + 目录注入 + load_skill（spec 09 §2 闭环，ADR-0011 Q3/Q5，Gate 2）。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_harness.capability.base import (
    CapabilityError,
    CapabilityRegistry,
    Degradation,
)
from agent_harness.capability.config import ProviderConfig
from agent_harness.capability.wiring import coerce_skill_path_list, wire_capabilities
from agent_harness.config import Settings
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session import Session
from agent_harness.skills.capability import SkillCapability
from agent_harness.skills.context_provider import SkillCatalogContextProvider
from agent_harness.skills.discovery import (
    SkillCatalog,
    SkillCatalogEntry,
    SkillDiscovery,
)
from agent_harness.skills.tool import LoadSkillTool, _LoadSkillArgs
from agent_harness.tooling.contract import ToolSideEffect
from agent_harness.tooling.result import ErrorCode

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
    """#529 后 capability 持 SkillDiscovery 引用：真实文件 + 真实发现路径。"""
    _entry("pdf-export", "导出 PDF 报告", BODY, tmp_path)
    _entry("code-review", "审查代码", "审查正文", tmp_path)
    discovery = SkillDiscovery(directories=[tmp_path], project_dir=tmp_path)
    discovery.discover()
    return SkillCapability(discovery)


def _capability_with_catalog(catalog: SkillCatalog) -> SkillCapability:
    """合成条目注入：catalog 直填 discovery 缓存投影（不走磁盘解析，#588 等用例）。"""
    discovery = SkillDiscovery(directories=[])
    discovery._catalog = catalog
    return SkillCapability(discovery)


class TestSkillCapability:
    def test_catalog_lists_entries_without_body(self, tmp_path):
        capability = _capability(tmp_path)
        names = [e.name for e in capability.catalog()]
        # discover() 按 sorted(iterdir()) 扫描：目录序即字母序。
        assert names == ["code-review", "pdf-export"]
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
        capability = _capability(tmp_path)
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
        provider = SkillCatalogContextProvider(SkillCapability(SkillDiscovery(directories=[])))
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
    async def test_known_skill_body_is_structurally_isolated(self, tmp_path):
        """#546 案 A：framing 是注册表 fragment 进 `message`，正文独占 `data["content"]`。

        结构隔离 = JSON 字段级（message/data 分离）+ 注册表 section 级
        （`frame:untrusted_skill`，与 knowledge/websearch/tool_output 同族同槽位），
        不再是"同 content 字段一句话前缀"。framing 是纵深防御，不替代 Runtime 权限。
        """
        tool = LoadSkillTool(_capability(tmp_path))
        result = await tool.execute(tool.args_schema(name="pdf-export"))
        assert result.ok is True
        frame = DEFAULT_REGISTRY.assemble("frame:untrusted_skill").fragment_text
        assert frame in result.message  # 系统声明走 message 通道
        assert result.data["content"] == BODY  # 正文原样独占 data 字段，零前缀拼接
        assert frame not in result.data["content"]

    @pytest.mark.asyncio
    async def test_small_body_is_not_truncated(self, tmp_path):
        tool = LoadSkillTool(_capability(tmp_path))
        result = await tool.execute(tool.args_schema(name="pdf-export"))
        assert result.data["content"].endswith(BODY)
        assert "已截断" not in result.data["content"]

    @pytest.mark.asyncio
    async def test_huge_body_is_capped_with_honest_marker(self, tmp_path):
        """64k 上限：技能是参考文档不是数据转储；无 Artifact 存储时防超大内容进 Context/事件。"""
        body = "A" * 64_000 + "B" * 36_000  # 10 万字符正文
        _entry("big", "大技能", body, tmp_path)
        capability = _capability(tmp_path)
        tool = LoadSkillTool(capability)
        result = await tool.execute(tool.args_schema(name="big"))
        assert result.ok is True
        content = result.data["content"]
        assert len(content) < 64_000 + 200  # 有界：截断正文 + 标记
        assert "A" * 64_000 in content  # 前 64k 字符完整保留
        assert "B" not in content  # 上限之后的正文绝不出现
        assert "已截断" in content and "100000" in content  # 诚实标记，不伪造"文档结束"

    @pytest.mark.asyncio
    async def test_unknown_skill_fails_without_fabrication(self, tmp_path):
        """未知技能名是模型传参错误 → INVALID_ARGUMENT（TOOL_NOT_FOUND 语义是"未知工具名"）。"""
        tool = LoadSkillTool(_capability(tmp_path))
        result = await tool.execute(tool.args_schema(name="ghost"))
        assert result.ok is False
        assert result.error_code is ErrorCode.INVALID_ARGUMENT
        assert result.retryable is False
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
    async def test_next_runtime_applies_pending_git_skill_before_discovery(self, tmp_path):
        from agent_harness.capability.config import parse_capabilities_config
        from agent_harness.skills.package_manager import SkillPackageManager

        repo = tmp_path / "repo"
        skill = repo / "packages" / "runtime-skill"
        skill.mkdir(parents=True)
        (skill / "references").mkdir()

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True
            )
            return result.stdout.strip()

        def write_version(body: str, resource: str) -> None:
            (skill / "SKILL.md").write_text(
                "---\nname: runtime-skill\ndescription: Runtime update.\n---\n\n"
                f"{body}\nRead [the guide](references/guide.md).\n",
                encoding="utf-8",
            )
            (skill / "references" / "guide.md").write_text(resource, encoding="utf-8")

        write_version("Old body.", "Old resource.\n")
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        git("add", ".")
        git("commit", "-qm", "old version")
        old_commit = git("rev-parse", "HEAD")
        write_version("New body.", "New resource.\n")
        git("add", ".")
        git("commit", "-qm", "new version")
        new_commit = git("rev-parse", "HEAD")

        workspace = tmp_path / "workspace"
        manager = SkillPackageManager(workspace)
        manager.install_git(repo.as_uri(), ref=old_commit, subdirectory="packages/runtime-skill")
        manager.enable("runtime-skill")
        manager.update("runtime-skill", ref=new_commit)
        assert "Old body." in (manager.managed_skills_dir / "runtime-skill" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        settings = Settings(
            _env_file=None,
            workspace_dir=str(workspace),
            skill_global_dir=str(tmp_path / "no-global"),
        )
        registry = CapabilityRegistry()
        wiring = await wire_capabilities(
            registry,
            parse_capabilities_config('{"skills": {}}'),
            settings=settings,
        )

        assert manager.list_packages()["runtime-skill"]["resolved_commit"] == new_commit
        load_tool = next(tool for tool in wiring.tools if isinstance(tool, LoadSkillTool))
        loaded = await load_tool.execute(load_tool.args_schema(name="runtime-skill"))
        assert loaded.ok is True
        assert "New body." in loaded.data["content"]
        assert (Path(loaded.data["resource_root"]) / "references" / "guide.md").read_text(
            encoding="utf-8"
        ) == "New resource.\n"

    @pytest.mark.asyncio
    async def test_next_runtime_applies_global_version_without_selecting_global_skill(self, tmp_path):
        from agent_harness.capability.config import parse_capabilities_config
        from agent_harness.skills.package_manager import SkillPackageManager

        repo = tmp_path / "repo"
        skill = repo / "packages" / "shared-skill"
        skill.mkdir(parents=True)

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True
            )
            return result.stdout.strip()

        def write_version(body: str) -> None:
            (skill / "SKILL.md").write_text(
                "---\nname: shared-skill\ndescription: Global version.\n---\n\n"
                f"{body}\n",
                encoding="utf-8",
            )

        write_version("Old version.")
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        git("add", ".")
        git("commit", "-qm", "old version")
        old_commit = git("rev-parse", "HEAD")
        write_version("New version.")
        git("add", ".")
        git("commit", "-qm", "new version")
        new_commit = git("rev-parse", "HEAD")

        workspace = tmp_path / "workspace"
        global_skills = tmp_path / "home" / ".intelligence-agent" / "skills"
        manager = SkillPackageManager(
            workspace, global_skills_dir=global_skills, scope="global"
        )
        manager.install_git(
            repo.as_uri(), ref=old_commit, subdirectory="packages/shared-skill"
        )
        manager.update("shared-skill", ref=new_commit)
        assert manager.list_packages()["shared-skill"]["pending_version"][
            "resolved_commit"
        ] == new_commit

        settings = Settings(
            _env_file=None,
            workspace_dir=str(workspace),
            skill_global_dir=str(global_skills),
        )
        registry = CapabilityRegistry()
        wiring = await wire_capabilities(
            registry,
            parse_capabilities_config('{"skills": {}}'),
            settings=settings,
        )

        record = manager.list_packages()["shared-skill"]
        assert record["resolved_commit"] == new_commit
        assert record["pending_version"] is None
        assert registry.get("skills").catalog() == []
        load_tool = next(tool for tool in wiring.tools if isinstance(tool, LoadSkillTool))
        result = await load_tool.execute(load_tool.args_schema(name="shared-skill"))
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_new_runtime_loads_only_enabled_managed_skill_packages(self, tmp_path):
        from agent_harness.capability.config import parse_capabilities_config
        from agent_harness.skills.package_manager import SkillPackageManager

        workspace = tmp_path / "workspace"
        user_skill = workspace / "skills" / "user-skill" / "SKILL.md"
        user_skill.parent.mkdir(parents=True)
        user_skill.write_text(
            "---\nname: user-skill\ndescription: User maintained.\n---\n\nUser body.\n",
            encoding="utf-8",
        )
        source = tmp_path / "source" / "managed-skill"
        (source / "references").mkdir(parents=True)
        (source / "SKILL.md").write_text(
            "---\nname: managed-skill\ndescription: Imported complete package.\n---\n\n"
            "Read [the reference](references/guide.md).\n",
            encoding="utf-8",
        )
        (source / "references" / "guide.md").write_text("Managed reference.\n", encoding="utf-8")
        manager = SkillPackageManager(workspace)
        manager.install(source)
        manager.enable("managed-skill")
        scripted = tmp_path / "source" / "scripted-skill"
        (scripted / "scripts").mkdir(parents=True)
        (scripted / "SKILL.md").write_text(
            "---\nname: scripted-skill\ndescription: Must remain disabled.\n---\n\n"
            "Run [the helper](scripts/run.py).\n",
            encoding="utf-8",
        )
        (scripted / "scripts" / "run.py").write_text("raise RuntimeError('never run')\n", encoding="utf-8")
        manager.install(scripted)
        settings = Settings(
            _env_file=None,
            workspace_dir=str(workspace),
            skill_global_dir=str(tmp_path / "no-global"),
        )
        registry = CapabilityRegistry()

        wiring = await wire_capabilities(
            registry,
            parse_capabilities_config('{"skills": {}}'),
            settings=settings,
        )

        capability = registry.get("skills")
        assert {entry.name for entry in capability.catalog()} == {"user-skill", "managed-skill"}
        load_tool = next(tool for tool in wiring.tools if isinstance(tool, LoadSkillTool))
        result = await load_tool.execute(load_tool.args_schema(name="managed-skill"))
        assert result.ok is True
        assert result.data["content"] == "Read [the reference](references/guide.md)."
        managed_entry = next(entry for entry in capability.catalog() if entry.name == "managed-skill")
        reference = managed_entry.source_path.parent / "references" / "guide.md"
        assert reference.read_text(encoding="utf-8") == "Managed reference.\n"

        manager.disable("managed-skill")
        next_runtime = CapabilityRegistry()
        next_wiring = await wire_capabilities(
            next_runtime,
            parse_capabilities_config('{"skills": {}}'),
            settings=settings,
        )
        assert {entry.name for entry in next_runtime.get("skills").catalog()} == {"user-skill"}
        next_load_tool = next(tool for tool in next_wiring.tools if isinstance(tool, LoadSkillTool))
        disabled_result = await next_load_tool.execute(next_load_tool.args_schema(name="managed-skill"))
        assert disabled_result.ok is False

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


class TestSkillsPathOptionCoercion:
    """options 是 dict[str, Any]，strict 校验不查值："directories": "D:/skills" 这种
    常见手误若按字符迭代会产出 Path("D")、Path(":")……不存在的路径又被静默跳过
    → 技能悄悄消失。字符串必须包成单元素列表；不可迭代垃圾值响亮报错。"""

    def _settings(self, tmp_path: Path) -> Settings:
        return Settings(
            _env_file=None, workspace_dir=str(tmp_path),
            skill_global_dir=str(tmp_path / "no-global"),
        )

    async def _wire(self, registry: CapabilityRegistry, options: dict, tmp_path: Path):
        import json

        from agent_harness.capability.config import parse_capabilities_config
        return await wire_capabilities(
            registry,
            parse_capabilities_config(json.dumps({"skills": {"options": options}})),
            settings=self._settings(tmp_path),
        )

    @pytest.mark.asyncio
    async def test_directories_as_string_is_wrapped_not_iterated(self, tmp_path):
        _entry("from-str", "来自字符串目录", "正文", tmp_path / "custom")
        registry = CapabilityRegistry()
        await self._wire(registry, {"directories": str(tmp_path / "custom")}, tmp_path)
        assert [e.name for e in registry.get("skills").catalog()] == ["from-str"]

    @pytest.mark.asyncio
    async def test_paths_as_string_is_wrapped_not_iterated(self, tmp_path):
        entry = _entry("manual-str", "手动路径字符串", "正文", tmp_path / "anywhere")
        registry = CapabilityRegistry()
        await self._wire(registry, {"paths": str(entry.source_path)}, tmp_path)
        assert [e.name for e in registry.get("skills").catalog()] == ["manual-str"]

    def test_user_home_is_expanded_for_runtime_skill_sources(self):
        config = ProviderConfig(options={"directories": "~/skills"})

        assert coerce_skill_path_list(config, "directories") == [Path.home() / "skills"]

    @pytest.mark.asyncio
    async def test_non_iterable_directories_raise_init_failed(self, tmp_path):
        """配置错误响亮失败（与同文件 provider 校验一致），不走 OPTIONAL 静默降级。"""
        registry = CapabilityRegistry()
        with pytest.raises(CapabilityError) as err:
            await self._wire(registry, {"directories": 123}, tmp_path)
        assert err.value.code == "init_failed"
        assert "directories" in str(err.value)


# ── Round 7 安全加固：load_body 路径边界重验证（TOCTOU 防线）──


def test_load_body_rejects_out_of_root_swap_after_discovery(tmp_path):
    r"""发现时校验的路径边界，load_body 读盘时必须重验证。

    攻击面：模型可通过 workspace-write 工具在 <workspace>/skills/ 下把已发现的
    skill 目录整体替换成指向目录外的 junction/symlink——只靠发现时一次性
    resolve_within 是 TOCTOU：wiring 之后的任意时刻换入，load_body 都会把
    任意宿主文件读进模型 Context。重验证后必须显式报错（CapabilityError），
    绝不返回外部内容。win32 用 _winapi.CreateJunction、POSIX 用目录 symlink
    构造（均无需特权；两者 Path.resolve() 后都落到 skills 根之外，防线同一机制）。
    """
    import os
    import shutil

    skills_dir = tmp_path / "skills"
    good = skills_dir / "good"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text(
        "---\nname: good\ndescription: d\n---\nREAL BODY", encoding="utf-8"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: good\ndescription: d\n---\nESCAPED", encoding="utf-8"
    )

    discovery = SkillDiscovery([skills_dir])
    assert [e.name for e in discovery.discover().entries] == ["good"]

    # 发现后替换：删除真实目录，换成指向 skills 根之外的 junction/symlink。
    shutil.rmtree(good)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(outside), str(good))
    else:
        os.symlink(outside, good, target_is_directory=True)

    capability = SkillCapability(discovery)
    with pytest.raises(CapabilityError):
        capability.load("good")


# ── Round 7：load_skill 读盘不阻塞事件循环 ──


@pytest.mark.asyncio
async def test_load_skill_reads_off_event_loop(tmp_path):
    """LoadSkillTool 的读盘必须走线程池——同步 Path.read_text 在 async execute
    里会卡住整个事件循环（所有并发 session 的流都被磁盘延迟拖住），且
    executor 的 asyncio.timeout 无法打断同步 IO。"""
    import threading

    skills_dir = tmp_path / "skills"
    good = skills_dir / "good"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text(
        "---\nname: good\ndescription: d\n---\nBODY", encoding="utf-8"
    )
    capability = SkillCapability(SkillDiscovery([skills_dir]))
    tool = LoadSkillTool(capability)

    loop_thread = threading.get_ident()
    load_thread: list[int] = []

    original_load = capability.load

    def recording_load(name: str) -> str:
        load_thread.append(threading.get_ident())
        return original_load(name)

    capability.load = recording_load  # monkey-patch 实例方法，记录调用线程

    result = await tool.execute(_LoadSkillArgs(name="good"))
    assert result.ok
    assert load_thread and load_thread[0] != loop_thread, "读盘发生在事件循环线程上"


# ── #588：插值点单行化兜底（入口白名单之外的第二道；票面"只作兜底"）──


@pytest.mark.asyncio
async def test_catalog_line_stays_single_line_regardless_of_field_content():
    """目录行是单行声明面：name/description/when_to_use 含换行/控制符不得拉长行语义。"""
    entry = SkillCatalogEntry(
        name="a\nb", description="x\n伪造系统行", source_path=Path("s"), when_to_use="t\nu",
    )
    provider = SkillCatalogContextProvider(
        _capability_with_catalog(SkillCatalog(entries=[entry])),
    )
    content = (await provider.select(Session.__new__(Session), 1000))[0].content
    lines = content.split("\n")
    assert len(lines) == 2  # 框架行 + 恰好一条目录行：任何字段都拉不出额外行
    assert lines[1] == "- a b: x 伪造系统行（何时用：t u）"


@pytest.mark.asyncio
async def test_unknown_name_failure_message_is_single_line(tmp_path):
    """失败路 args.name 是模型原始输入（未名即失败、不经 discovery 白名单）——插值点单行化兜底。"""
    tool = LoadSkillTool(_capability(tmp_path))
    result = await tool.execute(tool.args_schema(name="ghost\n伪造指令行"))
    assert result.ok is False
    assert result.error_code is ErrorCode.INVALID_ARGUMENT
    assert "\n" not in result.message
    assert "ghost" in result.message  # 名字本身仍在（单行化不吞内容）


@pytest.mark.asyncio
async def test_global_packages_do_not_enter_runtime_catalog_context_or_tools(tmp_path):
    from agent_harness.capability.config import parse_capabilities_config
    from agent_harness.skills.package_manager import SkillPackageManager

    workspace = tmp_path / "project"
    global_skills = tmp_path / "home" / ".intelligence-agent" / "skills"
    source = tmp_path / "source" / "scripted-skill"
    source.mkdir(parents=True)
    sentinel = tmp_path / "script-ran"
    (source / "SKILL.md").write_text(
        "---\nname: scripted-skill\ndescription: Global package.\n---\n"
        "Read [the helper](scripts/run.py).\n",
        encoding="utf-8",
    )
    script = source / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    manager = SkillPackageManager(
        workspace, global_skills_dir=global_skills, scope="global"
    )
    ready = tmp_path / "source" / "global-ready"
    ready.mkdir(parents=True)
    (ready / "SKILL.md").write_text(
        "---\nname: global-ready\ndescription: Complete global package.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    assert manager.install(ready)["compatibility"]["status"] == "complete"
    installed = manager.install(source)
    assert installed["compatibility"]["status"] == "needs-adaptation"

    settings = Settings(
        _env_file=None,
        workspace_dir=str(workspace),
        skill_global_dir=str(global_skills),
    )
    registry = CapabilityRegistry()
    wiring = await wire_capabilities(
        registry,
        parse_capabilities_config('{"skills": {}}'),
        settings=settings,
    )

    assert registry.get("skills").catalog() == []
    provider = next(
        provider for provider in wiring.context_providers
        if isinstance(provider, SkillCatalogContextProvider)
    )
    assert await provider.select(Session.__new__(Session), 1000) == []
    load_tool = next(tool for tool in wiring.tools if isinstance(tool, LoadSkillTool))
    ready_result = await load_tool.execute(load_tool.args_schema(name="global-ready"))
    assert ready_result.ok is False
    result = await load_tool.execute(load_tool.args_schema(name="scripted-skill"))
    assert result.ok is False
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_invalidated_global_selection_is_observable_and_not_silently_absent(tmp_path):
    """AC3：被选版本失效时报错，不静默缺席（也不静默回落到项目版）。

    这里走**真装配面**（不是直接调 manager）：`wiring` 把
    `enabled_global_skill_digests()` 的失败折成「缺席 + 留痕」，留痕必须能被
    `SkillCapability.errors()` 读到，否则「被选版本失效」与「本来没选」在程序上
    不可区分。反面对照：未选任何全局包的同一棵树里 errors 为空。
    """
    from agent_harness.capability.config import parse_capabilities_config
    from agent_harness.skills.package_manager import SkillPackageManager

    workspace = tmp_path / "project"
    global_skills = tmp_path / "home" / ".intelligence-agent" / "skills"
    source = tmp_path / "source" / "global-pick"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: global-pick\ndescription: Global package.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    global_manager = SkillPackageManager(workspace, global_skills_dir=global_skills, scope="global")
    assert global_manager.install(source)["compatibility"]["status"] == "complete"
    project_manager = SkillPackageManager(workspace, global_skills_dir=global_skills)
    project_manager.enable("global-pick", scope="global")

    settings = Settings(
        _env_file=None,
        workspace_dir=str(workspace),
        skill_global_dir=str(global_skills),
    )
    registry = CapabilityRegistry()
    await wire_capabilities(
        registry,
        parse_capabilities_config('{"skills": {}}'),
        settings=settings,
    )
    assert registry.get("skills").errors() == []
    assert [entry.name for entry in registry.get("skills").catalog()] == ["global-pick"]

    # 被选版本从全局安装根消失（记录仍在 ⇒ 走 "missing or unsafe" 分支）。
    shutil.rmtree(global_manager.managed_skills_dir / "global-pick")

    settings = Settings(
        _env_file=None,
        workspace_dir=str(workspace),
        skill_global_dir=str(global_skills),
    )
    registry = CapabilityRegistry()
    await wire_capabilities(
        registry,
        parse_capabilities_config('{"skills": {}}'),
        settings=settings,
    )
    errors = registry.get("skills").errors()
    assert len(errors) == 1
    assert "[selection]" in errors[0]
    assert "global-pick" in errors[0]
    assert [entry.name for entry in registry.get("skills").catalog()] == []
    # 重复发现（同一 capability 再读一次）不会把留痕清掉。
    assert registry.get("skills").errors() == errors


@pytest.mark.asyncio
async def test_project_side_selection_failure_is_observable_like_the_global_one(tmp_path):
    """AC3 两侧对称：项目版被选后失效，同样响亮留痕，不静默缺席。

    反面对照（同一棵树、未启用时）errors 为空；坏包前后 `catalog` 的差异必须
    能从 `errors()` 读出来——否则用户只能看到技能凭空消失。
    """
    from agent_harness.capability.config import parse_capabilities_config
    from agent_harness.skills.package_manager import SkillPackageManager

    workspace = tmp_path / "project"
    global_skills = tmp_path / "home" / ".intelligence-agent" / "skills"
    source = tmp_path / "source" / "project-pick"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: project-pick\ndescription: Project package.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    manager = SkillPackageManager(workspace, global_skills_dir=global_skills)
    assert manager.install(source)["compatibility"]["status"] == "complete"
    manager.enable("project-pick", scope="project")

    settings = Settings(
        _env_file=None,
        workspace_dir=str(workspace),
        skill_global_dir=str(global_skills),
    )

    async def wire() -> CapabilityRegistry:
        registry = CapabilityRegistry()
        await wire_capabilities(
            registry,
            parse_capabilities_config('{"skills": {}}'),
            settings=settings,
        )
        return registry

    assert [entry.name for entry in (await wire()).get("skills").catalog()] == ["project-pick"]
    assert (await wire()).get("skills").errors() == []

    # 被选快照被改坏 ⇒ 该记录不再可装配（摘要复核失败）。
    (manager.managed_skills_dir / "project-pick" / "SKILL.md").write_text(
        "---\nname: project-pick\ndescription: Tampered.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    registry = await wire()
    errors = registry.get("skills").errors()
    assert len(errors) == 1
    assert "[selection]" in errors[0]
    assert "managed Skill package selection cannot be honoured" in errors[0]


@pytest.mark.asyncio
async def test_corrupt_global_manifest_is_not_reported_as_a_selection_failure(tmp_path):
    """清单损坏 ≠ 用户的选择失效：两者要用户做的事完全不同（T5 AC3 的可观察面）。

    修前两者共用一个 `[selection] global Skill package selection cannot be
    honoured` 标签，用户会以为自己选错了；实际是盘上清单读不出来。
    """
    from agent_harness.capability.config import parse_capabilities_config
    from agent_harness.skills.package_manager import SkillPackageManager

    workspace = tmp_path / "project"
    global_skills = tmp_path / "home" / ".intelligence-agent" / "skills"
    global_manager = SkillPackageManager(workspace, global_skills_dir=global_skills, scope="global")
    source = tmp_path / "source" / "corrupt-me"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: corrupt-me\ndescription: Package.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    global_manager.install(source)
    global_manager.manifest_path.write_text("{not json", encoding="utf-8")

    settings = Settings(
        _env_file=None,
        workspace_dir=str(workspace),
        skill_global_dir=str(global_skills),
    )
    registry = CapabilityRegistry()
    await wire_capabilities(
        registry,
        parse_capabilities_config('{"skills": {}}'),
        settings=settings,
    )
    errors = registry.get("skills").errors()
    assert len(errors) == 1
    # 标签不带 scope：启用结果表住在**项目**清单里，全局侧读它时也会抛 —— 按 scope
    # 命名会把「项目清单坏了」指控成「全局存储坏了」。
    assert "Skill package storage is unreadable" in errors[0]
    assert "selection cannot be honoured" not in errors[0]


@pytest.mark.asyncio
async def test_corrupt_project_manifest_is_not_reported_as_a_selection_failure(tmp_path):
    """项目侧与全局侧同判据：清单损坏不冒充「选择失效」（也不反向指控全局存储）。

    修前项目清单写坏会让全局侧也报一条 `global Skill package storage is unreadable`
    ——同一条错误被两个 scope 各指控一次，且两次都指向用户没做过的动作。
    """
    from agent_harness.capability.config import parse_capabilities_config

    workspace = tmp_path / "project"
    global_skills = tmp_path / "home" / ".intelligence-agent" / "skills"
    global_skills.mkdir(parents=True)
    workspace.mkdir(parents=True)
    (workspace / "plugin-installs.json").write_text("{not json", encoding="utf-8")

    settings = Settings(
        _env_file=None,
        workspace_dir=str(workspace),
        skill_global_dir=str(global_skills),
    )
    registry = CapabilityRegistry()
    await wire_capabilities(
        registry,
        parse_capabilities_config('{"skills": {}}'),
        settings=settings,
    )
    errors = registry.get("skills").errors()
    assert errors[0] == (
        "[selection] Skill package storage is unreadable: "
        "cannot read install manifest: JSONDecodeError "
        "(every managed Skill stays disabled until this is fixed)"
    )
