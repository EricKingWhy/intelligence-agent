"""MEM-V2-4: deletion, settings, source authority, and explicit command contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agent_harness.capability.wiring import _MemorySettingsContextProvider
from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.types import memory_session_var
from agent_harness.memory.v2._sqlite import connect
from agent_harness.memory.v2.capability import MemoryIndexDeletePending, MemoryV2Service
from agent_harness.memory.v2.commands import (
    explicit_remember_matches,
    has_forget_intent,
)
from agent_harness.memory.v2.index import InMemoryMemoryV2Index, MemoryV2IndexRelay
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.tools import (
    ForgetMemoryV2Tool,
    RememberMemoryV2Tool,
    _ForgetV2Args,
    _RememberV2Args,
)
from agent_harness.memory.v2.types import (
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    SemanticCategory,
    SemanticPayload,
    SourceType,
    TrustedMemoryIdentity,
)
from agent_harness.session import (
    RUN_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)
from tests.memory.v2._records import make_draft

IDENTITY = IdentityContext("tenant-a", "user-a", ["user"])
TRUSTED = TrustedMemoryIdentity("tenant-a", "user-a")


@pytest_asyncio.fixture
async def governance(tmp_path):
    store = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await store.initialize()
    index = InMemoryMemoryV2Index()
    relay = MemoryV2IndexRelay(store, index)
    return MemoryV2Service(store, index, relay=relay), store, index


async def _write_user_turn(sessions: JsonlSessionStore, session_id: str, text: str) -> str:
    user_event = SessionEvent(
        seq=0, type=USER_MESSAGE, session_id=session_id, data={"content": text},
    )
    start_event = SessionEvent(
        seq=1, type=RUN_STARTED, session_id=session_id, run_id="run-1", data={},
    )
    sessions.append_event(session_id, user_event)
    sessions.append_event(session_id, start_event)
    return user_event.event_id


def _semantic_payload(fact: str) -> SemanticPayload:
    return SemanticPayload(
        subject="用户偏好", fact=fact, category=SemanticCategory.PREFERENCE,
    )


@pytest.mark.asyncio
async def test_delete_erases_all_versions_and_keeps_only_content_free_tombstones(governance):
    service, store, index = governance
    first = await service.create(make_draft(content="最初的偏好"), TRUSTED)
    second = await service.update(first.id, make_draft(content="修订后的偏好"), TRUSTED)
    await MemoryV2IndexRelay(store, index).flush()

    receipt = await service.delete(second.id, TRUSTED)

    assert receipt.deleted and {item.memory_id for item in receipt.memories} == {first.id, second.id}
    for memory_id in (first.id, second.id):
        with pytest.raises(KeyError):
            await store.get(memory_id, TRUSTED)
    assert await index.search("偏好", TRUSTED, MemoryScope.USER_GLOBAL, 10) == []
    pending = await store.pending()
    assert {item.operation.value for item in pending} == {"delete"}
    assert all(item.record is None for item in pending)

    async with connect(store.database_path) as connection:
        columns = await connection.execute_fetchall("PRAGMA table_info(memory_v2_tombstones)")
        names = {row["name"] for row in columns}
        assert names == {
            "memory_id", "root_id", "tenant_id", "user_id", "scope", "project_id",
            "deleted_at", "expires_at", "deletion_reason", "content_hashes", "source_hashes",
        }
        rows = await connection.execute_fetchall("SELECT * FROM memory_v2_tombstones")
    serialized = json.dumps([dict(row) for row in rows], ensure_ascii=False)
    assert "最初的偏好" not in serialized and "修订后的偏好" not in serialized
    assert "hash-1" not in serialized and "event-1" not in serialized
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_index_delete_failure_keeps_durable_delete_and_stale_hits_unreadable(governance):
    service, store, index = governance
    record = await service.create(make_draft(content="index delete retry"), TRUSTED)
    await service._relay.flush()
    index.fail_delete = RuntimeError("injected index failure")

    with pytest.raises(MemoryIndexDeletePending) as pending:
        await service.delete(record.id, TRUSTED)
    assert pending.value.memory_ids == (record.id,)
    with pytest.raises(KeyError):
        await store.get(record.id, TRUSTED)
    assert [change.operation.value for change in await store.pending()] == ["delete"]
    assert await service.search(
        "index delete", TRUSTED, scope=MemoryScope.USER_GLOBAL, limit=10,
    ) == []

    index.fail_delete = None
    await service._relay.flush()
    assert await store.pending() == []
    assert await index.search("index delete", TRUSTED, MemoryScope.USER_GLOBAL, 10) == []


@pytest.mark.asyncio
async def test_deleted_automatic_sources_cannot_replay_but_new_explicit_command_can(governance):
    service, store, _index = governance
    deleted = await service.create(make_draft(content="用户偏好简洁直接的回答"), TRUSTED)
    await service.delete(deleted.id, TRUSTED)

    with pytest.raises(ValueError, match="matches a deleted source"):
        await service.create(make_draft(content="用户偏好简洁直接地回答"), TRUSTED)

    explicit = await service.create(make_draft(
        content=deleted.content, source_type=SourceType.EXPLICIT_COMMAND,
        source_event_ids=["new-explicit-event"],
    ), TRUSTED)
    assert explicit.source_type is SourceType.EXPLICIT_COMMAND
    assert (await store.get(explicit.id, TRUSTED)).content == deleted.content


@pytest.mark.asyncio
async def test_bulk_delete_is_atomic_and_covers_invalidated_records(governance, monkeypatch):
    service, store, _index = governance
    active = await service.create(make_draft(content="active root"), TRUSTED)
    invalidated = await service.create(make_draft(
        content="invalidated root", source_event_ids=["event-invalidated"],
    ), TRUSTED)
    await service.invalidate(invalidated.id, TRUSTED)

    original_enqueue = store._enqueue
    calls = 0

    async def fail_second_enqueue(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected transaction failure")
        await original_enqueue(*args)

    monkeypatch.setattr(store, "_enqueue", fail_second_enqueue)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        await store.bulk_delete(TRUSTED, kind=None)
    monkeypatch.setattr(store, "_enqueue", original_enqueue)

    assert (await store.get(active.id, TRUSTED)).status is MemoryStatus.ACTIVE
    assert (await store.get(invalidated.id, TRUSTED)).status is MemoryStatus.INVALIDATED
    async with connect(store.database_path) as connection:
        tombstones = await connection.execute_fetchall("SELECT memory_id FROM memory_v2_tombstones")
    assert tombstones == []

    receipts = await service.bulk_delete(TRUSTED, kind=None)
    assert len(receipts) == 2 and all(receipt.deleted for receipt in receipts)
    assert sum(len(receipt.memories) for receipt in receipts) == 2


@pytest.mark.asyncio
async def test_tombstone_replay_window_and_independent_settings_survive_restart(tmp_path):
    now = [datetime(2026, 9, 25, tzinfo=UTC)]
    store = SqliteMemoryV2Store(tmp_path / "memory-v2.db", clock=lambda: now[0])
    await store.initialize()
    record = await store.create(make_draft(content="保留三十天的删除哈希"), TRUSTED)
    await store.delete(record.id, TRUSTED)
    await store.update_settings(TRUSTED, extraction_enabled=False)
    await store.update_settings(TRUSTED, recall_enabled=False)

    reopened = SqliteMemoryV2Store(tmp_path / "memory-v2.db", clock=lambda: now[0])
    await reopened.initialize()
    settings = await reopened.get_settings(TRUSTED)
    assert settings.extraction_enabled is False and settings.recall_enabled is False
    assert await reopened.get_settings(TrustedMemoryIdentity("tenant-a", "another-user")) == (
        type(settings)()
    )
    now[0] += timedelta(days=30)
    assert await reopened.purge_expired_tombstones() == 1
    with pytest.raises(KeyError):
        await reopened.delete(record.id, TRUSTED)


@pytest.mark.asyncio
async def test_durable_recall_setting_gates_the_existing_context_provider(governance):
    service, _store, _index = governance

    class _Provider:
        def __init__(self):
            self.calls = 0

        async def select(self, session, token_budget):
            self.calls += 1
            return ["recalled context"]

    provider = _Provider()
    guarded = _MemorySettingsContextProvider(provider, service)
    identity_token = set_identity_context(IDENTITY)
    try:
        assert await guarded.select(object(), 100) == ["recalled context"]
        await service.update_settings(TRUSTED, recall_enabled=False)
        assert await guarded.select(object(), 100) == []
        assert provider.calls == 1
        await service.update_settings(TRUSTED, recall_enabled=True)
        assert await guarded.select(object(), 100) == ["recalled context"]
        assert provider.calls == 2
    finally:
        identity_context_var.reset(identity_token)


@pytest.mark.asyncio
async def test_automatic_evidence_cannot_supersede_or_invalidate_user_edit(governance):
    service, store, _index = governance
    first = await service.create(make_draft(content="原始事实"), TRUSTED)
    edited = await service.edit(
        first.id, TRUSTED, expected_version=1, content="用户权威修订",
        payload=_semantic_payload("用户权威修订"),
    )
    assert edited.version == 2 and edited.source_type is SourceType.USER_EDIT

    with pytest.raises(PermissionError, match="cannot supersede"):
        await store.update(edited.id, make_draft(content="助手反驳"), TRUSTED)
    with pytest.raises(PermissionError, match="cannot be invalidated"):
        await service.invalidate(edited.id, TRUSTED)
    assert (await store.get(edited.id, TRUSTED)).status is MemoryStatus.ACTIVE


def test_explicit_command_match_rejects_negated_intent():
    assert explicit_remember_matches("Please remember I prefer tea", "I prefer tea")
    assert not explicit_remember_matches("Do not remember this chat", "this chat")
    assert not explicit_remember_matches("不要记住这次对话", "这次对话")
    assert has_forget_intent("Forget preferred editor")
    assert not has_forget_intent("Don't forget my preferred editor")
    assert not has_forget_intent("不要忘记我的编辑器偏好")


@pytest.mark.asyncio
async def test_remember_tool_requires_current_user_consent_and_writes_typed_source(
    governance, tmp_path,
):
    service, store, _index = governance
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    explicit_session = "explicit-session"
    event_id = await _write_user_turn(
        sessions, explicit_session, "Please remember I prefer tea",
    )
    denied_session = "denied-session"
    await _write_user_turn(sessions, denied_session, "I prefer coffee")
    optout_session = "optout-session"
    await _write_user_turn(sessions, optout_session, "Do not remember this chat")
    tool = RememberMemoryV2Tool(service, sessions)
    args = _RememberV2Args(
        content="I prefer tea", kind=MemoryKind.SEMANTIC,
        payload=_semantic_payload("I prefer tea"),
    )
    identity_token = set_identity_context(IDENTITY)
    try:
        for session_id in (denied_session, optout_session):
            binding = memory_session_var.set(session_id)
            try:
                candidate = args if session_id == denied_session else _RememberV2Args(
                    content="this chat", kind=MemoryKind.SEMANTIC,
                    payload=_semantic_payload("this chat"),
                )
                result = await tool.execute(candidate)
            finally:
                memory_session_var.reset(binding)
            assert not result.ok
        binding = memory_session_var.set(explicit_session)
        try:
            result = await tool.execute(args)
        finally:
            memory_session_var.reset(binding)
    finally:
        identity_context_var.reset(identity_token)

    assert result.ok
    record = await store.get(result.data["memory_id"], TRUSTED)
    assert record.source_type is SourceType.EXPLICIT_COMMAND
    assert record.source_event_ids == [event_id]


@pytest.mark.asyncio
async def test_remember_tool_rejects_credentials_without_persisting_them(governance, tmp_path):
    service, store, _index = governance
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    session_id = "credential-session"
    content = "API key: sk-test-abcdefghijklmnopqrstuv"
    await _write_user_turn(sessions, session_id, f"Please remember my {content}")
    tool = RememberMemoryV2Tool(service, sessions)
    args = _RememberV2Args(
        content=content, kind=MemoryKind.SEMANTIC,
        payload=_semantic_payload(content),
    )
    identity_token = set_identity_context(IDENTITY)
    binding = memory_session_var.set(session_id)
    try:
        result = await tool.execute(args)
    finally:
        memory_session_var.reset(binding)
        identity_context_var.reset(identity_token)

    assert not result.ok
    assert await store.list_records(TRUSTED) == []
    async with connect(store.database_path) as connection:
        tombstones = await connection.execute_fetchall("SELECT * FROM memory_v2_tombstones")
    assert tombstones == []


@pytest.mark.asyncio
async def test_forget_tool_returns_ambiguous_selection_without_mutating(governance, tmp_path):
    service, store, _index = governance
    first = await service.create(make_draft(content="常用编辑器是 VS Code"), TRUSTED)
    second = await service.create(make_draft(
        content="常用编辑器是 Neovim", source_event_ids=["editor-event-2"],
    ), TRUSTED)
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    ambiguous_session = "ambiguous-session"
    await _write_user_turn(sessions, ambiguous_session, "忘掉常用编辑器")
    selected_session = "selected-session"
    await _write_user_turn(sessions, selected_session, f"忘掉这条记忆 {first.id}")
    tool = ForgetMemoryV2Tool(service, sessions)
    identity_token = set_identity_context(IDENTITY)
    try:
        binding = memory_session_var.set(ambiguous_session)
        try:
            ambiguous = await tool.execute(_ForgetV2Args(query="常用编辑器"))
        finally:
            memory_session_var.reset(binding)
        assert ambiguous.ok and ambiguous.data["selection_required"] is True
        assert {item["memory_id"] for item in ambiguous.data["matches"]} == {first.id, second.id}
        assert (await store.get(first.id, TRUSTED)).status is MemoryStatus.ACTIVE
        assert (await store.get(second.id, TRUSTED)).status is MemoryStatus.ACTIVE
        binding = memory_session_var.set(selected_session)
        try:
            selected = await tool.execute(_ForgetV2Args(memory_id=first.id))
        finally:
            memory_session_var.reset(binding)
    finally:
        identity_context_var.reset(identity_token)

    assert selected.ok and selected.data["deleted"] is True
    with pytest.raises(KeyError):
        await store.get(first.id, TRUSTED)
    assert (await store.get(second.id, TRUSTED)).status is MemoryStatus.ACTIVE
