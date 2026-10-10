"""ApplyPatchTool 单元测试：多 hunk 原子补丁 + 失败回滚。

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
from agent_harness.tools import ApplyPatchTool


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


@pytest.fixture
def registry(sandbox: LocalSubprocessSandbox) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ApplyPatchTool(sandbox))
    return reg


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry)


def _tool_call(args: dict, call_id: str = "test_call") -> dict:
    return {"id": call_id, "name": "apply_patch", "args": args}


class TestApplyPatchSideEffect:
    def test_side_effect_is_mutating(self, sandbox: LocalSubprocessSandbox):
        assert ApplyPatchTool(sandbox).side_effect == ToolSideEffect.MUTATING


class TestApplyPatchHappyPath:
    @pytest.mark.asyncio
    async def test_multi_hunk_all_success(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """多 hunk 全部成功 → 文件最终内容正确，data 含 hunks_applied。"""
        sandbox.write_text("f.py", "import os\n\n\ndef foo():\n    return 1\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [
                    {"old_string": "import os", "new_string": "import sys"},
                    {"old_string": "return 1", "new_string": "return 2"},
                ],
            })
        )

        assert result.result.ok is True
        assert result.result.data["hunks_applied"] == 2
        content = sandbox.read_text("f.py")
        assert "import sys" in content
        assert "return 2" in content
        assert "import os" not in content

    @pytest.mark.asyncio
    async def test_single_hunk_success(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """单 hunk 也能正常工作。"""
        sandbox.write_text("f.py", "hello world")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [{"old_string": "hello", "new_string": "goodbye"}],
            })
        )

        assert result.result.ok is True
        assert sandbox.read_text("f.py") == "goodbye world"


class TestApplyPatchAtomicity:
    @pytest.mark.asyncio
    async def test_first_hunk_no_match_file_unchanged(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """第 1 块 0 匹配 → 失败，文件完全不变。"""
        original = "import os\n\n\ndef foo():\n    return 1\n"
        sandbox.write_text("f.py", original)

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [
                    {"old_string": "NONEXISTENT", "new_string": "x"},
                    {"old_string": "return 1", "new_string": "return 2"},
                ],
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        assert "第 1 块" in result.result.message
        # 原子性：文件完全没被改动
        assert sandbox.read_text("f.py") == original

    @pytest.mark.asyncio
    async def test_later_hunk_no_match_file_unchanged(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """非首 hunk 0 匹配 → 失败，前面 hunk 的改动也没落盘（原子性）。"""
        original = "import os\n\n\ndef foo():\n    return 1\n"
        sandbox.write_text("f.py", original)

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [
                    {"old_string": "import os", "new_string": "import sys"},
                    {"old_string": "NONEXISTENT", "new_string": "x"},
                ],
            })
        )

        assert result.result.ok is False
        assert "第 2 块" in result.result.message
        # 关键原子性断言：第 1 块虽然在内存里替换了，但没有写入磁盘
        assert sandbox.read_text("f.py") == original

    @pytest.mark.asyncio
    async def test_hunk_multiple_match_fails(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """某 hunk >1 匹配 → 失败，文件不变。"""
        original = "x x x"
        sandbox.write_text("f.py", original)

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [{"old_string": "x", "new_string": "y"}],
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        assert sandbox.read_text("f.py") == original


class TestApplyPatchErrors:
    @pytest.mark.asyncio
    async def test_empty_old_string_in_hunk(self, executor: ToolExecutor):
        """hunk 的 old_string 为空 → schema 拦截 → INVALID_ARGUMENT。"""
        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [{"old_string": "", "new_string": "x"}],
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT

    @pytest.mark.asyncio
    async def test_empty_hunks_list(self, executor: ToolExecutor):
        """空 hunks 列表 → schema min_length=1 拦截 → INVALID_ARGUMENT。"""
        result = await executor.execute(
            _tool_call({"path": "f.py", "hunks": []})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT

    @pytest.mark.asyncio
    async def test_path_escape_denied(self, executor: ToolExecutor):
        """路径越界 → PERMISSION_DENIED。"""
        result = await executor.execute(
            _tool_call({
                "path": "../../etc/passwd",
                "hunks": [{"old_string": "x", "new_string": "y"}],
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_nonexistent_file(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """文件不存在 → TOOL_EXECUTION_ERROR。"""
        result = await executor.execute(
            _tool_call({
                "path": "ghost.py",
                "hunks": [{"old_string": "x", "new_string": "y"}],
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR

    @pytest.mark.asyncio
    async def test_wrong_arg_type(self, executor: ToolExecutor):
        """参数类型错 → INVALID_ARGUMENT。"""
        result = await executor.execute(
            _tool_call({"path": 123, "hunks": [{"old_string": "x", "new_string": "y"}]})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT


class TestApplyPatchLineEndingTolerance:
    """#851：apply_patch 与 edit 共用同一条匹配路径，行尾容忍必须一致。"""

    @pytest.mark.asyncio
    async def test_lf_hunks_apply_to_crlf_file(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """CRLF 文件 + 多个 LF hunk：全部改得上，且逐 hunk 之后仍能继续匹配。"""
        sandbox.write_text("f.py", "a = 1\r\nb = 2\r\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [
                    {"old_string": "a = 1\n", "new_string": "a = 11\n"},
                    {"old_string": "b = 2\n", "new_string": "b = 22\n"},
                ],
            })
        )

        assert result.result.ok is True
        assert result.result.data["hunks_applied"] == 2
        assert sandbox.read_text("f.py") == "a = 11\r\nb = 22\r\n"

    @pytest.mark.asyncio
    async def test_mixed_line_endings_still_fail_closed(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """混行尾文件不走归一化：宁可整批失败，也不改写整文件行尾。"""
        sandbox.write_text("f.py", "a = 1\r\nb = 2\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [{"old_string": "a = 1\n", "new_string": "a = 11\n"}],
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        assert sandbox.read_text("f.py") == "a = 1\r\nb = 2\n"


class TestApplyPatchNotFoundHint:
    """#851 验收 (2) 的 apply_patch 侧：未命中文案同样不得把行尾与抄错混为一类。"""

    @pytest.mark.asyncio
    async def test_bare_cr_file_reports_line_ending_hint(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """裸 CR 文件 + LF old_string ⇒ 提示点名行尾事实，且整批原子失败。"""
        sandbox.write_text("f.py", "a = 1\rb = 2\r")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [{"old_string": "b = 2\n", "new_string": "b = 22\n"}],
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "行尾" in msg
        assert "裸 CR" in msg
        assert "CRLF" not in msg
        assert sandbox.read_text("f.py") == "a = 1\rb = 2\r"

    @pytest.mark.asyncio
    async def test_mixed_line_endings_hint_points_to_write(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """混行尾文件 ⇒ 点名行尾事实并指向 write，且不得给不可执行的改写指令。

        #851 P1 的 apply_patch 侧对称用例：段与段行尾不同时「按该文件的行尾改写」
        照做仍 count()==0，这条指令必须不出现。
        """
        sandbox.write_text("f.py", "a = 1\r\nb = 2\nc = 3\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [{"old_string": "a = 1\nb = 2\n", "new_string": "a = 9\nb = 2\n"}],
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "行尾" in msg
        assert "混用" in msg
        assert "CRLF" in msg
        assert "LF" in msg
        assert "write" in msg
        assert "按该文件的行尾改写" not in msg
        assert sandbox.read_text("f.py") == "a = 1\r\nb = 2\nc = 3\n"

    @pytest.mark.asyncio
    async def test_plain_context_typo_on_lf_file_stays_clean(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """判别力（LF 文件侧）：LF 文件里的抄错同样不得提行尾。"""
        sandbox.write_text("f.py", "a = 1\nb = 2\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [{"old_string": "z = 99\n", "new_string": "z = 100\n"}],
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "未找到匹配" in msg
        assert "行尾" not in msg
        assert sandbox.read_text("f.py") == "a = 1\nb = 2\n"

    @pytest.mark.asyncio
    async def test_plain_context_typo_does_not_mention_line_endings(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """判别力：纯抄错 ⇒ 仍是原「未找到匹配」，不得误报行尾。"""
        sandbox.write_text("f.py", "a = 1\r\nb = 2\r\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "hunks": [{"old_string": "z = 99\n", "new_string": "z = 100\n"}],
            })
        )

        assert result.result.ok is False
        msg = result.result.message
        assert "未找到匹配" in msg
        assert "行尾" not in msg
