"""Read-only compatibility inspection for local Agent Skills packages."""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

from agent_harness.skills.discovery import SKILL_FILE_MAX_BYTES, parse_skill_markdown

_STRICT_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)")
_MARKDOWN_REFERENCE = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*(<[^>]+>|[^\s]+)")
_HTML_REFERENCE = re.compile(r"(?:href|src)\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))", re.IGNORECASE)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MAX_PACKAGE_FILES = 10_000
_MAX_METADATA_SCAN_BYTES = SKILL_FILE_MAX_BYTES


def inspect_skill_package(
    source: Path | str,
    *,
    scope: str = "project",
    existing_skills: Path | str | None = None,
) -> dict[str, Any]:
    """Inspect a local Skill directory without writing files or running package code."""
    source_path = Path(source).expanduser()
    report: dict[str, Any] = {
        "status": "unsupported",
        "source": str(source_path),
        "scope": scope,
        "name": None,
        "license": "\u672a\u58f0\u660e",
        "metadata": {},
        "resources": {"scripts": [], "references": [], "assets": [], "other": []},
        "dependencies": [],
        "requirements": [],
        "errors": [],
    }

    errors: list[dict[str, str]] = report["errors"]
    if scope != "project":
        _add_error(errors, "UNSUPPORTED_SCOPE", str(source_path), "Only project scope is supported.")
        return report
    try:
        root = source_path.resolve(strict=True)
    except (OSError, RuntimeError):
        _add_error(errors, "DIRECTORY_NOT_FOUND", str(source_path), "Package directory does not exist or cannot be resolved.")
        return report
    if not root.is_dir():
        _add_error(errors, "NOT_A_DIRECTORY", str(source_path), "Package source must be a directory.")
        return report
    report["source"] = str(root)

    skill_file = root / "SKILL.md"
    if _is_link(skill_file):
        try:
            resolved_skill_file = skill_file.resolve(strict=True)
        except (OSError, RuntimeError):
            _add_error(errors, "BAD_SYMLINK", "SKILL.md", "SKILL.md is a broken or invalid symbolic link.")
            return report
        if not _within(resolved_skill_file, root):
            _add_error(errors, "SYMLINK_OUTSIDE_PACKAGE", "SKILL.md", "SKILL.md resolves outside the package directory.")
            return report
    if not skill_file.exists():
        _add_error(errors, "SKILL_FILE_MISSING", "SKILL.md", "Package must contain a SKILL.md file.")
        return report
    if not skill_file.is_file():
        _add_error(errors, "SKILL_FILE_NOT_A_FILE", "SKILL.md", "SKILL.md must be a regular file.")
        return report
    try:
        if skill_file.stat().st_size > SKILL_FILE_MAX_BYTES:
            _add_error(
                errors,
                "SKILL_FILE_TOO_LARGE",
                "SKILL.md",
                f"SKILL.md exceeds {SKILL_FILE_MAX_BYTES} bytes.",
            )
            return report
    except OSError as error:
        _add_error(errors, "SKILL_FILE_UNREADABLE", "SKILL.md", type(error).__name__)
        return report

    try:
        entry, parse_errors = parse_skill_markdown(skill_file)
    except RecursionError:
        entry, parse_errors = None, ["frontmatter nesting is too deep"]
    if entry is None:
        _add_error(
            errors,
            "SKILL_MARKDOWN_INVALID",
            "SKILL.md",
            "; ".join(parse_errors),
        )
        return report

    report["name"] = entry.name
    metadata = {"name": entry.name, "description": entry.description, **entry.meta}
    if entry.when_to_use:
        metadata["when_to_use"] = entry.when_to_use
    report["metadata"] = _json_value(metadata)
    declared_license = entry.meta.get("license")
    if isinstance(declared_license, str) and declared_license.strip():
        report["license"] = declared_license.strip()
    elif declared_license is not None:
        _add_error(errors, "INVALID_LICENSE", "SKILL.md", "license must be a non-empty string when provided.")

    if not _STRICT_NAME.fullmatch(entry.name) or len(entry.name) > 64:
        _add_error(errors, "INVALID_SKILL_NAME", "SKILL.md", "name must use lowercase letters, digits, and single hyphens (max 64 characters).")
    if root.name != entry.name:
        _add_error(
            errors,
            "DIRECTORY_NAME_MISMATCH",
            ".",
            f"Directory name {root.name!r} must match Skill name {entry.name!r}.",
        )
    description = entry.description
    if len(description) > 1024:
        _add_error(errors, "DESCRIPTION_TOO_LONG", "SKILL.md", "description exceeds 1024 characters.")
    compatibility = entry.meta.get("compatibility")
    if compatibility is not None and not isinstance(compatibility, str):
        _add_error(errors, "INVALID_COMPATIBILITY", "SKILL.md", "compatibility must be a string.")
    elif isinstance(compatibility, str) and len(compatibility) > 500:
        _add_error(errors, "COMPATIBILITY_TOO_LONG", "SKILL.md", "compatibility exceeds 500 characters.")
    custom_metadata = entry.meta.get("metadata")
    if custom_metadata is not None and (
        not isinstance(custom_metadata, dict)
        or any(not isinstance(key, str) or not isinstance(value, str) for key, value in custom_metadata.items())
    ):
        _add_error(errors, "INVALID_METADATA", "SKILL.md", "metadata must contain string keys and string values.")

    installed_target = _safe_skill_target(existing_skills, entry.name)
    if installed_target is not None and (installed_target.exists() or installed_target.is_symlink()):
        _add_error(errors, "NAME_CONFLICT", "SKILL.md", f"Project Skill name {entry.name!r} already exists.")

    files, scan_errors, symlink_paths = _inventory(root)
    errors.extend(scan_errors)
    for item in files:
        relative = item["path"]
        group = _resource_group(relative)
        if relative != "SKILL.md":
            report["resources"][group].append(item)
    for link_path in symlink_paths:
        report["requirements"].append(
            {
                "kind": "resource-link",
                "name": link_path,
                "support": "manual_review",
                "reason": "Symbolic links are not followed during inspection.",
            }
        )
    for script in report["resources"]["scripts"]:
        if script.get("kind") != "directory":
            report["requirements"].append(
                {
                    "kind": "script-runtime",
                    "name": script["path"],
                    "support": "manual_review",
                    "reason": "Scripts are inventoried but never executed; verify their runtime requirements manually.",
                }
            )

    markdown_paths = [root / relative for relative in ("SKILL.md", *(item["path"] for item in files if item["path"].lower().endswith(".md")))]
    for markdown_path in dict.fromkeys(markdown_paths):
        errors.extend(_check_markdown_references(root, markdown_path))

    dependencies, dependency_errors = _inspect_dependencies(root, files)
    report["dependencies"] = dependencies
    errors.extend(dependency_errors)
    report["requirements"].extend(dependencies)

    if isinstance(compatibility, str) and compatibility.strip():
        report["requirements"].append(
            {
                "kind": "runtime",
                "name": compatibility.strip(),
                "support": "manual_review",
                "source": "SKILL.md",
                "reason": "The required runtime environment cannot be verified without executing package code.",
            }
        )
    allowed_tools = entry.meta.get("allowed-tools")
    if isinstance(allowed_tools, str):
        tool_names = [item for item in re.split(r"[\s,]+", allowed_tools.strip()) if item]
    elif isinstance(allowed_tools, list) and all(isinstance(item, str) for item in allowed_tools):
        tool_names = [item.strip() for item in allowed_tools if item.strip()]
    elif allowed_tools is None:
        tool_names = []
    else:
        tool_names = []
        _add_error(errors, "INVALID_ALLOWED_TOOLS", "SKILL.md", "allowed-tools must be a string or a list of strings.")
    for tool_name in tool_names:
        report["requirements"].append(
            {
                "kind": "tool-permission",
                "name": tool_name,
                "support": "manual_review",
                "source": "SKILL.md",
                "reason": "Declaring an allowed tool does not grant runtime permission.",
            }
        )

    if errors:
        report["status"] = "unsupported"
    elif any(item["support"] != "supported" for item in report["requirements"]):
        report["status"] = "needs-adaptation"
    else:
        report["status"] = "complete"
    return report


def _add_error(errors: list[dict[str, str]], code: str, path: str, message: str) -> None:
    errors.append({"code": code, "path": path, "message": message})


def _add_reference_error(
    errors: list[dict[str, str]],
    code: str,
    path: str,
    source: str,
    message: str,
) -> None:
    errors.append(
        {"code": code, "path": path, "referenced_from": source, "message": message}
    )


def _json_value(
    value: Any, *, _seen: set[int] | None = None, _depth: int = 0
) -> Any:
    if _depth > 64:
        return "<nested value omitted>"
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        seen = _seen if _seen is not None else set()
        identity = id(value)
        if identity in seen:
            return "<recursive value>"
        seen.add(identity)
        result = [_json_value(item, _seen=seen, _depth=_depth + 1) for item in value]
        seen.remove(identity)
        return result
    if isinstance(value, dict):
        seen = _seen if _seen is not None else set()
        identity = id(value)
        if identity in seen:
            return "<recursive value>"
        seen.add(identity)
        result = {
            str(key): _json_value(item, _seen=seen, _depth=_depth + 1)
            for key, item in value.items()
        }
        seen.remove(identity)
        return result
    return repr(value)


def _safe_skill_target(existing_skills: Path | str | None, name: str) -> Path | None:
    if existing_skills is None or not _STRICT_NAME.fullmatch(name) or len(name) > 64:
        return None
    return Path(existing_skills).expanduser() / name


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _is_link(path: Path) -> bool:
    return path.is_symlink() or _is_junction(path)


def _is_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _inventory(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    files: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    symlinks: list[str] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda item: item.name)
        except OSError as error:
            _add_error(errors, "DIRECTORY_UNREADABLE", _relative(directory, root), type(error).__name__)
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = _relative(path, root)
            try:
                if entry.is_symlink() or _is_junction(path):
                    symlinks.append(relative)
                    try:
                        resolved = path.resolve(strict=True)
                    except (OSError, RuntimeError):
                        _add_error(errors, "BAD_SYMLINK", relative, "Symbolic link is broken or cannot be resolved.")
                        continue
                    if not _within(resolved, root):
                        _add_error(errors, "SYMLINK_OUTSIDE_PACKAGE", relative, "Symbolic link resolves outside the package directory.")
                        continue
                    if resolved.is_file():
                        files.append({"path": relative, "size_bytes": resolved.stat().st_size, "symlink": True})
                    elif resolved.is_dir():
                        files.append({"path": relative, "size_bytes": 0, "symlink": True, "kind": "directory"})
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    continue
                if entry.is_file(follow_symlinks=False):
                    files.append({"path": relative, "size_bytes": entry.stat(follow_symlinks=False).st_size})
                    if len(files) > _MAX_PACKAGE_FILES:
                        _add_error(errors, "PACKAGE_TOO_MANY_FILES", ".", f"Package exceeds {_MAX_PACKAGE_FILES} files.")
                        return files[:_MAX_PACKAGE_FILES], errors, symlinks
            except OSError as error:
                _add_error(errors, "RESOURCE_UNREADABLE", relative, type(error).__name__)
    files.sort(key=lambda item: item["path"])
    return files, errors, symlinks


def _relative(path: Path, root: Path) -> str:
    try:
        result = path.relative_to(root).as_posix()
        return result or "."
    except ValueError:
        return str(path)


def _resource_group(relative: str) -> str:
    first = PurePosixPath(relative).parts[0] if PurePosixPath(relative).parts else ""
    if first in {"scripts", "references", "assets"}:
        return first
    return "other"


def _has_symlink_component(candidate: Path, root: Path) -> bool:
    current = candidate
    while current != root and _within(current, root):
        if _is_link(current):
            return True
        current = current.parent
    return False


def _check_markdown_references(root: Path, markdown_path: Path) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    relative_markdown = _relative(markdown_path, root)
    try:
        size = markdown_path.stat().st_size
        if size > _MAX_METADATA_SCAN_BYTES:
            _add_error(errors, "REFERENCE_FILE_TOO_LARGE", relative_markdown, f"Markdown file exceeds {_MAX_METADATA_SCAN_BYTES} bytes and could not be checked.")
            return errors
        content = markdown_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        _add_error(errors, "MARKDOWN_UNREADABLE", relative_markdown, type(error).__name__)
        return errors

    raw_targets = [
        match.group(1)
        for pattern in (_MARKDOWN_LINK, _MARKDOWN_REFERENCE)
        for match in pattern.finditer(content)
    ]
    raw_targets.extend(next(group for group in match.groups() if group is not None) for match in _HTML_REFERENCE.finditer(content))
    for code_match in _INLINE_CODE.finditer(content):
        raw_targets.extend(
            token.strip(".,;:!?()[]{}")
            for token in re.findall(r"[^\s`'\"<>]+", code_match.group(1))
            if "/" in token or "\\" in token
        )
    for raw_target in dict.fromkeys(raw_targets):
        if raw_target.startswith("<") and raw_target.endswith(">"):
            raw_target = raw_target[1:-1]
        try:
            parsed = urlsplit(raw_target)
        except ValueError:
            _add_reference_error(
                errors, "BAD_REFERENCE", raw_target, relative_markdown,
                f"Reference {raw_target!r} is malformed.",
            )
            continue
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target_text = unquote(parsed.path).replace("\\", "/")
        windows_path = PureWindowsPath(target_text)
        reference_path = (
            target_text
            if PurePosixPath(target_text).is_absolute() or windows_path.drive
            else (PurePosixPath(relative_markdown).parent / target_text).as_posix()
        )
        if PurePosixPath(target_text).is_absolute() or windows_path.is_absolute() or windows_path.drive:
            _add_reference_error(
                errors, "REFERENCE_OUTSIDE_PACKAGE", reference_path, relative_markdown,
                "Absolute references are not allowed.",
            )
            continue
        candidate = markdown_path.parent.joinpath(*PurePosixPath(target_text).parts)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            _add_reference_error(
                errors, "BAD_SYMLINK", reference_path, relative_markdown,
                f"Reference {raw_target!r} cannot be resolved safely.",
            )
            continue
        if not _within(resolved, root):
            _add_reference_error(
                errors, "REFERENCE_OUTSIDE_PACKAGE", reference_path, relative_markdown,
                f"Reference {raw_target!r} resolves outside the package directory.",
            )
            continue
        try:
            strict_resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            code = "BAD_SYMLINK" if _has_symlink_component(candidate, root) else "MISSING_RESOURCE"
            _add_reference_error(
                errors, code, reference_path, relative_markdown,
                f"Reference {raw_target!r} does not resolve to an existing resource.",
            )
            continue
        if not _within(strict_resolved, root):
            _add_reference_error(
                errors, "REFERENCE_OUTSIDE_PACKAGE", reference_path, relative_markdown,
                f"Reference {raw_target!r} resolves outside the package directory.",
            )
        elif not strict_resolved.is_file():
            _add_reference_error(
                errors, "RESOURCE_NOT_FILE", reference_path, relative_markdown,
                f"Reference {raw_target!r} does not point to a file.",
            )
    return errors


def _inspect_dependencies(
    root: Path, files: list[dict[str, Any]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    dependencies: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    paths = {item["path"]: item for item in files}
    requirements_path = paths.get("requirements.txt")
    if requirements_path is not None:
        parsed_lines = _read_small_file(root / "requirements.txt", "requirements.txt", errors)
        if parsed_lines is not None:
            for line in parsed_lines.splitlines():
                requirement = line.split("#", 1)[0].strip()
                if not requirement or requirement.startswith(("-", "--")):
                    continue
                match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
                if match:
                    dependencies.append(_dependency(match.group(1), "requirements.txt"))
    package_json = paths.get("package.json")
    if package_json is not None:
        content = _read_small_file(root / "package.json", "package.json", errors)
        if content is not None:
            try:
                parsed_json = json.loads(content)
                for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                    values = parsed_json.get(section, {}) if isinstance(parsed_json, dict) else {}
                    if isinstance(values, dict):
                        dependencies.extend(_dependency(str(name), "package.json") for name in values)
            except (json.JSONDecodeError, TypeError):
                _add_error(errors, "DEPENDENCY_FILE_INVALID", "package.json", "Could not statically parse dependency declarations.")
    pyproject = paths.get("pyproject.toml")
    if pyproject is not None:
        content = _read_small_file(root / "pyproject.toml", "pyproject.toml", errors)
        if content is not None:
            try:
                parsed_toml = tomllib.loads(content)
                declared = parsed_toml.get("project", {}).get("dependencies", [])
                if isinstance(declared, list):
                    for item in declared:
                        if isinstance(item, str):
                            match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", item)
                            if match:
                                dependencies.append(_dependency(match.group(1), "pyproject.toml"))
            except (tomllib.TOMLDecodeError, AttributeError, TypeError):
                _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "Could not statically parse dependency declarations.")
    dependencies.sort(key=lambda item: (item["source"], item["name"].lower()))
    return dependencies, errors


def _read_small_file(path: Path, relative: str, errors: list[dict[str, str]]) -> str | None:
    try:
        if path.stat().st_size > _MAX_METADATA_SCAN_BYTES:
            _add_error(errors, "DEPENDENCY_FILE_TOO_LARGE", relative, f"File exceeds {_MAX_METADATA_SCAN_BYTES} bytes.")
            return None
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        _add_error(errors, "DEPENDENCY_FILE_UNREADABLE", relative, type(error).__name__)
        return None


def _dependency(name: str, source: str) -> dict[str, str]:
    return {
        "kind": "package",
        "name": name,
        "source": source,
        "support": "manual_review",
        "reason": "Dependency availability and compatibility are not verified by read-only inspection.",
    }
