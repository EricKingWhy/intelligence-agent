"""T4 #134：dsh 4-event compaction bracket 测试。

验证压缩从单个 CONTEXT_COMPACTED 升级为 replay 确定性 bracket：
  COMPACTION_START (source_seq_start, source_seq_end)
  → CONTEXT_COMPACTED (eight_section summary + source 区间)
  → USER_MESSAGE(replace) — 摘要替代被压缩段
  → COMPACTION_END (bracket_id)

原始被压缩事件保留在 JSONL 里（shadowed），derive_messages 跳过。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import (
    _SUMMARY_HEADINGS,
    ContextCompactor,
    _parse_summary_sections,
)
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    CONTEXT_COMPACTION_FAILED,
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
    SessionEvent,
)
from agent_harness.session.derive import derive_messages, derive_protected_facts
from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.tooling.overflow import ArtifactOverflowHandler
from agent_harness.tooling.result import ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

# ── 摘要 fixtures ──────────────────────────────────────────────────

LEGACY_SUMMARY = """## 目标
用户要求读取文件并总结内容。

## 约束
- 必须保持中文回答
- 文件路径必须在 workspace 内

## 进展
已成功读取 old.txt 文件，内容为历史记录。

## 决策
决定直接展示文件内容而非重新生成。

## 下一步
等待用户的新请求。

## 关键上下文
- 历史文件包含 6000 字的旧数据
- 用户已确认收到文件内容
- 当前工作目录为 /workspace"""

MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


# ── derive_messages 识别 bracket 边界 ───────────────────────────────

class TestDeriveMessagesBracket:
    """derive_messages 正确识别 4-event bracket，跳过 shadowed 段。"""

    def test_shadowed_events_skipped(self):
        """COMPACTION_START..COMPACTION_END 之间的原始事件被跳过。"""
        events = [
            SessionEvent(seq=1, type=USER_MESSAGE, session_id="s1",
                         data={"content": "old message"}),
            SessionEvent(seq=2, type=MODEL_COMPLETED, session_id="s1",
                         data={"content": "old reply"}),
            # bracket 开始：shadow seq 1-2
            SessionEvent(seq=3, type=COMPACTION_START, session_id="s1",
                         data={"bracket_id": "b1",
                               "source_seq_start": 1, "source_seq_end": 2}),
            SessionEvent(seq=4, type=CONTEXT_COMPACTED, session_id="s1",
                         data={"bracket_id": "b1", "summary": LEGACY_SUMMARY,
                               "schema": "six_section",
                               "source_seq_start": 1, "source_seq_end": 2}),
            SessionEvent(seq=5, type=COMPACTION_END, session_id="s1",
                         data={"bracket_id": "b1"}),
            # bracket 后的正常事件
            SessionEvent(seq=6, type=USER_MESSAGE, session_id="s1",
                         data={"content": "current request"}),
        ]
        messages = derive_messages(events)
        # shadowed 的 seq 1-2 不应出现；只有 summary + current request
        assert len(messages) == 2
        assert isinstance(messages[0], HumanMessage)
        assert messages[0].name == "context_compaction_summary"
        assert LEGACY_SUMMARY in messages[0].content
        assert isinstance(messages[1], HumanMessage)
        assert messages[1].content == "current request"

    def test_no_bracket_passes_through(self):
        """没有 bracket 时，derive_messages 行为不变。"""
        events = [
            SessionEvent(seq=1, type=USER_MESSAGE, session_id="s1",
                         data={"content": "hello"}),
            SessionEvent(seq=2, type=MODEL_COMPLETED, session_id="s1",
                         data={"content": "world"}),
        ]
        messages = derive_messages(events)
        assert len(messages) == 2

    def test_incomplete_bracket_does_not_shadow_original_events(self):
        """持久化中断留下的 START/SUMMARY 不能遮蔽原始消息。"""
        events = [
            SessionEvent(seq=1, type=USER_MESSAGE, session_id="s1",
                         data={"content": "不得删除 old_rows；R-042"}),
            SessionEvent(seq=2, type=MODEL_COMPLETED, session_id="s1",
                         data={"content": "old result"}),
            SessionEvent(seq=3, type=COMPACTION_START, session_id="s1",
                         data={"bracket_id": "partial",
                               "source_seq_start": 1, "source_seq_end": 2}),
            SessionEvent(seq=4, type=CONTEXT_COMPACTED, session_id="s1",
                         data={"bracket_id": "partial", "summary": "new summary",
                               "source_seq_start": 1, "source_seq_end": 2}),
        ]
        messages = derive_messages(events)
        assert [message.content for message in messages] == [
            "不得删除 old_rows；R-042", "old result",
        ]

    def test_multiple_brackets(self):
        """多个不重叠的 bracket 都被正确跳过。"""
        events = [
            SessionEvent(seq=1, type=USER_MESSAGE, session_id="s1",
                         data={"content": "first old"}),
            # 第一个 bracket
            SessionEvent(seq=2, type=COMPACTION_START, session_id="s1",
                         data={"bracket_id": "b1",
                               "source_seq_start": 1, "source_seq_end": 1}),
            SessionEvent(seq=3, type=CONTEXT_COMPACTED, session_id="s1",
                         data={"bracket_id": "b1", "summary": "first summary",
                               "schema": "six_section",
                               "source_seq_start": 1, "source_seq_end": 1}),
            SessionEvent(seq=4, type=COMPACTION_END, session_id="s1",
                         data={"bracket_id": "b1"}),
            # 中间的正常事件
            SessionEvent(seq=5, type=USER_MESSAGE, session_id="s1",
                         data={"content": "second old"}),
            # 第二个 bracket
            SessionEvent(seq=6, type=COMPACTION_START, session_id="s1",
                         data={"bracket_id": "b2",
                               "source_seq_start": 5, "source_seq_end": 5}),
            SessionEvent(seq=7, type=CONTEXT_COMPACTED, session_id="s1",
                         data={"bracket_id": "b2", "summary": "second summary",
                               "schema": "six_section",
                               "source_seq_start": 5, "source_seq_end": 5}),
            SessionEvent(seq=8, type=COMPACTION_END, session_id="s1",
                         data={"bracket_id": "b2"}),
            # 最后的正常事件
            SessionEvent(seq=9, type=USER_MESSAGE, session_id="s1",
                         data={"content": "current"}),
        ]
        messages = derive_messages(events)
        # 两个 summary + 一个 current
        assert len(messages) == 3
        assert isinstance(messages[0], HumanMessage)
        assert isinstance(messages[1], HumanMessage)
        assert isinstance(messages[2], HumanMessage)

    def test_newer_covering_bracket_replaces_older_summary(self):
        events = [
            SessionEvent(seq=1, type=USER_MESSAGE, session_id="s1",
                         data={"content": "first old"}),
            SessionEvent(seq=2, type=MODEL_COMPLETED, session_id="s1",
                         data={"content": "first reply"}),
            SessionEvent(seq=3, type=COMPACTION_START, session_id="s1",
                         data={"bracket_id": "b1", "source_seq_start": 1,
                               "source_seq_end": 2}),
            SessionEvent(seq=4, type=CONTEXT_COMPACTED, session_id="s1",
                         data={"bracket_id": "b1", "summary": "old summary",
                               "source_seq_start": 1, "source_seq_end": 2}),
            SessionEvent(seq=5, type=COMPACTION_END, session_id="s1",
                         data={"bracket_id": "b1"}),
            SessionEvent(seq=6, type=USER_MESSAGE, session_id="s1",
                         data={"content": "second old"}),
            SessionEvent(seq=7, type=MODEL_COMPLETED, session_id="s1",
                         data={"content": "second reply"}),
            SessionEvent(seq=8, type=COMPACTION_START, session_id="s1",
                         data={"bracket_id": "b2", "source_seq_start": 1,
                               "source_seq_end": 7}),
            SessionEvent(seq=9, type=CONTEXT_COMPACTED, session_id="s1",
                         data={"bracket_id": "b2", "summary": "new summary",
                               "source_seq_start": 1, "source_seq_end": 7}),
            SessionEvent(seq=10, type=COMPACTION_END, session_id="s1",
                         data={"bracket_id": "b2"}),
            SessionEvent(seq=11, type=USER_MESSAGE, session_id="s1",
                         data={"content": "current"}),
        ]

        messages = derive_messages(events)

        assert [message.content for message in messages] == ["new summary", "current"]


# ── ContextCompactor 产生 bracket 元数据 ────────────────────────────

class TestCompactorBracketMetadata:
    """ContextCompactor.compact() 返回 bracket 元数据。"""

    @pytest.mark.asyncio
    async def test_compact_returns_summary_without_bracket_id(self):
        """compact 返回 summary；bracket_id 为 None（#647 T11f：无溯源 ⇒ 无身份）。

        身份只在持久化边界由 builder 铸造（对标 Pi/DSH），compactor 直调结果
        永不携带可用身份。
        """
        model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
        # #556 裁决 C：目标节由 protected_facts 通道承载（与 builder 同一通路）。
        facts = derive_protected_facts([
            SessionEvent(seq=1, type=USER_MESSAGE, session_id="s",
                         data={"content": "读取 old.txt 后继续。"}),
        ])
        messages = [
            HumanMessage(content="读取 old.txt 后继续。"),
            AIMessage(content="old analysis " * 600),
            HumanMessage(content="current"),
        ]
        result = await ContextCompactor(
            model, max_context_tokens=8000,
        ).compact(messages, estimate_message_tokens(messages), protected_facts=facts)
        # #647 T11f：compactor 不铸造身份（无溯源 ⇒ 无身份）。
        assert result.bracket_id is None
        assert result.summary is not None
        assert result.summary.startswith("## 原始目标与用户约束\n")
        assert "读取 old.txt 后继续。" in result.summary
        assert result.compacted_turn_count == 1
        assert not result.failures
        assert len(model.snapshots) == 1

    @pytest.mark.asyncio
    async def test_compact_summary_is_eight_section(self):
        """摘要采用八节结构，且程序化节位于固定位置。"""
        model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
        messages = [
            HumanMessage(content="读取 old.txt 后继续。"),
            AIMessage(content="old analysis " * 600),
            HumanMessage(content="current"),
        ]
        result = await ContextCompactor(
            model, max_context_tokens=8000,
        ).compact(messages, estimate_message_tokens(messages))
        # 摘要消息应该是 HumanMessage，内容按八节顺序组成
        assert isinstance(result.messages[0], HumanMessage)
        assert result.summary is not None
        assert [line for line in result.summary.splitlines() if line.startswith("## ")] == [
            "## 原始目标与用户约束", "## 保护事实表",
            "## 已完成工作与关键决策", "## 失败方案",
            "## 当前进行中状态", "## Next Step",
            "## 精确标识清单", "## 文件清单",
        ]

    @pytest.mark.asyncio
    async def test_shrink_validation_rejects_larger_summary(self):
        """shrink 校验：摘要必须严格小于被压缩段。"""
        # 构造一个会产生超大摘要的场景
        huge_summary = "x" * 10000  # 比原始消息还大的摘要
        model = ScriptedModel([AIMessage(content=huge_summary)])
        messages = [
            HumanMessage(content="short old"),
            AIMessage(content="done"),
            HumanMessage(content="current"),
        ]
        result = await ContextCompactor(
            model, max_context_tokens=8000,
        ).compact(messages, estimate_message_tokens(messages))
        # 不符合八节契约时，拒绝压缩并保留旧投影。
        assert result.compacted_turn_count == 0
        assert result.summary is None
        assert result.messages == messages


# ── ContextBuilder 写 4-event bracket ───────────────────────────────

class TestBuilderWritesBracket:
    """ContextBuilder.build() 写 4-event bracket 而非单个 CONTEXT_COMPACTED。"""

    @pytest.mark.asyncio
    async def test_build_writes_four_event_bracket(self, tmp_path):
        """压缩触发后，session 里应有 COMPACTION_START → CONTEXT_COMPACTED → COMPACTION_END。"""
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
        session.append(USER_MESSAGE, {"content": "current request"})
        before_count = len(session.events)

        model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
        builder = ContextBuilder(
            model, max_context_tokens=10000, auto_compact_threshold=0.3,
        )
        await builder.build(session)

        new_events = session.events[before_count:]
        event_types = [e.type for e in new_events]

        # 4-event bracket
        assert COMPACTION_START in event_types
        assert CONTEXT_COMPACTED in event_types
        assert COMPACTION_END in event_types

        # 顺序：START → COMPACTED → END
        start_idx = event_types.index(COMPACTION_START)
        compacted_idx = event_types.index(CONTEXT_COMPACTED)
        end_idx = event_types.index(COMPACTION_END)
        assert start_idx < compacted_idx < end_idx

    @pytest.mark.asyncio
    async def test_build_bracket_source_seq_matches(self, tmp_path):
        """bracket 的 source_seq_start/end 指向被压缩的原始事件。"""
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
        session.append(USER_MESSAGE, {"content": "current request"})
        before_count = len(session.events)

        model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
        builder = ContextBuilder(
            model, max_context_tokens=10000, auto_compact_threshold=0.3,
        )
        await builder.build(session)

        new_events = session.events[before_count:]
        start_event = next(e for e in new_events if e.type == COMPACTION_START)
        compacted_event = next(e for e in new_events if e.type == CONTEXT_COMPACTED)

        # source_seq_start 和 source_seq_end 应该指向被压缩的事件
        assert start_event.data["source_seq_start"] == 1
        assert start_event.data["source_seq_end"] == 2
        assert compacted_event.data["source_seq_start"] == 1
        assert compacted_event.data["source_seq_end"] == 2

    @pytest.mark.asyncio
    async def test_build_bracket_has_bracket_id(self, tmp_path):
        """COMPACTION_END 包含 bracket_id。"""
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
        session.append(USER_MESSAGE, {"content": "current request"})
        before_count = len(session.events)

        model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
        builder = ContextBuilder(
            model, max_context_tokens=10000, auto_compact_threshold=0.3,
        )
        await builder.build(session)

        new_events = session.events[before_count:]
        end_event = next(e for e in new_events if e.type == COMPACTION_END)
        assert "bracket_id" in end_event.data
        assert end_event.data["bracket_id"]  # non-empty

    @pytest.mark.asyncio
    async def test_build_bracket_context_compacted_has_eight_section_schema(self, tmp_path):
        """CONTEXT_COMPACTED 事件包含经过校验的八节摘要。"""
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
        session.append(USER_MESSAGE, {"content": "current request"})
        before_count = len(session.events)

        model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
        builder = ContextBuilder(
            model, max_context_tokens=10000, auto_compact_threshold=0.3,
        )
        await builder.build(session)

        new_events = session.events[before_count:]
        compacted_event = next(e for e in new_events if e.type == CONTEXT_COMPACTED)
        assert compacted_event.data["schema"] == "eight_section"
        assert "summary" in compacted_event.data

    @pytest.mark.asyncio
    async def test_bracket_records_summary_model_duration_and_request_budget(
        self, tmp_path, monkeypatch,
    ):
        import agent_harness.context.compactor as compactor_module

        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
        session.append(USER_MESSAGE, {"content": "current request"})

        class ObservedSummaryModel:
            model_name = "configured-summary-model"

            async def ainvoke(self, _messages):
                return AIMessage(
                    content=MODEL_SECTIONS,
                    response_metadata={"model_name": "provider-reported-summary-model"},
                )

        ticks = iter((100.0, 100.05, 100.125))
        monkeypatch.setattr(
            compactor_module, "monotonic", lambda: next(ticks), raising=False,
        )
        builder = ContextBuilder(
            ScriptedModel([]), max_context_tokens=10000,
            auto_compact_threshold=0.3, summary_model=ObservedSummaryModel(),
        )
        await builder.build(session)

        event = next(e for e in session.events if e.type == CONTEXT_COMPACTED)
        assert event.data["summary_model_id"] == "provider-reported-summary-model"
        assert event.data["duration_ms"] == 125
        assert event.data["request_token_estimate"] > 0
        assert event.data["request_budget_tokens"] == 8500

    @pytest.mark.asyncio
    async def test_bracket_ignores_oversized_provider_summary_model_id(self, tmp_path):
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
        session.append(USER_MESSAGE, {"content": "current request"})

        class OversizedModelIdSummaryModel:
            model_name = "configured-summary-model"

            async def ainvoke(self, _messages):
                return AIMessage(
                    content=MODEL_SECTIONS,
                    response_metadata={"model_name": "provider-model" * 30},
                )

        builder = ContextBuilder(
            ScriptedModel([]), max_context_tokens=10000,
            auto_compact_threshold=0.3, summary_model=OversizedModelIdSummaryModel(),
        )
        await builder.build(session)

        event = next(e for e in session.events if e.type == CONTEXT_COMPACTED)
        assert event.data["summary_model_id"] == "configured-summary-model"
        assert len(event.data["summary_model_id"]) <= 256

    @pytest.mark.asyncio
    async def test_failed_summary_events_record_request_metadata_without_provider_echo(
        self, tmp_path, monkeypatch,
    ):
        import agent_harness.context.compactor as compactor_module

        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
        session.append(USER_MESSAGE, {"content": "current request"})

        class FailingSummaryModel:
            model_name = "configured-summary-model"

            async def ainvoke(self, _messages):
                raise ConnectionError("api_key=do-not-persist")

        ticks = iter((100.0, 100.0, 100.125, 100.2, 100.575))
        monkeypatch.setattr(
            compactor_module, "monotonic", lambda: next(ticks), raising=False,
        )
        builder = ContextBuilder(
            ScriptedModel([]), max_context_tokens=10000,
            auto_compact_threshold=0.3, summary_model=FailingSummaryModel(),
        )
        await builder.build(session)

        failures = [
            event for event in session.events
            if event.type == CONTEXT_COMPACTION_FAILED
        ]
        assert len(failures) == 2
        assert [event.data["summary_model_id"] for event in failures] == [
            "configured-summary-model", "configured-summary-model",
        ]
        assert [event.data["duration_ms"] for event in failures] == [125, 375]
        assert all(event.data["request_token_estimate"] > 0 for event in failures)
        assert all(event.data["request_budget_tokens"] == 8500 for event in failures)
        assert "api_key=do-not-persist" not in repr([event.data for event in failures])

    @pytest.mark.asyncio
    async def test_second_build_after_bracket_skips_shadowed(self, tmp_path):
        """第一次压缩写 bracket 后，第二次 build 的 derive_messages 跳过 shadowed 段。"""
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
        session.append(USER_MESSAGE, {"content": "current request"})

        model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
        builder = ContextBuilder(
            model, max_context_tokens=10000, auto_compact_threshold=0.3,
        )
        await builder.build(session)

        # 第二次 build：应该看到 bracket，跳过 shadowed 的 old 消息
        messages = await builder.build(session)
        # 不应包含 "old " * 8000 的 HumanMessage
        old_messages = [m for m in messages if isinstance(m, HumanMessage)
                        and "old" in (m.content or "")]
        assert len(old_messages) == 0, "shadowed 段未被跳过"

    @pytest.mark.asyncio
    async def test_second_compaction_merges_previous_summary_after_reload(self, tmp_path):
        session = make_session(tmp_path)
        original_user = "不得删除 old_rows；保留 R-042 与 4096 tokens。"
        session.append(USER_MESSAGE, {"content": original_user})
        session.append(MODEL_COMPLETED, {"content": "first analysis " * 1800})
        session.append(USER_MESSAGE, {"content": "first current request"})
        original_events = session.events
        model = ScriptedModel([
            AIMessage(content=MODEL_SECTIONS),
            AIMessage(content=MODEL_SECTIONS),
        ])
        builder = ContextBuilder(
            model, max_context_tokens=10000, auto_compact_threshold=0.3,
        )

        await builder.build(session)
        first_bracket = next(
            event for event in session.events if event.type == CONTEXT_COMPACTED
        )
        session.append(MODEL_COMPLETED, {"content": "follow-up analysis " * 1800})
        session.append(USER_MESSAGE, {"content": "second current request"})
        await builder.build(session)

        reloaded = Session.load(JsonlSessionStore(root=tmp_path), session.session_id)
        messages = reloaded.derive_messages()
        summaries = [
            message for message in messages
            if isinstance(message, HumanMessage)
            and message.content.startswith("## 原始目标与用户约束\n")
        ]
        latest = next(
            event for event in reversed(reloaded.events)
            if event.type == CONTEXT_COMPACTED
        )
        sections = dict(zip(
            _SUMMARY_HEADINGS,
            _parse_summary_sections(latest.data["summary"], _SUMMARY_HEADINGS),
        ))

        assert len(summaries) == 1
        assert messages[-1].content == "second current request"
        # #556 裁决 C：目标行 = 当前生效目标（builder 从全量 events 重建的
        # facts 通道），叙述性用户轮次不再跨压缩逐字合并——跨压缩的**继承**
        # 语义由标识节承担（下方 R-042 / 4096 断言：第一次压缩的提取结果
        # 经旧摘要继承进第二次压缩的节，且受确定性上限收敛）。
        # #710 方向 C：段内最新活跃用户消息进「当前生效指令」承载位（确定性
        # 重算，不链式继承旧摘要文本），来源 seq 指针可回读。
        section0_lines = sections["## 原始目标与用户约束"].splitlines()
        assert section0_lines[0] == json.dumps(
            original_user, ensure_ascii=False,
        )
        assert '当前生效指令："first current request"' in sections[
            "## 原始目标与用户约束"
        ]
        exact_identifiers = json.loads(sections["## 精确标识清单"])
        assert "R-042" in exact_identifiers
        assert "4096" in exact_identifiers
        assert latest.data["source_seq_start"] <= first_bracket.data["source_seq_start"]
        assert latest.data["source_seq_end"] >= first_bracket.data["source_seq_end"]
        assert reloaded.events[:len(original_events)] == original_events


# ── 参数融合 ────────────────────────────────────────────────────────

class TestFusedCompactionParameters:
    """Spec 06 参数融合：70% trigger, 85% hard guard, 20k keep recent, reserve max(15%, 16k)。"""

    @pytest.mark.asyncio
    async def test_default_parameters_match_spec(self):
        """ContextCompactor 默认参数匹配 Spec 06 冻结值（0.70/0.85，#379）。"""
        compactor = ContextCompactor(ScriptedModel([]), max_context_tokens=200_000)
        assert compactor.auto_compact_threshold == 0.70
        assert compactor.hard_guard_threshold == 0.85
        assert compactor.keep_recent_tokens == 20_000
        # reserve = max(15% of 200k, 16384) = max(30000, 16384) = 30000
        assert compactor.reserve == max(int(200_000 * 0.15), 16384)
        # #379 AC：缺省阈值落到 token 限额（同表达式浮点，位级相等）
        assert compactor._auto_limit == 200_000 * 0.70
        assert compactor._hard_limit == 200_000 * 0.85

    @pytest.mark.asyncio
    async def test_builder_default_parameters_match_spec(self):
        """ContextBuilder 默认参数匹配 Spec 06 冻结值（0.70/0.85，#379）。"""
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=200_000)
        assert builder.auto_compact_threshold == 0.70
        assert builder.hard_guard_threshold == 0.85


# ── W-03 #347：裁剪后仍超限 → ranges 覆盖 → bracket 正确 ────────────

class TestCompactionWithPrunedToolResults:
    """W-03 (#347) 地雷 3 回归：builder 传裁剪后消息时，投影内容与 derive 产物
    不再逐条相等——compactor 必须走 source_ranges 覆盖入参拿 source_seq 区间，
    否则压缩被静默拒绝（不写 bracket）、上下文只靠硬护栏停摆。"""

    @pytest.mark.asyncio
    async def test_bracket_written_for_pruned_projection(self, tmp_path):
        """裁剪后消息触发压缩：bracket 落盘、source 区间覆盖被压缩的原始事件。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = "历史数据行，包含编号 R-042 与路径 docs/a.md。\n" * 500

        async def overflowed_read(call_id: str) -> None:
            session.append(TOOL_CALL, {"tool_call_id": call_id,
                                       "tool_name": "read_file",
                                       "args": {"path": "big.txt"}})
            handler = ArtifactOverflowHandler(store, 2000, read_tool_name="read_artifact")
            result = ToolResult.success("file content", data={"output": content})
            overflowed, deferred = await handler.maybe_overflow(
                session, call_id, "read_file", result,
            )
            for event_type, data in deferred:
                session.append(event_type, data)
            session.append(TOOL_RESULT, {"tool_call_id": call_id,
                                         "content": overflowed.model_dump_json()})

        session.append(USER_MESSAGE, {"content": "读取历史。"})
        for i in range(3):
            session.append(MODEL_COMPLETED, {"content": "", "tool_calls": [
                {"id": f"c{i}", "name": "read_file", "args": {"path": "big.txt"}},
            ]})
            await overflowed_read(f"c{i}")
        session.append(MODEL_COMPLETED, {"content": "background " * 1_000})
        session.append(USER_MESSAGE, {"content": "当前请求"})

        # 加入足够的普通历史，稳定触发压缩且仍给摘要留出空间。
        builder = ContextBuilder(
            ScriptedModel([AIMessage(content=MODEL_SECTIONS)]),
            max_context_tokens=6000, auto_compact_threshold=0.4,
            artifact_store=store, artifact_read_tool_name="read_artifact",
            keep_recent_tool_results=0, clear_at_least_tokens=0,
        )
        messages = await builder.build(session)

        # 压缩确已触发（裁剪后估算仍超过 auto 阈值）并写出了 4-event bracket
        starts = [e for e in session.events if e.type == COMPACTION_START]
        assert len(starts) == 1
        assert [e.type for e in session.events[-3:]] == [
            COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END,
        ]
        # source 区间覆盖原始事件 seq 1..14：user + 3×(model/call/artifact/result) + model。
        assert starts[0].data["source_seq_start"] == 1
        assert starts[0].data["source_seq_end"] == 14
        # 压缩后的投影：摘要 + 当前请求（静态事实策略在摘要前）
        summary = next(
            message for message in messages
            if message.name == "context_compaction_summary"
        )
        assert isinstance(summary, HumanMessage)
        assert messages[-1].content == "当前请求"
