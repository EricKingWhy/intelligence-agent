"""#649 回归：结构化摘要里正文 Markdown 二级标题与保留节标题的结构区分。

票面（``ISSUE-649.md``）四条验收标准：

① 普通 ``## embedded heading`` 在某节正文中不造成失败，解析后正文仍含原始内容
   （若持久格式用了转义，则解码回原值）；
② 围栏代码块内的 ``##`` 行不构成结构边界，包括代码块内恰好与保留标题同文的行；
③ 缺节 / 顺序错误 / 重复保留标题 / 节外前置文本 / 无 ``(none)`` 的空节仍拒绝；
④ 无新增模型调用或无限重试：两次尝试上限与 programmatic 精确比对保持，成功压缩
   结果仍通过 shrink/target。

红测：修复前 ①② 直接命中 ``ValueError("Summary section headings do not match the
contract")``（与票面自包含最小探针同源）。③④ 是防回归钉子——修复必须保持其拒绝。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.context.compactor import (
    _MODEL_SUMMARY_HEADINGS,
    _SUMMARY_HEADINGS,
    ContextCompactor,
    _assemble_summary,
    _parse_summary_sections,
    _validate_summary,
)
from agent_harness.context.tokens import estimate_message_tokens
from tests.scripted_model import ScriptedModel

#: 票面复核里模型四节摘要的正文：第一节正文含普通 Markdown 二级标题。
EMBEDDED_BODIES = [
    "done\n## embedded heading\ntext",
    "(none)",
    "working",
    "continue",
]

#: 围栏代码块内同时含普通二级标题与**与保留标题逐字同文**的行。
FENCED_BODY = "讨论结构。\n```\n## 失败方案\n## embedded heading\n```\n收尾"


def _response(bodies: list[str]) -> str:
    """把四节正文组装成模型响应文本（与 ``_assemble`` 同形，但不做转义）。"""
    return "\n\n".join(
        f"{heading}\n{body}"
        for heading, body in zip(_MODEL_SUMMARY_HEADINGS, bodies)
    )


# ── ① 普通嵌入标题不失败、正文保真 ─────────────────────────────────────────


def test_ticket_minimal_probe_plain_embedded_heading_parses():
    """票面自包含最小探针逐字复刻：正文行首 ``## embedded heading`` 不再被当边界。"""
    text = _response(["done\n## embedded heading\ntext", "(none)", "working", "continue"])

    sections = _parse_summary_sections(text, _MODEL_SUMMARY_HEADINGS)

    assert sections == ["done\n## embedded heading\ntext", "(none)", "working", "continue"]


def test_embedded_heading_survives_assembly_and_reparse():
    """组装八节后再解析：第二节正文仍含原始 ``## embedded heading``。"""
    summary = _assemble_summary(
        [HumanMessage(content="goal")],
        _parse_summary_sections(_response(EMBEDDED_BODIES), _MODEL_SUMMARY_HEADINGS),
    )

    sections = _parse_summary_sections(summary, _SUMMARY_HEADINGS)

    assert "## embedded heading" in sections[2]
    assert sections[2] == "done\n## embedded heading\ntext"


def test_body_line_equal_to_reserved_heading_is_escaped_then_decoded():
    """正文行**逐字等于保留标题**时先转义再解析，解析后解码回原值（禁止默默吞掉）。"""
    model_sections = _parse_summary_sections(
        _response(["done\n## 文件清单\ntext", "(none)", "working", "continue"]),
        _MODEL_SUMMARY_HEADINGS,
    )

    summary = _assemble_summary([HumanMessage(content="goal")], model_sections)

    assert "\\## 文件清单" in summary, "持久格式必须把同文行转义，避免被当节边界"
    sections = _parse_summary_sections(summary, _SUMMARY_HEADINGS)
    assert sections[2] == "done\n## 文件清单\ntext", "解析时必须解码回原值"


# ── ② 围栏代码块内的 ## 不构成边界 ─────────────────────────────────────────


def test_fenced_lines_including_reserved_heading_are_not_boundaries():
    """围栏内 ``## 失败方案``（与保留标题同文）与普通 ``##`` 行都是字面正文。"""
    text = _response([FENCED_BODY, "(none)", "working", "continue"])

    sections = _parse_summary_sections(text, _MODEL_SUMMARY_HEADINGS)

    assert len(sections) == 4
    assert "## 失败方案" in sections[0]
    assert "## embedded heading" in sections[0]
    assert sections[0] == FENCED_BODY


def test_fenced_reserved_heading_not_escaped_and_still_not_a_boundary():
    """组装八节后：围栏内保留标题不转义（代码原文保真），也不被当节边界。"""
    summary = _assemble_summary(
        [HumanMessage(content="goal")],
        _parse_summary_sections(_response([FENCED_BODY, "(none)", "working", "continue"]),
                                _MODEL_SUMMARY_HEADINGS),
    )

    assert "```\n## 失败方案\n" in summary, "围栏内代码原文不得被改写"
    sections = _parse_summary_sections(summary, _SUMMARY_HEADINGS)
    assert sections[2] == FENCED_BODY


# ── ③ 五类非法结构仍拒绝 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("缺节", _response(["a", "(none)", "working"])),
        (
            "顺序错误",
            (
                "## 失败方案\n\n(none)\n\n"
                "## 已完成工作与关键决策\n\na\n\n"
                "## 当前进行中状态\n\nworking\n\n"
                "## Next Step\n\ncontinue"
            ),
        ),
        (
            "重复保留标题",
            (
                "## 已完成工作与关键决策\n\na\n\n"
                "## 已完成工作与关键决策\n\nb\n\n"
                "## 失败方案\n\n(none)\n\n"
                "## 当前进行中状态\n\nworking\n\n"
                "## Next Step\n\ncontinue"
            ),
        ),
        ("节外前置文本", "前言\n" + _response(["a", "(none)", "working", "continue"])),
        ("空节未用 (none)", _response(["", "(none)", "working", "continue"])),
    ],
)
def test_invalid_structures_are_still_rejected(name, text):
    with pytest.raises(ValueError):
        _parse_summary_sections(text, _MODEL_SUMMARY_HEADINGS)


# ── ④ 无新增调用 / 重试上限 / 精确比对 / shrink-target 保持 ────────────────


@pytest.mark.asyncio
async def test_embedded_heading_compaction_succeeds_on_first_attempt():
    """含普通嵌入标题的摘要一次通过：单次模型调用、无失败、shrink 达成。"""
    messages = [
        HumanMessage(content="不得删除 old_rows；精确 ID 是 R-042"),
        AIMessage(content="old analysis " * 600),
        HumanMessage(content="current"),
    ]
    before = estimate_message_tokens(messages)
    model = ScriptedModel([AIMessage(content=_response(EMBEDDED_BODIES))])

    result = await ContextCompactor(model, max_context_tokens=8000).compact(
        messages, before,
    )

    assert len(model.snapshots) == 1, "只应有一次模型调用（无额外摘要请求）"
    assert result.failures == []
    assert result.compacted_turn_count == 1
    assert result.summary is not None and "## embedded heading" in result.summary
    # shrink/target：压缩后投影必须小于压缩前（成功路径的硬判据）。
    assert estimate_message_tokens(result.messages) < before


@pytest.mark.asyncio
async def test_summary_retry_cap_is_still_two_calls():
    """两次尝试上限保持：持续非法摘要不产生第三次模型调用。"""

    class AlwaysBadModel:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            return AIMessage(content="not a structured summary")

    messages = [
        HumanMessage(content="old"),
        AIMessage(content="done"),
        HumanMessage(content="current"),
    ]
    model = AlwaysBadModel()
    result = await ContextCompactor(model, max_context_tokens=8000).compact(
        messages, estimate_message_tokens(messages),
    )

    assert model.calls == 2
    assert [failure.error_class for failure in result.failures] == [
        "heading_mismatch", "heading_mismatch",
    ]


def test_programmatic_exact_comparison_still_rejects_tampering():
    """programmatic 精确比对仍在：篡改程序化节必须拒绝。"""
    messages = [
        HumanMessage(content="不得删除 old_rows；精确 ID 是 R-042"),
        AIMessage(content="old analysis"),
    ]
    summary = _assemble_summary(
        messages,
        _parse_summary_sections(_response(EMBEDDED_BODIES), _MODEL_SUMMARY_HEADINGS),
    )
    tampered_sections = list(_parse_summary_sections(summary, _SUMMARY_HEADINGS))
    tampered_sections[0] = "rewritten\n" + tampered_sections[0]
    tampered = "\n\n".join(
        f"{heading}\n{body}"
        for heading, body in zip(_SUMMARY_HEADINGS, tampered_sections)
    )

    with pytest.raises(ValueError, match="Programmatic summary section"):
        _validate_summary(tampered, messages)
