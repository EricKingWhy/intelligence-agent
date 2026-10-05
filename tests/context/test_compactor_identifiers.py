"""#640（T12a + T2-X/Y）：标识清单的预算与 UUID 完整性。

被测入口（票面 AC；行号按被测 commit `1226f4bb` 给出，本文件按函数名定位）：

- **T12a**（已修覆盖的回归验证，不重造实现）：`range(30000)` 数字密集文本 ⇒
  程序化标识节 ≤50 条、单项 ≤200 字符（#556 裁决 C / #614 的既有收敛），
  **并单独走完整 `ContextCompactor.compact()`** 验证 shrink/target 结果——
  helper 绿不替代完整压缩路径绿。
- **T2-X/Y**：数字开头（`0-9a-f` 起）与字母开头的标准 8-4-4-4-12 十六进制
  UUID，在普通 Human/Tool 文本中都要**逐字完整**进入标识清单；修复前
  数字开头形态只能从第二段起截出尾部伪标识。
- **AC③**：UUID 不得从较长连续字母数字串里截取伪标识；重复值去重；
  预算淘汰顺序（最近偏置）与既有实现一致。

规格依据：`SPEC_ROOT/06_CONTEXT_ARTIFACT_MEMORY.md` §1–5/§8–9（完整保存 ≠
完整注入、模型输入有界）、`docs/adr/0007-context-compaction-three-tier-fallback.md`
（#348/#346/#383 修订）。本票只改标识提取，不新增 deterministic fallback、
不删除原始历史、不改模型预算契约。
"""

import json
import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_harness.context.compactor import (
    _PROG_SECTION_MAX_ENTRIES,
    _PROG_SECTION_MAX_ENTRY_CHARS,
    _SUMMARY_HEADINGS,
    ContextCompactor,
    _parse_summary_sections,
    _programmatic_summary_sections,
)
from agent_harness.context.tokens import estimate_message_tokens
from tests.scripted_model import ScriptedModel

#: T2-X/Y 的最小形态：数字开头（标准 UUID 允许，此前整条漏配）与字母开头
#: （既有分支已覆盖；`…-ALL-HEX` 型只有新分支能整条命中）。
UUID_DIGIT_LEADING = "123e4567-e89b-12d3-a456-426614174000"
UUID_LETTER_LEADING = "abc12345-e89b-12d3-a456-426614174000"
UUID_LETTER_LEADING_ALL_HEX = "abcdefab-abcd-abcd-abcd-abcdefabcdef"

#: 标准 8-4-4-4-12 形状（判 AC③ 用：任何条目都不该是"UUID 形状的伪标识"）。
_UUID_SHAPE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)

#: T12a 的输入形态（票面原文）：n=30000 空格分隔数字文本。
T12A_TEXT = " ".join(str(i) for i in range(30000))

#: 显式传入的窗口配置：断言不依赖 `ContextCompactor` 默认值的漂移。
MAX_CONTEXT_TOKENS = 200_000

_MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


def _identifiers(messages) -> list[str]:
    """取程序化标识节（`(none)` 解码为空表；与生产辅助函数同源）。"""
    sections = _programmatic_summary_sections(messages, [])
    body = sections[_SUMMARY_HEADINGS[6]]
    return [] if body == "(none)" else json.loads(body)


# ── T12a：已修覆盖的回归（有界标识节 + 完整 compact 的 shrink/target） ──────


def test_t12a_programmatic_identifier_section_is_bounded():
    """helper 面：`range(30000)` 派生清单收敛到 50 条 × 200 字符（回归钉）。"""
    identifiers = _identifiers([HumanMessage(content=T12A_TEXT)])
    assert len(identifiers) <= _PROG_SECTION_MAX_ENTRIES, (
        "数字密集文本不得再派生无界标识清单（#556 裁决 C 的既有收敛）"
    )
    assert all(
        len(entry) <= _PROG_SECTION_MAX_ENTRY_CHARS + 1  # 截断值 + 省略符
        for entry in identifiers
    ), "单条标识必须受 200 字符投影上限约束"


@pytest.mark.asyncio
async def test_t12a_full_compact_shrinks_and_reaches_target():
    """完整 compact 路径（不只 helper）：shrink 成立且结果落在 auto 目标内。

    被测版（`1226f4bb`）的原始症状是"摘要开销超过原文"；本用例在**完整**
    `compact()`（含两次尝试、程序化节组装、shrink 与 target 两道闸门）上
    验证该症状已消：返回的投影显著小于输入且 < auto 阈值。
    """
    messages = [
        HumanMessage(content=T12A_TEXT),
        AIMessage(content="old analysis"),
        HumanMessage(content="current request"),
    ]
    compactor = ContextCompactor(
        ScriptedModel([AIMessage(content=_MODEL_SECTIONS)]),
        max_context_tokens=MAX_CONTEXT_TOKENS,
    )
    before = estimate_message_tokens(messages)

    result = await compactor.compact(messages, before)

    # AC④：成功路径必须**零失败尝试**。`fallback_used` 的全部构造点都是字面量
    # False（看代码即知），单断言它是空断言；`failures` 才承载"本次有没有摘要
    # 尝试失败过"，是 AC④"记录失败类别与最终开销"的可观测面。
    assert result.failures == [], "成功压缩不得携带失败尝试记录"
    assert result.compacted_turn_count == 1
    assert result.summary is not None
    identifiers = json.loads(
        _parse_summary_sections(result.summary, _SUMMARY_HEADINGS)[6]
    )
    assert len(identifiers) <= _PROG_SECTION_MAX_ENTRIES
    after = estimate_message_tokens(result.messages)
    auto_limit = compactor.auto_compact_threshold * MAX_CONTEXT_TOKENS
    assert after < before, "shrink：压缩后的投影必须小于输入投影"
    assert after * 10 < before, (
        "shrink：数字密集文本的摘要开销必须显著小于原文（原报告症状是超过原文）"
    )
    assert after < auto_limit, "target：压缩结果必须落在 auto 阈值之内"


# ── T2-X/Y：数字开头与字母开头 UUID 逐字完整 ────────────────────────────────


@pytest.mark.parametrize(
    "uuid",
    [UUID_DIGIT_LEADING, UUID_LETTER_LEADING, UUID_LETTER_LEADING_ALL_HEX],
)
def test_uuid_in_human_text_is_extracted_verbatim(uuid):
    """普通 Human 文本：UUID 必须逐字完整进入清单，且不得只截出尾部片段。"""
    identifiers = _identifiers([HumanMessage(content=f"请核对记录 {uuid} 后继续")])
    assert uuid in identifiers, "标准 UUID 必须逐字完整（数字开头形态此前被遗漏）"
    assert len(identifiers) < _PROG_SECTION_MAX_ENTRIES, "总数低于上限，不会被窗口淘汰"
    fragments = [entry for entry in identifiers if entry != uuid and entry in uuid]
    assert fragments == [], "不得把 UUID 截成尾部伪标识（修复前的 e89b-… 形态）"


@pytest.mark.parametrize(
    "uuid",
    [UUID_DIGIT_LEADING, UUID_LETTER_LEADING, UUID_LETTER_LEADING_ALL_HEX],
)
def test_uuid_in_tool_text_is_extracted_verbatim(uuid):
    """Tool 文本同上（票面 AC 覆盖 Human / Tool 两个入口）。"""
    identifiers = _identifiers([
        ToolMessage(content=f"artifact {uuid} saved", tool_call_id="call-1"),
    ])
    assert uuid in identifiers, "Tool 文本里的标准 UUID 同样必须逐字完整"


# ── AC③：不得从较长串截取伪标识；去重与淘汰顺序不变 ─────────────────────────


def test_uuid_is_not_sliced_from_longer_alphanumeric_run():
    """较长连续字母数字串中的 UUID 不得被当成**独立完整标识**切出来。

    边界语义（`(?<![A-Za-z0-9_])` / `(?![A-Za-z0-9_])` 原样保留）下的实测形态：
    无分隔连续串整体不产出条目；`pre`+UUID 产出**更长的 token**（既有分支）；
    `999`+UUID 与 UUID+`0` 被尾 lookahead 挡下后落到既有分支1 的**尾部片段**
    （自第二段 `e89b` 起、可含黏连尾缀；修复前后一致，均非 8-4-4-4-12 形状）。本用例只钉两件事：
    条目中不得出现整条 `UUID_DIGIT_LEADING`、不得出现 UUID 形状条目——
    `999`+UUID 这一例对 `(?<![A-Za-z0-9_])` 有区分力：去掉该 lookbehind 后，
    新分支会在数字前缀内部命中整条 UUID，前两条断言同时失败。
    """
    continuous = "feedface123e4567e89b12d3a456426614174000"  # 无分隔的长字母数字串
    glued_prefix = "pre" + UUID_DIGIT_LEADING
    digit_glued_prefix = "999" + UUID_DIGIT_LEADING  # 去掉前置 lookbehind 时此例会转红
    glued_suffix = UUID_DIGIT_LEADING + "0"

    for text in (continuous, glued_prefix, digit_glued_prefix, glued_suffix):
        identifiers = _identifiers([HumanMessage(content=f"token {text} end")])
        assert UUID_DIGIT_LEADING not in identifiers, (
            f"较长的黏连串里不得切出完整 UUID 伪标识：{text} -> {identifiers}"
        )
        assert not any(_UUID_SHAPE.fullmatch(entry) for entry in identifiers), (
            f"不得产出 UUID 形状的伪标识：{text} -> {identifiers}"
        )

    assert _identifiers([HumanMessage(content=f"token {continuous} end")]) == [], (
        "无分隔的连续长串整体不产出标识"
    )


def test_uuid_dedup_and_recent_biased_eviction_unchanged():
    """重复值去重；超过 50 条时淘汰顺序仍是既有「保最近」语义。"""
    messages = [
        *(AIMessage(content=f"处理 TSK-{i:04d} 号任务") for i in range(59)),
        HumanMessage(content=f"{UUID_DIGIT_LEADING} / {UUID_DIGIT_LEADING} 复核 {UUID_DIGIT_LEADING}"),
    ]
    identifiers = _identifiers(messages)

    assert identifiers.count(UUID_DIGIT_LEADING) == 1, "重复值只保留一条"
    assert len(identifiers) == _PROG_SECTION_MAX_ENTRIES, "总数收敛到既有上限"
    assert UUID_DIGIT_LEADING in identifiers, "最近出现的标识保留（最近偏置）"
    assert "TSK-0000" not in identifiers, "最旧标识按既有顺序淘汰"
    assert "TSK-0058" in identifiers


def test_existing_identifier_extraction_is_unchanged():
    """既有形态（连字符数字后缀 / snake_case / 命令）提取结果逐条不变。

    钉住 UUID 分支的插入位置：新分支只能补在既有两分支**之后**，
    不得抢在既有匹配前改变其产出。
    """
    text = "ID R-042 与 py_project 和 call-r-042 及 TASK-1234"
    identifiers = _identifiers([HumanMessage(content=text)])
    assert {"R-042", "py_project", "call-r-042", "TASK-1234"} <= set(identifiers)
    # 字母开头的 UUID 仍由既有分支整体吃下（后缀黏连时产出更长的 token，行为不变）。
    assert UUID_LETTER_LEADING + "-extra1" in _identifiers(
        [HumanMessage(content=UUID_LETTER_LEADING + "-extra1")]
    )


# ── #707：保序去重等价钉（list 扫描 → set 成员检查，顺序契约不变） ───────────


def test_identifier_first_occurrence_order_is_preserved():
    """add_once 去重 = 首次出现顺序：重复值不移动位置也不重复入列。

    #707 把 O(n²) 的 list 线性扫描换成 seen 集合 O(1) 检查，输出契约钉死为
    首次出现顺序（含重复值出现时不得被"移到末尾"），与既有窗口淘汰
    （`_capped_entries`，最近偏置）正交。
    """
    text = (
        "先 TASK-1001 与 TASK-2002，复核 TASK-1001、再提 TASK-2002，"
        "新增 TASK-3003"
    )
    identifiers = _identifiers([HumanMessage(content=text)])
    positions = [identifiers.index(token) for token in (
        "TASK-1001", "TASK-2002", "TASK-3003",
    )]
    assert positions == sorted(positions), (
        f"必须保持首次出现顺序：{identifiers}"
    )
    for token in ("TASK-1001", "TASK-2002", "TASK-3003"):
        assert identifiers.count(token) == 1, (
            f"重复值只保留首次出现的那条：{token} -> {identifiers}"
        )


def test_first_occurrence_dedup_survives_window_boundary():
    """变异钉：首现去重先于窗口 ⇒ 早期条目不被末尾重复值"救回"窗口。

    [TSK-0000..0059, TSK-0005 重复]：去重后 distinct 流共 60 条，窗口取末 50
    （TSK-0010..0059），TSK-0005 按首现位置被淘汰；若去重被摘除（#707 变异
    M1），61 条原始流经 `_capped_entries` 的保末去重会把 TSK-0005 挪到队尾
    并留在窗口内。
    """
    messages = [
        *(AIMessage(content=f"处理 TSK-{i:04d} 号任务") for i in range(60)),
        AIMessage(content="处理 TSK-0005 号任务"),
    ]
    identifiers = _identifiers(messages)
    assert len(identifiers) == _PROG_SECTION_MAX_ENTRIES, "窗口规模不变"
    assert "TSK-0059" in identifiers, "最近偏置不变"
    assert "TSK-0005" not in identifiers, (
        "早期条目按首现位置参与窗口竞争，不得被末尾重复值带回窗口"
    )
