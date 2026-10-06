"""T12h（#647）：source-range 拒绝必须可观测（区别于"无须压缩"）。

复现自暴力测试报告 T12h：source_ranges 与消息失配时，模型返回的合法摘要被
弃用并返回旧投影，`failures=[]`——调用方无法区分"无须压缩"与"来源拒绝"。

修复判据（票面 AC）：
- 来源不可用时 `compacted_turn_count == 0`、`summary`/`bracket_id` 为空、
  messages 保持原投影；已有 attempt 失败记录不丢，新增来源拒绝记录可区别于
  无需压缩（有界 `error_class`，attempt=0 标记非摘要尝试）。
- 同一 fixture 在 `token_estimate > hard` 时受控异常携带可诊断来源拒绝；
  不写成功 bracket。
- 合法 ranges 与"首次失败第二次成功"不受影响；预检（0 次尝试）自 #639 阶段3a
  起改为携带一条有界诊断（`preflight_request_exceeds_hard_limit`，attempt=0），
  与来源失配、无须压缩仍凭 error_class 可区分。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.context.compactor import (
    ContextCompactor,
    ContextWindowExceededError,
)
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import USER_MESSAGE
from agent_harness.session.event import SessionEvent
from tests.scripted_model import ScriptedModel

#: 模型撰写的四节（与 test_compactor.py 同一合法摘要剧本）。
MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


def _messages() -> list:
    return [
        HumanMessage(content="读取 old.txt 后继续。"),
        AIMessage(content="old analysis " * 600),
        HumanMessage(content="current"),
    ]


def _events() -> list[SessionEvent]:
    return [
        SessionEvent(seq=1, type=USER_MESSAGE, session_id="s",
                     data={"content": "读取 old.txt 后继续。"}),
    ]


#: 两种"来源不可用"形态（票面复核要求分别覆盖）。
_INVALID_RANGES = {
    "length_mismatch": [(1, 1)],
    "equal_length_invalid_source": [None, None, None],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("ranges", _INVALID_RANGES.values(), ids=_INVALID_RANGES)
async def test_source_rejection_is_observable_in_failures(ranges):
    """来源不可用 ⇒ 原投影 + 一条来源拒绝失败记录（区别于无须压缩的空 failures）。"""
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    messages = _messages()
    result = await ContextCompactor(
        model, max_context_tokens=8000,
    ).compact(
        messages, estimate_message_tokens(messages),
        events=_events(), source_ranges=ranges,
    )
    # 既有语义保持：拒绝写成功 bracket、保留原投影。
    assert result.compacted_turn_count == 0
    assert result.summary is None
    assert result.bracket_id is None
    assert result.messages == messages
    # T12h 修复判据：来源拒绝可观测，且不是摘要尝试（attempt=0）。
    assert len(result.failures) == 1
    assert result.failures[0].error_class == "source_range_unavailable"
    assert result.failures[0].attempt == 0
    assert "source event range" in result.failures[0].message
    # 不为失配伪造第 3 次摘要尝试：合法摘要一次生成、一次调用。
    assert len(model.snapshots) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ranges", _INVALID_RANGES.values(), ids=_INVALID_RANGES)
async def test_source_rejection_over_hard_guard_raises_with_failure(ranges):
    """token_estimate > hard 时受控异常携带可诊断来源拒绝（不写成功 bracket）。"""
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    messages = _messages()
    hard = 8000 * 0.85
    with pytest.raises(ContextWindowExceededError) as error:
        await ContextCompactor(
            model, max_context_tokens=8000,
        ).compact(
            messages, int(hard) + 200,
            events=_events(), source_ranges=ranges,
        )
    assert [failure.error_class for failure in error.value.failures] == [
        "source_range_unavailable",
    ]
    assert error.value.failures[0].attempt == 0
    assert len(model.snapshots) == 1


@pytest.mark.asyncio
async def test_source_rejection_keeps_prior_attempt_failure_records():
    """首次摘要被拒、重试成功、随后来源失配 ⇒ attempt 记录与来源拒绝都在。"""
    model = ScriptedModel([
        AIMessage(content=""),
        AIMessage(content=MODEL_SECTIONS),
    ])
    messages = _messages()
    result = await ContextCompactor(
        model, max_context_tokens=8000,
    ).compact(
        messages, estimate_message_tokens(messages),
        events=_events(), source_ranges=[(1, 1)],
    )
    assert result.bracket_id is None
    assert result.compacted_turn_count == 0
    assert [failure.error_class for failure in result.failures] == [
        "empty_summary",
        "source_range_unavailable",
    ]
    assert [failure.attempt for failure in result.failures] == [1, 0]
    assert len(model.snapshots) == 2


@pytest.mark.asyncio
async def test_valid_source_ranges_still_succeed():
    """合法 source_ranges 不受影响：正常写成功 bracket、无失败记录。"""
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    messages = _messages()
    result = await ContextCompactor(
        model, max_context_tokens=8000,
    ).compact(
        messages, estimate_message_tokens(messages),
        events=_events(), source_ranges=[(1, 1), (2, 2), (3, 3)],
    )
    assert result.compacted_turn_count == 1
    assert result.bracket_id is not None
    assert result.summary is not None
    assert (result.source_seq_start, result.source_seq_end) == (1, 2)
    assert result.failures == []
    assert len(model.snapshots) == 1


@pytest.mark.asyncio
async def test_preflight_rejection_is_observable_and_distinct_from_source_rejection():
    """预检拒绝（0 次尝试）携带一条有界诊断，与来源失配/无须压缩可区分。

    #639 阶段3a 起：预检拒绝不再静默——按 #647 的 attempt=0 纪律落一条
    `preflight_request_exceeds_hard_limit`（有界 error_class + token 数字）。
    调用方仍能凭 error_class 区分三种 0 次尝试形态：无须压缩=空 failures /
    预检拒绝 / 生成后来源失配。
    """
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    # early 段足够大，使摘要请求转录超 hard（8000×0.85=6800）；
    # 调用方入参 token_estimate 在 hard 之内 ⇒ 预检安全继续路径。
    # fixture 取**不可缩小**形态（巨型内容在首条消息）：阶段 A（#639）的缩小
    # 重试被跳过，预检拒绝仍按 3a/647 语义立刻走出口（零摘要调用）。
    # （文本必须不可压缩：连串重复字符会被 BPE 高度合并，转录计不出超额。）
    messages = [
        HumanMessage(content="历史分析 " * 2000),
        AIMessage(content="ok"),
        HumanMessage(content="current"),
    ]
    result = await ContextCompactor(
        model, max_context_tokens=8000,
    ).compact(
        messages, 5000,
        events=_events(), source_ranges=[(1, 1)],
    )
    assert result.compacted_turn_count == 0
    assert result.bracket_id is None
    assert len(model.snapshots) == 0
    # 预检拒绝：一条 attempt=0 的有界诊断（class 不同于来源拒绝）。
    assert [failure.error_class for failure in result.failures] == [
        "preflight_request_exceeds_hard_limit",
    ]
    assert result.failures[0].attempt == 0
    assert result.failures[0].request_token_estimate > result.failures[0].hard_limit
