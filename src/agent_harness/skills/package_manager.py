"""Project-local lifecycle for imported Agent Skills directories."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from agent_harness.plugins.mcp_precheck import attach_mcp_section
from agent_harness.skills.inspection import (
    _MAX_PACKAGE_ENTRIES,
    _is_junction,
    inspect_skill_package,
)
from agent_harness.skills.package_lock import MANIFEST_FILENAME
from agent_harness.skills.package_lock import registry_lock as _registry_lock

MANIFEST_VERSION = 1
ENABLE_RESULTS_FIELD = "enable_results"
MANAGED_DIRECTORY_NAME = ".managed"
VERSION_DIRECTORY_NAME = ".versions"
_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SNAPSHOT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})-[0-9a-f]{64}\Z")
_SCP_GIT_URL_PATTERN = re.compile(r"(?:git@)?[A-Za-z0-9.-]+:[^\s:][^\s]*\Z")
_GIT_TIMEOUT_SECONDS = 120


class SkillPackageError(ValueError):
    """A package lifecycle operation cannot safely be completed."""


class SkillManifestError(SkillPackageError):
    """安装清单本身读不出 / 不合形状（区别于某个生命周期动作失败）。

    调用方要分开说这两件事：「选中的版本失效」是用户做过的选择出了问题，
    「清单损坏」是盘上状态出了问题，两者该让用户做的事完全不同。
    """


def _inspected(
    source: str | os.PathLike[str],
    *,
    scope: str,
    existing_skills: Path | str | None = None,
) -> dict[str, Any]:
    """预检一份包，并把 MCP 段折进去——安装/启停闸门读的就是这份报告。

    单点在这里而不是各个调用点：`install` / `install_git` / `update` / 暂存复核 /
    回退复核五处都靠 `status == "complete"` 与 `requirements` 放行，任何一处漏折
    MCP 段，一份 OAuth-only 的 server 描述就能以"完整兼容"进库（ADR-0052 D4、#875 AC4）。
    """
    report = inspect_skill_package(source, scope=scope, existing_skills=existing_skills)
    return attach_mcp_section(report)


class SkillPackageManager:
    """Install immutable Skill snapshots in project or user-global scope."""

    def __init__(
        self,
        workspace_dir: str | os.PathLike[str],
        *,
        scope: str = "project",
        global_skills_dir: str | os.PathLike[str] | None = None,
        additional_skill_directories: list[str | os.PathLike[str]] | None = None,
        additional_skill_paths: list[str | os.PathLike[str]] | None = None,
    ) -> None:
        if scope not in {"project", "global"}:
            raise SkillPackageError("Skill package scope must be project or global")
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.scope = scope
        self.project_skills_dir = self.workspace_dir / "skills"
        self.global_skills_dir = (
            Path(global_skills_dir).expanduser()
            if global_skills_dir is not None
            else Path.home() / ".intelligence-agent" / "skills"
        )
        self.scope_root = (
            self.workspace_dir
            if scope == "project"
            else self.global_skills_dir.parent / "plugin-packages"
        )
        self.scope_skills_dir = (
            self.project_skills_dir
            if scope == "project"
            else self.scope_root / "skills"
        )
        self.managed_skills_dir = self.scope_skills_dir / MANAGED_DIRECTORY_NAME
        self.managed_versions_dir = self.managed_skills_dir / VERSION_DIRECTORY_NAME
        self.manifest_path = self.scope_root / MANIFEST_FILENAME
        self.additional_skill_directories = [
            Path(path).expanduser() for path in (additional_skill_directories or [])
        ]
        self.additional_skill_paths = [
            Path(path).expanduser() for path in (additional_skill_paths or [])
        ]
        self._global_manager_cache: SkillPackageManager | None = None

    def global_manager(self) -> SkillPackageManager:
        """本 manager 所属安装根的全局 scope 视图（同 workspace、同全局目录）。

        启用结果是「本项目选了哪个 scope 的哪一版」——判断同 ID 是否跨 scope 并存
        必须同时读两个安装根（T5 AC2），所以项目 manager 需要一个全局视图。
        """
        if self.scope == "global":
            return self
        if self._global_manager_cache is None:
            self._global_manager_cache = SkillPackageManager(
                self.workspace_dir,
                scope="global",
                global_skills_dir=self.global_skills_dir,
            )
        return self._global_manager_cache

    def global_record(self, name: str) -> dict[str, Any] | None:
        """全局 scope 的安装记录；无则 None。清单损坏时响亮失败，不静默吞掉。"""
        return self.global_manager().list_packages().get(name)

    def enable_results(self) -> dict[str, dict[str, Any]]:
        """本项目的启用结果表副本（只读投影，供 CLI/装配面消费）。

        含读侧从 T4 旧清单派生补齐的条目（`_read_manifest` 回填），所以不等于
        「盘上原样保存的那份」——回填结果在下一次写盘时持久化。

        键是 `enable <id>` 用的 id（安装器保证它等于包名：检查阶段要求 SKILL.md
        的 name 与所在目录名一致）；值记录被选 scope / 来源 / 版本（spec 08 §6.2
        要求安装记录至少能还原「显式项目启用选择」，这条属于项目自己的清单）。
        """
        with self._locked_registry():
            return copy.deepcopy(self._read_manifest()[ENABLE_RESULTS_FIELD])

    def install(self, source: str | os.PathLike[str]) -> dict[str, Any]:
        source_report = _inspected(
            source, scope=self.scope, existing_skills=self.scope_skills_dir
        )
        self._raise_if_uninstallable(source_report)
        name = source_report["name"]
        if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
            raise SkillPackageError("preflight did not return a valid Skill name")
        source_root = Path(source_report["source"])
        return self._install_snapshot(
            source_root,
            name=name,
            source_value=str(source_root),
            initial_report=source_report,
        )

    def _install_snapshot(
        self,
        source_root: Path,
        *,
        name: str,
        source_value: str,
        initial_report: dict[str, Any],
        record_extras: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._raise_if_uninstallable(initial_report)
        if initial_report.get("name") != name:
            raise SkillPackageError("Skill name changed before snapshot installation")
        with self._locked_registry():
            manifest = self._read_manifest()
            packages = manifest["packages"]
            if name in packages:
                raise SkillPackageError(f"Skill {name!r} is already installed")
            self._assert_no_shadow_conflict(name)

            self._ensure_managed_directory()
            target = self.managed_skills_dir / name
            if self._path_exists(target):
                raise SkillPackageError(
                    f"managed path for Skill {name!r} already exists without an install record"
                )

            def tree_digest(root: Path) -> str:
                if record_extras and record_extras.get("source_kind") == "git":
                    return _git_tree_sha256(root, record_extras["git_modes_sha256"])
                return _tree_sha256(root)

            source_digest = tree_digest(source_root)
            staging_root = self.managed_skills_dir / f".staging-{uuid.uuid4().hex}"
            staging_package = staging_root / name
            staging_root.mkdir()
            try:
                shutil.copytree(
                    source_root,
                    staging_package,
                    symlinks=True,
                    copy_function=shutil.copy2,
                    ignore=_reject_junctions,
                )
                if tree_digest(staging_package) != source_digest:
                    raise SkillPackageError(
                        "Skill package changed or could not be copied without following linked directories"
                    )
                report = _inspected(staging_package, scope=self.scope)
                if report.get("name") != name:
                    raise SkillPackageError(
                        "Skill name changed between preflight and package snapshot"
                    )
                self._raise_if_uninstallable(report)
                digest = tree_digest(staging_package)
                record = {
                    "type": "skill",
                    "scope": self.scope,
                    "source": source_value,
                    "sha256": digest,
                    "skill_sha256": _file_sha256(staging_package / "SKILL.md"),
                    "trust": {"status": "untrusted", "sha256": digest},
                    "enabled": False,
                    "installed_at": datetime.now(UTC).isoformat(),
                    "compatibility": {
                        "status": report["status"],
                        "license": report["license"],
                        "requirements": report["requirements"],
                        "errors": report["errors"],
                    },
                }
                if record_extras:
                    record.update(copy.deepcopy(record_extras))
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

    def install_git(
        self,
        url: str,
        *,
        ref: str,
        subdirectory: str,
    ) -> dict[str, Any]:
        safe_url = _validate_git_url(url)
        safe_ref = _validate_git_ref(ref)
        safe_subdirectory = _validate_git_subdirectory(subdirectory)
        with tempfile.TemporaryDirectory(prefix="agent-harness-skill-git-") as temporary:
            package, commit, git_modes_sha256 = _fetch_git_skill(
                safe_url, safe_ref, safe_subdirectory, Path(temporary)
            )
            report = _inspected(
                package, scope=self.scope, existing_skills=self.scope_skills_dir
            )
            self._raise_if_uninstallable(report)
            name = report.get("name")
            if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
                raise SkillPackageError("Git package preflight did not return a valid Skill name")
            return self._install_snapshot(
                package,
                name=name,
                source_value=safe_url,
                initial_report=report,
                record_extras={
                    "source_kind": "git",
                    "git_subdirectory": safe_subdirectory,
                    "source_ref": safe_ref,
                    "resolved_commit": commit,
                    "git_modes_sha256": git_modes_sha256,
                    "previous_version": None,
                    "pending_version": None,
                },
            )

    def update(self, name: str, *, ref: str) -> dict[str, Any]:
        safe_ref = _validate_git_ref(ref)
        with self._locked_registry():
            manifest = self._read_manifest()
            record = self._record(manifest, name)
            if record.get("source_kind") != "git":
                raise SkillPackageError(f"Skill {name!r} is not installed from Git")
            if record.get("pending_version") is not None:
                raise SkillPackageError(f"Skill {name!r} already has a pending version")
            if record["compatibility"]["status"] != "complete":
                raise SkillPackageError(
                    f"Skill {name!r} must have complete compatibility before it can be updated"
                )
            source_url = _validate_git_url(record["source"])
            subdirectory = _validate_git_subdirectory(record["git_subdirectory"])
            current_commit = record["resolved_commit"]

        with tempfile.TemporaryDirectory(prefix="agent-harness-skill-update-") as temporary:
            package, commit, git_modes_sha256 = _fetch_git_skill(
                source_url, safe_ref, subdirectory, Path(temporary)
            )
            report = _inspected(package, scope=self.scope)
            self._raise_if_uninstallable(report)
            if report.get("name") != name:
                raise SkillPackageError(
                    f"Git ref resolves to a different Skill name than {name!r}"
                )
            if report["status"] != "complete":
                raise SkillPackageError(
                    f"Git update preflight is {report['status']!r}; the installed version was kept"
                )
            if commit == current_commit:
                raise SkillPackageError(f"Git ref already resolves to the installed commit for {name!r}")
            digest = _git_tree_sha256(package, git_modes_sha256)
            pending = _version_metadata(
                safe_ref,
                commit,
                digest,
                git_modes_sha256,
                _file_sha256(package / "SKILL.md"),
                report,
            )

            with self._locked_registry():
                manifest = self._read_manifest()
                record = self._record(manifest, name)
                if (
                    record.get("source_kind") != "git"
                    or record["source"] != source_url
                    or record["resolved_commit"] != current_commit
                    or record.get("pending_version") is not None
                ):
                    raise SkillPackageError(
                        f"Git source for Skill {name!r} changed during update; retry"
                    )
                self._store_version_snapshot(package, name, pending)
                record["pending_version"] = pending
                self._write_manifest(manifest)
                return copy.deepcopy(record)

    def rollback(self, name: str) -> dict[str, Any]:
        with self._locked_registry():
            manifest = self._read_manifest()
            record = self._record(manifest, name)
            if record.get("source_kind") != "git":
                raise SkillPackageError(f"Skill {name!r} is not installed from Git")
            if record.get("pending_version") is not None:
                raise SkillPackageError(f"Skill {name!r} already has a pending version")
            previous = record.get("previous_version")
            if previous is None:
                raise SkillPackageError(f"Skill {name!r} has no previous Git snapshot to restore")
            active = self._package_path(name)
            self._verify_package(active, record)
            snapshot = self._version_snapshot_path(name, previous["snapshot_id"])
            if _git_tree_sha256(snapshot, previous["git_modes_sha256"]) != previous["sha256"]:
                raise SkillPackageError(f"previous Git snapshot for Skill {name!r} changed")
            record["pending_version"] = copy.deepcopy(previous)
            self._write_manifest(manifest)
            return copy.deepcopy(record)

    def apply_pending_versions(self) -> int:
        """Apply staged Git snapshots before a new Runtime discovers Skills."""
        try:
            return self._apply_pending_versions()
        except OSError as error:
            raise SkillPackageError(
                "pending Git Skill activation failed; installed versions remain selected"
            ) from error

    def _apply_pending_versions(self) -> int:
        applied = 0
        with self._locked_registry():
            manifest = self._read_manifest()
            for name in sorted(manifest["packages"]):
                record = manifest["packages"][name]
                pending = record.get("pending_version")
                if pending is None:
                    continue
                active = self._package_path(name)
                self._verify_package(active, record)
                snapshot = self._version_snapshot_path(name, pending["snapshot_id"])
                if _git_tree_sha256(snapshot, pending["git_modes_sha256"]) != pending["sha256"]:
                    raise SkillPackageError(f"pending Git snapshot for Skill {name!r} changed")

                current = _current_version_metadata(record)
                self._store_version_snapshot(active, name, current)
                staging_root = self.managed_skills_dir / f".staging-{uuid.uuid4().hex}"
                staged = staging_root / name
                displaced = self.managed_skills_dir / f".replacing-{uuid.uuid4().hex}"
                staging_root.mkdir()
                try:
                    shutil.copytree(
                        snapshot,
                        staged,
                        symlinks=True,
                        copy_function=shutil.copy2,
                        ignore=_reject_junctions,
                    )
                    if _git_tree_sha256(staged, pending["git_modes_sha256"]) != pending["sha256"]:
                        raise SkillPackageError(
                            f"pending Git snapshot for Skill {name!r} changed while staging"
                        )
                    report = _inspected(staged, scope=self.scope)
                    self._raise_if_uninstallable(report)
                    if report.get("name") != name or report["status"] != "complete":
                        raise SkillPackageError(
                            f"pending Git snapshot for Skill {name!r} no longer passes preflight"
                        )
                    os.rename(active, displaced)
                    try:
                        os.rename(staged, active)
                    except BaseException:
                        os.rename(displaced, active)
                        raise

                    record["previous_version"] = current
                    record["source_ref"] = pending["source_ref"]
                    record["resolved_commit"] = pending["resolved_commit"]
                    record["sha256"] = pending["sha256"]
                    record["git_modes_sha256"] = pending["git_modes_sha256"]
                    record["skill_sha256"] = pending["skill_sha256"]
                    record["trust"] = {
                        "status": "untrusted",
                        "sha256": pending["sha256"],
                    }
                    record["compatibility"] = copy.deepcopy(pending["compatibility"])
                    record["pending_version"] = None
                    try:
                        self._write_manifest(manifest)
                    except BaseException:
                        shutil.rmtree(active, ignore_errors=True)
                        try:
                            os.rename(displaced, active)
                        except OSError as error:
                            raise SkillPackageError(
                                f"Skill {name!r} manifest update failed; old snapshot remains at {displaced}"
                            ) from error
                        raise
                    shutil.rmtree(displaced, ignore_errors=True)
                    applied += 1
                finally:
                    if staging_root.exists():
                        shutil.rmtree(staging_root, ignore_errors=True)
        return applied

    def enable(self, name: str, *, scope: str | None = None) -> dict[str, Any]:
        """显式启用本项目对该包的装配（spec 08 §6.2 / ADR-0052 D3）。

        `scope` 是显式选择：同 ID 的项目版与全局版并存且尚无启用结果时必须给出，
        否则拒绝并列出两项来源/版本（T5 AC2）。启用结果一旦落盘就不会被后来的安装
        或升级静默改写（T5 AC3）；被选 scope 失效时报错，不自动切到另一 scope。
        本方法只写启用结果：不触碰 permission mode / tool permission / approval
        策略，包声明的权限不因选择提升（T5 AC4）。
        """
        if scope is not None and scope not in {"project", "global"}:
            raise SkillPackageError(f"unsupported package scope {scope!r}")
        if self.scope == "global":
            raise SkillPackageError(
                "global packages are enabled per project; select them from the project that uses them"
            )
        with self._locked_registry():
            manifest = self._read_manifest()
            results = manifest[ENABLE_RESULTS_FIELD]
            previous = results.get(name, {}).get("selected_scope")
            local = manifest["packages"].get(name)
            global_record = self.global_record(name)
            target = scope
            if target is None:
                if local is not None and global_record is not None:
                    raise SkillPackageError(self._ambiguous_scope_message(name, local, global_record))
                if local is None and global_record is None:
                    raise SkillPackageError(f"Skill {name!r} is not installed")
                target = "project" if local is not None else "global"
            if target == "project":
                if local is None:
                    raise SkillPackageError(
                        f"Skill {name!r} has no project version; refusing to fall back to another scope"
                    )
                self._adopt_project_package(name, local)
                source, version = local["source"], None
            else:
                if global_record is None:
                    raise SkillPackageError(
                        f"Skill {name!r} has no global version; refusing to fall back to another scope"
                    )
                self._verify_selected_global(name, global_record)
                source = global_record["source"]
                version = global_record.get("resolved_commit") or global_record["sha256"]
                if local is not None:
                    # 改选全局版后，项目记录的 enabled 位必须跟着熄灭：它同时喂给
                    # 列表面（saved_selection / pending_restart）与装配面，留着 True
                    # 会让 CLI 把「已改选全局」的那条项目记录报成已启用且待重启。
                    local["enabled"] = False
            results[name] = {
                "selected_scope": target,
                "source": source,
                "version": version,
            }
            self._write_manifest(manifest)
            return {
                "selected_scope": target,
                "source": source,
                "version": version,
                "previous_scope": previous if isinstance(previous, str) else None,
            }

    def _adopt_project_package(self, name: str, record: dict[str, Any]) -> None:
        status = record["compatibility"]["status"]
        if status != "complete":
            raise SkillPackageError(
                f"Skill {name!r} has status {status!r}; only complete packages can be enabled"
            )
        self._assert_no_shadow_conflict(name)
        package = self._package_path(name)
        self._verify_package(package, record)
        record["enabled"] = True

    def _verify_selected_global(self, name: str, record: dict[str, Any]) -> None:
        """被选的全局版本必须可装配；失效就报错，不 shadow（T5 AC3）。"""
        status = record["compatibility"]["status"]
        if status != "complete":
            raise SkillPackageError(
                f"global Skill {name!r} has status {status!r}; only complete packages can be enabled"
            )
        manager = self.global_manager()
        package = manager._package_path(name)
        manager._verify_package(package, record)

    def _ambiguous_scope_message(
        self, name: str, local: dict[str, Any], global_record: dict[str, Any]
    ) -> str:
        return (
            f"Skill {name!r} exists in both project and global scope; "
            f"project {_source_label(local)} / global {_source_label(global_record)}. "
            f"Choose one explicitly with --scope project or --scope global"
        )

    def disable(self, name: str) -> dict[str, Any]:
        """清掉本项目对该包的启用结果（不动另一 scope 的记录，不静默切换）。"""
        if self.scope == "global":
            raise SkillPackageError(
                "global packages are disabled per project; clear the selection from that project"
            )
        with self._locked_registry():
            manifest = self._read_manifest()
            results = manifest[ENABLE_RESULTS_FIELD]
            previous = results.get(name, {}).get("selected_scope")
            record = manifest["packages"].get(name)
            if record is not None:
                record["enabled"] = False
            results.pop(name, None)
            self._write_manifest(manifest)
            return {"selected_scope": previous if isinstance(previous, str) else None}

    def remove(self, name: str) -> None:
        with self._locked_registry():
            manifest = self._read_manifest()
            self._record(manifest, name)
            if manifest[ENABLE_RESULTS_FIELD].get(name, {}).get("selected_scope") == "global":
                raise SkillPackageError(
                    f"Skill {name!r} is selected from global scope; remove the global copy instead"
                )
            manifest[ENABLE_RESULTS_FIELD].pop(name, None)
            record = manifest["packages"][name]
            package = self._package_path(name)
            self._verify_package(package, record)

            tombstone = self.managed_skills_dir / f".removing-{uuid.uuid4().hex}"
            os.rename(package, tombstone)
            try:
                unchanged = _managed_package_sha256(tombstone, record) == record["sha256"]
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
            if record.get("source_kind") == "git":
                self._remove_version_snapshots(name)

    def _remove_version_snapshots(self, name: str) -> None:
        if not self._path_exists(self.managed_versions_dir):
            return
        skill_versions = self._version_directory(name, create=False)
        for snapshot in skill_versions.iterdir():
            match = _SNAPSHOT_PATTERN.fullmatch(snapshot.name)
            if match is None:
                continue
            expected_digest = snapshot.name.rsplit("-", 1)[1]
            if (
                _is_reparse_point(snapshot)
                or not snapshot.is_dir()
                or snapshot.resolve(strict=True).parent != skill_versions.resolve(strict=True)
                or _tree_sha256(snapshot) != expected_digest
            ):
                raise SkillPackageError(
                    f"Skill {name!r} version snapshot changed; retained at {snapshot}"
                )
            try:
                shutil.rmtree(snapshot)
            except OSError as error:
                raise SkillPackageError(
                    f"Skill {name!r} was unregistered, but version cleanup remains at {snapshot}"
                ) from error
        with suppress(OSError):
            skill_versions.rmdir()
        with suppress(OSError):
            self.managed_versions_dir.rmdir()

    def list_packages(self) -> dict[str, dict[str, Any]]:
        with self._locked_registry():
            return copy.deepcopy(self._read_manifest()["packages"])

    def enabled_skill_digests(self) -> dict[str, str]:
        """本项目从 project scope 装配的 Skill 与安装期正文摘要。

        两个条件都要满足：启用结果表说「本项目选了 project scope」，且项目记录
        仍是 `enabled`。前者决定选哪一版（T5 AC2：同 ID 只装配所选一版），后者是
        安装记录的既有门。选择了 global 的同名包由
        :meth:`enabled_global_skill_digests` 承担，两处互斥。
        """
        if self.scope == "global":
            return {}
        with self._locked_registry():
            manifest = self._read_manifest()
            results = manifest[ENABLE_RESULTS_FIELD]
            enabled: dict[str, str] = {}
            for name, record in manifest["packages"].items():
                if results.get(name, {}).get("selected_scope") != "project":
                    continue
                if not record["enabled"] or record["compatibility"]["status"] != "complete":
                    continue
                self._assert_no_shadow_conflict(name)
                package = self.managed_skills_dir / name
                if not self._is_managed_package(package):
                    raise SkillPackageError(f"enabled Skill {name!r} is missing or unsafe")
                self._verify_package(package, record)
                enabled[name] = record["skill_sha256"]
            return enabled

    def enabled_skill_names(self) -> set[str]:
        """Return selected Skills after validating their installed snapshots."""
        return set(self.enabled_skill_digests())

    def enabled_global_skill_digests(self) -> dict[str, str]:
        """本项目显式选择 global scope 的 Skill 与安装期正文摘要。

        被选版本失效（记录被移除、快照被改、状态不再是 complete）时报错，
        **不**回落到项目版（T5 AC3：不自动 shadow）。其他项目的选择不进本清单
        （启用结果写在各项目自己的清单里）。
        """
        selected = self.global_selections()
        manager = self.global_manager()
        records = manager.list_packages()
        digests: dict[str, str] = {}
        for name in sorted(selected):
            record = records.get(name)
            if record is None:
                raise SkillPackageError(
                    f"Skill {name!r} is selected from global scope but that record no longer exists"
                )
            if record["compatibility"]["status"] != "complete":
                raise SkillPackageError(
                    f"global Skill {name!r} has status "
                    f"{record['compatibility']['status']!r}; only complete packages can be enabled"
                )
            package = manager.managed_skills_dir / name
            if not manager._is_managed_package(package):
                raise SkillPackageError(f"selected global Skill {name!r} is missing or unsafe")
            manager._verify_package(package, record)
            digests[name] = record["skill_sha256"]
        return digests

    def global_selections(self) -> dict[str, dict[str, Any]]:
        """本项目显式选择 global scope 的启用结果（只读投影）。

        键就是 `enable <id>` 的 id，等于该 scope 安装记录里的包名。
        """
        return {
            name: dict(result)
            for name, result in self.enable_results().items()
            if result.get("selected_scope") == "global"
        }

    def _read_manifest(self) -> dict[str, Any]:
        if self.scope == "global" and self._path_exists(self.scope_root) and (
            _is_reparse_point(self.scope_root)
            or not self.scope_root.is_dir()
            or self.scope_root.resolve(strict=True).parent
            != self.scope_root.parent.resolve(strict=True)
        ):
            raise SkillManifestError("global package storage directory is unsafe")
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": MANIFEST_VERSION, "packages": {}, ENABLE_RESULTS_FIELD: {}}
        except (OSError, ValueError) as error:
            raise SkillManifestError(f"cannot read install manifest: {type(error).__name__}") from error
        if (
            not isinstance(payload, dict)
            or payload.get("version") != MANIFEST_VERSION
            or not isinstance(payload.get("packages"), dict)
        ):
            raise SkillManifestError("install manifest has an unsupported shape")
        results = payload.setdefault(ENABLE_RESULTS_FIELD, {})
        if not isinstance(results, dict):
            raise SkillManifestError("install manifest has an unsupported shape")
        # T4 时代的清单只有 enabled 位、没有启用结果表。升级后按「显式项目选择」
        # 补齐，否则已启用的项目包会在读侧静默消失（AC1 要求会话间一致）。
        # 这是版本 1 内的字段演进，不动 MANIFEST_VERSION。补齐落在 payload 上，
        # 因此**下一次任何写盘都会把它持久化**——这正是我们要的升级迁移（幂等，
        # 回填值等于 T4 的 enabled 语义），不是只存在于本次读的临时视图。
        for legacy_name, legacy_record in payload["packages"].items():
            if (
                isinstance(legacy_record, dict)
                and legacy_record.get("enabled") is True
                and isinstance(legacy_record.get("source"), str)
                and legacy_name not in results
            ):
                results[legacy_name] = {
                    "selected_scope": "project",
                    "source": legacy_record["source"],
                    "version": None,
                }
        for name, result in results.items():
            if (
                not isinstance(name, str)
                or not _NAME_PATTERN.fullmatch(name)
                or not isinstance(result, dict)
                or not isinstance(result.get("selected_scope"), str)
                or result.get("selected_scope") not in {"project", "global"}
                or not isinstance(result.get("source"), str)
                or not isinstance(result.get("version"), (str, type(None)))
            ):
                raise SkillManifestError(f"enable result for {name!r} is invalid")
            if result["selected_scope"] == "project" and name not in payload["packages"]:
                raise SkillManifestError(
                    f"enable result for {name!r} names a missing project package"
                )
        for name, record in payload["packages"].items():
            if (
                not isinstance(name, str)
                or not _NAME_PATTERN.fullmatch(name)
                or not isinstance(record, dict)
                or record.get("type") != "skill"
                or record.get("scope") != self.scope
                or not isinstance(record.get("source"), str)
                or not isinstance(record.get("sha256"), str)
                or not _DIGEST_PATTERN.fullmatch(record["sha256"])
                or not isinstance(record.get("skill_sha256"), str)
                or not _DIGEST_PATTERN.fullmatch(record["skill_sha256"])
                or not isinstance(record.get("trust"), dict)
                or record["trust"].get("status") != "untrusted"
                or record["trust"].get("sha256") != record.get("sha256")
                or not isinstance(record.get("enabled"), bool)
                or (self.scope == "global" and record.get("enabled") is not False)
                or not isinstance(record.get("compatibility"), dict)
                or record["compatibility"].get("status") not in {"complete", "needs-adaptation"}
            ):
                raise SkillManifestError(f"install manifest record for {name!r} is invalid")
            _validate_git_manifest_record(name, record)
        return payload

    @contextmanager
    def _locked_registry(self) -> Iterator[None]:
        self._ensure_scope_root()
        with _registry_lock(self.manifest_path):
            if self.scope == "global" and _is_reparse_point(self.scope_root):
                raise SkillManifestError("global package storage directory is unsafe")
            yield

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self._ensure_scope_root()
        data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
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
        if _managed_package_sha256(package, record) != record["sha256"]:
            raise SkillPackageError(
                f"Skill {package.name!r} changed after installation; refusing this operation"
            )

    def _version_directory(self, name: str, *, create: bool) -> Path:
        if not _NAME_PATTERN.fullmatch(name):
            raise SkillPackageError(f"invalid Skill name {name!r}")
        self._ensure_managed_directory()
        versions = self.managed_versions_dir
        if _is_reparse_point(versions):
            raise SkillPackageError("managed Skill versions directory is unsafe")
        if create:
            versions.mkdir(exist_ok=True)
        elif not versions.is_dir():
            raise SkillPackageError("managed Skill versions directory is missing")
        if versions.resolve(strict=True).parent != self.managed_skills_dir.resolve(strict=True):
            raise SkillPackageError("managed Skill versions directory resolves outside its root")
        skill_versions = versions / name
        if _is_reparse_point(skill_versions):
            raise SkillPackageError(f"version history for Skill {name!r} is unsafe")
        if create:
            skill_versions.mkdir(exist_ok=True)
        elif not skill_versions.is_dir():
            raise SkillPackageError(f"version history for Skill {name!r} is missing")
        if skill_versions.resolve(strict=True).parent != versions.resolve(strict=True):
            raise SkillPackageError(f"version history for Skill {name!r} resolves outside its root")
        return skill_versions

    def _store_version_snapshot(
        self, source: Path, name: str, version: dict[str, Any]
    ) -> Path:
        digest = version["sha256"]
        snapshot_id = version["snapshot_id"]
        if (
            not _DIGEST_PATTERN.fullmatch(digest)
            or not _SNAPSHOT_PATTERN.fullmatch(snapshot_id)
            or snapshot_id != f"{version['resolved_commit']}-{digest}"
        ):
            raise SkillPackageError(f"version metadata for Skill {name!r} is invalid")
        if _git_tree_sha256(source, version["git_modes_sha256"]) != digest:
            raise SkillPackageError(f"source snapshot for Skill {name!r} changed")
        target = self._version_directory(name, create=True) / snapshot_id
        if self._path_exists(target):
            if (
                _is_reparse_point(target)
                or not target.is_dir()
                or _git_tree_sha256(target, version["git_modes_sha256"]) != digest
            ):
                raise SkillPackageError(f"stored Git snapshot for Skill {name!r} is unsafe")
            return target
        try:
            shutil.copytree(
                source,
                target,
                symlinks=True,
                copy_function=shutil.copy2,
                ignore=_reject_junctions,
            )
            if _git_tree_sha256(target, version["git_modes_sha256"]) != digest:
                raise SkillPackageError(f"Git snapshot for Skill {name!r} changed while storing")
            return target
        except BaseException:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise

    def _version_snapshot_path(self, name: str, snapshot_id: str) -> Path:
        if not _SNAPSHOT_PATTERN.fullmatch(snapshot_id):
            raise SkillPackageError(f"version snapshot id for Skill {name!r} is invalid")
        target = self._version_directory(name, create=False) / snapshot_id
        if (
            _is_reparse_point(target)
            or not target.is_dir()
            or target.resolve(strict=True).parent
            != self._version_directory(name, create=False).resolve(strict=True)
        ):
            raise SkillPackageError(f"version snapshot for Skill {name!r} is missing or unsafe")
        return target

    def _assert_no_shadow_conflict(self, name: str) -> None:
        if self._path_exists(self.scope_skills_dir / name):
            raise SkillPackageError(
                f"{self.scope} Skill {name!r} already exists; refusing a shadow conflict"
            )
        if self._path_exists(self.global_skills_dir / name):
            raise SkillPackageError(
                f"global Skill {name!r} already exists; refusing a shadow conflict"
            )
        if self.scope == "global":
            return
        from agent_harness.skills.discovery import SkillDiscovery

        catalog = SkillDiscovery(
            [
                self.project_skills_dir,
                self.global_skills_dir,
                *self.additional_skill_directories,
            ],
            manual_paths=self.additional_skill_paths,
            # 本 scope 的托管根里的内容不算「外部同名冲突」：它们是本安装根自己
            # 的快照，由启用结果决定是否装配（不传 enabled digests ⇒ 全不可见）。
            managed_directories={"project": self.managed_skills_dir},
        ).discover()
        source = next((entry.source_path for entry in catalog.entries if entry.name == name), None)
        if source is not None:
            source_resolved = source.resolve()
            try:
                source_resolved.relative_to(self.project_skills_dir.resolve())
                source_kind = "project"
            except ValueError:
                try:
                    source_resolved.relative_to(self.global_skills_dir.resolve())
                    source_kind = "global"
                except ValueError:
                    source_kind = "configured"
            raise SkillPackageError(
                f"{source_kind} Skill {name!r} already exists at {source}; refusing a shadow conflict"
            )

    def _is_managed_package(self, package: Path) -> bool:
        if (
            _is_reparse_point(self.scope_skills_dir)
            or _is_reparse_point(self.managed_skills_dir)
            or _is_reparse_point(package)
            or not package.is_dir()
        ):
            return False
        if self.scope == "global" and _is_reparse_point(self.scope_root):
            return False
        try:
            storage_root = self.scope_root.resolve(strict=True)
            skills_root = self.scope_skills_dir.resolve(strict=True)
            root = self.managed_skills_dir.resolve(strict=True)
            resolved = package.resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return skills_root.parent == storage_root and root.parent == skills_root and resolved.parent == root

    def _ensure_managed_directory(self) -> None:
        self._ensure_scope_root()
        if _is_reparse_point(self.scope_skills_dir):
            raise SkillPackageError(
                f"{self.scope} package Skills directory cannot be a symbolic link or junction"
            )
        self.scope_skills_dir.mkdir(exist_ok=True)
        if self.scope_skills_dir.resolve(strict=True).parent != self.scope_root.resolve(strict=True):
            raise SkillPackageError(f"{self.scope} package Skills directory resolves outside its root")
        if _is_reparse_point(self.managed_skills_dir):
            raise SkillPackageError("managed Skills directory cannot be a symbolic link or junction")
        self.managed_skills_dir.mkdir(exist_ok=True)
        if self.managed_skills_dir.resolve(strict=True).parent != self.scope_skills_dir.resolve(strict=True):
            raise SkillPackageError(f"managed Skills directory resolves outside the {self.scope} Skills directory")

    def _ensure_scope_root(self) -> None:
        if self.scope == "project":
            self.scope_root.mkdir(parents=True, exist_ok=True)
            return
        parent = self.scope_root.parent
        parent.mkdir(parents=True, exist_ok=True)
        if self._path_exists(self.scope_root) and _is_reparse_point(self.scope_root):
            raise SkillPackageError("global package storage directory cannot be a symbolic link or junction")
        self.scope_root.mkdir(exist_ok=True)
        if self.scope_root.resolve(strict=True).parent != parent.resolve(strict=True):
            raise SkillPackageError("global package storage directory resolves outside its parent")

    def _path_exists(self, path: Path) -> bool:
        return path.exists() or path.is_symlink() or _is_reparse_point(path)

    def _raise_if_uninstallable(self, report: dict[str, Any]) -> None:
        if report["status"] != "unsupported":
            return
        if any(error.get("code") == "NAME_CONFLICT" for error in report["errors"]):
            name = report.get("name")
            raise SkillPackageError(f"{self.scope} Skill {name!r} already exists")
        codes = ", ".join(error.get("code", "unknown") for error in report["errors"])
        raise SkillPackageError(f"Skill preflight rejected the package: {codes or 'unsupported'}")


def _source_label(record: dict[str, Any]) -> str:
    """来源/版本标签：Git 记录带 commit，其余退回内容摘要。"""
    version = record.get("resolved_commit") or record.get("sha256", "")
    return f"{record.get('source')}@{version}"


def _validate_git_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SkillPackageError("Git URL must be a non-empty URL without surrounding whitespace")
    if any(ord(character) < 0x20 for character in value) or "\\" in value:
        raise SkillPackageError("Git URL contains an invalid character")
    if PureWindowsPath(value).drive:
        raise SkillPackageError("local filesystem paths are not Git URLs; use a file:// URL")
    if "://" not in value and _SCP_GIT_URL_PATTERN.fullmatch(value):
        if value.startswith("-"):
            raise SkillPackageError("Git URL is invalid")
        return value
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
    except ValueError as error:
        raise SkillPackageError("Git URL is malformed") from error
    if scheme not in {"https", "ssh", "git", "file"}:
        raise SkillPackageError("Git URL must use https, ssh, git, or file")
    if parsed.query or parsed.fragment:
        raise SkillPackageError("Git URL query strings and fragments are not accepted")
    if (parsed.username or parsed.password) and (
        scheme != "ssh" or parsed.username != "git" or parsed.password
    ):
        raise SkillPackageError(
            "credential-bearing Git URLs are not accepted; configure a Git credential helper"
        )
    if scheme == "file":
        if parsed.netloc not in {"", "localhost"} or not parsed.path:
            raise SkillPackageError("file:// Git URL must point to a local repository")
    elif not hostname or not parsed.path:
        raise SkillPackageError("Git URL must include a host and repository path")
    return value


def _validate_git_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1024
        or value.startswith("-")
        or any(ord(character) < 0x20 for character in value)
    ):
        raise SkillPackageError("Git ref is invalid")
    if _COMMIT_PATTERN.fullmatch(value):
        return value
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--allow-onelevel", value],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SkillPackageError("could not validate Git ref") from error
    if result.returncode != 0:
        raise SkillPackageError("Git ref is invalid")
    return value


def _validate_git_subdirectory(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        raise SkillPackageError("Skill subdirectory is invalid")
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
        raise SkillPackageError("Skill subdirectory must be a normalized relative path")
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
        raise SkillPackageError("Git package contains an unsafe path")
    return value


def _validate_symlink_target(link_path: str, target: str) -> str:
    if not target or "\\" in target or ":" in target or PurePosixPath(target).is_absolute():
        raise SkillPackageError("Git package contains an unsafe symbolic link")
    if PureWindowsPath(target).drive:
        raise SkillPackageError("Git package contains an unsafe symbolic link")
    parts = list(PurePosixPath(link_path).parent.parts)
    for part in PurePosixPath(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise SkillPackageError("Git package symbolic link escapes its Skill directory")
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
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise SkillPackageError(f"Git {operation} timed out") from error
    except OSError as error:
        raise SkillPackageError(f"Git {operation} could not start") from error
    if result.returncode != 0:
        raise SkillPackageError(f"Git {operation} failed (exit {result.returncode})")
    if stdout == subprocess.PIPE:
        return result.stdout
    return b""


def _fetch_git_skill(
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
    if not _COMMIT_PATTERN.fullmatch(commit):
        raise SkillPackageError("Git ref did not resolve to a commit")
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
    if not _COMMIT_PATTERN.fullmatch(tree_oid):
        raise SkillPackageError("Git Skill subdirectory did not resolve to a tree")
    object_type = _run_git(
        [f"--git-dir={repo}", "cat-file", "-t", tree_oid],
        cwd=temporary_root,
        env=environment,
        operation="Skill tree verification",
    ).decode("ascii", errors="strict").strip()
    if object_type != "tree":
        raise SkillPackageError("Git Skill subdirectory is not a directory")
    object_format = _run_git(
        [f"--git-dir={repo}", "rev-parse", "--show-object-format"],
        cwd=temporary_root,
        env=environment,
        operation="object format detection",
    ).decode("ascii", errors="strict").strip()
    if object_format not in {"sha1", "sha256"}:
        raise SkillPackageError("Git repository uses an unsupported object format")
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
            raise SkillPackageError("Git package has an invalid tree entry") from error
        if path in entries:
            raise SkillPackageError("Git package contains duplicate paths")
        if mode not in {"100644", "100755", "120000"} or object_type != "blob":
            raise SkillPackageError("Git Skill submodules and special entries are unsupported")
        if not re.fullmatch(r"[0-9a-f]{40}" if object_format == "sha1" else r"[0-9a-f]{64}", object_id):
            raise SkillPackageError("Git package contains an invalid object id")
        entries[path] = (mode, object_id)
    if not entries:
        raise SkillPackageError("Git Skill subdirectory contains no tracked files")
    if len(entries) > _MAX_PACKAGE_ENTRIES:
        raise SkillPackageError("Git Skill package exceeds the supported file count")

    expected_directories: set[str] = set()
    for path in entries:
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts) + 1):
            component_path = "/".join(parts[:index])
            folded = component_path.casefold()
            previous_path = folded_paths.get(folded)
            if previous_path is not None and previous_path != component_path:
                raise SkillPackageError("Git package contains paths that collide on Windows")
            folded_paths[folded] = component_path
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if parent in entries:
                raise SkillPackageError("Git package path conflicts with a file")
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
                raise SkillPackageError("Git archive contains a path outside the Skill directory")
            path = _validate_git_tree_path(member_name[len("package/") :])
            if member.isdir():
                if path not in expected_directories:
                    raise SkillPackageError("Git archive contains an unexpected directory")
                continue
            if path in members_by_path:
                raise SkillPackageError("Git archive contains duplicate paths")
            expected = entries.get(path)
            if expected is None:
                raise SkillPackageError("Git archive contains an untracked path")
            if member.isfile() and expected[0] in {"100644", "100755"}:
                if bool(member.mode & 0o111) != (expected[0] == "100755"):
                    raise SkillPackageError("Git archive file mode does not match the pinned commit")
            elif member.issym() and expected[0] == "120000":
                _validate_symlink_target(path, member.linkname)
            else:
                raise SkillPackageError("Git archive contains an unsupported filesystem entry")
            members_by_path[path] = member
        if members_by_path.keys() != entries.keys():
            raise SkillPackageError("Git archive omitted tracked Skill files")

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
                raise SkillPackageError("Git package path escaped its extraction directory")
            digest = hashlib.new(object_format)
            digest.update(f"blob {member.size}\0".encode("ascii"))
            content = archive.extractfile(member)
            if content is None:
                raise SkillPackageError("Git archive contains an unreadable file")
            with content, target.open("xb") as output:
                while chunk := content.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != entries[path][1]:
                raise SkillPackageError("Git archive content does not match the pinned commit")
            os.chmod(target, 0o755 if entries[path][0] == "100755" else 0o644)

        for path, member in members_by_path.items():
            if not member.issym():
                continue
            target = package.joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not _path_is_within(target.parent.resolve(strict=True), resolved_package):
                raise SkillPackageError("Git package symbolic link escaped its extraction directory")
            link_bytes = member.linkname.encode("utf-8", errors="strict")
            digest = hashlib.new(object_format)
            digest.update(f"blob {len(link_bytes)}\0".encode("ascii"))
            digest.update(link_bytes)
            if digest.hexdigest() != entries[path][1]:
                raise SkillPackageError("Git archive symbolic link does not match the pinned commit")
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


def _version_metadata(
    source_ref: str,
    resolved_commit: str,
    sha256: str,
    git_modes_sha256: str,
    skill_sha256: str,
    report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source_ref": source_ref,
        "resolved_commit": resolved_commit,
        "snapshot_id": f"{resolved_commit}-{sha256}",
        "sha256": sha256,
        "git_modes_sha256": git_modes_sha256,
        "skill_sha256": skill_sha256,
        "compatibility": {
            "status": report["status"],
            "license": report["license"],
            "requirements": report["requirements"],
            "errors": report["errors"],
        },
    }


def _validate_git_manifest_record(name: str, record: dict[str, Any]) -> None:
    git_fields = {
        "git_subdirectory",
        "source_ref",
        "resolved_commit",
        "git_modes_sha256",
        "previous_version",
        "pending_version",
    }
    source_kind = record.get("source_kind")
    if source_kind is None:
        if git_fields.intersection(record):
            raise SkillManifestError(f"install manifest record for {name!r} is invalid")
        return
    if source_kind != "git":
        raise SkillManifestError(f"install manifest record for {name!r} is invalid")

    try:
        _validate_git_url(record["source"])
        subdirectory = _validate_git_subdirectory(record["git_subdirectory"])
        if PurePosixPath(subdirectory).name != name:
            raise SkillPackageError("Skill subdirectory does not match package name")
        _validate_git_ref(record["source_ref"])
        current = {
            "source_ref": record["source_ref"],
            "resolved_commit": record["resolved_commit"],
            "snapshot_id": f"{record['resolved_commit']}-{record['sha256']}",
            "sha256": record["sha256"],
            "git_modes_sha256": record["git_modes_sha256"],
            "skill_sha256": record["skill_sha256"],
            "compatibility": record["compatibility"],
        }
        _validate_version_metadata(current, allow_needs_adaptation=True)
        for key in ("previous_version", "pending_version"):
            version = record.get(key)
            if version is not None:
                _validate_version_metadata(version, allow_needs_adaptation=False)
    except (KeyError, SkillPackageError) as error:
        raise SkillManifestError(f"install manifest record for {name!r} is invalid") from error


def _validate_version_metadata(
    version: Any, *, allow_needs_adaptation: bool
) -> None:
    required = {
        "source_ref",
        "resolved_commit",
        "snapshot_id",
        "sha256",
        "git_modes_sha256",
        "skill_sha256",
        "compatibility",
    }
    if not isinstance(version, dict) or set(version) != required:
        raise SkillPackageError("version metadata has an invalid shape")
    source_ref = version["source_ref"]
    commit = version["resolved_commit"]
    digest = version["sha256"]
    modes_digest = version["git_modes_sha256"]
    skill_digest = version["skill_sha256"]
    compatibility = version["compatibility"]
    if (
        not isinstance(commit, str)
        or not _COMMIT_PATTERN.fullmatch(commit)
        or not isinstance(digest, str)
        or not _DIGEST_PATTERN.fullmatch(digest)
        or not isinstance(modes_digest, str)
        or not _DIGEST_PATTERN.fullmatch(modes_digest)
        or not isinstance(skill_digest, str)
        or not _DIGEST_PATTERN.fullmatch(skill_digest)
        or version["snapshot_id"] != f"{commit}-{digest}"
    ):
        raise SkillPackageError("version metadata does not match its snapshot")
    _validate_git_ref(source_ref)
    if (
        not isinstance(compatibility, dict)
        or compatibility.get("status") not in {"complete", "needs-adaptation"}
        or (
            not allow_needs_adaptation
            and compatibility.get("status") != "complete"
        )
        or not isinstance(compatibility.get("requirements"), list)
        or not isinstance(compatibility.get("errors"), list)
        or (
            compatibility.get("license") is not None
            and not isinstance(compatibility.get("license"), str)
        )
    ):
        raise SkillPackageError("version compatibility metadata is invalid")


def _current_version_metadata(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_ref": record["source_ref"],
        "resolved_commit": record["resolved_commit"],
        "snapshot_id": f"{record['resolved_commit']}-{record['sha256']}",
        "sha256": record["sha256"],
        "git_modes_sha256": record["git_modes_sha256"],
        "skill_sha256": record["skill_sha256"],
        "compatibility": copy.deepcopy(record["compatibility"]),
    }


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


def _reject_junctions(directory: str, names: list[str]) -> list[str]:
    junctions = [name for name in names if _is_junction(Path(directory) / name)]
    if junctions:
        joined = ", ".join(sorted(junctions))
        raise SkillPackageError(
            f"Skill package contains Windows junctions that cannot be copied safely: {joined}"
        )
    return []


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


def _managed_package_sha256(root: Path, record: dict[str, Any]) -> str:
    if record.get("source_kind") == "git":
        return _git_tree_sha256(root, record["git_modes_sha256"])
    return _tree_sha256(root)


def _git_tree_sha256(root: Path, expected_modes: str) -> str:
    """Validate the pinned Git modes while retaining the package content digest."""
    if not _DIGEST_PATTERN.fullmatch(expected_modes):
        raise SkillPackageError("Git Skill file-mode digest is invalid")
    if os.name != "nt" and _git_modes_sha256_from_package(root) != expected_modes:
        raise SkillPackageError(f"Git Skill file modes changed after installation: {root}")
    return _tree_sha256(root)


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
            if _is_reparse_point(path):
                modes[path.relative_to(root).as_posix()] = "120000"
                continue
            if stat.S_ISDIR(info.st_mode):
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                mode = "100755" if info.st_mode & 0o111 else "100644"
                modes[path.relative_to(root).as_posix()] = mode

    visit(root)
    return _git_modes_sha256(modes)
