"""#1041（spec 06 §8 Failure Semantics）：摘要失败保存 raw artifact。

规格：`06_CONTEXT_ARTIFACT_MEMORY.md` §8 ——「Summary 失败：保存 raw artifact，
走 fallback summary。」用户 **2026-10-12 17:48 裁决 §8 读法 = R1**：截断降级成功
分支**同样**保存早期原文 raw artifact 并在事件/诊断回填 `artifact_ref`（AC②）。

设计（**builder-owned**）：
- compactor 保持**纯变换**（不注入 store / session_id）：失败分支把**原始早期窗口
  `messages[prefix_end:cut]` 的完整转录文本**（`json.dumps([m.model_dump(mode=
  "json") ...], ensure_ascii=False)`，与既有摘要转录同形）带出——抛错分支经
  `ContextWindowExceededError.raw_artifact_content`，截断降级分支经
  `CompactionResult.raw_artifact_content`。
- **builder** 用既有 `self._artifact_store` 落盘（store 为 None 或落盘抛异常 ⇒
  不落盘、ref 保持 None，**绝不伪造 ref**），把 `artifact_id` 回填进本轮失败记录
  （`CompactionFailure.artifact_ref`）→ `context/compaction_failed` 事件 data
  新增 `artifact_ref` 键（additive；事件词汇生成器只镜像事件类型名，不需重生成）。
- raw artifact 内容 = **原始 early 窗口** `messages[prefix_end:cut]`——含被截断保留
  与逐出两部分（是"被逐出集"的超集），不受缩小段/截断影响，保证快照完整。

失败出口的落盘口径（判据 =「早期原文是否离开模型视野，或会话是否因失败受损」**且
存在可存的 early 原文**；early 为空的出口如 `no_compactable_early_turn` 无原文可存，
即便会话因失败受损也不落盘）：
- 双失败 + 不可截断 ⇒ `ContextWindowExceededError`（会话 run/paused）⇒ **落盘**；
- 双失败 + 可截断 ⇒ 截断降级成功（早期原文被压出模型视野）⇒ **落盘**；
- 预检拒绝 + 无法缩小 + 有效用量仍超 hard ⇒ `ContextWindowExceededError`（会话
  受损）⇒ **落盘**；
- 双失败 + 有效用量 ≤ hard ⇒ 安全继续（旧投影逐字保留、早期原文仍在模型视野内与
  append-only 事件流里、会话未受损）⇒ **不落盘**、ref 保持 None。

ref 盖章范围：预检拒绝诊断（`preflight_request_exceeds_hard_limit`，非摘要失败）
在本轮**另有摘要尝试失败**时**不**承载本轮 raw snapshot ref（ref 归摘要失败/降级
诊断）；纯预检轮（无任何摘要尝试）里失败记录全部是预检诊断——全段预检失败后缩小段
仍超限时该轮有两条（全段 + 缩小段，`narrowed` 分别 False/True），一条或两条都带 ref
（ref 由预检诊断承载）。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import ContextWindowExceededError
from agent_harness.session import (
    CONTEXT_COMPACTION_FAILED,
    MODEL_COMPLETED,
    USER_MESSAGE,
)
from agent_harness.storage.artifact import FakeArtifactStore, compute_artifact_id
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

#: 摘要模型恒失败（剧本耗尽 ⇒ 每次 ainvoke 抛 RuntimeError ⇒ 记为 transport_error）。
#: 两次 attempt 都失败 ⇒ 双失败分支。`_builder` 复用本实例。
_FAILING_MODEL = ScriptedModel([])

#: artifact 读回工具名（ctor 契约：artifact_store 必须配 artifact_read_tool_name）。
_READ_TOOL = "read_artifact"


# ── 场景内容（构造与期望值共用同一份字面量，窗口选择由测试自行判定） ───────────

#: 双失败 + 不可截断：短 early（两条）+ 巨型 recent（当前请求，截断无法压回）。
_UNTRUNC_EARLY = (
    (USER_MESSAGE, "旧任务。" + "a" * 300),
    (MODEL_COMPLETED, "分析。" + "b" * 300),
)
_UNTRUNC_CURRENT = "current" + "c" * 20_000

#: 双失败 + 可截断：三块 early + 极小 recent ⇒ 走确定性截断降级（R1 落盘）。
_TRUNC_EARLY = (
    (USER_MESSAGE, "旧任务。" + "a" * 3000),
    (MODEL_COMPLETED, "分析二。" + "b" * 3000),
    (MODEL_COMPLETED, "分析三。" + "c" * 3000),
)
_TRUNC_CURRENT = "current"

#: 双失败 + 有效用量 ≤ hard：安全继续（旧投影原样保留，不落盘）。
_SAFE_EARLY = (
    (USER_MESSAGE, "goal"),
    (MODEL_COMPLETED, " ".join(f"w{i}" for i in range(1600))),
)
_SAFE_CURRENT = "current"

#: 预检拒绝 raise：单条巨型 early（不可缩小 ⇒ narrow_cut=None）+ current。
_PREFLIGHT_EARLY = ((USER_MESSAGE, "A" * 40_000),)
_PREFLIGHT_CURRENT = "current"


def _failures(session):
    return [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]


def _session(root, early_specs, current):
    """按 (事件类型, 正文) 序列拼 early，再追加一条 current 用户消息。"""
    session = make_session(root)
    for event_type, content in early_specs:
        session.append(event_type, {"content": content})
    session.append(USER_MESSAGE, {"content": current})
    return session


def _early_messages(early_specs) -> list[HumanMessage | AIMessage]:
    """**独立**拼出期望的早期消息列表（不复用生产窗口/切分逻辑）。

    窗口选择若被生产选错（与这里的显式列表不一致），期望值与实际落盘内容即分叉，
    测试转红——这是 F6 要求的"窗口选错类 bug 可鉴出"。
    """
    return [
        HumanMessage(content=content) if event_type == USER_MESSAGE
        else AIMessage(content=content)
        for event_type, content in early_specs
    ]


def _render(messages) -> str:
    """期望转录文本：与落盘 artifact 的序列化格式一致（自带 json.dumps）。"""
    return json.dumps(
        [message.model_dump(mode="json") for message in messages],
        ensure_ascii=False,
    )


def _builder(store, *, max_context_tokens, **kw):
    common = {
        "max_context_tokens": max_context_tokens,
        "summary_timeout_seconds": 0.2,
        "summary_chunk_request_ratio": 0.5,
        "summary_chunk_max_tokens": 2000,
    }
    common.update(kw)
    if store is None:
        return ContextBuilder(_FAILING_MODEL, **common)
    return ContextBuilder(
        _FAILING_MODEL, artifact_store=store,
        artifact_read_tool_name=_READ_TOOL, **common,
    )


class _ExplodingArtifactStore(FakeArtifactStore):
    """save 恒抛异常的 store：锁死 builder 的 fail-open（F7）。"""

    async def save(self, *args, **kwargs):
        raise RuntimeError("artifact store down")


# 定标窗口（读码 + 实测）：命中目标分支的最小上下文。
_UNTRUNC_WINDOW = 6000     # hard=5100 < 有效用量，且 prefix+recent 超预算 ⇒ 抛错
_TRUNC_WINDOW = 2400       # 双失败后 token_estimate > hard 且可截断 ⇒ 降级成功
_SAFE_WINDOW = 6000        # hard=5100 > 有效用量 ⇒ 安全继续
_PREFLIGHT_WINDOW = 6000   # 单条巨型 early 不可缩小 ⇒ 预检拒绝后 raise


# ── AC①：双失败 + 不可截断 ⇒ 落盘 early 原文 + 事件回填正确 ref ──────────────


@pytest.mark.asyncio
async def test_double_failure_untruncatable_saves_raw_artifact_and_ref_in_event(tmp_path):
    """AC①：摘要双失败且不可截断 ⇒ raw artifact 落盘、ref 出现在失败事件。"""
    session = _session(tmp_path, _UNTRUNC_EARLY, _UNTRUNC_CURRENT)
    store = FakeArtifactStore()
    builder = _builder(store, max_context_tokens=_UNTRUNC_WINDOW)

    with pytest.raises(ContextWindowExceededError):
        await builder.compact_now(session)

    expected_raw = _render(_early_messages(_UNTRUNC_EARLY))
    expected_ref = compute_artifact_id(expected_raw)

    failures = _failures(session)
    assert failures, "双失败必须落 context/compaction_failed"
    refs = {event.data.get("artifact_ref") for event in failures}
    assert None not in refs, f"失败事件必须回填 artifact_ref；got {refs}"
    assert refs == {expected_ref}, f"ref 必须 = sha256(early 原文)[:16]；got {refs}"

    # artifact 落盘且取回内容 == early 原文（逐字节）。
    loaded = await store.load(expected_ref)
    assert loaded.content == expected_raw, "落盘内容必须是 early 原文（未截断）"
    # 与实现解耦的内容存在性断言（F6）：含首条 early 正文、不含 recent 正文。
    assert "旧任务。" in loaded.content
    assert _UNTRUNC_CURRENT not in loaded.content


# ── AC①-边：store 缺席 ⇒ ref=None、事件仍发射、不伪造 ────────────────────────


@pytest.mark.asyncio
async def test_artifact_store_absent_keeps_ref_none_no_fabrication(tmp_path):
    """AC①-边：store=None ⇒ ref 保持 None（§8「上传失败不能伪造 artifact_ref」）。"""
    session = _session(tmp_path, _UNTRUNC_EARLY, _UNTRUNC_CURRENT)
    builder = _builder(None, max_context_tokens=_UNTRUNC_WINDOW)

    with pytest.raises(ContextWindowExceededError):
        await builder.compact_now(session)

    failures = _failures(session)
    assert failures, "事件仍须发射（失败记录不因缺 store 而消失）"
    for event in failures:
        assert "artifact_ref" in event.data, "artifact_ref 键必须存在（additive）"
        assert event.data["artifact_ref"] is None, "无 store 时绝不伪造 ref"


# ── AC②：截断降级成功分支同样落盘（R1 裁决），预检诊断不盖 ref（F5） ──────────


@pytest.mark.asyncio
async def test_truncation_fallback_branch_saves_raw_artifact_r1(tmp_path):
    """AC②（R1）：双失败 + 可截断 ⇒ 确定性截断降级**且**落盘 early 原文 + ref 可见。

    本轮含一条预检拒绝诊断（非摘要失败）——它**不**承载本轮 raw snapshot ref；
    ref 只落在摘要尝试失败 / 截断降级诊断上（#1041 修回 F5）。
    """
    session = _session(tmp_path, _TRUNC_EARLY, _TRUNC_CURRENT)
    store = FakeArtifactStore()
    builder = _builder(store, max_context_tokens=_TRUNC_WINDOW)

    result = await builder.compact_now(session)
    assert result is not None and result.fallback_used is True, "必须走截断降级成功出口"
    assert result.compacted_turn_count == 0, "截断不产生摘要（无 bracket 语义）"

    expected_raw = _render(_early_messages(_TRUNC_EARLY))
    expected_ref = compute_artifact_id(expected_raw)
    assert result.raw_artifact_content == expected_raw, (
        "截断分支必须把 early 原文带出（供 builder 落盘）")

    failures = _failures(session)
    assert failures, "失败记录必须落盘"
    preflight = [
        event for event in failures
        if event.data.get("error_class") == "preflight_request_exceeds_hard_limit"
    ]
    assert preflight, "本场景含一条预检拒绝诊断"
    for event in preflight:
        assert event.data.get("artifact_ref") is None, (
            "预检拒绝诊断（非摘要失败）不得盖本轮 raw snapshot ref")
    summary_failures = [
        event for event in failures
        if event.data.get("error_class") != "preflight_request_exceeds_hard_limit"
    ]
    assert summary_failures, "本轮必须含摘要失败/降级诊断"
    refs = {event.data.get("artifact_ref") for event in summary_failures}
    assert None not in refs, f"摘要失败/降级诊断必须回填 ref；got {refs}"
    assert refs == {expected_ref}, f"ref 必须 = sha256(early 原文)[:16]；got {refs}"

    loaded = await store.load(expected_ref)
    assert loaded.content == expected_raw, "R1：截断降级同样保存 early 原文"
    assert "旧任务。" in loaded.content
    assert "分析三。" in loaded.content
    assert _TRUNC_CURRENT not in loaded.content


# ── AC②-边：安全继续出口不落盘（判定见文件头 docstring） ─────────────────────


@pytest.mark.asyncio
async def test_safe_continue_branch_policy(tmp_path):
    """AC②-边：双失败 + 有效用量 ≤ hard ⇒ 旧投影保留，**不落盘**、ref 保持 None。

    判定理由（§8/R1）：此出口 early 原文仍在模型视野内、会话未受损 ⇒ 无需独立快照。
    """
    session = _session(tmp_path, _SAFE_EARLY, _SAFE_CURRENT)
    store = FakeArtifactStore()
    builder = _builder(store, max_context_tokens=_SAFE_WINDOW)

    result = await builder.compact_now(session)
    assert result is None, "安全继续出口保留旧投影（build 走未压缩路径）"

    failures = _failures(session)
    assert failures, "双失败仍落失败记录（会话未受损不等于失败消失）"
    refs = {event.data.get("artifact_ref") for event in failures}
    assert refs == {None}, f"安全继续不落盘 ⇒ ref 必须全为 None；got {refs}"
    assert not store._artifacts, "安全继续出口不得写入任何 artifact"


# ── 预检拒绝 raise 出口：会话受损 ⇒ 同样落盘 + 回填 ref（F2 修回） ───────────


@pytest.mark.asyncio
async def test_preflight_reject_raise_saves_raw_artifact(tmp_path):
    """预检拒绝 + 无法缩小 + 有效用量仍超 hard ⇒ raise，且落盘 early 原文 + 回填 ref。

    该出口会话被挂起（run/paused）、早期原文离开模型视野，与 R1 判据一致（F2）。
    """
    session = _session(tmp_path, _PREFLIGHT_EARLY, _PREFLIGHT_CURRENT)
    store = FakeArtifactStore()
    builder = _builder(store, max_context_tokens=_PREFLIGHT_WINDOW)

    with pytest.raises(ContextWindowExceededError):
        await builder.compact_now(session)

    expected_raw = _render(_early_messages(_PREFLIGHT_EARLY))
    expected_ref = compute_artifact_id(expected_raw)

    failures = _failures(session)
    assert failures, "预检拒绝必须落 context/compaction_failed"
    # 纯预检轮（无摘要尝试）里失败记录全部是预检诊断（全段预检失败后缩小段仍超限时
    # 可有两条）⇒ 一条或两条都带 ref。
    refs = {event.data.get("artifact_ref") for event in failures}
    assert None not in refs, f"预检拒绝 raise 出口必须回填 ref；got {refs}"
    assert refs == {expected_ref}, f"ref 必须 = sha256(early 原文)[:16]；got {refs}"

    loaded = await store.load(expected_ref)
    assert loaded.content == expected_raw, "预检拒绝 raise 出口必须保存 early 原文"
    assert "A" * 100 in loaded.content
    assert _PREFLIGHT_CURRENT not in loaded.content


# ── save 抛异常 ⇒ fail-open（F7 修回） ───────────────────────────────────────


@pytest.mark.asyncio
async def test_save_failure_is_fail_open_ref_none_and_event_still_emitted(tmp_path):
    """artifact store 的 save 抛异常 ⇒ fail-open：ref 保持 None、失败事件仍发射、
    受控的 `ContextWindowExceededError` 仍抛出（不升级成未捕获异常）。"""
    session = _session(tmp_path, _UNTRUNC_EARLY, _UNTRUNC_CURRENT)
    builder = _builder(_ExplodingArtifactStore(), max_context_tokens=_UNTRUNC_WINDOW)

    with pytest.raises(ContextWindowExceededError):
        await builder.compact_now(session)

    failures = _failures(session)
    assert failures, "事件仍须发射（落盘失败不吞失败记录）"
    for event in failures:
        assert "artifact_ref" in event.data, "artifact_ref 键必须存在（additive）"
        assert event.data["artifact_ref"] is None, "落盘失败 ⇒ 绝不伪造 ref"
