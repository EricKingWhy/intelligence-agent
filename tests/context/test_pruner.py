"""W-03 #347：ToolResultPruner 单元测试。

覆盖票面验收：重复旧输出、不同版本、失败 ToolResult、call/result pair、
ArtifactStore 写成功读失败、超大 Unicode；外加 W-02 接缝豁免、重启决策
重建一致性、真实大输出测量（数字记录在测量用例 docstring）。

约束断言贯穿全部用例：session.events 逐字节不变（只改投影）、零模型调用、
零新事件（pruner 纯函数，不碰 session 与 store）。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import _validate_tool_blocks
from agent_harness.context.pruner import (
    SKIP_PROTECTED_REFERENCE,
    SKIP_UNREADABLE_REF,
    ToolResultPruner,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
    SessionEvent,
)
from agent_harness.session.derive import derive_messages_with_source_ranges
from agent_harness.session.event import MEMORY_UPDATED
from agent_harness.storage.artifact import FakeArtifactStore, compute_artifact_id
from agent_harness.tooling.overflow import ArtifactOverflowHandler
from agent_harness.tooling.result import ErrorCode, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

# ── 事件构造助手（镜像 runtime 落盘顺序：tool/call → artifact/externalized → tool/result） ──

#: 测试用 overflow 阈值：足够小让 100 字符级内容也外置，仍高于 marker 最小长度。
_OVERFLOW_CHARS = 200


async def _append_overflowed_read(
    session: Session, store: FakeArtifactStore, *, call_id: str, path: str,
    content: str, tool_name: str = "read_file", overflow_chars: int = _OVERFLOW_CHARS,
) -> SessionEvent:
    """按生产路径外置一份 tool result：原文进 store、session 留摘要 + artifact_ref。"""
    session.append(TOOL_CALL, {"tool_call_id": call_id, "tool_name": tool_name,
                               "args": {"path": path}})
    handler = ArtifactOverflowHandler(store, overflow_chars, read_tool_name="read_artifact")
    result = ToolResult.success("file content", data={"output": content})
    overflowed, deferred = await handler.maybe_overflow(
        session, call_id, tool_name, result,
    )
    for event_type, data in deferred:
        session.append(event_type, data)
    return session.append(TOOL_RESULT, {
        "tool_call_id": call_id, "content": overflowed.model_dump_json(),
    })


def _append_small_read(
    session: Session, *, call_id: str, path: str, output: str,
    tool_name: str = "read_file",
) -> SessionEvent:
    """不外置的小结果（无 artifact_ref，裁决 (A)：不参与裁剪）。"""
    result = ToolResult.success("ok", data={"output": output})
    session.append(TOOL_CALL, {"tool_call_id": call_id, "tool_name": tool_name,
                               "args": {"path": path}})
    return session.append(TOOL_RESULT, {
        "tool_call_id": call_id, "content": result.model_dump_json(),
    })


def _append_failed_read(
    session: Session, *, call_id: str, path: str, artifact_ref: str | None = None,
    tool_name: str = "read_file",
) -> SessionEvent:
    """失败结果（ok=false）；artifact_ref 仅在新旧组合用例中显式携带。"""
    result = ToolResult(
        ok=False, message=f"cannot read {path}",
        error_code=ErrorCode.TOOL_EXECUTION_ERROR, artifact_ref=artifact_ref,
    )
    session.append(TOOL_CALL, {"tool_call_id": call_id, "tool_name": tool_name,
                               "args": {"path": path}})
    return session.append(TOOL_RESULT, {
        "tool_call_id": call_id, "content": result.model_dump_json(),
    })


def _append_model_turn(session: Session, calls: list[tuple[str, dict]]) -> SessionEvent:
    """一条带多 tool_calls 的 model/completed。"""
    return session.append(MODEL_COMPLETED, {"content": "", "tool_calls": [
        {"id": call_id, "name": "read_file", "args": args} for call_id, args in calls
    ]})


def _tool_messages(messages) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


async def _prune(session: Session, pruner: ToolResultPruner):
    pairs = derive_messages_with_source_ranges(session.events)
    return await pruner.prune(pairs, session.events)


class _BrokenReadStore(FakeArtifactStore):
    """写成功读失败：save 走正常 dict，inspect 一律抛 KeyError。"""

    async def inspect(self, artifact_id, **kwargs):
        raise KeyError(f"Artifact '{artifact_id}' does not exist")


class _SilentModel(ScriptedModel):
    """空剧本模型：任何模型调用都让测试失败（剧本耗尽即抛错）。"""


# ── R1 同源重复 ──────────────────────────────────────────────────────


class TestR1Supersession:
    @pytest.mark.asyncio
    async def test_duplicate_reads_prune_older_keep_newest(self, tmp_path):
        """三连读同文件同版本：前两条裁骨架、最新保留；持久事件逐字节不变。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = "line-1\nline-2\nline-3\n" * 50
        events = []
        for i in range(3):
            events.append(await _append_overflowed_read(
                session, store, call_id=f"c{i}", path="a.txt", content=content))
        before = [event.to_dict() for event in session.events]
        messages, report = await _prune(session, ToolResultPruner(store, "read_artifact"))

        results = _tool_messages(messages)
        assert len(results) == 3
        skeletons = [json.loads(m.content) for m in results[:2]]
        assert all(s["pruned"] is True for s in skeletons)
        assert all(s["tool"] == "read_file" for s in skeletons)
        assert all(s["args"] == {"path": "a.txt"} for s in skeletons)
        assert all(s["ok"] is True for s in skeletons)
        ref = compute_artifact_id(content)
        assert all(s["artifact_ref"] == ref for s in skeletons)
        assert all("read_artifact" in s["note"] for s in skeletons)
        # 最新一条原样保留
        assert results[2].content == events[2].data["content"]
        assert [m.tool_call_id for m in results] == [f"c{i}" for i in range(3)]
        # 账目：逐条记录被裁 seq + superseded_by_seq + 指纹
        assert [r.seq for r in report.pruned] == [events[0].seq, events[1].seq]
        assert all(r.superseded_by_seq == events[2].seq for r in report.pruned)
        assert all(r.artifact_ref == ref for r in report.pruned)
        assert report.skipped == ()
        # 持久事件一字不动
        assert [event.to_dict() for event in session.events] == before

    @pytest.mark.asyncio
    async def test_different_versions_all_kept(self, tmp_path):
        """同 path 不同版本（不同 ref）：指纹不同，全保留。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        for i in range(3):
            await _append_overflowed_read(session, store, call_id=f"c{i}",
                                          path="a.txt", content=f"version {i}\n" * 50)
        messages, report = await _prune(session, ToolResultPruner(store, "read_artifact"))
        assert report.pruned == () and report.skipped == ()
        assert [m.content for m in _tool_messages(session.derive_messages())] == [
            m.content for m in _tool_messages(messages)
        ]

    @pytest.mark.asyncio
    async def test_failed_results_never_pruned(self, tmp_path):
        """ok=false 永不裁（含新旧组合：失败夹在两条同版本成功之间仍保留）。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = "stable content\n" * 50
        ref = compute_artifact_id(content)
        first = await _append_overflowed_read(session, store, call_id="c1",
                                              path="a.txt", content=content)
        failed = _append_failed_read(session, call_id="c2", path="a.txt",
                                     artifact_ref=ref)
        last = await _append_overflowed_read(session, store, call_id="c3",
                                             path="a.txt", content=content)
        messages, report = await _prune(session, ToolResultPruner(store, "read_artifact"))

        results = _tool_messages(messages)
        assert [r.seq for r in report.pruned] == [first.seq]
        assert report.pruned[0].superseded_by_seq == last.seq
        assert json.loads(results[0].content)["pruned"] is True
        # 失败诊断原样保留
        assert json.loads(results[1].content)["ok"] is False
        assert results[1].content == failed.data["content"]
        assert results[2].content == last.data["content"]

    @pytest.mark.asyncio
    async def test_unexternalized_results_never_pruned(self, tmp_path):
        """裁决 (A)：无 artifact_ref 的结果不参与裁剪（三连小读取全保留）。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        for i in range(3):
            _append_small_read(session, call_id=f"c{i}", path="a.txt", output="tiny")
        messages, report = await _prune(session, ToolResultPruner(store, "read_artifact"))
        assert report.pruned == ()
        assert [m.content for m in _tool_messages(session.derive_messages())] == [
            m.content for m in _tool_messages(messages)
        ]


# ── call/result 块结构 ───────────────────────────────────────────────


class TestBlockIntegrity:
    @pytest.mark.asyncio
    async def test_multi_call_block_partial_prune(self, tmp_path):
        """多 call 块：块内 result 各自可裁；tool_call_id 无一变化、消息数恒等。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = "same bytes\n" * 50
        session.append(USER_MESSAGE, {"content": "开始读取文件。"})
        # 第一轮：读 A（版本 v1）+ 读 B
        _append_model_turn(session, [("c1", {"path": "a.txt"}), ("c2", {"path": "b.txt"})])
        first = await _append_overflowed_read(session, store, call_id="c1",
                                              path="a.txt", content=content)
        await _append_overflowed_read(session, store, call_id="c2", path="b.txt",
                                      content="B file\n" * 50)
        # 第二轮：再读 A（同版本）+ 读 C
        _append_model_turn(session, [("c3", {"path": "a.txt"}), ("c4", {"path": "c.txt"})])
        third = await _append_overflowed_read(session, store, call_id="c3",
                                              path="a.txt", content=content)
        await _append_overflowed_read(session, store, call_id="c4", path="c.txt",
                                      content="C file\n" * 50)

        messages, report = await _prune(session, ToolResultPruner(store, "read_artifact"))
        assert [r.seq for r in report.pruned] == [first.seq]
        assert report.pruned[0].superseded_by_seq == third.seq

        tools = _tool_messages(messages)
        assert len(tools) == 4
        assert [m.tool_call_id for m in tools] == ["c1", "c2", "c3", "c4"]
        assert json.loads(tools[0].content)["pruned"] is True
        assert tools[1].content != "" and "pruned" not in tools[1].content
        assert tools[2].content == third.data["content"]
        assert "pruned" not in tools[3].content
        # 块结构完整：AIMessage 永不动、配对不断、消息数恒等
        _validate_tool_blocks(messages)
        assert len(messages) == len(session.derive_messages())
        assert [m for m in messages if isinstance(m, AIMessage)] == [
            m for m in session.derive_messages() if isinstance(m, AIMessage)
        ]


# ── 豁免（W-02 接缝 / 读回校验） ─────────────────────────────────────


class TestExemptions:
    @pytest.mark.asyncio
    async def test_write_success_read_failure_kept(self, tmp_path):
        """store.inspect 抛 KeyError ⇒ 保持原样 + PruneReport 记原因。"""
        session = make_session(tmp_path)
        store = _BrokenReadStore()
        content = "lost after write\n" * 50
        first = await _append_overflowed_read(session, store, call_id="c1",
                                              path="a.txt", content=content)
        await _append_overflowed_read(session, store, call_id="c2", path="a.txt",
                                      content=content)

        messages, report = await _prune(session, ToolResultPruner(store, "read_artifact"))
        assert report.pruned == ()
        assert [s.seq for s in report.skipped] == [first.seq]
        assert report.skipped[0].reason == SKIP_UNREADABLE_REF
        results = _tool_messages(messages)
        assert results[0].content == first.data["content"]

    @pytest.mark.asyncio
    async def test_protected_reference_w02_seam(self, tmp_path):
        """被 source_event_ids 引用的 result 永不裁（W-02 接缝；main 上该集合恒空）。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = "protected ground\n" * 50
        first = await _append_overflowed_read(session, store, call_id="c1",
                                              path="a.txt", content=content)
        await _append_overflowed_read(session, store, call_id="c2", path="a.txt",
                                      content=content)
        # 合成 W-02 引用事件：保护事实引用第一条 tool/result
        session.append(MEMORY_UPDATED, {"count": 1, "memory_ids": ["m-1"],
                                        "actions": {}, "job_id": "j-1"},
                       source_event_ids=[first.event_id])

        messages, report = await _prune(session, ToolResultPruner(store, "read_artifact"))
        assert report.pruned == ()
        assert [s.seq for s in report.skipped] == [first.seq]
        assert report.skipped[0].reason == SKIP_PROTECTED_REFERENCE
        assert all("pruned" not in m.content for m in _tool_messages(messages))


# ── Unicode 与可回读性 ───────────────────────────────────────────────


class TestUnicodeAndReadback:
    @pytest.mark.asyncio
    async def test_unicode_big_output_prune_and_roundtrip(self, tmp_path):
        """中文/emoji 大输出外置后重复读：裁剪生效、UTF-8 完整、指纹一致、原文可回读。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = "第12行：中文内容测试 🚀✨ 表意文字与变长序列 mixed ascii tail\n" * 800
        first = await _append_overflowed_read(session, store, call_id="c1",
                                              path="数据.txt", content=content)
        await _append_overflowed_read(session, store, call_id="c2", path="数据.txt",
                                      content=content)
        ref = compute_artifact_id(content)
        assert first.data["content"].count(ref) >= 1

        messages, report = await _prune(session, ToolResultPruner(store, "read_artifact"))
        assert len(report.pruned) == 1
        skeleton = json.loads(_tool_messages(messages)[0].content)
        # 骨架行是合法单行 JSON，ref 指回原文（content-hash 指纹）
        assert skeleton["artifact_ref"] == ref
        assert skeleton["args"] == {"path": "数据.txt"}
        artifact = await store.load(ref)
        assert artifact.content == content  # 原文逐字节找回（UTF-8 完整）
        assert compute_artifact_id(artifact.content) == ref

    @pytest.mark.asyncio
    async def test_skeleton_note_names_real_read_tool(self, tmp_path):
        """骨架 note 点名装配期真实读回工具名（#186：不得写死 read_artifact）。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        await _append_overflowed_read(session, store, call_id="c1", path="a.txt",
                                      content="x\n" * 150)
        await _append_overflowed_read(session, store, call_id="c2", path="a.txt",
                                      content="x\n" * 150)
        messages, _report = await _prune(
            session, ToolResultPruner(store, "inspect_artifact"))
        skeleton = json.loads(_tool_messages(messages)[0].content)
        assert "inspect_artifact(" in skeleton["note"]


# ── 重启可解释 / 无副作用 ────────────────────────────────────────────


class TestRestartAndPurity:
    @pytest.mark.asyncio
    async def test_restart_rebuilds_identical_decisions(self, tmp_path):
        """重启（新 pruner + 事件流重载）后决策逐条一致（PruneReport 逐条原因）。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        await _append_overflowed_read(session, store, call_id="c1", path="a.txt",
                                      content="v\n" * 150)
        await _append_overflowed_read(session, store, call_id="c2", path="a.txt",
                                      content="v\n" * 150)
        _append_small_read(session, call_id="c3", path="b.txt", output="tiny")

        pairs = derive_messages_with_source_ranges(session.events)
        _m1, report1 = await ToolResultPruner(store, "read_artifact").prune(
            pairs, session.events,
        )
        # 重启：事件从 JSONL 重载（event_id 持久化）+ 全新校验缓存
        reloaded = Session.load(JsonlSessionStore(root=tmp_path), session.session_id)
        pairs2 = derive_messages_with_source_ranges(reloaded.events)
        _m2, report2 = await ToolResultPruner(store, "read_artifact").prune(
            pairs2, reloaded.events,
        )
        assert report2 == report1
        assert [r.skeleton for r in report1.pruned] == [r.skeleton for r in report2.pruned]

    @pytest.mark.asyncio
    async def test_prune_does_not_touch_session_or_store(self, tmp_path):
        """零新事件、零模型调用、零工具执行：pruner 只读事件流与 store。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        await _append_overflowed_read(session, store, call_id="c1", path="a.txt",
                                      content="x\n" * 150)
        await _append_overflowed_read(session, store, call_id="c2", path="a.txt",
                                      content="x\n" * 150)
        before = [event.to_dict() for event in session.events]
        await _prune(session, ToolResultPruner(store, "read_artifact"))
        assert [event.to_dict() for event in session.events] == before
        assert list(store._artifacts.keys()) == [compute_artifact_id("x\n" * 150)]

    def test_pruner_requires_explicit_read_tool_name(self):
        with pytest.raises(ValueError):
            ToolResultPruner(FakeArtifactStore(), "")


# ── 真实大输出测量 ───────────────────────────────────────────────────


class TestRealLargeOutputMeasurement:
    @pytest.mark.asyncio
    async def test_400kb_chinese_file_three_rounds(self, tmp_path):
        """~400KB 中文文件 3 轮 read（触发 overflow 外置、同 hash）+ 无关小消息。

        真实大输出测量（``estimate_message_tokens``，cl100k_base；票面验收要求
        记录裁剪前后 token 估值与原件 ref）——2026-09-28 实测：
          - 原文 400,027 UTF-8 字节；原件 ref = sha256(content)[:16]
            （content-hash 指纹，3 轮外置同 hash，store 内仅 1 份）；
          - overflow（2000 阈值）已把每条 TOOL_RESULT 压成 ~2KB 摘要 + ref，
            本票裁剪作用在**重复摘要**上：
            裁剪关（off）= 5,693 tokens → 裁剪开（on）= 2,171 tokens（≈ 2.6x）；
          - 骨架行 ref 可 ``store.load`` 找回原文逐字（400KB 完整回读）。
        断言：裁剪生效、估值显著下降、骨架 ref 回读一致、零模型调用零新事件。
        """
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        content = ("这是一个用于验证超大中文输出的测试段落，包含标点与数字 12345，"
                   "以及少量英文 token。") * 8
        while len(content.encode("utf-8")) < 400_000:
            content += "续行：中文上下文压测，保证重复读取外置后同 hash。"
        assert len(content.encode("utf-8")) >= 400_000

        for i in range(3):
            await _append_overflowed_read(session, store, call_id=f"c{i}",
                                          path="big.txt", content=content,
                                          overflow_chars=2000)
        session.append(USER_MESSAGE, {"content": "顺便看一下 README。"})

        before = [event.to_dict() for event in session.events]
        builder_off = ContextBuilder(
            _SilentModel([]), max_context_tokens=10_000_000)
        messages_off = await builder_off.build(session)
        builder_on = ContextBuilder(
            _SilentModel([]), max_context_tokens=10_000_000,
            artifact_store=store, artifact_read_tool_name="read_artifact",
        )
        messages_on = await builder_on.build(session)

        # 零模型调用、零新事件
        assert builder_off.model_provider.snapshots == []
        assert builder_on.model_provider.snapshots == []
        assert [event.to_dict() for event in session.events] == before

        off_total = builder_off._token_estimate_total
        on_total = builder_on._token_estimate_total
        tools_on = _tool_messages(messages_on)
        tools_off = _tool_messages(messages_off)
        assert len(tools_on) == len(tools_off) == 3
        skeletons = [json.loads(m.content) for m in tools_on[:2]]
        assert all(s["pruned"] is True for s in skeletons)
        assert all(s["artifact_ref"] == compute_artifact_id(content) for s in skeletons)
        assert tools_on[2].content == tools_off[2].content
        # 骨架行 ref 找回原文逐字
        artifact = await store.load(skeletons[0]["artifact_ref"])
        assert artifact.content == content
        # 估值显著下降（骨架 << 完整 JSON）
        assert on_total < off_total * 0.5
        print(f"\n[measure] off={off_total} on={on_total} "
              f"bytes={len(content.encode('utf-8'))}")
