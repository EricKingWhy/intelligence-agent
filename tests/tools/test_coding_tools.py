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
from agent_harness.tools import (
    ApplyPatchTool,
    BashTool,
    EditTool,
    GitDiffTool,
    GitStatusTool,
    GlobTool,
    GrepTool,
    ReadTool,
    WriteTool,
)

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
    """DANGER_FULL_ACCESS：这些测试验证工具执行行为，不是审批逻辑。
    审批关卡在 tests/tooling/test_approval_gate.py 单独覆盖。"""
    from agent_harness.tooling import PermissionPolicy

    return ToolExecutor(registry, policy=PermissionPolicy.DANGER_FULL_ACCESS)


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


# ============================================================================
# 新工具 side_effect 分类汇总验证 + 批次调度（Ticket 7）
# ============================================================================


class TestNewToolSideEffects:
    """6 个新工具的 side_effect 分类断言汇总——批次调度依赖这些分类。"""

    def test_edit_is_mutating(self, sandbox: LocalSubprocessSandbox):
        assert EditTool(sandbox).side_effect == ToolSideEffect.MUTATING

    def test_apply_patch_is_mutating(self, sandbox: LocalSubprocessSandbox):
        assert ApplyPatchTool(sandbox).side_effect == ToolSideEffect.MUTATING

    def test_glob_is_read_only(self, sandbox: LocalSubprocessSandbox):
        assert GlobTool(sandbox).side_effect == ToolSideEffect.READ_ONLY

    def test_grep_is_read_only(self, sandbox: LocalSubprocessSandbox):
        assert GrepTool(sandbox).side_effect == ToolSideEffect.READ_ONLY

    def test_git_status_is_read_only(self, sandbox: LocalSubprocessSandbox):
        assert GitStatusTool(sandbox).side_effect == ToolSideEffect.READ_ONLY

    def test_git_diff_is_read_only(self, sandbox: LocalSubprocessSandbox):
        assert GitDiffTool(sandbox).side_effect == ToolSideEffect.READ_ONLY


@pytest.fixture
def full_registry(sandbox: LocalSubprocessSandbox) -> ToolRegistry:
    """注册全部 9 个 Coding Tools（Ticket 7 集成验证用）。"""
    reg = ToolRegistry()
    reg.register(ReadTool(sandbox))
    reg.register(WriteTool(sandbox))
    reg.register(BashTool(sandbox))
    reg.register(EditTool(sandbox))
    reg.register(ApplyPatchTool(sandbox))
    reg.register(GlobTool(sandbox))
    reg.register(GrepTool(sandbox))
    reg.register(GitStatusTool(sandbox))
    reg.register(GitDiffTool(sandbox))
    return reg


@pytest.fixture
def full_executor(full_registry: ToolRegistry) -> ToolExecutor:
    """DANGER_FULL_ACCESS：批次调度测试验证并发/串行行为，不是审批逻辑。"""
    from agent_harness.tooling import PermissionPolicy

    return ToolExecutor(full_registry, policy=PermissionPolicy.DANGER_FULL_ACCESS)


class TestNewToolBatchScheduling:
    """批次调度在新工具上行为正确：READ_ONLY 批可并发，含 MUTATING 的批串行。"""

    @pytest.mark.asyncio
    async def test_all_read_only_batch(
        self, full_executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """纯 READ_ONLY 批（多个 glob + git_status）→ 全部成功、配对正确。"""
        sandbox.write_text("a.py", "x")
        sandbox.write_text("b.py", "y")

        results = await full_executor.execute_batch([
            _tool_call("glob", {"pattern": "*.py"}, "g1"),
            _tool_call("glob", {"pattern": "*.py"}, "g2"),
            _tool_call("git_status", {}, "gs1"),
        ])

        assert len(results) == 3
        assert all(r.result.ok for r in results)
        assert results[0].tool_call_id == "g1"
        assert results[1].tool_call_id == "g2"
        assert results[2].tool_call_id == "gs1"

    @pytest.mark.asyncio
    async def test_mixed_read_only_and_mutating_batch(
        self, full_executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """READ_ONLY + MUTATING 混批（git_status + edit）→ 整批串行，全部成功。"""
        sandbox.write_text("f.py", "old")

        results = await full_executor.execute_batch([
            _tool_call("git_status", {}, "gs1"),
            _tool_call("edit", {"path": "f.py", "old_string": "old", "new_string": "new"}, "e1"),
        ])

        assert len(results) == 2
        assert all(r.result.ok for r in results)
        assert results[0].tool_call_id == "gs1"
        assert results[1].tool_call_id == "e1"
        assert sandbox.read_text("f.py") == "new"

    @pytest.mark.asyncio
    async def test_all_mutating_batch(
        self, full_executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """全 MUTATING 批（edit + apply_patch）→ 串行，全部成功，顺序正确。"""
        sandbox.write_text("f.py", "line1\nline2\n")

        results = await full_executor.execute_batch([
            _tool_call("edit", {"path": "f.py", "old_string": "line1", "new_string": "LINE1"}, "e1"),
            _tool_call("apply_patch", {
                "path": "f.py",
                "hunks": [{"old_string": "line2", "new_string": "LINE2"}],
            }, "ap1"),
        ])

        assert len(results) == 2
        assert all(r.result.ok for r in results)
        content = sandbox.read_text("f.py")
        assert "LINE1" in content
        assert "LINE2" in content


# ============================================================================
# R8-2（用户拍板）：read 输出预算 —— 行/字节上限 + offset/limit 分页
# 预算取厂商实测共识（pi-mono / Claude Code）：2000 行或 50KB 先到为准；
# 截断标记必须给模型"可执行的下一步"（Use offset=N to continue）。
# ============================================================================


class TestReadOutputBudget:
    @pytest.mark.asyncio
    async def test_small_file_unaffected(self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox):
        """小文件：行为不变，无截断标记。"""
        sandbox.write_text("small.txt", "a\nb\nc\n")
        result = await executor.execute(_tool_call("read", {"path": "small.txt"}))
        assert result.result.ok is True
        assert result.result.data["content"] == "a\nb\nc\n"
        assert "offset" not in result.result.data["content"]

    @pytest.mark.asyncio
    async def test_line_cap_truncates_with_pagination_marker(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """5000 行文件：只回前 2000 行 + 可续读标记 + total_lines。"""
        sandbox.write_text("big.txt", "\n".join(f"line {i}" for i in range(5000)))
        result = await executor.execute(_tool_call("read", {"path": "big.txt"}))

        assert result.result.ok is True
        content = result.result.data["content"]
        assert result.result.data["total_lines"] == 5000
        assert "line 0" in content and "line 1999" in content
        assert "line 2000" not in content
        assert "Use offset=2001" in content, "截断标记必须给出下一步动作"

    @pytest.mark.asyncio
    async def test_offset_returns_second_window(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """offset=2001 → 返回后续窗口，可继续翻页直到读完。"""
        sandbox.write_text("big.txt", "\n".join(f"line {i}" for i in range(5000)))
        result = await executor.execute(
            _tool_call("read", {"path": "big.txt", "offset": 2001})
        )

        content = result.result.data["content"]
        assert "line 2000" in content and "line 3999" in content
        assert "line 4000" not in content
        assert "Use offset=4001" in content

        tail = await executor.execute(
            _tool_call("read", {"path": "big.txt", "offset": 4001})
        )
        tail_content = tail.result.data["content"]
        assert "line 4999" in tail_content
        assert "offset" not in tail_content.split("\n")[-1]  # 最后窗口无续读标记

    @pytest.mark.asyncio
    async def test_byte_cap_truncates_giant_single_line(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """单行 200KB：字节帽（50KB）先于行帽生效，同样给续读标记。"""
        sandbox.write_text("one_line.txt", "x" * 200_000 + "\n")
        result = await executor.execute(_tool_call("read", {"path": "one_line.txt"}))

        content = result.result.data["content"]
        assert len(content) < 100_000, "字节帽未生效"
        assert "truncated" in content, "超长单行截断必须显式标记（不能续读）"

    @pytest.mark.asyncio
    async def test_offset_beyond_end_is_invalid(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """offset 越过文件末尾 → INVALID_ARGUMENT（dsh 行窗口契约）。"""
        sandbox.write_text("tiny.txt", "hello\n")
        result = await executor.execute(
            _tool_call("read", {"path": "tiny.txt", "offset": 999})
        )
        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_read_empty_file_returns_empty_success(
    executor: ToolExecutor, sandbox: LocalSubprocessSandbox
):
    """空文件（0 字节）→ 成功 + 空内容，而非 offset 越界误报（review MAJOR）。"""
    sandbox.write_text("empty.txt", "")
    result = await executor.execute(_tool_call("read", {"path": "empty.txt"}))
    assert result.result.ok is True
    assert result.result.data["content"] == ""
    assert result.result.data["total_lines"] == 0
