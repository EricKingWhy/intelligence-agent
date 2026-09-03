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
from datetime import datetime, timezone
from pathlib import Path

from agent_harness.sandbox.base import Sandbox
from agent_harness.sandbox.local import LocalSubprocessSandbox


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

    def create(self, session_id: str) -> Sandbox:
        """为新 session 创建 Sandbox，持久化映射，返回已 ensure_started 的 Sandbox。

        如果映射已存在（同 session_id 再次 create），直接从缓存或重建返回已有 Sandbox。
        """
        if session_id in self._cache:
            return self._cache[session_id]

        mapping = self._build_mapping(session_id)
        workspace_root = Path(mapping["workspace_root"])
        workspace_root.mkdir(parents=True, exist_ok=True)

        sandbox = self._instantiate_sandbox(mapping)
        sandbox.ensure_started()

        self._write_mapping(session_id, mapping)
        self._cache[session_id] = sandbox
        return sandbox

    def get(self, session_id: str) -> Sandbox:
        """查回 session 的 Sandbox 实例（必要时从 JSON 重建并 ensure_started）。"""
        if session_id in self._cache:
            return self._cache[session_id]

        mapping = self._read_mapping(session_id)
        if mapping is None:
            raise KeyError(
                f"Session '{session_id}' 没有对应的 workspace 映射记录。"
            )

        sandbox = self._instantiate_sandbox(mapping)
        sandbox.ensure_started()

        self._cache[session_id] = sandbox
        return sandbox

    def exists(self, session_id: str) -> bool:
        """检查 session 是否有映射记录。"""
        return self._mapping_path(session_id).exists()

    def stop(self, session_id: str) -> None:
        """停止 session 的 Sandbox。幂等。"""
        sandbox = self._cache.get(session_id)
        if sandbox is not None:
            sandbox.stop()

    def delete(self, session_id: str) -> None:
        """彻底清理 session 的 Sandbox 和映射。幂等。"""
        self.stop(session_id)
        self._cache.pop(session_id, None)
        mapping_file = self._mapping_path(session_id)
        if mapping_file.exists():
            mapping_file.unlink()

    # —— 内部方法 ——

    def _build_mapping(self, session_id: str) -> dict:
        """构造新 session 的映射字典。"""
        workspace_root = self._workspaces_dir / session_id
        return {
            "session_id": session_id,
            "backend": self._backend,
            "workspace_root": str(workspace_root),
            "container_name": None,
            "volume_name": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _instantiate_sandbox(self, mapping: dict) -> Sandbox:
        """根据映射字典重建 Sandbox 实例。"""
        backend = mapping.get("backend", "local")
        workspace_root = Path(mapping["workspace_root"])

        if backend == "local":
            return LocalSubprocessSandbox(workspace_root=workspace_root)

        if backend == "docker":
            # Ticket D 实现 Docker 后端时填充此分支。
            # 目前 DockerSandbox 需要确定性命名，由 Ticket D 接入。
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
        self._mapping_path(session_id).write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_mapping(self, session_id: str) -> dict | None:
        path = self._mapping_path(session_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
