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
    async def test_hint_joins_prefix_without_space_after_full_width_period(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """#851 八轮修回 P2：提示后缀与「未找到」前导句的**拼接边界**。

        两侧 caller 的前导句与 `not_found_hint` 的返回值是两条各自独立的文案，
        边界靠标点约定：调用方前导句以 `。` 收尾，提示后缀**不带前导空格**，
        拼出来才是 `…。该文件…`（中文正文里句号后不跟空格）。旧实现把分隔符塞进
        提示后缀（前导空格），前导句又留着 `。` ⇒ 出现「。」+ 空格 的异常断句。
        本用例只钉边界本身，不绑定任何一侧的措辞。
        """
        sandbox.write_text("f.py", "a = 1\rb = 2\r")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "b = 2\n", "new_string": "b = 22\n"})
        )

        assert result.result.ok is False
        msg = result.result.message
        assert "行尾" in msg  # 先确认走的是带提示的路径，边界断言才有意义
        assert "。 " not in msg
        assert "字符串。该文件" in msg  # 前导句末的句号直接接提示正文

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
    async def test_old_kind_absent_from_file_gets_both_paths_not_write_only(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """#851 三轮修回 P2：old 含文件里没有的行尾种类 ⇒ **不是**只有 write 一条路。

        不靠文案自证，先钉死事实：混行尾文件 `a\\r\\nb\\nc\\n` 里的 old
        `a\\rb\\n`（含文件里不存在的裸 CR）折平后命中；把 old 的裸 CR 改写成
        该段的 CRLF 后 `count()==1`，改写重试真走得通。旧文案「请改用 write
        整文件重写」是唯一路径断言，封死了这条可行路（与上轮 P1 同缺陷类）。
        """
        content = "a\r\nb\nc\n"
        sandbox.write_text("f.py", content)
        assert content.count("a\r\nb\n") == 1

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "a\rb\n", "new_string": "a\rb\nX"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "行尾" in msg
        assert "改写" in msg and "重试" in msg
        assert "改用 write 整文件重写" in msg
        assert "请改用" not in msg
        assert sandbox.read_text("f.py") == content

    @pytest.mark.asyncio
    async def test_mixed_file_old_kind_subset_position_mismatch_warns_rewrite_may_miss(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """#851 四轮修回 P3-1 + 六轮修回 P4：混行尾里 old 与对应段落不一致 ⇒ 提示给逐处核对的做法。

        文件 `a = 1\\nb = 2\\nc = 3\\r\\n` 的行尾是 LF+CRLF 混用，CRLF 只在**末尾**
        那一处；old `a = 1\\r\\nb = 2\\r\\n` 把两处都写成 CRLF，与对应的前两段（LF）
        都不一致 ⇒ `count()==0`。真实成因是**逐位置行尾错配**，不是「末端独有」
        （用例名与 docstring 一致，五轮修回 P4-1 改名）：折平后命中即位置对齐，
        逐位置照抄对应段落行尾改写（`a = 1\\nb = 2\\n`）就 `count()==1`
        （下面的机械断言钉死）。提示须给出这条可执行的改写指引。

        #851 六轮修回 P4：本条**不**再断言「不可能命中，需先纠正该处」——
        那个断言在子集分支上是假的（反例见下一条 `..._subset_kind_rewrite_can_hit...`），
        文案改为条件式「逐处核对行尾后改正重试」。本条的机械断言相应改锚新措辞。
        """
        content = "a = 1\nb = 2\nc = 3\r\n"
        old = "a = 1\r\nb = 2\r\n"
        sandbox.write_text("f.py", content)
        assert content.count(old) == 0
        faithful = "a = 1\nb = 2\n"
        assert content.count(faithful) == 1
        assert content.count(faithful.replace("\n", "\r\n")) == 0

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": old, "new_string": "a = 9\r\nb = 2\r\n"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "对应段落的行尾" in msg and "重试" in msg
        assert "不可能命中" not in msg
        assert "末端" not in msg
        assert "改用 write 整文件重写" in msg
        assert sandbox.read_text("f.py") == content

    @pytest.mark.asyncio
    async def test_mixed_file_subset_kind_rewrite_can_hit_no_impossible_claim(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """#851 六轮修回 P4：子集分支不得再断言「逐位置照抄本就不可能命中」（有反例）。

        反例（2026-10-10 补丁级复核给出）：`content="a\\r\\nb\\n"`、`old="a\\n"`。
        old 的行尾种类（LF）是文件种类（CRLF + LF）的**子集**，错配只是少了几处
        行尾；把那一处改成对应段落的 CRLF（`"a\\r\\n"`）后 `count()==1`（下两条
        机械断言钉死）。旧文案在子集分支上无条件打「按这种改写逐位置照抄本就不可能
        命中」，在本场景为假，且「需先纠正该处」无处可纠正 ⇒ 模型被劝离一条走得通的路。

        本用例钉住新措辞：给可执行动作、不含该绝对断言，也不出现「不可能」式措辞。
        """
        content = "a\r\nb\n"
        old = "a\n"
        sandbox.write_text("f.py", content)
        assert content.count(old) == 0
        assert content.count("a\r\n") == 1

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": old, "new_string": "a\nX"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "行尾" in msg
        assert "对应段落的行尾" in msg
        assert "重试" in msg
        assert "不可能" not in msg
        assert "改用 write 整文件重写" in msg
        assert sandbox.read_text("f.py") == content

    @pytest.mark.asyncio
    async def test_plain_context_typo_on_lf_file_stays_clean(
        self, executor: ToolExecutor, sandbox: LocalSubprocessSandbox
    ):
        """判别力（LF 文件侧，#851 三轮修回 P4 补回）：LF 文件里的纯上下文抄错不提行尾。

        `9b6f6a25` 删近重复用例时把 edit 侧这条判别力删没了（只剩 CRLF 版）；
        本用例补 LF 文件侧的覆盖——两版的早退路径相同
        （`canonical_old not in canonical_content`），是覆盖差别，不是路径差别。
        """
        sandbox.write_text("f.py", "a = 1\nb = 2\n")

        result = await executor.execute(
            _tool_call({"path": "f.py", "old_string": "z = 99\n", "new_string": "z = 100\n"})
        )

        assert result.result.ok is False
        assert result.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        msg = result.result.message
        assert "未找到匹配的字符串" in msg
        assert "行尾" not in msg
        assert sandbox.read_text("f.py") == "a = 1\nb = 2\n"

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
        assert "位置或次数可能不同" in msg
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
