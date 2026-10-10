"""#874 T5：全局包逐项目启用与同 ID 显式启用结果（spec 08 §6.2 / ADR-0052 D3）。

每条 AC 都配正反对照：AC1 逐项目生效 + 跨项目零增量；AC2 同 ID 未选择拒启用、
显式选择后只装配一版；AC3 禁用/升级/回退不静默换范围、被选版本失效报错不 shadow；
AC4 全局贡献需显式启用且不因包选择提权。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import wire_capabilities
from agent_harness.config import Settings
from agent_harness.skills.capability import SkillCapability
from agent_harness.skills.package_manager import SkillPackageError, SkillPackageManager

PACKAGE_NAME = "shared-skill"


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _package(root: Path, body: str = "Body.", *, name: str = PACKAGE_NAME) -> Path:
    package = root / PACKAGE_NAME
    _write(
        package / "SKILL.md",
        f"---\nname: {name}\ndescription: Shared example.\n---\n\n{body}\n",
    )
    return package


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_repo(root: Path) -> tuple[Path, str, str]:
    repo = root / "repo"
    package = repo / "packages" / PACKAGE_NAME
    _write(
        package / "SKILL.md",
        f"---\nname: {PACKAGE_NAME}\ndescription: Versioned.\n---\n\nOld body.\n",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "old version")
    old_commit = _git(repo, "rev-parse", "HEAD")
    _write(
        package / "SKILL.md",
        f"---\nname: {PACKAGE_NAME}\ndescription: Versioned.\n---\n\nNew body.\n",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "new version")
    return repo, old_commit, _git(repo, "rev-parse", "HEAD")


class _World:
    """一个全局安装根 + 两个项目（T5「其他项目不受影响」的对照面）。"""

    def __init__(self, tmp_path: Path) -> None:
        self.global_dir = tmp_path / "home" / ".intelligence-agent" / "skills"
        self.alpha_dir = tmp_path / "alpha"
        self.beta_dir = tmp_path / "beta"

    def workspace(self, name: str = "alpha") -> Path:
        return self.alpha_dir if name == "alpha" else self.beta_dir

    def project(self, name: str = "alpha") -> SkillPackageManager:
        return SkillPackageManager(self.workspace(name), global_skills_dir=self.global_dir)

    def global_(self, name: str = "alpha") -> SkillPackageManager:
        return SkillPackageManager(
            self.workspace(name), scope="global", global_skills_dir=self.global_dir
        )

    async def capability(self, name: str = "alpha") -> SkillCapability:
        """按本项目装配面接线（与 Runtime 走同一条 _wire_skills）。"""
        settings = Settings(
            _env_file=None,
            workspace_dir=str(self.workspace(name)),
            skill_global_dir=str(self.global_dir),
        )
        registry = CapabilityRegistry()
        await wire_capabilities(
            registry,
            parse_capabilities_config('{"skills": {}}'),
            settings=settings,
        )
        return registry.get("skills")

    async def catalog_names(self, name: str = "alpha") -> list[str]:
        return [entry.name for entry in (await self.capability(name)).catalog()]

    async def bodies(self, name: str = "alpha") -> dict[str, str]:
        return {
            entry.name: entry.load_body().strip()
            for entry in (await self.capability(name)).catalog()
        }


@pytest.mark.asyncio
async def test_global_package_enabled_per_project_only_there(tmp_path: Path) -> None:
    """`enable <id> --scope global` 只在该项目生效；其他项目零增量（T5 AC1）。"""
    world = _World(tmp_path)
    world.global_().install(_package(tmp_path / "src"))

    # 启用前：两个项目都看不见任何托管内容（对照面）。
    assert await world.catalog_names("alpha") == []
    assert await world.catalog_names("beta") == []

    world.project("alpha").enable(PACKAGE_NAME, scope="global")

    assert await world.catalog_names("alpha") == [PACKAGE_NAME]
    assert await world.catalog_names("beta") == []
    assert world.project("beta").enable_results() == {}
    # 未选择的项目只是「可见可启用」：记录在全局安装根里，装配面零增量。
    assert set(world.global_().list_packages()) == {PACKAGE_NAME}


@pytest.mark.asyncio
async def test_existing_and_new_sessions_agree_on_selected_scope(tmp_path: Path) -> None:
    """已选状态与新建 Runtime 看到的一致（T5 AC1：已有/新会话状态一致）。"""
    world = _World(tmp_path)
    world.global_().install(_package(tmp_path / "src"))
    existing = world.project("alpha")
    existing.enable(PACKAGE_NAME, scope="global")
    before = existing.enabled_global_skill_digests()

    # 新会话 = 重新构造 manager/capability，读同一份落盘选择。
    fresh = world.project("alpha")
    assert fresh.enable_results()[PACKAGE_NAME]["selected_scope"] == "global"
    assert fresh.enabled_global_skill_digests() == before
    assert await world.catalog_names("alpha") == [PACKAGE_NAME]

    # 另一个项目独立启用后，两边的装配结果各自收敛于自己的选择。
    _World(tmp_path).project("beta").enable(PACKAGE_NAME, scope="global")
    assert await world.catalog_names("alpha") == [PACKAGE_NAME]
    assert await world.catalog_names("beta") == [PACKAGE_NAME]


@pytest.mark.parametrize("install_global_first", [False, True])
def test_same_id_both_scopes_refuses_without_explicit_choice(
    tmp_path: Path, install_global_first: bool
) -> None:
    """同 ID 并存未选择 ⇒ 拒绝启用并列出两项来源/版本（T5 AC2，与安装顺序无关）。"""
    world = _World(tmp_path)
    project_source = _package(tmp_path / "project-src", body="Project body.")
    global_source = _package(tmp_path / "global-src", body="Global body.")
    if install_global_first:
        world.global_().install(global_source)
        world.project("alpha").install(project_source)
    else:
        world.project("alpha").install(project_source)
        world.global_().install(global_source)

    with pytest.raises(SkillPackageError) as excinfo:
        world.project("alpha").enable(PACKAGE_NAME)

    message = str(excinfo.value)
    assert "both project and global scope" in message
    assert str(project_source) in message
    assert str(global_source) in message
    # 拒绝后不留下任何启用结果：两个 scope 依旧都可选。
    assert world.project("alpha").enable_results() == {}


@pytest.mark.asyncio
async def test_explicit_selection_assembles_only_the_chosen_version(tmp_path: Path) -> None:
    """显式选择后只装配所选一版（T5 AC2）。"""
    world = _World(tmp_path)
    project = world.project("alpha")
    project.install(_package(tmp_path / "project-src", body="Project body."))
    world.global_().install(_package(tmp_path / "global-src", body="Global body."))

    project.enable(PACKAGE_NAME, scope="project")
    assert await world.bodies("alpha") == {PACKAGE_NAME: "Project body."}

    # 改选 global：只装配全局那一版，项目版不再进 catalog。
    project.enable(PACKAGE_NAME, scope="global")
    assert project.enable_results()[PACKAGE_NAME]["selected_scope"] == "global"
    assert await world.bodies("alpha") == {PACKAGE_NAME: "Global body."}
    # 两个 scope 的内容都还在原处（启用不改记录）。
    assert set(project.list_packages()) == {PACKAGE_NAME}
    assert set(world.global_().list_packages()) == {PACKAGE_NAME}


def test_single_scope_needs_no_explicit_choice(tmp_path: Path) -> None:
    """只有一个 scope 有该 ID 时无需 --scope；显式点另一个 scope 报错。"""
    world = _World(tmp_path)
    world.global_().install(_package(tmp_path / "src"))

    world.project("alpha").enable(PACKAGE_NAME)
    assert world.project("alpha").enable_results()[PACKAGE_NAME]["selected_scope"] == "global"

    with pytest.raises(SkillPackageError, match="no project version"):
        world.project("beta").enable(PACKAGE_NAME, scope="project")
    assert world.project("beta").enable_results() == {}


def test_enable_uses_the_package_id_and_records_its_source(tmp_path: Path) -> None:
    """`enable <id>` 的 id 即包 ID；启用结果记下来源，未知 id 响亮失败不静默。"""
    world = _World(tmp_path)
    source = _package(tmp_path / "src")
    world.global_().install(source)
    project = world.project("alpha")

    project.enable(PACKAGE_NAME)
    record = world.global_().list_packages()[PACKAGE_NAME]
    assert project.enable_results()[PACKAGE_NAME] == {
        "selected_scope": "global",
        "source": str(source),
        "version": record["sha256"],
    }

    with pytest.raises(SkillPackageError, match="not installed"):
        project.enable("ghost-skill")
    assert set(project.enable_results()) == {PACKAGE_NAME}


@pytest.mark.asyncio
async def test_selected_global_version_removed_errors_without_shadowing(
    tmp_path: Path,
) -> None:
    """被选版本失效 ⇒ 报错，不回落项目版（T5 AC3：不自动 shadow）。"""
    world = _World(tmp_path)
    project = world.project("alpha")
    project.install(_package(tmp_path / "project-src", body="Project body."))
    world.global_().install(_package(tmp_path / "src", body="Global body."))
    project.enable(PACKAGE_NAME, scope="global")
    world.global_().remove(PACKAGE_NAME)

    with pytest.raises(SkillPackageError, match="no longer exists"):
        project.enabled_global_skill_digests()
    # 装配面不因此启动失败（spec 08 §6.3），但该贡献缺席、且不回落到项目版。
    assert await world.catalog_names("alpha") == []

    # 对照：显式改选 project 后才装配项目版（启用结果必须由人给出，不自动发生）。
    with pytest.raises(SkillPackageError, match="no global version"):
        project.enable(PACKAGE_NAME, scope="global")
    project.enable(PACKAGE_NAME, scope="project")
    assert await world.bodies("alpha") == {PACKAGE_NAME: "Project body."}


@pytest.mark.asyncio
async def test_selected_global_snapshot_tampered_errors_without_shadowing(
    tmp_path: Path,
) -> None:
    """被选全局包的正文被改 ⇒ 报错，不静默回落项目版（T5 AC3）。"""
    world = _World(tmp_path)
    project = world.project("alpha")
    project.install(_package(tmp_path / "project-src", body="Project body."))
    world.global_().install(_package(tmp_path / "src", body="Global body."))
    project.enable(PACKAGE_NAME, scope="global")

    _write(
        world.global_().managed_skills_dir / PACKAGE_NAME / "SKILL.md",
        f"---\nname: {PACKAGE_NAME}\ndescription: Tampered.\n---\n\nX\n",
    )

    with pytest.raises(SkillPackageError):
        project.enabled_global_skill_digests()
    assert await world.catalog_names("alpha") == []


@pytest.mark.asyncio
async def test_upgrade_and_rollback_do_not_silently_switch_scope(tmp_path: Path) -> None:
    """升级/回退所选 scope 不改启用结果、不切到另一 scope（T5 AC3）。"""
    world = _World(tmp_path)
    project = world.project("alpha")
    project.install(_package(tmp_path / "project-src", body="Project body."))
    repo, old_commit, new_commit = _git_repo(tmp_path)
    global_manager = world.global_()
    global_manager.install_git(
        repo.as_uri(), ref=old_commit, subdirectory=f"packages/{PACKAGE_NAME}"
    )
    project.enable(PACKAGE_NAME, scope="global")

    global_manager.update(PACKAGE_NAME, ref=new_commit)
    # 待重启版本与运行版本分别可见，运行版本此刻仍是旧 commit（T4 AC3）。
    record = global_manager.list_packages()[PACKAGE_NAME]
    assert record["pending_version"]["resolved_commit"] == new_commit
    assert record["resolved_commit"] == old_commit

    global_manager.apply_pending_versions()

    assert project.enable_results()[PACKAGE_NAME]["selected_scope"] == "global"
    assert await world.bodies("alpha") == {PACKAGE_NAME: "New body."}

    global_manager.rollback(PACKAGE_NAME)
    global_manager.apply_pending_versions()

    assert project.enable_results()[PACKAGE_NAME]["selected_scope"] == "global"
    assert await world.bodies("alpha") == {PACKAGE_NAME: "Old body."}


@pytest.mark.asyncio
async def test_disabling_does_not_fall_back_to_the_other_scope(tmp_path: Path) -> None:
    """停用一个 scope 不静默切到另一 scope（T5 AC3）。"""
    world = _World(tmp_path)
    project = world.project("alpha")
    project.install(_package(tmp_path / "project-src", body="Project body."))
    world.global_().install(_package(tmp_path / "src", body="Global body."))
    project.enable(PACKAGE_NAME, scope="global")
    assert await world.bodies("alpha") == {PACKAGE_NAME: "Global body."}

    project.disable(PACKAGE_NAME)

    assert project.enable_results() == {}
    assert await world.catalog_names("alpha") == []  # 项目版没有被顺手启用
    assert set(project.list_packages()) == {PACKAGE_NAME}


def test_remove_refuses_to_orphan_an_enable_result(tmp_path: Path) -> None:
    """移除项目版不会让已选的全局启用结果变成隐式回落（删除面）。"""
    world = _World(tmp_path)
    project = world.project("alpha")
    project.install(_package(tmp_path / "project-src"))
    world.global_().install(_package(tmp_path / "src"))
    project.enable(PACKAGE_NAME, scope="global")

    with pytest.raises(SkillPackageError, match="selected from global scope"):
        project.remove(PACKAGE_NAME)

    project.enable(PACKAGE_NAME, scope="project")
    project.remove(PACKAGE_NAME)
    assert project.enable_results() == {}


def test_enable_state_cannot_be_written_by_the_global_manager(tmp_path: Path) -> None:
    """启用状态属于项目：全局 manager 不能替任何项目做选择（T5 AC1）。"""
    world = _World(tmp_path)
    world.global_().install(_package(tmp_path / "src"))

    with pytest.raises(SkillPackageError, match="enabled per project"):
        world.global_().enable(PACKAGE_NAME, scope="global")
    with pytest.raises(SkillPackageError, match="disabled per project"):
        world.global_().disable(PACKAGE_NAME)


@pytest.mark.asyncio
async def test_global_selection_does_not_escalate_permissions_or_tools(
    tmp_path: Path,
) -> None:
    """包声明权限不构成提权：启用不改工具面（T5 AC4）。"""
    world = _World(tmp_path)
    clean = _package(tmp_path / "clean-src")
    _package(tmp_path / "declaring-src")
    _write(
        tmp_path / "declaring-src" / PACKAGE_NAME / "SKILL.md",
        f"---\nname: {PACKAGE_NAME}\ndescription: Shared.\n"
        f"allowed-tools: Bash(rm -rf /) Write\n---\n\nBody.\n",
    )
    global_manager = world.global_()
    global_manager.install(clean)
    project = world.project("alpha")
    before = [tool.name for tool in (await world.capability("alpha")).contributes_tools()]

    project.enable(PACKAGE_NAME, scope="global")
    capability = await world.capability("alpha")

    assert [entry.name for entry in capability.catalog()] == [PACKAGE_NAME]
    # 启用前后工具面逐项相同：装配面只有既有通用工具，没有任何包自带的入口。
    assert [tool.name for tool in capability.contributes_tools()] == before
    assert before[:1] == ["load_skill"]
    # 包声明的 allowed-tools 不改信任状态：仍是未信任，等待人工处理。
    assert global_manager.list_packages()[PACKAGE_NAME]["trust"]["status"] == "untrusted"

    # 对照：同一包一旦声明权限（allowed-tools）就停在 needs-adaptation，根本启用不了。
    global_manager.remove(PACKAGE_NAME)
    global_manager.install(tmp_path / "declaring-src" / PACKAGE_NAME)
    declared = global_manager.list_packages()[PACKAGE_NAME]
    assert declared["compatibility"]["status"] == "needs-adaptation"
    assert {item["kind"] for item in declared["compatibility"]["requirements"]} == {
        "tool-permission"
    }
    with pytest.raises(SkillPackageError, match="needs-adaptation"):
        project.enable(PACKAGE_NAME, scope="global")
    # 已保存的启用结果指向的全局版本退化 ⇒ 装配响亮失败，不静默换成别的版本。
    with pytest.raises(SkillPackageError, match="needs-adaptation"):
        project.enabled_global_skill_digests()


@pytest.mark.asyncio
async def test_global_package_with_scripts_cannot_be_enabled_at_all(tmp_path: Path) -> None:
    """带脚本的全局包停在 needs-adaptation：启不了，脚本永不执行（T5 AC4）。"""
    world = _World(tmp_path)
    package = _package(tmp_path / "src")
    _write(package / "scripts" / "run.sh", "#!/bin/sh\necho should-never-run\n")
    world.global_().install(package)

    record = world.global_().list_packages()[PACKAGE_NAME]
    requirements = record["compatibility"]["requirements"]
    assert {item["kind"] for item in requirements} == {"script-runtime"}
    assert all(item["support"] == "manual_review" for item in requirements)

    with pytest.raises(SkillPackageError, match="needs-adaptation"):
        world.project("alpha").enable(PACKAGE_NAME, scope="global")
    assert await world.catalog_names("alpha") == []


def test_t4_manifest_without_enable_results_keeps_project_packages(tmp_path: Path) -> None:
    """升级兼容：T4 清单只有 enabled 位，读侧要按「项目选择」补齐（T5 AC1）。

    不补的话，升级后已启用的项目包会在装配面静默消失——`enabled` 位还在，
    但新的读路径只认启用结果表。MANIFEST_VERSION 不变，属版本 1 内的字段演进。
    """
    import json

    world = _World(tmp_path)
    project = world.project("alpha")
    project.install(_package(tmp_path / "src"))
    project.enable(PACKAGE_NAME, scope="project")

    # 回写成 T4 形状：只有 enabled 位、没有启用结果表。
    manifest = json.loads(project.manifest_path.read_text(encoding="utf-8"))
    manifest["packages"][PACKAGE_NAME]["enabled"] = True
    del manifest["enable_results"]
    project.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    fresh = world.project("alpha")
    assert fresh.enable_results()[PACKAGE_NAME]["selected_scope"] == "project"
    assert set(fresh.enabled_skill_digests()) == {PACKAGE_NAME}

    # 对照：没启用的旧记录不该被补成「已选择」。
    manifest["packages"][PACKAGE_NAME]["enabled"] = False
    project.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert world.project("alpha").enable_results() == {}
