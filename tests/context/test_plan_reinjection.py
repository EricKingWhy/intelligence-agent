"""W-29 (#383)：进度清单 ↔ 压缩锚点（重注入落地）判据测试。

四条票面判据（docs/tickets/workbench-2026-09-27/W-29-plan-compaction-anchor.md
工作指令 1）：

① 压缩后模型输入含完整清单（逐字比对投影）；
② 清单变更后下一 build 即含新版；
③ 连续 6 条消息无变更时兜底注入一次；
④ 摘要第 5 节与清单不一致时压缩被拒绝且原投影保留。

外加守卫：durable-seq 决策跨实例/重放确定（票面硬约束 2）、清单 token 计入
独立记账与看板"其他"桶（票面硬约束 3）、注入位置契约（§4.6 顺序「清单 →
摘要」）、配置可调与响亮失败。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from agent_harness.config import Settings
from agent_harness.context.builder import (
    _PLAN_BLOCK_HEADING,
    ContextBuilder,
    _render_plan_block,
    _should_inject_plan,
)
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    CONTEXT_COMPACTION_FAILED,
    MODEL_COMPLETED,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.plan import apply_plan_update, derive_plan
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

PLAN_ITEMS_V1 = [
    {"id": "1", "content": "梳理压缩规格", "activeForm": "梳理压缩规格中",
     "status": "completed", "source": "agent"},
    {"id": "2", "content": "实现清单重注入", "activeForm": "实现清单重注入中",
     "status": "in_progress", "source": "agent"},
]

#: V2 必须满足 W-26 状态机：项 2 in_progress→completed、新项 3 以 in_progress 出生。
PLAN_ITEMS_V2 = [
    {"id": "1", "content": "梳理压缩规格", "activeForm": "梳理压缩规格中",
     "status": "completed", "source": "agent"},
    {"id": "2", "content": "实现清单重注入", "activeForm": "实现清单重注入中",
     "status": "completed", "source": "agent"},
    {"id": "3", "content": "验证压缩接班", "activeForm": "验证压缩接手中",
     "status": "in_progress", "source": "agent"},
]

#: 全部完成的清单（零 in_progress）——第 5 节必须是 (none) 的形态。
#: 新 id 项以 completed 出生合法（W-26：新项出生不受状态机约束）。
PLAN_ITEMS_ALL_DONE = [
    {"id": "1", "content": "梳理压缩规格", "activeForm": "梳理压缩规格中",
     "status": "completed", "source": "agent"},
    {"id": "2", "content": "实现清单重注入", "activeForm": "实现清单重注入中",
     "status": "completed", "source": "agent"},
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


def _plan_block(messages):
    return next(
        (m for m in messages
         if isinstance(m, SystemMessage) and m.content.startswith(_PLAN_BLOCK_HEADING)),
        None,
    )


def test_builder_rejects_non_positive_fallback_period():
    with pytest.raises(ValueError, match="plan_reinject_every_messages"):
        ContextBuilder(ScriptedModel([]), plan_reinject_every_messages=0)


def test_settings_plan_period_default_and_validation():
    assert Settings.model_fields["plan_reinject_every_messages"].default == 6
    with pytest.raises(ValidationError):
        Settings(plan_reinject_every_messages=0)


@pytest.mark.asyncio
async def test_after_compaction_model_input_contains_full_plan_verbatim(tmp_path):
    """判据①：压缩后模型输入含完整清单，逐字等于 derive_plan 投影的渲染。

    清单变更后隔了 2 条投影消息才 build（默认节奏的静默窗，见判据③的负例），
    压缩落 bracket 后决策重估进入"恒注入"——本用例同时锁住该重估行为。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    session.append(MODEL_COMPLETED, {"content": "前情一 " * 600})
    session.append(USER_MESSAGE, {"content": "换一部分。"})
    _apply_plan(session, PLAN_ITEMS_V1)
    session.append(MODEL_COMPLETED, {"content": "前情二 " * 600})
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([AIMessage(content=_model_sections("正在实现清单重注入。"))])
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    assert any(event.type == COMPACTION_END for event in session.events)
    expected = _render_plan_block(derive_plan(session.events))
    block = _plan_block(messages)
    assert block is not None
    assert block.content == expected  # 逐字比对投影（含清单两行、状态、id、source）
    # §4.6 顺序：清单块在压缩摘要之前。
    summary_index = next(
        i for i, m in enumerate(messages)
        if isinstance(m, SystemMessage) and m.content.startswith("## 原始目标与用户约束")
    )
    assert messages.index(block) < summary_index

    # 验收「压缩接班后清单逐字一致（事件重放验证）」：重载事件流重建，块逐字相同。
    reloaded = Session.load(JsonlSessionStore(root=tmp_path), session.session_id)
    rebuilt = await ContextBuilder(
        ScriptedModel([]), max_context_tokens=10000,
    ).build(reloaded)
    rebuilt_block = _plan_block(rebuilt)
    assert rebuilt_block is not None
    assert rebuilt_block.content == expected


@pytest.mark.asyncio
async def test_plan_change_next_build_contains_new_version(tmp_path):
    """判据②：清单变更后下一 build 即含新版（事件驱动窗口，stale=1）。

    生产形态复现（P2-2 审查修正）：update_plan 工具回合 = MODEL_COMPLETED
    (tool_calls) → `task/plan_updated` → TOOL_RESULT → 下一次 build。TOOL_RESULT
    钉住事件驱动窗口 =1 的边界——窗口若回归成 0，本用例即红（stale=1 落进
    静默窗，生产「变更后下一 build 即含新版」会破）。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session, PLAN_ITEMS_V1)
    session.append(MODEL_COMPLETED, {"content": "推进中。"})
    session.append(MODEL_COMPLETED, {
        "content": "",
        "tool_calls": [{"id": "call-plan-2", "name": "update_plan",
                        "args": {"items": PLAN_ITEMS_V2}}],
    })
    _apply_plan(session, PLAN_ITEMS_V2)
    session.append(TOOL_RESULT, {
        "tool_call_id": "call-plan-2", "content": "清单已更新。",
    })

    messages = await ContextBuilder(ScriptedModel([])).build(session)

    block = _plan_block(messages)
    assert block is not None
    expected = _render_plan_block(derive_plan(session.events))
    assert block.content == expected
    assert "验证压缩接班" in block.content  # 新版 item 3 在场
    # 尚无压缩摘要：块落在开头系统块区（首个 SystemMessage 位）。
    assert messages[0] is block
    assert not any(event.type == COMPACTION_END for event in session.events)


@pytest.mark.asyncio
async def test_fallback_injects_after_six_stale_messages(tmp_path):
    """判据③：连续 6 条投影消息无清单变更时兜底注入一次。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session, PLAN_ITEMS_V1)
    for i in range(3):
        session.append(MODEL_COMPLETED, {"content": f"进展 {i}"})
        session.append(USER_MESSAGE, {"content": f"继续 {i}"})
    # 距清单变更恰好 6 条投影消息（M1/U2/M2/U3/M3/U4）。

    messages = await ContextBuilder(ScriptedModel([])).build(session)

    block = _plan_block(messages)
    assert block is not None
    assert block.content == _render_plan_block(derive_plan(session.events))


@pytest.mark.asyncio
async def test_quiet_window_and_planless_session_do_not_inject(tmp_path):
    """节奏负例：静默窗（2..5 条无变更）不注入；无清单会话永不注入。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session, PLAN_ITEMS_V1)
    session.append(MODEL_COMPLETED, {"content": "推进一。"})
    session.append(USER_MESSAGE, {"content": "继续。"})
    session.append(MODEL_COMPLETED, {"content": "推进二。"})
    # 距清单变更 3 条投影消息：落在静默窗 (1, 6)。

    messages = await ContextBuilder(ScriptedModel([])).build(session)
    assert _plan_block(messages) is None

    planless = make_session(tmp_path)
    planless.append(USER_MESSAGE, {"content": "普通会话。"})
    planless.append(MODEL_COMPLETED, {"content": "回复。"})
    plain = await ContextBuilder(ScriptedModel([])).build(planless)
    assert _plan_block(plain) is None


@pytest.mark.asyncio
async def test_fallback_period_is_configurable(tmp_path):
    """兜底周期可配置（票面契约 2）：period=2 时同一事件流在静默窗内即注入。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session, PLAN_ITEMS_V1)
    session.append(MODEL_COMPLETED, {"content": "推进一。"})
    session.append(USER_MESSAGE, {"content": "继续。"})
    session.append(MODEL_COMPLETED, {"content": "推进二。"})

    messages = await ContextBuilder(
        ScriptedModel([]), plan_reinject_every_messages=2,
    ).build(session)
    assert _plan_block(messages) is not None


@pytest.mark.asyncio
async def test_inconsistent_plan_section_rejects_compaction(tmp_path):
    """判据④：摘要第 5 节缺 in_progress 项 ⇒ 压缩被拒、原投影保留。

    两次尝试同一缺陷各落一条 `context/compaction_failed`
    （error_class=plan_section_mismatch，W-04 失败留痕通道复用）。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _apply_plan(session, PLAN_ITEMS_V1)
    session.append(MODEL_COMPLETED, {"content": "历史 " * 900})
    session.append(USER_MESSAGE, {"content": "current request"})
    stale_section = "摘要覆盖的历史工作已完成。"  # 不含 in_progress 项的任何逐字文本
    model = ScriptedModel([
        AIMessage(content=_model_sections(stale_section)),
        AIMessage(content=_model_sections(stale_section)),
    ])

    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["error_class"] for e in failures] == [
        "plan_section_mismatch", "plan_section_mismatch",
    ]
    # 原投影保留：无压缩 bracket，当前用户消息与大内容原样在场。
    assert not any(
        event.type in {COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END}
        for event in session.events
    )
    assert any(
        getattr(m, "content", None) == "current request" for m in messages
    )
    assert any("历史 " * 900 == getattr(m, "content", None) for m in messages)


@pytest.mark.asyncio
async def test_consistent_plan_section_allows_compaction(tmp_path):
    """第 5 节带上 in_progress 项（content 子串命中）⇒ 压缩照常发生。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _apply_plan(session, PLAN_ITEMS_V1)
    session.append(MODEL_COMPLETED, {"content": "历史 " * 900})
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([AIMessage(content=_model_sections("正在实现清单重注入。"))])

    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    assert any(event.type == COMPACTION_END for event in session.events)
    assert _plan_block(messages) is not None


@pytest.mark.asyncio
async def test_plan_section_must_be_none_when_nothing_in_progress(tmp_path):
    """清单存在但零 in_progress ⇒ 第 5 节必须是 (none)；写别的逐字文本被拒。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _apply_plan(session, PLAN_ITEMS_ALL_DONE)
    session.append(MODEL_COMPLETED, {"content": "历史 " * 900})
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([
        # 第一次尝试谎报进行中工作 → 拒绝；第二次守约 → 通过。
        AIMessage(content=_model_sections("还在实现清单重注入。")),
        AIMessage(content=_model_sections("(none)")),
    ])

    await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)

    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["error_class"] for e in failures] == ["plan_section_mismatch"]
    assert any(event.type == COMPACTION_END for event in session.events)


def test_injection_decision_is_replay_deterministic(tmp_path):
    """票面硬约束 2：决策只依赖事件流（durable seq）——重载后逐字节同一决策。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session, PLAN_ITEMS_V1)
    session.append(MODEL_COMPLETED, {"content": "推进。"})

    first = _should_inject_plan(session.events, 6)
    assert first is not None

    reloaded = Session.load(JsonlSessionStore(root=tmp_path), session.session_id)
    second = _should_inject_plan(reloaded.events, 6)
    assert first == second


@pytest.mark.asyncio
async def test_plan_block_tokens_accounted_independently(tmp_path):
    """票面硬约束 3：清单块 token 计入独立记账口，并折进看板"其他"桶。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session, PLAN_ITEMS_V1)

    builder = ContextBuilder(ScriptedModel([]))
    messages = await builder.build(session)

    block = _plan_block(messages)
    assert block is not None
    assert builder._last_plan_tokens > 0
    snapshot = builder.usage_snapshot(session)
    # 独立记账的实测值如数进"other"（未归类注入的定义性内容），used_tokens 含它。
    assert snapshot["other"] >= builder._last_plan_tokens
    assert snapshot["used_tokens"] >= builder._last_plan_tokens


class _RecordingProvider:
    """记录 `_with_providers` 实际分配给 provider 的 remaining（预算观察口）。"""

    name = "recorder"

    def __init__(self) -> None:
        self.captured: int | None = None

    async def select(self, session, remaining):
        self.captured = remaining
        return []


class _FailingModel:
    """摘要模型双失败替身（W-04 语义：至多重试一次）。"""

    async def ainvoke(self, messages):
        raise TimeoutError("summary timed out")


#: 压缩夹具大内容：实测 ≈5626（Human 投影）/≈5640（AI 回合）token（幂等确定性；
#: auto=0.5×10000=5000 ⇒ 必进压缩路径）。
BIG_CONTENT = "前情占位内容。" * 800
#: 阈值边缘夹具：实测 ≈4576（Human 投影）/≈4590（AI 回合）token，无清单时 < auto
#: （5000），加大清单块后 > 5000。
THRESH_CONTENT = "前情占位内容。" * 650

#: 50 项清单（恰好到达软上限，不超）：49 pending + 1 in_progress（实测清单块 ≈1710 token）。
PLAN_ITEMS_50 = [
    {"id": str(i), "content": f"待办事项第{i}号",
     "activeForm": f"处理第{i}号事项中",
     "status": ("in_progress" if i == 7 else "pending"), "source": "agent"}
    for i in range(1, 51)
]


@pytest.mark.asyncio
async def test_plan_tokens_counted_in_below_auto_budget(tmp_path):
    """清单 token 计入 token_estimate 后流进 provider remaining（精确算术）。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session, PLAN_ITEMS_V1)
    provider = _RecordingProvider()
    builder = ContextBuilder(
        ScriptedModel([]), max_context_tokens=10000, context_providers=[provider],
    )

    await builder.build(session)

    assert provider.captured is not None
    assert builder._last_plan_tokens > 0
    expected = (int(10000 * 0.85)
                - estimate_message_tokens(session.derive_messages())
                - builder._last_plan_tokens)
    assert provider.captured == expected


@pytest.mark.asyncio
async def test_plan_tokens_counted_in_post_compaction_provider_budget(tmp_path):
    """压缩成功路径：remaining = int(hard) −（压缩后投影 + 清单块）。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    session.append(MODEL_COMPLETED, {"content": BIG_CONTENT})
    _apply_plan(session, PLAN_ITEMS_V1)  # 清单事件在 M_big 之后 ⇒ stale=1（事件驱动窗）
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([AIMessage(content=_model_sections("正在实现清单重注入。"))])
    provider = _RecordingProvider()
    builder = ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.5,
        context_providers=[provider],
    )

    await builder.build(session)

    assert any(e.type == COMPACTION_END for e in session.events)  # 压缩确实发生
    assert builder._last_plan_tokens > 0
    expected = (int(10000 * 0.85)
                - estimate_message_tokens(session.derive_messages())
                - builder._last_plan_tokens)
    assert provider.captured == expected


@pytest.mark.asyncio
async def test_plan_block_pushes_estimate_over_auto_threshold(tmp_path):
    """无清单 ≈4576 < auto（5000）不压缩；50 项清单块 ≈1710 ⇒ 6286 > 5000 必须压缩。"""
    big = estimate_message_tokens([HumanMessage(content=THRESH_CONTENT)])
    assert 4300 < big < 4900  # 夹具自检：无清单确实在 auto 之下

    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    session.append(MODEL_COMPLETED, {"content": THRESH_CONTENT})
    _apply_plan(session, PLAN_ITEMS_50)
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([AIMessage(content=_model_sections("正在处理待办事项第7号。"))])

    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.5,
    ).build(session)

    assert any(e.type == COMPACTION_END for e in session.events)
    block = _plan_block(messages)
    assert block is not None
    assert "待办事项第7号" in block.content  # 50 项全表在场（压缩锚点）


@pytest.mark.asyncio
async def test_safe_continue_provider_budget_not_double_counted(tmp_path):
    """CompactionResult.token_estimate 契约统一为 messages-only 后，直通路径
    不再让 builder 的补回项（sys+rt+plan）二次计入 provider 预算。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    session.append(MODEL_COMPLETED, {"content": BIG_CONTENT})
    _apply_plan(session, PLAN_ITEMS_V1)  # stale=1 ⇒ 注入 ⇒ plan_tokens > 0
    session.append(USER_MESSAGE, {"content": "current request"})
    provider = _RecordingProvider()
    builder = ContextBuilder(
        _FailingModel(), max_context_tokens=10000, auto_compact_threshold=0.5,
        context_providers=[provider],
    )

    messages = await builder.build(session)

    # 双失败安全继续：无 bracket、原投影保留、两次失败留痕（W-04 通道）
    assert not any(e.type == COMPACTION_END for e in session.events)
    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["attempt"] for e in failures] == [1, 2]
    assert _plan_block(messages) is not None  # 清单块照常在场（与压缩成败无关）
    assert builder._last_plan_tokens > 0
    expected = (int(10000 * 0.85)
                - estimate_message_tokens(session.derive_messages())
                - builder._last_plan_tokens)
    assert provider.captured == expected
