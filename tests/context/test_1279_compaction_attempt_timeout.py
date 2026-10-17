"""#1279：摘要墙钟随 attempt 自适应缩放（慢模型第二次尝试获得放大预算）。

症状（#1231 场景四一手证据）：`summary_timeout_seconds` 默认 30s，`hy4-preview-f`
单轮 45–60s ⇒ attempt 1 恒 timeout；两次 attempt 共用同一公式值 ⇒ attempt 2 没有
任何放大，压缩必失败（`{"attempt": 1, "error_class": "timeout", "duration_ms": 30159}`）。

修法：`min((base + kt × slope) × multiplier^(attempt-1), max_seconds)`。
- attempt 1 与 #1026 公式逐字相同（快模型行为不变）；
- attempt 2 默认 2×，上限默认 300s（有界，失败语义保留）；
- `context/compaction_failed` 增加 `timeout_seconds` 诊断。

测试用秒级小值（0.05–0.4s）替代真实 45s，形状与 `test_1026_compaction_timeout.py`
的 `SlowSummaryModel` 一致：延迟由 fake 模型自己承担，墙钟由 compactor 控制。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from agent_harness.context.compactor import ContextCompactor
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import CONTEXT_COMPACTION_FAILED, USER_MESSAGE

MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录。

## 失败方案
(none)

## 当前进行中状态
实现清单重注入（进行中）。

## Next Step
等待当前请求继续。"""


class SequenceDelayModel:
    """按调用序号 sleep（最后一项复用）；记录请求与每次调用的墙钟耗时。"""

    def __init__(self, delays: list[float]):
        self.delays = delays
        self.requests: list[list] = []
        self.durations: list[float] = []

    async def ainvoke(self, messages):
        index = len(self.requests)
        self.requests.append(list(messages))
        delay = self.delays[min(index, len(self.delays) - 1)]
        started = time.monotonic()
        try:
            await asyncio.sleep(delay)
        finally:
            self.durations.append(time.monotonic() - started)
        return AIMessage(content=MODEL_SECTIONS)


def _early_messages() -> list:
    """最小 early 窗口（两轮）+ 当前请求（recent），与 #1026 同形。"""
    return [
        HumanMessage(content="旧任务开始。" + "a" * 400),
        AIMessage(content="分析完成。" + "b" * 400),
        HumanMessage(content="current"),
    ]


def _compactor(model, *, base: float, multiplier: float, maximum: float,
               per_kt: float = 0.0) -> ContextCompactor:
    return ContextCompactor(
        model, max_context_tokens=1_000_000,
        summary_timeout_seconds=base,
        summary_timeout_per_kilotoken_seconds=per_kt,
        summary_timeout_attempt_multiplier=multiplier,
        summary_timeout_max_seconds=maximum,
        summary_chunk_request_ratio=0.5,
        summary_chunk_max_tokens=24_000,
    )


# ── 公式：attempt 1 逐字不变，attempt 2 起放大，最终受 max 夹逼 ─────────────


def test_attempt_one_keeps_1026_formula_and_later_attempts_scale():
    compactor = ContextCompactor(
        object(), summary_timeout_seconds=30.0,
        summary_timeout_per_kilotoken_seconds=0.25)
    # #1026 锁定的锚点，attempt=1 必须逐字相同。
    assert compactor._summary_timeout_for(0, 1) == 30.0
    assert compactor._summary_timeout_for(167_000, 1) == pytest.approx(71.75)
    assert compactor._summary_timeout_for(30_000, 1) == pytest.approx(37.5)
    # 默认 multiplier=2：第二次尝试在第一次公式值上翻倍。
    assert compactor._summary_timeout_for(0, 2) == pytest.approx(60.0)
    assert compactor._summary_timeout_for(167_000, 2) == pytest.approx(143.5)


def test_attempt_timeout_is_capped_by_max_seconds():
    compactor = ContextCompactor(
        object(), summary_timeout_seconds=10.0,
        summary_timeout_per_kilotoken_seconds=0.0,
        summary_timeout_attempt_multiplier=2.0,
        summary_timeout_max_seconds=25.0)
    assert compactor._summary_timeout_for(0, 1) == pytest.approx(10.0)
    assert compactor._summary_timeout_for(0, 2) == pytest.approx(20.0)
    assert compactor._summary_timeout_for(0, 3) == pytest.approx(25.0)
    assert compactor._summary_timeout_for(0, 20) == pytest.approx(25.0)
    # attempt=1 无条件走裸公式值：公式值超过 max_seconds 也不夹逼（审查 F1——
    # "与 #1026 逐字相同"必须无条件成立，min 只作用于 attempt≥2）。
    uncapped = ContextCompactor(
        object(), summary_timeout_seconds=40.0,
        summary_timeout_per_kilotoken_seconds=0.0,
        summary_timeout_attempt_multiplier=2.0,
        summary_timeout_max_seconds=25.0)
    assert uncapped._summary_timeout_for(0, 1) == pytest.approx(40.0)
    assert uncapped._summary_timeout_for(0, 2) == pytest.approx(25.0)


# ── 行为：慢模型 attempt 1 超时、attempt 2 在放大墙钟内成功 ──────────────────


@pytest.mark.asyncio
async def test_slow_summary_succeeds_on_second_attempt_with_scaled_timeout():
    messages = _early_messages()
    model = SequenceDelayModel([0.4, 0.4])
    # attempt 1 = 0.2s（必超时）；attempt 2 = min(0.2 × 10, 2.0) = 2.0s（0.4s 可完成）。
    compactor = _compactor(model, base=0.2, multiplier=10.0, maximum=2.0)

    result = await compactor.compact(
        messages, estimate_message_tokens(messages))

    assert result.summary is not None, (
        f"慢模型第二次尝试应在放大墙钟内成功；failures={result.failures}")
    assert len(model.requests) == 2, "第一次超时后必须重试一次"
    assert [failure.attempt for failure in result.failures] == [1]
    assert result.failures[0].error_class == "timeout"
    assert result.failures[0].timeout_seconds == pytest.approx(0.2)
    assert model.durations[0] < 0.35, "attempt 1 应在 0.2s 墙钟附近被取消"
    assert model.durations[1] < 2.0, "attempt 2 的实际等待必须落在放大墙钟内"


@pytest.mark.asyncio
async def test_huge_multiplier_is_bounded_by_max_seconds():
    messages = _early_messages()
    model = SequenceDelayModel([5.0])  # 恒 5s：两次都完不成
    # attempt 1 = 0.1s；若上限失效，attempt 2 = 0.1 × 1000 = 100s。
    compactor = _compactor(model, base=0.1, multiplier=1000.0, maximum=0.3)

    started = time.monotonic()
    result = await compactor.compact(
        messages, estimate_message_tokens(messages))
    elapsed = time.monotonic() - started

    assert result.summary is None, "两次都超时 ⇒ 失败语义保留（不得无限等待）"
    assert [failure.attempt for failure in result.failures] == [1, 2]
    assert result.failures[0].timeout_seconds == pytest.approx(0.1)
    assert result.failures[1].timeout_seconds == pytest.approx(0.3)
    assert result.failures[1].duration_ms < 800, (
        f"attempt 2 必须在 0.3s 上限内失败；实得 {result.failures[1].duration_ms}ms")
    assert elapsed < 2.0, f"上限失效会等待 100s；实测 {elapsed:.2f}s"


# ── 事件：双超时仍落 compaction_failed，且带 timeout_seconds ────────────────


#: 与 test_1041 同源的“安全继续”定标窗口：有效用量 ≤ hard，压缩后旧投影保留。
_SAFE_EARLY = (
    (USER_MESSAGE, "goal"),
    (USER_MESSAGE, MODEL_SECTIONS + " ".join(f"w{i}" for i in range(1600))),
)
_SAFE_CURRENT = "current"


@pytest.mark.asyncio
async def test_double_timeout_still_emits_failure_event_with_timeout_seconds(tmp_path):
    from agent_harness.context.builder import ContextBuilder
    from tests.conftest import make_session

    session = make_session(tmp_path)
    for event_type, content in _SAFE_EARLY:
        session.append(event_type, {"content": content})
    session.append(USER_MESSAGE, {"content": _SAFE_CURRENT})

    model = SequenceDelayModel([5.0])
    builder = ContextBuilder(
        model, max_context_tokens=6000,
        summary_timeout_seconds=0.05,
        summary_timeout_per_kilotoken_seconds=0.0,
        summary_timeout_attempt_multiplier=2.0,
        summary_timeout_max_seconds=0.2,
        summary_chunk_request_ratio=0.5,
        summary_chunk_max_tokens=2000,
    )
    result = await builder.compact_now(session)
    assert result is None, "双失败但有效用量未超 hard ⇒ 安全继续（行为不变）"

    failures = [
        event for event in session.events
        if event.type == CONTEXT_COMPACTION_FAILED
    ]
    assert [event.data["attempt"] for event in failures] == [1, 2]
    assert [event.data["error_class"] for event in failures] == ["timeout", "timeout"]
    assert [event.data["timeout_seconds"] for event in failures] == pytest.approx(
        [0.05, 0.1]), "失败事件必须携带本次 attempt 实际使用的墙钟，供诊断"


# ── 配置面：非法值响亮拒绝；非默认值一路到达 compactor ─────────────────────


def test_invalid_attempt_multiplier_and_max_are_rejected():
    # multiplier 必须 ≥1：(0,1) 会把重试墙钟缩到 attempt 1 以下，与"放大"相悖。
    with pytest.raises(ValueError, match="multiplier"):
        ContextCompactor(object(), summary_timeout_attempt_multiplier=0.5)
    with pytest.raises(ValueError, match="multiplier"):
        ContextCompactor(object(), summary_timeout_attempt_multiplier=0)
    with pytest.raises(ValueError, match="multiplier"):
        ContextCompactor(object(), summary_timeout_attempt_multiplier=-1)
    with pytest.raises(ValueError, match="max_seconds"):
        ContextCompactor(object(), summary_timeout_max_seconds=0)
    with pytest.raises(ValueError, match="max_seconds"):
        ContextCompactor(object(), summary_timeout_max_seconds=-1)
    # “不许无限大”：非有限上限同样响亮拒绝（默认 300s 只是上限，不是无界）。
    with pytest.raises(ValueError, match="max_seconds"):
        ContextCompactor(object(), summary_timeout_max_seconds=float("inf"))


def test_settings_reject_invalid_attempt_timeout_fields():
    from agent_harness.config import Settings

    with pytest.raises(ValidationError):
        Settings(summary_timeout_attempt_multiplier=0.5)
    with pytest.raises(ValidationError):
        Settings(summary_timeout_attempt_multiplier=0)
    with pytest.raises(ValidationError):
        Settings(summary_timeout_attempt_multiplier=-1)
    with pytest.raises(ValidationError):
        Settings(summary_timeout_max_seconds=0)
    with pytest.raises(ValidationError):
        Settings(summary_timeout_max_seconds=-1)
    with pytest.raises(ValidationError):
        Settings(summary_timeout_max_seconds=float("inf"))


def test_builder_construction_site_passes_attempt_timeout_to_compactor(
    tmp_path, monkeypatch,
):
    import asyncio

    from agent_harness.context import builder as builder_module
    from agent_harness.context.builder import ContextBuilder
    from tests.conftest import make_session

    captured: dict = {}

    class SpyCompactor(builder_module.ContextCompactor):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(builder_module, "ContextCompactor", SpyCompactor)
    builder = ContextBuilder(
        object(), summary_timeout_attempt_multiplier=3.0,
        summary_timeout_max_seconds=120.0,
    )
    asyncio.run(builder.compact_now(make_session(tmp_path)))

    assert captured.get("summary_timeout_attempt_multiplier") == 3.0
    assert captured.get("summary_timeout_max_seconds") == 120.0


@pytest.mark.asyncio
async def test_assembly_wires_attempt_timeout_settings_to_builder(tmp_path, monkeypatch):
    from unittest.mock import patch

    from langchain_core.messages import AIMessageChunk

    from agent_harness import assembly as assembly_module
    from agent_harness.assembly import (
        assemble_wiring,
        build_runtime,
        initialize_stores,
        recovery_stores,
    )
    from agent_harness.config import Settings
    from agent_harness.context.builder import ContextBuilder
    from agent_harness.sandbox import WorkspaceRegistry
    from agent_harness.tooling.contract import PermissionPolicy

    class _Factory:
        def bind_tools(self, tools, **kwargs):
            return self

        async def astream(self, messages, **kwargs):
            yield AIMessageChunk(content="ok")

    class SpyBuilder(ContextBuilder):
        def __init__(self, *args, **kwargs):
            self.captured_kwargs = dict(kwargs)
            super().__init__(*args, **kwargs)

    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        summary_timeout_attempt_multiplier=3.0,
        summary_timeout_max_seconds=120.0,
    )
    _, wiring = await assemble_wiring(settings)
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    # assembly 在导入期绑定了 ContextBuilder 名字，必须 patch 它自己的模块属性。
    monkeypatch.setattr(assembly_module, "ContextBuilder", SpyBuilder)

    with patch("agent_harness.assembly.create_chat_model", return_value=_Factory()):
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=WorkspaceRegistry(root=tmp_path, backend="local"),
            session_id="sess-1279", workspace=tmp_path / "w",
            max_agent_turns=5,
            permission_mode=PermissionPolicy.WORKSPACE_WRITE,
        )

    assert isinstance(runtime._context_builder, SpyBuilder)
    assert runtime._context_builder.captured_kwargs[
        "summary_timeout_attempt_multiplier"] == 3.0
    assert runtime._context_builder.captured_kwargs[
        "summary_timeout_max_seconds"] == 120.0
