from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_harness import cli
from agent_harness.instance_lock import InstanceLockError
from agent_harness.skills import inspection
from agent_harness.skills.inspection import inspect_skill_package


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def test_inspection_reports_skill_metadata_resources_and_requirements(tmp_path: Path) -> None:
    source = tmp_path / "pdf-processing"
    _write(
        source / "SKILL.md",
        "---\n"
        "name: pdf-processing\n"
        "description: Extract and summarize PDF documents.\n"
        "license: MIT\n"
        "compatibility: Requires Python 3.11 and git.\n"
        "allowed-tools: Read Bash\n"
        "metadata:\n"
        "  author: example\n"
        "---\n"
        "Read [the guide](references/REFERENCE.md). Run `scripts/check.py`.\n",
    )
    _write(source / "scripts" / "check.py", "print('must not run')\n")
    _write(source / "references" / "REFERENCE.md", "See [the image](assets/logo.bin).\n")
    _write(source / "assets" / "logo.bin", b"image-data")

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "needs-adaptation"
    assert report["name"] == "pdf-processing"
    assert report["license"] == "MIT"
    assert report["metadata"]["metadata"] == {"author": "example"}
    assert report["resources"]["scripts"][0]["path"] == "scripts/check.py"
    assert report["resources"]["references"][0]["path"] == "references/REFERENCE.md"
    assert report["resources"]["assets"][0]["path"] == "assets/logo.bin"
    assert report["requirements"]
    assert report["errors"] == []


@pytest.mark.parametrize(
    "command_guidance",
    ["Run `pandoc` to render the report.", "Execute `pandoc --version` before rendering."],
)
def test_inspection_marks_bare_inline_host_command_for_manual_review(
    tmp_path: Path, command_guidance: str
) -> None:
    source = tmp_path / "command-skill"
    _write(
        source / "SKILL.md",
        "---\nname: command-skill\ndescription: Uses a host command.\n---\n\n"
        f"{command_guidance}\n",
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "needs-adaptation"
    assert any(
        requirement["kind"] == "command-runtime"
        and requirement["name"] == "pandoc"
        and requirement["support"] == "manual_review"
        for requirement in report["requirements"]
    )


@pytest.mark.parametrize(
    ("reference", "expected_code"),
    [
        ("missing.md", "MISSING_RESOURCE"),
        ("../../outside.txt", "REFERENCE_OUTSIDE_PACKAGE"),
        ("C:/outside.md", "REFERENCE_OUTSIDE_PACKAGE"),
        ("file:///outside.md", "REFERENCE_OUTSIDE_PACKAGE"),
    ],
)
def test_inspection_rejects_missing_and_escaping_references(
    tmp_path: Path, reference: str, expected_code: str
) -> None:
    source = tmp_path / "my-skill"
    _write(
        source / "SKILL.md",
        f"---\nname: my-skill\ndescription: Example.\n---\nSee [reference]({reference}).\n",
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert expected_code in {error["code"] for error in report["errors"]}
    assert all(error["path"] for error in report["errors"])
    if expected_code == "MISSING_RESOURCE":
        assert any(
            error["code"] == expected_code and error["path"].endswith("missing.md")
            for error in report["errors"]
        )


def test_inspection_reports_directory_conflict(tmp_path: Path) -> None:
    source = tmp_path / "other-name"
    _write(
        source / "SKILL.md",
        "---\nname: same-name\ndescription: Example.\n---\n",
    )
    existing_skills = tmp_path / "workspace" / "skills"
    _write(
        existing_skills / "same-name" / "SKILL.md",
        "---\nname: same-name\ndescription: Installed.\n---\n",
    )

    report = inspect_skill_package(
        source, scope="project", existing_skills=existing_skills
    )

    assert report["status"] == "unsupported"
    assert {error["code"] for error in report["errors"]} >= {
        "DIRECTORY_NAME_MISMATCH",
        "NAME_CONFLICT",
    }


def test_inspection_rejects_invalid_agent_skills_name(tmp_path: Path) -> None:
    source = tmp_path / "bad_name"
    _write(
        source / "SKILL.md",
        "---\nname: bad_name\ndescription: Example.\n---\n",
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "INVALID_SKILL_NAME" in {error["code"] for error in report["errors"]}


def test_inspection_rejects_missing_inline_resource(tmp_path: Path) -> None:
    source = tmp_path / "inline-reference"
    _write(
        source / "SKILL.md",
        "---\nname: inline-reference\ndescription: Example.\n---\nRun `scripts/missing.py`.\n",
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "MISSING_RESOURCE" in {error["code"] for error in report["errors"]}


def test_inspection_checks_reference_style_markdown_links(tmp_path: Path) -> None:
    source = tmp_path / "reference-link"
    _write(
        source / "SKILL.md",
        "---\nname: reference-link\ndescription: Example.\n---\nSee [the guide][guide].\n\n[guide]: references/missing.md\n",
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "MISSING_RESOURCE" in {error["code"] for error in report["errors"]}


def test_nested_markdown_references_resolve_from_skill_root(tmp_path: Path) -> None:
    source = tmp_path / "root-relative-skill"
    _write(
        source / "SKILL.md",
        "---\nname: root-relative-skill\ndescription: Example.\n---\n"
        "See [the guide](references/REFERENCE.md).\n",
    )
    _write(
        source / "references" / "REFERENCE.md",
        "See [the image](assets/logo.bin) and [online](https://example.test/guide).\n",
    )
    _write(source / "assets" / "logo.bin", b"image-data")

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "complete"
    assert report["errors"] == []


def test_inspection_lists_declared_dependencies_for_manual_review(tmp_path: Path) -> None:
    source = tmp_path / "dependency-skill"
    _write(
        source / "SKILL.md",
        "---\nname: dependency-skill\ndescription: Example.\n---\n",
    )
    _write(source / "requirements.txt", "requests>=2.0\n# comment\nPyYAML\n")
    _write(
        source / "package.json",
        '{"dependencies":{"left-pad":"^1.3.0"},"devDependencies":{"vitest":"^1.0.0"}}\n',
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "needs-adaptation"
    assert {item["name"] for item in report["dependencies"]} == {
        "requests",
        "PyYAML",
        "left-pad",
        "vitest",
    }
    assert all(item["support"] == "manual_review" for item in report["dependencies"])


def test_inspection_lists_pyproject_build_and_optional_dependencies(tmp_path: Path) -> None:
    source = tmp_path / "pyproject-dependencies"
    _write(
        source / "SKILL.md",
        "---\nname: pyproject-dependencies\ndescription: Example.\n---\n",
    )
    _write(
        source / "pyproject.toml",
        "[build-system]\nrequires = ['setuptools>=68']\n"
        "[project]\nrequires-python = '>=3.11'\ndependencies = ['requests>=2']\n"
        "[project.optional-dependencies]\ntest = ['pytest>=8']\n",
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "needs-adaptation"
    assert {item["name"] for item in report["dependencies"]} == {
        "setuptools",
        "requests",
        "pytest",
    }


def test_inspection_marks_dynamic_python_requirement_for_manual_review(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dynamic-python-requirement"
    _write(
        source / "SKILL.md",
        "---\nname: dynamic-python-requirement\ndescription: Example.\n---\n",
    )
    _write(source / "pyproject.toml", "[project]\ndynamic = ['requires-python']\n")

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "needs-adaptation"
    assert {
        (item["kind"], item["name"], item["support"])
        for item in report["requirements"]
    } >= {("runtime", "requires-python (dynamic)", "manual_review")}


@pytest.mark.parametrize(
    ("manifest", "content"),
    [("package.json", "[]\n"), ("pyproject.toml", "[project]\ndependencies = 'requests'\n")],
)
def test_inspection_rejects_unrecognized_dependency_manifest_shapes(
    tmp_path: Path, manifest: str, content: str
) -> None:
    source = tmp_path / "invalid-dependency-manifest"
    _write(
        source / "SKILL.md",
        "---\nname: invalid-dependency-manifest\ndescription: Example.\n---\n",
    )
    _write(source / manifest, content)

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "DEPENDENCY_FILE_INVALID" in {error["code"] for error in report["errors"]}


@pytest.mark.parametrize(
    "include",
    ["-r base.txt", "--requirement base.txt", "-rbase.txt", "--requirement=base.txt"],
)
def test_inspection_follows_requirements_includes_without_executing_them(
    tmp_path: Path, include: str
) -> None:
    source = tmp_path / "requirements-include-skill"
    _write(
        source / "SKILL.md",
        "---\nname: requirements-include-skill\ndescription: Example.\n---\n",
    )
    _write(source / "requirements.txt", f"{include}\n")
    _write(source / "base.txt", "requests>=2.0\n")

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "needs-adaptation"
    assert len(report["dependencies"]) == 1
    dependency = report["dependencies"][0]
    assert dependency["name"] == "requests"
    assert dependency["source"] == "base.txt"
    assert dependency["support"] == "manual_review"


@pytest.mark.parametrize("include", ["../../outside.txt", "%2e%2e/outside.txt"])
def test_inspection_rejects_requirements_include_outside_package(
    tmp_path: Path, include: str
) -> None:
    source = tmp_path / "escaping-requirements-include"
    _write(
        source / "SKILL.md",
        "---\nname: escaping-requirements-include\ndescription: Example.\n---\n",
    )
    _write(source / "requirements.txt", f"-r {include}\n")

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "DEPENDENCY_INCLUDE_OUTSIDE_PACKAGE" in {
        error["code"] for error in report["errors"]
    }


def test_inspection_lists_poetry_dependencies_and_python_requirement(tmp_path: Path) -> None:
    source = tmp_path / "poetry-dependencies"
    _write(
        source / "SKILL.md",
        "---\nname: poetry-dependencies\ndescription: Example.\n---\n",
    )
    _write(
        source / "pyproject.toml",
        "[tool.poetry.dependencies]\npython = '^3.11'\nrequests = '^2.31'\n"
        "[tool.poetry.group.test.dependencies]\npytest = '^8'\n",
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "needs-adaptation"
    assert {item["name"] for item in report["dependencies"]} == {"requests", "pytest"}
    assert any(item["kind"] == "runtime" for item in report["requirements"])


def test_inspection_rejects_missing_requirements_include(tmp_path: Path) -> None:
    source = tmp_path / "missing-requirements-include"
    _write(
        source / "SKILL.md",
        "---\nname: missing-requirements-include\ndescription: Example.\n---\n",
    )
    _write(source / "requirements.txt", "--requirement missing.txt\n")

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "MISSING_DEPENDENCY_FILE" in {error["code"] for error in report["errors"]}


def test_inspection_bounds_empty_directory_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "many-directories"
    _write(
        source / "SKILL.md",
        "---\nname: many-directories\ndescription: Example.\n---\n",
    )
    (source / "empty-one").mkdir(parents=True)
    (source / "empty-two").mkdir()
    monkeypatch.setattr(inspection, "_MAX_PACKAGE_ENTRIES", 2)

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "PACKAGE_TOO_MANY_ENTRIES" in {error["code"] for error in report["errors"]}


def test_inspection_rejects_skill_file_replaced_between_check_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "changed-skill"
    skill_file = source / "SKILL.md"
    replacement = tmp_path / "replacement.md"
    _write(
        skill_file,
        "---\nname: changed-skill\ndescription: Original.\n---\n",
    )
    _write(
        replacement,
        "---\nname: changed-skill\ndescription: Replaced.\n---\n",
    )
    real_open_package_file = inspection._open_package_file
    replaced = False

    def replace_before_open(root: Path, path: Path, changed_code: str) -> int:
        nonlocal replaced
        if path == skill_file and not replaced:
            replacement.replace(skill_file)
            replaced = True
        return real_open_package_file(root, path, changed_code)

    monkeypatch.setattr(inspection, "_open_package_file", replace_before_open)

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "SKILL_FILE_CHANGED" in {error["code"] for error in report["errors"]}


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor boundary check")
def test_inspection_rejects_parent_symlink_swapped_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "parent-race-skill"
    outside = tmp_path / "outside"
    _write(
        source / "SKILL.md",
        "---\nname: parent-race-skill\ndescription: Example.\n---\n"
        "See [the guide](references/REFERENCE.md).\n",
    )
    _write(source / "references" / "REFERENCE.md", "Package content.\n")
    _write(outside / "REFERENCE.md", "Outside content.\n")
    real_open = inspection._open_posix_package_file
    swapped = False

    def swap_parent_before_open(
        root: Path, path: Path, flags: int, changed_code: str
    ) -> int:
        nonlocal swapped
        if path == source / "references" / "REFERENCE.md" and not swapped:
            package_references = source / "references"
            package_references.rename(source / "references-original")
            package_references.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(root, path, flags, changed_code)

    if (
        os.open not in os.supports_dir_fd
        or not getattr(os, "O_DIRECTORY", 0)
        or not getattr(os, "O_NOFOLLOW", 0)
    ):
        pytest.skip("Safe package-relative file reads are unavailable on this platform")
    monkeypatch.setattr(inspection, "_open_posix_package_file", swap_parent_before_open)

    report = inspect_skill_package(source, scope="project")

    assert swapped
    assert report["status"] == "unsupported"
    assert "REFERENCE_FILE_CHANGED" in {error["code"] for error in report["errors"]}


@pytest.mark.skipif(os.name != "nt", reason="Windows opened-handle path check")
def test_inspection_rejects_open_handle_outside_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "handle-boundary-skill"
    outside = tmp_path / "outside" / "SKILL.md"
    _write(
        source / "SKILL.md",
        "---\nname: handle-boundary-skill\ndescription: Example.\n---\n",
    )
    _write(outside, "---\nname: outside\ndescription: Must not be read.\n---\n")
    monkeypatch.setattr(inspection, "_windows_open_handle_path", lambda _fd: outside)

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert report["name"] is None
    assert "SKILL_FILE_CHANGED" in {error["code"] for error in report["errors"]}


def test_inspection_does_not_read_skill_symlink_outside_package(tmp_path: Path) -> None:
    source = tmp_path / "symlinked-skill"
    outside = tmp_path / "outside" / "SKILL.md"
    _write(outside, "---\nname: outside\ndescription: Do not read.\n---\n")
    source.mkdir()
    try:
        (source / "SKILL.md").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert report["name"] is None
    assert "SYMLINK_OUTSIDE_PACKAGE" in {error["code"] for error in report["errors"]}


def test_inspection_reports_oversized_skill_file(tmp_path: Path) -> None:
    source = tmp_path / "large-skill"
    _write(
        source / "SKILL.md",
        "---\nname: large-skill\ndescription: Example.\n---\n" + ("x" * 1_000_001),
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "SKILL_FILE_TOO_LARGE" in {error["code"] for error in report["errors"]}


def test_inspection_reports_oversized_markdown_reference(tmp_path: Path) -> None:
    source = tmp_path / "large-reference"
    _write(
        source / "SKILL.md",
        "---\nname: large-reference\ndescription: Example.\n---\n"
        "See [the guide](references/REFERENCE.md).\n",
    )
    _write(source / "references" / "REFERENCE.md", "x" * 1_000_001)

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "REFERENCE_FILE_TOO_LARGE" in {error["code"] for error in report["errors"]}


def test_cli_plugins_inspect_is_read_only_and_never_runs_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    source = tmp_path / "safe-skill"
    marker = tmp_path / "script-ran.txt"
    _write(
        source / "SKILL.md",
        "---\nname: safe-skill\ndescription: Example.\n---\n"
        "Read [the reference](references/REFERENCE.md).\n",
    )
    _write(source / "scripts" / "sentinel.py", f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    _write(source / "references" / "REFERENCE.md", "See [the asset](assets/data.bin).\n")
    _write(source / "assets" / "data.bin", b"asset bytes")
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _write(workspace / "skills" / "installed" / "SKILL.md", "installed bytes\n")
    _write(workspace / "plugin-installs.json", '{"project": []}\n')
    _write(global_skills / "plugin-installs.json", '{"global": []}\n')
    _write(global_skills / "installed" / "SKILL.md", "global skill bytes\n")
    before = _snapshot(source, workspace, global_skills)

    _configure_cli_inspect(monkeypatch, source, workspace, global_skills)

    cli.main()

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["status"] == "needs-adaptation"
    assert any(item["kind"] == "script-runtime" for item in report["requirements"])
    assert report["license"] == "\u672a\u58f0\u660e"
    assert report["resources"]["references"][0]["path"] == "references/REFERENCE.md"
    assert report["resources"]["assets"][0]["path"] == "assets/data.bin"
    assert marker.exists() is False
    assert _snapshot(source, workspace, global_skills) == before


@pytest.mark.parametrize(
    ("skill_name", "body", "expected_code"),
    [
        ("cli-missing", "See [reference](references/missing.md).\n", "MISSING_RESOURCE"),
        ("cli-escape", "See [reference](../../outside.txt).\n", "REFERENCE_OUTSIDE_PACKAGE"),
        ("cli-absolute", "See [reference](C:/outside.txt).\n", "REFERENCE_OUTSIDE_PACKAGE"),
        ("cli-conflict", "No references.\n", "NAME_CONFLICT"),
    ],
)
def test_cli_plugins_inspect_reports_negative_samples_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    skill_name: str,
    body: str,
    expected_code: str,
) -> None:
    source = tmp_path / skill_name
    _write(
        source / "SKILL.md",
        f"---\nname: {skill_name}\ndescription: Example.\n---\n{body}",
    )
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _write(workspace / "plugin-installs.json", '{"project": []}\n')
    _write(global_skills / "plugin-installs.json", '{"global": []}\n')
    if expected_code == "NAME_CONFLICT":
        _write(
            workspace / "skills" / skill_name / "SKILL.md",
            f"---\nname: {skill_name}\ndescription: Installed.\n---\n",
        )
    before = _snapshot(source, workspace, global_skills)
    _configure_cli_inspect(monkeypatch, source, workspace, global_skills)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "unsupported"
    assert expected_code in {error["code"] for error in report["errors"]}
    assert _snapshot(source, workspace, global_skills) == before


def test_cli_plugins_lifecycle_tracks_saved_and_live_runtime_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    source = tmp_path / "complete-skill"
    _write(
        source / "SKILL.md",
        "---\nname: complete-skill\ndescription: Complete local package.\n---\n"
        "Read [the guide](references/guide.md).\n",
    )
    _write(source / "references" / "guide.md", "Guide.\n")
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    runtime = ["unavailable", "host Skills catalog is not queryable", None]
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            workspace_dir=str(workspace),
            skill_global_dir=str(global_skills),
            capabilities=json.dumps({"skills": {"enabled": True}}),
        ),
    )
    monkeypatch.setattr(cli, "_query_current_skill_runtime", lambda *_: tuple(runtime))
    monkeypatch.setattr(cli, "setup_logging", lambda *args, **kwargs: pytest.fail("plugins command initialized logging"))
    monkeypatch.setattr(cli, "flush_process_sink", lambda *args, **kwargs: pytest.fail("plugins command flushed telemetry"))

    def run(*args: str) -> str:
        monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", *args])
        cli.main()
        return capsys.readouterr().out

    installed = json.loads(run("install", str(source)))
    assert installed["name"] == "complete-skill"
    assert installed["enabled"] is False
    assert installed["compatibility"]["status"] == "complete"

    run("enable", "complete-skill")
    listing = json.loads(run("list"))
    package = listing["packages"][0]
    assert package["saved_selection"] == "enabled"
    assert package["current_runtime"] == "unavailable"
    assert package["pending_restart"] is None

    run("disable", "complete-skill")
    listing = json.loads(run("list"))
    assert listing["packages"][0]["saved_selection"] == "disabled"
    assert listing["packages"][0]["current_runtime"] == "unavailable"
    assert listing["packages"][0]["pending_restart"] is None

    managed_skill = (
        workspace / "skills" / ".managed" / "complete-skill" / "SKILL.md"
    ).resolve()
    runtime[:] = ["running", "authenticated live Skills catalog", {os.path.normcase(str(managed_skill))}]
    run("enable", "complete-skill")
    listing = json.loads(run("list"))
    assert listing["packages"][0]["current_runtime"] == "discovered"
    assert listing["packages"][0]["pending_restart"] is False

    runtime[:] = ["running", "authenticated live Skills catalog", set()]
    listing = json.loads(run("list"))
    assert listing["packages"][0]["current_runtime"] == "not_discovered"
    assert listing["packages"][0]["pending_restart"] is True

    run("disable", "complete-skill")
    listing = json.loads(run("list"))
    assert listing["packages"][0]["saved_selection"] == "disabled"
    assert listing["packages"][0]["current_runtime"] == "not_discovered"
    assert listing["packages"][0]["pending_restart"] is False

    class _RunningRuntime:
        def acquire(self):
            raise InstanceLockError("held")

    monkeypatch.setattr(cli, "InstanceLock", lambda *_args, **_kwargs: _RunningRuntime())
    with pytest.raises(SystemExit) as excinfo:
        run("remove", "complete-skill")
    assert excinfo.value.code == 2
    assert (workspace / "skills" / ".managed" / "complete-skill" / "SKILL.md").exists()

    class _StoppedRuntime:
        def acquire(self):
            return self

        def release(self):
            pass

    monkeypatch.setattr(cli, "InstanceLock", lambda *_args, **_kwargs: _StoppedRuntime())
    run("remove", "complete-skill")
    assert not (workspace / "skills" / ".managed" / "complete-skill").exists()
    assert json.loads(run("list"))["packages"] == []


def test_cli_git_skill_install_update_list_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = tmp_path / "repo"
    skill = repo / "packages" / "cli-skill"
    _write(
        skill / "SKILL.md",
        "---\nname: cli-skill\ndescription: CLI lifecycle example.\n---\n\nOld version.\n",
    )
    _write(skill / "references" / "guide.md", "Old guide.\n")
    repo.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-qm", "old version")
    old_commit = git("rev-parse", "HEAD")
    _write(
        skill / "SKILL.md",
        "---\nname: cli-skill\ndescription: CLI lifecycle example.\n---\n\nNew version.\n",
    )
    _write(skill / "references" / "guide.md", "New guide.\n")
    git("add", ".")
    git("commit", "-qm", "new version")
    new_commit = git("rev-parse", "HEAD")

    workspace = tmp_path / "workspace"
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            workspace_dir=str(workspace),
            skill_global_dir=str(tmp_path / "global-skills"),
            capabilities=json.dumps({"skills": {"enabled": True}}),
        ),
    )
    monkeypatch.setattr(
        cli, "_query_current_skill_runtime", lambda *_: ("not_running", "stopped", None)
    )

    def run(*args: str) -> dict[str, object]:
        monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", *args])
        cli.main()
        return json.loads(capsys.readouterr().out)

    installed = run(
        "install-git",
        repo.as_uri(),
        "--ref",
        old_commit,
        "--subdirectory",
        "packages/cli-skill",
    )
    assert installed["resolved_commit"] == old_commit
    assert run("update", "cli-skill", "--ref", new_commit)["pending_version"][
        "resolved_commit"
    ] == new_commit
    listed = run("list")["packages"][0]
    assert listed["current_version"]["resolved_commit"] == old_commit
    assert listed["pending_version"]["resolved_commit"] == new_commit
    assert listed["pending_restart"] is True

    from agent_harness.skills.package_manager import SkillPackageManager

    SkillPackageManager(workspace).apply_pending_versions()
    rolled_back = run("rollback", "cli-skill")
    assert rolled_back["pending_version"]["resolved_commit"] == old_commit


@pytest.mark.parametrize(
    "capabilities",
    [None, '{"skills":{"enabled":false}}'],
)
def test_cli_plugins_enable_requires_skills_capability_without_changing_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    capabilities: str | None,
) -> None:
    source = tmp_path / "complete-skill"
    _write(
        source / "SKILL.md",
        "---\nname: complete-skill\ndescription: Complete local package.\n---\n\nBody.\n",
    )
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            workspace_dir=str(workspace),
            skill_global_dir=str(global_skills),
            **({} if capabilities is None else {"capabilities": capabilities}),
        ),
    )
    monkeypatch.setattr(
        cli,
        "_query_current_skill_runtime",
        lambda *_: ("not_running", "absent", None),
    )

    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "install", str(source)])
    cli.main()
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "enable", "complete-skill"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2
    assert "Skills capability is enabled in CAPABILITIES" in capsys.readouterr().err
    manifest = json.loads((workspace / "plugin-installs.json").read_text(encoding="utf-8"))
    assert manifest["packages"]["complete-skill"]["enabled"] is False


def test_cli_plugins_install_uses_configured_extension_skill_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    source = tmp_path / "duplicate-skill"
    _write(
        source / "SKILL.md",
        "---\nname: duplicate-skill\ndescription: Imported package.\n---\n\nBody.\n",
    )
    extension = tmp_path / "extension-skills"
    _write(
        extension / "custom-folder" / "SKILL.md",
        "---\nname: duplicate-skill\ndescription: Existing extension Skill.\n---\n\nBody.\n",
    )
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    settings = SimpleNamespace(
        workspace_dir=str(workspace),
        skill_global_dir=str(global_skills),
        capabilities=json.dumps(
            {"skills": {"options": {"directories": [str(extension)]}}}
        ),
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "install", str(source)])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2
    assert "configured Skill 'duplicate-skill' already exists" in capsys.readouterr().err
    assert not (workspace / "plugin-installs.json").exists()


def test_cli_plugins_lists_adaptation_requirements_and_rejects_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    source = tmp_path / "scripted-skill"
    _write(
        source / "SKILL.md",
        "---\nname: scripted-skill\ndescription: Keeps a script disabled.\n---\n"
        "Read [the helper](scripts/run.py).\n",
    )
    script = b"raise RuntimeError('must never run')\n"
    _write(source / "scripts" / "run.py", script)
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(workspace_dir=str(workspace), skill_global_dir=str(global_skills)),
    )
    monkeypatch.setattr(
        cli,
        "_query_current_skill_runtime",
        lambda *_: ("not_running", "absent", None),
    )
    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "install", str(source)])
    cli.main()
    installed = json.loads(capsys.readouterr().out)

    assert installed["enabled"] is False
    assert installed["compatibility"]["status"] == "needs-adaptation"
    assert any(
        requirement["kind"] == "script-runtime"
        for requirement in installed["compatibility"]["requirements"]
    )
    copied_script = workspace / "skills" / ".managed" / "scripted-skill" / "scripts" / "run.py"
    assert copied_script.read_bytes() == script

    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "enable", "scripted-skill"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 2
    assert copied_script.read_bytes() == script

    monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", "list"])
    cli.main()
    listing = json.loads(capsys.readouterr().out)
    package = listing["packages"][0]
    assert package["saved_selection"] == "disabled"
    assert package["compatibility"]["status"] == "needs-adaptation"
    assert package["compatibility"]["requirements"][0]["name"] == "scripts/run.py"


def test_current_runtime_query_falls_back_to_instance_lock_without_host_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"

    stopped = cli._query_current_skill_runtime(str(workspace))
    assert stopped[0] == "not_running"

    class _LockedRuntime:
        def acquire(self):
            raise InstanceLockError("held")

    monkeypatch.setattr(cli, "InstanceLock", lambda *_args, **_kwargs: _LockedRuntime())
    active = cli._query_current_skill_runtime(str(workspace))
    assert active[0] == "unavailable"


def _configure_cli_inspect(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    workspace: Path,
    global_skills: Path,
) -> None:
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            workspace_dir=str(workspace), skill_global_dir=str(global_skills)
        ),
    )
    monkeypatch.setattr(
        cli,
        "setup_logging",
        lambda *args, **kwargs: pytest.fail("read-only inspect must not initialize logging"),
    )
    monkeypatch.setattr(
        cli,
        "InstanceLock",
        lambda *args, **kwargs: pytest.fail("read-only inspect must not acquire the runtime lock"),
    )
    monkeypatch.setattr(
        cli,
        "flush_process_sink",
        lambda *args, **kwargs: pytest.fail("read-only inspect must not flush telemetry"),
    )
    monkeypatch.setattr(
        sys, "argv", ["agent-harness", "plugins", "inspect", str(source), "--scope", "project"]
    )


def _snapshot(*roots: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix() + f"@{root.name}": (
            path.read_bytes() if path.is_file() else None
        )
        for root in roots
        for path in sorted(root.rglob("*"))
    }


def test_cli_global_scope_is_shared_and_project_list_keeps_it_unselected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    global_skills = tmp_path / "home" / ".intelligence-agent" / "skills"
    workspace = tmp_path / "project-a"
    source = tmp_path / "global-skill"
    _write(
        source / "SKILL.md",
        "---\nname: global-skill\ndescription: Shared package.\n---\n\nBody.\n",
    )
    settings = SimpleNamespace(
        workspace_dir=str(workspace),
        skill_global_dir=str(global_skills),
        capabilities=json.dumps({"skills": {"enabled": True}}),
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        cli, "_query_current_skill_runtime", lambda *_: ("not_running", "stopped", None)
    )

    def run(*args: str) -> dict[str, object]:
        monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", *args])
        cli.main()
        return json.loads(capsys.readouterr().out)

    installed = run("install", str(source), "--scope", "global")
    assert installed["scope"] == "global"
    listing = run("list")
    assert listing["packages"] == []
    available = listing["available_global_packages"]
    assert available[0]["name"] == "global-skill"
    assert available[0]["saved_selection"] == "not_selected"
    assert available[0]["availability"] == "available_to_enable"
    assert available[0]["runtime_version"] is None

    settings.workspace_dir = str(tmp_path / "project-b")
    second_project = run("list")
    assert second_project["packages"] == []
    assert second_project["available_global_packages"] == available

    project_source = tmp_path / "project-skill"
    _write(
        project_source / "SKILL.md",
        "---\nname: project-skill\ndescription: Local package.\n---\n\nBody.\n",
    )
    run("install", str(project_source))
    project_listing = run("list")
    assert [item["name"] for item in project_listing["packages"]] == ["project-skill"]
    assert [item["name"] for item in project_listing["available_global_packages"]] == [
        "global-skill"
    ]


def test_cli_global_git_update_rollback_and_remove_route_to_global_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = tmp_path / "repo"
    source = repo / "packages" / "global-git-skill"

    def write_version(body: str) -> None:
        _write(
            source / "SKILL.md",
            "---\nname: global-git-skill\ndescription: Global Git package.\n---\n\n"
            f"{body}\n",
        )

    write_version("Old version.")
    repo.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

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

    workspace = tmp_path / "project"
    settings = SimpleNamespace(
        workspace_dir=str(workspace),
        skill_global_dir=str(tmp_path / "home" / ".intelligence-agent" / "skills"),
        capabilities=json.dumps({"skills": {"enabled": True}}),
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        cli, "_query_current_skill_runtime", lambda *_: ("not_running", "stopped", None)
    )

    def run(*args: str) -> dict[str, object]:
        monkeypatch.setattr(sys, "argv", ["agent-harness", "plugins", *args])
        cli.main()
        return json.loads(capsys.readouterr().out)

    installed = run(
        "install-git",
        repo.as_uri(),
        "--ref",
        old_commit,
        "--subdirectory",
        "packages/global-git-skill",
        "--scope",
        "global",
    )
    assert installed["scope"] == "global"
    assert run("update", "global-git-skill", "--ref", new_commit, "--scope", "global")[
        "pending_version"
    ]["resolved_commit"] == new_commit
    staged = run("list", "--scope", "global")["packages"][0]
    assert staged["current_version"]["resolved_commit"] == old_commit
    assert staged["pending_restart"] is True

    from agent_harness.skills.package_manager import SkillPackageManager

    global_manager = SkillPackageManager(
        workspace,
        scope="global",
        global_skills_dir=settings.skill_global_dir,
    )
    assert global_manager.apply_pending_versions() == 1
    rolled_back = run("rollback", "global-git-skill", "--scope", "global")
    assert rolled_back["pending_version"]["resolved_commit"] == old_commit
    assert global_manager.apply_pending_versions() == 1
    removed = run("remove", "global-git-skill", "--scope", "global")
    assert removed["removed"] is True
    assert run("list", "--scope", "global")["packages"] == []
    assert not (workspace / "plugin-installs.json").exists()


def test_cli_plugins_inspect_reports_a_deeply_nested_description_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """修复轮 R2：真实 CLI 对 40 KB 深嵌套描述必须给明确失败，不能掀 RecursionError。

    预检契约"绝不崩"在**用户入口**上同样成立——`inspect` 只是把报告打出来，深嵌套
    描述落成 `MCP_DESCRIPTION_TOO_DEEP` 并照常 exit 1（unsupported），不是 traceback。
    """
    source = tmp_path / "deep-mcp"
    _write(source / "SKILL.md", "---\nname: deep-mcp\ndescription: Deep MCP.\n---\nBody.\n")
    _write(source / "mcp.json", '{"mcpServers": ' + "[" * 20_000 + "0" + "]" * 20_000 + "}")
    workspace = tmp_path / "workspace"
    global_skills = tmp_path / "global-skills"
    _write(workspace / "plugin-installs.json", '{"project": []}\n')
    _write(global_skills / "plugin-installs.json", '{"global": []}\n')
    before = _snapshot(source, workspace, global_skills)
    _configure_cli_inspect(monkeypatch, source, workspace, global_skills)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()  # RecursionError 会在这里穿出去，测试直接 error

    assert excinfo.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "unsupported"
    assert "MCP_DESCRIPTION_TOO_DEEP" in {
        error["code"] for error in report["mcp"]["errors"]
    }
    assert _snapshot(source, workspace, global_skills) == before
