"""#878 T9：DSH 原生包（dsh-codex-effort-slider）安装/启停生命周期的定向测试。

形状比照 ``tests/test_cli_plugins_inspect.py`` 的既有 DSH 用例。包内 JS 只被
哈希/复制为字节快照（lib/client.js 永不 import/执行），主要证明手段是快照与
来源的字节对账 + 调用面无任何 import/exec/子进程路径。个别用例把 Python 语句
写进入口 .js 作 canary——该内容作为 JS 本就永远 SyntaxError、不可能被任何
JS 引擎执行，marker 断言只是廉价的冗余旁证，不构成独立证明。

Q1 设计判断（待协调员提交用户裁决，不阻塞施工）：enable 语义按启用适配版
（enabled-adapted）实现，记录如实携带 needs-adaptation + off gap，不标
complete（施工方式保持现状，由协调员走裁决流程）。
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
    SkillPackageError,
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


def test_dsh_install_snapshot_byte_fidelity_and_no_canary_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    marker = tmp_path / "must-never-exist"
    source = tmp_path / "slider-source"
    shutil.copytree(FIXTURE, source)
    # 入口文件被改写会让 pin 摘要失配，故对合法 fixture 只能证明：安装路径
    # 只做哈希/复制，快照与来源逐字节一致（字节对账）。marker 不存在是冗余
    # 旁证：canary 是写在 .js 里的 Python 语句，作为 JS 本就永远 SyntaxError。
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
    # 与既有 inspect 用例同形：包形如 pin 目标但内容漂移。入口 JS 塞的是
    # Python 语句作 canary（作为 JS 本就永远 SyntaxError、不可能执行），
    # marker 仅作冗余旁证；真正的判据是 pin 摘要失配 ⇒ 拒装。
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
    # Q1（待裁决，当前按 enabled-adapted 实现）：如实携带 needs-adaptation + off gap，不标 complete。
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


def test_dsh_list_projection_adapted_state_and_disable_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE))

    listing = _cli_run(monkeypatch, capsys, "list")
    row = next(item for item in listing["packages"] if item["name"] == DSH_NAME)
    assert row["type"] == "dsh-native"
    # 记录面负词：installed 但未启用 → disabled（与 skill 行同词，S-P2）
    assert row["saved_selection"] == "disabled"
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
    # disable → disabled（记录面负词与 skill 行同词，S-P2）
    assert disabled_row["saved_selection"] == "disabled"
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


def test_dsh_git_record_carries_modes_and_detects_chmod_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """P1-1 修法 (a)：dsh-git 记录顶层落 modes 摘要，活动快照 chmod 漂移可检。

    与 skill git 车道同机制：`_managed_package_sha256` 对 dsh-git 走
    `_git_tree_sha256`，对已装快照 ``chmod +x`` 的漂移 fail-closed。
    """
    import os

    from agent_harness.skills.package_manager import _git_modes_sha256_from_package

    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    repo = tmp_path / "repo"
    _seed_repo(repo)
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
    manager = _manager(workspace, global_skills)
    record = manager.list_packages()[DSH_NAME]
    snapshot = manager.managed_skills_dir / DSH_NAME
    assert record["git_modes_sha256"] == _git_modes_sha256_from_package(snapshot)

    # 正例：未漂移时 enable 放行（Q1 待裁决，当前按 enabled-adapted 实现）。
    manager.enable(DSH_NAME)
    manager.disable(DSH_NAME)

    # 反例：对已装快照 chmod +x ⇒ 启用必须拒绝。
    os.chmod(snapshot / "lib" / "client.js", 0o755)
    with pytest.raises(SkillPackageError, match="modes changed"):
        manager.enable(DSH_NAME)


def test_dsh_global_enable_and_available_global_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """全局 DSH enable（_verify_selected_global DSH 分支）+ 全局投影口径。

    availability = available_adapted（可启用，C-P3-2）；available_global 行的
    saved_selection 与 packages[] 同词 enabled-adapted（C-P3-1）；enable 回显按
    record type 派生（S-P3-1）。
    """
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE), "--scope", "global")

    listing = _cli_run(monkeypatch, capsys, "list")
    row = next(
        item
        for item in listing["available_global_packages"]
        if item["name"] == DSH_NAME
    )
    assert row["availability"] == "available_adapted"
    assert row["saved_selection"] == "not_selected"
    assert row["current_runtime"] == "not_selected"
    assert row["pending_restart"] is False

    result = _cli_run(monkeypatch, capsys, "enable", DSH_NAME, "--scope", "global")
    assert result["selected_scope"] == "global"
    assert result["saved_selection"] == "enabled-adapted"

    row = next(
        item
        for item in _cli_run(monkeypatch, capsys, "list")["available_global_packages"]
        if item["name"] == DSH_NAME
    )
    assert row["saved_selection"] == "enabled-adapted"
    assert row["current_runtime"] == "active(native adaptation)"
    assert row["pending_restart"] is False

    # 全局视图本体（--scope global）的 availability 同词。
    global_row = next(
        item
        for item in _cli_run(monkeypatch, capsys, "list", "--scope", "global")["packages"]
        if item["name"] == DSH_NAME
    )
    assert global_row["availability"] == "available_adapted"


def test_dsh_rollback_rejects_without_previous_or_with_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """DSH rollback 拒绝路径：本地记录 / 无 previous / 已有 pending。"""
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    repo = tmp_path / "repo"
    _seed_repo(repo)

    # 子例 A：本地（非 git）DSH 记录没有可回退版本。
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE))
    local_manager = _manager(workspace, global_skills)
    with pytest.raises(SkillPackageError, match="not installed from Git"):
        local_manager.rollback(DSH_NAME)

    # 子例 B：dsh-git 记录——先无 previous，后 pending 占位。
    git_workspace = tmp_path / "workspace-git"
    _configure_cli(monkeypatch, git_workspace, global_skills)
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
    manager = _manager(git_workspace, global_skills)
    with pytest.raises(SkillPackageError, match="no previous DSH snapshot"):
        manager.rollback(DSH_NAME)

    _write(repo / DSH_NAME / "NOTES.md", "harmless extra file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "extra notes")
    commit2 = _git(repo, "rev-parse", "HEAD")
    _cli_run(monkeypatch, capsys, "update", DSH_NAME, "--ref", commit2)
    with pytest.raises(SkillPackageError, match="already has a pending version"):
        manager.rollback(DSH_NAME)
    record = manager.list_packages()[DSH_NAME]
    assert record["pending_version"]["resolved_commit"] == commit2
    assert record["previous_version"] is None


def test_dsh_apply_staged_drift_keeps_installed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """apply 复核失败（staged drift）后已装版本保持选中，不半换。"""
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
    _write(repo / DSH_NAME / "NOTES.md", "harmless extra file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "extra notes")
    commit2 = _git(repo, "rev-parse", "HEAD")
    _cli_run(monkeypatch, capsys, "update", DSH_NAME, "--ref", commit2)

    manager = _manager(workspace, global_skills)
    pending = manager.list_packages()[DSH_NAME]["pending_version"]
    snapshot_dir = manager.managed_versions_dir / DSH_NAME / pending["snapshot_id"]
    _write(snapshot_dir / "lib" / "client.js", "throw new Error('tampered')\n")

    with pytest.raises(SkillPackageError, match="changed"):
        manager.apply_pending_versions()
    record = manager.list_packages()[DSH_NAME]
    assert record["resolved_commit"] == commit1
    assert record["pending_version"]["resolved_commit"] == commit2
    assert not (manager.managed_skills_dir / DSH_NAME / "NOTES.md").exists()


def test_dsh_apply_recognition_failure_keeps_installed_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """apply 复核失败（识别失败）后已装版本保持选中；恢复识别后可重试。"""
    from agent_harness.skills import package_manager as package_manager_module

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
    _write(repo / DSH_NAME / "NOTES.md", "harmless extra file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "extra notes")
    commit2 = _git(repo, "rev-parse", "HEAD")
    _cli_run(monkeypatch, capsys, "update", DSH_NAME, "--ref", commit2)

    manager = _manager(workspace, global_skills)
    # 识别函数在 package_manager 模块顶层绑定（S-P4d 提升导入后），打桩目标是
    # 该模块的属性，不是 plugin_inspection 本体。
    monkeypatch.setattr(
        package_manager_module,
        "inspect_dsh_effort_slider",
        lambda *_args, **_kw: None,
    )
    with pytest.raises(SkillPackageError, match="no longer recognized"):
        manager.apply_pending_versions()
    record = manager.list_packages()[DSH_NAME]
    assert record["resolved_commit"] == commit1
    assert record["pending_version"]["resolved_commit"] == commit2
    assert not (manager.managed_skills_dir / DSH_NAME / "NOTES.md").exists()

    # 恢复识别后可重试成功：复核是可恢复门槛，不是一次性毒化。
    monkeypatch.undo()
    assert manager.apply_pending_versions() == 1
    assert manager.list_packages()[DSH_NAME]["resolved_commit"] == commit2


def test_dsh_manifest_record_rejects_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """_validate_dsh_manifest_record 负例：篡改 complete / 删 gaps.off /
    本地记录塞 git 字段 / 贡献与 gap 条目形状损坏（C-P4-7）。"""
    import copy as copy_module

    from agent_harness.skills.package_manager import (
        MANIFEST_FILENAME,
        SkillManifestError,
    )

    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    _cli_run(monkeypatch, capsys, "install", str(FIXTURE))
    manager = _manager(workspace, global_skills)
    manifest_path = workspace / MANIFEST_FILENAME
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = original["packages"][DSH_NAME]

    def _rewrite(record_override: dict) -> None:
        tampered = copy_module.deepcopy(original)
        tampered["packages"][DSH_NAME] = record_override
        manifest_path.write_text(
            json.dumps(tampered, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # 篡改 complete：Q1（待裁决，当前实现）的 needs-adaptation 不可被清单改写。
    bad = copy_module.deepcopy(record)
    bad["compatibility"]["status"] = "complete"
    _rewrite(bad)
    with pytest.raises(SkillManifestError):
        manager.list_packages()

    # 删 gaps.off：声明的缺口必须包含 off。
    bad = copy_module.deepcopy(record)
    bad["gaps"] = []
    _rewrite(bad)
    with pytest.raises(SkillManifestError):
        manager.list_packages()

    # 本地记录塞 git 字段：source_kind 缺失时禁止任何 git 维度。
    bad = copy_module.deepcopy(record)
    bad["resolved_commit"] = "0" * 40
    _rewrite(bad)
    with pytest.raises(SkillManifestError):
        manager.list_packages()

    # 贡献条目形状损坏：条目不是形状完整的映射（C-P4-7）。
    bad = copy_module.deepcopy(record)
    bad["contributions"][0] = "tampered"
    _rewrite(bad)
    with pytest.raises(SkillManifestError):
        manager.list_packages()

    # gap 条目形状损坏：off gap 缺 status（C-P4-7）。
    bad = copy_module.deepcopy(record)
    del bad["gaps"][0]["status"]
    _rewrite(bad)
    with pytest.raises(SkillManifestError):
        manager.list_packages()

    # 还原后恢复可读：上面的红是校验在挡，不是文件写坏。
    manifest_path.write_text(
        json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    assert (
        manager.list_packages()[DSH_NAME]["compatibility"]["status"]
        == "needs-adaptation"
    )


def test_dsh_disable_then_update_projection_coexists_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """disable 后 update 暂存：disabled 与 pending_restart=true 并存。"""
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    repo = tmp_path / "repo"
    _seed_repo(repo)
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
    _cli_run(monkeypatch, capsys, "enable", DSH_NAME)
    _cli_run(monkeypatch, capsys, "disable", DSH_NAME)
    _write(repo / DSH_NAME / "NOTES.md", "harmless extra file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "extra notes")
    commit2 = _git(repo, "rev-parse", "HEAD")
    _cli_run(monkeypatch, capsys, "update", DSH_NAME, "--ref", commit2)

    row = _cli_run(monkeypatch, capsys, "list")["packages"][0]
    assert row["saved_selection"] == "disabled"
    assert row["current_runtime"] == "not_selected"
    assert row["pending_restart"] is True
    assert row["pending_version"]["resolved_commit"] == commit2


def test_manifest_write_failure_recovers_both_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """清单写失败时两条同构车道都要回滚干净（S-P3 第 8 条）。

    失败注入同时驱动 Skill 车道与 DSH 车道：`_write_manifest` 抛错时，
    已换位的目标目录必须被换回 staging（不留半装目录）、清单里不留该包
    记录。两条车道的恢复路径只有 docstring 文字约束，这条用例把它锁住。
    """
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _configure_cli(monkeypatch, workspace, global_skills)
    manager = _manager(workspace, global_skills)

    def _boom(_self: object, _manifest: dict) -> None:
        raise OSError("injected manifest write failure")

    monkeypatch.setattr(SkillPackageManager, "_write_manifest", _boom)

    # Skill 车道
    skill_source = tmp_path / "sample-skill"
    _write(
        skill_source / "SKILL.md",
        "---\nname: sample-skill\ndescription: Local example.\n---\n\nBody.\n",
    )
    with pytest.raises(OSError, match="injected manifest write failure"):
        manager.install(str(skill_source))
    assert "sample-skill" not in manager.list_packages()
    assert not (manager.managed_skills_dir / "sample-skill").exists()

    # DSH 车道
    with pytest.raises(OSError, match="injected manifest write failure"):
        manager.install(str(FIXTURE))
    assert DSH_NAME not in manager.list_packages()
    assert not (manager.managed_skills_dir / DSH_NAME).exists()
