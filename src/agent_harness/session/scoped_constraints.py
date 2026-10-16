"""约束作用域三层（会话 / 项目 / 全局）的**独立规则层**——#1191 / #1176 Phase 2。

## 为什么要独立于 Memory V2（用户裁决 Fork 2 = A，2026-10-16）

用户原话（第一性原理）：*"protected facts 和 memory v2 是两种东西，不能混在一起，
更不能存在一起——protected facts 相当于维护了一个文件，内容可展示在面板；memory v2
是长期记忆，召回靠检索。"* 据此本模块刻意**不碰 memory v2**（`memory/v2/**` 一行不改）：
- 约束走"规则层"：会话 / 项目 / 全局各一个"文件"（概念上），**面板展示什么就注入什么**、
  **精确取用不检索**；
- 项目键（git 根）算法可复用，仅作定位 key；
- 票面 §三.1"落在 memory v2 链路"按一手证据/用户裁决**证伪处理**，语义落在本模块与 PR
  描述里，票面本身不动（AGENTS §9.1.1 的票面变更控制）。

## 三层落点

- **session**：仍活在会话事件流（`task/protected_fact` 投影，`derive_protected_facts`）——
  本模块**不接管**它，只提供枚举值。
- **project**：`<root>/projects/<sha256(project_key)>.json`，一个项目一个文件；
  project_key = `git rev-parse --git-common-dir` 归一化后的**主仓库根绝对路径**
  （同一仓库的 worktree / 子目录共享同一键——抄 Claude Code auto memory 口径
  `code.claude.com/docs/en/memory`："all worktrees and subdirectories within the same
  repo share one auto memory directory"）。
- **global**：`<root>/global.json`，用户级单文件。

## 历史保留链（不物理删除）

撤销 = 把记录置 `status="revoked"` 并补 `revoked_at` / `revoked_by`，**行仍在文件里**
（可审计；与 #1190 会话级撤销的"抑制在视图层、历史不删"同构）。register 只追加。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from agent_harness.session.errors import SessionServiceError


class ConstraintScope(str, Enum):
    """约束作用域三值（票面 §四.1 的三选一）。"""

    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


class ScopeConstraintError(SessionServiceError):
    """规则层存储读写失败（IO / 损坏文件）。

    继承 `SessionServiceError` 才能进 `_DOMAIN_ERROR_STATUS` 单一映射（与同类的
    `EventLogCorruptError` 同立场：存储损坏 → 409，不可重试）。此前是裸
    `RuntimeError`，从规则层端点逃出去只能落 FastAPI 默认 500（审查 finding C6）。
    """


@dataclass(frozen=True, slots=True)
class ScopedConstraint:
    """项目/全局级约束的一条记录（规则层最小单元）。"""

    fact_id: str
    type: str
    value: Any
    scope: str  # "project" | "global"
    registered_at: str
    status: str = "active"  # active | revoked
    revoked_at: str | None = None
    revoked_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "type": self.type,
            "value": self.value,
            "scope": self.scope,
            "registered_at": self.registered_at,
            "status": self.status,
            "revoked_at": self.revoked_at,
            "revoked_by": self.revoked_by,
        }


def _require_scope(scope: ConstraintScope | str) -> ConstraintScope:
    try:
        return ConstraintScope(scope)
    except ValueError as error:
        raise ValueError(f"unknown constraint scope: {scope!r}") from error


def resolve_project_key(cwd: str | Path) -> str | None:
    """会话工作目录 → 项目键（主仓库根绝对路径）；非 git 目录返回 None。

    口径（票面 §三.2）：`git rev-parse --git-common-dir` 归一化——同一仓库的
    worktree / 子目录算出同一个键（worktree 的 common dir 指向主仓库 `.git`）。
    """
    base = Path(cwd)
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    common = Path(raw)
    if not common.is_absolute():
        common = base / common
    try:
        common = common.resolve()
    except OSError:
        return None
    # `.git`（普通仓库）或 `<main>/.git`（worktree）——其父目录即主仓库根。
    root = common.parent
    return str(root)


def project_label(cwd: str | Path) -> str | None:
    """面板 PROJECT 组右上的 mono 标注：`owner/repo`；无 remote 回落目录名。

    票面 §五.2："无 remote 的仓库显示 git 根目录名"。非 git 目录返回 None。
    """
    key = resolve_project_key(cwd)
    if key is None:
        return None
    try:
        remote = subprocess.run(
            ["git", "-C", key, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        remote = None
    if remote is not None and remote.returncode == 0:
        url = remote.stdout.strip()
        slug = _remote_slug(url)
        if slug:
            return slug
    return Path(key).name


def _remote_slug(url: str) -> str | None:
    """`git@github.com:owner/repo.git` / `https://host/owner/repo(.git)` → `owner/repo`。"""
    if not url:
        return None
    tail = url
    if "://" in tail:
        tail = tail.split("://", 1)[1]
        tail = tail.split("/", 1)[1] if "/" in tail else ""
    elif ":" in tail:  # scp-like: git@host:owner/repo.git
        tail = tail.split(":", 1)[1]
    tail = tail.strip("/").removesuffix(".git")
    parts = [part for part in tail.split("/") if part]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])


class ScopedConstraintStore:
    """项目 / 全局级约束的文件存储（每 scope-key 一个 JSON 文件）。

    并发：单实例一把可重入锁串行化读改写 + 原子替换（`os.replace`）落盘。Web 层
    每次请求经 `AppState` 复用**同一个**实例（与 `workspace_index` 同款：持有内存
    语义的组件只构造一次），故锁覆盖同一进程内的所有请求。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # 同一根目录的全进程共享一把锁：Web 的 `AppState` 与装配层（注入 provider）
        # 会**各建一个实例**指向同一批文件——共享锁才不会出现"两个锁各写各的"。
        self._lock = _lock_for(self.root)

    # ── 路径 ────────────────────────────────────────────────────────────
    def _path(self, scope: ConstraintScope, project_key: str | None) -> Path:
        if scope is ConstraintScope.GLOBAL:
            return self.root / "global.json"
        if project_key is None:
            raise ValueError("project scope requires a project key")
        digest = hashlib.sha256(project_key.encode("utf-8")).hexdigest()[:24]
        return self.root / "projects" / f"{digest}.json"

    def _load(self, path: Path) -> list[dict[str, Any]]:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as error:  # pragma: no cover - 环境相关
            raise ScopeConstraintError(f"cannot read {path}: {error}") from error
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise ScopeConstraintError(f"corrupt constraint file {path}") from error
        if not isinstance(data, list):
            raise ScopeConstraintError(f"corrupt constraint file {path}")
        return [row for row in data if isinstance(row, dict)]

    def _save(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as error:  # pragma: no cover - 环境相关
            tmp.unlink(missing_ok=True)
            raise ScopeConstraintError(f"cannot write {path}: {error}") from error

    @staticmethod
    def _row_to_constraint(row: dict[str, Any]) -> ScopedConstraint | None:
        fact_id = row.get("fact_id")
        fact_type = row.get("type")
        scope = row.get("scope")
        registered_at = row.get("registered_at")
        if not (
            isinstance(fact_id, str)
            and isinstance(fact_type, str)
            and isinstance(scope, str)
            and isinstance(registered_at, str)
        ):
            return None
        status = row.get("status")
        revoked_at = row.get("revoked_at")
        revoked_by = row.get("revoked_by")
        return ScopedConstraint(
            fact_id=fact_id,
            type=fact_type,
            value=row.get("value"),
            scope=scope,
            registered_at=registered_at,
            status=status if isinstance(status, str) else "active",
            revoked_at=revoked_at if isinstance(revoked_at, str) else None,
            revoked_by=revoked_by if isinstance(revoked_by, str) else None,
        )

    # ── 读 ──────────────────────────────────────────────────────────────
    def list(
        self, scope: ConstraintScope | str, *, project_key: str | None = None,
    ) -> list[ScopedConstraint]:
        scope = _require_scope(scope)
        if scope is ConstraintScope.SESSION:
            raise ValueError("session-scope constraints live in the session event log")
        path = self._path(scope, project_key)
        with self._lock:
            rows = self._load(path)
        out: list[ScopedConstraint] = []
        for row in rows:
            constraint = self._row_to_constraint(row)
            if constraint is not None:
                out.append(constraint)
        return out

    def get(
        self, scope: ConstraintScope | str, fact_id: str, *, project_key: str | None = None,
    ) -> ScopedConstraint | None:
        return next(
            (c for c in self.list(scope, project_key=project_key) if c.fact_id == fact_id),
            None,
        )

    # ── 写 ──────────────────────────────────────────────────────────────
    def register(
        self,
        scope: ConstraintScope | str,
        *,
        project_key: str | None = None,
        fact_type: str,
        value: Any,
        registered_at: str,
    ) -> ScopedConstraint:
        scope = _require_scope(scope)
        if scope is ConstraintScope.SESSION:
            raise ValueError("session-scope constraints are registered in the session log")
        if not isinstance(fact_type, str) or not fact_type:
            raise ValueError("fact_type is required")
        prefix = "glb" if scope is ConstraintScope.GLOBAL else "prj"
        constraint = ScopedConstraint(
            fact_id=f"{prefix}-{uuid.uuid4().hex}",
            type=fact_type,
            value=value,
            scope=scope.value,
            registered_at=registered_at,
        )
        path = self._path(scope, project_key)
        with self._lock:
            rows = self._load(path)
            rows.append(constraint.to_dict())
            self._save(path, rows)
        return constraint

    def revoke(
        self,
        scope: ConstraintScope | str,
        fact_id: str,
        *,
        project_key: str | None = None,
        revoked_by: str | None = None,
        revoked_at: str,
    ) -> ScopedConstraint | None:
        """撤销一条项目/全局约束（历史保留链，不物理删除）。幂等：已撤销原样返回。"""
        scope = _require_scope(scope)
        if scope is ConstraintScope.SESSION:
            raise ValueError("session-scope revocations go through the session log")
        path = self._path(scope, project_key)
        with self._lock:
            rows = self._load(path)
            for index, row in enumerate(rows):
                constraint = self._row_to_constraint(row)
                if constraint is None or constraint.fact_id != fact_id:
                    continue
                if constraint.status == "revoked":
                    return constraint
                updated = replace(
                    constraint,
                    status="revoked",
                    revoked_at=revoked_at,
                    revoked_by=revoked_by,
                )
                rows[index] = updated.to_dict()
                self._save(path, rows)
                return updated
        return None


def normalized_fact_value(value: Any) -> Any:
    """覆盖判据的值归一：字符串去首尾空白，其余原样（票面 §五.4）。

    **单一 owner**（#1191 审查 finding S-b1）：注入侧（`scoped_context_facts`）与
    面板侧（`SessionService._merge_effective_rows`）共用这一条判据——两处各写一份
    会让「同文」的定义分叉（本仓先例：`context/builder.py` 的"单一 owner"注释）。
    """
    return value.strip() if isinstance(value, str) else value


def covering_fact_id(
    type_: str, value: Any, candidates: Iterable[tuple[str, Any, str]],
) -> str | None:
    """返回覆盖 `(type_, value)` 的候选 `fact_id`；无则 None。

    候选顺序即优先级（会话＞项目＞全局）——由调用方按该顺序拼 `candidates`。
    `value` 须已过 `normalized_fact_value`。
    """
    for candidate_type, candidate_value, candidate_id in candidates:
        if candidate_type == type_ and candidate_value == value:
            return candidate_id
    return None


def scoped_context_facts(
    store: ScopedConstraintStore,
    session_facts: list[Any],
    *,
    session_id: str,
    project_key: str | None,
) -> list[Any]:
    """项目/全局级 **active** 约束 → 可注入的 `ProtectedFact` 列表（Fork 3，票面 §一）。

    注入规则（票面 §五.1 / §六.1）：`overridden_by` 非 null 的条目不注入——被更高
    优先级同文（type 相同且 value 去首尾空白后逐字相等；会话＞项目＞全局）的 active
    约束覆盖时跳过。真值直接来自规则层文件，**精确取用不检索**。

    返回的 `ProtectedFact` 用合成来源（`source_event_id="scoped:<fact_id>"`，
    `source_seq=0`）——它们不在本会话事件流里，合成值是"无会话来源事件"的显式表达，
    注入体（`serialize_protected_facts`）无需改动。
    """
    from agent_harness.session.derive import ProtectedFact  # 局部导入避免模块加载环

    project_rows = (
        store.list(ConstraintScope.PROJECT, project_key=project_key)
        if project_key is not None
        else []
    )
    global_rows = store.list(ConstraintScope.GLOBAL)
    active_session = [
        (fact.type, normalized_fact_value(fact.value), fact.fact_id)
        for fact in session_facts
        if getattr(fact, "status", "active") == "active"
    ]
    active_project = [
        (row.type, normalized_fact_value(row.value), row.fact_id)
        for row in project_rows
        if row.status == "active"
    ]

    out: list[Any] = []
    for row in [*project_rows, *global_rows]:
        if row.status != "active":
            continue
        candidates = (
            active_session
            if row.scope == ConstraintScope.PROJECT.value
            else [*active_session, *active_project]
        )
        if covering_fact_id(row.type, normalized_fact_value(row.value), candidates):
            continue  # 已被更高优先级同文约束覆盖 → 不注入
        out.append(
            ProtectedFact(
                fact_id=row.fact_id,
                type=row.type,
                value=row.value,
                source_event_id=f"scoped:{row.fact_id}",
                source_seq=0,
                status="active",
                session_id=session_id,
                scope=row.scope,
            )
        )
    return out


__all__ = [
    "ConstraintScope",
    "ScopeConstraintError",
    "ScopedConstraint",
    "ScopedConstraintStore",
    "covering_fact_id",
    "normalized_fact_value",
    "project_label",
    "resolve_project_key",
    "scoped_context_facts",
]
