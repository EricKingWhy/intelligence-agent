from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from agent_harness.skills import inspection, package_lock, package_manager
from agent_harness.skills.capability import SkillCapability
from agent_harness.skills.discovery import SkillDiscovery, parse_skill_markdown_text
from agent_harness.skills.package_manager import SkillPackageError, SkillPackageManager
from agent_harness.skills.tool import LoadSkillTool


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def _complete_package(root: Path, name: str = "sample-skill") -> Path:
    package = root / name
    _write(
        package / "SKILL.md",
        "---\nname: sample-skill\ndescription: Local example.\n---\n\n"
        "Read [the guide](references/guide.md) and [the icon](assets/icon.bin).\n",
    )
    _write(package / "references" / "guide.md", "Guide content.\n")
    _write(package / "assets" / "icon.bin", b"\x00\x01asset")
    return package


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _git_skill_repo(root: Path) -> tuple[Path, str, str]:
    repo = root / "repo"
    package = repo / "packages" / "sample-skill"
    _write(
        package / "SKILL.md",
        "---\nname: sample-skill\ndescription: Versioned example.\n---\n\n"
        "Read [the guide](references/guide.md). Old body.\n",
    )
    _write(package / "references" / "guide.md", "Old resource.\n")
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "old version")
    old_commit = _git(repo, "rev-parse", "HEAD")

    _write(
        package / "SKILL.md",
        "---\nname: sample-skill\ndescription: Versioned example.\n---\n\n"
        "Read [the guide](references/guide.md). New body.\n",
    )
    _write(package / "references" / "guide.md", "New resource.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "new version")
    new_commit = _git(repo, "rev-parse", "HEAD")
    return repo, old_commit, new_commit


def _load_installed_skill(manager: SkillPackageManager) -> tuple[str, Path]:
    digests = manager.enabled_skill_digests()
    discovery = SkillDiscovery(
        directories=[manager.project_skills_dir, manager.managed_skills_dir],
        project_dir=manager.project_skills_dir,
        managed_directories={"project": manager.managed_skills_dir},
        enabled_managed_digests={"project": digests},
    )
    tool = LoadSkillTool(SkillCapability(discovery))
    result = asyncio.run(tool.execute(tool.args_schema(name="sample-skill")))
    assert result.data is not None
    return result.data["content"], Path(result.data["resource_root"])


def test_git_skill_update_and_rollback_keep_complete_snapshots(tmp_path: Path) -> None:
    repo, old_commit, new_commit = _git_skill_repo(tmp_path)
    manager = SkillPackageManager(tmp_path / "workspace")

    installed = manager.install_git(repo.as_uri(), ref=old_commit, subdirectory="packages/sample-skill")
    assert installed["source"] == repo.as_uri()
    assert installed["resolved_commit"] == old_commit
    assert installed["enabled"] is False
    assert installed["sha256"] == package_manager._git_tree_sha256(
        manager.managed_skills_dir / "sample-skill", installed["git_modes_sha256"]
    )

    manager.enable("sample-skill")
    old_body, old_root = _load_installed_skill(manager)
    assert "Old body." in old_body
    assert (old_root / "references" / "guide.md").read_text(encoding="utf-8") == "Old resource.\n"

    pending = manager.update("sample-skill", ref=new_commit)
    assert pending["pending_version"]["resolved_commit"] == new_commit
    assert (manager.managed_skills_dir / "sample-skill" / "references" / "guide.md").read_text(
        encoding="utf-8"
    ) == "Old resource.\n"

    manager.apply_pending_versions()
    current = manager.list_packages()["sample-skill"]
    assert current["resolved_commit"] == new_commit
    assert current["previous_version"]["resolved_commit"] == old_commit
    new_body, new_root = _load_installed_skill(manager)
    assert "New body." in new_body
    assert (new_root / "references" / "guide.md").read_text(encoding="utf-8") == "New resource.\n"

    rollback = manager.rollback("sample-skill")
    assert rollback["pending_version"]["resolved_commit"] == old_commit
    assert (manager.managed_skills_dir / "sample-skill" / "references" / "guide.md").read_text(
        encoding="utf-8"
    ) == "New resource.\n"

    manager.apply_pending_versions()
    restored = manager.list_packages()["sample-skill"]
    assert restored["resolved_commit"] == old_commit
    assert restored["previous_version"]["resolved_commit"] == new_commit
    old_body, old_root = _load_installed_skill(manager)
    assert "Old body." in old_body
    assert (old_root / "references" / "guide.md").read_text(encoding="utf-8") == "Old resource.\n"

    manager.remove("sample-skill")
    assert not (manager.managed_skills_dir / "sample-skill").exists()
    assert not (manager.managed_versions_dir / "sample-skill").exists()


def test_git_skill_update_failure_preserves_manifest_and_active_snapshot(tmp_path: Path) -> None:
    repo, old_commit, _new_commit = _git_skill_repo(tmp_path)
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install_git(repo.as_uri(), ref=old_commit, subdirectory="packages/sample-skill")
    active = manager.managed_skills_dir / "sample-skill"
    before_manifest = manager.manifest_path.read_bytes()
    record = manager.list_packages()["sample-skill"]
    before_tree = package_manager._git_tree_sha256(active, record["git_modes_sha256"])

    with pytest.raises(SkillPackageError, match="fetch"):
        manager.update("sample-skill", ref="missing-ref")

    assert manager.manifest_path.read_bytes() == before_manifest
    assert package_manager._git_tree_sha256(active, record["git_modes_sha256"]) == before_tree


def test_git_skill_activation_io_failure_keeps_old_skill_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, old_commit, new_commit = _git_skill_repo(tmp_path)
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install_git(repo.as_uri(), ref=old_commit, subdirectory="packages/sample-skill")
    manager.enable("sample-skill")
    manager.update("sample-skill", ref=new_commit)

    real_copytree = shutil.copytree
    def fail_staging_copy(source: Path, destination: Path, *args, **kwargs):
        if Path(destination).parent.name.startswith(".staging-"):
            raise OSError("simulated disk full")
        return real_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(package_manager.shutil, "copytree", fail_staging_copy)

    with pytest.raises(SkillPackageError, match="pending Git Skill activation failed"):
        manager.apply_pending_versions()

    current = manager.list_packages()["sample-skill"]
    assert current["resolved_commit"] == old_commit
    assert current["pending_version"]["resolved_commit"] == new_commit
    content, resource_root = _load_installed_skill(manager)
    assert "Old body." in content
    assert (resource_root / "references" / "guide.md").read_text(encoding="utf-8") == "Old resource.\n"


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose Git executable bits in st_mode")
def test_git_skill_install_preserves_and_verifies_executable_mode(tmp_path: Path) -> None:
    repo, _old_commit, _new_commit = _git_skill_repo(tmp_path)
    _write(repo / "packages" / "sample-skill" / "references" / "run.sh", "#!/bin/sh\necho ready\n")
    _git(repo, "add", ".")
    _git(repo, "update-index", "--chmod=+x", "packages/sample-skill/references/run.sh")
    _git(repo, "commit", "-qm", "add executable resource")
    commit = _git(repo, "rev-parse", "HEAD")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install_git(repo.as_uri(), ref=commit, subdirectory="packages/sample-skill")

    script = manager.managed_skills_dir / "sample-skill" / "references" / "run.sh"
    assert script.stat().st_mode & 0o111
    script.chmod(0o644)
    record = manager.list_packages()["sample-skill"]
    with pytest.raises(SkillPackageError, match="changed after installation"):
        manager._verify_package(manager.managed_skills_dir / "sample-skill", record)
    assert not (manager.managed_skills_dir / ".pending" / "sample-skill").exists()


def test_git_skill_rejects_credentials_and_unsafe_subdirectory(tmp_path: Path) -> None:
    manager = SkillPackageManager(tmp_path / "workspace")

    with pytest.raises(SkillPackageError) as credentials_error:
        manager.install_git(
            "https://user:secret-token@example.invalid/repo.git",
            ref="main",
            subdirectory="packages/sample-skill",
        )
    assert "secret-token" not in str(credentials_error.value)
    assert manager.list_packages() == {}

    with pytest.raises(SkillPackageError, match="subdirectory"):
        manager.install_git(
            "https://example.invalid/repo.git",
            ref="main",
            subdirectory="../../outside",
        )
    assert manager.list_packages() == {}


def test_git_skill_install_never_runs_repository_scripts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "packages" / "scripted-skill"
    marker = tmp_path / "script-ran"
    _write(
        skill / "SKILL.md",
        "---\nname: scripted-skill\ndescription: Script must stay inert.\n---\n\n"
        "Run [the helper](scripts/run.py).\n",
    )
    _write(
        skill / "scripts" / "run.py",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
    )
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "scripted skill")
    commit = _git(repo, "rev-parse", "HEAD")

    manager = SkillPackageManager(tmp_path / "workspace")
    installed = manager.install_git(
        repo.as_uri(), ref=commit, subdirectory="packages/scripted-skill"
    )

    assert installed["compatibility"]["status"] == "needs-adaptation"
    assert installed["enabled"] is False
    assert not marker.exists()


def test_git_manifest_rejects_tampered_version_metadata(tmp_path: Path) -> None:
    repo, old_commit, new_commit = _git_skill_repo(tmp_path)
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install_git(repo.as_uri(), ref=old_commit, subdirectory="packages/sample-skill")
    manager.update("sample-skill", ref=new_commit)

    manifest = json.loads(manager.manifest_path.read_text(encoding="utf-8"))
    manifest["packages"]["sample-skill"]["pending_version"]["source_ref"] = "../secret"
    manager.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SkillPackageError, match="manifest record") as error:
        manager.list_packages()
    assert "secret" not in str(error.value)


def test_install_snapshots_complete_package_and_enables_only_after_explicit_selection(
    tmp_path: Path,
) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")

    record = manager.install(source)

    installed = manager.managed_skills_dir / "sample-skill"
    assert record["scope"] == "project"
    assert record["source"] == str(source.resolve())
    assert record["compatibility"]["status"] == "complete"
    assert record["trust"] == {"status": "untrusted", "sha256": record["sha256"]}
    assert record["enabled"] is False
    assert (installed / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    assert (installed / "references" / "guide.md").read_bytes() == (
        source / "references" / "guide.md"
    ).read_bytes()
    assert (installed / "assets" / "icon.bin").read_bytes() == b"\x00\x01asset"
    assert manager.enabled_skill_names() == set()

    manager.enable("sample-skill")
    restarted = SkillPackageManager(tmp_path / "workspace")
    discovery = SkillDiscovery(
        directories=[restarted.project_skills_dir, restarted.managed_skills_dir],
        project_dir=restarted.project_skills_dir,
        managed_directories={"project": restarted.managed_skills_dir},
        enabled_managed_digests={"project": restarted.enabled_skill_digests()},
    )
    catalog = discovery.discover()

    assert [entry.name for entry in catalog.entries] == ["sample-skill"]
    assert catalog.entries[0].load_body() == (
        "Read [the guide](references/guide.md) and [the icon](assets/icon.bin)."
    )
    assert (catalog.entries[0].source_path.parent / "references" / "guide.md").read_text(
        encoding="utf-8"
    ) == "Guide content.\n"


def test_install_rejects_name_change_between_preflight_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    tree_sha256 = package_manager._tree_sha256

    def change_name_before_hash(root: Path) -> str:
        if root == source:
            (source / "SKILL.md").write_text(
                "---\nname: other-skill\ndescription: d\n---\n\nBody\n",
                encoding="utf-8",
            )
        return tree_sha256(root)

    monkeypatch.setattr(package_manager, "_tree_sha256", change_name_before_hash)

    with pytest.raises(SkillPackageError, match="name changed between preflight"):
        manager.install(source)

    assert manager.list_packages() == {}
    assert not any(manager.managed_skills_dir.glob(".staging-*"))


def test_needs_adaptation_package_is_preserved_but_cannot_be_enabled(tmp_path: Path) -> None:
    source = tmp_path / "source" / "scripted-skill"
    marker = tmp_path / "script-ran"
    script = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('unexpected', encoding='utf-8')\n"
    )
    _write(
        source / "SKILL.md",
        "---\nname: scripted-skill\ndescription: Has a helper script.\n---\n\n"
        "Run [the helper](scripts/run.py).\n",
    )
    _write(source / "scripts" / "run.py", script)
    manager = SkillPackageManager(tmp_path / "workspace")

    record = manager.install(source)
    manifest_before_enable = manager.manifest_path.read_bytes()

    assert record["compatibility"]["status"] == "needs-adaptation"
    assert record["enabled"] is False
    assert manager.enabled_skill_names() == set()
    assert (manager.managed_skills_dir / "scripted-skill" / "scripts" / "run.py").read_bytes() == (
        source / "scripts" / "run.py"
    ).read_bytes()
    with pytest.raises(SkillPackageError, match="complete"):
        manager.enable("scripted-skill")
    assert manager.manifest_path.read_bytes() == manifest_before_enable
    assert not marker.exists()


def test_nested_requirements_include_resolves_against_containing_file(
    tmp_path: Path,
) -> None:
    source = _complete_package(tmp_path / "source")
    _write(source / "requirements.txt", "-r requirements/nested.txt\n")
    _write(source / "common.txt", "")
    _write(source / "requirements" / "nested.txt", "-r common.txt\n")
    _write(
        source / "requirements" / "common.txt",
        "nested-only-dependency==1.2.3\n",
    )

    report = inspection.inspect_skill_package(source)

    assert report["status"] == "needs-adaptation"
    assert any(
        item["name"] == "nested-only-dependency"
        and item["source"] == "requirements/common.txt"
        for item in report["dependencies"]
    )


def test_shell_fence_command_requires_manual_review(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    _write(
        source / "SKILL.md",
        "---\nname: sample-skill\ndescription: Uses a host command.\n---\n\n"
        "Run:\n\n```bash\ncd output && pandoc --version\n```\n",
    )

    report = inspection.inspect_skill_package(source)

    assert report["status"] == "needs-adaptation"
    assert any(
        item["kind"] == "command-runtime" and item["name"] == "pandoc"
        for item in report["requirements"]
    )


def test_shell_fence_accepts_longer_markdown_closing_fence(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    _write(
        source / "SKILL.md",
        "---\nname: sample-skill\ndescription: Uses a host command.\n---\n\n"
        "```bash\npandoc --version\n````\n",
    )

    report = inspection.inspect_skill_package(source)

    assert report["status"] == "needs-adaptation"
    assert any(
        item["kind"] == "command-runtime" and item["name"] == "pandoc"
        for item in report["requirements"]
    )


@pytest.mark.parametrize("language", ["powershell", "pwsh", "ps1", "cmd", "bat", "batch"])
def test_windows_shell_fence_command_requires_manual_review(
    tmp_path: Path, language: str
) -> None:
    source = _complete_package(tmp_path / "source")
    _write(
        source / "SKILL.md",
        "---\nname: sample-skill\ndescription: Uses a host command.\n---\n\n"
        f"```{language}\npandoc --version\n```\n",
    )

    report = inspection.inspect_skill_package(source)

    assert report["status"] == "needs-adaptation"
    assert any(
        item["kind"] == "command-runtime" and item["name"] == "pandoc"
        for item in report["requirements"]
    )


def test_explicit_run_context_marks_plain_argv_as_manual_review(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    _write(
        source / "SKILL.md",
        "---\nname: sample-skill\ndescription: Uses a host command.\n---\n\n"
        "Run `pandoc README` to convert the document.\n",
    )

    report = inspection.inspect_skill_package(source)

    assert report["status"] == "needs-adaptation"
    assert any(
        item["kind"] == "command-runtime" and item["name"] == "pandoc"
        for item in report["requirements"]
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "Use the `file name` as the heading.\n",
        "Require the `file name` field.\n",
        "Call the `REST API` endpoint.\n",
    ],
)
def test_plain_multword_inline_code_does_not_look_like_command(
    tmp_path: Path, sentence: str
) -> None:
    source = _complete_package(tmp_path / "source")
    _write(
        source / "SKILL.md",
        "---\nname: sample-skill\ndescription: Refers to a filename.\n---\n\n"
        + sentence,
    )

    report = inspection.inspect_skill_package(source)

    assert report["status"] == "complete"
    assert not any(item["kind"] == "command-runtime" for item in report["requirements"])


def test_loaded_package_reports_root_for_relative_resources(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    manager.enable("sample-skill")
    digests = manager.enabled_skill_digests()
    discovery = SkillDiscovery(
        directories=[manager.project_skills_dir, manager.managed_skills_dir],
        project_dir=manager.project_skills_dir,
        managed_directories={"project": manager.managed_skills_dir},
        enabled_managed_digests={"project": digests},
    )
    tool = LoadSkillTool(SkillCapability(discovery))

    result = asyncio.run(tool.execute(tool.args_schema(name="sample-skill")))

    resource_root = Path(result.data["resource_root"])
    assert resource_root == manager.managed_skills_dir / "sample-skill"
    assert (resource_root / "references" / "guide.md").read_text(encoding="utf-8") == "Guide content.\n"
    assert "相对资源以" in result.message


def test_promoted_project_skill_cannot_shadow_an_installed_snapshot(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    draft_path = tmp_path / "draft.md"
    draft_path.write_text(
        "---\nname: sample-skill\ndescription: New project skill.\n---\n\nNew body.\n",
        encoding="utf-8",
    )
    entry, errors = parse_skill_markdown_text(draft_path.read_text(encoding="utf-8"), draft_path)
    assert entry is not None and errors == []
    discovery = SkillDiscovery(
        directories=[manager.project_skills_dir, manager.managed_skills_dir],
        project_dir=manager.project_skills_dir,
        managed_directories={"project": manager.managed_skills_dir},
    )

    with pytest.raises(ValueError, match="managed Skill 'sample-skill' already exists"):
        discovery.register(entry)

    assert not (manager.project_skills_dir / "sample-skill").exists()


def test_enable_rechecks_project_name_conflicts(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    _write(
        manager.project_skills_dir / "other-folder" / "SKILL.md",
        "---\nname: sample-skill\ndescription: User skill.\n---\n\nUser body.\n",
    )

    with pytest.raises(SkillPackageError, match="project Skill 'sample-skill' already exists"):
        manager.enable("sample-skill")

    assert manager.list_packages()["sample-skill"]["enabled"] is False


def test_install_rejects_project_source_name_conflict_in_arbitrary_folder(
    tmp_path: Path,
) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    _write(
        manager.project_skills_dir / "other-folder" / "SKILL.md",
        "---\nname: sample-skill\ndescription: User skill.\n---\n\nUser body.\n",
    )

    with pytest.raises(SkillPackageError, match="project Skill 'sample-skill' already exists"):
        manager.install(source)


@pytest.mark.parametrize("source_kind", ["directory", "manual"])
def test_install_rejects_configured_skill_source_name_conflict(
    tmp_path: Path, source_kind: str
) -> None:
    source = _complete_package(tmp_path / "source")
    configured_root = tmp_path / "configured-skills"
    _write(
        configured_root / "provider-folder" / "SKILL.md"
        if source_kind == "directory"
        else configured_root / "manual-skill.md",
        "---\nname: sample-skill\ndescription: Existing configured Skill.\n---\n\nBody.\n",
    )
    manager = SkillPackageManager(
        tmp_path / "workspace",
        additional_skill_directories=(
            [configured_root] if source_kind == "directory" else []
        ),
        additional_skill_paths=(
            [configured_root / "manual-skill.md"] if source_kind == "manual" else []
        ),
    )

    with pytest.raises(SkillPackageError, match="configured Skill 'sample-skill' already exists"):
        manager.install(source)

    assert manager.list_packages() == {}


def test_enable_rechecks_configured_extension_name_conflicts(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    configured_root = tmp_path / "configured-skills"
    manager = SkillPackageManager(
        tmp_path / "workspace", additional_skill_directories=[configured_root]
    )
    manager.install(source)
    _write(
        configured_root / "other-folder" / "SKILL.md",
        "---\nname: sample-skill\ndescription: Existing configured Skill.\n---\n\nBody.\n",
    )

    with pytest.raises(SkillPackageError, match="configured Skill 'sample-skill' already exists"):
        manager.enable("sample-skill")

    assert manager.list_packages()["sample-skill"]["enabled"] is False


@pytest.mark.parametrize("source_kind", ["project", "global", "configured-directory", "configured-path"])
def test_runtime_selection_refuses_sources_added_after_enable(
    tmp_path: Path, source_kind: str
) -> None:
    configured_dir = tmp_path / "configured"
    configured_path = tmp_path / "external" / "SKILL.md"
    manager = SkillPackageManager(
        tmp_path / "workspace",
        global_skills_dir=tmp_path / "global",
        additional_skill_directories=[configured_dir],
        additional_skill_paths=[configured_path],
    )
    manager.install(_complete_package(tmp_path / "source"))
    manager.enable("sample-skill")

    source_roots = {
        "project": tmp_path / "workspace" / "skills" / "other-folder",
        "global": tmp_path / "global" / "other-folder",
        "configured-directory": configured_dir / "sample-skill",
        "configured-path": configured_path.parent,
    }
    _write(
        source_roots[source_kind] / "SKILL.md",
        "---\nname: sample-skill\ndescription: Conflicting source.\n---\n\nOther body.\n",
    )

    with pytest.raises(SkillPackageError, match="already exists"):
        manager.enabled_skill_digests()


def test_discovery_drops_managed_skill_when_a_conflict_appears_after_snapshot(
    tmp_path: Path,
) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    manager.enable("sample-skill")
    digests = manager.enabled_skill_digests()
    _write(
        manager.project_skills_dir / "other-folder" / "SKILL.md",
        "---\nname: sample-skill\ndescription: User skill.\n---\n\nUser body.\n",
    )

    catalog = SkillDiscovery(
        directories=[manager.project_skills_dir, manager.managed_skills_dir],
        managed_directories={"project": manager.managed_skills_dir},
        enabled_managed_digests={"project": digests},
    ).discover()

    assert not any(entry.name == "sample-skill" for entry in catalog.entries)
    assert any("sample-skill" in conflict for conflict in catalog.conflicts)


def test_python_311_reparse_attribute_marks_nested_junction_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _complete_package(tmp_path / "source")
    junction = source / "linked"
    junction.mkdir()
    real_lstat = Path.lstat

    class _JunctionStat:
        st_mode = stat.S_IFDIR
        st_file_attributes = 0x400

    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: _JunctionStat() if path == junction else real_lstat(path),
    )

    report = inspection.inspect_skill_package(source)

    assert report["status"] == "unsupported"
    assert any(error["code"] == "UNSUPPORTED_JUNCTION" for error in report["errors"])


def test_windows_registry_lock_verifies_open_handle_before_writing_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if package_lock.msvcrt is None:
        pytest.skip("Windows byte-range locking is unavailable")
    manager = SkillPackageManager(tmp_path / "workspace")
    lock_path = manager.manifest_path.with_name(manager.manifest_path.name + ".lock")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(b"")
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"must stay unchanged")
    monkeypatch.setattr(inspection, "_windows_open_handle_path", lambda _fd: victim)

    with (
        pytest.raises(OSError, match="opened outside its path"),
        package_lock.registry_lock(manager.manifest_path),
    ):
        pass

    assert victim.read_bytes() == b"must stay unchanged"


def test_windows_lock_create_race_never_follows_reparse_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "registry.lock"
    reparse_states = iter((False, True))
    open_calls: list[tuple[Path, int]] = []

    def fake_open(path: Path, flags: int, _mode: int) -> int:
        open_calls.append((path, flags))
        if flags & os.O_EXCL:
            raise FileExistsError(path)
        raise AssertionError("must not open the newly linked target")

    monkeypatch.setattr(package_lock, "_is_reparse_point", lambda _path: next(reparse_states))
    monkeypatch.setattr(package_lock.os, "open", fake_open)

    with pytest.raises(OSError, match="refusing linked package registry lock"):
        package_lock._open_windows_lock_file(lock_path, os.O_RDWR | os.O_CREAT)

    assert len(open_calls) == 1
    assert open_calls[0][1] & os.O_EXCL


def test_managed_package_cannot_bypass_saved_selection_through_path_alias(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    managed = manager.managed_skills_dir
    alias = managed.parent / ".." / managed.parent.name / managed.name
    assert alias != managed and alias.resolve() == managed.resolve()

    disabled = SkillDiscovery(
        directories=[alias],
        managed_directories={"project": managed},
        enabled_managed_digests={"project": manager.enabled_skill_digests()},
    ).discover()
    assert disabled.entries == []

    manager.enable("sample-skill")
    enabled = SkillDiscovery(
        directories=[alias],
        managed_directories={"project": managed},
        enabled_managed_digests={"project": manager.enabled_skill_digests()},
    ).discover()
    assert [entry.name for entry in enabled.entries] == ["sample-skill"]


def test_managed_directory_swap_cannot_expose_a_previously_enabled_name(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    manager.enable("sample-skill")
    previously_enabled = manager.enabled_skill_digests()
    managed = manager.managed_skills_dir
    shutil.rmtree(managed)
    outside = _complete_package(tmp_path / "outside")
    try:
        managed.symlink_to(outside.parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are not available on this host")

    catalog = SkillDiscovery(
        directories=[managed],
        managed_directories={"project": managed},
        enabled_managed_digests={"project": previously_enabled},
    ).discover()

    assert catalog.entries == []


def test_existing_project_skill_name_is_not_overwritten(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    workspace = tmp_path / "workspace"
    existing = workspace / "skills" / "sample-skill" / "SKILL.md"
    _write(existing, "user-owned skill\n")
    original = existing.read_bytes()
    manager = SkillPackageManager(workspace)

    with pytest.raises(SkillPackageError, match="already exists"):
        manager.install(source)

    assert existing.read_bytes() == original
    assert not manager.manifest_path.exists()
    assert not manager.managed_skills_dir.exists()


def test_remove_refuses_to_delete_files_changed_after_install(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    manager.enable("sample-skill")
    package = manager.managed_skills_dir / "sample-skill"
    user_file = package / "notes.txt"
    user_file.write_text("user data\n", encoding="utf-8")
    manifest_before = manager.manifest_path.read_bytes()

    with pytest.raises(SkillPackageError, match="changed"):
        manager.remove("sample-skill")

    assert user_file.read_text(encoding="utf-8") == "user data\n"
    assert manager.manifest_path.read_bytes() == manifest_before


def test_runtime_selection_refuses_changed_enabled_snapshot(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    manager.enable("sample-skill")
    skill_file = manager.managed_skills_dir / "sample-skill" / "SKILL.md"
    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "Changed after review.\n", encoding="utf-8")

    with pytest.raises(SkillPackageError, match="changed after installation"):
        manager.enabled_skill_names()


def test_lazy_runtime_load_refuses_skill_body_changed_after_discovery(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    manager.enable("sample-skill")
    discovery = SkillDiscovery(
        directories=[manager.managed_skills_dir],
        managed_directories={"project": manager.managed_skills_dir},
        enabled_managed_digests={"project": manager.enabled_skill_digests()},
    )
    entry = discovery.discover().entries[0]
    skill_file = manager.managed_skills_dir / "sample-skill" / "SKILL.md"
    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "Injected after discovery.\n", encoding="utf-8")

    with pytest.raises(OSError, match="changed after installation"):
        entry.load_body()


def test_windows_reparse_point_attribute_is_detected_on_supported_python_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Stat:
        st_mode = 0
        st_file_attributes = 0x400

    monkeypatch.setattr(Path, "lstat", lambda _path: _Stat())
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)

    assert package_manager._is_reparse_point(tmp_path / "junction") is True


def test_project_skills_junction_is_rejected_before_package_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _complete_package(tmp_path / "source")
    workspace = tmp_path / "workspace"
    manager = SkillPackageManager(workspace)
    manager.project_skills_dir.mkdir(parents=True)
    real_lstat = Path.lstat

    class _JunctionStat:
        st_mode = 0o040000
        st_file_attributes = 0x400

    def lstat(path: Path):
        if path == manager.project_skills_dir:
            return _JunctionStat()
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat)

    with pytest.raises(SkillPackageError, match="symbolic link or junction"):
        manager.install(source)

    assert not manager.managed_skills_dir.exists()


def test_remove_rechecks_after_rename_and_restores_modified_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    manifest_before = manager.manifest_path.read_bytes()
    real_tree_sha256 = package_manager._tree_sha256
    modified = False

    def change_tombstone_before_recheck(root: Path) -> str:
        nonlocal modified
        if root.name.startswith(".removing-") and not modified:
            (root / "user-note.txt").write_text("concurrent edit\n", encoding="utf-8")
            modified = True
        return real_tree_sha256(root)

    monkeypatch.setattr(package_manager, "_tree_sha256", change_tombstone_before_recheck)

    with pytest.raises(SkillPackageError, match="changed during removal"):
        manager.remove("sample-skill")

    restored = manager.managed_skills_dir / "sample-skill"
    assert (restored / "user-note.txt").read_text(encoding="utf-8") == "concurrent edit\n"
    assert manager.manifest_path.read_bytes() == manifest_before
    assert not any(manager.managed_skills_dir.glob(".removing-*"))


def test_manifest_writer_does_not_follow_preexisting_temp_link(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SkillPackageManager(workspace)
    protected = workspace / "user-owned.json"
    protected.write_text("preserve\n", encoding="utf-8")
    legacy_temp = manager.manifest_path.with_name(manager.manifest_path.name + ".tmp")
    try:
        os.link(protected, legacy_temp)
    except OSError:
        try:
            legacy_temp.symlink_to(protected)
        except (OSError, NotImplementedError):
            pytest.skip("this host cannot create a hard link or symbolic link")

    manager._write_manifest({"version": 1, "packages": {}})

    assert protected.read_text(encoding="utf-8") == "preserve\n"
    assert json.loads(manager.manifest_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "packages": {},
    }


def test_remove_registry_write_failure_restores_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    installed = manager.managed_skills_dir / "sample-skill"
    before_tree = {
        item.relative_to(installed).as_posix(): item.read_bytes()
        for item in installed.rglob("*")
        if item.is_file()
    }
    manifest_before = manager.manifest_path.read_bytes()
    real_replace = os.replace

    def fail_manifest_replace(source_path: str | os.PathLike[str], target_path: str | os.PathLike[str]) -> None:
        if Path(target_path) == manager.manifest_path:
            raise OSError("injected registry failure")
        real_replace(source_path, target_path)

    monkeypatch.setattr("agent_harness.skills.package_manager.os.replace", fail_manifest_replace)

    with pytest.raises(OSError, match="injected registry failure"):
        manager.remove("sample-skill")

    assert manager.manifest_path.read_bytes() == manifest_before
    assert {
        item.relative_to(installed).as_posix(): item.read_bytes()
        for item in installed.rglob("*")
        if item.is_file()
    } == before_tree


def test_unsafe_managed_directory_is_never_used(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    managed = workspace / "skills" / ".managed"
    managed.parent.mkdir(parents=True)
    try:
        managed.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are not available on this host")
    manager = SkillPackageManager(workspace)

    assert manager.enabled_skill_names() == set()
    with pytest.raises(SkillPackageError, match="symbolic link or junction"):
        manager.install(_complete_package(tmp_path / "source"))
    assert list(outside.iterdir()) == []


def test_install_registry_write_failure_leaves_no_visible_or_partial_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    real_replace = os.replace

    def fail_manifest_replace(source_path: str | os.PathLike[str], target_path: str | os.PathLike[str]) -> None:
        if Path(target_path) == manager.manifest_path:
            raise OSError("injected registry failure")
        real_replace(source_path, target_path)

    monkeypatch.setattr("agent_harness.skills.package_manager.os.replace", fail_manifest_replace)

    with pytest.raises(OSError, match="injected registry failure"):
        manager.install(source)

    assert not manager.manifest_path.exists()
    assert not (manager.managed_skills_dir / "sample-skill").exists()


def test_global_git_package_is_shared_and_lifecycle_isolated_from_projects(tmp_path: Path) -> None:
    global_skills = tmp_path / "home" / ".intelligence-agent" / "skills"
    workspace_a = tmp_path / "project-a"
    workspace_b = tmp_path / "project-b"
    project_a = SkillPackageManager(workspace_a, global_skills_dir=global_skills)
    project_b = SkillPackageManager(workspace_b, global_skills_dir=global_skills)
    global_a = SkillPackageManager(
        workspace_a, global_skills_dir=global_skills, scope="global"
    )
    global_b = SkillPackageManager(
        workspace_b, global_skills_dir=global_skills, scope="global"
    )

    project_a.install(_complete_package(tmp_path / "project-a-source"))
    project_b.install(_complete_package(tmp_path / "project-b-source"))

    def project_snapshot(manager: SkillPackageManager) -> tuple[bytes, dict[str, bytes]]:
        return (
            manager.manifest_path.read_bytes(),
            {
                item.relative_to(manager.managed_skills_dir).as_posix(): item.read_bytes()
                for item in manager.managed_skills_dir.rglob("*")
                if item.is_file()
            },
        )

    project_states = [
        (manager, project_snapshot(manager)) for manager in (project_a, project_b)
    ]

    repo, old_commit, new_commit = _git_skill_repo(tmp_path / "upstream")
    installed = global_a.install_git(
        repo.as_uri(), ref=old_commit, subdirectory="packages/sample-skill"
    )

    assert installed["scope"] == "global"
    assert installed["enabled"] is False
    assert global_b.list_packages() == global_a.list_packages()
    assert global_a.manifest_path == global_b.manifest_path
    manifest_before_enable = global_a.manifest_path.read_bytes()
    with pytest.raises(SkillPackageError, match="enabled per project"):
        global_a.enable("sample-skill")
    assert global_a.manifest_path.read_bytes() == manifest_before_enable
    for manager, snapshot in project_states:
        assert project_snapshot(manager) == snapshot

    global_state = {
        item.relative_to(global_a.scope_root).as_posix(): item.read_bytes()
        for item in global_a.scope_root.rglob("*")
        if item.is_file()
    }
    project_extra = tmp_path / "project-extra"
    _write(
        project_extra / "SKILL.md",
        "---\nname: project-extra\ndescription: Project-only package.\n---\n\nBody.\n",
    )
    project_a.install(project_extra)
    assert {
        item.relative_to(global_a.scope_root).as_posix(): item.read_bytes()
        for item in global_a.scope_root.rglob("*")
        if item.is_file()
    } == global_state
    project_states = [
        (manager, project_snapshot(manager)) for manager in (project_a, project_b)
    ]

    global_a.update("sample-skill", ref=new_commit)
    assert global_a.apply_pending_versions() == 1
    assert global_a.list_packages()["sample-skill"]["resolved_commit"] == new_commit
    assert global_a.rollback("sample-skill")["pending_version"]["resolved_commit"] == old_commit
    assert global_a.apply_pending_versions() == 1
    global_a.remove("sample-skill")

    for manager, snapshot in project_states:
        assert project_snapshot(manager) == snapshot
    assert global_b.list_packages() == {}
    assert not (global_a.managed_skills_dir / "sample-skill").exists()


def test_global_storage_link_fails_without_falling_back_to_project_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    global_skills = tmp_path / "home" / ".intelligence-agent" / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    storage = global_skills.parent / "plugin-packages"
    storage.parent.mkdir(parents=True)
    try:
        storage.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this host")

    manager = SkillPackageManager(workspace, global_skills_dir=global_skills, scope="global")
    with pytest.raises(SkillPackageError, match="global package storage directory"):
        manager.install(_complete_package(tmp_path / "source"))

    assert not (workspace / "plugin-installs.json").exists()
    assert list(outside.iterdir()) == []
