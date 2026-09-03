"""GrepTool 单元测试：正则内容搜索 + 截断 + 二进制跳过。

测试缝 2（见 spec）：用 LocalSubprocessSandbox 做后端，构造 tool_call dict 喂给
ToolExecutor.execute()，断言 ToolResult 形状。复用 test_coding_tools.py 的风格。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.tooling import (
    ErrorCode,
    ToolExecutor,
    ToolRegistry,
    ToolSideEffect,
)
from agent_harness.tools import GrepTool


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


@pytest.fixture
def registry(sandbox: LocalSubprocessSandbox) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(GrepTool(sandbox))
    return reg


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry)


def _tool_call(args: dict, call_id: str = "test_call") -> dict:
    return {"id": call_id, "name": "grep", "args": args}


class TestGrepSideEffect:
    def test_side_effect_is_read_only(self, sandbox: LocalSubprocessSandbox):
        assert GrepTool(sandbox).side_effect == ToolSideEffect.READ_ONLY


class TestGrepHappyPath:
    @pytest.mark.asyncio
    async def test_basic_regex_search(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """基础正则搜索：匹配返回 path/line_number/line。"""
        sandbox.write_text("f.py", "def foo():\n    return 1\n")

        result = await executor.execute(
            _tool_call({"pattern": r"def (\w+)"})
        )

        assert result.result.ok is True
        matches = result.result.data["matches"]
        assert len(matches) == 1
        assert matches[0]["path"] == "f.py"
        assert matches[0]["line_number"] == 1
        assert "def foo" in matches[0]["line"]

    @pytest.mark.asyncio
    async def test_multi_file_multi_match(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """多文件多匹配：行号正确、line 是匹配行全文。"""
        sandbox.write_text("a.py", "import os\nimport sys\n")
        sandbox.write_text("b.py", "import re\n")

        result = await executor.execute(
            _tool_call({"pattern": r"^import"})
        )

        assert result.result.ok is True
        matches = result.result.data["matches"]
        assert result.result.data["count"] == 3
        # 每条都有 path/line_number/line
        for m in matches:
            assert "path" in m
            assert "line_number" in m
            assert "line" in m
            assert m["line"].startswith("import")

    @pytest.mark.asyncio
    async def test_include_filter(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """include 过滤：只有 .py 文件被搜索。"""
        sandbox.write_text("a.py", "target line\n")
        sandbox.write_text("b.txt", "target line\n")

        result = await executor.execute(
            _tool_call({"pattern": "target", "include": "*.py"})
        )

        assert result.result.ok is True
        paths = [m["path"] for m in result.result.data["matches"]]
        assert "a.py" in paths
        assert all(not p.endswith(".txt") for p in paths)

    @pytest.mark.asyncio
    async def test_path_subtree_limit(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """path 子树限定：只在指定子目录搜索。"""
        sandbox.write_text("src/a.py", "findme\n")
        sandbox.write_text("other/b.py", "findme\n")

        result = await executor.execute(
            _tool_call({"pattern": "findme", "path": "src"})
        )

        assert result.result.ok is True
        paths = [m["path"] for m in result.result.data["matches"]]
        assert all("src/" in p for p in paths)
        assert all("other/" not in p for p in paths)


class TestGrepTruncation:
    @pytest.mark.asyncio
    async def test_max_results_truncation(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """匹配数 > max_results → 截断，truncated=True。"""
        lines = "\n".join(f"match_{i}" for i in range(150))
        sandbox.write_text("big.py", lines + "\n")

        result = await executor.execute(
            _tool_call({"pattern": "match_", "max_results": 50})
        )

        assert result.result.ok is True
        assert result.result.data["count"] == 50
        assert result.result.data["truncated"] is True


class TestGrepErrors:
    @pytest.mark.asyncio
    async def test_bad_regex(self, executor: ToolExecutor):
        """坏正则 → INVALID_ARGUMENT。"""
        result = await executor.execute(
            _tool_call({"pattern": "[unclosed"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT

    @pytest.mark.asyncio
    async def test_empty_workspace(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """空 workspace → count=0, matches=[], truncated=False。"""
        result = await executor.execute(
            _tool_call({"pattern": "anything"})
        )

        assert result.result.ok is True
        assert result.result.data["count"] == 0
        assert result.result.data["matches"] == []
        assert result.result.data["truncated"] is False

    @pytest.mark.asyncio
    async def test_binary_file_skipped(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """二进制文件（含不可解码字节）被跳过，不影响其他文件匹配。"""
        # 写一个合法 UTF-8 文件 + 一个纯二进制文件
        sandbox.write_text("good.py", "target_line\n")
        binary_path = sandbox._resolve_within_workspace("bad.bin")
        binary_path.write_bytes(b"\x80\x81\x82\xff\xfe" + b"target_line\n")

        result = await executor.execute(
            _tool_call({"pattern": "target_line"})
        )

        assert result.result.ok is True
        # 只有好文件被匹配到，二进制文件被跳过没崩溃
        paths = [m["path"] for m in result.result.data["matches"]]
        assert "good.py" in paths
        assert "bad.bin" not in paths

    @pytest.mark.asyncio
    async def test_wrong_arg_type(self, executor: ToolExecutor):
        """参数类型错 → INVALID_ARGUMENT。"""
        result = await executor.execute(
            _tool_call({"pattern": 123})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT
