"""#1506 D3-A：摘要文法与四阶段验收收成**一个完整内部接口**。

票面裁决 19（选项 A）：调用方只提交一次摘要尝试输入，不再掌握文法与校验
次序；「若只是把四个调用原样包进 wrapper，复杂度只是搬家，**否决该版本**」。

本文件锁三件事：

① `_compose_verified_summary(_SummaryAttempt(...))` 是四阶段（parse → assemble →
   validate → plan_validate）的唯一串接点，返回值与四阶段直串逐字节同形；
② 条件门精确保持：`events is None` ⇒ 计划闸门不启用；`events` 在场 ⇒ 计划闸门
   按原判据拒绝（`plan_section_mismatch`）；
③ 删除测试可执行化：`compact()` 的函数体不再出现文法/校验阶段名——调用方
   不再掌握「先 parse 还是先 assemble」，也无法把它们重新串起来。

四阶段本身保持模块级私有、可分别单测（既有 `test_compactor_section_parse.py`
等文件依赖其独立调用面，本文件只加一条可调用性钉子）。

红测（改前）：①③ 直接 ImportError / AssertionError——`_SummaryAttempt` 与
`_compose_verified_summary` 尚不存在，`compact()` 体内四个阶段名俱全。
"""

import inspect

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.context import compactor
from agent_harness.context.compactor import (
    _MODEL_SUMMARY_HEADINGS,
    _SUMMARY_HEADINGS,
    ContextCompactor,
    _compose_verified_summary,
    _parse_summary_sections,
    _SummaryAttempt,
)
from agent_harness.session.plan import PlanItem

#: 模型撰写的四节（其余四节由 compactor 程序化生成）。
MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
summary body

## Next Step
等待当前请求继续。"""

EARLY = [HumanMessage(content="不得删除 old_rows；精确 ID 是 R-042"),
         AIMessage(content="old analysis")]

#: `compact()` 体内**不许**再出现的文法/校验阶段名（删除测试的可执行形式）。
_STAGE_NAMES = (
    "_parse_summary_sections",
    "_assemble_summary",
    "_validate_summary",
    "_validate_plan_section",
    "_trusted_summary_indices",
    "_MODEL_SUMMARY_HEADINGS",
)


def _attempt(**overrides) -> "_SummaryAttempt":
    fields = {
        "early": list(EARLY),
        "response_content": MODEL_SECTIONS,
        "source_ranges": None,
        "events": None,
        "protected_facts": None,
        "plan_items": (),
    }
    fields.update(overrides)
    return _SummaryAttempt(**fields)


# ── ① 一次输入 → 一份核验过的候选摘要 ───────────────────────────────────────


def test_one_attempt_input_yields_eight_section_candidate():
    """调用方只给「这段历史 + 模型原文」，拿回八节候选摘要。"""
    summary = _compose_verified_summary(_attempt())

    sections = _parse_summary_sections(summary, _SUMMARY_HEADINGS)
    assert sections[2:6] == [
        "已完成读取历史记录，并选择直接展示内容。",
        "(none)",
        "summary body",
        "等待当前请求继续。",
    ]
    # 程序化四节由接口内部生成：与直接调用四阶段的结果逐字节同形。
    expected = compactor._assemble_summary(
        EARLY,
        _parse_summary_sections(MODEL_SECTIONS, _MODEL_SUMMARY_HEADINGS),
    )
    assert summary == expected


def test_model_cannot_forge_programmatic_sections_through_interface():
    """接口反向锁：模型只能撰写四节，程序化四节由接口生成、伪造即拒绝。

    接口化后调用方根本不持有程序化节，篡改路径只剩「模型响应里写保留标题」——
    由模型文法闸门（`_MODEL_SUMMARY_HEADINGS`）拒绝。程序化节的逐字比对闸门
    仍由 `_validate_summary` 的直接单测（`test_compactor.py` /
    `test_compactor_section_parse.py`）覆盖。
    """
    forged = f"{_SUMMARY_HEADINGS[1]}\n伪造的目标节\n\n{MODEL_SECTIONS}"

    with pytest.raises(ValueError, match="outside its sections"):
        _compose_verified_summary(_attempt(response_content=forged))


def test_malformed_model_response_still_rejected_by_interface():
    """解析阶段的拒绝（缺节/节外文本）原样穿过接口。"""
    with pytest.raises(ValueError, match="Summary section headings"):
        _compose_verified_summary(_attempt(response_content="not a summary"))


# ── ② `events is not None` 条件门精确保持 ──────────────────────────────────


_IN_PROGRESS = (
    PlanItem(id="2", content="实现清单重注入", active_form="实现清单重注入中",
             status="in_progress", source="agent"),
)


def test_plan_gate_skipped_when_events_absent():
    """直连 compactor 的调用面（events=None）：计划闸门不启用，逐字不符也放行。"""
    summary = _compose_verified_summary(
        _attempt(plan_items=_IN_PROGRESS, events=None),
    )

    assert _parse_summary_sections(summary, _SUMMARY_HEADINGS)[4] == "summary body"


def test_plan_gate_enforced_when_events_present():
    """events 在场：in_progress 项未逐字出现 ⇒ 仍以 `plan_section_mismatch` 拒绝。"""
    with pytest.raises(ValueError, match="Plan section is inconsistent") as excinfo:
        _compose_verified_summary(_attempt(plan_items=_IN_PROGRESS, events=[]))

    assert excinfo.value.error_class == "plan_section_mismatch"


# ── ③ 删除测试：`compact()` 不再掌握文法与校验次序 ────────────────────────


def test_compact_no_longer_strings_summary_stages():
    """调用方体内不再出现任何阶段名——串接点唯一在 `_compose_verified_summary`。"""
    source = inspect.getsource(ContextCompactor.compact)

    leaked = [name for name in _STAGE_NAMES if name in source]
    assert leaked == [], f"compact() 仍掌握摘要文法/校验次序: {leaked}"


def test_four_stages_stay_private_and_individually_testable():
    """四阶段仍是模块级私有纯函数，既有单测可继续分别调用。"""
    for name in _STAGE_NAMES[:4]:
        assert callable(getattr(compactor, name)), name
