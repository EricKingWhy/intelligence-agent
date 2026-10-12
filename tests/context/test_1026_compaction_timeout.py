"""#1026：压缩超时硬编码 30s 与 ~167k tok 摘要请求错配（B1 TDD 清单）。

九条清单中后端七条（1–7、9）在本文件；前端控件对齐（清单 8）在
`web/src/lib/runBudget.test.ts` 与 `web/src/components/PausedPanel.test.tsx`。

症状（票面 F-06）：`summary_timeout_seconds` 默认 30.0 硬编码、两次 attempt 共用
同一固定墙钟、不分块（166,892 ≤ hard 170,000 不进收窄）、失败 → run/paused。
修法（裁决记录）：① 墙钟随请求规模缩放 + Settings 可配；② 分块摘要（递进归并
变体，spike 已验证过闸条件）；③ 确定性截断降级（裁决 A，spec 06 §5）。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent_harness.context.compactor import (
    _CHUNK_CARRY_CHAR_CAP,
    ContextCompactor,
    ContextWindowExceededError,
    _chunk_early,
    _clip_block_to_budget,
    _fallback_message_budget,
)
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session import CONTEXT_COMPACTED, USER_MESSAGE

#: 模型撰写的四节（与 tests/context/test_compactor.py 同形）；第 5 节带进行中
#: 计划项 content 的逐字文本，供 W-29 #383 计划闸通过。
MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录。

## 失败方案
(none)

## 当前进行中状态
实现清单重注入（进行中）。

## Next Step
等待当前请求继续。"""


class SlowSummaryModel:
    """模拟 prefill 主导的摘要延迟：sleep = base + tokens × per_token。

    记录每次 ainvoke 的请求（供单请求有界断言），恒返回合法四节输出。
    """

    def __init__(self, base_sleep: float, per_token_sleep: float):
        self.base_sleep = base_sleep
        self.per_token_sleep = per_token_sleep
        self.requests: list[list] = []

    async def ainvoke(self, messages):
        self.requests.append(list(messages))
        await asyncio.sleep(
            self.base_sleep + estimate_message_tokens(messages) * self.per_token_sleep)
        return AIMessage(content=MODEL_SECTIONS)


class AlwaysTimeoutModel:
    """恒超时的摘要模型（模拟 provider 停摆）。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise TimeoutError("summary timed out")


def _early_messages(analysis_chars: int) -> list:
    """最小 early 窗口：两轮历史（early = messages[:2]）+ 当前请求（recent）。"""
    return [
        HumanMessage(content="旧任务开始。" + "a" * analysis_chars),
        AIMessage(content="分析完成。" + "b" * analysis_chars),
        HumanMessage(content="current"),
    ]


def _request_tokens(early: list) -> int:
    """compact() 的摘要请求估算（同形：prompt + early 转录）。"""
    return estimate_message_tokens([
        SystemMessage(content=DEFAULT_REGISTRY.assemble("aux:compaction").system_text),
        HumanMessage(content=json.dumps(
            [m.model_dump(mode="json") for m in early], ensure_ascii=False)),
    ])


def _sized_compactor(messages: list, *, ratio: float, chunk_max: int,
                     base_timeout: float, per_kt: float,
                     model) -> tuple[ContextCompactor, int]:
    """按请求规模定标：hard = 0.85·max ≥ request 且 threshold = ratio·hard < request。"""
    request_tokens = _request_tokens(messages[:2])
    compactor = ContextCompactor(
        model, max_context_tokens=int(request_tokens * 1.4),
        summary_timeout_seconds=base_timeout,
        summary_timeout_per_kilotoken_seconds=per_kt,
        summary_chunk_request_ratio=ratio, summary_chunk_max_tokens=chunk_max,
    )
    assert request_tokens <= compactor._hard_limit
    assert request_tokens > compactor._hard_limit * ratio
    return compactor, request_tokens


# ── 清单 1：固定墙钟复现 → 缩放/分块后通过 ────────────────────────────────


@pytest.mark.asyncio
async def test_large_summary_request_no_longer_killed_by_fixed_wall_clock():
    """~票面场景：单请求摘要延迟随规模增长 > 固定墙钟 ⇒ 现状 timeout×2。

    修后：请求超分块阈值走分块（每块请求小、各自按规模取墙钟）⇒ 摘要成功、
    result.chunked 置位、无 timeout 失败记录（验收 1 的「或已分块」臂）。
    红证据（旧码，见 checkpoint）：单请求 sleep ≈ 1.6s > 0.3s 固定墙钟 → 双
    attempt timeout、summary=None。
    """
    messages = _early_messages(analysis_chars=6000)
    model = SlowSummaryModel(base_sleep=0.05, per_token_sleep=0.0006)
    compactor, _request_tokens = _sized_compactor(
        messages, ratio=0.5, chunk_max=2000, base_timeout=0.3, per_kt=1.0,
        model=model)
    result = await compactor.compact(
        messages, estimate_message_tokens(messages))
    assert result.summary is not None, (
        f"摘要应成功；failures={result.failures}")
    assert result.chunked is True, "超阈值请求必须走分块路径（验收 3）"
    assert not [f for f in result.failures if f.error_class == "timeout"], (
        "分块路径下单请求墙钟按块规模取，不得再以固定墙钟超时")
    assert len(model.requests) >= 2, "分块 = 多次有界请求"


# ── 清单 2：Settings 透传 ────────────────────────────────────────────────


def test_settings_summary_timeout_reaches_builder():
    """Settings 四项经 builder 形参进入构造点（验收 2 的透传链）。"""
    from agent_harness.context.builder import ContextBuilder

    builder = ContextBuilder(
        object(), summary_timeout_seconds=77.0,
        summary_timeout_per_kilotoken_seconds=0.5,
        summary_chunk_request_ratio=0.6, summary_chunk_max_tokens=12_000,
    )
    assert builder._summary_timeout_seconds == 77.0
    assert builder._summary_timeout_per_kilotoken_seconds == 0.5
    assert builder._summary_chunk_request_ratio == 0.6
    assert builder._summary_chunk_max_tokens == 12_000


def test_builder_construction_site_passes_timeout_to_compactor(tmp_path, monkeypatch):
    """构造点把 builder 配置原样传给 ContextCompactor（compact_now 的透传）。"""
    from agent_harness.context import builder as builder_module
    from agent_harness.context.builder import ContextBuilder
    from tests.conftest import make_session

    captured: dict = {}

    class SpyCompactor(builder_module.ContextCompactor):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(builder_module, "ContextCompactor", SpyCompactor)
    builder = ContextBuilder(
        object(), summary_timeout_seconds=77.0,
        summary_timeout_per_kilotoken_seconds=0.5,
        summary_chunk_request_ratio=0.6, summary_chunk_max_tokens=12_000,
    )
    asyncio.run(builder.compact_now(make_session(tmp_path)))
    assert captured.get("summary_timeout_seconds") == 77.0
    assert captured.get("summary_timeout_per_kilotoken_seconds") == 0.5
    assert captured.get("summary_chunk_request_ratio") == 0.6
    assert captured.get("summary_chunk_max_tokens") == 12_000


def test_settings_rejects_nonpositive_summary_timeout():
    """配置面 fail-closed：非正超时 / 非法 ratio 响亮拒绝。"""
    import pydantic

    from agent_harness.config import Settings

    with pytest.raises(pydantic.ValidationError):
        Settings(summary_timeout_seconds=0)
    with pytest.raises(pydantic.ValidationError):
        Settings(summary_timeout_per_kilotoken_seconds=-1)
    with pytest.raises(pydantic.ValidationError):
        Settings(summary_chunk_request_ratio=1.5)


# ── 清单 3：缩放公式（含消融两候选的锚点） ────────────────────────────────


def test_timeout_formula_scales_with_request_size():
    """候选①线性缩放：base + kt × slope。167k 锚点 ≥ 60s（阶段 A 实测锚）。"""
    compactor = ContextCompactor(
        object(), summary_timeout_seconds=30.0,
        summary_timeout_per_kilotoken_seconds=0.25)
    assert compactor._summary_timeout_for(0) == 30.0
    assert compactor._summary_timeout_for(167_000) == pytest.approx(71.75)
    assert compactor._summary_timeout_for(30_000) == pytest.approx(37.5)
    # 消融候选②（固定大墙钟）= slope 0 的退化形态，配置面可达。
    fixed = ContextCompactor(
        object(), summary_timeout_seconds=90.0,
        summary_timeout_per_kilotoken_seconds=0.0)
    assert fixed._summary_timeout_for(167_000) == 90.0
    assert fixed._summary_timeout_for(30_000) == 90.0


# ── 清单 4：分块触发 + 事件可观测 ────────────────────────────────────────


def _request_sized_builder(model, *, max_multiplier: float = 1.25, **kwargs):
    """builder 定标：用真实 prompt+转录估算请求，按倍数设 max_context_tokens。"""
    from agent_harness.context.builder import ContextBuilder

    early = [
        HumanMessage(content="旧任务一。" + "a" * 20000),
        HumanMessage(content="旧任务二。" + "b" * 20000),
    ]
    request_tokens = _request_tokens(early)
    return ContextBuilder(
        model, max_context_tokens=int(request_tokens * max_multiplier),
        auto_compact_threshold=0.5, summary_chunk_max_tokens=2000,
        summary_chunk_request_ratio=0.5, **kwargs)


@pytest.mark.asyncio
async def test_chunked_flag_reaches_result_and_event(tmp_path):
    """超阈值 ⇒ result.chunked 置位并落 CONTEXT_COMPACTED 事件（验收 3）。"""
    from agent_harness.context.builder import ContextBuilder
    from agent_harness.model.scripted import ScriptedModel
    from tests.conftest import make_session

    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "旧任务一。" + "a" * 20000})
    session.append(USER_MESSAGE, {"content": "旧任务二。" + "b" * 20000})
    session.append(USER_MESSAGE, {"content": "current"})
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)] * 12)
    builder = _request_sized_builder(model)
    messages = await builder.build(session)
    assert any(
        isinstance(m, HumanMessage) and m.name == "context_compaction_summary"
        for m in messages
    ), f"应发生压缩；events={[e.type for e in session.events]}"
    compacted = [e for e in session.events if e.type == CONTEXT_COMPACTED]
    assert compacted and compacted[-1].data.get("chunked") is True
    # 未超阈值的普通压缩（请求 ≤ 阈值、靠 recent 补足触发）不带分块标记
    # （单请求路径等价）。定标：max = 2.4×req ⇒ threshold = 1.02×req ≥ req、
    # auto = 1.68×req，recent ≈ req 使 total > auto 且 target 闸可过。
    session2 = make_session(tmp_path)
    early_content = "旧任务历史。" + "analysis " * 3000
    req2 = _request_tokens([HumanMessage(content=early_content)])
    session2.append(USER_MESSAGE, {"content": early_content})
    session2.append(USER_MESSAGE, {"content": "current " + "analysis " * 3000})
    model2 = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    builder2 = ContextBuilder(
        model2, max_context_tokens=int(req2 * 2.4),
        auto_compact_threshold=0.7, summary_chunk_max_tokens=2000,
        summary_chunk_request_ratio=0.5,
    )
    await builder2.build(session2)
    compacted2 = [e for e in session2.events if e.type == CONTEXT_COMPACTED]
    assert compacted2 and compacted2[-1].data.get("chunked") is False


# ── 清单 5：分块单请求有界 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chunked_requests_are_bounded():
    """逐次摘要请求的转录块 ≤ 单块上限（验收 3 的「单请求 token 有界」）。"""
    messages = _early_messages(analysis_chars=12000)
    model = SlowSummaryModel(base_sleep=0.0, per_token_sleep=0.0)
    compactor, _request_tokens = _sized_compactor(
        messages, ratio=0.5, chunk_max=2000, base_timeout=5.0, per_kt=0.0,
        model=model)
    result = await compactor.compact(
        messages, estimate_message_tokens(messages))
    assert result.summary is not None and result.chunked is True
    assert len(model.requests) >= 2
    # 每次请求的最后一条消息是本块转录；JSON 载荷对块内容有转义开销
    # （引号/反斜杠），给 30% 头寸。
    for request in model.requests:
        transcript = request[-1]
        assert estimate_message_tokens([transcript]) <= 2000 * 1.3, (
            f"单块转录超界：{estimate_message_tokens([transcript])}")


@pytest.mark.asyncio
async def test_oversize_atomic_block_is_clipped_to_chunk_bound():
    """单条超大原子消息不可按块边界切分 ⇒ 确定性 head/tail 裁剪后有界（spike 发现 5）。"""
    oversized = AIMessage(content="x" * 40_000)
    messages = [
        HumanMessage(content="旧任务。"),
        oversized,
        HumanMessage(content="current"),
    ]
    model = SlowSummaryModel(base_sleep=0.0, per_token_sleep=0.0)
    compactor, _request_tokens = _sized_compactor(
        messages, ratio=0.5, chunk_max=2000, base_timeout=5.0, per_kt=0.0,
        model=model)
    result = await compactor.compact(
        messages, estimate_message_tokens(messages))
    assert result.summary is not None and result.chunked is True
    for request in model.requests:
        transcript = request[-1]
        assert estimate_message_tokens([transcript]) <= 2000 * 1.3, (
            "裁剪后单块转录必须有界")


# ── 清单 6：tool 原子块配对完整性 ────────────────────────────────────────


def test_chunk_boundaries_never_split_tool_pairs():
    """分块切分不拆 AI tool_call / ToolMessage 对（`_validate_tool_blocks` 语义）。"""
    from agent_harness.context.compactor import _validate_tool_blocks

    pair = [
        AIMessage(content="", tool_calls=[
            {"name": "t", "args": {}, "id": "c1", "type": "tool_call"}]),
        ToolMessage(content="r" * 900, tool_call_id="c1"),
    ]
    early = [
        HumanMessage(content="旧任务。" + "a" * 900),
        *pair,
        AIMessage(content="分析。" + "b" * 900),
    ]
    # bound=600：三块（143 / 548 / 268）互不合并，pair 块不触发裁剪（保身份）。
    chunks = _chunk_early(early, 600)
    assert len(chunks) >= 2
    for chunk in chunks:
        _validate_tool_blocks(chunk)
    flattened = [m for chunk in chunks for m in chunk]
    assert any(m is pair[0] for m in flattened) and any(
        m is pair[1] for m in flattened), "配对消息不得丢失"
    for chunk in chunks:
        has_call = any(
            isinstance(m, AIMessage) and m.tool_calls for m in chunk)
        has_result = any(isinstance(m, ToolMessage) for m in chunk)
        assert has_call == has_result, "AI/Tool 配对必须同块"


# ── 清单 7：确定性截断降级（裁决 A） ─────────────────────────────────────


@pytest.mark.asyncio
async def test_total_failure_with_hard_overflow_falls_back_to_truncation():
    """双 attempt 失败 + 有效用量超硬护栏 ⇒ 截断 CompactionResult（不再必然暂停）。"""
    messages = [
        HumanMessage(content="旧任务一。" + "a" * 3000),
        AIMessage(content="分析二。" + "b" * 3000),
        AIMessage(content="分析三。" + "c" * 3000),
        HumanMessage(content="current"),
    ]
    model = AlwaysTimeoutModel()
    total = estimate_message_tokens(messages)
    # 定标：hard < total（暂停前提成立）、auto ≈ 0.8×total > 两块 < 全部三块
    # （尾部优先恰好保留两块）。
    compactor = ContextCompactor(
        model, max_context_tokens=int(total / 0.87),
        summary_timeout_seconds=0.2, summary_chunk_request_ratio=0.5,
        summary_chunk_max_tokens=2000,
    )
    assert total > compactor._hard_limit

    result = await compactor.compact(messages, total)
    assert result.fallback_used is True, "降级截断必须带 fallback 标记"
    assert result.compacted_turn_count == 0
    assert result.summary is None
    assert result.token_estimate < compactor._auto_limit, (
        "截断产物必须压回 auto_limit 内")
    assert result.messages[-1].content == "current", (
        "recent（当前请求）逐字保留")
    fallbacks = [f for f in result.failures
                 if f.error_class == "deterministic_truncation_fallback"]
    assert len(fallbacks) == 1 and fallbacks[0].attempt == 0
    # 截断保留尽量多 early、尾部优先：auto ≈ 0.78×total 只装得下尾部两块，
    # 头块（最早轮次）被截掉。
    assert len(result.messages) == 3, "尾部两块 + recent"
    assert result.messages[0].content.startswith("分析二。")
    assert result.messages[1].content.startswith("分析三。")


@pytest.mark.asyncio
async def test_untruncatable_context_still_raises():
    """recent+prefix 自身就超预算 ⇒ 完全不可截断，落既有异常兜底（暂停面不变）。"""
    messages = [
        HumanMessage(content="旧任务。" + "a" * 300),
        AIMessage(content="分析。" + "b" * 300),
        HumanMessage(content="current" + "c" * 20_000),
    ]
    model = AlwaysTimeoutModel()
    total = estimate_message_tokens(messages)
    # max=6000：hard = 5100 < total（暂停前提）；auto = 4200 < prefix+recent
    # （完全不可截断）。
    compactor = ContextCompactor(
        model, max_context_tokens=6000, summary_timeout_seconds=0.2,
        summary_chunk_request_ratio=0.5, summary_chunk_max_tokens=2000,
    )
    assert total > compactor._hard_limit
    assert estimate_message_tokens(
        [messages[0], messages[2]]) >= compactor._auto_limit
    with pytest.raises(ContextWindowExceededError):
        await compactor.compact(messages, total)


# ── 清单 9：同会话重复触发按降级收敛（验收 6） ────────────────────────────


@pytest.mark.asyncio
async def test_repeated_compaction_converges_via_fallback(tmp_path):
    """连续 N 次触发压缩、摘要恒败且超硬护栏：每次按降级截断收敛，不抛不砖。"""
    from agent_harness.context.builder import ContextBuilder
    from tests.conftest import make_session

    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "旧任务。" + "a" * 40000})
    session.append(USER_MESSAGE, {"content": "current"})
    builder = ContextBuilder(
        AlwaysTimeoutModel(), max_context_tokens=12_000,
        auto_compact_threshold=0.5, summary_timeout_seconds=0.2,
        summary_chunk_request_ratio=0.5, summary_chunk_max_tokens=2000,
    )
    for _round in range(3):
        messages = await builder.build(session)
        assert messages, "降级截断后本次 build 必须产出可用投影（会话不死）"
        assert estimate_message_tokens(
            messages) < 12_000 * 0.70, "投影压回 auto_limit 内"


# ── 计划块注入（spike 发现 2：计划闸的确定性缓解） ────────────────────────


@pytest.mark.asyncio
async def test_chunked_final_request_carries_plan_items_verbatim(tmp_path):
    """分块下 final 请求注入 derive_plan 窗口产物的逐字计划块（spike 变体 D）。"""
    from agent_harness.model.scripted import ScriptedModel
    from agent_harness.session.plan import apply_plan_update
    from tests.conftest import make_session

    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "旧任务一。" + "a" * 20000})
    session.append(USER_MESSAGE, {"content": "旧任务二。" + "b" * 20000})
    outcome = apply_plan_update(session, [
        {"id": "2", "content": "实现清单重注入",
         "activeForm": "实现清单重注入中", "status": "in_progress",
         "source": "agent"},
    ])
    assert outcome.ok, outcome.reason
    session.append(USER_MESSAGE, {"content": "current"})
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)] * 12)
    builder = _request_sized_builder(model)
    messages = await builder.build(session)
    assert any(
        isinstance(m, HumanMessage) and m.name == "context_compaction_summary"
        for m in messages), "压缩应成功（计划闸以注入块逐字通过）"
    assert any("实现清单重注入" in snapshot.messages[-2].content
               for snapshot in model.snapshots), (
        "分块请求必须携带确定性计划块（逐字内容）")


# ── 修回轮（独立双轴审查 2026-10-12 findings）─────────────────────────────


def test_clip_block_to_budget_terminates_on_all_short_messages():
    """审查 P1 回归：全短消息超预算原子块有限步返回、不越裁越长。

    旧码在 content 长度 4~58 区间「越裁越长」（head/tail + 截断标记比原文
    还长），收敛到 ~58 字符固定点后 while 恒真、break 不触发 ⇒ 挂死而非报错
    （连降级路径都到不了）。本用例在旧码下永不返回。
    """
    from agent_harness.context.compactor import _validate_tool_blocks

    block = [
        AIMessage(content="", tool_calls=[
            {"name": "t", "args": {}, "id": "c1", "type": "tool_call"},
            {"name": "t", "args": {}, "id": "c2", "type": "tool_call"},
        ]),
        ToolMessage(content="r" * 10, tool_call_id="c1"),
        ToolMessage(content="r" * 20, tool_call_id="c2"),
    ]
    # bound 远低于块估算 ⇒ 必然进入裁剪循环；块内全是 4~58 区间的短文本。
    clipped = _clip_block_to_budget(block, 5)
    assert [len(m.content) if isinstance(m.content, str) else -1
            for m in clipped] == [0, 10, 20], (
        "短文本不可再裁 ⇒ 原样放行（替换串不再变短即停，不越裁越长）")
    assert estimate_message_tokens(clipped) > 5, (
        "token 仍超界 ⇒ 如实放行（结构面有界；放行面由 "
        "chunk_bound_released 诊断留痕，见下一用例）")
    _validate_tool_blocks(clipped)  # 放行块结构完好（tool 配对不拆）


@pytest.mark.asyncio
async def test_unclipable_oversize_block_released_with_diagnostic():
    """审查 P2：content 不可裁的超界原子块如实放行 ⇒ chunk_bound_released 留痕。

    有界逃逸口（非文本/超短 content 放行）事后可观测：attempt=0 诊断记入
    既有 failures 通道（不新造事件类型），request_budget_tokens 记被突破的
    单块预算（与单请求路径的 hard 口径可区分）。
    """
    messages = [
        HumanMessage(content="旧任务。" + "a" * 6000),
        AIMessage(content="", tool_calls=[
            {"id": "c1", "name": "write",
             "args": {"path": "big.txt", "content": "y" * 20_000}},
        ]),
        ToolMessage(content="written", tool_call_id="c1"),
        HumanMessage(content="current"),
    ]
    model = SlowSummaryModel(base_sleep=0.0, per_token_sleep=0.0)
    compactor, _request_tokens = _sized_compactor(
        messages, ratio=0.5, chunk_max=2000, base_timeout=5.0, per_kt=0.0,
        model=model)
    result = await compactor.compact(messages, estimate_message_tokens(messages))
    assert result.summary is not None and result.chunked is True, (
        f"分块路径应走通；failures={[f.error_class for f in result.failures]}")
    released = [f for f in result.failures
                if f.error_class == "chunk_bound_released"]
    assert len(released) == 1 and released[0].attempt == 0, (
        "超界放行必须有一条 attempt=0 诊断留痕")
    assert released[0].request_budget_tokens == 2000
    assert released[0].request_token_estimate > released[0].request_budget_tokens, (
        "诊断读数：放行块自身规模 > 被突破的单块预算")


def test_fallback_budget_deducts_non_message_overhead_once():
    """审查 P3：降级预算对非 messages 开销**单扣**（reserved 与 overhead 是同一笔）。

    旧码 `auto − reserved − overhead` 把 pf/sys/rt/plan/paths 五项扣两次
    （方向保守但多丢 early 轮）；修后取两口径较大者扣一次。
    """
    messages = [HumanMessage(content="goal"), HumanMessage(content="next")]
    messages_tokens = estimate_message_tokens(messages)
    auto_limit = 30_000
    # builder 场景：reserved（构造点声明的加回份额）== overhead（token_estimate
    # 的实测非 messages 份额）——同一笔加回的两种计量。
    reserved = 5_000
    assert _fallback_message_budget(
        auto_limit, reserved, messages_tokens + reserved, messages,
    ) == 25_000, "同笔开销只扣一次（旧码会双扣成 20_000）"
    # 两口径不一致时取大者（任一口径下「截断产物 + 加回 ≤ auto」都守得住）。
    assert _fallback_message_budget(
        auto_limit, reserved, messages_tokens + 7_000, messages,
    ) == 23_000
    # B1 防线保留：有效用量吃满预算 ⇒ 负预算 ⇒ 不可截断 ⇒ 落既有异常兜底，
    # 不会假成功放行超窗请求（85001 边界用例的口径前提）。
    assert _fallback_message_budget(
        auto_limit, 0, messages_tokens + 40_000, messages,
    ) < 0


@pytest.mark.asyncio
async def test_chunked_failure_diagnostic_records_per_chunk_scale():
    """审查 P4：分块路径失败诊断的 request_token_estimate 记逐块实际规模。

    单请求路径记全 early 单请求估算；分块路径记已发起块请求的最大实际规模
    （≤ 单块预算 + 携带封顶 + prompt）——两口径可区分。
    """
    messages = _early_messages(analysis_chars=12000)
    model = AlwaysTimeoutModel()
    compactor, request_tokens = _sized_compactor(
        messages, ratio=0.5, chunk_max=2000, base_timeout=0.2, per_kt=0.0,
        model=model)
    result = await compactor.compact(messages, estimate_message_tokens(messages))
    attempts = [f for f in result.failures if f.attempt >= 1]
    assert attempts, "分块 + 恒超时 ⇒ 两次 attempt 失败留痕"
    prompt_tokens = estimate_message_tokens([SystemMessage(
        content=DEFAULT_REGISTRY.assemble("aux:compaction").system_text)])
    carry_cap_tokens = estimate_message_tokens(
        [HumanMessage(content="c" * _CHUNK_CARRY_CHAR_CAP)])
    per_chunk_bound = 2000 * 1.3 + prompt_tokens + carry_cap_tokens
    for failure in attempts:
        assert 0 < failure.request_token_estimate <= per_chunk_bound, (
            f"分块失败诊断须记逐块规模（≤ 单块预算 + 携带封顶 + prompt），"
            f"实得 {failure.request_token_estimate}")
        assert failure.request_token_estimate < request_tokens, (
            f"逐块口径必须与单请求口径（全 early {request_tokens}）可区分")
