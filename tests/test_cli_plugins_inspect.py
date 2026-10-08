from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_harness import cli
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
    ("reference", "expected_code"),
    [
        ("missing.md", "MISSING_RESOURCE"),
        ("../../outside.txt", "REFERENCE_OUTSIDE_PACKAGE"),
        ("C:/outside.md", "REFERENCE_OUTSIDE_PACKAGE"),
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
    _write(source / "references" / "REFERENCE.md", "See [the image](assets/logo.bin).\n")
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
        "[project]\ndependencies = ['requests>=2']\n"
        "[project.optional-dependencies]\ntest = ['pytest>=8']\n",
    )

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "needs-adaptation"
    assert {item["name"] for item in report["dependencies"]} == {
        "setuptools",
        "requests",
        "pytest",
    }


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


def test_inspection_rejects_requirements_include_outside_package(tmp_path: Path) -> None:
    source = tmp_path / "escaping-requirements-include"
    _write(
        source / "SKILL.md",
        "---\nname: escaping-requirements-include\ndescription: Example.\n---\n",
    )
    _write(source / "requirements.txt", "-r ../../outside.txt\n")

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
    assert "DEPENDENCY_INCLUDE_OUTSIDE_PACKAGE" in {
        error["code"] for error in report["errors"]
    }


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
    real_open = os.open
    replaced = False

    def replace_before_open(path: str | bytes | os.PathLike[str], flags: int) -> int:
        nonlocal replaced
        if Path(path) == skill_file and not replaced:
            replacement.replace(skill_file)
            replaced = True
        return real_open(path, flags)

    monkeypatch.setattr(inspection.os, "open", replace_before_open)

    report = inspect_skill_package(source, scope="project")

    assert report["status"] == "unsupported"
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
