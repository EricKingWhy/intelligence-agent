"""#874 T5：CLI 的 `plugins enable <id> --scope <scope> --project <项目>`（AC1/AC2/AC3）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_harness import cli
from agent_harness.skills.package_manager import SkillPackageManager

PACKAGE_NAME = "shared-skill"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _package(root: Path, body: str = "Body.") -> Path:
    package = root / PACKAGE_NAME
    _write(
        package / "SKILL.md",
        f"---\nname: {PACKAGE_NAME}\ndescription: Shared example.\n---\n\n{body}\n",
    )
    return package


class _Cli:
    """两个项目 + 一个全局安装根，`--project` 决定命令作用于谁。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        self.global_dir = tmp_path / "home" / ".intelligence-agent" / "skills"
        self.alpha = tmp_path / "alpha"
        self.beta = tmp_path / "beta"
        self.settings = SimpleNamespace(
            workspace_dir=str(self.alpha),
            skill_global_dir=str(self.global_dir),
            capabilities=json.dumps({"skills": {"enabled": True}}),
        )
        monkeypatch.setattr(cli, "Settings", lambda: self.settings)
        monkeypatch.setattr(
            cli, "_query_current_skill_runtime", lambda *_: ("not_running", "stopped", None)
        )
        self._monkeypatch = monkeypatch
        self._capsys = capsys

    def run(self, *args: str) -> dict[str, object]:
        self._monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", *args])
        cli.main()
        return json.loads(self._capsys.readouterr().out)

    def fails(self, *args: str) -> str:
        self._monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", *args])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        return self._capsys.readouterr().err

    def project(self, workspace: Path) -> SkillPackageManager:
        return SkillPackageManager(workspace, global_skills_dir=self.global_dir)

    def global_(self) -> SkillPackageManager:
        return SkillPackageManager(
            self.alpha, scope="global", global_skills_dir=self.global_dir
        )


def test_cli_enables_global_package_for_one_project_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`enable <id> --scope global --project <项目>` 逐项目生效（T5 AC1）。"""
    cli_world = _Cli(tmp_path, monkeypatch, capsys)
    cli_world.run("install", str(_package(tmp_path / "src")), "--scope", "global")

    result = cli_world.run(
        "enable",
        PACKAGE_NAME,
        "--scope",
        "global",
        "--project",
        str(cli_world.beta),
    )

    assert result == {
        "name": PACKAGE_NAME,
        "saved_selection": "enabled",
        "selected_scope": "global",
        "project": str(cli_world.beta),
    }
    assert cli_world.project(cli_world.beta).enable_results()[PACKAGE_NAME][
        "selected_scope"
    ] == "global"
    # 另一个项目零增量：全局安装不会自动进入任何项目。
    assert cli_world.project(cli_world.alpha).enable_results() == {}


def test_cli_enable_without_scope_refuses_ambiguous_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """同 ID 并存未选择 ⇒ CLI 拒绝并列出两项来源（T5 AC2）。"""
    cli_world = _Cli(tmp_path, monkeypatch, capsys)
    project_source = _package(tmp_path / "project-src", body="Project body.")
    global_source = _package(tmp_path / "global-src", body="Global body.")
    cli_world.run("install", str(project_source))
    cli_world.run("install", str(global_source), "--scope", "global")

    message = cli_world.fails("enable", PACKAGE_NAME)

    assert "both project and global scope" in message
    assert str(project_source) in message
    assert str(global_source) in message
    assert cli_world.project(cli_world.alpha).enable_results() == {}


def test_cli_enable_scope_selects_the_only_assembled_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """显式 --scope 后只装配所选一版（T5 AC2）；改选后旧选择被替换。"""
    cli_world = _Cli(tmp_path, monkeypatch, capsys)
    cli_world.run("install", str(_package(tmp_path / "project-src")))
    cli_world.run("install", str(_package(tmp_path / "global-src")), "--scope", "global")

    chosen_project = cli_world.run("enable", PACKAGE_NAME, "--scope", "project")
    assert chosen_project["selected_scope"] == "project"
    assert set(cli_world.project(cli_world.alpha).enabled_skill_digests()) == {PACKAGE_NAME}
    assert cli_world.project(cli_world.alpha).enabled_global_skill_digests() == {}

    chosen_global = cli_world.run("enable", PACKAGE_NAME, "--scope", "global")
    assert chosen_global["selected_scope"] == "global"
    assert cli_world.project(cli_world.alpha).enabled_skill_digests() == {}
    assert set(cli_world.project(cli_world.alpha).enabled_global_skill_digests()) == {
        PACKAGE_NAME
    }


def test_cli_disable_clears_the_enable_result_without_switching_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`disable` 只清本项目启用结果，不静默切到另一 scope（T5 AC3）。"""
    cli_world = _Cli(tmp_path, monkeypatch, capsys)
    cli_world.run("install", str(_package(tmp_path / "project-src")))
    cli_world.run("install", str(_package(tmp_path / "global-src")), "--scope", "global")
    cli_world.run("enable", PACKAGE_NAME, "--scope", "global")

    assert cli_world.run("disable", PACKAGE_NAME) == {
        "name": PACKAGE_NAME,
        "saved_selection": "disabled",
        "previous_scope": "global",
        "project": str(cli_world.alpha),
    }
    manager = cli_world.project(cli_world.alpha)
    assert manager.enable_results() == {}
    assert manager.enabled_skill_digests() == {}
    assert manager.enabled_global_skill_digests() == {}


def test_cli_list_reports_this_projects_selection_of_both_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`list` 并列两个安装根与本项目的启用结果（T5 AC1/AC2 的可见面）。"""
    cli_world = _Cli(tmp_path, monkeypatch, capsys)
    cli_world.run("install", str(_package(tmp_path / "project-src")))
    cli_world.run("install", str(_package(tmp_path / "global-src")), "--scope", "global")

    before = cli_world.run("list")
    assert [item["scope"] for item in before["packages"]] == ["project"]
    assert before["packages"][0]["saved_selection"] == "disabled"
    assert before["packages"][0]["selected_scope"] is None
    assert [
        item["saved_selection"] for item in before["available_global_packages"]
    ] == ["not_selected"]

    cli_world.run("enable", PACKAGE_NAME, "--scope", "global")
    after = cli_world.run("list")
    # 选了 global：全局那条可用项标 enabled，项目记录行的 saved_selection 仍是它自身
    # 的 enabled 位（T4 口径），本项目选了哪一版看 selected_scope。
    assert after["available_global_packages"][0]["saved_selection"] == "enabled"
    assert after["packages"][0]["saved_selection"] == "disabled"
    assert after["packages"][0]["selected_scope"] == "global"
    # 全局安装根自己的视图（T4 的 --scope global）不受本项目选择影响。
    assert (
        cli_world.run("list", "--scope", "global")["packages"][0]["saved_selection"]
        == "not_selected"
    )

    # 别的项目看同一份全局记录，但没有自己的项目记录、也没有任何启用结果。
    other = cli_world.run("list", "--project", str(cli_world.beta))
    assert other["packages"] == []
    assert [
        item["saved_selection"] for item in other["available_global_packages"]
    ] == ["not_selected"]


def test_cli_list_does_not_report_a_same_id_global_copy_as_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """选了项目版时，同名全局版不得被列成已启用（T5 AC2：只装配所选一版）。

    两条安装记录同名并存，启用结果只可能属于其中一条。若可用性视图只看
    「名字在不在启用结果表里」，选了项目版会把全局版一并报成 enabled——
    用户会以为两版同时生效。判据必须带上 selected_scope。
    """
    cli_world = _Cli(tmp_path, monkeypatch, capsys)
    cli_world.run("install", str(_package(tmp_path / "project-src")))
    cli_world.run("install", str(_package(tmp_path / "global-src")), "--scope", "global")

    cli_world.run("enable", PACKAGE_NAME, "--scope", "project")
    listing = cli_world.run("list")

    assert listing["packages"][0]["selected_scope"] == "project"
    assert [
        item["saved_selection"] for item in listing["available_global_packages"]
    ] == ["not_selected"]

    # 对照：显式改选全局版后，同一条可用项才标 enabled。
    cli_world.run("enable", PACKAGE_NAME, "--scope", "global")
    assert [
        item["saved_selection"]
        for item in cli_world.run("list")["available_global_packages"]
    ] == ["enabled"]


def test_cli_runtime_projection_follows_the_selected_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """选了 global 时，运行态必须拿**全局**受管路径比对（T5 AC3）。

    只看项目根下有没有同名文件的话，「全局版正在生效」与「全局版装配缺席」会投影
    成同一个值，用户看不出自己选的那一版到底有没有生效。
    """
    import os

    cli_world = _Cli(tmp_path, monkeypatch, capsys)
    cli_world.run("install", str(_package(tmp_path / "global-src")), "--scope", "global")
    cli_world.run("enable", PACKAGE_NAME, "--scope", "global")
    global_managed = cli_world.global_().managed_skills_dir / PACKAGE_NAME / "SKILL.md"

    runtime: list = []
    monkeypatch.setattr(cli, "_query_current_skill_runtime", lambda *_: runtime[0])
    runtime.append(
        ("running", "live", {os.path.normcase(os.path.realpath(str(global_managed)))})
    )
    entry = cli_world.run("list")["available_global_packages"][0]
    assert entry["current_runtime"] == "discovered"
    assert entry["pending_restart"] is False

    # 全局版失效（装配缺席）⇒ 被选却不在运行态 = 待重启，不是「无事发生」。
    runtime[0] = ("running", "live", set())
    entry = cli_world.run("list")["available_global_packages"][0]
    assert entry["current_runtime"] == "not_discovered"
    assert entry["pending_restart"] is True
