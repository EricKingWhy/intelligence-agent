"""#648 特征测试（characterization）：超 hard + 无 early 可压 ⇒ fail-closed 拒绝。

票面执行入口："待产品裁决；执行模型只补特征测试/证据，不修改切分与 hard 行为。"
本文件只**刻画当前行为**，不对其是否算 bug 做产品判断；断言与当前冻结代码
（`src/agent_harness/context/compactor.py` 的 `ContextCompactor.compact`）逐字对齐，
供产品裁决"维持拒绝（fail-closed）还是改为支持摘要后块压缩"时作为行为基线。

被刻画的输入形态（issue #648 自包含最小探针的 resume 投影）：

    [HumanMessage(name="context_compaction_summary"),   # 旧压缩摘要
     AIMessage(tool_calls=[...]),                       # 完整 tool 块（配对合法）
     ToolMessage(tool_call_id=...)]

机制：`compactable_early_window` 的 cut = 最后一条 HumanMessage 下标——摘要本身就是
HumanMessage 且位于下标 0，cut 回落到 0，`early = messages[0:0] = []`，没有可压缩的
完整早期用户轮。此时硬护栏比对用调用方入参的有效用量（含 system/运行时快照等口径），
`token_estimate > hard_limit` ⇒ 抛 `ContextWindowExceededError`（写前失败，非
`CompactionPostWriteError`，历史无 bracket）；≤ hard ⇒ 原投影直通返回。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_harness.context.compactor import (
    CompactionPostWriteError,
    ContextCompactor,
    ContextWindowExceededError,
    compactable_early_window,
)
from agent_harness.context.tokens import estimate_message_tokens

#: 票面阈值口径：max_context_tokens=100000 × hard_guard_threshold=0.85（默认）。
MAX_CONTEXT_TOKENS = 100_000
HARD_LIMIT = 85_000


class _CountingModel:
    """记录摘要调用次数的桩——预检拒绝路径必须零调用。"""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, request):
        self.calls += 1
        return AIMessage(content="(unused: preflight must not call the model)")


def _resume_projection():
    """issue #648 探针的恢复投影形态：完整 tool 块 + 无 early 可压。"""
    return [
        HumanMessage(name="context_compaction_summary", content="x"),
        AIMessage(content="", tool_calls=[{"id": "a", "name": "read", "args": {}}]),
        ToolMessage(tool_call_id="a", content="r"),
    ]


def test_resume_projection_has_no_compactable_early_turn():
    """根因机制：cut 落在摘要 HumanMessage 自身（下标 0），early 为空。

    tool 块配对完整（AIMessage + 匹配 ToolMessage），因此拒绝不来自
    `_validate_tool_blocks`，而来自"无完整早期轮可压"分支。
    """
    messages = _resume_projection()
    # 摘要标记被识别（它是 HumanMessage，同时是最后一条 HumanMessage ⇒ cut=0）。
    assert messages[0].name == "context_compaction_summary"
    prefix_end, cut = compactable_early_window(messages)
    assert (prefix_end, cut) == (0, 0)
    # messages-only 估算远小于 hard 线：拒绝用的是调用方入参的有效用量口径，
    # 不是 messages-only 口径。
    assert estimate_message_tokens(messages) < HARD_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("token_estimate", [85_001, 90_000])
async def test_over_hard_without_early_turn_raises_fail_closed(token_estimate):
    """超 hard + 无 early ⇒ fail-closed：抛 ContextWindowExceededError。

    刻画点：异常类型、逐字文案、failures 恒空（预检类失败不算摘要尝试）、
    非"写后"失败（无 bracket 写入）、模型零调用、入参投影不被改动。
    """
    model = _CountingModel()
    messages = _resume_projection()
    before = [message.model_copy() for message in messages]

    with pytest.raises(ContextWindowExceededError) as exc_info:
        await ContextCompactor(
            model, max_context_tokens=MAX_CONTEXT_TOKENS,
        ).compact(messages, token_estimate)

    assert str(exc_info.value) == "No complete early turn can be compacted"
    # 预检类超限（tool 块 / 无完整早期轮 / 请求本身超限）没有尝试记录。
    assert exc_info.value.failures == []
    # 写前失败：历史没有多出 bracket，不得被吞成"未改动"的写后语义。
    assert not isinstance(exc_info.value, CompactionPostWriteError)
    # 摘要模型从未被调用：拒绝发生在任何摘要尝试之前（区别于"双失败后仍超限"
    # 的另一条出口，其文案是 "Summary validation failed and original context
    # exceeds hard guard" 且 failures 非空）。
    assert model.calls == 0
    # 入参投影逐条未被改动（无原地压缩）。
    assert [m.model_dump(mode="json") for m in messages] == [
        m.model_dump(mode="json") for m in before
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("token_estimate", [84_999, 85_000])
async def test_at_or_below_hard_line_returns_original_projection(token_estimate):
    """边界内侧（≤ hard，含恰好压线）不拒绝：原投影直通返回。

    刻画点：不抛异常、消息原样（未压缩、turn 数 0、fallback False）、
    token_estimate 回传 messages-only 口径（P2-4 契约）而非调用方入参、
    无 bracket、模型零调用。注意：这不是"放行超限请求被接受"——hard 比较符
    是严格 `>`，压线值本身按当前冻结代码属于直通区间。
    """
    model = _CountingModel()
    messages = _resume_projection()

    result = await ContextCompactor(
        model, max_context_tokens=MAX_CONTEXT_TOKENS,
    ).compact(messages, token_estimate)

    assert result.messages == messages
    assert result.compacted_turn_count == 0
    assert result.fallback_used is False
    assert result.failures == []
    assert result.bracket_id is None
    # P2-4 契约：直通路径回传 messages-only 估算，不回传调用方入参的有效用量。
    assert result.token_estimate == estimate_message_tokens(messages)
    assert model.calls == 0
