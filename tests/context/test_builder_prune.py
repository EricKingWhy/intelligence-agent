"""W-03 #347：ContextBuilder 裁剪接入测试。

覆盖：默认关闭（不装 store 行为不变）、build 投影裁剪、memo 失效正确性
（地雷 1：失效不彻底=高估、失效错条目=低估）、usage_snapshot 同一裁剪路径
（地雷 2：#200 双视图教训）、构造参数校验。
压缩链路（裁剪后仍超限 → ranges 覆盖 → bracket 正确）在 test_compaction_bracket.py。
"""

from __future__ import annotations

import json

import pytest

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import TOOL_CALL, TOOL_RESULT, Session
from agent_harness.session.derive import derive_messages_with_source_ranges
from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.tooling.overflow import ArtifactOverflowHandler
from agent_harness.tooling.result import ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


async def _append_read(
    session: Session, store: FakeArtifactStore, *, call_id: str, path: str,
    content: str,
):
    session.append(TOOL_CALL, {"tool_call_id": call_id, "tool_name": "read_file",
                               "args": {"path": path}})
    handler = ArtifactOverflowHandler(store, 200, read_tool_name="read_artifact")
    result = ToolResult.success("file content", data={"output": content})
    overflowed, deferred = await handler.maybe_overflow(
        session, call_id, "read_file", result,
    )
    for event_type, data in deferred:
        session.append(event_type, data)
    return session.append(TOOL_RESULT, {
        "tool_call_id": call_id, "content": overflowed.model_dump_json(),
    })


def _builder(store, **kwargs) -> ContextBuilder:
    return ContextBuilder(
        ScriptedModel([]), max_context_tokens=10_000_000,
        artifact_store=store, artifact_read_tool_name="read_artifact",
        keep_recent_tool_results=kwargs.pop("keep_recent_tool_results", 0),
        clear_at_least_tokens=kwargs.pop("clear_at_least_tokens", 0),
        **kwargs,
    )


def _expected_pruned_total(session: Session, builder: ContextBuilder) -> int:
    """用 builder 当前决策重放投影后的 token 总数（估算/看板一致性的对照值）。"""
    pairs = derive_messages_with_source_ranges(session.events)
    messages = builder._pruner.apply(
        pairs, builder._prune_decisions.get(session.session_id, {}),
    )
    return estimate_message_tokens(messages)


def json_pruned(content: str) -> bool:
    return json.loads(content).get("pruned") is True


class TestBuilderPruneIntegration:
    @pytest.mark.asyncio
    async def test_default_off_without_store(self, tmp_path):
        """不装 artifact_store（默认）→ 裁剪整体缺席，投影与 W-03 之前逐字节一致。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        await _append_read(session, store, call_id="c1", path="a.txt", content="x\n" * 150)
        await _append_read(session, store, call_id="c2", path="a.txt", content="x\n" * 150)
        builder = ContextBuilder(ScriptedModel([]), max_context_tokens=10_000_000)
        messages = await builder.build(session)
        assert builder._pruner is None
        assert builder._last_prune_report is None
        assert all("pruned" not in m.content
                   for m in messages if m.type == "tool")
        assert builder.usage_snapshot(session)["messages"] == builder._token_estimate_total

    @pytest.mark.asyncio
    async def test_build_prunes_projection(self, tmp_path):
        """build 返回裁剪后投影；报告落在观察口；估算等于投影重估。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        first = await _append_read(session, store, call_id="c1", path="a.txt",
                                   content="x\n" * 150)
        last = await _append_read(session, store, call_id="c2", path="a.txt",
                                  content="x\n" * 150)
        builder = _builder(store)
        messages = await builder.build(session)

        report = builder._last_prune_report
        assert report is not None
        assert [r.seq for r in report.pruned] == [first.seq]
        assert report.pruned[0].superseded_by_seq == last.seq
        tools = [m for m in messages if m.type == "tool"]
        assert json_pruned(tools[0].content)
        assert tools[1].content == last.data["content"]
        # memo/估算一致性：估算与"决策重放后的投影"逐 token 相等
        assert builder._token_estimate_total == _expected_pruned_total(session, builder)
        # 每次裁剪的骨架成本已进 memo（同决策再 build 命中缓存，数字不变）
        again = await builder.build(session)
        # Protected facts are injected SystemMessages but tracked in usage_snapshot's
        # separate ``other`` bucket; the cache remains the durable session projection.
        assert builder._token_estimate_total == (
            estimate_message_tokens(again) - builder._last_protected_fact_tokens
        )

    @pytest.mark.asyncio
    async def test_memo_invalidated_when_new_duplicate_arrives(self, tmp_path):
        """地雷 1：新重复到来让旧"最新"变为可裁——其 memo 条目必须失效。

        build 1 时 seq(c2) 是等价类最新（完整内容成本进 memo）；build 2 前追加了
        seq(c3)，c2 被裁。若 memo 不失效，_token_estimate_total 会残留 c2 的完整
        内容成本（高估）；若失效错条目则会低估。两者都与"决策重放后的投影"重估
        不等——用同一断言双向卡住。
        """
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = "y\n" * 150
        await _append_read(session, store, call_id="c1", path="a.txt", content=content)
        await _append_read(session, store, call_id="c2", path="a.txt", content=content)
        builder = _builder(store)
        await builder.build(session)

        third = await _append_read(session, store, call_id="c3", path="a.txt",
                                   content=content)
        messages = await builder.build(session)

        tools = [m for m in messages if m.type == "tool"]
        assert all(json_pruned(m.content) for m in tools[:2])
        assert tools[2].content == third.data["content"]
        assert builder._token_estimate_total == _expected_pruned_total(session, builder)
        assert builder.usage_snapshot(session)["messages"] == builder._token_estimate_total

    @pytest.mark.asyncio
    async def test_usage_snapshot_matches_build_projection(self, tmp_path):
        """地雷 2（#200 双视图）：看板 messages 桶走与 build 同一条裁剪路径。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        await _append_read(session, store, call_id="c1", path="a.txt", content="z\n" * 150)
        await _append_read(session, store, call_id="c2", path="a.txt", content="z\n" * 150)
        builder = _builder(store)
        messages = await builder.build(session)
        snapshot = builder.usage_snapshot(session)
        assert snapshot["messages"] == (
            estimate_message_tokens(messages) - builder._last_protected_fact_tokens
        )
        assert snapshot["messages"] < estimate_message_tokens(session.derive_messages())

    def test_constructor_requires_pairing(self):
        model = ScriptedModel([])
        with pytest.raises(ValueError):
            ContextBuilder(model, artifact_store=FakeArtifactStore())
        with pytest.raises(ValueError):
            ContextBuilder(model, artifact_read_tool_name="read_artifact")

    @pytest.mark.asyncio
    async def test_builder_gate_blocks_small_benefit_prunes(self, tmp_path):
        """T9（#414）：门在 build 链路里真实生效——同一重复 read 流，护栏关
        （夹具缺省双 0）照常裁；clear_at_least_tokens=5000 时单次小收益
        （数百 token）低于阈值 ⇒ 不再被裁（投影保持全文）。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = "result line\n" * 20
        for i in range(3):
            await _append_read(
                session, store, call_id=f"c{i}", path="a.txt", content=content)

        off_builder = _builder(store)
        messages = await off_builder.build(session)
        assert sum(1 for m in messages if m.type == "tool" and json_pruned(m.content)) == 2

        gated_builder = _builder(store, clear_at_least_tokens=5000)
        messages = await gated_builder.build(session)
        assert not any(
            m.type == "tool" and json_pruned(m.content) for m in messages
        ), "小收益低于 clear_at_least_tokens ⇒ 门拦下，投影保持全文"

    @pytest.mark.asyncio
    async def test_builder_gate_blocks_prune_keeps_memo_consistent(self, tmp_path):
        """T10（#414）：门拦下路径的记账一致性。build1 用护栏关的 builder 裁掉
        （决策落地）；追加事件后换一个带 clear_at_least_tokens 门的 builder 重
        build——门拦下时 report.pruned 为空、决策 dict 被覆写为空，投影回到全文
        成本；`_token_estimate_total` 必须仍等于「按该 builder 决策重放投影」的重估
        （`_expected_pruned_total` 对账）。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = "result line\n" * 20
        for i in range(3):
            await _append_read(
                session, store, call_id=f"c{i}", path="a.txt", content=content)

        builder = _builder(store)
        await builder.build(session)
        assert builder._prune_decisions.get(session.session_id), "build1 必须裁过"

        # 追加一条不改变等价类的新结果，再以门拦形态重 build。
        await _append_read(
            session, store, call_id="tail", path="other.txt", content=content)
        builder = _builder(store, clear_at_least_tokens=5000)
        await builder.build(session)
        assert builder._token_estimate_total == _expected_pruned_total(session, builder)
