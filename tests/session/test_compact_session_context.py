"""#635 T3：`SessionService.compact_session_context` 的行为契约。

覆盖：拒绝路径（404/422/在途 run/空 transcript 零写入）、低水位手动成功产出合法
bracket、append-only、in-flight 防重与 finally 释放、token 计数、失败上报、
`--model` 优先级与非法模型零副作用、dry_run 零 LLM 零写入。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.context.builder import ContextBuilder
from agent_harness.model.config import ConfigError
from agent_harness.session import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    CONTEXT_COMPACTION_FAILED,
    MODEL_COMPLETED,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.errors import (
    ActiveRunConflict,
    InvalidSessionId,
    SessionNotFound,
)
from agent_harness.session.runmanager import RunManager
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


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, workspace_dir=str(tmp_path))


def _seed_history(session: Session) -> None:
    session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
    session.append(USER_MESSAGE, {"content": "current request"})


class _ObservedSummaryModel:
    """带可辨认 model_name 的摘要模型替身（验证 --model 透传）。"""

    model_name = "chosen-summary"

    async def ainvoke(self, _messages):
        return AIMessage(
            content=MODEL_SECTIONS,
            response_metadata={"model_name": "chosen-summary"},
        )


def _builder_factory(
    *,
    responses,
    max_context_tokens: int = 10_000,
    auto_compact_threshold: float = 0.3,
    hard_guard_threshold: float = 0.9,
    main_model_name: str = "main-model",
    captured: dict | None = None,
):
    async def factory(_session, summary_model):
        main = ScriptedModel(list(responses))
        main.model_name = main_model_name
        if captured is not None:
            captured["summary_model"] = summary_model
        return ContextBuilder(
            main,
            max_context_tokens=max_context_tokens,
            auto_compact_threshold=auto_compact_threshold,
            hard_guard_threshold=hard_guard_threshold,
            summary_model=summary_model,
        )

    return factory


def _events(store: JsonlSessionStore, session_id: str):
    return store.read_events(session_id)


# ── 拒绝路径 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_session_is_404_and_unknown_shape_is_422(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    with pytest.raises(SessionNotFound):
        await service.compact_session_context("no-such-session", entry_point="api")
    with pytest.raises(InvalidSessionId):
        await service.compact_session_context("../escape", entry_point="api")


@pytest.mark.asyncio
async def test_busy_run_is_rejected_with_zero_event_writes(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    before = _events(store, session.session_id)

    class _BusyRunManager:
        def is_busy(self, _session_id: str) -> bool:
            return True

        def session_lock(self, _session_id: str):
            return asyncio.Lock()

    service = make_session_service(
        store=store, run_manager=_BusyRunManager(), settings=_settings(tmp_path),
    )
    with pytest.raises(ActiveRunConflict):
        await service.compact_session_context(session.session_id, entry_point="cli")
    assert _events(store, session.session_id) == before, "在途 run 拒绝必须零写入"


@pytest.mark.asyncio
async def test_empty_transcript_is_below_floor_with_zero_writes(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)  # 只有 session/started
    before = _events(store, session.session_id)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    service._compact_context_builder = _builder_factory(responses=[])

    outcome = await service.compact_session_context(session.session_id, entry_point="api")

    assert outcome.compacted_turn_count == 0
    assert outcome.bracket_id is None
    assert _events(store, session.session_id) == before, "空 transcript 必须零写入"


# ── 低水位手动成功 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_below_threshold_manual_compaction_writes_valid_bracket(
    make_session_service, tmp_path,
):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    service._compact_context_builder = _builder_factory(
        responses=[AIMessage(content=MODEL_SECTIONS)],
    )

    outcome = await service.compact_session_context(session.session_id, entry_point="cli")

    assert outcome.compacted_turn_count == 1
    assert outcome.bracket_id
    assert outcome.source_seq_start == 1
    assert outcome.source_seq_end == 2
    assert outcome.summary_model == "main-model"

    events = _events(store, session.session_id)
    types = [event.type for event in events]
    assert types[-3:] == [COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END]

    compacted = next(event for event in events if event.type == CONTEXT_COMPACTED)
    assert compacted.data["schema"] == "eight_section"
    assert compacted.data["source_seq_start"] == 1
    assert compacted.data["source_seq_end"] == 2
    assert compacted.data["bracket_id"] == outcome.bracket_id
    assert compacted.data["compacted_turn_count"] == 1
    assert compacted.data["fallback_used"] is False
    assert compacted.data["summary_model_id"] == "main-model"
    assert isinstance(compacted.data["token_estimate"], int)


@pytest.mark.asyncio
async def test_compaction_is_append_only(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    before = _events(store, session.session_id)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    service._compact_context_builder = _builder_factory(
        responses=[AIMessage(content=MODEL_SECTIONS)],
    )

    await service.compact_session_context(session.session_id, entry_point="api")

    after = _events(store, session.session_id)
    assert after[:len(before)] == before, "历史事件必须逐字不变（append-only）"


@pytest.mark.asyncio
async def test_token_counters_shrink_and_match_dto(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    service._compact_context_builder = _builder_factory(
        responses=[AIMessage(content=MODEL_SECTIONS)],
    )

    outcome = await service.compact_session_context(session.session_id, entry_point="api")

    compacted = next(
        event for event in _events(store, session.session_id)
        if event.type == CONTEXT_COMPACTED
    )
    assert outcome.tokens_before > outcome.tokens_after
    assert outcome.tokens_after == compacted.data["token_estimate"]


# ── in-flight 防重 + finally 释放 ───────────────────────────────────

@pytest.mark.asyncio
async def test_in_flight_guard_rejects_second_call(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    gate = asyncio.Event()
    entered = asyncio.Event()

    class _BlockingBuilder:
        async def compact_now(self, _session, *, write_guard=None):
            entered.set()
            await gate.wait()

    async def factory(_session, _summary_model):
        return _BlockingBuilder()

    service._compact_context_builder = factory
    first = asyncio.create_task(
        service.compact_session_context(session.session_id, entry_point="api")
    )
    await entered.wait()

    with pytest.raises(ActiveRunConflict):
        await service.compact_session_context(session.session_id, entry_point="api")

    gate.set()
    outcome = await first
    assert outcome.compacted_turn_count == 0


@pytest.mark.asyncio
async def test_in_flight_guard_released_on_exception(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )

    class _ExplodingBuilder:
        async def compact_now(self, _session, *, write_guard=None):
            raise RuntimeError("boom")

    async def exploding_factory(_session, _summary_model):
        return _ExplodingBuilder()

    service._compact_context_builder = exploding_factory
    with pytest.raises(RuntimeError):
        await service.compact_session_context(session.session_id, entry_point="api")

    # guard 已释放：换成正常 builder 再调不再被拒。
    service._compact_context_builder = _builder_factory(responses=[])
    outcome = await service.compact_session_context(session.session_id, entry_point="api")
    assert outcome.compacted_turn_count == 0


# ── 失败上报 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summary_failure_records_events_and_session_continues(
    make_session_service, tmp_path,
):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    # 剧本耗尽的 ScriptedModel 恒抛错 → 两次摘要尝试都失败（不是预检拒绝）。
    service._compact_context_builder = _builder_factory(
        responses=[], max_context_tokens=100_000,
    )

    outcome = await service.compact_session_context(session.session_id, entry_point="api")

    assert outcome.compacted_turn_count == 0
    events = _events(store, session.session_id)
    failures = [event for event in events if event.type == CONTEXT_COMPACTION_FAILED]
    assert len(failures) == 2
    assert not any(event.type == CONTEXT_COMPACTED for event in events)
    # 会话可继续：原历史事件一字未改。
    assert [event.type for event in events[:3]] == [
        "session/started", USER_MESSAGE, MODEL_COMPLETED,
    ]


# ── --model 解析 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_explicit_model_reaches_summary_compactor(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    sentinel = _ObservedSummaryModel()

    async def resolve(_model):
        return sentinel

    service._resolve_compaction_summary_model = resolve
    captured: dict = {}
    service._compact_context_builder = _builder_factory(
        responses=[], captured=captured,
    )

    outcome = await service.compact_session_context(
        session.session_id, model="explicit-model", entry_point="cli",
    )

    assert captured["summary_model"] is sentinel
    compacted = next(
        event for event in _events(store, session.session_id)
        if event.type == CONTEXT_COMPACTED
    )
    assert compacted.data["summary_model_id"] == "chosen-summary"
    assert outcome.summary_model == "chosen-summary"


@pytest.mark.asyncio
async def test_default_model_is_main_model(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    captured: dict = {}
    service._compact_context_builder = _builder_factory(
        responses=[AIMessage(content=MODEL_SECTIONS)], captured=captured,
    )

    await service.compact_session_context(session.session_id, entry_point="api")

    assert captured["summary_model"] is None, "缺省 = 主模型（None）"


@pytest.mark.asyncio
async def test_invalid_model_name_raises_config_error_with_zero_side_effects(
    make_session_service, tmp_path,
):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    before = _events(store, session.session_id)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    builder_called = False

    async def factory(_session, _summary_model):
        nonlocal builder_called
        builder_called = True
        return object()

    service._compact_context_builder = factory

    with pytest.raises(ConfigError):
        await service.compact_session_context(
            session.session_id, model="definitely-not-a-model", entry_point="api",
        )
    assert not builder_called, "非法模型必须在装配任何 builder 之前失败"
    assert _events(store, session.session_id) == before


# ── dry_run ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_previews_without_llm_or_writes(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    _seed_history(session)
    before = _events(store, session.session_id)
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )
    main = ScriptedModel([])
    main.model_name = "main-model"

    async def factory(_session, _summary_model):
        return ContextBuilder(main, max_context_tokens=10_000, auto_compact_threshold=0.3)

    service._compact_context_builder = factory

    outcome = await service.compact_session_context(
        session.session_id, entry_point="cli", dry_run=True,
    )

    assert outcome.dry_run is True
    assert outcome.compacted_turn_count == 1
    assert outcome.tokens_before == outcome.tokens_after
    assert main.snapshots == [], "dry_run 绝不允许 LLM 调用"
    assert _events(store, session.session_id) == before, "dry_run 必须零写入"


@pytest.mark.asyncio
async def test_dry_run_reports_below_floor_when_no_early_turn(make_session_service, tmp_path):
    store = JsonlSessionStore(root=tmp_path)
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "only current"})
    service = make_session_service(
        store=store, run_manager=RunManager(), settings=_settings(tmp_path),
    )

    outcome = await service.compact_session_context(
        session.session_id, entry_point="api", dry_run=True,
    )

    assert outcome.dry_run is True
    assert outcome.compacted_turn_count == 0
