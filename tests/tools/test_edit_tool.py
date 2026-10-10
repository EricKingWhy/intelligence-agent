"""EditTool 单元测试：精确字符串替换 0/1/>1 三态 + replace_all。

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
from agent_harness.tools import EditTool


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


@pytest.fixture
def registry(sandbox: LocalSubprocessSandbox) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EditTool(sandbox))
    return reg


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry)


def _tool_call(args: dict, call_id: str = "test_call") -> dict:
    return {"id": call_id, "name": "edit", "args": args}


class TestEditSideEffect:
    def test_side_effect_is_mutating(self, sandbox: LocalSubprocessSandbox):
        assert EditTool(sandbox).side_effect == ToolSideEffect.MUTATING


class TestEditHappyPath:
    @pytest.mark.asyncio
    async def test_single_match_success(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """1 处匹配 → 成功，文件内容确实被替换。"""
        sandbox.write_text("f.py", "def foo():\n    return 1\n")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "return 1", "new_string": "return 2"})
        )

        assert result.result.ok is True
        assert result.result.data["replacements"] == 1
        assert sandbox.read_text("f.py") == "def foo():\n    return 2\n"

    @pytest.mark.asyncio
    async def test_multiline_old_string(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """多行 old_string 也能精确匹配替换。"""
        sandbox.write_text("f.py", "def foo():\n    return 1\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "old_string": "def foo():\n    return 1\n",
                "new_string": "def bar():\n    return 2\n",
            })
        )

        assert result.result.ok is True
        assert sandbox.read_text("f.py") == "def bar():\n    return 2\n"


class TestEditThreeStates:
    @pytest.mark.asyncio
    async def test_zero_match_fails(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """0 匹配 → failure(TOOL_EXECUTION_ERROR)，文件不变。"""
        sandbox.write_text("f.py", "content")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "nonexistent", "new_string": "x"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        assert sandbox.read_text("f.py") == "content"

    @pytest.mark.asyncio
    async def test_multiple_match_fails(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """>1 匹配且 replace_all=False → failure(AMBIGUOUS)，文件不变。"""
        sandbox.write_text("f.py", "x x x")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "x", "new_string": "y"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        assert "3" in result.result.message
        assert sandbox.read_text("f.py") == "x x x"


class TestEditReplaceAll:
    @pytest.mark.asyncio
    async def test_replace_all_succeeds(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """replace_all=True + 多匹配 → 全替换成功。"""
        sandbox.write_text("f.py", "x x x")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "old_string": "x",
                "new_string": "y",
                "replace_all": True,
            })
        )

        assert result.result.ok is True
        assert result.result.data["replacements"] == 3
        assert sandbox.read_text("f.py") == "y y y"

    @pytest.mark.asyncio
    async def test_replace_all_zero_match_still_fails(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """replace_all=True + 0 匹配 → 仍然失败。"""
        sandbox.write_text("f.py", "content")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "old_string": "nope",
                "new_string": "x",
                "replace_all": True,
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR


class TestEditErrors:
    @pytest.mark.asyncio
    async def test_empty_old_string_rejected(self, executor: ToolExecutor):
        """空 old_string → schema min_length=1 拦截 → INVALID_ARGUMENT。"""
        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "", "new_string": "x"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT

    @pytest.mark.asyncio
    async def test_path_escape_denied(self, executor: ToolExecutor):
        """路径越界 → PERMISSION_DENIED。"""
        result = await executor.execute(
            _tool_call({
                "path": "../../etc/passwd",
                "old_string": "x",
                "new_string": "y",
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_nonexistent_file_fails(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """文件不存在 → TOOL_EXECUTION_ERROR。"""
        result = await executor.execute(
            _tool_call({"path": "ghost.py", "old_string": "x", "new_string": "y"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR

    @pytest.mark.asyncio
    async def test_wrong_arg_type(self, executor: ToolExecutor):
        """参数类型错（path 非 str）→ INVALID_ARGUMENT。"""
        result = await executor.execute(
            _tool_call({"path": 123, "old_string": "x", "new_string": "y"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.INVALID_ARGUMENT


class TestEditLineEndingTolerance:
    """#851：CRLF 文件 + 模型给的 LF old_string。

    Windows 上 `core.autocrlf=true` 检出即 CRLF，而模型（几乎所有）生成 LF 字符串。
    字节精确匹配下「从 read 结果里逐字抄下来的一段」在 CRLF 文件里匹配不到 ⇒
    报「未找到匹配的字符串」，把「行尾不同」与「上下文抄错」混成一类，
    模型只能盲试（#365 的三次真实运行都撞上）。
    """

    @pytest.mark.asyncio
    async def test_lf_old_string_matches_crlf_file(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """真实症状：CRLF 文件 + LF old_string 必须改得上，且写回仍是 CRLF。"""
        sandbox.write_text("f.py", "def foo():\r\n    return 1\r\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "old_string": "def foo():\n    return 1\n",
                "new_string": "def bar():\n    return 2\n",
            })
        )

        assert result.result.ok is True
        assert result.result.data["replacements"] == 1
        # 行尾必须原样保留：CRLF 进、CRLF 出，否则整个文件都会进 diff
        assert sandbox.read_text("f.py") == "def bar():\r\n    return 2\r\n"

    @pytest.mark.asyncio
    async def test_crlf_file_untouched_region_keeps_its_bytes(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """只动 old_string 命中的那一段：其余行（含行尾）逐字节不变。"""
        sandbox.write_text("f.py", "a = 1\r\nb = 2\r\nc = 3\r\n")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "b = 2", "new_string": "b = 22"})
        )

        assert result.result.ok is True
        assert sandbox.read_text("f.py") == "a = 1\r\nb = 22\r\nc = 3\r\n"

    @pytest.mark.asyncio
    async def test_crlf_file_ambiguity_still_detected(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """归一化匹配不得吃掉三态语义：CRLF 文件里 2 处匹配仍须报多匹配。"""
        sandbox.write_text("f.py", "x\r\nx\r\n")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "x", "new_string": "y"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        assert "2" in result.result.message
        assert sandbox.read_text("f.py") == "x\r\nx\r\n"

    @pytest.mark.asyncio
    async def test_lf_file_still_edits_byte_exact(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """LF 文件走原路径：不因本修复引入任何行尾改写。"""
        sandbox.write_text("f.py", "a = 1\nb = 2\n")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "b = 2", "new_string": "b = 22"})
        )

        assert result.result.ok is True
        assert sandbox.read_text("f.py") == "a = 1\nb = 22\n"

    @pytest.mark.asyncio
    async def test_mixed_line_endings_are_not_normalized(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """混行尾文件**不**走归一化：宁可报错，也不把整文件行尾改写掉。"""
        sandbox.write_text("f.py", "a = 1\r\nb = 2\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "old_string": "a = 1\nb = 2\n",
                "new_string": "a = 9\nb = 2\n",
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        # 文件逐字节不变
        assert sandbox.read_text("f.py") == "a = 1\r\nb = 2\n"


class TestEditNotFoundHint:
    """#851 验收 (2)：匹配失败的**文案**必须可执行，且不把两类失败混为一类。

    `3ef9f8de` 已让纯 CRLF/LF 文件的行尾差异自动命中，所以剩下的失败里
    「只差行尾」只会出现在裸 CR 或混行尾文件上。命中该事实时给出可执行提示；
    纯上下文抄错时**不得**冒出行尾字样，否则模型会去改一个不存在的行尾问题。
    """

    @pytest.mark.asyncio
    async def test_bare_cr_file_reports_line_ending_hint(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """裸 CR 文件 + LF old_string：确实只差行尾 ⇒ 提示必须点名行尾事实。"""
        sandbox.write_text("f.py", "a = 1\rb = 2\r")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "b = 2\n", "new_string": "b = 22\n"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "行尾" in msg
        assert "裸 CR" in msg
        assert "CRLF" not in msg
        assert sandbox.read_text("f.py") == "a = 1\rb = 2\r"

    @pytest.mark.asyncio
    async def test_mixed_line_endings_report_line_ending_hint(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """混行尾文件 + 行尾种类是文件子集的 old_string ⇒ 两条路都给。

        #851 二轮修回 P1：混行尾文件的段与段行尾不一致，「改写成**对应段落**的行尾」
        是可能命中的（本条的 old 照 LF 抄确实命中不了，但同一份文件里另一段命中得了
        ——见下一条机械对照用例），所以文案必须同时给「改写后重试」与
        「write 整文件重写」两条路，**不得**再出现「无法靠改写 old_string 的行尾命中」
        这种绝对断言（2026-10-10 复审反例已证伪它）。
        """
        sandbox.write_text("f.py", "a = 1\r\nb = 2\nc = 3\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "old_string": "a = 1\nb = 2\n",
                "new_string": "a = 9\nb = 2\n",
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "行尾" in msg
        assert "混用" in msg
        assert "CRLF" in msg
        assert "LF" in msg
        assert "对应段落的行尾" in msg
        assert "改用 write 整文件重写" in msg
        assert "无法" not in msg
        assert sandbox.read_text("f.py") == "a = 1\r\nb = 2\nc = 3\n"

    @pytest.mark.asyncio
    async def test_mixed_line_endings_rewrite_that_can_hit_gets_both_paths(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """#851 二轮修回 P1 的机械对照：混行尾文件里改写成对应段落行尾**真能命中**。

        先钉死事实（不靠文案自证）：这段 old_string 改写成 CRLF 后在本文件里
        `count()==1`；再断言提示同时给两条路，且不含「无法」这类绝对断言。
        """
        content = "a = 1\r\nb = 2\r\nc = 3\n"
        sandbox.write_text("f.py", content)
        assert content.count("a = 1\r\nb = 2\r\n") == 1

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "old_string": "a = 1\nb = 2\n",
                "new_string": "a = 9\nb = 2\n",
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "对应段落的行尾" in msg
        assert "改用 write 整文件重写" in msg
        assert "无法" not in msg
        assert sandbox.read_text("f.py") == content

    @pytest.mark.asyncio
    async def test_same_line_ending_kinds_different_positions_still_hints(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """#851 二轮修回 P3：两侧行尾**种类相同**、只是段落位置不同 ⇒ 不许吞掉。

        混行尾文件里 old_string 自带 CRLF 与 LF 各一处、位置与文件对不上：
        字节不命中、折平命中，而枚举出的种类集合两侧相同。旧实现把这当成
        「不可能发生」防御性降级成 None ⇒ 漏报；现在出谨慎提示。
        """
        sandbox.write_text("f.py", "a = 1\r\nb = 2\nc = 3\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "old_string": "a = 1\r\nb = 2\nc = 3\r\n",
                "new_string": "a = 9\r\nb = 2\nc = 3\r\n",
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "行尾" in msg
        assert "种类相同" in msg
        assert "改用 write 整文件重写" in msg
        assert sandbox.read_text("f.py") == "a = 1\r\nb = 2\nc = 3\n"

    @pytest.mark.asyncio
    async def test_bare_cr_old_string_reports_line_ending_hint(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """#851 二轮修回 P3：old_string 里一个 `\\n` 都没有（全裸 CR）也提行尾。

        这是货真价实的行尾差异（折平后命中、字节不命中）。旧实现用
        「old_string 里连一个 \\n 都没有，无法命名它的行尾差异」提前返回 None ⇒
        把真实的行尾差异吞成「上下文抄错」，模型只能盲试。
        """
        sandbox.write_text("f.py", "a = 1\r\nb = 2\n")

        result = await executor.execute(
            _tool_call({
                "path": "f.py",
                "old_string": "a = 1\rb = 2",
                "new_string": "a = 9\rb = 2",
            })
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "未找到匹配的字符串" in msg
        assert "行尾" in msg
        assert "裸 CR" in msg
        assert sandbox.read_text("f.py") == "a = 1\r\nb = 2\n"

    @pytest.mark.asyncio
    async def test_plain_context_typo_does_not_mention_line_endings(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """判别力：纯上下文抄错（连折平行尾也对不上）⇒ 不得误报行尾问题。"""
        sandbox.write_text("f.py", "a = 1\r\nb = 2\r\n")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "z = 99\n", "new_string": "z = 100\n"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "未找到匹配的字符串" in msg
        assert "行尾" not in msg
        assert "\\r" not in msg
