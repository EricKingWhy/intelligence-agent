"""ContextBuilder token 估算增量 memo（perf-fix 1+2，diagnosing-bugs 实证）。

剖析实证：每步对全部历史消息做两次 model_dump_json + 全文 BPE 编码占循环
开销 88%（O(N²)）。修复后每条消息终身只编码一次（事件落盘即冻结）。

契约：
- memo 总量与朴素全量估算严格相等（阈值语义不变）；
- 增量性：已估算过的消息不再进入 tiktoken（只估新投影消息）；
- 投影与事件失去一一对应（dangling 合成注入）时整体回退全量估算；
- build 内只做一次全量级估算（_with_providers 复用 memo 总量）。
"""

import pytest

from agent_harness.context import builder as builder_module
from agent_harness.context.builder import ContextBuilder
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


def _append_tool_step(session, i: int, output_kb: int) -> None:
    """追加一步 user→model(tool_call)→tool/result(big) 事件。"""
    filler = "x" * (output_kb * 1024)
    session.append(USER_MESSAGE, {"content": f"第 {i} 步：读取文件"})
    session.append(MODEL_COMPLETED, {
        "content": "",
        "tool_calls": [{"id": f"call_{i:05d}", "name": "read",
                        "args": {"path": f"src/mod_{i}.py"}}],
    })
    session.append(TOOL_RESULT, {
        "tool_call_id": f"call_{i:05d}",
        "content": f"def f_{i}():\n    {filler}",
    })


class TestTokenMemoCorrectness:
    @pytest.mark.asyncio
    async def test_memo_total_equals_naive_estimate(self, tmp_path):
        """memo 总量必须与朴素 estimate_message_tokens 严格相等。"""
        session = make_session(tmp_path)
        for i in range(8):
            _append_tool_step(session, i, output_kb=2)
        builder = ContextBuilder(ScriptedModel([]))

        messages = await builder.build(session)

        assert (
            builder._token_estimate_total + builder._last_protected_fact_tokens
            == estimate_message_tokens(messages)
        )

    @pytest.mark.asyncio
    async def test_second_build_estimates_only_new_messages(self, tmp_path, monkeypatch):
        """增量性：第二次 build 只对新投影消息调用 tiktoken，旧消息命中 memo。"""
        session = make_session(tmp_path)
        _append_tool_step(session, 0, output_kb=2)
        _append_tool_step(session, 1, output_kb=2)
        builder = ContextBuilder(ScriptedModel([]))

        calls = {"n": 0}
        real_cost = builder_module.message_cost

        def counting_cost(message) -> int:
            calls["n"] += 1
            return real_cost(message)

        monkeypatch.setattr(builder_module, "message_cost", counting_cost)

        await builder.build(session)
        first_round = calls["n"]
        assert first_round == 6  # 每个投影事件恰好编码一次

        _append_tool_step(session, 2, output_kb=2)
        await builder.build(session)
        assert calls["n"] == first_round + 3  # 只编码 3 条新消息

        # 无新事件的重复 build：零新增编码
        await builder.build(session)
        assert calls["n"] == first_round + 3

    def test_memo_does_not_cross_session_objects_with_same_id(self, tmp_path):
        """A cache entry belongs to its Session object, even when ids/sequences match."""
        session_a = Session.start(
            JsonlSessionStore(root=tmp_path / "a"), session_id="same",
        )
        session_b = Session.start(
            JsonlSessionStore(root=tmp_path / "b"), session_id="same",
        )
        session_a.append(USER_MESSAGE, {"content": "small"})
        session_a.append(MODEL_COMPLETED, {"content": "ok"})
        session_b.append(USER_MESSAGE, {"content": "big " * 4000})
        session_b.append(MODEL_COMPLETED, {"content": "ok"})
        messages_a = session_a.derive_messages()
        messages_b = session_b.derive_messages()
        builder = ContextBuilder(ScriptedModel([]))

        assert [event.seq for event in session_a.events[1:]] == [
            event.seq for event in session_b.events[1:]
        ]
        estimate_a = builder._estimate_tokens_cached(session_a, messages_a)
        estimate_b = builder._estimate_tokens_cached(session_b, messages_b)
        estimate_a_again = builder._estimate_tokens_cached(session_a, messages_a)

        assert estimate_a == estimate_message_tokens(messages_a)
        assert estimate_b == estimate_message_tokens(messages_b)
        assert estimate_a_again == estimate_message_tokens(messages_a)


class TestTokenMemoFallback:
    @pytest.mark.asyncio
    async def test_dangling_synthesis_falls_back_to_full_estimate(self, tmp_path):
        """投影出现事件无法对应的消息（dangling 合成 ToolMessage）→
        放弃增量、整体重估，总量仍然正确。"""
        from agent_harness.session.derive import DANGLING_TOOL_CONTENT

        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取文件"})
        session.append(MODEL_COMPLETED, {
            "content": "",
            "tool_calls": [{"id": "dangling-1", "name": "read", "args": {}}],
        })
        # 故意不追加 tool/result → derive_messages 注入合成 ToolMessage
        builder = ContextBuilder(ScriptedModel([]))

        messages = await builder.build(session)

        # 静态保护策略 + Human 角色事实 + 2 个投影事件 + 合成 ToolMessage。
        assert len(messages) == 5
        assert messages[-1].content == DANGLING_TOOL_CONTENT
        # 计数失配 → 走 fallback：本会话 memo 清空，总量 = 朴素全量估算
        assert not any(key[0] == session.session_id for key in builder._token_memo)
        assert (
            builder._token_estimate_total + builder._last_protected_fact_tokens
            == estimate_message_tokens(messages)
        )

    @pytest.mark.asyncio
    async def test_memo_survives_across_builds_after_fallback(self, tmp_path, monkeypatch):
        """fallback 清掉本会话 memo → 对应恢复后的下一轮全量重建一次，
        再之后恢复增量（无新事件零编码）。"""
        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {"content": "读取文件"})
        session.append(MODEL_COMPLETED, {
            "content": "",
            "tool_calls": [{"id": "dangling-1", "name": "read", "args": {}}],
        })
        builder = ContextBuilder(ScriptedModel([]))
        await builder.build(session)  # fallback 路径

        calls = {"n": 0}
        real_cost = builder_module.message_cost

        def counting_cost(message) -> int:
            calls["n"] += 1
            return real_cost(message)

        monkeypatch.setattr(builder_module, "message_cost", counting_cost)

        session.append(TOOL_RESULT, {"tool_call_id": "dangling-1", "content": "结果"})
        await builder.build(session)
        # dangling 已由真实 tool/result 解决 → 一一对应恢复；
        # 但 memo 在 fallback 时已清空 → 本轮全量重建（3 条各一次）
        assert calls["n"] == 3
        # 再来一次无新事件的 build：零新增编码（增量已恢复）
        await builder.build(session)
        assert calls["n"] == 3


class TestTokenMemoVisionDimension:
    """#823 / MM-02 重审 P3：memo 键必须含视觉维度。

    `set_supports_vision` 可在 mid-run 变更（A2）；同一 (session_id, seq) 的带图
    事件在视觉/非视觉两种投影下内容不同（图片块列表 vs 原文+占位符串），编码成本
    也不同。若 memo 只按 (session_id, seq) 记忆，切换口径后那次估算会命中旧口径的
    memo，使 `set_supports_vision` 的"估算全局同口径"声明失效。
    """

    def test_vision_toggle_reestimates_same_event(self, tmp_path, monkeypatch):
        from agent_harness.session.derive import derive_messages

        session = make_session(tmp_path)
        session.append(USER_MESSAGE, {
            "content": "看图",
            "attachments": [{
                "kind": "image", "attachment_id": "sha256:" + "a" * 64,
                "media_type": "image/png", "bytes": 1024, "width": 8, "height": 6,
            }],
        })
        builder = ContextBuilder(ScriptedModel([]), model_supports_vision=True)
        vision_messages = derive_messages(session.events, supports_vision=True)
        non_vision_messages = derive_messages(session.events, supports_vision=False)

        calls = {"n": 0}
        real_cost = builder_module.message_cost

        def counting_cost(message) -> int:
            calls["n"] += 1
            return real_cost(message)

        monkeypatch.setattr(builder_module, "message_cost", counting_cost)

        builder._estimate_tokens_cached(session, vision_messages)
        assert calls["n"] == 1  # 单个投影事件恰好编码一次

        # 切换到非视觉口径：同一事件必须重新编码（键含视觉维度 ⇒ memo 未命中）。
        builder.set_supports_vision(False)
        builder._estimate_tokens_cached(session, non_vision_messages)
        assert calls["n"] == 2, "切换口径后同一事件必须按新口径重估（memo 未命中）"
        assert builder._token_estimate_total == estimate_message_tokens(
            non_vision_messages,
        )


class TestSingleEstimationPass:
    @pytest.mark.asyncio
    async def test_provider_budget_reuses_memo_total(self, tmp_path, monkeypatch):
        """_with_providers 不再重复全量估算：provider 预算用 memo 总量。"""
        session = make_session(tmp_path)
        _append_tool_step(session, 0, output_kb=2)
        builder = ContextBuilder(ScriptedModel([]))

        calls = {"n": 0}
        real_cost = builder_module.message_cost

        def counting_cost(message) -> int:
            calls["n"] += 1
            return real_cost(message)

        monkeypatch.setattr(builder_module, "message_cost", counting_cost)
        await builder.build(session)

        assert calls["n"] == 3  # 3 条投影消息各一次（旧实现要 6 次）
