"""git worktree 工具（#367 [W-23] 选项 A）。

目录冲突默认自动 worktree 并行隔离——六家成熟产品共识（Copilot app：
"run multiple isolated agent sessions simultaneously… without conflicts"；
Codex："Each agent works on an isolated copy of your code"；Cline Kanban：
"run multiple coding agents in parallel with isolated git worktrees"）。
任务级排队六家全无，故 worktree 是默认，排队只是次选项。

本模块只做 worktree 的创建/判定；"何时用 worktree 代替排队"的决策在
session 创建层（`on_conflict` 参数）。
"""
from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger("agent_harness.workspace.worktree")


class WorktreeError(RuntimeError):
    """worktree 创建失败（非 git 仓库、路径已存在、git 命令失败等）。"""


def is_git_repo(path: Path) -> bool:
    """`path` 是否是 git 仓库（含 worktree 自身）。"""
    if not path.is_dir():
        return False
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _git_toplevel(path: Path) -> Path:
    """仓库顶层目录（worktree 里调用返回主仓库顶层）。"""
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise WorktreeError(f"不是 git 仓库：{path}")
    return Path(proc.stdout.strip())


def create_worktree(repo_path: Path, *, path: Path | None = None,
                    branch: str | None = None) -> Path:
    """为 `repo_path` 创建一个隔离的 git worktree，返回 worktree 路径。

    - `path` 未给时自动生成：`<toplevel-parent>/worktrees/<repo>-<8位随机>`。
    - `branch` 未给时自动生成：`agent/<8位随机>`（新分支，不污染现有分支）。
    - 非 git 仓库 / 目标路径已存在 / git 命令失败 → `WorktreeError`。
    """
    repo_path = Path(repo_path)
    if not is_git_repo(repo_path):
        raise WorktreeError(f"不是 git 仓库，无法创建 worktree：{repo_path}")
    toplevel = _git_toplevel(repo_path)

    suffix = uuid.uuid4().hex[:8]
    if path is None:
        path = toplevel.parent / "worktrees" / f"{toplevel.name}-{suffix}"
    path = Path(path)
    if path.exists():
        raise WorktreeError(f"worktree 目标路径已存在：{path}")
    if branch is None:
        branch = f"agent/{suffix}"

    # 先确保父目录存在（git worktree add 不会建多级父目录）
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(toplevel), "worktree", "add", "-b", branch,
         str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise WorktreeError(
            f"git worktree add 失败：{proc.stderr.strip() or proc.stdout.strip()}"
        )
    logger.info("worktree 已创建：%s（分支 %s）", path, branch)
    return path
