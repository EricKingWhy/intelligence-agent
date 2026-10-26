"""#1497：ContextBuilder 完整预算账本与模型调用前硬闸门。

票面反例「四项各自及组合都计入同一完整账本；H 接受/H+1 拒绝；越限请求
不再调用模型」的落点测试。核心场景全部走**真实** build/compact_now 管线
（不 mock 压缩），保证钉的是生产可达路径，不是 mock 人为缝：

R2（清单翻转）：压缩前清单处于静默窗（`plan_tokens=0`，压缩器收到的
reserved_tokens 里清单预留也为 0）⇒ 压缩器所有硬护栏比对
（no-op 守卫 / 成功闸门 / 摘要请求预检）都按"无清单"口径通过；压缩成功
落 bracket 后 `COMPACTION_END` 短路"恒注入"⇒ **完整清单块**在既有全部
护栏之外翻转入账。修复前：越限请求被静默装配发出（build 的压缩后 recheck
刻意不含清单，compact_now 的重投影复核也不含）。

修复语义（#1497 裁决 9-A / 裁决 8-A）：
- ContextBuilder 一份私有 accounted snapshot 覆盖全部请求组成，
  `build()` 尾部、`_finalize()` 之前统一硬闸门（strict `>`，H 接受/H+1 拒绝）；
- 拒绝抛 `ContextWindowExceededError`，携带分项账本（`breakdown`）与
  post-compaction 标记；`failures` 为空 ⇒ 结构性拒绝，runtime
  （`_is_retriable_compaction_failure`）不得自动二次压缩；
- durable pause 收口走 ADR-0033 既有 `_terminal_context_exceeded` 通道，
  本文件只钉 builder 侧契约。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from agent_harness.context.builder import (
    ContextBuilder,
    _render_plan_block,
)
from agent_harness.context.compactor import ContextWindowExceededError
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import COMPACTION_END, MODEL_COMPLETED, USER_MESSAGE
from agent_harness.session.derive import derive_messages_with_source_ranges
from agent_harness.session.plan import apply_plan_update, derive_plan
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

# 长清单文本：量级按"渲染块估算 > 硬护栏"反解（下方有显式断言防漂移）。
_BIG_PLAN_TEXT = "背景说明与约束条件 " * 500
_PLAN_ITEMS = [
    {
        "id": "1",
        "content": _BIG_PLAN_TEXT,
        "activeForm": "执行长清单中",
        "status": "in_progress",
        "source": "agent",
    },
]


def _apply_plan(session, items) -> None:
    outcome = apply_plan_update(session, items)
    assert outcome.ok, outcome.reason


def _model_sections(section5: str) -> str:
    """模型撰写的四节（其余四节由 compactor 程序化生成）。"""
    return (
        "## 已完成工作与关键决策\n已完成读取历史记录。\n\n"
        "## 失败方案\n(none)\n\n"
        f"## 当前进行中状态\n{section5}\n\n"
        "## Next Step\n等待当前请求继续。"
    )


def _fixture_fact_tokens(session) -> int:
    """会话当前保护事实的 token 成本（与 build 同一纯选择器；默认构造的
    builder 无 scoped fact provider ⇒ extra_facts 为空，与被测路径同口径）。"""
    from agent_harness.context.builder import (
        protected_fact_token_count,
        protected_facts_for_context,
    )

    facts = protected_facts_for_context(session.events)
    return protected_fact_token_count(facts)


@pytest.mark.asyncio
async def test_plan_flip_after_compaction_is_rejected_before_model_call(tmp_path):
    """R2：压缩成功落 bracket 后清单恒注入 ⇒ 总账越硬护栏必须拒发。

    修复前现状：压缩器按"无清单"预留通过全部闸门，build 压缩后 recheck
    刻意不含清单 ⇒ 越限请求静默装配、模型被真实调用。
    """
    session = make_session(tmp_path)
    _apply_plan(session, _PLAN_ITEMS)
    session.append(USER_MESSAGE, {"content": "开始。"})
    session.append(MODEL_COMPLETED, {"content": "前情一 " * 700})
    session.append(USER_MESSAGE, {"content": "继续。"})

    # 静默窗：清单更新后 3 条投影事件 ∈ (事件驱动窗=1, 兜底周期=6) ⇒ 压缩前
    # 不注入清单（compact_now 的 reserved_tokens 因此不含清单成本）。
    projection = [
        message for message, _source_range
        in derive_messages_with_source_ranges(session.events)
    ]
    est_pre = estimate_message_tokens(projection)

    # 反解窗口：est_pre 落在 (auto*M, hard*M] —— 触发自动压缩；压缩器沿
    # early-early 窗口路径的闸门（摘要请求预检 / 成功闸门 / 落盘复核）都不含
    # 清单（静默窗预留为 0）⇒ 压缩干净成功、bracket 真实落盘。
    max_context_tokens = int(est_pre / 0.7 * 0.95)
    auto_line = 0.7 * max_context_tokens
    hard_line = 0.85 * max_context_tokens
    assert auto_line < est_pre <= hard_line, (
        f"fixture 未落带内：est={est_pre}, auto={auto_line:.0f}, hard={hard_line:.0f}"
    )
    # 清单渲染块的独立估算必须大到"一旦入账必然越硬护栏"（即使压缩后
    # messages 缩到 0）——这是反例的成因，不是旁证。
    plan_block = SystemMessage(content=_render_plan_block(derive_plan(session.events)))
    plan_est = estimate_message_tokens([plan_block])
    assert plan_est > hard_line, (
        f"fixture 失效：清单块估算 {plan_est} 未超硬护栏 {hard_line:.0f}"
    )

    # 摘要第 5 节须逐字含 in_progress 项的 activeForm（W-29 一致性闸门）。
    model = ScriptedModel([
        AIMessage(content=_model_sections(f"{_PLAN_ITEMS[0]['activeForm']}：正在推进。")),
    ])
    builder = ContextBuilder(
        model,
        max_context_tokens=max_context_tokens,
        auto_compact_threshold=0.7,
        plan_reinject_every_messages=6,
    )

    with pytest.raises(ContextWindowExceededError) as exc_info:
        await builder.build(session)

    # 压缩真实发生且已持久化（bracket 在场）⇒ 这是"压缩成功仍越限"路径，
    # 不是压缩失败路径。
    assert any(e.type == COMPACTION_END for e in session.events)
    error = exc_info.value

    # 结构性拒绝：failures 为空 ⇒ runtime 的再压判定（#1264）不会触发，
    # 落 ADR-0033 durable pause；无自动二次压缩环。
    assert error.failures == []

    # post-compaction 标记：区分"压缩前就装不下"与"压缩后仍装不下"。
    assert "post-compaction" in str(error)

    # 分项账本：可对账到独立估算的清单块；越限主因可指认。
    breakdown = error.breakdown
    assert isinstance(breakdown, dict)
    assert breakdown["plan"] == plan_est
    assert breakdown["messages"] < plan_est
    assert sum(breakdown.values()) > hard_line

    # 调用前硬闸门：模型只被调过一次（摘要），被拒的请求从未发出。
    assert len(model.snapshots) == 1


@pytest.mark.asyncio
async def test_pressure_warning_never_tips_the_request_over_hard_guard(tmp_path):
    """R1：接近硬护栏的 warning 文本不得把请求顶过硬护栏。

    no-op 路径（单条消息、无可压缩早期轮）有效用量落在 [auto, hard) 带内、
    距硬护栏不足一个 warning 文本：旧行为先计入再注入 ⇒ 发出的请求越硬护栏
    （warning 成本此前不进任何护栏比对）；闸门落地后若照抛，则是"提醒接近
    上限"的文本本身让 run 暂停——惩罚方向荒谬。正确语义与 provider 注入的
    remaining 截断同一条纪律：**装不下就不注入**，请求保持在护栏内、run 继续。
    """
    from langchain_core.messages import HumanMessage

    from agent_harness.prompt import DEFAULT_REGISTRY

    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "前情一 " * 200})
    # 不带 usage：纯估算路径（隔离锚维度；锚维度由账本组成测试单独钉）。
    # 有效用量 = 投影 + 保护事实（Session.start 的初始事件自带 work_boundary
    # 事实——不并入会把带内判定解错，压力预算测试同源教训）。
    projection = [
        message for message, _source_range
        in derive_messages_with_source_ranges(session.events)
    ]
    est = estimate_message_tokens(projection) + _fixture_fact_tokens(session)

    # 反解窗口：est 落 (auto*M, hard*M) 带内且距 hard 不足一个 warning 文本。
    max_context_tokens = int(est / 0.84)
    auto_line = 0.7 * max_context_tokens
    hard_line = 0.85 * max_context_tokens
    pressure_text = DEFAULT_REGISTRY.assemble("frame:context_pressure").meta_user_text
    pressure_est = estimate_message_tokens([HumanMessage(content=pressure_text)])
    gap = hard_line - est
    assert auto_line < est < hard_line, (
        f"fixture 未落带内：est={est}, auto={auto_line:.0f}, hard={hard_line:.0f}"
    )
    assert 0 < gap < pressure_est, (
        f"fixture 失效：护栏内余量 {gap:.1f} 未小于 warning 成本 {pressure_est}"
    )

    builder = ContextBuilder(
        ScriptedModel([]), max_context_tokens=max_context_tokens,
    )
    built = await builder.build(session)

    # 不抛（不因 warning 暂停 run）、warning 未注入、请求保持在护栏内。
    assert not any(
        isinstance(message, HumanMessage) and message.content == pressure_text
        for message in built
    ), "护栏内余量装不下 warning 时仍注入 ⇒ 请求被顶过硬护栏"
    assert builder._last_pressure_warning_tokens == 0
    assert estimate_message_tokens(built) <= hard_line


@pytest.mark.asyncio
async def test_hard_guard_boundary_at_build_seam(tmp_path):
    """H 接受 / H+1 拒绝（与 compactor 侧 :434 同一条边界约定，上移到 build seam）。

    内容含受保护事实（goal 事实随内容截断、session/started 带可变数据）⇒
    精确"内容 == 85000"的求解跨会话不稳定；改为在**本会话**上实测有效用量
    T，反解两档窗口：接受档 M 使 T 距 H 不足 1 token（含恰好 == H），拒绝档
    H 下调 0.85（T 越 H 不足 2 token）⇒ 边界语义在 ±1 token 内被钉死，
    浮点表达式与 strict `>` 与既有各闸门逐字相同。
    """
    import math

    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "hello " * 84_990})
    projection = [
        message for message, _source_range
        in derive_messages_with_source_ranges(session.events)
    ]
    total = estimate_message_tokens(projection) + _fixture_fact_tokens(session)
    assert total > 80_000, "fixture 失效：未到边界量级"

    m_accept = math.ceil(total * 20 / 17)  # 0.85 = 17/20
    gap = 0.85 * m_accept - total
    assert 0 <= gap < 1, f"接受档求解失败：gap={gap}"

    built = await ContextBuilder(
        ScriptedModel([]), max_context_tokens=m_accept,
    ).build(session)
    assert built  # 有效用量 ≤ H（距边界不足 1 token）：接受

    with pytest.raises(ContextWindowExceededError):
        await ContextBuilder(
            ScriptedModel([]), max_context_tokens=m_accept - 1,
        ).build(session)  # H 下调 0.85 ⇒ 有效用量越 H：拒绝


@pytest.mark.asyncio
async def test_usage_anchor_survives_into_ledger_on_noop_path(tmp_path):
    """U2：no-op 路径的账本 messages 项保持 Phase A 的 max(估算, usage 锚)。

    旧分叉在 no-op 路径二次裸估算（丢锚）——真实管线下被压缩器自己的锚口径
    守卫遮住，但它是与 Phase A 并存的第二套真相（删除判据的反面）。本用例按
    票面「删除判据」在 build 自足 seam 上钉：即使压缩器守卫不在场（mock
    compact_now 返回 None），build 自己的账本也不丢锚、越限照拒。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
    session.append(
        MODEL_COMPLETED,
        {"content": "历史分析" * 20, "usage": {"prompt_tokens": 900}},
    )
    session.append(USER_MESSAGE, {"content": "next"})
    projection = [
        message for message, _source_range
        in derive_messages_with_source_ranges(session.events)
    ]
    plain_est = estimate_message_tokens(projection)
    assert plain_est < 400  # 反证前提：纯估算远小于锚

    builder = ContextBuilder(
        ScriptedModel([]), max_context_tokens=1_000,
    )

    async def _no_compact(session, **kwargs):
        return None

    builder.compact_now = _no_compact

    with pytest.raises(ContextWindowExceededError) as exc_info:
        await builder.build(session)

    breakdown = exc_info.value.breakdown
    # 锚入账：messages 项 ≥ 真实 usage.prompt_tokens，不被二次裸估算抹掉。
    assert breakdown["messages"] >= 900
    assert breakdown["messages"] > plain_est
    assert exc_info.value.failures == []
