"""#639 阶段 A：preflight 超限后缩小 early 段重试一次（有界）。

preflight 拒绝 = `ContextCompactor.compact` 在**任何摘要尝试之前**发现摘要请求本身
超 hard（`compactor.py` 的 `request_token_estimate > self._hard_limit` 分支，3a 已
补诊断）。阶段 A 在判定式**拒绝之后**、走两条既有出口**之前**，加一次有界缩小重试：

- 区间选择照 deepseek-harness `selectCompactableRange`（PORT DESIGN）：从 early 段
  尾部向前累计，保留**尾部约 16% recent**（`estimate_message_tokens` 同一口径），
  且**不拆 tool 原子块**——切点吸附到块边界，使缩小后的「摘要段」与「保留尾段」都是
  完整 tool 块序列（`_validate_tool_blocks` 可过）。缩小段（即被摘要替换的段）=
  `[prefix_end:narrow_cut]`，保留尾段 = `[narrow_cut:cut]` 逐字留在投影里。
- 用缩小段重组摘要请求，用**同一判定式**再 preflight 一次；通过则走正常摘要流程
  （attempt 1/2 语义不变），仍超则回落现有两条出口（旧投影 / 抛错，逐字不变）。
- 重试**仅一次**（有界）；判定式 `request_token_estimate > self._hard_limit` 本身与
  两条出口一字不动。

落点理由：与既有 compactor 特征测试同目录（`tests/context/`），文件名以票号前缀，
沿用仓库既有 `ScriptedModel` / `make_session` 夹具，不新造基础设施。
"""

import json

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import (
    _SUMMARY_ERROR_CLASSES,
    ContextCompactor,
    ContextWindowExceededError,
    _validate_tool_blocks,
)
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session import MODEL_COMPLETED, TOOL_RESULT, USER_MESSAGE
from agent_harness.session.event import (
    CONTEXT_COMPACTED,
    CONTEXT_COMPACTION_FAILED,
    SessionEvent,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

PREFLIGHT_ERROR_CLASS = "preflight_request_exceeds_hard_limit"

#: aux:compaction 固定摘要 prompt（与 compactor 内一致；用于重算"摘要请求"令牌数）。
_PROMPT = SystemMessage(content=DEFAULT_REGISTRY.assemble("aux:compaction").system_text)

#: 模型撰写的四节（与 tests/context 其余用例同一合法摘要剧本）。
MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


def _round(index: str, result: str) -> list:
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "id": f"call-{index}", "name": "read",
                "args": {"path": f"f{index}.txt"},
            }],
        ),
        ToolMessage(content=result, tool_call_id=f"call-{index}"),
    ]


def _transcript(segment: list) -> HumanMessage:
    return HumanMessage(content=json.dumps(
        [message.model_dump(mode="json") for message in segment],
        ensure_ascii=False,
    ))


def _request_tokens(segment: list) -> int:
    return estimate_message_tokens([_PROMPT, _transcript(segment)])


#: 缩小重试的窗口选择：从尾部向前累计 token，保留尾部约 16% recent；
#: 切点吸附到 tool 块边界（向后吸附——使摘要段只缩不增，见 REPORT 设计记录）。
_RETAIN_RATIO = 0.16


def _expected_narrow_boundary(messages: list, prefix_end: int, cut: int):
    """独立重算阶段 A 的缩小切点（表征 fixture；非实现副本）。

    返回 `(narrow_cut)` 或 `None`（无可缩小段）。切点吸附到**块起点**，保证摘要段
    `[prefix_end:narrow_cut]` 与保留尾段 `[narrow_cut:cut]` 都是完整 tool 块序列。
    """
    early = messages[prefix_end:cut]
    if not early:
        return None
    target = _RETAIN_RATIO * estimate_message_tokens(early)
    accumulated = 0
    k = cut
    for index in range(cut - 1, prefix_end - 1, -1):
        accumulated += estimate_message_tokens([messages[index]])
        k = index
        if accumulated >= target:
            break
    starts: list[int] = []
    index = prefix_end
    while index < cut:
        starts.append(index)
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            index += 1
            while index < cut and isinstance(messages[index], ToolMessage):
                index += 1
        else:
            index += 1
    boundary = max((start for start in starts if start <= k), default=None)
    if boundary is None or boundary <= prefix_end or boundary >= cut:
        return None
    return boundary


def _narrowable_messages() -> tuple[list, int]:
    """可缩小 fixture：`early = [goal] + 20 轮小结果 tool 轮 + 1 个巨型 tool 轮`。

    巨型 tool 轮的单块 token 已超过 early 的 16% ⇒ 尾部保留段 = 该巨型块，缩小段 =
    其前面的全部小轮；切点落在巨型块中间，必须吸附到块起点（不拆 tool pair）。
    """
    messages = [HumanMessage(content="goal")]
    for index in range(20):
        messages += _round(f"r{index}", "ok")
    big_ai = AIMessage(
        content="",
        tool_calls=[{
            "id": "call-big", "name": "read", "args": {"path": "big.txt"},
        }],
    )
    messages.append(big_ai)
    messages.append(ToolMessage(
        content=" ".join(f"w{index}" for index in range(800)),
        tool_call_id="call-big",
    ))
    messages.append(HumanMessage(content="current"))
    return messages, big_ai


def _window_between(narrowed_tokens: int, full_tokens: int) -> int:
    """选一个 max_context_tokens，使 `narrowed < hard < full`（默认阈值 0.85）。"""
    midpoint = (narrowed_tokens + full_tokens) / 2
    window = int(midpoint / 0.85)
    hard = window * 0.85
    assert narrowed_tokens < hard < full_tokens, (narrowed_tokens, hard, full_tokens)
    return window


def _events() -> list[SessionEvent]:
    return [
        SessionEvent(seq=1, type=USER_MESSAGE, session_id="s",
                     data={"content": "goal"}),
    ]


@pytest.mark.asyncio
async def test_full_preflight_rejected_then_narrowed_segment_compacts():
    """全段 preflight 拒绝 + 缩小后放行 → 压缩成功，且打上 narrowed 标记。"""
    messages, big_ai = _narrowable_messages()
    prefix_end, cut = 0, len(messages) - 1
    full_tokens = _request_tokens(messages[prefix_end:cut])
    boundary = _expected_narrow_boundary(messages, prefix_end, cut)
    assert boundary == messages.index(big_ai)  # 吸附到巨型块起点
    narrowed_tokens = _request_tokens(messages[prefix_end:boundary])
    assert narrowed_tokens < full_tokens  # 缩小确实缩小了请求

    window = _window_between(narrowed_tokens, full_tokens)
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    source_ranges = [(i + 1, i + 1) for i in range(len(messages))]
    result = await ContextCompactor(model, max_context_tokens=window).compact(
        messages, estimate_message_tokens(messages),
        events=_events(), source_ranges=source_ranges,
    )

    # 恰好一次摘要调用（缩小重试本身不调用模型）。
    assert len(model.snapshots) == 1
    assert result.compacted_turn_count == 1
    assert result.narrowed is True
    assert result.bracket_id is not None
    # 投影 = 摘要 + 保留尾段（prefix 为空）；巨型块逐字保留、未被摘要替换。
    assert result.messages[1] is big_ai
    assert result.messages[2].tool_call_id == "call-big"
    assert result.messages[-1].content == "current"
    # 摘要 < 被替换的缩小段（early 取缩小段）。
    summary_message = result.messages[0]
    assert estimate_message_tokens([summary_message]) < narrowed_tokens
    # source_seq 区间 = 缩小段的 early_ranges 切片（head = messages[:boundary]）。
    assert (result.source_seq_start, result.source_seq_end) == (1, boundary)
    # 成功路径携带"成功之前"的预检拒绝诊断（全段那条，narrowed=False）。
    assert [f.error_class for f in result.failures] == [PREFLIGHT_ERROR_CLASS]
    assert result.failures[0].attempt == 0
    assert result.failures[0].narrowed is False
    assert result.failures[0].error_class in _SUMMARY_ERROR_CLASSES


@pytest.mark.asyncio
async def test_narrowed_segment_also_rejected_falls_back_to_keep_projection():
    """缩小后仍超限 ⇒ 回落出口①（有效用量 ≤ hard，保留旧投影），零摘要调用。"""
    messages, _big_ai = _narrowable_messages()
    source_ranges = [(i + 1, i + 1) for i in range(len(messages))]
    model = ScriptedModel([])
    # window 取到远小于缩小后请求：全段与缩小段都超 hard。
    result = await ContextCompactor(model, max_context_tokens=2000).compact(
        messages, 500,  # 有效用量 ≤ hard（1700）⇒ 出口①
        events=_events(), source_ranges=source_ranges,
    )

    assert model.snapshots == []  # 零模型调用
    assert result.compacted_turn_count == 0
    assert result.messages == messages  # 旧投影逐字保留
    assert result.narrowed is False
    assert result.bracket_id is None
    # 有界：恰两条 attempt=0 诊断（全段 + 缩小后），不是两轮缩小循环。
    assert [f.error_class for f in result.failures] == [
        PREFLIGHT_ERROR_CLASS, PREFLIGHT_ERROR_CLASS,
    ]
    assert [f.attempt for f in result.failures] == [0, 0]
    assert [f.narrowed for f in result.failures] == [False, True]
    # 缩小后的诊断带缩小段的 request_token_estimate（只增不减的记号字段之外逐字同词表）。
    assert result.failures[1].request_token_estimate < result.failures[0].request_token_estimate
    assert result.failures[1].request_token_estimate > result.failures[1].hard_limit


@pytest.mark.asyncio
async def test_narrowed_segment_also_rejected_falls_back_to_hard_guard_exit():
    """缩小后仍超限 ⇒ 回落出口②（有效用量 > hard，抛错），零摘要调用。"""
    messages, _big_ai = _narrowable_messages()
    source_ranges = [(i + 1, i + 1) for i in range(len(messages))]
    model = ScriptedModel([])
    with pytest.raises(ContextWindowExceededError) as error:
        await ContextCompactor(model, max_context_tokens=2000).compact(
            messages, 1701,  # 有效用量 > hard（1700）⇒ 出口②
            events=_events(), source_ranges=source_ranges,
        )
    assert str(error.value) == (
        "Summary validation failed and original context exceeds hard guard"
    )
    assert [f.narrowed for f in error.value.failures] == [False, True]
    assert model.snapshots == []


@pytest.mark.asyncio
async def test_narrowing_impossible_is_byte_identical_to_3a():
    """切点无法缩小（保留尾段吞掉整个 early）⇒ 跳过重试，行为与 3a 逐字节一致。"""
    def _impossible() -> list:
        messages = [HumanMessage(content=" ".join(f"g{i}" for i in range(1200)))]
        messages += _round("only", "ok")
        messages.append(HumanMessage(content="current"))
        return messages

    messages = _impossible()
    prefix_end, cut = 0, len(messages) - 1
    # 该 fixture 下 16% 只在包含首条巨型消息时才达到 ⇒ 无可缩小段。
    assert _expected_narrow_boundary(messages, prefix_end, cut) is None

    full_tokens = _request_tokens(messages[prefix_end:cut])
    window = int(full_tokens / 0.85) - 1  # hard 略低于 full ⇒ 全段预检拒绝
    model = ScriptedModel([])
    result = await ContextCompactor(model, max_context_tokens=window).compact(
        messages, 300,  # 有效用量 ≤ hard ⇒ 出口①
        events=_events(), source_ranges=[(i + 1, i + 1) for i in range(len(messages))],
    )

    # 3a 行为逐字节一致：一条 attempt=0 诊断、旧投影不变、零 bracket、零模型调用。
    assert model.snapshots == []
    assert result.messages == messages
    assert result.compacted_turn_count == 0
    assert result.narrowed is False
    assert len(result.failures) == 1
    assert result.failures[0].attempt == 0
    assert result.failures[0].error_class == PREFLIGHT_ERROR_CLASS
    assert result.failures[0].narrowed is False
    assert result.failures[0].request_token_estimate > result.failures[0].hard_limit


@pytest.mark.asyncio
async def test_narrowing_does_not_split_tool_pair():
    """切点落在巨型 tool 块中间 ⇒ 吸附到块起点；两段都是完整 tool 块序列。"""
    messages, big_ai = _narrowable_messages()
    prefix_end, cut = 0, len(messages) - 1
    big_index = messages.index(big_ai)
    # 先证明 fixture 的"裸"16% 切点确实落在块中间：巨型 ToolMessage 的下标。
    narrow_cut = _expected_narrow_boundary(messages, prefix_end, cut)
    assert narrow_cut == big_index  # 吸附到块起点，而不是块内的 ToolMessage
    # 摘要段与保留尾段都能通过统一 tool 块闸门（无孤立 ToolMessage / 截断 tool_calls）。
    _validate_tool_blocks(messages[prefix_end:narrow_cut])
    _validate_tool_blocks(messages[narrow_cut:cut])

    window = _window_between(
        _request_tokens(messages[prefix_end:narrow_cut]),
        _request_tokens(messages[prefix_end:cut]),
    )
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    result = await ContextCompactor(model, max_context_tokens=window).compact(
        messages, estimate_message_tokens(messages),
        events=_events(), source_ranges=[(i + 1, i + 1) for i in range(len(messages))],
    )

    assert result.narrowed is True
    _validate_tool_blocks(result.messages)  # 整个投影仍是完整 tool 块序列
    # 巨型 AIMessage 及其 ToolMessage 相邻成对出现在投影里（未被切点拆开）。
    assert result.messages[-3] is big_ai
    assert result.messages[-2].tool_call_id == "call-big"


@pytest.mark.asyncio
async def test_builder_persists_narrowed_marker_and_projection(tmp_path):
    """端到端 `builder.build`：成功走缩小段时，CONTEXT_COMPACTED 载荷带 narrowed=true。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "goal"})
    for index in range(20):
        session.append(MODEL_COMPLETED, {
            "content": "",
            "tool_calls": [{
                "id": f"call-r{index}", "name": "read", "args": {"path": "x"},
            }],
        })
        session.append(TOOL_RESULT, {"tool_call_id": f"call-r{index}", "content": "ok"})
    session.append(MODEL_COMPLETED, {
        "content": "",
        "tool_calls": [{"id": "call-big", "name": "read", "args": {"path": "big.txt"}}],
    })
    session.append(TOOL_RESULT, {
        "tool_call_id": "call-big",
        "content": " ".join(f"w{i}" for i in range(800)),
    })
    session.append(USER_MESSAGE, {"content": "current"})

    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    messages = await ContextBuilder(model, max_context_tokens=4600).build(session)

    assert len(model.snapshots) == 1
    compacted = [e for e in session.events if e.type == CONTEXT_COMPACTED]
    assert len(compacted) == 1
    assert compacted[0].data["narrowed"] is True
    # 投影含摘要 + 逐字保留的巨型 tool 块 + 当前用户消息。
    summary_messages = [
        message for message in messages
        if getattr(message, "name", None) == "context_compaction_summary"
    ]
    assert len(summary_messages) == 1
    assert messages[-1].content == "current"
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "call-big"
        for message in messages
    )
    # "成功尝试之前的失败都在内"：全段预检拒绝那条诊断随成功路径带给调用方，
    # 落成一条 context/compaction_failed（narrowed=False）；缩小段本身成功。
    failure_events = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert len(failure_events) == 1
    assert failure_events[0].data["error_class"] == PREFLIGHT_ERROR_CLASS
    assert failure_events[0].data["attempt"] == 0
    assert failure_events[0].data["narrowed"] is False
