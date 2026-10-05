"""#643 T15/P-1：裁剪与压缩叠加时被 shadow 的 survivor 导致唯一可见原文丢失。

红测构造（票面 2026-10-04 复核给出的确定性 fixture；原攻击脚本未取得，不以脚本文件名
代替运行证据）：两条同 `tool_name` / `args_key` / `artifact_ref` 的 TOOL_RESULT 构成 R1
等价类；最新成员落在一个**完整有效** compaction bracket 的 source 范围内（derive 有效投影
里被 shadow、不进 Runtime Context），旧成员在范围外且仍投影成 ToolMessage；
`keep_recent_tool_results=0`、无保护引用、FakeArtifactStore 确实能回读该 ref；比较裁剪前后
`derive_messages_with_source_ranges` 的可见结果。

修复前 `pruner._decide` 取**全量事件**等价类的最后一条为 survivor ⇒ 选中不可见的最新成员，
把当前投影里唯一可见的旧成员裁成骨架行，该类原文在 Runtime Context 里整体消失（只剩骨架 +
回读提示）。修复方向 = survivor 只从 derive 有效投影**当前可见**的成员中选；可见性一律取自
derive 自己的输出（bracket 有效性 / 嵌套 / 覆盖与 `message/superseded` 规则在 derive 只有
一处实现），pruner 不重写区间算法。

可见性口径说明：`Runtime Context`（不变量 #6）里不存在的成员不参与裁剪决策——被 shadow 的
事件不是「本轮上下文里可替代的旧结果」，对它们下裁决策既无投影效果、又会污染收益门读数。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent_harness.context.compactor import _validate_tool_blocks
from agent_harness.context.pruner import (
    SKIP_PROTECTED_REFERENCE,
    ToolResultPruner,
)
from agent_harness.session import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
    SessionEvent,
)
from agent_harness.session.derive import derive_messages_with_source_ranges
from agent_harness.session.event import MEMORY_UPDATED, MESSAGE_SUPERSEDED
from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.tooling.result import ToolResult
from tests.conftest import make_session

SESSION_ID = "s-shadow-survivor"
READ_ARGS = {"path": "big.txt"}
#: 与真实外置场景同量级的原文（含可辨认指纹），保证「原文 − 骨架」的收益量级不退化。
CONTENT = "历史数据行，包含编号 R-9001 与路径 docs/archive.md。\n" * 40
SUMMARY = "## 原始目标与用户约束\n" + json.dumps("（bracket 摘要）", ensure_ascii=False)


# ── fixture 构造 ────────────────────────────────────────────────────


async def _seed_artifact(store: FakeArtifactStore) -> str:
    """把原文写进 store（内容寻址），返回 artifact_ref。"""
    artifact = await store.save(
        SESSION_ID, CONTENT, mime_type="text/plain",
        source_tool="read_file", tool_call_id="seed",
    )
    return artifact.artifact_id


def _content_json(artifact_ref: str) -> str:
    """已外置的 TOOL_RESULT content（生产形状：ToolResult JSON + artifact_ref）。"""
    return ToolResult(
        ok=True, message="file content", data={"output": CONTENT},
        artifact_ref=artifact_ref,
    ).model_dump_json()


def _read_turn(first_seq: int, call_id: str, content_json: str) -> list[SessionEvent]:
    """一条完整读取轮：model(tool_calls) → tool/call → tool/result（seq 连续）。"""
    return [
        SessionEvent(seq=first_seq, type=MODEL_COMPLETED, session_id=SESSION_ID,
                     data={"content": "", "tool_calls": [
                         {"id": call_id, "name": "read_file", "args": READ_ARGS}]}),
        SessionEvent(seq=first_seq + 1, type=TOOL_CALL, session_id=SESSION_ID,
                     data={"tool_call_id": call_id, "tool_name": "read_file",
                           "args": READ_ARGS}),
        SessionEvent(seq=first_seq + 2, type=TOOL_RESULT, session_id=SESSION_ID,
                     data={"tool_call_id": call_id, "content": content_json}),
    ]


def _bracket(
    first_seq: int, start: int, end: int, bracket_id: str = "b1",
    *, complete: bool = True,
) -> list[SessionEvent]:
    """4-event bracket 元数据三连（`complete=False` = 缺 END 的不完整 bracket）。"""
    events = [
        SessionEvent(seq=first_seq, type=COMPACTION_START, session_id=SESSION_ID,
                     data={"bracket_id": bracket_id,
                           "source_seq_start": start, "source_seq_end": end}),
        SessionEvent(seq=first_seq + 1, type=CONTEXT_COMPACTED, session_id=SESSION_ID,
                     data={"bracket_id": bracket_id, "summary": SUMMARY,
                           "source_seq_start": start, "source_seq_end": end}),
    ]
    if complete:
        events.append(SessionEvent(seq=first_seq + 2, type=COMPACTION_END,
                                   session_id=SESSION_ID,
                                   data={"bracket_id": bracket_id}))
    return events


def _user(seq: int, content: str) -> SessionEvent:
    return SessionEvent(seq=seq, type=USER_MESSAGE, session_id=SESSION_ID,
                        data={"content": content})


def _ranges(pairs) -> list[tuple[int, int] | None]:
    return [source_range for _message, source_range in pairs]


def _visible_tool_contents(messages, source_ranges) -> dict[str, str]:
    """当前投影中可见的 tool 结果内容：ToolMessage 且带来源范围（未被 shadow）。"""
    return {
        message.tool_call_id: message.content
        for message, source_range in zip(messages, source_ranges)
        if isinstance(message, ToolMessage) and source_range is not None
    }


def _pruner(store: FakeArtifactStore, **kwargs) -> ToolResultPruner:
    """票面口径：keep_recent_tool_results=0（无最近窗口豁免）；收益门关闭 = 旧 W-03 行为。"""
    return ToolResultPruner(
        store, "read_artifact",
        keep_recent_tool_results=kwargs.pop("keep_recent_tool_results", 0),
        clear_at_least_tokens=kwargs.pop("clear_at_least_tokens", 0),
        **kwargs,
    )


def _is_skeleton(content: str) -> bool:
    try:
        return json.loads(content).get("pruned") is True
    except ValueError:
        return False


async def _append_read(
    session: Session, store: FakeArtifactStore, *, call_id: str,
) -> SessionEvent:
    """真实 Session 路径：store 落盘 → model(tool_calls) → tool/call → tool/result。"""
    artifact = await store.save(
        session.session_id, CONTENT, mime_type="text/plain",
        source_tool="read_file", tool_call_id=call_id,
    )
    session.append(MODEL_COMPLETED, {"content": "", "tool_calls": [
        {"id": call_id, "name": "read_file", "args": READ_ARGS}]})
    session.append(TOOL_CALL, {"tool_call_id": call_id, "tool_name": "read_file",
                               "args": READ_ARGS})
    return session.append(TOOL_RESULT, {
        "tool_call_id": call_id,
        "content": _content_json(artifact.artifact_id),
    })


# ── AC①：最新成员被 shadow、只有旧成员可见 ──────────────────────────


class TestShadowedSurvivor:
    @pytest.mark.asyncio
    async def test_shadowed_newest_is_not_survivor_for_visible_older_member(self):
        """AC①：唯一可见的旧成员不得因不可见的「survivor」被裁成骨架。"""
        store = FakeArtifactStore()
        content_json = _content_json(await _seed_artifact(store))
        events = [
            _user(1, "第一问"),
            *_read_turn(2, "c1", content_json),      # seq 2..4：旧成员（在投影里可见）
            *_read_turn(5, "c2", content_json),      # seq 5..7：最新成员（落进 bracket）
            *_bracket(8, 5, 7),                      # seq 8..10：完整有效 bracket
            _user(11, "当前请求"),
        ]
        pairs = derive_messages_with_source_ranges(events)
        visible_before = _visible_tool_contents([m for m, _ in pairs], _ranges(pairs))
        # fixture 成立性前置断言：最新成员确被 shadow、旧成员确可见。
        assert set(visible_before) == {"c1"}
        snapshot = [event.to_dict() for event in events]

        messages, report = await _pruner(store).prune(pairs, events)

        visible_after = _visible_tool_contents(messages, _ranges(pairs))
        assert visible_after == visible_before, "唯一可见原文被裁成骨架（T15/P-1）"
        assert visible_after["c1"] == content_json
        assert report.pruned_seqs == frozenset()
        assert [event.to_dict() for event in events] == snapshot

    @pytest.mark.asyncio
    async def test_superseded_turn_does_not_hide_the_newest_member_either(self):
        """AC①/#196：supersede 隐藏整轮时同样只按可见成员选 survivor（derive 同一套规则）。"""
        store = FakeArtifactStore()
        content_json = _content_json(await _seed_artifact(store))
        events = [
            _user(1, "第一问"),
            *_read_turn(2, "c1", content_json),      # seq 2..4：旧成员（可见）
            _user(5, "第二问"),                      # seq 5：被取代的旧问句（整轮 shadow）
            *_read_turn(6, "c2", content_json),      # seq 6..8：最新成员（在 supersede 区间内）
            _user(9, "第二问（改）"),
            SessionEvent(seq=10, type=MESSAGE_SUPERSEDED, session_id=SESSION_ID,
                         data={"superseded_seq": 5}),
            _user(11, "当前请求"),
        ]
        pairs = derive_messages_with_source_ranges(events)
        visible_before = _visible_tool_contents([m for m, _ in pairs], _ranges(pairs))
        assert set(visible_before) == {"c1"}

        messages, report = await _pruner(store).prune(pairs, events)

        assert _visible_tool_contents(messages, _ranges(pairs)) == visible_before
        assert report.pruned_seqs == frozenset()


# ── AC②：两成员都可见 ⇒ 保留最新可见成员，旧的仍按既有规则可裁 ──────


class TestVisibleMembersKeepExistingRule:
    @pytest.mark.asyncio
    async def test_both_visible_keeps_newest_and_prunes_older(self):
        """AC②：ref 可回读且两成员都可见 ⇒ 最新保留原文、旧成员照裁（既有 R1 语义不变）。"""
        store = FakeArtifactStore()
        content_json = _content_json(await _seed_artifact(store))
        events = [
            _user(1, "第一问"),
            *_read_turn(2, "c1", content_json),
            *_read_turn(5, "c2", content_json),
            _user(8, "当前请求"),
        ]
        pairs = derive_messages_with_source_ranges(events)
        assert set(_visible_tool_contents([m for m, _ in pairs], _ranges(pairs))) == {
            "c1", "c2"}

        messages, report = await _pruner(store).prune(pairs, events)

        after = _visible_tool_contents(messages, _ranges(pairs))
        assert after["c2"] == content_json, "最新可见成员必须保留原文"
        assert _is_skeleton(after["c1"]), "旧成员仍应被裁成骨架行"
        assert [record.seq for record in report.pruned] == [4]
        assert report.pruned[0].superseded_by_seq == 7


# ── AC③：bracket 有效性与 derive 一致（不完整不造成 shadow；覆盖时同一口径）──


class TestBracketValidityMatchesDerive:
    @pytest.mark.asyncio
    async def test_incomplete_bracket_does_not_shadow_the_newest_member(self):
        """AC③：缺 END 的不完整 bracket 不造成 shadow ⇒ 最新成员仍是 candidate 内的 survivor。"""
        store = FakeArtifactStore()
        content_json = _content_json(await _seed_artifact(store))
        events = [
            _user(1, "第一问"),
            *_read_turn(2, "c1", content_json),
            *_read_turn(5, "c2", content_json),
            *_bracket(8, 5, 7, complete=False),       # 写入中断形态：START+SUMMARY 无 END
            _user(11, "当前请求"),
        ]
        pairs = derive_messages_with_source_ranges(events)
        assert set(_visible_tool_contents([m for m, _ in pairs], _ranges(pairs))) == {
            "c1", "c2"}

        messages, report = await _pruner(store).prune(pairs, events)

        after = _visible_tool_contents(messages, _ranges(pairs))
        assert after["c2"] == content_json
        assert _is_skeleton(after["c1"])
        assert [record.seq for record in report.pruned] == [4]

    @pytest.mark.asyncio
    async def test_covering_bracket_leaves_no_visible_member_to_decide(self):
        """AC③：后续压缩覆盖旧 bracket（b2 取代 b1）⇒ 无可见成员，不对不可见成员下裁决策。"""
        store = FakeArtifactStore()
        content_json = _content_json(await _seed_artifact(store))
        events = [
            _user(1, "第一问"),
            *_read_turn(2, "c1", content_json),       # seq 2..4
            *_read_turn(5, "c2", content_json),       # seq 5..7
            _user(8, "第二问"),
            *_bracket(9, 1, 4, "b1"),                 # 旧 bracket：覆盖 seq 1..4
            *_bracket(12, 1, 7, "b2"),                # 覆盖并取代 b1：seq 1..7 全 shadow
            _user(15, "当前请求"),
        ]
        pairs = derive_messages_with_source_ranges(events)
        assert _visible_tool_contents([m for m, _ in pairs], _ranges(pairs)) == {}

        messages, report = await _pruner(store).prune(pairs, events)

        assert report.pruned_seqs == frozenset()
        assert [message.model_dump_json() for message in messages] == [
            message.model_dump_json() for message, _range in pairs]


# ── AC④：既有豁免不变（keep_recent 窗口 / protected reference）──────


class TestExistingExemptionsUnchanged:
    @pytest.mark.asyncio
    async def test_keep_recent_window_still_counts_all_results(self):
        """AC④：窗口按全量事件序计（含被 shadow 的结果占名额），只有可见成员进裁决。"""
        store = FakeArtifactStore()
        content_json = _content_json(await _seed_artifact(store))
        events = [
            _user(1, "第一问"),
            *_read_turn(2, "c1", content_json),       # seq 4：可见旧成员
            *_read_turn(5, "c2", content_json),       # seq 7：被 bracket shadow
            *_read_turn(8, "c3", content_json),       # seq 10：可见最新成员（窗口内）
            *_bracket(11, 5, 7),
            _user(14, "当前请求"),
        ]
        pairs = derive_messages_with_source_ranges(events)
        assert set(_visible_tool_contents([m for m, _ in pairs], _ranges(pairs))) == {
            "c1", "c3"}

        pruner = _pruner(store, keep_recent_tool_results=1)
        messages, report = await pruner.prune(pairs, events)

        after = _visible_tool_contents(messages, _ranges(pairs))
        assert after["c3"] == content_json, "窗口内的最新可见成员保留原文"
        assert _is_skeleton(after["c1"])
        assert [record.seq for record in report.pruned] == [4]

    @pytest.mark.asyncio
    async def test_protected_reference_is_still_skipped(self):
        """AC④：source_event_ids 引用的可见成员仍走 protected 豁免（W-02 接缝不变）。"""
        store = FakeArtifactStore()
        content_json = _content_json(await _seed_artifact(store))
        first_turn = _read_turn(2, "c1", content_json)
        events = [
            _user(1, "第一问"),
            *first_turn,                              # seq 2..4：可见旧成员（被引用）
            *_read_turn(5, "c2", content_json),       # seq 7：被 bracket shadow
            *_read_turn(8, "c3", content_json),       # seq 10：可见最新成员
            *_bracket(11, 5, 7),
            _user(14, "当前请求"),
            SessionEvent(seq=15, type=MEMORY_UPDATED, session_id=SESSION_ID,
                         data={"count": 1, "memory_ids": ["m-1"], "actions": {},
                               "job_id": "j-1"},
                         source_event_ids=[first_turn[2].event_id]),
        ]
        pairs = derive_messages_with_source_ranges(events)

        messages, report = await _pruner(store).prune(pairs, events)

        assert report.pruned_seqs == frozenset()
        assert [(skip.seq, skip.reason) for skip in report.skipped] == [
            (4, SKIP_PROTECTED_REFERENCE)]
        after = _visible_tool_contents(messages, _ranges(pairs))
        assert after["c1"] == content_json
        assert after["c3"] == content_json


# ── AC⑤：reload 后同一投影参数复现同一可见原文集合 + 配对完整 ────────


class TestReloadDeterminism:
    @pytest.mark.asyncio
    async def test_reload_reproduces_visible_originals_and_pairing(self, tmp_path):
        """AC⑤：持久事件不被改写；reload 后同参数重建同一可见原文集合，call/result 配对完整。"""
        session = make_session(tmp_path)
        store = FakeArtifactStore()
        session.append(USER_MESSAGE, {"content": "第一问"})
        await _append_read(session, store, call_id="c1")
        newest = await _append_read(session, store, call_id="c2")
        # bracket 覆盖最新读取轮的 model/tool_call/tool_result 三个 seq。
        start, end = newest.seq - 2, newest.seq
        session.append(COMPACTION_START, {
            "bracket_id": "b1", "source_seq_start": start, "source_seq_end": end})
        session.append(CONTEXT_COMPACTED, {
            "bracket_id": "b1", "summary": SUMMARY,
            "source_seq_start": start, "source_seq_end": end})
        session.append(COMPACTION_END, {"bracket_id": "b1"})
        session.append(USER_MESSAGE, {"content": "当前请求"})
        snapshot = [event.to_dict() for event in session.events]

        pairs = derive_messages_with_source_ranges(session.events)
        visible_before = _visible_tool_contents([m for m, _ in pairs], _ranges(pairs))
        assert set(visible_before) == {"c1"}
        messages, report = await _pruner(store).prune(pairs, session.events)

        assert _visible_tool_contents(messages, _ranges(pairs)) == visible_before
        _validate_tool_blocks(messages)  # 拆断配对会抛 ContextWindowExceededError
        call_ids = {
            call["id"] for message in messages if isinstance(message, AIMessage)
            for call in (message.tool_calls or [])
        }
        assert {
            message.tool_call_id for message in messages
            if isinstance(message, ToolMessage)
        } <= call_ids

        reloaded = Session.load(JsonlSessionStore(root=tmp_path), session.session_id)
        assert [event.to_dict() for event in reloaded.events] == snapshot
        pairs2 = derive_messages_with_source_ranges(reloaded.events)
        messages2, report2 = await _pruner(store).prune(pairs2, reloaded.events)
        assert _visible_tool_contents(messages2, _ranges(pairs2)) == visible_before
        assert report2.pruned_seqs == report.pruned_seqs == frozenset()
