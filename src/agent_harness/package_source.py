"""包类型中立的来源与快照安全原语（issue #1083）。

Skill 安装器（`agent_harness.skills.package_manager`）与 MCP 包生命周期
（`agent_harness.plugins.mcp_package_manager`）需要**同一套**来源/快照安全校验：
URL/ref/subdir 校验、固定 commit 获取、Git 文件模式与树摘要、local 树摘要、
reparse/junction 拒绝。原先把这套原语放在 Skill 安装器里、由 MCP 反向 import 其
下划线私有符号，使 MCP 依赖 Skill 包的内部布局，改动 Skill 侧时 MCP 可能悄然漂移。

这里把**双方确实共用**的实现原样搬到既不属于 skills 也不属于 plugins 的中性顶层
模块（与既有 `agent_harness.mcp_precheck` / `agent_harness.plugin_inspection` 同构，
见 `agent_harness/plugins/__init__.py`）。各安装器只在**边界处**把本模块的
`PackageSourceError` 翻译成自己的产品异常（`SkillPackageError` / `MCPPackageError`）；
manifest、预检、安装范围、错误类型、启停与回退策略仍归各自所有。

错误消息与文件模式/树摘要算法均**逐字保留**，以便既有 CLI/API 结果与旧快照可读性不变。
不引通用插件 SPI / 基类层级 / 新依赖（ADR-0052 D7：等第二个同类原生贡献再评估 SPI）。
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from agent_harness.skills.inspection import _MAX_PACKAGE_ENTRIES, _is_junction

DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
SCP_GIT_URL_PATTERN = re.compile(r"(?:git@)?[A-Za-z0-9.-]+:[^\s:][^\s]*\Z")
GIT_TIMEOUT_SECONDS = 120


class PackageSourceError(ValueError):
    """A package source/snapshot cannot be validated or fetched safely.

    这是中性原语自己的错误类型；各产品安装器在边界处把它翻译成既有的
    `SkillPackageError` / `MCPPackageError`，不把本类型泄漏到 CLI/API 面。
    """


def validate_git_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PackageSourceError("Git URL must be a non-empty URL without surrounding whitespace")
    if any(ord(character) < 0x20 for character in value) or "\\" in value:
        raise PackageSourceError("Git URL contains an invalid character")
    if PureWindowsPath(value).drive:
        raise PackageSourceError("local filesystem paths are not Git URLs; use a file:// URL")
    if "://" not in value and SCP_GIT_URL_PATTERN.fullmatch(value):
        if value.startswith("-"):
            raise PackageSourceError("Git URL is invalid")
        return value
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
    except ValueError as error:
        raise PackageSourceError("Git URL is malformed") from error
    if scheme not in {"https", "ssh", "git", "file"}:
        raise PackageSourceError("Git URL must use https, ssh, git, or file")
    if parsed.query or parsed.fragment:
        raise PackageSourceError("Git URL query strings and fragments are not accepted")
    if (parsed.username or parsed.password) and (
        scheme != "ssh" or parsed.username != "git" or parsed.password
    ):
        raise PackageSourceError(
            "credential-bearing Git URLs are not accepted; configure a Git credential helper"
        )
    if scheme == "file":
        if parsed.netloc not in {"", "localhost"} or not parsed.path:
            raise PackageSourceError("file:// Git URL must point to a local repository")
    elif not hostname or not parsed.path:
        raise PackageSourceError("Git URL must include a host and repository path")
    return value


def validate_git_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1024
        or value.startswith("-")
        or any(ord(character) < 0x20 for character in value)
    ):
        raise PackageSourceError("Git ref is invalid")
    if COMMIT_PATTERN.fullmatch(value):
        return value
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--allow-onelevel", value],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PackageSourceError("could not validate Git ref") from error
    if result.returncode != 0:
        raise PackageSourceError("Git ref is invalid")
    return value


def validate_git_subdirectory(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        raise PackageSourceError("Skill subdirectory is invalid")
    normalized = value.removesuffix("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or PureWindowsPath(normalized).drive
        or ":" in normalized
        or "/".join(path.parts) != normalized
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PackageSourceError("Skill subdirectory must be a normalized relative path")
    return normalized


def _validate_git_tree_path(value: str) -> str:
    path = PurePosixPath(value)
    reserved_names = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{number}"
        for prefix in ("com", "lpt")
        for number in range(1, 10)
    }
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(character in '<>"|?*' or ord(character) < 0x20 for character in value)
        or PureWindowsPath(value).drive
        or "/".join(path.parts) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.endswith((".", " ")) for part in path.parts)
        or any(part.split(".", 1)[0].casefold() in reserved_names for part in path.parts)
    ):
        raise PackageSourceError("Git package contains an unsafe path")
    return value


def _validate_symlink_target(link_path: str, target: str) -> str:
    if not target or "\\" in target or ":" in target or PurePosixPath(target).is_absolute():
        raise PackageSourceError("Git package contains an unsafe symbolic link")
    if PureWindowsPath(target).drive:
        raise PackageSourceError("Git package contains an unsafe symbolic link")
    parts = list(PurePosixPath(link_path).parent.parts)
    for part in PurePosixPath(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise PackageSourceError("Git package symbolic link escapes its Skill directory")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    operation: str,
    stdout: Any = subprocess.PIPE,
) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            check=False,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise PackageSourceError(f"Git {operation} timed out") from error
    except OSError as error:
        raise PackageSourceError(f"Git {operation} could not start") from error
    if result.returncode != 0:
        raise PackageSourceError(f"Git {operation} failed (exit {result.returncode})")
    if stdout == subprocess.PIPE:
        return result.stdout
    return b""


def fetch_git_source(
    url: str,
    ref: str,
    subdirectory: str,
    temporary_root: Path,
) -> tuple[Path, str, str]:
    repo = temporary_root / "source.git"
    repo.mkdir()
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "never"
    _run_git(
        ["init", "--bare", "--quiet", str(repo)],
        cwd=temporary_root,
        env=environment,
        operation="repository initialization",
    )
    _run_git(
        [
            f"--git-dir={repo}",
            "fetch",
            "--quiet",
            "--depth=1",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            url,
            ref,
        ],
        cwd=temporary_root,
        env=environment,
        operation="fetch",
    )
    commit = _run_git(
        [f"--git-dir={repo}", "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        cwd=temporary_root,
        env=environment,
        operation="commit resolution",
    ).decode("ascii", errors="strict").strip()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise PackageSourceError("Git ref did not resolve to a commit")
    tree_oid = _run_git(
        [
            f"--git-dir={repo}",
            "rev-parse",
            "--verify",
            f"{commit}:{subdirectory}",
        ],
        cwd=temporary_root,
        env=environment,
        operation="Skill subdirectory resolution",
    ).decode("ascii", errors="strict").strip()
    if not COMMIT_PATTERN.fullmatch(tree_oid):
        raise PackageSourceError("Git Skill subdirectory did not resolve to a tree")
    object_type = _run_git(
        [f"--git-dir={repo}", "cat-file", "-t", tree_oid],
        cwd=temporary_root,
        env=environment,
        operation="Skill tree verification",
    ).decode("ascii", errors="strict").strip()
    if object_type != "tree":
        raise PackageSourceError("Git Skill subdirectory is not a directory")
    object_format = _run_git(
        [f"--git-dir={repo}", "rev-parse", "--show-object-format"],
        cwd=temporary_root,
        env=environment,
        operation="object format detection",
    ).decode("ascii", errors="strict").strip()
    if object_format not in {"sha1", "sha256"}:
        raise PackageSourceError("Git repository uses an unsupported object format")
    package = temporary_root / PurePosixPath(subdirectory).name
    modes_sha256 = _extract_git_tree(
        repo, tree_oid, package, object_format, temporary_root, environment
    )
    return package, commit, modes_sha256


def _extract_git_tree(
    repo: Path,
    tree_oid: str,
    package: Path,
    object_format: str,
    temporary_root: Path,
    environment: dict[str, str],
) -> str:
    listing = _run_git(
        [f"--git-dir={repo}", "ls-tree", "-r", "-z", "--full-tree", tree_oid],
        cwd=temporary_root,
        env=environment,
        operation="Skill tree listing",
    )
    entries: dict[str, tuple[str, str]] = {}
    folded_paths: dict[str, str] = {}
    for item in listing.split(b"\0"):
        if not item:
            continue
        try:
            metadata, raw_path = item.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ")
            path = _validate_git_tree_path(raw_path.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError) as error:
            raise PackageSourceError("Git package has an invalid tree entry") from error
        if path in entries:
            raise PackageSourceError("Git package contains duplicate paths")
        if mode not in {"100644", "100755", "120000"} or object_type != "blob":
            raise PackageSourceError("Git Skill submodules and special entries are unsupported")
        if not re.fullmatch(r"[0-9a-f]{40}" if object_format == "sha1" else r"[0-9a-f]{64}", object_id):
            raise PackageSourceError("Git package contains an invalid object id")
        entries[path] = (mode, object_id)
    if not entries:
        raise PackageSourceError("Git Skill subdirectory contains no tracked files")
    if len(entries) > _MAX_PACKAGE_ENTRIES:
        raise PackageSourceError("Git Skill package exceeds the supported file count")

    expected_directories: set[str] = set()
    for path in entries:
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts) + 1):
            component_path = "/".join(parts[:index])
            folded = component_path.casefold()
            previous_path = folded_paths.get(folded)
            if previous_path is not None and previous_path != component_path:
                raise PackageSourceError("Git package contains paths that collide on Windows")
            folded_paths[folded] = component_path
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in entries:
                raise PackageSourceError("Git package path conflicts with a file")
            expected_directories.add(parent)

    archive_path = temporary_root / "package.tar"
    with archive_path.open("xb") as archive_file:
        _run_git(
            [
                "-c",
                "core.autocrlf=false",
                f"--git-dir={repo}",
                "archive",
                "--format=tar",
                "--prefix=package/",
                tree_oid,
            ],
            cwd=temporary_root,
            env=environment,
            operation="Skill snapshot creation",
            stdout=archive_file,
        )

    package.mkdir()
    members_by_path: dict[str, tarfile.TarInfo] = {}
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive.getmembers():
            member_name = member.name.rstrip("/") if member.isdir() else member.name
            if member_name == "package":
                continue
            if not member_name.startswith("package/"):
                raise PackageSourceError("Git archive contains a path outside the Skill directory")
            path = _validate_git_tree_path(member_name[len("package/") :])
            if member.isdir():
                if path not in expected_directories:
                    raise PackageSourceError("Git archive contains an unexpected directory")
                continue
            if path in members_by_path:
                raise PackageSourceError("Git archive contains duplicate paths")
            expected = entries.get(path)
            if expected is None:
                raise PackageSourceError("Git archive contains an untracked path")
            if member.isfile() and expected[0] in {"100644", "100755"}:
                if bool(member.mode & 0o111) != (expected[0] == "100755"):
                    raise PackageSourceError("Git archive file mode does not match the pinned commit")
            elif member.issym() and expected[0] == "120000":
                _validate_symlink_target(path, member.linkname)
            else:
                raise PackageSourceError("Git archive contains an unsupported filesystem entry")
            members_by_path[path] = member
        if members_by_path.keys() != entries.keys():
            raise PackageSourceError("Git archive omitted tracked Skill files")

        directory_targets: dict[str, bool] = {}

        def points_to_directory(path: str, seen: set[str] | None = None) -> bool:
            if path in expected_directories:
                return True
            entry = entries.get(path)
            if entry is None or entry[0] != "120000":
                return False
            visited = set() if seen is None else set(seen)
            if path in visited:
                return False
            visited.add(path)
            target = members_by_path[path].linkname
            resolved = _validate_symlink_target(path, target)
            return points_to_directory(resolved, visited)

        for path, member in members_by_path.items():
            if member.issym():
                directory_targets[path] = points_to_directory(
                    _validate_symlink_target(path, member.linkname)
                )

        resolved_package = package.resolve(strict=True)
        for path, member in members_by_path.items():
            if not member.isfile():
                continue
            target = package.joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not _path_is_within(target.parent.resolve(strict=True), resolved_package):
                raise PackageSourceError("Git package path escaped its extraction directory")
            digest = hashlib.new(object_format)
            digest.update(f"blob {member.size}\0".encode("ascii"))
            content = archive.extractfile(member)
            if content is None:
                raise PackageSourceError("Git archive contains an unreadable file")
            with content, target.open("xb") as output:
                while chunk := content.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != entries[path][1]:
                raise PackageSourceError("Git archive content does not match the pinned commit")
            os.chmod(target, 0o755 if entries[path][0] == "100755" else 0o644)

        for path, member in members_by_path.items():
            if not member.issym():
                continue
            target = package.joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not _path_is_within(target.parent.resolve(strict=True), resolved_package):
                raise PackageSourceError("Git package symbolic link escaped its extraction directory")
            link_bytes = member.linkname.encode("utf-8", errors="strict")
            digest = hashlib.new(object_format)
            digest.update(f"blob {len(link_bytes)}\0".encode("ascii"))
            digest.update(link_bytes)
            if digest.hexdigest() != entries[path][1]:
                raise PackageSourceError("Git archive symbolic link does not match the pinned commit")
            os.symlink(
                member.linkname,
                target,
                target_is_directory=directory_targets[path],
            )
    return _git_modes_sha256({path: entry[0] for path, entry in entries.items()})


def _path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_point)


def reject_junctions(directory: str, names: list[str]) -> list[str]:
    junctions = [name for name in names if _is_junction(Path(directory) / name)]
    if junctions:
        joined = ", ".join(sorted(junctions))
        raise PackageSourceError(
            f"Skill package contains Windows junctions that cannot be copied safely: {joined}"
        )
    return []


def tree_sha256(root: Path) -> str:
    if not root.is_dir() or is_reparse_point(root):
        raise PackageSourceError(f"package path is not a regular directory: {root}")
    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            add(relative.encode("utf-8", errors="surrogateescape"))
            if is_reparse_point(path):
                add(b"link")
                add(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            elif stat.S_ISDIR(info.st_mode):
                add(b"directory")
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                add(b"file")
                add(info.st_size.to_bytes(8, "big"))
                with path.open("rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if not os.path.samestat(info, opened):
                        raise PackageSourceError(f"package changed while hashing: {relative}")
                    bytes_read = 0
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                        bytes_read += len(chunk)
                    current = path.lstat()
                    if (
                        not os.path.samestat(opened, current)
                        or bytes_read != opened.st_size
                        or opened.st_size != current.st_size
                        or opened.st_mtime_ns != current.st_mtime_ns
                    ):
                        raise PackageSourceError(f"package changed while hashing: {relative}")
            else:
                raise PackageSourceError(f"unsupported filesystem entry in package: {relative}")

    visit(root)
    return digest.hexdigest()


def git_tree_sha256(root: Path, expected_modes: str) -> str:
    """Validate the pinned Git modes while retaining the package content digest."""
    if not DIGEST_PATTERN.fullmatch(expected_modes):
        raise PackageSourceError("Git Skill file-mode digest is invalid")
    if os.name != "nt" and _git_modes_sha256_from_package(root) != expected_modes:
        raise PackageSourceError(f"Git file modes changed after installation: {root}")
    return tree_sha256(root)


def _git_modes_sha256(modes: dict[str, str]) -> str:
    digest = hashlib.sha256(b"git-skill-modes-v1\0")

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    for path in sorted(modes):
        add(path.encode("utf-8", errors="surrogateescape"))
        add(modes[path].encode("ascii"))
    return digest.hexdigest()


def _git_modes_sha256_from_package(root: Path) -> str:
    modes: dict[str, str] = {}

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            info = path.lstat()
            if is_reparse_point(path):
                modes[path.relative_to(root).as_posix()] = "120000"
                continue
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                mode = "100755" if info.st_mode & 0o111 else "100644"
                modes[path.relative_to(root).as_posix()] = mode

    visit(root)
    return _git_modes_sha256(modes)
