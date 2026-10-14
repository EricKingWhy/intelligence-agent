"""#1048 AC5/AC6：本地 REST 显式导出工作区（副本 → 全新宿主目标目录）。

设计（见 ADR-0054）：

- **AC5**：`POST /api/sessions/{id}/workspace/export` 显式触发；请求必须带
  `host_target`（宿主目标路径）。路径授权**沿用既有 host-directory 边界**
  （`Settings.workspace_dir` 为宿主工作区根；越出即 403）。不新增 UI。
- **AC6**：导出是**显式副本**，只能发布到**全新**宿主目标目录——已存在即拒绝，
  绝不覆盖。归档构建拒绝绝对路径 / `..` 穿越 / 不安全特殊项（设备、FIFO、socket），
  符号链接不写穿新目标、硬链接按内容复制（不保留对象身份）——外部 canary 不受影响。

不引入第二套内核：目标物化复用各后端的 `export_to_host`（Docker 走 tar 快照，
本地走目录复制）。
"""

from __future__ import annotations

import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from agent_harness.web.projects import require_trusted_origin

if TYPE_CHECKING:
    from fastapi import FastAPI


class ExportRequest(BaseModel):
    """`POST .../workspace/export`：显式导出，必须给宿主目标路径。"""

    host_target: str = Field(
        ..., description="全新宿主目标目录（已存在即拒绝，不覆盖）"
    )


def _authorize_host_target(target: Path, workspace_dir: str) -> None:
    """宿主目标必须在既有 host-directory 边界内（AC5 复用来源闸语义）。

    边界根 = `Settings.workspace_dir`。越出即 403（不 mkdir、不泄漏路径）。
    """
    root = Path(workspace_dir).resolve()
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise HTTPException(
            status_code=403,
            detail=f"导出目标越出宿主工作区边界：{resolved}",
        )


def _reject_unsafe_member(name: str) -> None:
    """归档成员名必须安全（AC6）：拒绝绝对路径 / `..` 穿越 / 空名。"""
    if not name or name in (".", ".."):
        raise ValueError(f"归档成员名非法：{name!r}")
    pure = name.replace("\\", "/")
    if pure.startswith("/") or (len(pure) > 1 and pure[1] == ":"):
        raise ValueError(f"归档成员名为绝对路径，拒绝：{name!r}")
    for part in pure.split("/"):
        if part == "..":
            raise ValueError(f"归档成员名含路径穿越，拒绝：{name!r}")


def build_export_archive(source_root: Path, dest: Path) -> None:
    """把 `source_root` 的**副本**发布到全新目录 `dest`（AC6）。

    - `dest` 必须不存在（已存在即拒绝，绝不覆盖/合并）；
    - 遍历源树，逐项校验成员名（绝对 / `..` / 特殊项拒绝）；
    - 符号链接按**链接文本**原样重建（不解析、不写穿目标）；硬链接按**内容**复制
      成独立 inode（不保留对象身份，故与源/外部 canary 无共享）；
    - 设备 / FIFO / socket 等不安全特殊项拒绝。
    """
    source = Path(source_root)
    target = Path(dest)
    if target.exists():
        raise FileExistsError(f"导出目标已存在，拒绝覆盖：{target}")
    if not source.is_dir():
        # 无源（如会话尚无工作区）⇒ 空导出目录，语义仍是"全新目录副本"。
        target.mkdir(parents=True)
        return
    target.mkdir(parents=True)
    for entry in sorted(source.rglob("*"), key=lambda p: p.as_posix()):
        rel = entry.relative_to(source).as_posix()
        _reject_unsafe_member(rel)
        if entry.is_symlink():
            link = os_readlink(entry)
            _reject_unsafe_member(link)
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.symlink_to(link)
            continue
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode):
            (target / rel).mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(mode):
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            # 按内容复制成独立对象：不保留硬链接身份，外部 canary 不受写穿。
            shutil.copyfile(entry, out)
        else:
            raise ValueError(f"归档含不安全特殊项，拒绝：{rel}")


def os_readlink(path: Path) -> str:
    import os

    return os.readlink(path)


def _materialize_sandbox_workspace(sandbox: Any, staging: Path) -> None:
    """把 sandbox 工作区物化到宿主暂存目录（导出源），不走宿主 /workspace 直连。"""
    exporter = getattr(sandbox, "export_to_host", None)
    if exporter is not None:
        exporter(staging)
        return
    root = getattr(sandbox, "workspace_root", None)
    if isinstance(root, Path) and root.is_dir():
        shutil.copytree(root, staging, dirs_exist_ok=True)


def register_workspace_export_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把工作区导出路由挂到既有 app（`create_app` 里一行调用的接入面）。"""

    async def _maybe_sandbox(session_id: str) -> Any:
        from agent_harness.web.workspace_files import _session_sandbox

        try:
            return await _session_sandbox(
                app.state.agent, session_id, validate_session_id
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise

    @app.post("/api/sessions/{session_id}/workspace/export")
    async def export_workspace(
        session_id: str,
        body: ExportRequest,
        _: None = Depends(require_trusted_origin),
    ) -> dict[str, str]:
        """显式导出工作区到全新宿主目录（AC5/AC6）。

        错误矩阵：缺 `host_target` → 422（Pydantic）/ 越出宿主边界 → 403 /
        目标已存在 → 409 / 归档含不安全成员 → 422。
        """
        from tempfile import mkdtemp

        settings = app.state.agent.settings
        target = Path(body.host_target)
        _authorize_host_target(target, settings.workspace_dir)
        if target.exists():
            raise HTTPException(
                status_code=409,
                detail=f"导出目标已存在，拒绝覆盖：{target}",
            )
        sandbox = await _maybe_sandbox(session_id)
        staging = Path(mkdtemp(prefix="export-staging-"))
        try:
            if sandbox is not None:
                await anyio.to_thread.run_sync(
                    _materialize_sandbox_workspace, sandbox, staging
                )
            try:
                await anyio.to_thread.run_sync(build_export_archive, staging, target)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {"status": "ok", "target": str(target)}
