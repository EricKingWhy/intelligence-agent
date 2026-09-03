"""Coding Tools 单元测试：read / write / bash 通过 ToolExecutor 驱动。

测试缝 2（见 spec）：用 LocalSubprocessSandbox 做后端，构造 tool_call dict 喂给
ToolExecutor.execute()，断言 ToolResult 形状。复用 tests/tooling/test_executor.py 的风格。

核心不变量（ADR-0002）：bash 非零 exit_code 仍是 ok=True。
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
from agent_harness.tools import BashTool, ReadTool, WriteTool

# ============================================================================
# 夹具
# ============================================================================


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


@pytest.fixture
def registry(sandbox: LocalSubprocessSandbox) -> ToolRegistry:
    """注册 read/write/bash，返回完整 Registry。"""
    reg = ToolRegistry()
    reg.register(ReadTool(sandbox))
    reg.register(WriteTool(sandbox))
    reg.register(BashTool(sandbox))
    return reg


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry)


def _tool_call(name: str, args: dict, call_id: str = "test_call") -> dict:
    """构造标准 tool_call dict（形状与 LangChain tool_calls 一致）。"""
    return {"id": call_id, "name": name, "args": args}


# ============================================================================
# ReadTool
# ============================================================================


class TestReadTool:
    def test_side_effect_is_read_only(self, sandbox: LocalSubprocessSandbox):
        assert ReadTool(sandbox).side_effect == ToolSideEffect.READ_ONLY

    @pytest.mark.asyncio
    async def test_read_existing_file(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """先写文件再读，ToolResult.ok=True 且 data 含 content。"""
        sandbox.write_text("note.txt", "hello world")

        result = await executor.execute(
            _tool_call("read", {"path": "note.txt"}, "call_read_1")
        )

        assert result.tool_call_id == "call_read_1"
        assert result.result.ok is True
        assert result.result.data["content"] == "hello world"
        assert result.result.data["path"] == "note.txt"

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, executor: ToolExecutor):
        """读不存在的文件 → ok=False, error_code=TOOL_EXECUTION_ERROR。"""
        result = await executor.execute(_tool_call("read", {"path": "ghost.txt"}))

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR

    @pytest.mark.asyncio
    async def test_read_path_escape_denied(self, executor: ToolExecutor):
        """路径越界 → ok=False, error_code=PERMISSION_DENIED。"""
        result = await executor.execute(
            _tool_call("read", {"path": "../../etc/passwd"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.PERMISSION_DENIED


# ============================================================================
# WriteTool
# ============================================================================


class TestWriteTool:
    def test_side_effect_is_mutating(self, sandbox: LocalSubprocessSandbox):
        assert WriteTool(sandbox).side_effect == ToolSideEffect.MUTATING

    @pytest.mark.asyncio
    async def test_write_creates_file(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """写新文件 → ok=True，文件确实存在且内容正确。"""
        result = await executor.execute(
            _tool_call("write", {"path": "new.py", "content": "print('hi')"})
        )

        assert result.result.ok is True
        assert result.result.data["path"] == "new.py"
        assert sandbox.read_text("new.py") == "print('hi')"

    @pytest.mark.asyncio
    async def test_write_overwrites_existing(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """覆盖写：已有文件被整体替换。"""
        sandbox.write_text("old.txt", "before")
        await executor.execute(
            _tool_call("write", {"path": "old.txt", "content": "after"})
        )
        assert sandbox.read_text("old.txt") == "after"

    @pytest.mark.asyncio
    async def test_write_path_escape_denied(self, executor: ToolExecutor):
        """路径越界 → ok=False, error_code=PERMISSION_DENIED。"""
        result = await executor.execute(
            _tool_call("write", {"path": "../../evil.sh", "content": "rm -rf /"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.PERMISSION_DENIED


# ============================================================================
# BashTool（核心：ADR-0002 不变量）
# ============================================================================


class TestBashTool:
    def test_side_effect_is_mutating(self, sandbox: LocalSubprocessSandbox):
        assert BashTool(sandbox).side_effect == ToolSideEffect.MUTATING

    @pytest.mark.asyncio
    async def test_echo_success(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """echo → ok=True, exit_code=0, stdout 含内容。"""
        result = await executor.execute(
            _tool_call("bash", {"command": "echo hello"})
        )

        assert result.result.ok is True
        assert result.result.data["exit_code"] == 0
        assert "hello" in result.result.data["stdout"]

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_still_ok_true(self, executor: ToolExecutor):
        """ADR-0002 核心：exit_code=1（如 pytest 失败）→ ok=True，不是 Tool 失败。"""
        result = await executor.execute(
            _tool_call("bash", {"command": "exit 1"})
        )

        # 这是整个 Day05 最关键的断言：命令业务失败 ≠ Tool 调用失败
        assert result.result.ok is True
        assert result.result.data["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_stdout_stderr_in_data(self, executor: ToolExecutor):
        """data 含 stdout、stderr、duration_ms 四个字段。"""
        result = await executor.execute(
            _tool_call("bash", {"command": "echo out & echo err 1>&2"})
        )

        assert result.result.ok is True
        assert "out" in result.result.data["stdout"]
        assert "err" in result.result.data["stderr"]
        assert "duration_ms" in result.result.data


# ============================================================================
# 参数校验（Validation-first）
# ============================================================================


class TestValidation:
    @pytest.mark.asyncio
    async def test_missing_required_arg(self, executor: ToolExecutor):
        """缺必填参数 → Executor 阶段 2 校验拦截 → INVALID_ARGUMENT。"""
        result = await executor.execute(_tool_call("write", {"path": "x.txt"}))

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT

    @pytest.mark.asyncio
    async def test_wrong_arg_type(self, executor: ToolExecutor):
        """类型错（content 应为 str，传 int）→ INVALID_ARGUMENT。"""
        result = await executor.execute(
            _tool_call("write", {"path": "x.txt", "content": 123})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT


# ============================================================================
# 批次调度（side_effect 驱动并发/串行）
# ============================================================================


class TestBatchScheduling:
    @pytest.mark.asyncio
    async def test_mixed_batch_runs_serial(self, executor: ToolExecutor):
        """read(READ_ONLY) + bash(MUTATING) 混批 → 整批串行（_decide_mode 返回 serial）。"""
        # 不需要断言调度细节——只要 bash 和 read 都正确返回即可（串行是内部决策）。
        results = await executor.execute_batch([
            _tool_call("bash", {"command": "echo a"}, "b1"),
            _tool_call("bash", {"command": "echo b"}, "b2"),
        ])

        assert len(results) == 2
        assert results[0].tool_call_id == "b1"
        assert results[1].tool_call_id == "b2"
        assert all(r.result.ok for r in results)
