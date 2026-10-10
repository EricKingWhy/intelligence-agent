"""Read-only compatibility inspection for local Agent Skills packages."""

from __future__ import annotations

import errno
import json
import math
import os
import re
import shlex
import stat
import tomllib
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

from agent_harness.skills.discovery import (
    SKILL_FILE_MAX_BYTES,
    parse_skill_markdown_text,
)

_STRICT_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)")
_MARKDOWN_REFERENCE = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*(<[^>]+>|[^\s]+)")
_HTML_REFERENCE = re.compile(r"(?:href|src)\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))", re.IGNORECASE)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_COMMAND_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*\Z")
_COMMAND_INVOCATION = re.compile(r"\s*(?:[$>]\s*)?[A-Za-z][A-Za-z0-9_.+-]*\s+.+\Z")
_SHELL_FENCE_START = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*(?P<language>bash|sh|shell|zsh|fish|powershell|pwsh|ps1|cmd|bat|batch)\b[^\r\n]*\r?\n",
    re.IGNORECASE | re.MULTILINE,
)
_SHELL_BUILTINS = frozenset(
    {
        ".", "break", "cd", "continue", "echo", "eval", "exec", "exit", "export",
        "false", "getopts", "hash", "pwd", "read", "return", "set", "shift",
        "source", "test", "times", "trap", "true", "type", "ulimit", "umask",
        "unset", "wait",
    }
)
_POWERSHELL_BUILTINS = frozenset(
    {
        "add-content", "clear-host", "convertfrom-json", "convertto-json", "copy-item",
        "foreach-object", "get-childitem", "get-command", "get-content", "get-date",
        "get-location", "get-member", "get-process", "join-path", "measure-object",
        "move-item", "new-item", "out-file", "pop-location", "push-location",
        "read-host", "remove-item", "rename-item", "resolve-path", "select-object",
        "set-content", "set-location", "sort-object", "split-path", "start-sleep",
        "test-path", "where-object", "write-debug", "write-error", "write-host",
        "write-output", "write-verbose", "write-warning",
    }
)
_CMD_BUILTINS = frozenset(
    {
        "assoc", "attrib", "break", "call", "cd", "chdir", "choice", "cls", "color",
        "copy", "date", "del", "dir", "echo", "endlocal", "erase", "exit", "for",
        "ftype", "goto", "if", "md", "mkdir", "mklink", "move", "path", "pause",
        "popd", "prompt", "pushd", "rd", "rem", "ren", "rename", "rmdir", "set",
        "setlocal", "shift", "start", "time", "title", "type", "ver", "verify", "vol",
    }
)
_COMMAND_ACTION_CONTEXT = re.compile(
    r"\b(?:run|execute|invoke|call|launch|install|require(?:s)?|need(?:s)?|use|using|command|executable)"
    r"(?:\s+the)?\s*[:=]?\s*\Z",
    re.IGNORECASE,
)
_COMMAND_EXECUTION_CONTEXT = re.compile(
    r"\b(?:run|execute|invoke|launch|install)(?:\s+the)?\s*[:=]?\s*\Z",
    re.IGNORECASE,
)
_MAX_PACKAGE_ENTRIES = 10_000
_MAX_REQUIREMENT_INCLUDE_DEPTH = 64
_MAX_METADATA_SCAN_BYTES = SKILL_FILE_MAX_BYTES


class _PackageReadBoundaryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
    if scope not in {"project", "global"}:
        _add_error(errors, "UNSUPPORTED_SCOPE", str(source_path), "Scope must be project or global.")
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
    try:
        resolved_skill_file = skill_file.resolve(strict=True)
    except FileNotFoundError:
        _add_error(errors, "SKILL_FILE_MISSING", "SKILL.md", "Package must contain a SKILL.md file.")
        return report
    except (OSError, RuntimeError):
        _add_error(errors, "SKILL_FILE_UNREADABLE", "SKILL.md", "SKILL.md cannot be resolved safely.")
        return report
    if not _within(resolved_skill_file, root):
        _add_error(errors, "SYMLINK_OUTSIDE_PACKAGE", "SKILL.md", "SKILL.md resolves outside the package directory.")
        return report
    if not resolved_skill_file.is_file():
        _add_error(errors, "SKILL_FILE_NOT_A_FILE", "SKILL.md", "SKILL.md must be a regular file.")
        return report
    skill_text = _read_skill_file(root, resolved_skill_file, errors)
    if skill_text is None:
        return report

    try:
        entry, parse_errors = parse_skill_markdown_text(skill_text, skill_file)
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
        _add_error(errors, "NAME_CONFLICT", "SKILL.md", f"{scope.title()} Skill name {entry.name!r} already exists.")

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

    markdown_paths = [root / "SKILL.md"]
    markdown_paths.extend(
        root / item["path"]
        for item in files
        if item["path"] != "SKILL.md"
        and item["path"].lower().endswith(".md")
        and not item.get("symlink")
    )
    command_requirements: list[dict[str, str]] = []
    for markdown_path in dict.fromkeys(markdown_paths):
        content = skill_text if markdown_path == skill_file else None
        errors.extend(
            _check_markdown_references(
                root,
                markdown_path,
                content,
                command_requirements=command_requirements,
            )
        )
    report["requirements"].extend(command_requirements)

    dependencies, dependency_requirements, dependency_errors = _inspect_dependencies(root, files)
    report["dependencies"] = dependencies
    errors.extend(dependency_errors)
    report["requirements"].extend(dependency_requirements)
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


def _read_skill_file(
    root: Path, path: Path, errors: list[dict[str, str]]
) -> str | None:
    return _read_bounded_package_text(
        root,
        path,
        "SKILL.md",
        SKILL_FILE_MAX_BYTES,
        errors,
        too_large_code="SKILL_FILE_TOO_LARGE",
        unreadable_code="SKILL_FILE_UNREADABLE",
        changed_code="SKILL_FILE_CHANGED",
        not_file_code="SKILL_FILE_NOT_A_FILE",
        outside_code="SYMLINK_OUTSIDE_PACKAGE",
    )


def _read_bounded_package_text(
    root: Path,
    path: Path,
    relative: str,
    max_bytes: int,
    errors: list[dict[str, str]],
    *,
    too_large_code: str,
    unreadable_code: str,
    changed_code: str,
    not_file_code: str,
    outside_code: str,
) -> str | None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        _add_error(errors, unreadable_code, relative, type(error).__name__)
        return None
    if not _within(resolved, root):
        _add_error(errors, outside_code, relative, "File resolves outside the package directory.")
        return None
    if resolved != path:
        _add_error(errors, changed_code, relative, "File path became a symbolic link during inspection.")
        return None
    try:
        before = resolved.lstat()
        if not stat.S_ISREG(before.st_mode):
            _add_error(errors, not_file_code, relative, "Expected a regular file.")
            return None
        if before.st_size > max_bytes:
            _add_error(
                errors,
                too_large_code,
                relative,
                f"File exceeds {max_bytes} bytes.",
            )
            return None
        descriptor = _open_package_file(root, resolved, changed_code)
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
            ):
                _add_error(errors, changed_code, relative, "File changed during inspection; retry with a stable package.")
                return None
            if opened.st_size > max_bytes:
                _add_error(
                    errors,
                    too_large_code,
                    relative,
                    f"File exceeds {max_bytes} bytes.",
                )
                return None
            content = source.read(max_bytes + 1)
        if len(content) > max_bytes:
            _add_error(
                errors,
                too_large_code,
                relative,
                f"File exceeds {max_bytes} bytes.",
            )
            return None
        return content.decode("utf-8-sig")
    except _PackageReadBoundaryError as error:
        _add_error(errors, error.code, relative, str(error))
    except UnicodeDecodeError as error:
        _add_error(errors, unreadable_code, relative, type(error).__name__)
    except OSError as error:
        _add_error(errors, unreadable_code, relative, type(error).__name__)
    return None


def _open_package_file(root: Path, path: Path, changed_code: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name == "nt":
        descriptor = os.open(path, flags)
        try:
            opened_path = _windows_open_handle_path(descriptor)
            if not _windows_path_within(opened_path, root) or not _same_windows_path(
                opened_path, path
            ):
                raise _PackageReadBoundaryError(
                    changed_code,
                    "Opened file path changed during inspection; retry with a stable package.",
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    return _open_posix_package_file(root, path, flags, changed_code)


def _open_posix_package_file(
    root: Path, path: Path, flags: int, changed_code: str
) -> int:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if (
        os.open not in os.supports_dir_fd
        or not getattr(os, "O_DIRECTORY", 0)
        or not getattr(os, "O_NOFOLLOW", 0)
    ):
        raise OSError("Safe package-relative file reads are unavailable on this platform.")

    try:
        relative_parts = path.relative_to(root).parts
    except ValueError as error:
        raise _PackageReadBoundaryError(
            changed_code, "File path moved outside the package directory."
        ) from error
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        raise _PackageReadBoundaryError(changed_code, "Invalid package-relative file path.")

    descriptor = os.open(root.anchor, directory_flags)
    try:
        for part in root.parts[1:]:
            next_descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        for part in relative_parts[:-1]:
            next_descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return os.open(
            relative_parts[-1],
            flags | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _PackageReadBoundaryError(
                changed_code,
                "A package path component changed during inspection; retry with a stable package.",
            ) from error
        raise
    finally:
        os.close(descriptor)


def _windows_open_handle_path(descriptor: int) -> Path:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD

    handle = msvcrt.get_osfhandle(descriptor)
    buffer_size = 32768
    while True:
        buffer = ctypes.create_unicode_buffer(buffer_size)
        length = get_final_path(handle, buffer, buffer_size, 0)
        if length == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if length < buffer_size:
            final_path = buffer.value
            break
        if length > 1_048_576:
            raise OSError("Opened file path is too long to verify safely.")
        buffer_size = length + 1

    if final_path.startswith("\\\\?\\UNC\\"):
        final_path = "\\\\" + final_path[8:]
    elif final_path.startswith("\\\\?\\"):
        final_path = final_path[4:]
    return Path(final_path)


def _same_windows_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
        os.path.normpath(str(right))
    )


def _windows_path_within(path: Path, root: Path) -> bool:
    candidate = os.path.normcase(os.path.normpath(str(path)))
    package_root = os.path.normcase(os.path.normpath(str(root)))
    try:
        return os.path.commonpath((candidate, package_root)) == package_root
    except ValueError:
        return False


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
    if is_junction is not None and is_junction():
        return True
    if path.is_symlink():
        return False
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_attribute)


def _inventory(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    files: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    symlinks: list[str] = []
    pending = [root]
    entry_count = 0
    too_many_entries = False
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = []
                for entry in scan:
                    entry_count += 1
                    if entry_count > _MAX_PACKAGE_ENTRIES:
                        _add_error(
                            errors,
                            "PACKAGE_TOO_MANY_ENTRIES",
                            ".",
                            f"Package exceeds {_MAX_PACKAGE_ENTRIES} filesystem entries.",
                        )
                        too_many_entries = True
                        break
                    entries.append(entry)
        except OSError as error:
            _add_error(errors, "DIRECTORY_UNREADABLE", _relative(directory, root), type(error).__name__)
            continue
        entries.sort(key=lambda item: item.name)
        for entry in entries:
            path = Path(entry.path)
            relative = _relative(path, root)
            try:
                if _is_junction(path):
                    symlinks.append(relative)
                    _add_error(
                        errors,
                        "UNSUPPORTED_JUNCTION",
                        relative,
                        "Windows junctions cannot be safely snapshotted; replace them with regular files or directories.",
                    )
                    continue
                if entry.is_symlink():
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
            except OSError as error:
                _add_error(errors, "RESOURCE_UNREADABLE", relative, type(error).__name__)
        if too_many_entries:
            break
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


def _check_markdown_references(
    root: Path,
    markdown_path: Path,
    content: str | None = None,
    *,
    command_requirements: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    relative_markdown = _relative(markdown_path, root)
    if content is None:
        content = _read_bounded_package_text(
            root,
            markdown_path,
            relative_markdown,
            _MAX_METADATA_SCAN_BYTES,
            errors,
            too_large_code="REFERENCE_FILE_TOO_LARGE",
            unreadable_code="REFERENCE_FILE_UNREADABLE",
            changed_code="REFERENCE_FILE_CHANGED",
            not_file_code="RESOURCE_NOT_FILE",
            outside_code="REFERENCE_OUTSIDE_PACKAGE",
        )
        if content is None:
            return errors

    if command_requirements is not None:
        command_requirements.extend(
            _inspect_inline_commands(content, relative_markdown)
        )

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
        decoded_target = unquote(raw_target)
        has_windows_drive = bool(re.match(r"^[A-Za-z]:", decoded_target))
        normalized_target = decoded_target.replace("\\", "/")
        is_posix_absolute = normalized_target.startswith("/") and not normalized_target.startswith("//")
        is_windows_unc = decoded_target.startswith("\\\\")
        if is_posix_absolute or is_windows_unc or has_windows_drive:
            _add_reference_error(
                errors,
                "REFERENCE_OUTSIDE_PACKAGE",
                normalized_target,
                relative_markdown,
                "Absolute references are not allowed.",
            )
            continue
        try:
            parsed = urlsplit(raw_target)
        except ValueError:
            _add_reference_error(
                errors, "BAD_REFERENCE", raw_target, relative_markdown,
                f"Reference {raw_target!r} is malformed.",
            )
            continue
        is_file_uri = parsed.scheme.lower() == "file"
        if (parsed.scheme and not is_file_uri) or (parsed.netloc and not is_file_uri) or not parsed.path:
            continue
        target_text = unquote(parsed.path).replace("\\", "/")
        windows_path = PureWindowsPath(target_text)
        reference_path = target_text
        if (
            (is_file_uri and parsed.netloc)
            or PurePosixPath(target_text).is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
        ):
            _add_reference_error(
                errors, "REFERENCE_OUTSIDE_PACKAGE", reference_path, relative_markdown,
                "Absolute references are not allowed.",
            )
            continue
        candidate = root.joinpath(*PurePosixPath(target_text).parts)
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


def _inspect_inline_commands(content: str, source: str) -> list[dict[str, str]]:
    requirements: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(
        command: str,
        reason: str = "The host command is declared in Skill guidance but is not executed or availability-checked during inspection.",
    ) -> None:
        if not _COMMAND_IDENTIFIER.fullmatch(command) or command.casefold() in seen:
            return
        seen.add(command.casefold())
        requirements.append(
            {
                "kind": "command-runtime",
                "name": command,
                "support": "manual_review",
                "source": source,
                "reason": reason,
            }
        )

    for match in _INLINE_CODE.finditer(content):
        snippet = match.group(1).strip()
        context = content[max(0, match.start() - 80) : match.start()]
        has_action_context = bool(_COMMAND_ACTION_CONTEXT.search(context))
        command_text = snippet.lstrip("$> ")
        if _COMMAND_IDENTIFIER.fullmatch(command_text):
            if not has_action_context and not snippet.startswith(("$ ", "> ")):
                continue
        elif not (
            _looks_like_command_invocation(snippet)
            or _COMMAND_EXECUTION_CONTEXT.search(context)
        ):
            continue
        command = command_text.split(maxsplit=1)[0]
        add(command)

    for language, body in _shell_fence_bodies(content):
        builtins = _SHELL_BUILTINS
        if language.casefold() in {"powershell", "pwsh", "ps1"}:
            builtins |= _POWERSHELL_BUILTINS
        elif language.casefold() in {"cmd", "bat", "batch"}:
            builtins |= _CMD_BUILTINS
        for line in body.splitlines():
            command_groups, parse_failed = _split_shell_commands(line)
            if parse_failed:
                add(
                    "shell",
                    "A fenced shell line could not be parsed safely; review its host command requirements manually.",
                )
            for command_words in command_groups:
                words = command_words
                while words and words[0].casefold() in {"if", "then", "elif", "while", "until", "else"}:
                    words = words[1:]
                while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.+", words[0]):
                    words = words[1:]
                if not words:
                    continue
                command = words[0].replace("\\", "/").rsplit("/", 1)[-1]
                if command.casefold() not in builtins:
                    add(command)
    return requirements


def _shell_fence_bodies(content: str) -> list[tuple[str, str]]:
    """Return shell fence bodies, accepting Markdown's longer closing fences."""
    bodies: list[tuple[str, str]] = []
    search_from = 0
    while fence := _SHELL_FENCE_START.search(content, search_from):
        marker = fence.group("fence")
        marker_char = re.escape(marker[0])
        body_start = fence.end()
        closing_pattern = re.compile(
            rf"^[ \t]*{marker_char}{{{len(marker)},}}[ \t]*\r?$",
            re.MULTILINE,
        )
        closing = closing_pattern.search(content, body_start)
        if closing is None:
            break
        bodies.append((fence.group("language"), content[body_start : closing.start()]))
        search_from = closing.end()
    return bodies


def _looks_like_command_invocation(snippet: str) -> bool:
    """Require shell-like argument syntax for context-free inline code spans."""
    if not _COMMAND_INVOCATION.fullmatch(snippet):
        return False
    arguments = snippet.strip().lstrip("$> ").split()[1:]
    return any(
        argument.startswith(("-", "./", "../"))
        or "/" in argument
        or "\\" in argument
        or re.search(r"\.[A-Za-z0-9]{1,8}$", argument) is not None
        or "=" in argument
        for argument in arguments
    )


def _split_shell_commands(line: str) -> tuple[list[list[str]], bool]:
    """Use the standard shell tokenizer so quoted separators do not split commands."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return [], True
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and all(character in ";&|" for character in token):
            if commands[-1]:
                commands.append([])
        else:
            commands[-1].append(token)
    return [command for command in commands if command], False


def _inspect_dependencies(
    root: Path, files: list[dict[str, Any]]
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    dependencies: list[dict[str, str]] = []
    requirements: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    paths = {item["path"]: item for item in files}
    requirements_path = paths.get("requirements.txt")
    if requirements_path is not None:
        if requirements_path.get("symlink"):
            requirements.append(
                _manual_requirement(
                    "dependency-file",
                    "requirements.txt",
                    "requirements.txt",
                    "Symbolic links are not followed while reading dependency declarations.",
                )
            )
        else:
            _read_requirements_file(
                root,
                "requirements.txt",
                paths,
                dependencies,
                requirements,
                errors,
                active=set(),
                visited=set(),
            )
    package_json = paths.get("package.json")
    if package_json is not None:
        content = _read_small_file(root, "package.json", errors)
        if content is not None:
            try:
                parsed_json = json.loads(content)
                if not isinstance(parsed_json, dict):
                    _add_error(errors, "DEPENDENCY_FILE_INVALID", "package.json", "Dependency manifest must be a JSON object.")
                else:
                    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                        if section not in parsed_json:
                            continue
                        values = parsed_json[section]
                        if isinstance(values, dict):
                            dependencies.extend(_dependency(str(name), "package.json") for name in values)
                        else:
                            _add_error(errors, "DEPENDENCY_FILE_INVALID", "package.json", f"{section} must be a JSON object.")
            except (json.JSONDecodeError, TypeError):
                _add_error(errors, "DEPENDENCY_FILE_INVALID", "package.json", "Could not statically parse dependency declarations.")
    pyproject = paths.get("pyproject.toml")
    if pyproject is not None:
        content = _read_small_file(root, "pyproject.toml", errors)
        if content is not None:
            try:
                parsed_toml = tomllib.loads(content)
                project = parsed_toml.get("project", {})
                if not isinstance(project, dict):
                    _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "project must be a TOML table.")
                else:
                    requires_python = project.get("requires-python")
                    if requires_python is not None:
                        if isinstance(requires_python, str) and requires_python.strip():
                            requirements.append(
                                _manual_requirement(
                                    "runtime",
                                    requires_python.strip(),
                                    "pyproject.toml [project.requires-python]",
                                    "Required Python versions cannot be verified during read-only inspection.",
                                )
                            )
                        else:
                            _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "project.requires-python must be a non-empty string.")
                    _append_python_dependencies(
                        project.get("dependencies", []),
                        "pyproject.toml [project.dependencies]",
                        dependencies,
                        requirements,
                        errors,
                    )
                    optional = project.get("optional-dependencies", {})
                    if not isinstance(optional, dict):
                        _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "project.optional-dependencies must be a TOML table.")
                    else:
                        for group, values in optional.items():
                            _append_python_dependencies(
                                values,
                                f"pyproject.toml [project.optional-dependencies.{group}]",
                                dependencies,
                                requirements,
                                errors,
                            )
                    dynamic = project.get("dynamic", [])
                    if not isinstance(dynamic, list) or any(not isinstance(item, str) for item in dynamic):
                        _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "project.dynamic must be an array of strings.")
                    else:
                        dynamic_dependencies = [
                            item
                            for item in dynamic
                            if item in {"dependencies", "optional-dependencies"}
                        ]
                        if dynamic_dependencies:
                            requirements.append(
                                _manual_requirement(
                                    "dependency-declaration",
                                    ", ".join(dynamic_dependencies),
                                    "pyproject.toml [project.dynamic]",
                                    "Dynamic dependencies cannot be fully determined from this manifest.",
                                )
                            )
                        if "requires-python" in dynamic:
                            requirements.append(
                                _manual_requirement(
                                    "runtime",
                                    "requires-python (dynamic)",
                                    "pyproject.toml [project.dynamic]",
                                    "The dynamic Python version requirement cannot be determined during read-only inspection.",
                                )
                            )
                build_system = parsed_toml.get("build-system", {})
                if not isinstance(build_system, dict):
                    _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "build-system must be a TOML table.")
                else:
                    _append_python_dependencies(
                        build_system.get("requires", []),
                        "pyproject.toml [build-system.requires]",
                        dependencies,
                        requirements,
                        errors,
                    )
                tool = parsed_toml.get("tool", {})
                if not isinstance(tool, dict):
                    _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "tool must be a TOML table.")
                else:
                    poetry = tool.get("poetry")
                    if poetry is not None:
                        if not isinstance(poetry, dict):
                            _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "tool.poetry must be a TOML table.")
                        else:
                            if "dependencies" in poetry:
                                _append_poetry_dependencies(
                                    poetry["dependencies"],
                                    "pyproject.toml [tool.poetry.dependencies]",
                                    dependencies,
                                    requirements,
                                    errors,
                                )
                            if "dev-dependencies" in poetry:
                                _append_poetry_dependencies(
                                    poetry["dev-dependencies"],
                                    "pyproject.toml [tool.poetry.dev-dependencies]",
                                    dependencies,
                                    requirements,
                                    errors,
                                )
                            groups = poetry.get("group", {})
                            if not isinstance(groups, dict):
                                _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "tool.poetry.group must be a TOML table.")
                            else:
                                for group, definition in groups.items():
                                    if not isinstance(definition, dict):
                                        _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", f"tool.poetry.group.{group} must be a TOML table.")
                                        continue
                                    if "dependencies" in definition:
                                        _append_poetry_dependencies(
                                            definition["dependencies"],
                                            f"pyproject.toml [tool.poetry.group.{group}.dependencies]",
                                            dependencies,
                                            requirements,
                                            errors,
                                        )
            except (tomllib.TOMLDecodeError, AttributeError, TypeError):
                _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", "Could not statically parse dependency declarations.")
    dependencies.sort(key=lambda item: (item["source"], item["name"].lower()))
    return dependencies, requirements, errors


def _append_python_dependencies(
    values: Any,
    source: str,
    dependencies: list[dict[str, str]],
    requirements: list[dict[str, str]],
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(values, list):
        _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", f"{source} must be an array of dependency strings.")
        return
    for value in values:
        if not isinstance(value, str):
            _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", f"{source} contains a non-string dependency.")
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", value)
        if match:
            dependencies.append(_dependency(match.group(1), source))
        else:
            requirements.append(
                _manual_requirement(
                    "dependency-declaration",
                    value,
                    source,
                    "Dependency declaration could not be classified statically.",
                )
            )


def _append_poetry_dependencies(
    values: Any,
    source: str,
    dependencies: list[dict[str, str]],
    requirements: list[dict[str, str]],
    errors: list[dict[str, str]],
) -> None:
    if not isinstance(values, dict):
        _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", f"{source} must be a TOML table.")
        return
    for name, declaration in values.items():
        if not isinstance(name, str):
            _add_error(errors, "DEPENDENCY_FILE_INVALID", "pyproject.toml", f"{source} contains a non-string dependency name.")
        elif name.lower() == "python":
            requirements.append(
                _manual_requirement(
                    "runtime",
                    str(declaration),
                    source,
                    "Required Python versions cannot be verified during read-only inspection.",
                )
            )
        else:
            dependencies.append(_dependency(name, source))


def _read_requirements_file(
    root: Path,
    relative: str,
    files: dict[str, dict[str, Any]],
    dependencies: list[dict[str, str]],
    requirements: list[dict[str, str]],
    errors: list[dict[str, str]],
    *,
    active: set[str],
    visited: set[str],
) -> None:
    if len(active) >= _MAX_REQUIREMENT_INCLUDE_DEPTH:
        _add_error(
            errors,
            "DEPENDENCY_INCLUDE_TOO_DEEP",
            relative,
            f"Requirements includes exceed the {_MAX_REQUIREMENT_INCLUDE_DEPTH}-file nesting limit.",
        )
        return
    if relative in active:
        _add_error(errors, "DEPENDENCY_INCLUDE_CYCLE", relative, "Requirements files include each other recursively.")
        return
    if relative in visited:
        return
    file_info = files.get(relative)
    if file_info is None:
        _add_error(errors, "MISSING_DEPENDENCY_FILE", relative, "Included dependency file is missing from the package.")
        return
    if file_info.get("symlink"):
        requirements.append(
            _manual_requirement(
                "dependency-file",
                relative,
                relative,
                "Symbolic links are not followed while reading dependency declarations.",
            )
        )
        return
    content = _read_small_file(root, relative, errors)
    if content is None:
        return

    active.add(relative)
    visited.add(relative)
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        include = _requirements_include(line)
        if include is not None:
            include = include.strip().strip("\"'")
            if not include:
                _add_error(errors, "DEPENDENCY_INCLUDE_INVALID", relative, "Requirements include path is empty.")
                continue
            decoded_include = unquote(include)
            normalized_include = decoded_include.replace("\\", "/")
            absolute_path = (
                normalized_include.startswith("/")
                and not normalized_include.startswith("//")
            ) or normalized_include.startswith("//") or bool(
                re.match(r"^[A-Za-z]:", decoded_include)
            ) or decoded_include.startswith("\\\\")
            if absolute_path:
                _add_error(
                    errors,
                    "DEPENDENCY_INCLUDE_OUTSIDE_PACKAGE",
                    normalized_include,
                    "Absolute requirements includes are not allowed.",
                )
                continue
            try:
                include_url = urlsplit(include)
            except ValueError:
                _add_error(errors, "DEPENDENCY_INCLUDE_INVALID", relative, "Requirements include path is malformed.")
                continue
            if include_url.scheme or include_url.netloc:
                requirements.append(
                    _manual_requirement(
                        "dependency-file",
                        include,
                        relative,
                        "Remote dependency includes are not downloaded or executed during inspection.",
                    )
                )
                continue
            include_path = unquote(include_url.path).replace("\\", "/")
            try:
                candidate = (
                    root / Path(relative).parent / include_path
                ).resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                _add_error(errors, "BAD_DEPENDENCY_INCLUDE", include, "Requirements include cannot be resolved safely.")
                continue
            if not _within(candidate, root):
                _add_error(
                    errors,
                    "DEPENDENCY_INCLUDE_OUTSIDE_PACKAGE",
                    include_path,
                    "Requirements include resolves outside the package directory.",
                )
                continue
            relative_include = _relative(candidate, root)
            if relative_include not in files:
                if candidate.exists():
                    _add_error(errors, "DEPENDENCY_INCLUDE_NOT_FILE", relative_include, "Requirements include must point to a file.")
                else:
                    _add_error(errors, "MISSING_DEPENDENCY_FILE", relative_include, "Included dependency file is missing from the package.")
                continue
            _read_requirements_file(
                root,
                relative_include,
                files,
                dependencies,
                requirements,
                errors,
                active=active,
                visited=visited,
            )
            continue

        if line.startswith("-"):
            requirements.append(
                _manual_requirement(
                    "dependency-option",
                    line,
                    relative,
                    "Dependency options can affect installation behavior and require manual review.",
                )
            )
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match:
            dependencies.append(_dependency(match.group(1), relative))
        else:
            requirements.append(
                _manual_requirement(
                    "dependency-declaration",
                    line,
                    relative,
                    "Dependency declaration could not be classified statically.",
                )
            )
    active.remove(relative)


def _requirements_include(line: str) -> str | None:
    if line == "-r" or line.startswith(("-r ", "-r\t")):
        return line[2:].strip()
    if line.startswith("-r") and not line.startswith("--"):
        return line[2:].strip().removeprefix("=").strip()
    if line == "--requirement" or line.startswith(("--requirement ", "--requirement\t", "--requirement=")):
        return line[len("--requirement"):].strip().removeprefix("=").strip()
    return None


def _manual_requirement(
    kind: str, name: str, source: str, reason: str
) -> dict[str, str]:
    return {
        "kind": kind,
        "name": name,
        "source": source,
        "support": "manual_review",
        "reason": reason,
    }


def _read_small_file(
    root: Path, relative: str, errors: list[dict[str, str]]
) -> str | None:
    return _read_bounded_package_text(
        root,
        root / relative,
        relative,
        _MAX_METADATA_SCAN_BYTES,
        errors,
        too_large_code="DEPENDENCY_FILE_TOO_LARGE",
        unreadable_code="DEPENDENCY_FILE_UNREADABLE",
        changed_code="DEPENDENCY_FILE_CHANGED",
        not_file_code="DEPENDENCY_FILE_NOT_A_FILE",
        outside_code="DEPENDENCY_FILE_OUTSIDE_PACKAGE",
    )


def _dependency(name: str, source: str) -> dict[str, str]:
    return {
        "kind": "package",
        "name": name,
        "source": source,
        "support": "manual_review",
        "reason": "Dependency availability and compatibility are not verified by read-only inspection.",
    }
