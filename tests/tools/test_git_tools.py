"""GitStatusTool + GitDiffTool 单元测试：只读 git 查询 + ADR-0002。

测试缝 2（见 spec）：用 LocalSubprocessSandbox 做后端，在 tmp_path 里 git init
构造真实仓库状态，构造 tool_call dict 喂给 ToolExecutor.execute()，断言 ToolResult。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.tooling import (
    ToolExecutor,
    ToolRegistry,
    ToolSideEffect,
)
from agent_harness.tools import GitDiffTool, GitStatusTool


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


@pytest.fixture
def registry(sandbox: LocalSubprocessSandbox) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(GitStatusTool(sandbox))
    reg.register(GitDiffTool(sandbox))
    return reg


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry)


def _tool_call(name: str, args: dict, call_id: str = "test_call") -> dict:
    return {"id": call_id, "name": name, "args": args}


def _init_git_repo(sandbox: LocalSubprocessSandbox) -> None:
    """在 workspace 里 git init + 配 user（防 git 报错）。"""
    sandbox.exec("git init -q")
    sandbox.exec("git config user.email test@test.com")
    sandbox.exec("git config user.name test")


class TestGitStatusSideEffect:
    def test_side_effect_is_read_only(self, sandbox: LocalSubprocessSandbox):
        assert GitStatusTool(sandbox).side_effect == ToolSideEffect.READ_ONLY

    def test_git_diff_side_effect_is_read_only(self, sandbox: LocalSubprocessSandbox):
        assert GitDiffTool(sandbox).side_effect == ToolSideEffect.READ_ONLY


class TestGitStatus:
    @pytest.mark.asyncio
    async def test_status_shows_changes(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """有改动时 git status 返回 porcelain 行，exit_code=0。"""
        _init_git_repo(sandbox)
        sandbox.write_text("new.py", "print('hi')")

        result = await executor.execute(_tool_call("git_status", {}))

        assert result.result.ok is True
        assert result.result.data["exit_code"] == 0
        assert "new.py" in result.result.data["stdout"]

    @pytest.mark.asyncio
    async def test_status_clean_repo(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """干净仓库（已提交）→ stdout 为空或无改动行。"""
        _init_git_repo(sandbox)
        sandbox.write_text("committed.py", "x")
        sandbox.exec("git add . && git commit -q -m init")

        result = await executor.execute(_tool_call("git_status", {}))

        assert result.result.ok is True
        assert result.result.data["stdout"].strip() == ""

    @pytest.mark.asyncio
    async def test_non_git_repo_exit_nonzero_but_ok_true(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """非 git 仓库 → exit_code 非零但 ok=True（ADR-0002 关键断言）。"""
        # 不 git init，干净 tmp_path 不是 git 仓库

        result = await executor.execute(_tool_call("git_status", {}))

        assert result.result.ok is True
        assert result.result.data["exit_code"] != 0
        assert "not a git repository" in result.result.data["stderr"].lower() or \
               "not a git repo" in result.result.data["stderr"].lower()

    @pytest.mark.asyncio
    async def test_pathspec_filter(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """pathspec 过滤：只看 src/ 下的改动（porcelain 对未跟踪目录折叠成 ?? src/）。"""
        _init_git_repo(sandbox)
        sandbox.write_text("src/a.py", "x")
        sandbox.write_text("other/b.py", "y")

        result = await executor.execute(_tool_call("git_status", {"pathspec": "src/"}))

        assert result.result.ok is True
        assert "src/" in result.result.data["stdout"]
        assert "other/" not in result.result.data["stdout"]


class TestGitDiff:
    @pytest.mark.asyncio
    async def test_diff_shows_unstaged_changes(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """有未暂存改动 → stdout 含 diff 内容。"""
        _init_git_repo(sandbox)
        sandbox.write_text("f.py", "original")
        sandbox.exec("git add . && git commit -q -m init")
        sandbox.write_text("f.py", "modified")

        result = await executor.execute(_tool_call("git_diff", {}))

        assert result.result.ok is True
        assert "-original" in result.result.data["stdout"]
        assert "+modified" in result.result.data["stdout"]

    @pytest.mark.asyncio
    async def test_diff_staged_shows_staged_changes(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """staged=True → 看暂存区差异。"""
        _init_git_repo(sandbox)
        sandbox.write_text("f.py", "original")
        sandbox.exec("git add . && git commit -q -m init")
        sandbox.write_text("f.py", "staged_change")
        sandbox.exec("git add f.py")

        result = await executor.execute(_tool_call("git_diff", {"staged": True}))

        assert result.result.ok is True
        assert "+staged_change" in result.result.data["stdout"]

    @pytest.mark.asyncio
    async def test_diff_non_git_repo_ok_true(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """非 git 仓库 → exit_code 非零但 ok=True（ADR-0002）。"""

        result = await executor.execute(_tool_call("git_diff", {}))

        assert result.result.ok is True
        assert result.result.data["exit_code"] != 0

    @pytest.mark.asyncio
    async def test_diff_path_filter(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """path 过滤：只看指定文件的差异。"""
        _init_git_repo(sandbox)
        sandbox.write_text("a.py", "x")
        sandbox.write_text("b.py", "y")
        sandbox.exec("git add . && git commit -q -m init")
        sandbox.write_text("a.py", "changed_a")
        sandbox.write_text("b.py", "changed_b")

        result = await executor.execute(_tool_call("git_diff", {"path": "a.py"}))

        assert result.result.ok is True
        assert "+changed_a" in result.result.data["stdout"]
        assert "+changed_b" not in result.result.data["stdout"]
