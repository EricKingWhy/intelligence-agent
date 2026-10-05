"""#699 回归：摘要转义的转义碰撞（#649 P3-1 跟进）。

#649 引入的确定性转义只处理"逐字等于保留标题"的行：组装时 ``## X`` →
``\\## X``，解析时 ``\\## X`` → ``## X``。但正文里**原本就含有** ``\\`` +
保留标题的行（例如模型按 CommonMark 写法在正文里写了一个转义标题当普通
文本），组装时不算保留标题、原样通过，解析时却被当成"本轮转义的"解码——
原文被改写，round-trip 不保真。

修复方向（issue #699 已定，成熟产品参考见票面）：转义符本身也要转义
（escape-the-escape，与 CommonMark §6.1 / RFC 8259 §7 同源）——
``^(\\\\*)(## 保留标题)$`` 的行组装时再加一层 ``\\``，解析时去一层。

红测：修复前 ``test_p3_probe`` 命中解码改写（``\\`` 丢失）。
"""

import pytest
from langchain_core.messages import HumanMessage

from agent_harness.context.compactor import (
    _SUMMARY_HEADINGS,
    _assemble_summary,
    _escape_section_body,
    _parse_summary_sections,
    _unescape_section_body,
)

#: 用一个真实的保留标题做碰撞体。
RESERVED = "## 失败方案"


def _roundtrip(body: str) -> str:
    """四节模型正文 → 组装八节 → 解析，取回第三节正文（bodies[4]）。"""
    summary = _assemble_summary(
        [HumanMessage(content="goal")],
        ["done", "(none)", body, "continue"],
    )
    return _parse_summary_sections(summary, _SUMMARY_HEADINGS)[4]


# ── P3-1 探针：正文原有的反斜杠+保留标题行 ────────────────────────────────


def test_p3_probe_literal_backslash_heading_survives():
    """票面探针逐字复刻：``\\## 失败方案`` 原样存活，不多不少一个 ``\\``。"""
    body = "text\n\\## 失败方案\nmore"

    assert _roundtrip(body) == body


@pytest.mark.parametrize("slashes", [0, 1, 2])
def test_escape_levels_roundtrip(slashes: int):
    """0/1/2 层 ``\\`` + 保留标题：各加一层、解一层，逐字保真。"""
    body = "a\n" + "\\" * slashes + RESERVED + "\nb"

    assert _roundtrip(body) == body


def test_backslash_non_heading_lines_untouched():
    """反斜杠 + 非保留行：组装/解析都不碰。"""
    for line in ("\\hello", "\\", "", "  ## 失败方案"):
        body = f"x\n{line}\ny"

        assert _roundtrip(body) == body


def test_fenced_lines_not_escaped_or_decoded():
    """围栏内的 ``\\##`` 行：转义与解码都跳过，代码原文保真。"""
    body = "讨论\n```\n\\## 失败方案\n```\n收尾"

    assert _roundtrip(body) == body


# ── 单元级：转义/解码互逆 ──────────────────────────────────────────────────


@pytest.mark.parametrize("slashes", [0, 1, 2, 3])
def test_escape_unescape_inverse(slashes: int):
    line = "\\" * slashes + RESERVED

    assert _unescape_section_body(
        _escape_section_body(line, _SUMMARY_HEADINGS), _SUMMARY_HEADINGS
    ) == line
