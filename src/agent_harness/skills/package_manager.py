"""Project-local lifecycle for imported Agent Skills directories."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_harness.skills.inspection import inspect_skill_package

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


MANIFEST_FILENAME = "plugin-installs.json"
MANIFEST_VERSION = 1
MANAGED_DIRECTORY_NAME = ".managed"
_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class SkillPackageError(ValueError):
    """A package lifecycle operation cannot safely be completed."""


class SkillPackageManager:
    """Install immutable local Skill snapshots and persist project activation."""

    def __init__(
        self,
        workspace_dir: str | os.PathLike[str],
        *,
        global_skills_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.project_skills_dir = self.workspace_dir / "skills"
        self.managed_skills_dir = self.project_skills_dir / MANAGED_DIRECTORY_NAME
        self.manifest_path = self.workspace_dir / MANIFEST_FILENAME
        self.global_skills_dir = (
            Path(global_skills_dir).expanduser()
            if global_skills_dir is not None
            else Path.home() / ".intelligence-agent" / "skills"
        )

    def install(self, source: str | os.PathLike[str]) -> dict[str, Any]:
        source_report = inspect_skill_package(
            source, scope="project", existing_skills=self.project_skills_dir
        )
        self._raise_if_uninstallable(source_report)
        name = source_report["name"]
        if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
            raise SkillPackageError("preflight did not return a valid Skill name")
        source_root = Path(source_report["source"])

        with _registry_lock(self.manifest_path):
            manifest = self._read_manifest()
            packages = manifest["packages"]
            if name in packages:
                raise SkillPackageError(f"Skill {name!r} is already installed")
            if self._path_exists(self.project_skills_dir / name):
                raise SkillPackageError(f"project Skill {name!r} already exists")
            if self._path_exists(self.global_skills_dir / name):
                raise SkillPackageError(f"global Skill {name!r} already exists; refusing a shadow conflict")

            self._ensure_managed_directory()
            target = self.managed_skills_dir / name
            if self._path_exists(target):
                raise SkillPackageError(
                    f"managed path for Skill {name!r} already exists without an install record"
                )

            staging_root = self.managed_skills_dir / f".staging-{uuid.uuid4().hex}"
            staging_package = staging_root / name
            staging_root.mkdir()
            try:
                shutil.copytree(source_root, staging_package, symlinks=True, copy_function=shutil.copy2)
                report = inspect_skill_package(staging_package, scope="project")
                self._raise_if_uninstallable(report)
                digest = _tree_sha256(staging_package)
                record = {
                    "type": "skill",
                    "scope": "project",
                    "source": str(source_root),
                    "sha256": digest,
                    "skill_sha256": _file_sha256(staging_package / "SKILL.md"),
                    "enabled": False,
                    "installed_at": datetime.now(UTC).isoformat(),
                    "compatibility": {
                        "status": report["status"],
                        "license": report["license"],
                        "requirements": report["requirements"],
                        "errors": report["errors"],
                    },
                }
                os.rename(staging_package, target)
                packages[name] = record
                try:
                    self._write_manifest(manifest)
                except BaseException:
                    with suppress(OSError):
                        os.rename(target, staging_package)
                    raise
                return copy.deepcopy(record)
            finally:
                if staging_root.exists():
                    shutil.rmtree(staging_root, ignore_errors=True)

    def enable(self, name: str) -> None:
        with _registry_lock(self.manifest_path):
            manifest = self._read_manifest()
            record = self._record(manifest, name)
            status = record["compatibility"]["status"]
            if status != "complete":
                raise SkillPackageError(
                    f"Skill {name!r} has status {status!r}; only complete packages can be enabled"
                )
            package = self._package_path(name)
            self._verify_package(package, record)
            if record["enabled"]:
                return
            record["enabled"] = True
            self._write_manifest(manifest)

    def disable(self, name: str) -> None:
        with _registry_lock(self.manifest_path):
            manifest = self._read_manifest()
            record = self._record(manifest, name)
            if not record["enabled"]:
                return
            record["enabled"] = False
            self._write_manifest(manifest)

    def remove(self, name: str) -> None:
        with _registry_lock(self.manifest_path):
            manifest = self._read_manifest()
            self._record(manifest, name)
            record = manifest["packages"][name]
            package = self._package_path(name)
            self._verify_package(package, record)

            tombstone = self.managed_skills_dir / f".removing-{uuid.uuid4().hex}"
            os.rename(package, tombstone)
            try:
                unchanged = _tree_sha256(tombstone) == record["sha256"]
            except BaseException:
                try:
                    os.rename(tombstone, package)
                except OSError as error:
                    raise SkillPackageError(
                        f"Skill {name!r} could not be rechecked; registry retained and package preserved at {tombstone}"
                    ) from error
                raise
            if not unchanged:
                try:
                    os.rename(tombstone, package)
                except OSError as error:
                    raise SkillPackageError(
                        f"Skill {name!r} changed during removal; registry retained and package preserved at {tombstone}"
                    ) from error
                raise SkillPackageError(
                    f"Skill {name!r} changed during removal; package restored at {package}"
                )

            del manifest["packages"][name]
            try:
                self._write_manifest(manifest)
            except BaseException:
                with suppress(OSError):
                    os.rename(tombstone, package)
                raise

            try:
                shutil.rmtree(tombstone)
            except OSError as error:
                raise SkillPackageError(
                    f"Skill {name!r} was unregistered, but cleanup remains at {tombstone}"
                ) from error

    def list_packages(self) -> dict[str, dict[str, Any]]:
        with _registry_lock(self.manifest_path):
            return copy.deepcopy(self._read_manifest()["packages"])

    def enabled_skill_digests(self) -> dict[str, str]:
        """Return selected Skills and their install-time bodies after snapshot checks."""
        with _registry_lock(self.manifest_path):
            packages = self._read_manifest()["packages"]
            enabled: dict[str, str] = {}
            for name, record in packages.items():
                if not record["enabled"] or record["compatibility"]["status"] != "complete":
                    continue
                package = self.managed_skills_dir / name
                if not self._is_managed_package(package):
                    raise SkillPackageError(f"enabled Skill {name!r} is missing or unsafe")
                self._verify_package(package, record)
                enabled[name] = record["skill_sha256"]
            return enabled

    def enabled_skill_names(self) -> set[str]:
        """Return selected Skills after validating their installed snapshots."""
        return set(self.enabled_skill_digests())

    def _read_manifest(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": MANIFEST_VERSION, "packages": {}}
        except (OSError, ValueError) as error:
            raise SkillPackageError(f"cannot read install manifest: {type(error).__name__}") from error
        if (
            not isinstance(payload, dict)
            or payload.get("version") != MANIFEST_VERSION
            or not isinstance(payload.get("packages"), dict)
        ):
            raise SkillPackageError("install manifest has an unsupported shape")
        for name, record in payload["packages"].items():
            if (
                not isinstance(name, str)
                or not _NAME_PATTERN.fullmatch(name)
                or not isinstance(record, dict)
                or record.get("type") != "skill"
                or record.get("scope") != "project"
                or not isinstance(record.get("source"), str)
                or not isinstance(record.get("sha256"), str)
                or not _DIGEST_PATTERN.fullmatch(record["sha256"])
                or not isinstance(record.get("skill_sha256"), str)
                or not _DIGEST_PATTERN.fullmatch(record["skill_sha256"])
                or not isinstance(record.get("enabled"), bool)
                or not isinstance(record.get("compatibility"), dict)
                or record["compatibility"].get("status") not in {"complete", "needs-adaptation"}
            ):
                raise SkillPackageError(f"install manifest record for {name!r} is invalid")
        return payload

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.manifest_path.name + ".tmp-", dir=self.workspace_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.manifest_path)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _record(self, manifest: dict[str, Any], name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
            raise SkillPackageError(f"invalid Skill name {name!r}")
        record = manifest["packages"].get(name)
        if record is None:
            raise SkillPackageError(f"Skill {name!r} is not installed")
        return record

    def _package_path(self, name: str) -> Path:
        self._ensure_managed_directory()
        package = self.managed_skills_dir / name
        if not self._is_managed_package(package):
            raise SkillPackageError(f"managed Skill package {name!r} is missing or unsafe")
        return package

    def _verify_package(self, package: Path, record: dict[str, Any]) -> None:
        if _tree_sha256(package) != record["sha256"]:
            raise SkillPackageError(
                f"Skill {package.name!r} changed after installation; refusing this operation"
            )

    def _is_managed_package(self, package: Path) -> bool:
        if (
            _is_reparse_point(self.project_skills_dir)
            or _is_reparse_point(self.managed_skills_dir)
            or _is_reparse_point(package)
            or not package.is_dir()
        ):
            return False
        try:
            workspace_root = self.workspace_dir.resolve(strict=True)
            project_root = self.project_skills_dir.resolve(strict=True)
            root = self.managed_skills_dir.resolve(strict=True)
            resolved = package.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return project_root.parent == workspace_root and root.parent == project_root and resolved.parent == root

    def _ensure_managed_directory(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.project_skills_dir):
            raise SkillPackageError("project Skills directory cannot be a symbolic link or junction")
        self.project_skills_dir.mkdir(exist_ok=True)
        if self.project_skills_dir.resolve(strict=True).parent != self.workspace_dir.resolve(strict=True):
            raise SkillPackageError("project Skills directory resolves outside the workspace")
        if _is_reparse_point(self.managed_skills_dir):
            raise SkillPackageError("managed Skills directory cannot be a symbolic link or junction")
        self.managed_skills_dir.mkdir(exist_ok=True)
        if self.managed_skills_dir.resolve(strict=True).parent != self.project_skills_dir.resolve(strict=True):
            raise SkillPackageError("managed Skills directory resolves outside the project Skills directory")

    def _path_exists(self, path: Path) -> bool:
        return path.exists() or path.is_symlink() or _is_reparse_point(path)

    @staticmethod
    def _raise_if_uninstallable(report: dict[str, Any]) -> None:
        if report["status"] != "unsupported":
            return
        if any(error.get("code") == "NAME_CONFLICT" for error in report["errors"]):
            name = report.get("name")
            raise SkillPackageError(f"project Skill {name!r} already exists")
        codes = ", ".join(error.get("code", "unknown") for error in report["errors"])
        raise SkillPackageError(f"Skill preflight rejected the package: {codes or 'unsupported'}")


@contextmanager
def _registry_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                with suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    yield  # pragma: no cover


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_point)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    if not root.is_dir() or _is_reparse_point(root):
        raise SkillPackageError(f"package path is not a regular directory: {root}")
    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            add(relative.encode("utf-8", errors="surrogateescape"))
            if _is_reparse_point(path):
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
                        raise SkillPackageError(f"package changed while hashing: {relative}")
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
                        raise SkillPackageError(f"package changed while hashing: {relative}")
            else:
                raise SkillPackageError(f"unsupported filesystem entry in package: {relative}")

    visit(root)
    return digest.hexdigest()
