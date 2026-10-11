"""#878 T9：DSH 原生包（dsh-codex-effort-slider）安装/启停生命周期的定向测试。

形状比照 ``tests/test_cli_plugins_inspect.py`` 的既有 DSH 用例。全程不断言/执行
包内 JS：包代码只被哈希/复制为字节快照（lib/client.js 永不 import/执行），
证明手段 = 触碰标记文件 + 快照字节对账。

Q1 裁决（协调员已批准）：enable 语义 = 启用适配版（enabled-adapted），
记录如实携带 needs-adaptation + off gap，不标 complete。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_harness import cli
from agent_harness.skills.discovery import SkillDiscovery
from agent_harness.skills.package_manager import (
    SkillPackageManager,
    _tree_sha256,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dsh-codex-effort-slider"
DSH_NAME = "dsh-codex-effort-slider"
PINNED_COMMIT = "af723caf3387e64ae28aa69c4fd235b1b662e3ae"
CONTRIBUTION_NAMES = {
    "Model-specific effort options and default",
    "Select and send an effort value",
    "Keyboard and accessible slider interaction",
    "Theme and reduced motion",
}


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _configure_cli(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    global_skills: Path,
) -> SimpleNamespace:
    settings = SimpleNamespace(
        workspace_dir=str(workspace),
        skill_global_dir=str(global_skills),
        capabilities=json.dumps({"skills": {"enabled": True}}),
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        cli, "_query_current_skill_runtime", lambda *_: ("not_running", "stopped", None)
    )
    return settings


def _argv(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", *args])


def _cli_run(monkeypatch: pytest.MonkeyPatch, capsys, *args: str) -> dict:
    _argv(monkeypatch, *args)
    cli.main()
    return json.loads(capsys.readouterr().out)


def _cli_fail(monkeypatch: pytest.MonkeyPatch, capsys, *args: str) -> str:
    _argv(monkeypatch, *args)
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    return capsys.readouterr().err


def _manager(workspace: Path, global_skills: Path) -> SkillPackageManager:
    return SkillPackageManager(
        workspace, scope="project", global_skills_dir=global_skills
    )


def _file_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_dsh_install_records_fields_snapshot_and_never_runs_package_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    marker = tmp_path / "must-never-exist"
    source = tmp_path / "slider-source"
    shutil.copytree(FIXTURE, source)
    # 入口文件被改写为"若被解释执行就会留下标记"的内容会让 pin 摘要失配；
    # 这里对合法 fixture 只能证明：安装路径只做哈希/复制，标记文件不存在，
    # 且快照与来源逐字节一致（字节对账）。
    record = _cli_run(monkeypatch, capsys, "install", str(source))

    assert record["name"] == DSH_NAME
    assert record["type"] == "dsh-native"
    assert record["scope"] == "project"
    assert record["source_pin"]["commit"] == PINNED_COMMIT
    assert record["version"] == "1.0.0"
    assert record["compatibility"] == {
        "status": "needs-adaptation",
        "license": "MIT",
    }
    assert "native reasoning-effort adaptation" in record["adapter"]
    assert "#865" in record["adapter"]
    assert {item["name"] for item in record["contributions"]} == CONTRIBUTION_NAMES
    assert all(item["status"] == "adapted" for item in record["contributions"])
    assert [gap["name"] for gap in record["gaps"]] == ["off"]
    assert record["gaps"][0]["status"] == "unmapped"
    assert record["trust"] == {"status": "untrusted", "sha256": record["sha256"]}
    assert record["enabled"] is False

    manager = _manager(workspace, global_skills)
    snapshot = manager.managed_skills_dir / DSH_NAME
    for relative in ("package.json", "lib/client.js", "lib/index.js", "LICENSE"):
        assert (snapshot / relative).read_bytes() == (source / relative).read_bytes()
    assert _tree_sha256(snapshot) == record["sha256"]

    # 隔离于 SkillDiscovery 发现路径：不进 skill 装配面
    catalog = SkillDiscovery(
        [workspace / "skills"],
        managed_directories={"project": manager.managed_skills_dir},
    ).discover()
    assert DSH_NAME not in {entry.name for entry in catalog.entries}
    assert manager.enabled_skill_digests() == {}
    assert not marker.exists()


def test_dsh_install_rejects_drifted_source_without_writes_or_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    marker = tmp_path / "must-never-exist"
    source = tmp_path / "dsh-codex-effort-slider"
    # 与既有 inspect 用例同形：包形如 pin 目标但内容漂移，且入口 JS 若被
    # 解释执行会留下标记文件。
    package = {
        "name": DSH_NAME,
        "version": "1.0.0",
        "license": "MIT",
        "main": "./lib/index.js",
        "exports": {".": "./lib/index.js", "./client": "./lib/client.js"},
        "dsh": {
            "bundle": {"patch": "./cordis.patch.yml"},
            "client": {"platform": "web", "immediately": True},
        },
    }
    _write(source / "package.json", json.dumps(package))
    _write(source / "LICENSE", "MIT License\n")
    _write(source / "cordis.patch.yml", "- name: dsh-codex-effort-slider\n")
    _write(source / "install-profile.ps1", "# fixture only\n")
    _write(source / "install.cmd", "rem fixture only\r\n")
    _write(source / "pack-dist.ps1", "# fixture only\n")
    _write(
        source / "lib" / "index.js",
        f"from pathlib import Path; Path({str(marker)!r}).touch()\n",
    )
    _write(
        source / "lib" / "client.js",
        f"from pathlib import Path; Path({str(marker)!r}).touch()\n",
    )

    error = _cli_fail(monkeypatch, capsys, "install", str(source))

    assert "SOURCE_CONTENT_DRIFT" in error
    assert not (workspace / "plugin-installs.json").exists()
    assert not (workspace / "skills" / ".managed").exists()
    assert not marker.exists()


def test_dsh_enable_adapted_carries_gaps_and_never_marks_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE))

    result = _cli_run(monkeypatch, capsys, "enable", DSH_NAME)

    assert result["selected_scope"] == "project"
    manager = _manager(workspace, global_skills)
    record = manager.list_packages()[DSH_NAME]
    # Q1 裁决：启用适配版——如实携带 needs-adaptation + off gap，不标 complete。
    assert record["enabled"] is True
    assert record["compatibility"]["status"] == "needs-adaptation"
    assert [gap["name"] for gap in record["gaps"]] == ["off"]
    assert record["gaps"][0]["status"] == "unmapped"
    assert manager.enable_results()[DSH_NAME]["selected_scope"] == "project"
    # 适配激活不把包送进 Skill 装配面（原生 UI 语义，#865）。
    assert manager.enabled_skill_digests() == {}


def test_dsh_same_id_in_both_scopes_requires_explicit_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE))
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE), "--scope", "global")

    error = _cli_fail(monkeypatch, capsys, "enable", DSH_NAME)

    assert "--scope" in error
    assert "project" in error and "global" in error
    manager = _manager(workspace, global_skills)
    assert manager.enable_results() == {}
    assert manager.list_packages()[DSH_NAME]["enabled"] is False


def test_dsh_disable_leaves_no_plugin_specific_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE))
    _cli_run(monkeypatch, capsys, "enable", DSH_NAME)
    manager = _manager(workspace, global_skills)
    before = _file_tree(workspace)

    result = _cli_run(monkeypatch, capsys, "disable", DSH_NAME)

    assert result["previous_scope"] == "project"
    after = _file_tree(workspace)
    # 停用后无插件专属残留：工作树里唯一变化是安装清单本身（没有第二套状态、
    # 没有插件专属入口文件/目录）。
    changed = {
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    }
    assert changed == {"plugin-installs.json"}
    manifest = json.loads(
        (workspace / "plugin-installs.json").read_text(encoding="utf-8")
    )
    assert manifest.get("enable_results", {}) == {}
    assert manifest["packages"][DSH_NAME]["enabled"] is False
    assert manager.enable_results() == {}
    assert manager.enabled_skill_digests() == {}
    catalog = SkillDiscovery(
        [workspace / "skills"],
        managed_directories={"project": manager.managed_skills_dir},
    ).discover()
    assert DSH_NAME not in {entry.name for entry in catalog.entries}


def test_dsh_list_projection_adapted_state_and_disable_not_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE))

    listing = _cli_run(monkeypatch, capsys, "list")
    row = next(item for item in listing["packages"] if item["name"] == DSH_NAME)
    assert row["type"] == "dsh-native"
    assert row["saved_selection"] == "not_selected"
    assert row["current_runtime"] == "not_selected"
    assert row["pending_restart"] is False

    _cli_run(monkeypatch, capsys, "enable", DSH_NAME)
    enabled_row = _cli_run(monkeypatch, capsys, "list")["packages"][0]
    # enabled-adapted → current=active(native adaptation)
    assert enabled_row["saved_selection"] == "enabled-adapted"
    assert enabled_row["current_runtime"] == "active(native adaptation)"
    assert enabled_row["pending_restart"] is False
    assert enabled_row["compatibility"]["status"] == "needs-adaptation"

    _cli_run(monkeypatch, capsys, "disable", DSH_NAME)
    disabled_row = _cli_run(monkeypatch, capsys, "list")["packages"][0]
    # disable → not_selected
    assert disabled_row["saved_selection"] == "not_selected"
    assert disabled_row["current_runtime"] == "not_selected"
    assert disabled_row["pending_restart"] is False


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    return result.stdout.strip()


def _seed_repo(repo: Path) -> str:
    """提交 fixture 内容作为 commit1，返回完整 commit SHA。"""
    repo.mkdir(parents=True)
    _git(repo, "init", "--quiet", "-b", "main")
    package = repo / DSH_NAME
    shutil.copytree(FIXTURE, package)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "pinned slider")
    return _git(repo, "rev-parse", "HEAD")


def test_dsh_git_install_update_apply_rollback_and_pending_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    repo = tmp_path / "repo"
    commit1 = _seed_repo(repo)
    url = f"file://{repo}"

    record = _cli_run(
        monkeypatch, capsys, "install-git", url, "--ref", "main", "--subdirectory", DSH_NAME
    )
    assert record["type"] == "dsh-native"
    assert record["source_kind"] == "dsh-git"
    assert record["resolved_commit"] == commit1
    assert record["source_pin"]["commit"] == PINNED_COMMIT
    assert record["enabled"] is False

    # commit2：pin 内文件逐字节不变 + 一个未被 pin 的附加文件 ⇒ 识别仍成立。
    package_dir = repo / DSH_NAME
    _write(package_dir / "NOTES.md", "harmless extra file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "extra notes")
    commit2 = _git(repo, "rev-parse", "HEAD")

    updated = _cli_run(monkeypatch, capsys, "update", DSH_NAME, "--ref", commit2)
    assert updated["pending_version"]["resolved_commit"] == commit2
    manager = _manager(workspace, global_skills)
    row = _cli_run(monkeypatch, capsys, "list")["packages"][0]
    # update 暂存 → pending_restart=true；当前态与待重启态在投影里并存。
    assert row["pending_restart"] is True
    assert row["pending_version"]["resolved_commit"] == commit2

    # 重启（apply）：新版本激活，旧版本成为 previous_version。
    assert manager.apply_pending_versions() == 1
    applied = manager.list_packages()[DSH_NAME]
    assert applied["resolved_commit"] == commit2
    assert applied["pending_version"] is None
    assert applied["previous_version"]["resolved_commit"] == commit1
    snapshot = manager.managed_skills_dir / DSH_NAME
    assert (snapshot / "NOTES.md").read_text(encoding="utf-8") == "harmless extra file\n"
    assert _tree_sha256(snapshot) == applied["sha256"]

    # rollback：把 previous 暂存回去 → pending_restart=true；再 apply 回到 commit1。
    rolled = manager.rollback(DSH_NAME)
    assert rolled["pending_version"]["resolved_commit"] == commit1
    row = _cli_run(monkeypatch, capsys, "list")["packages"][0]
    assert row["pending_restart"] is True
    assert manager.apply_pending_versions() == 1
    restored = manager.list_packages()[DSH_NAME]
    assert restored["resolved_commit"] == commit1
    assert not (manager.managed_skills_dir / DSH_NAME / "NOTES.md").exists()


def test_dsh_git_update_rejects_drift_and_keeps_installed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    repo = tmp_path / "repo"
    commit1 = _seed_repo(repo)
    _cli_run(
        monkeypatch,
        capsys,
        "install-git",
        f"file://{repo}",
        "--ref",
        "main",
        "--subdirectory",
        DSH_NAME,
    )
    # 造一个漂移 ref：pin 文件被改 ⇒ update 必须拒收并保留现版本。
    package_dir = repo / DSH_NAME
    _write(package_dir / "lib" / "client.js", "throw new Error('drifted')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "drift")
    drift_commit = _git(repo, "rev-parse", "HEAD")

    error = _cli_fail(monkeypatch, capsys, "update", DSH_NAME, "--ref", drift_commit)

    assert "SOURCE_CONTENT_DRIFT" in error
    manager = _manager(workspace, global_skills)
    record = manager.list_packages()[DSH_NAME]
    assert record["resolved_commit"] == commit1
    assert record["pending_version"] is None


def test_dsh_local_record_cannot_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE))

    error = _cli_fail(monkeypatch, capsys, "update", DSH_NAME, "--ref", "main")

    assert "not installed from Git" in error
    manager = _manager(workspace, global_skills)
    # 本地目录安装的记录不携带任何 Git 字段（fail-closed 校验也禁止它们出现）。
    assert manager.list_packages()[DSH_NAME].get("pending_version") is None
