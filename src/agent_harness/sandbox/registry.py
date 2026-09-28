"""WorkspaceRegistry：Session ↔ Sandbox 映射表的持久化管理。

05_SANDBOX_CODING_TOOLS.md §2 + 07_STORAGE_PERSISTENCE_RECOVERY.md §9 要求的恢复顺序
（load Session → load sandbox mapping → ensure sandbox started → ... → resume）在
第二步需要一张持久化的 session_id → sandbox 映射表。WorkspaceRegistry 就是这张表。

映射存为 JSON 文件（与 SessionStore 的 JSONL 同级技术，不引入 SQLite/Postgres）：
  <root>/workspaces/<session_id>.json

LocalSubprocessSandbox 后端：workspace 是真实目录，天然持久——进程重启后目录还在。
DockerSandbox 后端（Ticket D）：容器名/volume 名基于 session_id 确定性生成，
volume 持久，resume 时用确定性名字重启容器即可恢复 workspace。

本模块只负责映射管理和 Sandbox 实例重建，不改 Sandbox 路径边界（ADR-0001 不变）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_harness.sandbox.base import Sandbox
from agent_harness.sandbox.local import LocalSubprocessSandbox
from agent_harness.sandbox.paths import canonical_workspace_path

logger = logging.getLogger(__name__)


class WorkspaceBindingError(RuntimeError):
    """A persisted workspace owner/alias binding is invalid or immutable."""


class WorkspaceRegistry:
    """Session ↔ Sandbox 映射表的持久化管理器。"""

    def __init__(self, root: Path, backend: str = "local") -> None:
        """root 是映射表和 workspace 的根目录。backend='local'|'docker'。"""
        self._root = Path(root).resolve()
        self._backend = backend
        self._workspaces_dir = self._root / "workspaces"
        self._workspaces_dir.mkdir(parents=True, exist_ok=True)
        #: 进程内缓存：session_id → Sandbox 实例（避免重复重建）。
        self._cache: dict[str, Sandbox] = {}

    def create(self, session_id: str, *, workspace_root: Path | None = None) -> Sandbox:
        """为新 session 创建 Sandbox，持久化映射，返回已 ensure_started 的 Sandbox。

        如果映射已存在（同 session_id 再次 create），直接从缓存或重建返回已有 Sandbox。
        workspace_root 允许调用方指定实际工作目录（web 层的命名 workspace）；
        缺省用 <root>/workspaces/<session_id>。映射里记录真实目录——
        RecoveryCoordinator 据此恢复（R8-1）。

        映射里的路径是**规范化后**的（`canonical_workspace_path`，WS-1 AC5）：
        先 mkdir 是这里原本的行为（确保 workspace 目录存在），随后按 `fs.realpath`
        语义规范化，所以已存在的链接会被解析到目标。会话侧 `session/started` 的
        `cwd` 走同一个函数，故"日志里的路径"与"映射里的路径"对同一物理目录必然
        逐字符相等。
        """
        existing = self._read_mapping(session_id)
        if existing is not None:
            if "workspace_owner_session_id" in existing:
                raise WorkspaceBindingError(
                    f"Session '{session_id}' has a non-owning workspace alias; "
                    "create() cannot replace its owner binding."
                )
            return self.get(session_id)
        if session_id in self._cache:
            return self._cache[session_id]

        mapping = self._build_mapping(session_id)
        requested = (
            Path(workspace_root) if workspace_root is not None
            else Path(mapping["workspace_root"])
        )
        requested.mkdir(parents=True, exist_ok=True)
        workspace_root = Path(canonical_workspace_path(requested))
        workspace_root.mkdir(parents=True, exist_ok=True)
        mapping["workspace_root"] = str(workspace_root)

        sandbox = self._instantiate_sandbox(mapping)
        sandbox.ensure_started()

        self._write_mapping(session_id, mapping)
        self._cache[session_id] = sandbox
        return sandbox

    def bind_alias(self, session_id: str, owner_session_id: str) -> Sandbox:
        """Persist a non-owning session binding to its canonical workspace owner.

        Delegated sessions share the parent's workspace, but recovery resolves a
        sandbox by session id. This durable alias makes that lookup survive process
        restart without granting the child ownership of the workspace resource.
        """
        if session_id == owner_session_id:
            raise WorkspaceBindingError(
                f"Session '{session_id}' cannot alias its own workspace."
            )

        canonical_owner, _ = self._resolve_workspace_owner(owner_session_id)
        existing = self._read_mapping(session_id)
        if existing is not None:
            if "workspace_owner_session_id" not in existing:
                raise WorkspaceBindingError(
                    f"Session '{session_id}' already owns a workspace; "
                    "its workspace owner cannot be changed."
                )
            existing_owner, _ = self._resolve_workspace_owner(session_id)
            if existing_owner != canonical_owner:
                raise WorkspaceBindingError(
                    f"Session '{session_id}' is already bound to workspace owner "
                    f"'{existing_owner}' and cannot be rebound to '{canonical_owner}'."
                )
            return self.get(session_id)

        self._write_mapping(session_id, {
            "session_id": session_id,
            "workspace_owner_session_id": canonical_owner,
            "created_at": datetime.now(UTC).isoformat(),
        })
        return self.get(canonical_owner)

    def get(self, session_id: str) -> Sandbox:
        """查回 session 的 Sandbox 实例（必要时从 JSON 重建并 ensure_started）。"""
        mapping = self._read_mapping(session_id)
        if mapping is None:
            raise KeyError(
                f"Session '{session_id}' 没有对应的 workspace 映射记录。"
            )

        owner_session_id, owner_mapping = self._resolve_workspace_owner(session_id)
        if owner_session_id in self._cache:
            sandbox = self._cache[owner_session_id]
            self._cache[session_id] = sandbox
            return sandbox

        sandbox = self._instantiate_sandbox(owner_mapping)
        sandbox.ensure_started()

        self._cache[owner_session_id] = sandbox
        self._cache[session_id] = sandbox
        return sandbox

    def exists(self, session_id: str) -> bool:
        """检查 session 是否有映射记录。"""
        return self._mapping_path(session_id).exists()

    def recorded_workspace_roots(self, session_id: str) -> list[str]:
        """本注册表为该 session 登记的**全部**工作目录（持久映射 + 进程内 cache）。

        只读对账入口（#266）：会话侧拿 `session/started.cwd`，注册表拿这些值，任一项
        不一致时由调用方类型化失败。两个记录都可能成为 runtime 实际使用的目录
        （`create()` 的 cache 分支按定义返回既有实例、**不重写映射**），所以两个都要
        对账——只看其中一个，都会漏掉另一种"静默跑在别的目录"的形态。

        **不实例化 Sandbox**：`get()` / `create()` 都会在构造 Sandbox 时 mkdir 工作
        目录，对一个已被用户删掉的外部目录，那等于先把它凭空建回来、再宣布"目录在，
        一切正常"（"目录没了"就永远看不见了）。

        无任何记录 → 空列表："没登记"与"登记在别处"必须可区分。cache 项只对本地后端取
        `workspace_root`：DockerSandbox 的同名属性是**容器内**路径，与会话侧 cwd 不是
        一个坐标系（AppState 固定 backend='local'）。
        """
        roots: list[str] = []
        mapping = self._read_mapping(session_id)
        if mapping is not None:
            owner_session_id, owner_mapping = self._resolve_workspace_owner(session_id)
            recorded = owner_mapping.get("workspace_root")
            if isinstance(recorded, str) and recorded:
                roots.append(recorded)
        else:
            owner_session_id = session_id
        sandbox = self._cache.get(owner_session_id)
        if isinstance(sandbox, LocalSubprocessSandbox):
            cached = canonical_workspace_path(sandbox.workspace_root)
            if cached not in roots:
                roots.append(cached)
        return roots

    def stop(self, session_id: str) -> None:
        """停止 session 的 Sandbox（保留 Volume/workspace 以便 resume）。幂等。

        跨进程安全：即使本进程没缓存该 Sandbox，也会从映射记录重建实例并停掉它。
        """
        mapping = self._read_mapping(session_id)
        if mapping is not None and "workspace_owner_session_id" in mapping:
            self._cache.pop(session_id, None)
            return

        sandbox = self._cache.get(session_id)
        if sandbox is None:
            # 跨进程恢复：进程重启后 cache 为空，但容器可能还在跑。
            mapping = self._read_mapping(session_id)
            if mapping is None:
                return  # 没有映射记录，幂等 no-op
            sandbox = self._instantiate_sandbox(mapping)
        sandbox.stop()
        self._cache.pop(session_id, None)
        for alias_id in self._alias_ids_for_owner(session_id):
            self._cache.pop(alias_id, None)

    def delete(self, session_id: str) -> None:
        """彻底清理 session 的 Sandbox 资源和映射（容器 + Volume + workspace 目录）。幂等。

        跨进程安全：即使本进程没缓存该 Sandbox，也会从映射记录重建实例再彻底销毁。
        """
        mapping = self._read_mapping(session_id)
        if mapping is not None and "workspace_owner_session_id" in mapping:
            self._cache.pop(session_id, None)
            self._mapping_path(session_id).unlink(missing_ok=True)
            return

        sandbox = self._cache.get(session_id)
        if sandbox is None:
            mapping = self._read_mapping(session_id)
            if mapping is None:
                # 没有映射记录——清理可能残留的孤儿 workspace 目录后返回。
                workspace_dir = self._workspaces_dir / session_id
                if workspace_dir.exists():
                    shutil.rmtree(workspace_dir, ignore_errors=True)
                self._invalidate_descendant_aliases(session_id)
                return
            sandbox = self._instantiate_sandbox(mapping)
        sandbox.delete()
        self._invalidate_descendant_aliases(session_id)
        self._cache.pop(session_id, None)
        mapping_file = self._mapping_path(session_id)
        if mapping_file.exists():
            mapping_file.unlink()

    def discard_session_artifacts(self, session_id: str) -> None:
        """硬删会话时丢弃 harness 自己造的两样沙箱工件。**绝不解引用映射**。幂等。

        只删本注册表用 `root + session_id` 自己拼出来的路径：

        - `<root>/workspaces/<session_id>.json`（映射记录）
        - `<root>/workspaces/<session_id>/`（默认形态的会话工作区）

        第二条为什么不需要再判"确实是默认形态"（#172 的原话）：这个路径**就是**默认形态
        的定义——它由本注册表用 root 与 session_id 拼成，只有 harness 会往里写（
        `create` 的默认分支、`resume_and_launch` 的无条件 mkdir 都写在同一个位置），
        用户自己的目录永远不在这个前缀下。判定因此是"写死的构造规则"，不是"读映射再
        决定删什么"——后者才会删到用户仓库。

        **为什么不用 `delete()`**（ADR-0029 D2）：`delete()` 走 `Sandbox.delete()`，
        本地后端是 `shutil.rmtree(self._workspace_root)`；而 ADR-0027 之后
        `workspace_root` 可能是**用户的真实目录**（cwd 会话），映射里就写着
        `D:\\some\\repo`。任何用它做硬删的路径都会删掉用户的仓库——所以那条路径
        今天不能有生产调用方（登记在 `docs/phase_status/2026-09.md:293`，即 2026-09-13
        的 #172 会话硬删除条目：`WorkspaceRegistry.delete()` 被硬性否决），本方法就是它的安全替代。

        映射指向别处时，只删映射文件本身，**不碰 `workspace_root`**（这是刻意的：
        用户目录不归 harness 处置）。

        其他 session 的 alias mapping 不属于本方法的删除白名单，因此父会话被硬删后
        这些记录可能留在磁盘；由于 owner mapping 已不存在，`get()` 会拒绝恢复并失败关闭。
        """
        mapping_file = self._mapping_path(session_id)
        mapping_file.unlink(missing_ok=True)
        default_workspace = self._workspaces_dir / session_id
        if default_workspace.is_dir():
            shutil.rmtree(default_workspace, ignore_errors=True)
        self._cache.pop(session_id, None)

    # —— 内部方法 ——

    def _build_mapping(self, session_id: str) -> dict:
        """构造新 session 的映射字典。"""
        workspace_root = self._workspaces_dir / session_id
        mapping = {
            "session_id": session_id,
            "backend": self._backend,
            "workspace_root": str(workspace_root),
            "container_name": None,
            "volume_name": None,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if self._backend == "docker":
            # 确定性命名：基于 session_id，进程重启后能按名字找回容器/volume。
            mapping["container_name"] = f"agent-harness-{session_id}"
            mapping["volume_name"] = f"agent-harness-{session_id}"
        return mapping

    def _instantiate_sandbox(self, mapping: dict) -> Sandbox:
        """根据映射字典重建 Sandbox 实例。"""
        backend = mapping.get("backend", "local")
        workspace_root = Path(mapping["workspace_root"])

        if backend == "local":
            return LocalSubprocessSandbox(workspace_root=workspace_root)

        if backend == "docker":
            from agent_harness.sandbox.docker import DockerSandbox

            container_name = mapping.get("container_name") or None
            volume_name = mapping.get("volume_name") or None
            return DockerSandbox(
                container_name=container_name,
                volume_name=volume_name,
            )

        raise ValueError(f"未知的 Sandbox 后端: {backend}")

    def _mapping_path(self, session_id: str) -> Path:
        return self._workspaces_dir / f"{session_id}.json"

    def _write_mapping(self, session_id: str, mapping: dict) -> None:
        # temp + os.replace 原子落盘：truncate-in-place 写到一半崩溃会留下损坏
        # JSON，get()/stop()/delete() 从此对该 session 永久 JSONDecodeError
        # （resume 与清理双断，且不自愈）。同目录 rename 在两种平台都原子。
        path = self._mapping_path(session_id)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(
                json.dumps(mapping, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            with suppress(OSError):
                tmp.unlink()

    def _read_mapping(self, session_id: str) -> dict | None:
        path = self._mapping_path(session_id)
        if not path.exists():
            return None
        try:
            mapping = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.error(
                "Workspace mapping is unreadable; refusing workspace restore",
                extra={"workspace_session_id": session_id},
            )
            raise WorkspaceBindingError(
                f"Workspace mapping for session '{session_id}' is unreadable "
                f"({type(error).__name__})."
            ) from error
        if not isinstance(mapping, dict):
            logger.error(
                "Workspace mapping is not a JSON object; refusing workspace restore",
                extra={"workspace_session_id": session_id},
            )
            raise WorkspaceBindingError(
                f"Workspace mapping for session '{session_id}' is not a JSON object."
            )
        return mapping

    def _resolve_workspace_owner(self, session_id: str) -> tuple[str, dict]:
        """Resolve persisted aliases to one owning mapping; reject bad chains."""
        current = session_id
        seen: list[str] = []
        while True:
            if current in seen:
                chain = " -> ".join([*seen, current])
                logger.error(
                    "Workspace alias cycle; refusing workspace restore",
                    extra={"workspace_session_id": session_id},
                )
                raise WorkspaceBindingError(
                    f"Workspace alias cycle while resolving '{session_id}': {chain}."
                )
            seen.append(current)
            mapping = self._read_mapping(current)
            if mapping is None:
                if current == session_id:
                    raise KeyError(
                        f"Session '{session_id}' 没有对应的 workspace 映射记录。"
                    )
                logger.error(
                    "Workspace alias references a missing owner; refusing restore",
                    extra={
                        "workspace_session_id": session_id,
                        "workspace_owner_session_id": current,
                    },
                )
                raise WorkspaceBindingError(
                    f"Workspace alias for session '{session_id}' references missing "
                    f"owner '{current}'."
                )
            if mapping.get("session_id") != current:
                logger.error(
                    "Workspace mapping identity mismatch; refusing restore",
                    extra={"workspace_session_id": current},
                )
                raise WorkspaceBindingError(
                    f"Workspace mapping identity mismatch for session '{current}'."
                )
            if "workspace_owner_session_id" not in mapping:
                if not isinstance(mapping.get("workspace_root"), str):
                    logger.error(
                        "Owning workspace mapping has no root; refusing restore",
                        extra={"workspace_session_id": current},
                    )
                    raise WorkspaceBindingError(
                        f"Owning workspace mapping for session '{current}' has no root."
                    )
                return current, mapping

            owner_session_id = mapping.get("workspace_owner_session_id")
            if not isinstance(owner_session_id, str) or not owner_session_id:
                logger.error(
                    "Workspace alias has an invalid owner id; refusing restore",
                    extra={"workspace_session_id": current},
                )
                raise WorkspaceBindingError(
                    f"Workspace alias for session '{current}' has an invalid owner id."
                )
            current = owner_session_id

    def _alias_ids_for_owner(self, owner_session_id: str) -> set[str]:
        """Return persisted aliases transitively referencing an owner id."""
        reverse_references: dict[str, set[str]] = {}
        for mapping_path in self._workspaces_dir.glob("*.json"):
            alias_id = mapping_path.stem
            try:
                mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.error(
                    "Cannot inspect workspace mapping while deleting/stopping owner",
                    extra={"workspace_session_id": alias_id},
                )
                continue
            if not isinstance(mapping, dict):
                continue
            owner = mapping.get("workspace_owner_session_id")
            if isinstance(owner, str) and owner:
                reverse_references.setdefault(owner, set()).add(alias_id)

        descendants: set[str] = set()
        pending = [owner_session_id]
        while pending:
            owner = pending.pop()
            for alias_id in reverse_references.get(owner, ()):
                if alias_id == owner_session_id or alias_id in descendants:
                    continue
                descendants.add(alias_id)
                pending.append(alias_id)
        return descendants

    def _invalidate_descendant_aliases(self, owner_session_id: str) -> None:
        """Remove child binding records after explicit owner cleanup."""
        for alias_id in self._alias_ids_for_owner(owner_session_id):
            self._mapping_path(alias_id).unlink(missing_ok=True)
            self._cache.pop(alias_id, None)
