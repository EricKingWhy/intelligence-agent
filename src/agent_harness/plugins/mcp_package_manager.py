"""Explicit, snapshot based lifecycle for MCP server description packages.

This module manages only package provenance and project selection. Connections and
tool calls stay in the existing MCP capability and ToolExecutor path.
"""

from __future__ import annotations

import copy
import functools
import json
import os
import shutil
import tempfile
import uuid
from contextlib import ExitStack, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from agent_harness import package_source
from agent_harness.mcp_precheck import inspect_mcp_servers, load_mcp_servers
from agent_harness.skills.package_lock import registry_lock
from agent_harness.skills.package_manager import SkillPackageError, SkillPackageManager

MCP_MANIFEST_FILENAME = "mcp-plugin-installs.json"
MCP_MANIFEST_VERSION = 1


class MCPPackageError(ValueError):
    """An MCP package operation cannot be completed safely."""


def _mcp_source(func: Any) -> Any:
    """把中性来源原语的 `PackageSourceError` 在 MCP 边界翻译成 `MCPPackageError`。

    共用实现住在 `agent_harness.package_source`（issue #1083）；这里只做错误类型
    适配，行为（消息、摘要算法、校验时机）与迁移前逐字一致。
    """

    @functools.wraps(func)
    def boundary(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except package_source.PackageSourceError as error:
            raise MCPPackageError(str(error)) from error

    return boundary


# 共用来源/快照原语（原样实现见 `agent_harness.package_source`）。不抛错的直接绑定，
# 会抛 `PackageSourceError` 的走 `_mcp_source` 翻译成 `MCPPackageError`。
_COMMIT_PATTERN = package_source.COMMIT_PATTERN
_DIGEST_PATTERN = package_source.DIGEST_PATTERN
_is_reparse_point = package_source.is_reparse_point
_validate_git_url = _mcp_source(package_source.validate_git_url)
_validate_git_ref = _mcp_source(package_source.validate_git_ref)
_validate_git_subdirectory = _mcp_source(package_source.validate_git_subdirectory)
_fetch_git_skill = _mcp_source(package_source.fetch_git_source)
_tree_sha256 = _mcp_source(package_source.tree_sha256)
_git_tree_sha256 = _mcp_source(package_source.git_tree_sha256)
_reject_junctions = _mcp_source(package_source.reject_junctions)


class MCPPackageManager:
    """Install immutable MCP descriptor snapshots and select them per project."""

    def __init__(
        self,
        workspace_dir: str | os.PathLike[str],
        *,
        scope: str = "project",
        global_skills_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if scope not in {"project", "global"}:
            raise MCPPackageError("MCP package scope must be project or global")
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.scope = scope
        # Reuse the established project/global storage roots and existing Git source
        # validation. MCP snapshots stay in their own lane below that same package root.
        self._skill_manager = SkillPackageManager(
            self.workspace_dir,
            scope=scope,
            global_skills_dir=global_skills_dir,
        )
        self.scope_root = self._skill_manager.scope_root
        self.manifest_path = self.scope_root / MCP_MANIFEST_FILENAME
        self.package_root = self.scope_root / "mcp"
        self.versions_root = self.package_root / ".versions"
        self.global_skills_dir = self._skill_manager.global_skills_dir
        self.errors: list[str] = []
        self.runtime_packages: list[dict[str, Any]] = []

    def install(
        self,
        source: str | os.PathLike[str],
        *,
        package_id: str | None = None,
    ) -> dict[str, Any]:
        source_root = Path(source).expanduser().resolve(strict=True)
        package_id = self._package_id(package_id or source_root.name)
        return self._install_snapshot(
            source_root,
            package_id=package_id,
            source=str(source_root),
            source_kind="local",
        )

    def install_git(
        self,
        url: str,
        *,
        ref: str,
        subdirectory: str,
        package_id: str | None = None,
    ) -> dict[str, Any]:
        safe_url = _validate_git_url(url)
        safe_ref = _validate_git_ref(ref)
        safe_subdirectory = _validate_git_subdirectory(subdirectory)
        package_id = self._package_id(
            package_id or PurePosixPath(safe_subdirectory).name
        )
        with tempfile.TemporaryDirectory(prefix="agent-harness-mcp-git-") as temporary:
            package, commit, git_modes_sha256 = _fetch_git_skill(
                safe_url, safe_ref, safe_subdirectory, Path(temporary)
            )
            return self._install_snapshot(
                package,
                package_id=package_id,
                source=safe_url,
                source_kind="git",
                source_ref=safe_ref,
                resolved_commit=commit,
                git_subdirectory=safe_subdirectory,
                git_modes_sha256=git_modes_sha256,
            )

    def update(
        self,
        package_id: str,
        *,
        ref: str | None = None,
        source: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """Stage an explicit local snapshot or Git ref; current remains selected until restart."""
        package_id = self._package_id(package_id)
        record = self._record_for_scope(package_id)
        if record.get("pending_version") is not None:
            raise MCPPackageError(
                f"MCP package {package_id!r} already has a pending version"
            )
        expected_current = record["current_version"]["sha256"]
        if source is not None:
            pending_root = Path(source).expanduser().resolve(strict=True)
            new_version = self._build_version(
                pending_root, source=str(pending_root), source_kind="local"
            )
            return self._stage_update(
                package_id, new_version, pending_root, expected_current=expected_current
            )
        elif ref is not None:
            if record["current_version"]["source_kind"] != "git":
                raise MCPPackageError(
                    f"MCP package {package_id!r} is not installed from Git"
                )
            safe_ref = _validate_git_ref(ref)
            current = record["current_version"]
            safe_url = _validate_git_url(current["source"])
            safe_subdirectory = _validate_git_subdirectory(current["git_subdirectory"])
            with tempfile.TemporaryDirectory(
                prefix="agent-harness-mcp-update-"
            ) as temporary:
                package, commit, git_modes_sha256 = _fetch_git_skill(
                    safe_url, safe_ref, safe_subdirectory, Path(temporary)
                )
                new_version = self._build_version(
                    package,
                    source=safe_url,
                    source_kind="git",
                    source_ref=safe_ref,
                    resolved_commit=commit,
                    git_subdirectory=safe_subdirectory,
                    git_modes_sha256=git_modes_sha256,
                )
                return self._stage_update(
                    package_id, new_version, package, expected_current=expected_current
                )
        else:
            raise MCPPackageError("update requires a local --source or Git --ref")

    def _stage_update(
        self,
        package_id: str,
        version: dict[str, Any],
        source_root: Path,
        *,
        expected_current: str,
    ) -> dict[str, Any]:
        with self._locked_registry():
            manifest = self._read_manifest()
            record = self._record(manifest, package_id)
            if (
                record.get("pending_version") is not None
                or record["current_version"]["sha256"] != expected_current
            ):
                raise MCPPackageError(
                    f"MCP package {package_id!r} changed while the update was being prepared"
                )
            if version["sha256"] == record["current_version"]["sha256"]:
                raise MCPPackageError(
                    f"MCP package {package_id!r} already has this content"
                )
            self._store_snapshot(package_id, version, source_root)
            record["pending_version"] = version
            self._write_manifest(manifest)
            return copy.deepcopy(record)

    def rollback(self, package_id: str) -> dict[str, Any]:
        package_id = self._package_id(package_id)
        with self._locked_registry():
            manifest = self._read_manifest()
            record = self._record(manifest, package_id)
            if record.get("pending_version") is not None:
                raise MCPPackageError(
                    f"MCP package {package_id!r} already has a pending version"
                )
            previous = record.get("previous_version")
            if previous is None:
                raise MCPPackageError(
                    f"MCP package {package_id!r} has no previous snapshot"
                )
            self._verify_snapshot(package_id, previous)
            record["pending_version"] = copy.deepcopy(previous)
            self._write_manifest(manifest)
            return copy.deepcopy(record)

    def trust(self, package_id: str, *, pending: bool = False) -> dict[str, Any]:
        package_id = self._package_id(package_id)
        with self._locked_registry():
            manifest = self._read_manifest()
            record = self._record(manifest, package_id)
            version_key = "pending_version" if pending else "current_version"
            version = record.get(version_key)
            if version is None:
                raise MCPPackageError(
                    f"MCP package {package_id!r} has no pending version"
                )
            self._verify_snapshot(package_id, version)
            if not self._is_runnable_compatibility(version["compatibility"]):
                raise MCPPackageError(
                    f"MCP package {package_id!r} is not fully compatible and cannot be trusted"
                )
            version["trust_status"] = "trusted"
            version["trust_sha256"] = version["sha256"]
            self._write_manifest(manifest)
            return copy.deepcopy(record)

    def enable(self, package_id: str, *, scope: str | None = None) -> dict[str, Any]:
        package_id = self._package_id(package_id)
        if self.scope == "global":
            raise MCPPackageError(
                "global MCP packages are enabled per project; select them from the project that uses them"
            )
        if scope is not None and scope not in {"project", "global"}:
            raise MCPPackageError(f"unsupported package scope {scope!r}")
        with self._locked_registry():
            manifest = self._read_manifest()
            selections = manifest["enable_results"]
            local = manifest["packages"].get(package_id)
            global_record = (
                self._scope_manager("global").list_packages().get(package_id)
            )
            if scope is None:
                if local is not None and global_record is not None:
                    raise MCPPackageError(
                        f"MCP package {package_id!r} exists in both project and global scope; "
                        "select --scope project or --scope global explicitly"
                    )
                if local is None and global_record is None:
                    raise MCPPackageError(
                        f"MCP package {package_id!r} is not installed"
                    )
                scope = "project" if local is not None else "global"
            selected = local if scope == "project" else global_record
            if selected is None:
                raise MCPPackageError(
                    f"MCP package {package_id!r} has no {scope} version"
                )
            version = selected["current_version"]
            if version.get("trust_status") != "trusted":
                raise MCPPackageError(
                    f"MCP package {package_id!r} is untrusted; run plugins mcp trust before enabling it"
                )
            if not self._is_runnable_compatibility(version["compatibility"]):
                raise MCPPackageError(
                    f"MCP package {package_id!r} is not fully compatible; refusing to enable it"
                )
            source_manager = (
                self if scope == "project" else self._scope_manager("global")
            )
            source_manager._verify_snapshot(package_id, version)
            names = source_manager._active_server_names(package_id, version)
            for other_id, selection in selections.items():
                if other_id == package_id:
                    continue
                other_scope = selection["selected_scope"]
                if other_scope == "project":
                    other_manager = self
                    other_record = manifest["packages"].get(other_id)
                else:
                    other_manager = self._scope_manager(other_scope)
                    other_record = other_manager.list_packages().get(other_id)
                if other_record is None:
                    raise MCPPackageError(
                        f"selected MCP package {other_id!r} is missing"
                    )
                other_names = other_manager._active_server_names(
                    other_id, other_record["current_version"]
                )
                collisions = sorted(set(names) & set(other_names))
                if collisions:
                    raise MCPPackageError(
                        f"MCP server name conflict {collisions} between packages "
                        f"{other_id!r} and {package_id!r}; resolve it explicitly"
                    )
            selections[package_id] = {
                "selected_scope": scope,
                "source": selected["source"],
            }
            self._write_manifest(manifest)
            return copy.deepcopy(selections[package_id])

    def disable(self, package_id: str) -> dict[str, Any]:
        package_id = self._package_id(package_id)
        if self.scope == "global":
            raise MCPPackageError("global MCP packages are disabled per project")
        with self._locked_registry():
            manifest = self._read_manifest()
            previous = manifest["enable_results"].pop(package_id, None)
            self._write_manifest(manifest)
            return {
                "selected_scope": previous.get("selected_scope") if previous else None
            }

    def enable_results(self) -> dict[str, dict[str, Any]]:
        if self.scope == "global":
            return {}
        with self._locked_registry():
            return copy.deepcopy(self._read_manifest()["enable_results"])

    def list_packages(self) -> dict[str, dict[str, Any]]:
        with self._locked_registry():
            return copy.deepcopy(self._read_manifest()["packages"])

    def _apply_pending_versions_to_manifest(
        self, manifest: dict[str, Any], blocked_package_ids: set[str]
    ) -> int:
        applied = 0
        for package_id, record in sorted(manifest["packages"].items()):
            pending = record.get("pending_version")
            if pending is None or package_id in blocked_package_ids:
                continue
            if pending[
                "trust_status"
            ] != "trusted" or not self._is_runnable_compatibility(
                pending["compatibility"]
            ):
                continue
            try:
                self._verify_snapshot(package_id, pending)
            except (MCPPackageError, ValueError, OSError) as error:
                self.errors.append(
                    f"pending version for MCP package {package_id!r} failed preflight; "
                    f"current version retained ({type(error).__name__})"
                )
                continue
            record["previous_version"] = record["current_version"]
            record["current_version"] = pending
            record["pending_version"] = None
            applied += 1
        return applied

    def selected_servers(
        self,
        *,
        apply_pending: bool = True,
        reserved_server_names: set[str] | None = None,
    ) -> list[Any]:
        """Load selected trusted MCP configs; environment references expand only here."""
        if self.scope == "global":
            raise MCPPackageError(
                "runtime selection must be read from a project package manager"
            )
        self.errors = []
        self.runtime_packages = []
        combined: list[Any] = []
        global_manager = self._scope_manager("global")
        managers = {"project": self, "global": global_manager}
        with ExitStack() as locks:
            locks.enter_context(self._locked_registry())
            manifests = {"project": self._read_manifest()}
            selections = copy.deepcopy(manifests["project"]["enable_results"])
            selected_scopes = {item["selected_scope"] for item in selections.values()}
            if "global" in selected_scopes:
                locks.enter_context(global_manager._locked_registry())
                manifests["global"] = global_manager._read_manifest()
            if apply_pending and selections:
                blocked = self._blocked_pending_versions(
                    selections,
                    reserved_server_names or set(),
                    manifests,
                    managers,
                )
                for scope in selected_scopes:
                    manager = managers[scope]
                    if manager._apply_pending_versions_to_manifest(
                        manifests[scope], blocked.get(scope, set())
                    ):
                        manager._write_manifest(manifests[scope])
                self.errors.extend(global_manager.errors)
        for package_id, selection in sorted(selections.items()):
            scope = selection["selected_scope"]
            manager = managers[scope]
            record = manifests[scope]["packages"].get(package_id)
            if record is None:
                self.errors.append(f"selected MCP package {package_id!r} is missing")
                continue
            version = record["current_version"]
            if version["trust_status"] != "trusted":
                self.errors.append(
                    f"selected MCP package {package_id!r} is untrusted; server not started"
                )
                continue
            if not self._is_runnable_compatibility(version["compatibility"]):
                self.errors.append(
                    f"selected MCP package {package_id!r} is not fully compatible; server not started"
                )
                continue
            try:
                package = manager._verify_snapshot(package_id, version)
                servers = load_mcp_servers(package)
                combined.extend(servers)
                active_names = [server.name for server in servers if server.enabled]
                if active_names:
                    self.runtime_packages.append(
                        {
                            "package_id": package_id,
                            "scope": selection["selected_scope"],
                            "sha256": version["sha256"],
                            "server_names": active_names,
                        }
                    )
            except (MCPPackageError, ValueError, OSError) as error:
                self.errors.append(
                    f"selected MCP package {package_id!r} failed preflight; server not started ({type(error).__name__})"
                )
        return combined

    def _blocked_pending_versions(
        self,
        selections: dict[str, dict[str, Any]],
        reserved_server_names: set[str],
        manifests: dict[str, dict[str, Any]],
        managers: dict[str, MCPPackageManager],
    ) -> dict[str, set[str]]:
        """Keep a selected update pending when its servers would collide at startup."""
        versions: dict[tuple[str, str], dict[str, Any]] = {}
        for package_id, selection in sorted(selections.items()):
            scope = selection["selected_scope"]
            manager = managers[scope]
            record = manifests[scope]["packages"].get(package_id)
            if record is None:
                continue
            current = record["current_version"]
            current_names: list[str] = []
            if current["trust_status"] == "trusted" and self._is_runnable_compatibility(
                current["compatibility"]
            ):
                try:
                    current_names = manager._active_server_names(package_id, current)
                except (MCPPackageError, ValueError, OSError):
                    pass
            pending = record.get("pending_version")
            pending_names: list[str] = []
            pending_usable = bool(
                pending
                and pending["trust_status"] == "trusted"
                and self._is_runnable_compatibility(pending["compatibility"])
            )
            if pending_usable:
                try:
                    pending_names = manager._active_server_names(package_id, pending)
                except (MCPPackageError, ValueError, OSError) as error:
                    self.errors.append(
                        f"pending version for MCP package {package_id!r} failed preflight; "
                        f"current version retained ({type(error).__name__})"
                    )
                    versions[(scope, package_id)] = {
                        "current": current_names,
                        "pending": [],
                        "pending_invalid": True,
                    }
                    continue
            versions[(scope, package_id)] = {
                "current": current_names,
                "pending": pending_names,
                "pending_invalid": False,
            }

        blocked: dict[str, set[str]] = {"project": set(), "global": set()}
        for (scope, package_id), version in versions.items():
            if version["pending_invalid"]:
                blocked[scope].add(package_id)
        pending_items = [
            (key, value["pending"])
            for key, value in versions.items()
            if value["pending"]
        ]
        for (scope, package_id), names in pending_items:
            conflicts = set(names) & reserved_server_names
            other_sources: list[str] = []
            for (other_scope, other_id), other in versions.items():
                if (other_scope, other_id) == (scope, package_id):
                    continue
                if set(names) & set(other["current"]):
                    conflicts.update(set(names) & set(other["current"]))
                    other_sources.append(f"{other_scope} package {other_id!r}")
            for (other_scope, other_id), other_names in pending_items:
                if (other_scope, other_id) == (scope, package_id):
                    continue
                overlap = set(names) & set(other_names)
                if overlap:
                    conflicts.update(overlap)
                    other_sources.append(
                        f"{other_scope} package {other_id!r} pending version"
                    )
            if conflicts:
                blocked[scope].add(package_id)
                source_detail = ", ".join(sorted(set(other_sources)))
                if set(names) & reserved_server_names:
                    source_detail = ", ".join(
                        part
                        for part in (source_detail, "configured MCP servers")
                        if part
                    )
                self.errors.append(
                    f"pending version for MCP package {package_id!r} conflicts on "
                    f"server names {sorted(conflicts)}"
                    + (f" with {source_detail}" if source_detail else "")
                    + "; current version retained"
                )
        return blocked

    def _install_snapshot(
        self,
        source_root: Path,
        *,
        package_id: str,
        source: str,
        source_kind: str,
        source_ref: str | None = None,
        resolved_commit: str | None = None,
        git_subdirectory: str | None = None,
        git_modes_sha256: str | None = None,
    ) -> dict[str, Any]:
        version = self._build_version(
            source_root,
            source=source,
            source_kind=source_kind,
            source_ref=source_ref,
            resolved_commit=resolved_commit,
            git_subdirectory=git_subdirectory,
            git_modes_sha256=git_modes_sha256,
        )
        with self._locked_registry():
            manifest = self._read_manifest()
            if package_id in manifest["packages"]:
                raise MCPPackageError(
                    f"MCP package {package_id!r} is already installed"
                )
            self._store_snapshot(package_id, version, source_root)
            record = {
                "type": "mcp",
                "scope": self.scope,
                "source": source,
                "installed_at": datetime.now(UTC).isoformat(),
                "current_version": version,
                "pending_version": None,
                "previous_version": None,
            }
            manifest["packages"][package_id] = record
            self._write_manifest(manifest)
            return copy.deepcopy(record)

    def _build_version(
        self,
        source_root: Path,
        *,
        source: str,
        source_kind: str,
        source_ref: str | None = None,
        resolved_commit: str | None = None,
        git_subdirectory: str | None = None,
        git_modes_sha256: str | None = None,
    ) -> dict[str, Any]:
        report = inspect_mcp_servers(source_root)
        if report["status"] == "unsupported" or not report.get("description_file"):
            codes = sorted(
                {item.get("code", "unknown") for item in report.get("errors", [])}
            )
            for server in report.get("servers", []):
                codes.extend(
                    item.get("code", "unknown") for item in server.get("errors", [])
                )
            raise MCPPackageError(
                f"MCP package preflight rejected the source: {', '.join(codes) or 'missing description'}"
            )
        if source_kind == "git":
            if not git_modes_sha256 or not _DIGEST_PATTERN.fullmatch(git_modes_sha256):
                raise MCPPackageError("Git source file-mode metadata is invalid")
            digest = _git_tree_sha256(source_root, git_modes_sha256)
        else:
            digest = _tree_sha256(source_root)
        version = {
            "source": source,
            "source_kind": source_kind,
            "sha256": digest,
            "snapshot_id": digest,
            "trust_status": "untrusted",
            "trust_sha256": digest,
            "compatibility": self._compatibility_from_report(report),
            "server_names": [server["name"] for server in report["servers"]],
        }
        if source_kind == "git":
            if not source_ref or not resolved_commit or not git_subdirectory:
                raise MCPPackageError("Git source version metadata is incomplete")
            if not _COMMIT_PATTERN.fullmatch(resolved_commit):
                raise MCPPackageError("Git source did not resolve to a full commit")
            version.update(
                source_ref=source_ref,
                resolved_commit=resolved_commit,
                git_subdirectory=git_subdirectory,
                git_modes_sha256=git_modes_sha256,
            )
        return version

    def _store_snapshot(
        self, package_id: str, version: dict[str, Any], source_root: Path
    ) -> None:
        self._ensure_storage_root()
        digest = version["sha256"]
        target = self._snapshot_path(package_id, digest, create=True)
        if _is_reparse_point(target):
            raise MCPPackageError(f"MCP snapshot target for {package_id!r} is unsafe")
        if target.exists():
            if self._version_tree_sha256(target, version) != digest:
                raise MCPPackageError(
                    f"stored MCP snapshot {package_id!r}@{digest} changed"
                )
            return
        if _is_reparse_point(source_root) or not source_root.is_dir():
            raise MCPPackageError("MCP package source must be a regular directory")
        staging = target.parent / f".staging-{uuid.uuid4().hex}"
        try:
            shutil.copytree(
                source_root,
                staging,
                symlinks=True,
                copy_function=shutil.copy2,
                ignore=_reject_junctions,
            )
            if self._version_tree_sha256(staging, version) != digest:
                raise MCPPackageError(
                    "MCP package changed while creating its immutable snapshot"
                )
            snapshot_report = inspect_mcp_servers(staging)
            if snapshot_report["status"] == "unsupported" or not snapshot_report.get(
                "description_file"
            ):
                raise MCPPackageError("MCP package snapshot failed preflight")
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _snapshot_path(self, package_id: str, digest: str, *, create: bool) -> Path:
        package_id = self._package_id(package_id)
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise MCPPackageError("MCP snapshot digest is invalid")
        self._ensure_storage_root()
        package_versions = self.versions_root / package_id
        if _is_reparse_point(package_versions):
            raise MCPPackageError(
                f"MCP snapshot directory for {package_id!r} is unsafe"
            )
        if create:
            package_versions.mkdir(exist_ok=True)
        elif not package_versions.is_dir():
            raise MCPPackageError(
                f"MCP snapshot directory for {package_id!r} is missing"
            )
        if package_versions.resolve(strict=True).parent != self.versions_root.resolve(
            strict=True
        ):
            raise MCPPackageError(
                f"MCP snapshot directory for {package_id!r} escapes its root"
            )
        target = package_versions / digest
        if create:
            return target
        if (
            _is_reparse_point(target)
            or not target.is_dir()
            or target.resolve(strict=True).parent
            != package_versions.resolve(strict=True)
        ):
            raise MCPPackageError(
                f"MCP snapshot {package_id!r}@{digest} is missing or unsafe"
            )
        return target

    def _verify_snapshot(self, package_id: str, version: dict[str, Any]) -> Path:
        package, _report = self._verify_snapshot_with_report(package_id, version)
        return package

    def _verify_snapshot_with_report(
        self, package_id: str, version: dict[str, Any]
    ) -> tuple[Path, dict[str, Any]]:
        package = self._snapshot_path(package_id, version["sha256"], create=False)
        if self._version_tree_sha256(package, version) != version["sha256"]:
            raise MCPPackageError(
                f"MCP package {package_id!r} changed after installation"
            )
        report = inspect_mcp_servers(package)
        if report["status"] == "unsupported" or not report.get("description_file"):
            raise MCPPackageError(
                f"MCP package {package_id!r} no longer passes static preflight"
            )
        if self._compatibility_from_report(report) != version["compatibility"]:
            raise MCPPackageError(
                f"MCP package {package_id!r} compatibility record changed"
            )
        names = [server["name"] for server in report["servers"]]
        if names != version["server_names"]:
            raise MCPPackageError(f"MCP package {package_id!r} server identity changed")
        return package, report

    @staticmethod
    def _version_tree_sha256(root: Path, version: dict[str, Any]) -> str:
        if version.get("source_kind") == "git":
            return _git_tree_sha256(root, version["git_modes_sha256"])
        return _tree_sha256(root)

    def _active_server_names(
        self, package_id: str, version: dict[str, Any]
    ) -> list[str]:
        _package, report = self._verify_snapshot_with_report(package_id, version)
        return [server["name"] for server in report["servers"] if server["enabled"]]

    @staticmethod
    def _compatibility_from_report(report: dict[str, Any]) -> dict[str, Any]:
        requirements = list(report["requirements"])
        errors = list(report["errors"])
        for server in report["servers"]:
            requirements.extend(server["requirements"])
            errors.extend(server["errors"])
        return {
            "status": report["status"],
            "requirements": copy.deepcopy(requirements),
            "errors": copy.deepcopy(errors),
        }

    def _read_manifest(self) -> dict[str, Any]:
        self._ensure_scope_root()
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "version": MCP_MANIFEST_VERSION,
                "packages": {},
                "enable_results": {},
            }
        except (OSError, ValueError) as error:
            raise MCPPackageError(
                f"cannot read MCP install manifest: {type(error).__name__}"
            ) from error
        if (
            not isinstance(payload, dict)
            or payload.get("version") != MCP_MANIFEST_VERSION
            or not isinstance(payload.get("packages"), dict)
            or not isinstance(payload.get("enable_results"), dict)
        ):
            raise MCPPackageError("MCP install manifest has an unsupported shape")
        for package_id, record in payload["packages"].items():
            if (
                self._valid_id(package_id)
                and isinstance(record, dict)
                and record.get("type") == "mcp"
                and record.get("scope") == self.scope
                and isinstance(record.get("source"), str)
                and self._valid_version(record.get("current_version"))
                and (
                    record.get("pending_version") is None
                    or self._valid_version(record.get("pending_version"))
                )
                and (
                    record.get("previous_version") is None
                    or self._valid_version(record.get("previous_version"))
                )
            ):
                continue
            raise MCPPackageError(
                f"MCP install manifest record for {package_id!r} is invalid"
            )
        for package_id, selection in payload["enable_results"].items():
            if (
                not self._valid_id(package_id)
                or not isinstance(selection, dict)
                or selection.get("selected_scope") not in {"project", "global"}
                or not isinstance(selection.get("source"), str)
            ):
                raise MCPPackageError(f"MCP selection for {package_id!r} is invalid")
        return payload

    @staticmethod
    def _valid_version(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("source"), str)
            and value.get("source_kind") in {"local", "git"}
            and isinstance(value.get("sha256"), str)
            and bool(_DIGEST_PATTERN.fullmatch(value["sha256"]))
            and value.get("snapshot_id") == value.get("sha256")
            and value.get("trust_status") in {"untrusted", "trusted"}
            and value.get("trust_sha256") == value.get("sha256")
            and isinstance(value.get("compatibility"), dict)
            and value["compatibility"].get("status") in {"complete", "needs-adaptation"}
            and isinstance(value["compatibility"].get("requirements"), list)
            and all(
                isinstance(item, dict)
                for item in value["compatibility"]["requirements"]
            )
            and isinstance(value["compatibility"].get("errors"), list)
            and all(isinstance(item, dict) for item in value["compatibility"]["errors"])
            and isinstance(value.get("server_names"), list)
            and all(isinstance(name, str) for name in value["server_names"])
            and (
                value.get("source_kind") != "git"
                or (
                    isinstance(value.get("source_ref"), str)
                    and isinstance(value.get("resolved_commit"), str)
                    and bool(_COMMIT_PATTERN.fullmatch(value["resolved_commit"]))
                    and isinstance(value.get("git_subdirectory"), str)
                    and isinstance(value.get("git_modes_sha256"), str)
                    and bool(_DIGEST_PATTERN.fullmatch(value["git_modes_sha256"]))
                )
            )
        )

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self._ensure_scope_root()
        data = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.manifest_path.name + ".tmp-", dir=self.scope_root
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

    def _locked_registry(self):
        self._ensure_scope_root()
        return registry_lock(self.manifest_path)

    def _ensure_scope_root(self) -> None:
        try:
            self._skill_manager._ensure_scope_root()
        except SkillPackageError as error:
            raise MCPPackageError(str(error)) from error
        if self.scope == "global" and _is_reparse_point(self.scope_root):
            raise MCPPackageError("global MCP package root is unsafe")
        if self.manifest_path.exists() and _is_reparse_point(self.manifest_path):
            raise MCPPackageError(
                "MCP install manifest cannot be a symbolic link or junction"
            )

    def _ensure_storage_root(self) -> None:
        self._ensure_scope_root()
        if _is_reparse_point(self.package_root):
            raise MCPPackageError(
                "MCP package storage cannot be a symbolic link or junction"
            )
        self.package_root.mkdir(exist_ok=True)
        if self.package_root.resolve(strict=True).parent != self.scope_root.resolve(
            strict=True
        ):
            raise MCPPackageError("MCP package storage escapes its scope root")
        for directory in (self.versions_root,):
            if _is_reparse_point(directory):
                raise MCPPackageError(
                    "MCP snapshot storage cannot be a symbolic link or junction"
                )
            directory.mkdir(exist_ok=True)
            if directory.resolve(strict=True).parent != self.package_root.resolve(
                strict=True
            ):
                raise MCPPackageError("MCP snapshot storage escapes its root")

    def _record_for_scope(self, package_id: str) -> dict[str, Any]:
        with self._locked_registry():
            return copy.deepcopy(self._record(self._read_manifest(), package_id))

    def _record(self, manifest: dict[str, Any], package_id: str) -> dict[str, Any]:
        record = manifest["packages"].get(package_id)
        if record is None:
            raise MCPPackageError(
                f"MCP package {package_id!r} is not installed in {self.scope} scope"
            )
        return record

    def _scope_manager(self, scope: str) -> MCPPackageManager:
        return MCPPackageManager(
            self.workspace_dir,
            scope=scope,
            global_skills_dir=self.global_skills_dir,
        )

    @staticmethod
    def _package_id(value: str) -> str:
        if not MCPPackageManager._valid_id(value):
            raise MCPPackageError(
                "MCP package id must use lowercase letters, digits, and single hyphens"
            )
        return value

    @staticmethod
    def _valid_id(value: Any) -> bool:
        if (
            not isinstance(value, str)
            or not value
            or value.startswith("-")
            or value.endswith("-")
        ):
            return False
        if "--" in value:
            return False
        return all(
            part.isascii() and part.isalnum() and part == part.lower()
            for part in value.split("-")
        )

    @staticmethod
    def _is_runnable_compatibility(compatibility: dict[str, Any]) -> bool:
        if compatibility.get("status") == "complete":
            return not compatibility.get("errors") and not compatibility.get(
                "requirements"
            )
        # Environment references are expanded by MCPServerConfig at Runtime assembly;
        # a static preflight must not read the process environment to decide compatibility.
        requirements = compatibility.get("requirements", [])
        return (
            compatibility.get("status") == "needs-adaptation"
            and bool(requirements)
            and all(
                item.get("kind") == "env-var" and item.get("support") == "manual_review"
                for item in requirements
                if isinstance(item, dict)
            )
            and all(isinstance(item, dict) for item in requirements)
            and not compatibility.get("errors")
        )
