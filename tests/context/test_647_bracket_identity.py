"""#647 T11f：bracket 身份契约——无溯源 ⇒ 无身份（照搬成熟产品）。

设计来源（一手代码，详见 PR 描述"方案依据"）：
- Pi（earendil-works/pi @28dcce2b）：
  packages/coding-agent/src/core/compaction/compaction.ts:104
  "Result from compact() - SessionManager adds uuid/parentUuid when saving"；
  身份只在 packages/coding-agent/src/core/session-manager.ts 的
  appendCompaction（L1262-1288）里由 generateId（L277）铸造。
- DeepSeek Harness（@5badb150）：
  packages/compaction/compaction-basic/src/summarizer.ts:87-106 的
  SummaryResult 无 id 字段；
  packages/compaction/compaction-basic/src/region.ts:204 在
  session.append('compaction/start') 前一刻铸造 compactionId；
  packages/compaction/compaction/src/types.ts:95 定义 compactionId 为
  "Stable identity shared by this compaction's complete durable lifecycle."

本仓映射：
- ContextCompactor.compact() ≈ 纯摘要缝（Pi 的 compact / DSH 的 summarizeWithLlm）：
  成功结果不带 bracket_id（None）——无论是否传 events。
- ContextBuilder.compact_now() ≈ 持久化事务方（Pi 的 appendCompaction /
  DSH 的 compactRegion）：决定持久化的那一刻才铸造 bracket_id，
  并回填到返回的 CompactionResult（对标 DSH CompactionResult 携带
  compactionId + 落盘 seq）。
"""

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import ContextCompactor
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import (
    COMPACTION_END,
    COMPACTION_START,
    MODEL_COMPLETED,
    USER_MESSAGE,
)
from agent_harness.session.event import SessionEvent
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

#: 模型撰写的四节（与 test_compactor_source_rejection.py 同一合法摘要剧本）。
MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


def _messages():
    return [
        HumanMessage(content="读取 old.txt 后继续。"),
        AIMessage(content="old analysis " * 600),
        HumanMessage(content="current"),
    ]


def _events():
    return [
        SessionEvent(
            seq=1,
            type=USER_MESSAGE,
            session_id="s",
            data={"content": "读取 old.txt 后继续。"},
        ),
    ]


@pytest.mark.asyncio
async def test_compactor_direct_call_yields_no_bracket_id():
    """直接调用（events=None）成功：无溯源 ⇒ 无身份，bracket_id 为 None。"""
    result = await ContextCompactor(
        ScriptedModel([AIMessage(content=MODEL_SECTIONS)]),
        max_context_tokens=8000,
    ).compact(_messages(), estimate_message_tokens(_messages()))
    assert result.compacted_turn_count == 1
    assert result.summary is not None
    assert result.bracket_id is None
    assert result.source_seq_start is None
    assert result.source_seq_end is None


@pytest.mark.asyncio
async def test_compactor_event_path_yields_no_bracket_id():
    """事件路径成功：compactor 也不铸造身份（身份只在持久化边界铸造）。"""
    result = await ContextCompactor(
        ScriptedModel([AIMessage(content=MODEL_SECTIONS)]),
        max_context_tokens=8000,
    ).compact(
        _messages(),
        estimate_message_tokens(_messages()),
        events=_events(),
        source_ranges=[(1, 1), (2, 2), (3, 3)],
    )
    assert result.compacted_turn_count == 1
    assert result.summary is not None
    # 有溯源（source_seq 可用）但身份仍不在这里铸造——对标 Pi/DSH。
    assert result.source_seq_start == 1
    assert result.source_seq_end == 2
    assert result.bracket_id is None


@pytest.mark.asyncio
async def test_builder_mints_bracket_id_at_persist_boundary(tmp_path):
    """builder 决定持久化的那一刻铸造 bracket_id，并回填到返回结果。

    对标 Pi appendCompaction / DSH compactRegion：身份是"完整持久化
    生命周期"的身份（DSH types.ts:95），在落盘事务开启处铸造。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
    session.append(USER_MESSAGE, {"content": "current request"})

    result = await ContextBuilder(
        ScriptedModel([AIMessage(content=MODEL_SECTIONS)]),
        max_context_tokens=10_000,
        auto_compact_threshold=0.3,
    ).compact_now(session)

    assert result is not None
    assert result.compacted_turn_count == 1
    # builder 回填了在持久化边界铸造的身份。
    assert result.bracket_id
    uuid.UUID(result.bracket_id)  # 合法 UUID，现场铸造非复用
    # 落盘的三事件携带同一身份。
    starts = [e for e in session.events if e.type == COMPACTION_START]
    ends = [e for e in session.events if e.type == COMPACTION_END]
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0].data["bracket_id"] == result.bracket_id
    assert ends[0].data["bracket_id"] == result.bracket_id


@pytest.mark.asyncio
async def test_builder_mints_fresh_id_per_compaction(tmp_path):
    """每次持久化铸造新身份：两次压缩的 bracket_id 不同（无复用）。"""
    ids = []
    for i in range(2):
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": f"读取旧记录并继续{i}。"})
        session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
        session.append(USER_MESSAGE, {"content": "current request"})
        result = await ContextBuilder(
            ScriptedModel([AIMessage(content=MODEL_SECTIONS)]),
            max_context_tokens=10_000,
            auto_compact_threshold=0.3,
        ).compact_now(session)
        assert result is not None and result.bracket_id
        ids.append(result.bracket_id)
    assert ids[0] != ids[1]
