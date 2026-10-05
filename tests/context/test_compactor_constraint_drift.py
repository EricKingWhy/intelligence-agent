"""Issue #710 红测：压缩摘要第 0 节不跟踪会话中途的约束变更（方向 C）。

票面：[P2][Context] 压缩摘要第 0 节不跟踪会话中途的约束变更——旧约束在压缩后
残留，可能把模型带偏。已批准设计大纲：`~/workspace/issue-710-design/outline.md`
（方向 C = B 为骨架 + 第 0 节新增确定性"最新活跃用户消息"承载位 + 修正 §0 标题
错位）。本文件只写红测与存量回归锁，不写实现。

复现场景（票 §1）：首轮 USER_MESSAGE"请用中文回答，帮我写一个爬虫。"→ 长
MODEL_COMPLETED → 中途 USER_MESSAGE"改用日文回答。"（普通消息，无 supersede、
无工具调用）→ 长 MODEL_COMPLETED → "继续"触发压缩
（`max_context_tokens=10000`、`auto_compact_threshold=0.3`）。
当前实现第 0 节只含 `_current_goal_body` 的首条消息 JSON，"日文"丢失。

关键口径假设（对齐大纲 §3.2/§6，实现阶段须满足）：

- 承载位选取窗口 = **本次压缩覆盖段（early）内**的最新活跃用户消息。触发压缩
  的当前轮"继续"仍逐字保留在 recent 投影中，不是承载位来源——若按全量事件取
  最新，红测 1 要求的"日文"在几何上不可能进入第 0 节，票面红测自身不可满足。
- "来源可回读"（大纲红测 1）= 第 0 节携带来源事件的 event_id（或 seq 指针）。
- 旧约束**允许**（不强制单独计数行）以归档计数行/历史行表达；被禁止的是以
  "当前生效"形态出现（用户任务指令红测 2 原文用"允许"）。
- 全部红测不构造任何 #663 `constraint` 事实（见
  `test_red_scenarios_do_not_depend_on_663_constraint_facts`）。
"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import (
    _SUMMARY_HEADINGS,
    _parse_summary_sections,
    _programmatic_summary_sections,
)
from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE
from agent_harness.session.derive import (
    COMPACTION_SUMMARY_MESSAGE_NAME,
    derive_messages,
    derive_protected_facts,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

FIRST_MESSAGE = "请用中文回答，帮我写一个爬虫。"
JAPANESE_CHANGE = "改用日文回答。"
ENGLISH_CHANGE = "改用英文回答。"
RESUME_MESSAGE = "继续"
NEXT_MESSAGE = "接着来"
#: 票 §1 的"长 MODEL_COMPLETED"（900 字符级）：放大到 2000 字符保证
# `max_context_tokens=10000, auto_compact_threshold=0.3`（auto 限 3000 tokens）
# 在 tiktoken 精确计数与离线字节上界两种估算下都稳定触发压缩（实测 6196 tokens）。
LONG_REPLY_CN = "好的。" + "爬" * 2000
LONG_REPLY_JP = "わかりました。" + "日" * 2000
LONG_REPLY_EN = "Sure." + "英" * 2000

#: 现有 compactor 测试的模型撰写的四节；其余四节由程序化投影生成。
MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""

HEADING_0 = _SUMMARY_HEADINGS[0]
#: `_current_goal_body` / 新承载位同款 JSON 编码口径（ensure_ascii=False）。
GOAL_JSON = json.dumps(FIRST_MESSAGE, ensure_ascii=False)
JAPANESE_JSON = json.dumps(JAPANESE_CHANGE, ensure_ascii=False)
ENGLISH_JSON = json.dumps(ENGLISH_CHANGE, ensure_ascii=False)


def _append_repro_turns(session) -> None:
    """票 §1 复现事件流：中文约束 → 长回复 → 日文变更（普通 USER_MESSAGE）→ 长回复。

    中途变更不带 supersede、不带工具调用——`derive_protected_facts` 现状下
    它不成任何事实，这是本票的缺口。
    """
    session.append(USER_MESSAGE, {"content": FIRST_MESSAGE})
    session.append(MODEL_COMPLETED, {"content": LONG_REPLY_CN})
    session.append(USER_MESSAGE, {"content": JAPANESE_CHANGE})
    session.append(MODEL_COMPLETED, {"content": LONG_REPLY_JP})


def _summary_message(messages):
    return next(
        message for message in messages
        if getattr(message, "name", None) == COMPACTION_SUMMARY_MESSAGE_NAME
    )


def _section0(summary_content: str) -> str:
    return _parse_summary_sections(summary_content, _SUMMARY_HEADINGS)[0]


def _japanese_event(session):
    return next(
        event for event in session.events
        if event.type == USER_MESSAGE
        and event.data.get("content") == JAPANESE_CHANGE
    )


async def _build_with_repro(tmp_path):
    """复现场景 + "继续"触发压缩，返回 (session, messages)。"""
    session = make_session(tmp_path)
    _append_repro_turns(session)
    session.append(USER_MESSAGE, {"content": RESUME_MESSAGE})
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    return session, messages


@pytest.mark.asyncio
async def test_red1_slot_carries_newest_active_user_message_with_source(tmp_path):
    """红测 1（当前生效位）：压缩后第 0 节含"日文"承载位 + 来源可回读。"""
    session, messages = await _build_with_repro(tmp_path)
    japanese_event = _japanese_event(session)
    section0 = _section0(_summary_message(messages).content)

    # 存量锚：user_goal 目标行语义不动（#710 §5 验收 4），仍在第 0 节。
    assert GOAL_JSON in section0
    # 红测主体：当前生效指令承载位 = 压缩段内最新活跃用户消息的确定性 JSON 投影。
    assert JAPANESE_JSON in section0, (
        "第 0 节缺少'当前生效指令'承载位：压缩段内最新活跃用户消息"
        "（'改用日文回答。'）必须以 JSON 投影进入第 0 节；"
        f"实际第 0 节：{section0!r}"
    )
    # 来源可回读（大纲红测 1：来源 event_id/seq 可回读）。
    assert (
        japanese_event.event_id in section0
        or str(japanese_event.seq) in section0
    ), (
        "承载位缺少来源指针：必须携带来源事件的 event_id 或 seq 供 SessionEvent 回读"
    )


@pytest.mark.asyncio
async def test_red2_old_constraint_not_in_current_effective_form(tmp_path):
    """红测 2（旧约束降级）：'中文'不得出现在当前生效形态；归档/历史行表达允许。"""
    _, messages = await _build_with_repro(tmp_path)
    section0 = _section0(_summary_message(messages).content)
    lines = section0.splitlines()

    slot_lines = [line for line in lines if JAPANESE_JSON in line]
    assert slot_lines, (
        "第 0 节缺少当前生效指令承载位，无法验证旧约束是否降级；"
        f"实际第 0 节：{section0!r}"
    )
    assert all("中文" not in line for line in slot_lines), (
        "当前生效承载位不得残留旧约束'中文'（它只能降级为归档/历史行）"
    )
    for line in lines:
        if "中文" in line:
            assert GOAL_JSON in line or "归档" in line or "历史" in line, (
                f"'中文'只允许出现在 user_goal 目标行或归档/历史行，"
                f"实际出现在疑似当前生效形态：{line!r}"
            )


@pytest.mark.asyncio
async def test_red3_deterministic_replay_and_recompute_across_compactions(tmp_path):
    """红测 3（确定性重放）：纯函数两次调用逐字节相等；压缩→再压缩第 0 节重算，
    承载位随段内最新活跃用户消息更新，不链式继承旧摘要文本。"""
    session = make_session(tmp_path)
    _append_repro_turns(session)
    session.append(USER_MESSAGE, {"content": RESUME_MESSAGE})
    events_before_first = list(session.events)
    facts_first = derive_protected_facts(events_before_first)
    # compact() 的 cut = 最后一条 HumanMessage ⇒ early = 投影去掉最后一条。
    early_first = derive_messages(events_before_first)[:-1]

    model = ScriptedModel([
        AIMessage(content=MODEL_SECTIONS),
        AIMessage(content=MODEL_SECTIONS),
    ])
    builder = ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    )
    messages_first = await builder.build(session)
    section0_first = _section0(_summary_message(messages_first).content)

    # 纯函数重放：同一事件流两次调用逐字节相等（_validate_summary 逐字节闸门的前提）。
    replay_a = _programmatic_summary_sections(early_first, facts_first)
    replay_b = _programmatic_summary_sections(early_first, facts_first)
    assert replay_a == replay_b
    # 承载位必须由该确定性纯函数产出（才能被程序化节逐字节校验覆盖）。
    assert JAPANESE_JSON in replay_a[HEADING_0], (
        "承载位不在 _programmatic_summary_sections 的确定性输出内："
        f"实际第 0 节：{replay_a[HEADING_0]!r}"
    )
    assert replay_a[HEADING_0] == section0_first

    # 压缩 → 再压缩：新指令轮进入压缩段后，承载位重算为"英文"，不继承旧摘要文本。
    session.append(USER_MESSAGE, {"content": ENGLISH_CHANGE})
    session.append(MODEL_COMPLETED, {"content": LONG_REPLY_EN})
    session.append(USER_MESSAGE, {"content": NEXT_MESSAGE})
    events_before_second = list(session.events)
    facts_second = derive_protected_facts(events_before_second)
    early_second = derive_messages(events_before_second)[:-1]

    messages_second = await builder.build(session)
    section0_second = _section0(_summary_message(messages_second).content)

    replay_c = _programmatic_summary_sections(early_second, facts_second)
    replay_d = _programmatic_summary_sections(early_second, facts_second)
    assert replay_c == replay_d
    assert ENGLISH_JSON in replay_c[HEADING_0], (
        "第二次压缩的承载位必须是压缩段内最新活跃用户消息（'改用英文回答。'），"
        f"实际第 0 节：{replay_c[HEADING_0]!r}"
    )
    # 第 0 节是全量事件重算，不是从旧摘要链式继承。
    assert replay_c[HEADING_0] == section0_second


@pytest.mark.asyncio
async def test_red4_eight_heading_contract_intact(tmp_path):
    """红测 4（标题契约）：八节契约不被破坏——承载位在第 0 节内部，不新增第九节。"""
    _, messages = await _build_with_repro(tmp_path)
    summary_content = _summary_message(messages).content
    sections = _parse_summary_sections(summary_content, _SUMMARY_HEADINGS)

    assert len(sections) == len(_SUMMARY_HEADINGS) == 8
    assert summary_content.count("## ") == 8, "不得新增第九节承载约束"
    for body in sections:
        assert body.strip(), "每节非空（_parse_summary_sections 契约）"
    # 红测主体：约束承载在第 0 节内部，而不是靠新标题。
    assert JAPANESE_JSON in sections[0], (
        f"第 0 节必须内部承载当前生效指令；实际第 0 节：{sections[0]!r}"
    )


@pytest.mark.asyncio
async def test_green_path_single_constraint_behavior_locked(tmp_path):
    """绿路径回归（锁存量）：仅首条消息含约束、无中途变更时，现有行为不变。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": FIRST_MESSAGE})
    session.append(MODEL_COMPLETED, {"content": LONG_REPLY_CN})
    session.append(USER_MESSAGE, {"content": RESUME_MESSAGE})
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    summary_content = _summary_message(messages).content
    sections = _parse_summary_sections(summary_content, _SUMMARY_HEADINGS)
    assert len(sections) == 8
    # 目标行承载不变：首条消息 JSON 仍在第 0 节（_current_goal_body 口径）。
    assert GOAL_JSON in sections[0], (
        f"仅首条消息含约束时目标行不得变化；实际第 0 节：{sections[0]!r}"
    )
    # 压缩只覆盖早期轮：当前轮请求仍逐字保留在投影尾部。
    assert messages[-1] == HumanMessage(content=RESUME_MESSAGE)


@pytest.mark.asyncio
async def test_red_scenarios_do_not_depend_on_663_constraint_facts(tmp_path):
    """#663 交集：复现场景无任何 `constraint` 事实——红测 1/2 只测本票逻辑，
    不依赖 #663 未落地的登记入口；实现也不得从普通消息语义化造 constraint 事实。"""
    session = make_session(tmp_path)
    _append_repro_turns(session)
    session.append(USER_MESSAGE, {"content": RESUME_MESSAGE})
    facts = derive_protected_facts(session.events)
    assert not [fact for fact in facts if fact.type == "constraint"], (
        "复现场景不应产生 #663 constraint 事实（无 register_constraint 调用）"
    )
