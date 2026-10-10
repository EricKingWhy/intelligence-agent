"""#367 [W-23] 选项 A：git worktree 工具 TDD（先红）。

目录冲突默认自动 worktree 并行隔离（六家成熟产品共识：Copilot/Codex/Cline
Kanban 全是自动换地方，无任务级排队）。
"""
import subprocess
from pathlib import Path

import pytest

from agent_harness.workspace.worktree import (
    WorktreeError,
    create_worktree,
    is_git_repo,
)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """一个最小 git 仓库。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                   check=True, capture_output=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)
    return repo


class TestIsGitRepo:
    def test_git_repo_true(self, git_repo: Path):
        assert is_git_repo(git_repo) is True

    def test_non_repo_false(self, tmp_path: Path):
        assert is_git_repo(tmp_path) is False

    def test_nonexistent_false(self, tmp_path: Path):
        assert is_git_repo(tmp_path / "nope") is False


class TestCreateWorktree:
    def test_non_ascii_repo_path(self, git_repo: Path, tmp_path: Path):
        repo = tmp_path / "仓库"
        git_repo.rename(repo)

        wt = create_worktree(repo, path=tmp_path / "工作树")

        assert wt.is_dir()
        assert (wt / "a.txt").read_text() == "a"

    def test_creates_worktree(self, git_repo: Path, tmp_path: Path):
        wt = create_worktree(git_repo, path=tmp_path / "wt1")
        assert wt.is_dir()
        assert (wt / "a.txt").read_text() == "a"

    def test_auto_path(self, git_repo: Path):
        wt = create_worktree(git_repo)
        assert wt.is_dir()
        assert wt.parent == git_repo.parent or "worktree" in wt.name.lower() \
            or wt != git_repo

    def test_branch_isolated(self, git_repo: Path, tmp_path: Path):
        wt = create_worktree(git_repo, path=tmp_path / "wt2")
        (wt / "b.txt").write_text("b")
        # 主仓库不受影响（新文件只在 worktree）
        assert not (git_repo / "b.txt").exists()

    def test_not_a_repo_raises(self, tmp_path: Path):
        with pytest.raises(WorktreeError):
            create_worktree(tmp_path)

    def test_existing_path_raises(self, git_repo: Path, tmp_path: Path):
        existing = tmp_path / "exists"
        existing.mkdir()
        with pytest.raises(WorktreeError):
            create_worktree(git_repo, path=existing)
