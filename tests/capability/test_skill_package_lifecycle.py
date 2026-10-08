from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from agent_harness.skills import package_manager
from agent_harness.skills.discovery import SkillDiscovery
from agent_harness.skills.package_manager import SkillPackageError, SkillPackageManager


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
        managed_directory=restarted.managed_skills_dir,
        enabled_managed_skills=set(restarted.enabled_skill_digests()),
        enabled_managed_skill_digests=restarted.enabled_skill_digests(),
    )
    catalog = discovery.discover()

    assert [entry.name for entry in catalog.entries] == ["sample-skill"]
    assert catalog.entries[0].load_body() == (
        "Read [the guide](references/guide.md) and [the icon](assets/icon.bin)."
    )
    assert (catalog.entries[0].source_path.parent / "references" / "guide.md").read_text(
        encoding="utf-8"
    ) == "Guide content.\n"


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


def test_managed_package_cannot_bypass_saved_selection_through_path_alias(tmp_path: Path) -> None:
    source = _complete_package(tmp_path / "source")
    manager = SkillPackageManager(tmp_path / "workspace")
    manager.install(source)
    managed = manager.managed_skills_dir
    alias = managed.parent / ".." / managed.parent.name / managed.name
    assert alias != managed and alias.resolve() == managed.resolve()

    disabled = SkillDiscovery(
        directories=[alias],
        managed_directory=managed,
        enabled_managed_skills=manager.enabled_skill_names(),
        enabled_managed_skill_digests=manager.enabled_skill_digests(),
    ).discover()
    assert disabled.entries == []

    manager.enable("sample-skill")
    enabled = SkillDiscovery(
        directories=[alias],
        managed_directory=managed,
        enabled_managed_skills=manager.enabled_skill_names(),
        enabled_managed_skill_digests=manager.enabled_skill_digests(),
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
        managed_directory=managed,
        enabled_managed_skills=set(previously_enabled),
        enabled_managed_skill_digests=previously_enabled,
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
        managed_directory=manager.managed_skills_dir,
        enabled_managed_skills=set(manager.enabled_skill_digests()),
        enabled_managed_skill_digests=manager.enabled_skill_digests(),
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
