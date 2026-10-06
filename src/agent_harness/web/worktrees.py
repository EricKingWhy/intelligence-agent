"""#367 [W-23] 选项 A：git worktree 的 minimal REST 面（独立 router）。

目录冲突默认自动 worktree 并行隔离（六家成熟产品共识）——前端在租约
`acquire` 返回 queued（目录被占）时，调本面创建 worktree，再用 worktree
路径建会话/切目录。"排队等"仍是次选项（既有租约 API 不动）。

错误码口径与 task_lease 同型：形状非法 422（非 git 仓库、路径不存在），
worktree 创建失败 500（git 命令失败是环境问题，不是请求形状问题）。
"""
from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from fastapi import HTTPException
from pydantic import BaseModel

from agent_harness.workspace.worktree import (
    WorktreeError,
    create_worktree,
    is_git_repo,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


class WorktreeCreateRequest(BaseModel):
    """`repo_path`：要开 worktree 的 git 仓库（任意子目录，自动找顶层）。

    `path` 可选：不给则自动生成 `<toplevel-parent>/worktrees/<repo>-<随机>`。
    `branch` 可选：不给则自动生成 `agent/<随机>`。
    """

    repo_path: str
    path: str | None = None
    branch: str | None = None


def register_worktree_routes(app: FastAPI) -> None:
    """把 worktree 路由挂到既有 app（app.py 侧一行调用）。"""

    @app.post("/api/worktrees")
    async def create_worktree_endpoint(req: WorktreeCreateRequest) -> dict:
        """为 git 仓库创建一个隔离 worktree，返回其路径。"""
        repo_path = Path(req.repo_path)
        if not repo_path.is_dir():
            raise HTTPException(
                status_code=422, detail=f"目录不存在：{req.repo_path}")
        if not is_git_repo(repo_path):
            raise HTTPException(
                status_code=422, detail=f"不是 git 仓库：{req.repo_path}")
        try:
            wt = await anyio.to_thread.run_sync(
                partial(
                    create_worktree,
                    repo_path,
                    path=Path(req.path) if req.path else None,
                    branch=req.branch,
                )
            )
        except WorktreeError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return {"worktree_path": str(wt)}
