"""#1048 AC5/AC6：本地 REST 显式导出工作区（副本 → 全新宿主目标目录）。

设计（见 ADR-0054）：

- **AC5**：`POST /api/sessions/{id}/workspace/export` 显式触发；请求必须带
  `host_target`（**绝对**宿主目标路径，非绝对即 422）。来源可信闸沿用既有
  host-directory API 口径——`web.projects.require_trusted_origin` 只判定请求
  来源（本机/可信 Origin，ADR-0025 D1(b) / ADR-0028 D2），**不是路径级授权**；
  接受任意绝对路径与 `GET /api/host/dirs` 一致（协调员裁决 2026-10-14），故
  **不设** `Settings.workspace_dir` 根限制。不新增 UI。
- **AC6**：导出是**显式副本**，只能发布到**全新**宿主目标目录——已存在即拒绝，
  绝不覆盖。导出树拒绝绝对路径 / `..` 穿越 / 不安全特殊项（设备、FIFO、socket），
  符号链接不写穿新目标、硬链接按内容复制（不保留对象身份）——外部 canary 不受影响。

不引入第二套内核：目标物化复用各后端的 `export_to_host`（Docker 走 tar 快照，
本地走目录复制）。
"""

from __future__ import annotations

import os
import shutil
import stat
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from agent_harness.sandbox.paths import is_absolute_path
from agent_harness.sandbox.registry import IMPORT_COMPLETE_MARKER
from agent_harness.web.projects import require_trusted_origin
from agent_harness.web.workspace_files import session_sandbox

if TYPE_CHECKING:
    from fastapi import FastAPI


class ExportRequest(BaseModel):
    """`POST .../workspace/export`：显式导出，必须给宿主目标路径。"""

    host_target: str = Field(
        ..., description="全新宿主目标目录（已存在即拒绝，不覆盖）"
    )


def _require_absolute_target(target: str) -> None:
    """宿主目标必须是**本平台绝对路径**（AC5）。

    与 `web.host_dirs._directory_listing` / `web.projects._require_absolute_path`
    同款：必须在任何 `realpath`/`exists` 之前挡住形态——`realpath(".")` 会解析成进程
    当前工作目录，把一次笔误变成"导出到服务器碰巧启动的目录"。**不设**根限制：既有
    host-directory API 也接受任意绝对路径（来源闸见 `require_trusted_origin`）。
    """
    if not is_absolute_path(target):
        raise HTTPException(status_code=422, detail="host_target 必须是绝对路径")


def _reject_unsafe_path(name: str) -> None:
    """路径串必须安全（AC6）：拒绝绝对路径 / `..` 穿越 / 空名。

    同时用于导出树里的相对成员路径与符号链接的**链接文本**——两者都是待物化的
    路径串，绝对 / 穿越 / 空都会写穿新目标，故同一判据。
    """
    if not name or name in (".", ".."):
        raise ValueError(f"路径非法：{name!r}")
    pure = name.replace("\\", "/")
    if pure.startswith("/") or (len(pure) > 1 and pure[1] == ":"):
        raise ValueError(f"路径为绝对路径，拒绝：{name!r}")
    for part in pure.split("/"):
        if part == "..":
            raise ValueError(f"路径含穿越，拒绝：{name!r}")


def _iter_export_members(source: Path):
    """产出 `(rel, entry)` 供"先全量校验、再物化"两阶段共用（AC6）。"""
    for entry in sorted(source.rglob("*"), key=lambda p: p.as_posix()):
        yield entry.relative_to(source).as_posix(), entry


def _validate_export_members(source: Path) -> None:
    """AC6：先校验**全部**成员再建目标目录。

    任一成员不安全（绝对/`..`/逃逸链接文本/特殊项）即整体拒绝，且此时目标目录
    尚未创建——不会留下半个/空的 `dest` 让用户重试时撞 409（"全新目录"语义）。
    """
    for rel, entry in _iter_export_members(source):
        _reject_unsafe_path(rel)
        if entry.is_symlink():
            # 链接文本同样是待物化的路径串：绝对/穿越即写穿新目标。
            _reject_unsafe_path(os.readlink(entry))
            continue
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
            continue
        raise ValueError(f"含不安全特殊项，拒绝：{rel}")


def build_export_archive(source_root: Path, dest: Path) -> None:
    """把 `source_root` 的**副本**发布到全新目录 `dest`（AC6）。

    - `dest` 必须不存在（已存在即拒绝，绝不覆盖/合并）；
    - **先全量校验**成员路径（绝对 / `..` / 特殊项拒绝），通过后才建目录；
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
    _validate_export_members(source)
    target.mkdir(parents=True)
    for rel, entry in _iter_export_members(source):
        if entry.is_symlink():
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.symlink_to(os.readlink(entry))
            continue
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode):
            (target / rel).mkdir(parents=True, exist_ok=True)
        elif stat.S_ISREG(mode):
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            # 按内容复制成独立对象：不保留硬链接身份，外部 canary 不受写穿。
            shutil.copyfile(entry, out)
        else:  # pragma: no cover —— 已在 _validate_export_members 拦下
            raise ValueError(f"含不安全特殊项，拒绝：{rel}")


def register_workspace_export_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把工作区导出路由挂到既有 app（`create_app` 里一行调用的接入面）。"""

    @app.post("/api/sessions/{session_id}/workspace/export")
    async def export_workspace(
        session_id: str,
        body: ExportRequest,
        _: None = Depends(require_trusted_origin),
    ) -> dict[str, str]:
        """显式导出工作区到全新宿主目录（AC5/AC6）。

        错误矩阵：缺 `host_target` → 422（Pydantic）/ 非绝对路径 → 422 /
        目标已存在 → 409（纯请求校验，不依赖会话存在）/ 会话不存在或无 workspace
        映射 → 404（复用 `session_sandbox` 既有语义）/ 并发建同一目标 → 409 /
        导出树含不安全成员 → 422 / 服务端故障（磁盘/权限）→ 500。
        """
        from tempfile import mkdtemp

        target = Path(body.host_target)
        _require_absolute_target(body.host_target)
        if target.exists():
            raise HTTPException(
                status_code=409,
                detail=f"导出目标已存在，拒绝覆盖：{target}",
            )
        # 会话不存在 / 无 workspace 映射 → 404（不吞成"导出空目录"），只导出真实工作区。
        sandbox = await session_sandbox(
            app.state.agent, session_id, validate_session_id
        )
        staging = Path(mkdtemp(prefix="export-staging-"))
        try:
            # 错误映射刻意**精确**，不宽泛：
            # - `ValueError` → 422：导出树成员不安全（绝对/`..`/特殊项）是请求问题；
            # - `tarfile.TarError` → 422：Docker 归档 `extractall(filter="data")`
            #   拒收逃逸成员；
            # - `FileExistsError` → 409：与 exists() 快检竞态的并发建同一目标；
            # - 其余（`shutil.Error` 含 ENOSPC/权限、`OSError` 等）→ 500：
            #   服务端故障不是请求问题，不吞成 422。
            try:
                await anyio.to_thread.run_sync(sandbox.export_to_host, staging)
                # harness 内部簿记（导入完成标记）不是用户工作产物，不进导出树。
                (staging / IMPORT_COMPLETE_MARKER).unlink(missing_ok=True)
                await anyio.to_thread.run_sync(build_export_archive, staging, target)
            except FileExistsError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except (ValueError, tarfile.TarError) as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {"status": "ok", "target": str(target)}
