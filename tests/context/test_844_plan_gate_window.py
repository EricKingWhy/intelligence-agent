"""#844：手动压缩被计划节闸门（`plan_section_mismatch`）误拒的复现与消融。

本文件锁定方案 A 的两个调整（详见 `~/workspace/system/dispatch/844-plan.md`）：

- **A-sem**：(none) 触发条件从「零 `in_progress`」放宽为「零**未完成项**」
  （`status != "completed"`，即无 `pending` 且无 `in_progress`）——对齐 prompt
  「列出尚未完成的工作……无则写 (none)」的语义（`prompt/builtin.py:90`）。
- **A-vis**：闸门比较基准对齐摘要器的**可见窗口**——摘要只覆盖 early 段
  `[prefix_end:cut]`，落在 `cut` 之后的计划更新对摘要器不可见，闸门不再要求
  其逐字出现。

用例分工（消融）：
- R1 只 exercise A-sem（计划更新全在窗口内，A-vis 输入不变）；
- R2 只 exercise A-vis（窗口内计划仍含 `in_progress` 项，A-sem 不参与判定）；
- D1/D2 锁判别力保留；D4 锁「只放宽不收紧」；R3 锁自动路径逐字节等价。

fixture 写法沿用 `tests/context/test_plan_reinjection.py`（PLAN_ITEMS_* / ScriptedModel）。
"""

import pytest
from langchain_core.messages import AIMessage

from agent_harness.context import compactor
from agent_harness.context.builder import ContextBuilder
from agent_harness.session import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    CONTEXT_COMPACTION_FAILED,
    MODEL_COMPLETED,
    USER_MESSAGE,
)
from agent_harness.session.plan import apply_plan_update, derive_plan
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

#: V1：item2 处于 in_progress（在 early 窗口内也可见）。
PLAN_ITEMS_V1 = [
    {"id": "1", "content": "梳理压缩规格", "activeForm": "梳理压缩规格中",
     "status": "completed", "source": "agent"},
    {"id": "2", "content": "实现清单重注入", "activeForm": "实现清单重注入中",
     "status": "in_progress", "source": "agent"},
]

#: V2：item2 转 completed、新 item3 以 in_progress 出生（W-26 状态机合法）。
PLAN_ITEMS_V2 = [
    {"id": "1", "content": "梳理压缩规格", "activeForm": "梳理压缩规格中",
     "status": "completed", "source": "agent"},
    {"id": "2", "content": "实现清单重注入", "activeForm": "实现清单重注入中",
     "status": "completed", "source": "agent"},
    {"id": "3", "content": "验证压缩接班", "activeForm": "验证压缩接手中",
     "status": "in_progress", "source": "agent"},
]

#: 全部完成的清单（零未完成项）——第 5 节必须是 (none) 的形态。
PLAN_ITEMS_ALL_DONE = [
    {"id": "1", "content": "梳理压缩规格", "activeForm": "梳理压缩规格中",
     "status": "completed", "source": "agent"},
    {"id": "2", "content": "实现清单重注入", "activeForm": "实现清单重注入中",
     "status": "completed", "source": "agent"},
]

#: 零 in_progress、两个 pending（均有未完成工作）——prompt 要求罗列、旧闸门要求 (none)。
PLAN_ITEMS_PENDING_TWO = [
    {"id": "1", "content": "梳理压缩规格", "activeForm": "梳理压缩规格中",
     "status": "pending", "source": "agent"},
    {"id": "2", "content": "实现清单重注入", "activeForm": "实现清单重注入中",
     "status": "pending", "source": "agent"},
]


def _apply_plan(session, items) -> None:
    outcome = apply_plan_update(session, items)
    assert outcome.ok, outcome.reason


def _model_sections(section5: str) -> str:
    """模型撰写的四节（其余四节由 compactor 程序化生成），第 5 节可注入。"""
    return (
        "## 已完成工作与关键决策\n已完成读取历史记录。\n\n"
        "## 失败方案\n(none)\n\n"
        f"## 当前进行中状态\n{section5}\n\n"
        "## Next Step\n等待当前请求继续。"
    )


#: 按 prompt「列出尚未完成的工作」语义罗列两个 pending 项（子情形 1 复现）。
_PENDING_LISTING_V1 = (
    "尚未完成的工作：梳理压缩规格（待办）；实现清单重注入（待办）。"
)


@pytest.mark.asyncio
async def test_zero_in_progress_with_pending_passes_when_listed(tmp_path):
    """R1（锁定 A-sem）：窗口计划零 in_progress + 2 个 pending。

    prompt 要求「列出尚未完成的工作」，旧闸门（零 in_progress ⇒ 必须 (none)）与
    之矛盾。修后：未完成项非空 ⇒ 不再强制 (none)，且无 in_progress 项需要逐字校验
    ⇒ 通过、bracket 落盘。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _apply_plan(session, PLAN_ITEMS_PENDING_TWO)
    session.append(MODEL_COMPLETED, {"content": "历史 " * 900})
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([
        AIMessage(content=_model_sections(_PENDING_LISTING_V1)),
        AIMessage(content=_model_sections(_PENDING_LISTING_V1)),
    ])

    await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["error_class"] for e in failures] == []
    assert any(event.type == COMPACTION_END for event in session.events)


@pytest.mark.asyncio
async def test_in_window_in_progress_missing_still_rejected(tmp_path):
    """D1（判别力保留）：窗口内 in_progress 项被模型漏写 ⇒ 仍拒绝。

    两次尝试同一缺陷各落一条 failed（error_class=plan_section_mismatch），
    零 bracket（压缩被拒、不做任何落盘）——真阳性能力不因 #844 放宽而丧失。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _apply_plan(session, PLAN_ITEMS_V1)  # item2 in_progress 在窗口内
    session.append(MODEL_COMPLETED, {"content": "历史 " * 900})
    session.append(USER_MESSAGE, {"content": "current request"})
    stale = "摘要覆盖的历史工作已完成。"  # 不含 item2 的 content/activeForm
    model = ScriptedModel([
        AIMessage(content=_model_sections(stale)),
        AIMessage(content=_model_sections(stale)),
    ])

    await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["error_class"] for e in failures] == [
        "plan_section_mismatch", "plan_section_mismatch",
    ]
    assert not any(
        event.type in {COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END}
        for event in session.events
    )


@pytest.mark.asyncio
async def test_all_completed_non_none_still_rejected(tmp_path):
    """D2（判别力保留）：窗口内计划全 completed + 模型写非 (none) ⇒ 仍拒绝。

    锁住 (none) 规则：零未完成项 ⇒ 第 5 节必须逐字 (none)（两试均违例 ⇒ 零 bracket）。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _apply_plan(session, PLAN_ITEMS_ALL_DONE)
    session.append(MODEL_COMPLETED, {"content": "历史 " * 900})
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([
        AIMessage(content=_model_sections("还在实现清单重注入。")),
        AIMessage(content=_model_sections("还在实现清单重注入。")),
    ])

    await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["error_class"] for e in failures] == [
        "plan_section_mismatch", "plan_section_mismatch",
    ]
    assert not any(
        event.type in {COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END}
        for event in session.events
    )


@pytest.mark.asyncio
async def test_pending_items_with_none_section_still_passes(tmp_path):
    """D4（只放宽不收紧，口径 A1）：窗口内有 pending、模型写 (none) ⇒ 通过。

    A-sem 只放宽 (none) 触发条件（不再强制零 in_progress 时写 (none)），不新增
    拒绝模式——有未完成项却写 (none) 依旧放行（旧闸门本就放行）。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _apply_plan(session, PLAN_ITEMS_PENDING_TWO)
    session.append(MODEL_COMPLETED, {"content": "历史 " * 900})
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([
        AIMessage(content=_model_sections("(none)")),
        AIMessage(content=_model_sections("(none)")),
    ])

    await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["error_class"] for e in failures] == []
    assert any(event.type == COMPACTION_END for event in session.events)


@pytest.mark.asyncio
async def test_plan_as_of_cut_equals_full_plan_for_auto_path(tmp_path, monkeypatch):
    """R3（自动路径结构等价）：计划更新在 early 窗口内 ⇒ 闸门输入 == 全量 derive。

    捕获 `_validate_plan_section` 实际收到的 `plan_items`，与
    `derive_plan(session.events).items` 逐字节比对。计划更新落在窗口内时，
    A-vis 的窗口过滤是恒等变换——自动路径典型态行为不变（结构保证，非巧合）。
    """
    captured: dict[str, tuple] = {}
    original = compactor._validate_plan_section

    def spy(summary, plan_items):
        captured["plan_items"] = plan_items
        return original(summary, plan_items)

    monkeypatch.setattr(compactor, "_validate_plan_section", spy)

    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _apply_plan(session, PLAN_ITEMS_V1)  # 计划更新在窗口内
    session.append(MODEL_COMPLETED, {"content": "历史 " * 900})
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([AIMessage(content=_model_sections("正在实现清单重注入。"))])

    await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    assert any(event.type == COMPACTION_END for event in session.events)
    assert "plan_items" in captured
    assert captured["plan_items"] == derive_plan(session.events).items


@pytest.mark.asyncio
async def test_recent_plan_update_not_required_in_summary(tmp_path):
    """R2（锁定 A-vis）：in_progress 的计划更新落在 cut 之后。

    V1（item2 in_progress）在 early 窗口内、摘要器可见；V2（item3 in_progress）追加
    在 cut 之后、不产生投影消息，摘要器看不到。模型忠实总结 early 窗口（写 V1 的
    item2）。修后：闸门比较基准 = 窗口内计划（V1）⇒ 通过、bracket 落盘。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _apply_plan(session, PLAN_ITEMS_V1)  # 窗口内
    session.append(MODEL_COMPLETED, {"content": "历史 " * 900})
    session.append(USER_MESSAGE, {"content": "current request"})  # cut = 这条
    _apply_plan(session, PLAN_ITEMS_V2)  # cut 之后（无投影消息）
    model = ScriptedModel([
        AIMessage(content=_model_sections("正在实现清单重注入。")),
        AIMessage(content=_model_sections("正在实现清单重注入。")),
    ])

    await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["error_class"] for e in failures] == []
    assert any(event.type == COMPACTION_END for event in session.events)
