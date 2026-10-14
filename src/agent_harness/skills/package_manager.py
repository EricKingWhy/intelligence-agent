"""Project-local lifecycle for imported Agent Skills directories."""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from agent_harness import package_source
from agent_harness.mcp_precheck import attach_mcp_section
from agent_harness.plugin_inspection import (
    DSH_SLIDER_PACKAGE_NAME,
    DSH_SLIDER_PINNED_COMMIT,
    DSH_SLIDER_PINNED_FILES_SHA256,
    DSH_SLIDER_PINNED_VERSION,
    inspect_dsh_effort_slider,
)
from agent_harness.skills.inspection import inspect_skill_package
from agent_harness.skills.package_lock import MANIFEST_FILENAME
from agent_harness.skills.package_lock import registry_lock as _registry_lock

MANIFEST_VERSION = 1
ENABLE_RESULTS_FIELD = "enable_results"
MANAGED_DIRECTORY_NAME = ".managed"
VERSION_DIRECTORY_NAME = ".versions"
_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_DIGEST_PATTERN = package_source.DIGEST_PATTERN
_COMMIT_PATTERN = package_source.COMMIT_PATTERN
_SNAPSHOT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})-[0-9a-f]{64}\Z")


class SkillPackageError(ValueError):
    """A package lifecycle operation cannot safely be completed."""


class SkillManifestError(SkillPackageError):
    """安装清单本身读不出 / 不合形状（区别于某个生命周期动作失败）。

    调用方要分开说这两件事：「选中的版本失效」是用户做过的选择出了问题，
    「清单损坏」是盘上状态出了问题，两者该让用户做的事完全不同。
    """


def _skill_source(func: Any) -> Any:
    """把中性来源原语的 `PackageSourceError` 在 Skill 边界翻译成 `SkillPackageError`。

    共用实现住在 `agent_harness.package_source`（issue #1083）；这里只做错误类型
    适配，行为（消息、摘要算法、校验时机）与迁移前逐字一致。
    """

    @functools.wraps(func)
    def boundary(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except package_source.PackageSourceError as error:
            raise SkillPackageError(str(error)) from error

    return boundary


# 共用来源/快照原语（原样实现见 `agent_harness.package_source`）。不抛错的直接绑定，
# 会抛 `PackageSourceError` 的走 `_skill_source` 翻译，保持既有 `SkillPackageError` 契约。
_is_reparse_point = package_source.is_reparse_point
_validate_git_url = _skill_source(package_source.validate_git_url)
_validate_git_ref = _skill_source(package_source.validate_git_ref)
_validate_git_subdirectory = _skill_source(package_source.validate_git_subdirectory)
_fetch_git_skill = _skill_source(package_source.fetch_git_source)
_tree_sha256 = _skill_source(package_source.tree_sha256)
_git_tree_sha256 = _skill_source(package_source.git_tree_sha256)
_reject_junctions = _skill_source(package_source.reject_junctions)


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

        # DSH 原生包识别复用 T8 的 inspect：识别成功就走 DSH 车道，不进 Skill 预检。
        dsh_report = inspect_dsh_effort_slider(source, scope=self.scope)
        if dsh_report is not None:
            # 记录存识别器解析后的绝对路径（expanduser + resolve），与
            # skill 车道的 source_value=str(source_root) 同源：直接对用户
            # 原始输入 resolve() 会把 "~/x" 展开成 "<cwd>/~/x"（Path.resolve
            # 不做 expanduser），写进记录的 source 就不可还原了（C-P2）。
            source_root = Path(dsh_report["source"])
            return self._install_dsh_snapshot(
                source_root,
                report=dsh_report,
                source_value=str(source_root),
            )
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
        """Skill 快照安装（staging + 树摘要 + 原子改名 + 清单回滚）。

        与 :meth:`_install_dsh_snapshot` 有意保持同构、不抽公共脚手架：两者
        的预检函数、门槛判据、记录字段集各不相同，参数化需要 5-6 个钩子，
        抽象成本高于重复本身。单边修 bug（尤其 manifest 写失败的恢复路径）
        必须同步检查另一处。
        """
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

    def _install_dsh_snapshot(
        self,
        source_root: Path,
        *,
        report: dict[str, Any],
        source_value: str,
        record_extras: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """DSH 原生包的不可变快照安装（#878 T9）。

        与 :meth:`_install_snapshot` 同构（staging + 树摘要 + 原子改名 +
        清单回滚），两处有意不抽公共脚手架（理由见对方 docstring）；单边修
        bug（尤其 manifest 写失败恢复路径）必须同步另一处。预检/复核都走
        :func:`inspect_dsh_effort_slider`，且门槛更严：pin 文件摘要不匹配
        （drift）一律拒装。包内 JS 只被哈希/复制，从不执行。
        """

        self._raise_if_dsh_drift(report, "preflight")
        name = report["package"]
        with self._locked_registry():
            manifest = self._read_manifest()
            packages = manifest["packages"]
            if name in packages:
                raise SkillPackageError(f"DSH package {name!r} is already installed")
            self._assert_no_shadow_conflict(name)

            self._ensure_managed_directory()
            target = self.managed_skills_dir / name
            if self._path_exists(target):
                raise SkillPackageError(
                    f"managed path for DSH package {name!r} already exists without an install record"
                )

            def tree_digest(root: Path) -> str:
                # 与 _install_snapshot.tree_digest 同形：dsh-git 记录在案时
                # 连文件 mode 一起对账（C-P3）；_git_tree_sha256 先校验 mode
                # 摘要、返回的仍是纯内容摘要，落盘的 sha256 口径不变。
                if record_extras and record_extras.get("source_kind") == "dsh-git":
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
                        "DSH package changed or could not be copied without following linked directories"
                    )
                staged_report = inspect_dsh_effort_slider(
                    staging_package, scope=self.scope
                )
                if staged_report is None or staged_report.get("package") != name:
                    raise SkillPackageError(
                        "DSH package stopped being recognized between preflight and snapshot"
                    )
                self._raise_if_dsh_drift(staged_report, "snapshot")
                record = {
                    "type": "dsh-native",
                    "scope": self.scope,
                    "source": source_value,
                    "sha256": source_digest,
                    "trust": {"status": "untrusted", "sha256": source_digest},
                    "enabled": False,
                    "installed_at": datetime.now(UTC).isoformat(),
                    # Q1（待裁决，当前按 enabled-adapted 实现）：如实携带 needs-adaptation + gaps，
                    # 不标 complete；适配激活 = 原生 reasoning-effort 语义（#865）。
                    "compatibility": {
                        "status": "needs-adaptation",
                        "license": report["license"],
                    },
                    "adapter": "native reasoning-effort adaptation (#865)",
                    "package": name,
                    "version": report["version"],
                    "source_pin": copy.deepcopy(report["source_pin"]),
                    "contributions": copy.deepcopy(report["contributions"]),
                    "gaps": copy.deepcopy(report["gaps"]),
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

    @staticmethod
    def _raise_if_dsh_drift(report: dict[str, Any], phase: str) -> None:
        """pin 摘要不匹配（drift）⇒ 拒绝；fail-closed，不降级不续装。

        错误消息必须可操作：附 pin 提交、各 finding 的 path/expected/actual，
        并给出恢复动作——只报裸 code 列表时用户既不知道差在哪、也不知道
        该恢复到什么内容。
        """
        if report.get("checked_files_match") is True and not report.get("findings"):
            return
        pin_commit = (report.get("source_pin") or {}).get("commit") or "unknown"
        details: list[str] = []
        for finding in report.get("findings", []):
            parts = [str(finding.get("code", "unknown"))]
            if finding.get("path"):
                parts.append(f"path={finding['path']}")
            if "expected" in finding:
                parts.append(f"expected={finding['expected']}")
            if "actual" in finding:
                parts.append(f"actual={finding['actual']}")
            details.append(" ".join(parts))
        raise SkillPackageError(
            f"DSH package {phase} does not match the pinned source"
            f" (pinned commit {pin_commit}); refusing"
            f" ({'; '.join(details) or 'content drift'})."
            f" Restore the content of pinned commit {pin_commit} and retry"
        )

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

            dsh_report = inspect_dsh_effort_slider(package, scope=self.scope)
            if dsh_report is not None:
                # DSH 原生包走自己的车道：目录名必须等于包名（与 Skill 记录的
                # fail-closed 判据同形），内容漂移拒装。
                if PurePosixPath(safe_subdirectory).name != dsh_report["package"]:
                    raise SkillPackageError(
                        f"DSH Git subdirectory {safe_subdirectory!r} does not match"
                        f" package name {dsh_report['package']!r}"
                    )
                return self._install_dsh_snapshot(
                    package,
                    report=dsh_report,
                    source_value=safe_url,
                    record_extras={
                        "source_kind": "dsh-git",
                        "git_subdirectory": safe_subdirectory,
                        "source_ref": safe_ref,
                        "resolved_commit": commit,
                        # 顶层落 modes 摘要（与 skill git 记录同形）：让
                        # _managed_package_sha256 对 dsh-git 也能对账文件
                        # mode，活动快照的 chmod 漂移才可检（P1-1 修法 a）。
                        "git_modes_sha256": git_modes_sha256,
                        "previous_version": None,
                        "pending_version": None,
                    },
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
        # 只窥探记录类型（_update_dsh_version 自带两段锁，不能在持锁时调用）。
        with self._locked_registry():
            manifest = self._read_manifest()
            record = self._record(manifest, name)
            is_dsh = record.get("type") == "dsh-native"
        if is_dsh:
            return self._update_dsh_version(name, safe_ref)
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

    def _update_dsh_version(self, name: str, safe_ref: str) -> dict[str, Any]:
        """DSH 原生包的显式 Git 版本暂存（与 Skill update 同构，两段锁）。

        预检门槛与安装一致：pin 文件摘要不匹配（drift）拒收，已装版本保留。
        适配内容永远锚定 pin，因此暂存成功只意味着「同 pin 内容的新提交」。
        """

        with self._locked_registry():
            manifest = self._read_manifest()
            record = self._record(manifest, name)
            if record.get("source_kind") != "dsh-git":
                raise SkillPackageError(f"DSH package {name!r} is not installed from Git")
            if record.get("pending_version") is not None:
                raise SkillPackageError(
                    f"DSH package {name!r} already has a pending version"
                )
            source_url = _validate_git_url(record["source"])
            subdirectory = _validate_git_subdirectory(record["git_subdirectory"])
            current_commit = record["resolved_commit"]

        with tempfile.TemporaryDirectory(prefix="agent-harness-dsh-update-") as temporary:
            # modes 摘要直接复用 fetch 产物：提取时已按 git mode 逐文件落盘，
            # 对同一目录再算一遍是重复劳动（且语义上就是同一个值）。
            package, commit, modes = _fetch_git_skill(
                source_url, safe_ref, subdirectory, Path(temporary)
            )
            report = inspect_dsh_effort_slider(package, scope=self.scope)
            if report is None or report.get("package") != name:
                raise SkillPackageError(
                    f"Git ref resolves to something that is not {name!r}; "
                    "the installed version was kept"
                )
            self._raise_if_dsh_drift(report, "update preflight")
            if commit == current_commit:
                raise SkillPackageError(
                    f"Git ref already resolves to the installed commit for {name!r}"
                )
            digest = _tree_sha256(package)
            pending = {
                "source_ref": safe_ref,
                "resolved_commit": commit,
                "snapshot_id": f"{commit}-{digest}",
                "sha256": digest,
                "git_modes_sha256": modes,
                "compatibility": {
                    "status": "needs-adaptation",
                    "license": report["license"],
                },
            }

            with self._locked_registry():
                manifest = self._read_manifest()
                record = self._record(manifest, name)
                if (
                    record.get("source_kind") != "dsh-git"
                    or record["source"] != source_url
                    or record["resolved_commit"] != current_commit
                    or record.get("pending_version") is not None
                ):
                    raise SkillPackageError(
                        f"Git source for DSH package {name!r} changed during update; retry"
                    )
                self._store_version_snapshot(package, name, pending)
                record["pending_version"] = pending
                self._write_manifest(manifest)
                return copy.deepcopy(record)

    def rollback(self, name: str) -> dict[str, Any]:
        with self._locked_registry():
            manifest = self._read_manifest()
            record = self._record(manifest, name)
            if record.get("type") == "dsh-native":
                if record.get("source_kind") != "dsh-git":
                    raise SkillPackageError(
                        f"DSH package {name!r} is not installed from Git"
                    )
                if record.get("pending_version") is not None:
                    raise SkillPackageError(
                        f"DSH package {name!r} already has a pending version"
                    )
                previous = record.get("previous_version")
                if previous is None:
                    raise SkillPackageError(
                        f"DSH package {name!r} has no previous DSH snapshot to restore"
                    )
                _validate_dsh_version_metadata(previous)
                active = self._package_path(name)
                self._verify_package(active, record)
                snapshot = self._version_snapshot_path(name, previous["snapshot_id"])
                # 与 skill git 车道同判据：连文件 mode 一起对账（P1-1 修法 a）。
                if _git_tree_sha256(snapshot, previous["git_modes_sha256"]) != previous["sha256"]:
                    raise SkillPackageError(
                        f"previous DSH snapshot for DSH package {name!r} changed"
                    )
                record["pending_version"] = copy.deepcopy(previous)
                self._write_manifest(manifest)
                return copy.deepcopy(record)
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
        """把暂存的 Skill 快照换到活动位（重启时由 Runtime 调用）。

        与 :meth:`_apply_dsh_pending_version` 有意保持同构、不抽公共脚手架
        （复核函数、门槛判据、落盘字段集不同，参数化需要 5-6 个钩子）；单边
        修 bug（尤其 manifest 写失败的恢复路径）必须同步检查另一处。
        """
        applied = 0
        with self._locked_registry():
            manifest = self._read_manifest()
            for name in sorted(manifest["packages"]):
                record = manifest["packages"][name]
                pending = record.get("pending_version")
                if pending is None:
                    continue
                if record.get("type") == "dsh-native":
                    self._apply_dsh_pending_version(manifest, name, record, pending)
                    applied += 1
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

    def _apply_dsh_pending_version(
        self,
        manifest: dict[str, Any],
        name: str,
        record: dict[str, Any],
        pending: dict[str, Any],
    ) -> None:
        """把暂存的 DSH 快照换到活动位（重启时由 Runtime 调用）。

        复核与安装同门槛：字节摘要 + 文件 mode（P1-1 修法 a）+ inspect 识别 +
        无 drift；失败时已装版本保持选中，不半换。与 :meth:`_apply_pending_versions`
        有意保持同构、不抽公共脚手架（理由见对方 docstring）；单边修 bug
        （尤其 manifest 写失败恢复路径）必须同步另一处。
        """

        _validate_dsh_version_metadata(pending)
        active = self._package_path(name)
        self._verify_package(active, record)
        snapshot = self._version_snapshot_path(name, pending["snapshot_id"])
        if _git_tree_sha256(snapshot, pending["git_modes_sha256"]) != pending["sha256"]:
            raise SkillPackageError(
                f"pending DSH snapshot for DSH package {name!r} changed"
            )

        current = {
            "source_ref": record["source_ref"],
            "resolved_commit": record["resolved_commit"],
            "snapshot_id": f"{record['resolved_commit']}-{record['sha256']}",
            "sha256": record["sha256"],
            # 顶层 modes 摘要取记录值而非从活动目录现算：_verify_package 已在
            # 上面按记录值对活动快照做过 mode+内容对账，现算反而会让随后
            # _store_version_snapshot 的 mode 复核对着新值恒过、失去判别力，
            # 且 verify→store 窗口的漂移会被烘焙进 previous_version（C-P4）。
            "git_modes_sha256": record["git_modes_sha256"],
            "compatibility": copy.deepcopy(record["compatibility"]),
        }
        # 当前活动版本先落成版本快照，rollback 才有 previous 可回（与 Skill 流一致）。
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
                    f"pending DSH snapshot for DSH package {name!r} changed while staging"
                )
            report = inspect_dsh_effort_slider(staged, scope=self.scope)
            if report is None or report.get("package") != name:
                raise SkillPackageError(
                    f"pending DSH snapshot for DSH package {name!r} is no longer recognized"
                )
            self._raise_if_dsh_drift(report, "pending snapshot")
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
            # 顶层 modes 摘要跟着换上的版本走（与 skill 车道一致）：
            # _managed_package_sha256 对 dsh-git 要拿它对账活动快照。
            record["git_modes_sha256"] = pending["git_modes_sha256"]
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
                        f"DSH package {name!r} manifest update failed; old snapshot remains at {displaced}"
                    ) from error
                raise
            shutil.rmtree(displaced, ignore_errors=True)
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)

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
        if record.get("type") == "dsh-native":
            # Q1（待裁决，当前按 enabled-adapted 实现）：DSH 包启用适配版——如实携带
            # needs-adaptation + gaps，不标 complete（spec 08 §6.3 禁止的是
            # "以完整兼容启用"）。适配激活 = 原生 UI 语义（#865），不进 Skill
            # 装配面；用快照字节对账证明激活对象就是安装时的那份内容。
            self._assert_no_shadow_conflict(name)
            self._verify_package(self._package_path(name), record)
            record["enabled"] = True
            return
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
        if record.get("type") == "dsh-native":
            # DSH 全局版同样按 enabled-adapted 放行（Q1 待裁决，当前实现），字节对账放行。
            manager = self.global_manager()
            manager._verify_package(manager._package_path(name), record)
            return
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
            if record.get("source_kind") in {"git", "dsh-git"}:
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
            if record.get("type") == "dsh-native":
                # DSH 适配不贡献 Skill 装配（原生 UI 语义，#865）；选择本身仍
                # 记录在案并由记录面可见，这里跳过而不是报"不完整"。
                continue
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
        except (OSError, ValueError, RecursionError) as error:
            # RecursionError（深嵌套清单）既非 OSError 也非 ValueError，不并入会让它逸出本模块的
            # 错误契约。同 `skills/inspection.py` 的 `DEPENDENCY_FILE_TOO_DEEP` 口径：读得出、判不了
            # 就落成明确失败。
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
            if isinstance(record, dict) and record.get("type") == "dsh-native":
                _validate_dsh_manifest_record(name, record, self.scope)
                continue
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
                f"package {package.name!r} changed after installation; refusing this operation"
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


def _validate_dsh_manifest_record(
    name: str, record: dict[str, Any], scope: str
) -> None:
    """DSH 原生包记录的 fail-closed 校验（#878 T9）。

    与 Skill 记录的关键差异：type 固定 ``dsh-native``、包名锚定 pin、
    compatibility **永远** needs-adaptation（Q1 待裁决，当前实现不标 complete）、
    source_pin/逐贡献映射/gaps 必须在案。
    """

    try:
        if name != DSH_SLIDER_PACKAGE_NAME:
            raise SkillPackageError(f"unexpected DSH package name {name!r}")
        if (
            record.get("package") != name
            or record.get("scope") != scope
            or not isinstance(record.get("source"), str)
            or not isinstance(record.get("installed_at"), str)
        ):
            raise SkillPackageError("record header is invalid")
        digest = record.get("sha256")
        if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
            raise SkillPackageError("content digest is invalid")
        trust = record.get("trust")
        if (
            not isinstance(trust, dict)
            or trust.get("status") != "untrusted"
            or trust.get("sha256") != digest
        ):
            raise SkillPackageError("trust metadata is invalid")
        if not isinstance(record.get("enabled"), bool):
            raise SkillPackageError("enabled flag is invalid")
        # 与 Skill 车道同判据：global scope 的记录不允许 enabled（C-P4）。
        if scope == "global" and record.get("enabled") is not False:
            raise SkillPackageError("global DSH record must not be enabled")
        compatibility = record.get("compatibility")
        if (
            not isinstance(compatibility, dict)
            or compatibility.get("status") != "needs-adaptation"
            or not isinstance(compatibility.get("license"), str)
        ):
            raise SkillPackageError("compatibility metadata is invalid")
        if not isinstance(record.get("adapter"), str) or not record["adapter"]:
            raise SkillPackageError("adapter identity is invalid")
        if record.get("version") != DSH_SLIDER_PINNED_VERSION:
            raise SkillPackageError("version does not match the pinned source")
        source_pin = record.get("source_pin")
        if (
            not isinstance(source_pin, dict)
            or source_pin.get("commit") != DSH_SLIDER_PINNED_COMMIT
            or source_pin.get("checked_files_sha256") != DSH_SLIDER_PINNED_FILES_SHA256
        ):
            raise SkillPackageError("source pin metadata is invalid")
        contributions = record.get("contributions")
        if not isinstance(contributions, list) or not contributions:
            raise SkillPackageError("contribution mapping is missing")
        # 条目关键字段形状 fail-closed：被篡改的贡献映射只要还是非空 list
        # 就放行，等于没有校验。evidence 只要求是 list（元素形状是信息性
        # 字段，不参与任何判据，不校验；注释不宣称"全格式校验"，与实际
        # 强度一致——C-P4）。
        for contribution in contributions:
            if (
                not isinstance(contribution, dict)
                or not isinstance(contribution.get("name"), str)
                or not contribution["name"]
                or not isinstance(contribution.get("source"), str)
                or not contribution["source"]
                or not isinstance(contribution.get("project_mapping"), str)
                or not contribution["project_mapping"]
                or contribution.get("status") != "adapted"
                or not isinstance(contribution.get("evidence"), list)
            ):
                raise SkillPackageError("contribution mapping entry is invalid")
        gaps = record.get("gaps")
        if not isinstance(gaps, list) or not gaps:
            raise SkillPackageError("declared gaps are missing")
        for gap in gaps:
            if (
                not isinstance(gap, dict)
                or not isinstance(gap.get("name"), str)
                or not gap["name"]
                or not isinstance(gap.get("status"), str)
                or not gap["status"]
                or not isinstance(gap.get("reason"), str)
                or not gap["reason"]
                or not isinstance(gap.get("evidence"), list)
            ):
                raise SkillPackageError("declared gap entry is invalid")
        if not any(
            gap["name"] == "off" and gap["status"] == "unmapped" for gap in gaps
        ):
            raise SkillPackageError("declared gaps must include the unmapped off gap")

        source_kind = record.get("source_kind")
        git_fields = {
            "git_subdirectory",
            "source_ref",
            "resolved_commit",
            "git_modes_sha256",
            "previous_version",
            "pending_version",
        }
        if source_kind is None:
            if git_fields.intersection(record):
                raise SkillPackageError("local DSH record carries Git fields")
            return
        if source_kind != "dsh-git":
            raise SkillPackageError(f"unsupported DSH source kind {source_kind!r}")
        # dsh-git 记录必须携带合法的顶层 modes 摘要（_managed_package_sha256
        # 对 dsh-git 要拿它对账文件 mode；schema 承诺的维度必须在案）。
        modes = record.get("git_modes_sha256")
        if not isinstance(modes, str) or not _DIGEST_PATTERN.fullmatch(modes):
            raise SkillPackageError("Git file-mode digest is missing or invalid")
        _validate_git_url(record["source"])
        subdirectory = _validate_git_subdirectory(record["git_subdirectory"])
        if PurePosixPath(subdirectory).name != name:
            raise SkillPackageError(
                f"Git subdirectory {subdirectory!r} does not match package name {name!r}"
            )
        _validate_git_ref(record["source_ref"])
        commit = record.get("resolved_commit")
        if not isinstance(commit, str) or not _COMMIT_PATTERN.fullmatch(commit):
            raise SkillPackageError("resolved commit is invalid")
        for key in ("previous_version", "pending_version"):
            version = record.get(key)
            if version is not None:
                _validate_dsh_version_metadata(version)
    except (KeyError, SkillPackageError) as error:
        raise SkillManifestError(
            f"install manifest record for {name!r} is invalid"
        ) from error


def _validate_dsh_version_metadata(version: Any) -> None:
    """DSH 版本元数据（previous_version / pending_version）的形状校验。"""
    required = {
        "source_ref",
        "resolved_commit",
        "snapshot_id",
        "sha256",
        "git_modes_sha256",
        "compatibility",
    }
    if not isinstance(version, dict) or set(version) != required:
        raise SkillPackageError("DSH version metadata has an invalid shape")
    commit = version["resolved_commit"]
    digest = version["sha256"]
    modes_digest = version["git_modes_sha256"]
    compatibility = version["compatibility"]
    if (
        not isinstance(commit, str)
        or not _COMMIT_PATTERN.fullmatch(commit)
        or not isinstance(digest, str)
        or not _DIGEST_PATTERN.fullmatch(digest)
        or not isinstance(modes_digest, str)
        or not _DIGEST_PATTERN.fullmatch(modes_digest)
        or version["snapshot_id"] != f"{commit}-{digest}"
    ):
        raise SkillPackageError("DSH version metadata does not match its snapshot")
    _validate_git_ref(version["source_ref"])
    if (
        not isinstance(compatibility, dict)
        or compatibility.get("status") != "needs-adaptation"
        or not isinstance(compatibility.get("license"), str)
    ):
        raise SkillPackageError("DSH version compatibility metadata is invalid")


def _source_label(record: dict[str, Any]) -> str:
    """来源/版本标签：Git 记录带 commit，其余退回内容摘要。"""
    version = record.get("resolved_commit") or record.get("sha256", "")
    return f"{record.get('source')}@{version}"


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _managed_package_sha256(root: Path, record: dict[str, Any]) -> str:
    if record.get("source_kind") in {"git", "dsh-git"}:
        # git 与 dsh-git 记录同判据：内容 + 文件 mode 一起对账，活动快照的
        # chmod 漂移才逃不过 _verify_package（P1-1 修法 a）。
        modes = record.get("git_modes_sha256")
        if not isinstance(modes, str) or not _DIGEST_PATTERN.fullmatch(modes):
            raise SkillPackageError(
                "package record is missing a valid Git file-mode digest"
            )
        return _git_tree_sha256(root, modes)
    return _tree_sha256(root)
