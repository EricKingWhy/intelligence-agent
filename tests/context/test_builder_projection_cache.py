"""#1528：build 每轮全量重投影的复用（等价性 + 失效反控 + 少做工作的计数）。

票面口径：机制 CONFIRMED，但量级是数十毫秒、曲线有噪声——本文件不断言性能阈值，
只钉三件事：

1. **等价**：append / compaction / superseded / fork-resume / 视觉口径变化 /
   pruner 装配六种形态下，投影消息与 source ranges 都等于全量基线
   （`derive_messages_with_source_ranges(session.events, ...)` 直接调用）。
2. **失效**：缓存键少一个维度就必须实测变红（键 = 会话对象 + 事件前缀 + 视觉口径）。
3. **少做真实工作**：同夹具下 derive 调用次数下降，且缓存可丢弃、可从事件流重建。

投影是纯函数（`derive.py` docstring），会话事件 append-only 且 `SessionEvent` 冻结
——「同一事件前缀 + 同一视觉口径」两次派生必然逐字节相同，这是复用的证明前提，
不是假设（见 builder `_derive_pairs` 注释）。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER
from agent_harness.context import builder as builder_module
from agent_harness.context.builder import ContextBuilder
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.derive import derive_messages_with_source_ranges
from agent_harness.session.event import MESSAGE_SUPERSEDED
from agent_harness.storage.artifact import FakeArtifactStore
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


def _baseline(session: Session, *, supports_vision: bool = False):
    """全量基准：直接调纯函数，不经 builder 的任何缓存。"""
    return derive_messages_with_source_ranges(
        session.events, supports_vision=supports_vision,
    )


def _append_turn(session: Session, i: int, *, size: int = 400) -> int:
    """追加一步 user→model(tool_call)→tool/result→model；返回该轮 user 事件的 seq。"""
    user_seq = session.append(USER_MESSAGE, {"content": f"第 {i} 步：读取文件"}).seq
    session.append(MODEL_COMPLETED, {
        "content": "",
        "tool_calls": [{"id": f"call_{i}", "name": "read",
                        "args": {"path": f"m{i}.py"}}],
    })
    session.append(TOOL_RESULT, {"tool_call_id": f"call_{i}", "content": "x" * size})
    session.append(MODEL_COMPLETED, {"content": f"完成 {i}"})
    return user_seq


def _counting(monkeypatch) -> dict[str, int]:
    """把 builder 模块里的 derive seam 换成计数器（与 test_builder_token_cache 同法）。"""
    calls = {"n": 0}
    real_pairs = builder_module.derive_messages_with_source_ranges

    def counting_pairs(events, *, supports_vision=False):
        calls["n"] += 1
        return real_pairs(events, supports_vision=supports_vision)

    monkeypatch.setattr(
        builder_module, "derive_messages_with_source_ranges", counting_pairs,
    )
    return calls


class TestProjectionEquivalence:
    """六种形态下：消息与 source ranges 必须与全量基准逐条相等。"""

    @pytest.mark.asyncio
    async def test_append_is_visible_next_build(self, tmp_path):
        session = make_session(tmp_path)
        _append_turn(session, 0)
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)
        await builder.build(session)

        _append_turn(session, 1)
        await builder.build(session)

        assert builder._derive_pairs(session) == _baseline(session)

    @pytest.mark.asyncio
    async def test_compaction_bracket_equivalent(self, tmp_path):
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {
            "content": "历史分析" * 100,
            "usage": {"prompt_tokens": 4000, "completion_tokens": 1,
                      "total_tokens": 4001},
        })
        session.append(USER_MESSAGE, {"content": "next"})
        builder = ContextBuilder(
            ScriptedModel([AIMessage(content=MODEL_SECTIONS)]),
            max_context_tokens=10000, auto_compact_threshold=0.3,
        )

        await builder.build(session)

        assert any(e.type == "compaction/end" for e in session.events), "前置：必须已压缩"
        assert builder._derive_pairs(session) == _baseline(session)
        # 摘要消息必须是被压缩段的 HumanMessage，不是当前请求
        summary = [m for m, _ in _baseline(session)
                   if isinstance(m, HumanMessage)
                   and "已完成工作与关键决策" in str(m.content)]
        assert summary, "前置：投影里必须有压缩摘要"

    @pytest.mark.asyncio
    async def test_superseded_round_equivalent(self, tmp_path):
        session = make_session(tmp_path)
        first_seq = _append_turn(session, 0)
        _append_turn(session, 1)
        # 取代第一轮：build 前追加（与运行时编辑问句同一路径）
        session.append(MESSAGE_SUPERSEDED, {"superseded_seq": first_seq,
                                            "carrier": "edit"})
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)

        await builder.build(session)

        assert builder._derive_pairs(session) == _baseline(session)
        texts = [str(m.content) for m, _ in builder._derive_pairs(session)]
        assert not any("第 0 步" in text for text in texts), "被取代轮必须消失"
        assert any("第 1 步" in text for text in texts), "后续轮必须保留"

    @pytest.mark.asyncio
    async def test_fork_resume_from_store_equivalent(self, tmp_path):
        """fork/resume：丢弃内存 Session，从同一 JSONL 重新 load 后必须等价。"""
        session = make_session(tmp_path)
        _append_turn(session, 0)
        _append_turn(session, 1)
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)
        await builder.build(session)
        before = list(builder._derive_pairs(session))

        reloaded = Session.load(JsonlSessionStore(root=tmp_path), session.session_id)
        await builder.build(reloaded)

        assert builder._derive_pairs(reloaded) == _baseline(reloaded)
        assert [m for m, _ in builder._derive_pairs(reloaded)] == [m for m, _ in before]

    @pytest.mark.asyncio
    async def test_vision_toggle_equivalent(self, tmp_path):
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {
            "content": "看看这张图",
            "attachments": [{
                "kind": "image", "attachment_id": "sha256:" + "a" * 64,
                "media_type": "image/png", "bytes": 1024, "width": 8, "height": 6,
            }],
        })
        builder = ContextBuilder(
            ScriptedModel([]), max_context_tokens=10_000_000,
            model_supports_vision=True,
        )
        await builder.build(session)
        vision_pairs = builder._derive_pairs(session)
        assert vision_pairs == _baseline(session, supports_vision=True)

        builder.set_supports_vision(False)
        await builder.build(session)

        non_vision_pairs = builder._derive_pairs(session)
        assert non_vision_pairs == _baseline(session, supports_vision=False)
        assert non_vision_pairs != vision_pairs, "视觉口径变化必须换投影"
        assert any(
            IMAGE_OMITTED_PLACEHOLDER in str(m.content) for m, _ in non_vision_pairs
        )

    @pytest.mark.asyncio
    async def test_budget_provider_change_keeps_projection_equivalent(self, tmp_path):
        """预算 provider 变化（上下文预算来源改变）不得影响投影与 source ranges。

        投影只依赖 (事件前缀, 视觉口径)；provider 注入是 `_with_providers` 在投影
        **之后**拼接的 ephemeral 消息。因此换 provider 后 derive 结果必须仍等于
        全量基准——缓存若把 provider 状态掺进键或结果，这里会红。
        """
        class _Provider:
            name = "memory"

            async def select(self, session, budget):
                return []

        class _OtherProvider:
            name = "skills"

            async def select(self, session, budget):
                return []

        session = make_session(tmp_path)
        _append_turn(session, 0)
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000,
                                 context_providers=[_Provider()])
        await builder.build(session)

        builder.context_providers = [_OtherProvider()]
        await builder.build(session)

        assert builder._derive_pairs(session) == _baseline(session)
        assert builder._last_provider_tokens_by_name == {}

    @pytest.mark.asyncio
    async def test_usage_anchor_unchanged_by_cache(self, tmp_path):
        """usage anchor 的定位输入（messages + source ranges）不因复用而漂移。"""
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "旧记录"})
        session.append(MODEL_COMPLETED, {
            "content": "历史分析" * 50,
            "usage": {"prompt_tokens": 3000, "completion_tokens": 1,
                      "total_tokens": 3001},
        })
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)
        await builder.build(session)

        pairs = builder._derive_pairs(session)
        baseline_pairs = _baseline(session)
        assert pairs == baseline_pairs
        assert builder._usage_anchored_tokens(
            session,
            [message for message, _range in pairs],
            [source_range for _message, source_range in pairs],
        ) == builder._usage_anchored_tokens(
            session,
            [message for message, _range in baseline_pairs],
            [source_range for _message, source_range in baseline_pairs],
        )

    @pytest.mark.asyncio
    async def test_pruner_path_equivalent(self, tmp_path):
        """pruner 装配时，复用后的 pairs 经同一 apply 必须与基线重放逐条相等。"""
        from agent_harness.session import TOOL_CALL
        from agent_harness.tooling.overflow import ArtifactOverflowHandler
        from agent_harness.tooling.result import ToolResult

        session = make_session(tmp_path)
        store = FakeArtifactStore()
        for i in range(3):
            session.append(TOOL_CALL, {
                "tool_call_id": f"c{i}", "tool_name": "read_file",
                "args": {"path": "a.txt"},
            })
            handler = ArtifactOverflowHandler(store, 200, read_tool_name="read_artifact")
            overflowed, deferred = await handler.maybe_overflow(
                session, f"c{i}", "read_file",
                ToolResult.success("file content", data={"output": "x\n" * 150}),
            )
            for event_type, data in deferred:
                session.append(event_type, data)
            session.append(TOOL_RESULT, {
                "tool_call_id": f"c{i}",
                "content": overflowed.model_dump_json(),
            })
        builder = ContextBuilder(
            ScriptedModel([]), max_context_tokens=10_000_000,
            artifact_store=store, artifact_read_tool_name="read_artifact",
        )

        await builder.build(session)

        expected = builder._pruner.apply(
            _baseline(session), builder._prune_decisions.get(session.session_id, {}),
        )
        actual = builder._pruner.apply(
            builder._derive_pairs(session),
            builder._prune_decisions.get(session.session_id, {}),
        )
        assert actual == expected
        assert builder._derive_pairs(session) == _baseline(session), (
            "缓存的是**未裁剪**的 derive 产物；裁剪决策仍由 pruner 每次重放"
        )


class TestInvalidationNegativeControls:
    """失效反控：键少任一维度都必须被这些用例抓住（消融时逐条变红）。"""

    @pytest.mark.asyncio
    async def test_append_invalidates(self, tmp_path):
        session = make_session(tmp_path)
        _append_turn(session, 0)
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)
        await builder.build(session)
        first = builder._derive_pairs(session)

        _append_turn(session, 1)
        second = builder._derive_pairs(session)

        assert len(second) > len(first), "append 后必须重新派生（键含事件前缀）"

    def test_vision_dimension_in_key(self, tmp_path):
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {
            "content": "看图",
            "attachments": [{
                "kind": "image", "attachment_id": "sha256:" + "b" * 64,
                "media_type": "image/png", "bytes": 512, "width": 8, "height": 6,
            }],
        })
        builder = ContextBuilder(
            ScriptedModel([]), max_context_tokens=10_000_000,
            model_supports_vision=True,
        )
        with_vision = builder._derive_pairs(session)
        builder.set_supports_vision(False)
        without_vision = builder._derive_pairs(session)

        assert with_vision != without_vision, (
            "同一事件前缀、不同视觉口径必须换投影（键须含 supports_vision）"
        )

    def test_session_identity_in_key(self, tmp_path):
        """同 id 不同 Session 对象：缓存不得跨对象复用（与 `_token_memo_session` 同纪律）。"""
        session_a = Session.start(
            JsonlSessionStore(root=tmp_path / "a"), session_id="same",
        )
        session_b = Session.start(
            JsonlSessionStore(root=tmp_path / "b"), session_id="same",
        )
        session_a.append(USER_MESSAGE, {"content": "a 的内容"})
        session_b.append(USER_MESSAGE, {"content": "b 的内容"})
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)

        assert builder._derive_pairs(session_a) == _baseline(session_a)
        assert builder._derive_pairs(session_b) == _baseline(session_b)
        assert builder._derive_pairs(session_b)[0][0].content == "b 的内容"

    @pytest.mark.asyncio
    async def test_cache_is_droppable(self, tmp_path):
        """缓存可丢：清空后重新派生，结果逐条不变（可从事实源重建）。"""
        session = make_session(tmp_path)
        _append_turn(session, 0)
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)
        await builder.build(session)
        cached = list(builder._derive_pairs(session))

        builder._projection_cache = None
        builder._projection_cache_session = None
        builder._projection_cache_key = None
        rebuilt = builder._derive_pairs(session)

        assert rebuilt == cached
        assert rebuilt == _baseline(session)


class TestReducedWork:
    """同夹具记录投影调用次数：证明少做了真实工作（不断言性能阈值）。"""

    @pytest.mark.asyncio
    async def test_repeated_build_without_append_derives_once(self, tmp_path, monkeypatch):
        session = make_session(tmp_path)
        _append_turn(session, 0)
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)
        calls = _counting(monkeypatch)

        await builder.build(session)
        assert calls["n"] == 1, "首次 build 派生一次"

        await builder.build(session)
        assert calls["n"] == 1, "无新事件的重复 build 不得再全量派生"

    @pytest.mark.asyncio
    async def test_usage_snapshot_reuses_build_projection(self, tmp_path, monkeypatch):
        session = make_session(tmp_path)
        _append_turn(session, 0)
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)
        calls = _counting(monkeypatch)

        await builder.build(session)
        builder.usage_snapshot(session)

        assert calls["n"] == 1, (
            "看板读 usage_snapshot 与刚 build 过的投影同 mark ⇒ 不得重复派生"
        )

    @pytest.mark.asyncio
    async def test_compacting_build_does_not_derive_twice_for_same_mark(
        self, tmp_path, monkeypatch,
    ):
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
        session.append(MODEL_COMPLETED, {
            "content": "历史分析" * 100,
            "usage": {"prompt_tokens": 4000, "completion_tokens": 1,
                      "total_tokens": 4001},
        })
        session.append(USER_MESSAGE, {"content": "next"})
        builder = ContextBuilder(
            ScriptedModel([AIMessage(content=MODEL_SECTIONS)]),
            max_context_tokens=10000, auto_compact_threshold=0.3,
        )
        calls = _counting(monkeypatch)

        await builder.build(session)

        assert any(e.type == "compaction/end" for e in session.events), "前置：必须已压缩"
        # build 派生 1 次 + 落 bracket 后 _reproject 派生 1 次（mark 已变，合法重算）。
        # 修复前是同 mark 的 3 次（build + compact_now + _reproject）。
        assert calls["n"] == 2, f"压缩 build 的派生次数应为 2，实际 {calls['n']}"

    @pytest.mark.asyncio
    async def test_build_never_appends_events(self, tmp_path):
        """复用不得引入第二份 Session 真相：build 前后事件流逐条不变。"""
        session = make_session(tmp_path)
        _append_turn(session, 0)
        before = session.events
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)

        await builder.build(session)
        await builder.build(session)
        builder.usage_snapshot(session)

        assert session.events == before
