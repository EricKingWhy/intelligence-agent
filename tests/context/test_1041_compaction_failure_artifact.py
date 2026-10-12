"""#1041（spec 06 §8 Failure Semantics）：摘要失败保存 raw artifact（阶段 B TDD）。

规格：`06_CONTEXT_ARTIFACT_MEMORY.md` §8 ——「Summary 失败：保存 raw artifact，
走 fallback summary。」用户 **2026-10-12 17:48 裁决 §8 读法 = R1**：截断降级成功
分支**同样**保存 early 原文 raw artifact 并在事件/诊断回填 `artifact_ref`（AC②）。

设计（**builder-owned**，见 checkpoint §6）：
- compactor 保持**纯变换**（不注入 store / session_id）：双失败分支把 **early 原文
  文本**（`json.dumps([m.model_dump(mode="json") for m in early])`，与既有摘要转录
  同形）带出——抛错分支经 `ContextWindowExceededError.raw_artifact_content`，
  截断降级分支经 `CompactionResult.raw_artifact_content`。
- **builder** 用既有 `self._artifact_store` 落盘（store 为 None → 不落、ref 保持
  None，**绝不伪造 ref**），把 `artifact_id` 回填进本轮失败记录
  （`CompactionFailure.artifact_ref`）→ `context/compaction_failed` 事件 data
  新增 `artifact_ref` 键（additive；生成器只镜像事件类型名，不需重生成）。
- raw artifact 内容 = **原始 early 窗口** `messages[prefix_end:cut]`（未被缩小段/
  截断影响的完整 early 原文）。

分支口径（AC②-边 safe continue）：
- 双失败 + `token_estimate <= hard_limit` ⇒ 旧投影原样保留（`fallback_used=False`），
  **early 原文仍在模型视野内、也在 append-only 事件流里**，会话未因失败受损 ⇒
  按 §8/R1 的理由**不落盘**、`artifact_ref` 保持 None。
  判据 =「early 原文是否离开模型视野或会话是否因失败受损」——此出口两者皆否。
"""

from __future__ import annotations

import json

import pytest

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import (
    ContextWindowExceededError,
    compactable_early_window,
)
from agent_harness.session import (
    CONTEXT_COMPACTION_FAILED,
    MODEL_COMPLETED,
    USER_MESSAGE,
)
from agent_harness.session.derive import derive_messages
from agent_harness.storage.artifact import FakeArtifactStore, compute_artifact_id
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

#: 摘要模型恒失败（剧本耗尽 ⇒ 每次 ainvoke 抛 RuntimeError ⇒ 记为 transport_error）。
#: 两次 attempt 都失败 ⇒ 双失败分支。
_FAILING_MODEL = ScriptedModel([])

#: artifact 读回工具名（ctor 契约：artifact_store 必须配 artifact_read_tool_name）。
_READ_TOOL = "read_artifact"


def _failures(session):
    return [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]


def _expected_early_raw(session) -> str:
    """原始 early 窗口的转录文本（与 compactor 带出格式同形）。"""
    messages = derive_messages(session.events)
    prefix_end, cut = compactable_early_window(messages)
    return json.dumps(
        [m.model_dump(mode="json") for m in messages[prefix_end:cut]],
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
        return ContextBuilder(ScriptedModel([]), **common)
    return ContextBuilder(
        ScriptedModel([]), artifact_store=store,
        artifact_read_tool_name=_READ_TOOL, **common,
    )


def _untruncatable_session(root):
    """双失败 + 不可截断：recent 自身就超预算（prefix+recent ≥ budget）⇒ 抛错。

    构造：短 early（两条）+ 巨型 recent（当前请求，截断无法压回）。
    """
    session = make_session(root)
    session.append(USER_MESSAGE, {"content": "旧任务。" + "a" * 300})
    session.append(MODEL_COMPLETED, {"content": "分析。" + "b" * 300})
    session.append(USER_MESSAGE, {"content": "current" + "c" * 20_000})
    return session


def _truncatable_session(root):
    """双失败 + 可截断：三块 early + 极小 recent ⇒ 走确定性截断降级（R1 落盘）。"""
    session = make_session(root)
    session.append(USER_MESSAGE, {"content": "旧任务。" + "a" * 3000})
    session.append(MODEL_COMPLETED, {"content": "分析二。" + "b" * 3000})
    session.append(MODEL_COMPLETED, {"content": "分析三。" + "c" * 3000})
    session.append(USER_MESSAGE, {"content": "current"})
    return session


def _safe_continue_session(root):
    """双失败 + 有效用量 ≤ hard：安全继续（旧投影原样保留，不落盘）。"""
    session = make_session(root)
    session.append(USER_MESSAGE, {"content": "goal"})
    session.append(MODEL_COMPLETED, {
        "content": " ".join(f"w{i}" for i in range(1600))})
    session.append(USER_MESSAGE, {"content": "current"})
    return session


# 定标窗口（读码实证；见 checkpoint §12）：命中目标分支的最小上下文。
_UNTRUNC_WINDOW = 6000     # hard=5100 < 有效用量，且 prefix+recent 超预算 ⇒ 抛错
_TRUNC_WINDOW = 2400       # 双失败后 token_estimate > hard 且可截断 ⇒ 降级成功
_SAFE_WINDOW = 6000        # hard=5100 > 有效用量 ⇒ 安全继续


# ── AC①：双失败 + 不可截断 ⇒ 落盘 early 原文 + 事件回填正确 ref ──────────────


@pytest.mark.asyncio
async def test_double_failure_untruncatable_saves_raw_artifact_and_ref_in_event(tmp_path):
    """AC①：摘要双失败且不可截断 ⇒ raw artifact 落盘、ref 出现在失败事件。"""
    session = _untruncatable_session(tmp_path)
    store = FakeArtifactStore()
    builder = _builder(store, max_context_tokens=_UNTRUNC_WINDOW)

    with pytest.raises(ContextWindowExceededError):
        await builder.compact_now(session)

    expected_raw = _expected_early_raw(session)
    expected_ref = compute_artifact_id(expected_raw)

    failures = _failures(session)
    assert failures, "双失败必须落 context/compaction_failed"
    refs = {event.data.get("artifact_ref") for event in failures}
    assert None not in refs, f"失败事件必须回填 artifact_ref；got {refs}"
    assert refs == {expected_ref}, f"ref 必须 = sha256(early 原文)[:16]；got {refs}"

    # artifact 落盘且取回内容 == early 原文（逐字节）。
    loaded = await store.load(expected_ref)
    assert loaded.content == expected_raw, "落盘内容必须是 early 原文（未截断）"


# ── AC①-边：store 缺席 ⇒ ref=None、事件仍发射、不伪造 ────────────────────────


@pytest.mark.asyncio
async def test_artifact_store_absent_keeps_ref_none_no_fabrication(tmp_path):
    """AC①-边：store=None ⇒ ref 保持 None（§8「上传失败不能伪造 artifact_ref」）。"""
    session = _untruncatable_session(tmp_path)
    builder = _builder(None, max_context_tokens=_UNTRUNC_WINDOW)

    with pytest.raises(ContextWindowExceededError):
        await builder.compact_now(session)

    failures = _failures(session)
    assert failures, "事件仍须发射（失败记录不因缺 store 而消失）"
    for event in failures:
        assert "artifact_ref" in event.data, "artifact_ref 键必须存在（additive）"
        assert event.data["artifact_ref"] is None, "无 store 时绝不伪造 ref"


# ── AC②：截断降级成功分支同样落盘（R1 裁决） ────────────────────────────────


@pytest.mark.asyncio
async def test_truncation_fallback_branch_saves_raw_artifact_r1(tmp_path):
    """AC②（R1）：双失败 + 可截断 ⇒ 确定性截断降级**且**落盘 early 原文 + ref 可见。"""
    session = _truncatable_session(tmp_path)
    store = FakeArtifactStore()
    builder = _builder(store, max_context_tokens=_TRUNC_WINDOW)

    result = await builder.compact_now(session)
    assert result is not None and result.fallback_used is True, "必须走截断降级成功出口"
    assert result.compacted_turn_count == 0, "截断不产生摘要（无 bracket 语义）"

    expected_raw = _expected_early_raw(session)
    expected_ref = compute_artifact_id(expected_raw)
    assert result.raw_artifact_content == expected_raw, (
        "截断分支必须把 early 原文带出（供 builder 落盘）")

    failures = _failures(session)
    assert failures, "失败记录必须落盘"
    refs = {event.data.get("artifact_ref") for event in failures}
    assert None not in refs, f"截断降级的事件也必须回填 ref；got {refs}"
    assert refs == {expected_ref}, f"ref 必须 = sha256(early 原文)[:16]；got {refs}"

    loaded = await store.load(expected_ref)
    assert loaded.content == expected_raw, "R1：截断降级同样保存 early 原文"


# ── AC②-边：安全继续出口不落盘（判定见文件头 docstring） ─────────────────────


@pytest.mark.asyncio
async def test_safe_continue_branch_policy(tmp_path):
    """AC②-边：双失败 + 有效用量 ≤ hard ⇒ 旧投影保留，**不落盘**、ref 保持 None。

    判定理由（§8/R1）：此出口 early 原文仍在模型视野内、会话未受损 ⇒ 无需独立快照。
    """
    session = _safe_continue_session(tmp_path)
    store = FakeArtifactStore()
    builder = _builder(store, max_context_tokens=_SAFE_WINDOW)

    result = await builder.compact_now(session)
    assert result is None, "安全继续出口保留旧投影（build 走未压缩路径）"

    failures = _failures(session)
    assert failures, "双失败仍落失败记录（会话未受损不等于失败消失）"
    refs = {event.data.get("artifact_ref") for event in failures}
    assert refs == {None}, f"安全继续不落盘 ⇒ ref 必须全为 None；got {refs}"
    assert not store._artifacts, "安全继续出口不得写入任何 artifact"
