"""MEM-V2-4: deletion, settings, source authority, and explicit command contracts."""

from __future__ import annotations

import asyncio
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
    explicit_forget_query_matches,
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
    EpisodicPayload,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    ProceduralPayload,
    SemanticCategory,
    SemanticPayload,
    SourceType,
    TrustedMemoryIdentity,
)
from agent_harness.session import (
    RUN_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
    SessionEvent,
    run_context_var,
)
from agent_harness.session.event import MEMORY_DEGRADED
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
        subject=fact, fact=fact, category=SemanticCategory.PREFERENCE,
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
            "version", "deleted_at", "expires_at", "deletion_reason", "content_hashes",
            "source_hashes",
        }
        rows = await connection.execute_fetchall(
            "SELECT memory_id, version FROM memory_v2_tombstones ORDER BY version DESC"
        )
    serialized = json.dumps([dict(row) for row in rows], ensure_ascii=False)
    assert "最初的偏好" not in serialized and "修订后的偏好" not in serialized
    assert "hash-1" not in serialized and "event-1" not in serialized
    assert [(row["memory_id"], row["version"]) for row in rows] == [
        (second.id, 2), (first.id, 1),
    ]


@pytest.mark.asyncio
async def test_initialize_adds_nullable_version_order_to_legacy_tombstones(tmp_path):
    database_path = tmp_path / "legacy-memory-v2.db"
    async with connect(database_path) as connection:
        await connection.execute(
            "CREATE TABLE memory_v2_tombstones ("
            "memory_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, scope TEXT NOT NULL, project_id TEXT, deleted_at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, deletion_reason TEXT NOT NULL, content_hashes TEXT NOT NULL, "
            "source_hashes TEXT NOT NULL)"
        )
        await connection.execute(
            "INSERT INTO memory_v2_tombstones VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old-memory", "old-root", "tenant-a", "user-a", "user_global", None,
             "2026-09-25T00:00:00+00:00", "2026-10-25T00:00:00+00:00",
             "user_request", "[]", "[]"),
        )
        await connection.commit()

    store = SqliteMemoryV2Store(database_path)
    await store.initialize()

    async with connect(database_path) as connection:
        columns = await connection.execute_fetchall("PRAGMA table_info(memory_v2_tombstones)")
        old_tombstone = await connection.execute_fetchall(
            "SELECT version FROM memory_v2_tombstones WHERE memory_id='old-memory'"
        )
    assert "version" in {row["name"] for row in columns}
    assert old_tombstone[0]["version"] is None


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
async def test_recall_settings_failure_is_a_single_redacted_degradation(tmp_path):
    class _BrokenService:
        async def get_settings(self, _identity):
            raise RuntimeError("secret settings backend detail")

    class _Provider:
        async def select(self, *_args):
            raise AssertionError("recall must not run when its setting is unavailable")

    session = Session.start(
        JsonlSessionStore(root=tmp_path / "sessions"), session_id="session-settings-failed",
    )
    guarded = _MemorySettingsContextProvider(_Provider(), _BrokenService())
    identity_token = set_identity_context(IDENTITY)
    run_token = run_context_var.set("run-settings-failed")
    try:
        assert await guarded.select(session, 100) == []
    finally:
        run_context_var.reset(run_token)
        identity_context_var.reset(identity_token)

    degraded = [event for event in session.events if event.type == MEMORY_DEGRADED]
    assert len(degraded) == 1
    assert degraded[0].run_id == "run-settings-failed"
    assert degraded[0].data == {
        "operation": "recall", "stage": "retrieval",
        "reason_code": "retrieval_unavailable", "job_id": None,
        "attempts": 1, "fallback_used": False,
    }
    assert "secret settings backend detail" not in str(degraded[0].data)


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


@pytest.mark.asyncio
async def test_edit_cannot_resurrect_a_memory_deleted_after_its_initial_read(
    governance, monkeypatch,
):
    service, store, _index = governance
    original = await service.create(make_draft(content="需要删除的权威事实"), TRUSTED)
    insert_started = asyncio.Event()
    resume_insert = asyncio.Event()
    insert = store._insert

    async def pause_before_insert(*args, **kwargs):
        insert_started.set()
        await resume_insert.wait()
        return await insert(*args, **kwargs)

    monkeypatch.setattr(store, "_insert", pause_before_insert)
    edit_task = asyncio.create_task(store.update(
        original.id,
        make_draft(content="删除后不应复活", source_type=SourceType.USER_EDIT),
        TRUSTED,
    ))
    await asyncio.wait_for(insert_started.wait(), timeout=1)
    try:
        receipt = await service.delete(original.id, TRUSTED)
        assert receipt.deleted
    finally:
        resume_insert.set()

    with pytest.raises(ValueError, match="changed|no longer active"):
        await edit_task
    assert await store.list_records(TRUSTED) == []


@pytest.mark.asyncio
async def test_invalidate_cannot_overwrite_a_version_superseded_after_its_initial_read(
    governance, monkeypatch,
):
    service, store, _index = governance
    original = await service.create(make_draft(content="等待失效的事实"), TRUSTED)
    read_complete = asyncio.Event()
    resume_invalidation = asyncio.Event()
    authorized_row = store._authorized_row
    first_read = True

    async def pause_after_first_read(*args, **kwargs):
        nonlocal first_read
        row = await authorized_row(*args, **kwargs)
        if first_read:
            first_read = False
            read_complete.set()
            await resume_invalidation.wait()
        return row

    monkeypatch.setattr(store, "_authorized_row", pause_after_first_read)
    invalidate_task = asyncio.create_task(service.invalidate(original.id, TRUSTED))
    await asyncio.wait_for(read_complete.wait(), timeout=1)
    updated = await store.update(
        original.id, make_draft(content="更新后的事实"), TRUSTED,
    )
    resume_invalidation.set()

    with pytest.raises(ValueError, match="changed|no longer active"):
        await invalidate_task
    assert (await store.get(original.id, TRUSTED)).status is MemoryStatus.SUPERSEDED
    assert (await store.get(updated.id, TRUSTED)).status is MemoryStatus.ACTIVE


def test_explicit_command_match_rejects_negated_intent():
    assert explicit_remember_matches("Please remember I prefer tea", "I prefer tea")
    assert explicit_remember_matches("Can you remember that I prefer tea?", "I prefer tea")
    assert not explicit_remember_matches(
        "Can you remember if I prefer tea?", "I prefer tea",
    )
    assert not explicit_remember_matches(
        "Could you remember whether I prefer tea?", "I prefer tea",
    )
    assert explicit_remember_matches(
        "Remember I prefer tea even if coffee is unavailable", "I prefer tea",
    )
    assert explicit_remember_matches(
        "请记住我常用的问候语是“你好吗”", "我常用的问候语是“你好吗”",
    )
    assert explicit_remember_matches(
        "请记住我常用的问候语是你好吗", "我常用的问候语是你好吗",
    )
    assert not explicit_remember_matches("请记住我喜欢茶吗", "我喜欢茶吗")
    assert explicit_remember_matches(
        "Remember my office uses Slack. I don't agree with my manager's explanation.",
        "my office uses Slack",
    )
    assert not explicit_remember_matches(
        "请你记得我是否患有抑郁症", "患有抑郁症",
    )
    assert not explicit_remember_matches("Do not remember this chat", "this chat")
    assert not explicit_remember_matches(
        "You should not remember this: I prefer tea", "I prefer tea",
    )
    assert not explicit_remember_matches(
        "Remember I prefer tea. My diagnosis is X.", "My diagnosis is X",
    )
    assert not explicit_remember_matches("I remember I prefer tea", "I prefer tea")
    assert not explicit_remember_matches("I’ll remember I prefer tea", "I prefer tea")
    assert not explicit_remember_matches("我记得我喜欢喝茶", "我喜欢喝茶")
    assert not explicit_remember_matches("我记住了我喜欢喝茶", "我喜欢喝茶")
    assert not explicit_remember_matches(
        "You shouldn’t remember this: I prefer tea", "I prefer tea",
    )
    assert not explicit_remember_matches(
        "Remember I prefer tea, but do not store my diagnosis: I have lupus",
        "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and avoid storing my diagnosis: I have lupus.", "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember I like tea and don't include my diagnosis in memory: I have lupus",
        "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember my tea preference and not to include my diagnosis in memory: I have lupus",
        "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember my tea preference and avoid including my diagnosis in memory: I have lupus",
        "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and exclude my diagnosis from memory: I have lupus", "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and omit my diagnosis from memory: I have lupus", "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and I don't want to include my diagnosis in memory: I have lupus",
        "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and no need to add my diagnosis to memory: I have lupus", "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and I don't want you storing my diagnosis in memory: I have lupus",
        "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and I don't want my diagnosis stored in memory: I have lupus",
        "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and I don't want the system to include my diagnosis in memory: I have lupus",
        "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and keep my diagnosis out of memory: I have lupus", "I have lupus",
    )
    assert not explicit_remember_matches(
        "Remember tea and I do not consent to storing my diagnosis in memory: I have lupus",
        "I have lupus",
    )
    for text, candidate in (
        ("Remember my diagnosis is lupus, but keep my diagnosis out of memory", "my diagnosis is lupus"),
        ("Remember my diagnosis is lupus; I do not consent to storing it in memory", "my diagnosis is lupus"),
        ("Remember my diagnosis is lupus. I do not consent to storing it in memory", "my diagnosis is lupus"),
        ("Remember I have lupus. I don't want this remembered.", "I have lupus"),
        ("Remember I have lupus. I don't want this memorized.", "I have lupus"),
        ("Remember I have lupus. Do not memorize this.", "I have lupus"),
        ("Remember tea. I'd rather you not remember this.", "tea"),
        ("Remember tea. I don't wish to have this remembered.", "tea"),
        ("Remember tea. I wish you wouldn't remember this.", "tea"),
        ("Remember tea. Please avoid remembering this.", "tea"),
        ("Remember tea. I do not agree to this.", "tea"),
        ("Remember I have lupus. I don't want this in long-term memory.", "I have lupus"),
        ("Remember I have lupus. I would prefer this not to be remembered.", "I have lupus"),
        ("Remember I have lupus. I would prefer not to have this remembered.", "I have lupus"),
        ("Remember I have lupus. I don't want you remembering this.", "I have lupus"),
        ("Remember tea and I opt out of storing my diagnosis in memory: I have lupus", "I have lupus"),
        ("Remember tea and I am opting out of storing my diagnosis in memory: I have lupus", "I have lupus"),
        ("Remember tea and I withdraw consent to storing my diagnosis in memory: I have lupus", "I have lupus"),
        ("Remember tea, there is no consent to storing my diagnosis in memory: I have lupus", "I have lupus"),
        ("Remember tea and I do not give consent to storing my diagnosis in memory: I have lupus", "I have lupus"),
        ("Remember tea and I refuse to consent to storing my diagnosis in memory: I have lupus", "I have lupus"),
        ("Remember tea and I refuse consent to storing my diagnosis in memory: I have lupus", "I have lupus"),
        ("Remember tea, but I am opting out of this.", "tea"),
        ("Remember tea; I am opting out of that.", "tea"),
        ("Remember tea. I do not consent.", "tea"),
        ("Remember tea. I refuse consent.", "tea"),
        ("Remember tea. I do not give consent.", "tea"),
        ("Remember tea and I do not agree to store my diagnosis in memory: I have lupus", "I have lupus"),
        ("Remember tea and I do not authorize storing my diagnosis in memory: I have lupus", "I have lupus"),
        ("记住茶，我不同意把病情写入记忆：我患有抑郁症", "我患有抑郁症"),
        ("记住茶，我不同意。", "茶"),
        ("记住茶，我拒绝同意。", "茶"),
        ("记住茶，我不愿意让你记住病情。", "茶"),
    ):
        assert not explicit_remember_matches(text, candidate)
        assert not explicit_remember_matches(text, "tea")
    assert not explicit_remember_matches(
        "Remember I prefer tea, and keep my diagnosis out of memory: I have lupus",
        "I prefer tea",
    )
    assert not explicit_remember_matches(
        "Remember I prefer tea, and I do not consent to storing my diagnosis in memory: I have lupus",
        "I prefer tea",
    )
    assert not explicit_remember_matches(
        "Remember tea and keep my diagnosis out of memory: I have lupus", "tea",
    )
    assert explicit_remember_matches(
        "Remember I prefer tea, and do you remember whether I prefer coffee?", "I prefer tea",
    )
    assert not explicit_remember_matches(
        "Remember I prefer tea and remember whether I prefer coffee", "I prefer coffee",
    )
    assert explicit_remember_matches(
        "Remember I prefer tea and remember whether I prefer coffee", "I prefer tea",
    )
    assert not explicit_remember_matches(
        "Remember I prefer tea and remember whether I prefer coffee",
        "I prefer tea and remember whether I prefer coffee",
    )
    assert not explicit_remember_matches(
        "记住我喜欢茶并且你记得我是否喜欢咖啡吗", "我喜欢咖啡",
    )
    assert explicit_remember_matches(
        "记住我喜欢茶并且你记得我是否喜欢咖啡吗", "我喜欢茶",
    )
    assert not explicit_remember_matches(
        "Remember not to store my diagnosis, and remember I prefer tea", "I prefer tea",
    )
    assert not explicit_remember_matches(
        "记住我喜欢茶而且不要把病情写入记忆：我患有抑郁症", "我患有抑郁症",
    )
    assert not explicit_remember_matches(
        "记住我喜欢茶而且我不想把病情写入记忆：我患有抑郁症", "我患有抑郁症",
    )
    assert not explicit_remember_matches(
        "记住我喜欢茶而且不希望把病情加入记忆：我患有抑郁症", "我患有抑郁症",
    )
    assert not explicit_remember_matches(
        "记得我患有抑郁症吗？", "我患有抑郁症",
    )
    assert not explicit_remember_matches("不要记住这次对话", "这次对话")
    assert has_forget_intent("Forget preferred editor")
    assert not has_forget_intent("Don't forget my preferred editor")
    assert not has_forget_intent("You shouldn't forget my preferred editor")
    assert not has_forget_intent(
        "PLEASE FORGET MY PREFERRED EDITOR, BUT DO NOT DELETE THE MEMORY ABOUT MY PREFERRED EDITOR."
    )
    assert not has_forget_intent("Don't delete the memory about coffee")
    assert not has_forget_intent("Don’t delete the memory about coffee")
    assert not has_forget_intent("I don't want you to delete the memory about coffee")
    assert not has_forget_intent("我不想让你删除记忆：咖啡偏好")
    assert not explicit_forget_query_matches(
        "I’ll forget the memory about coffee", "coffee",
    )
    assert not explicit_forget_query_matches(
        "我忘记了这条关于咖啡的记忆", "咖啡",
    )
    assert not explicit_forget_query_matches(
        "Forget my coffee preference. I also like tea.", "tea",
    )
    assert explicit_forget_query_matches("Forget my coffee preference", "coffee")
    assert not has_forget_intent("不要忘记我的编辑器偏好")


def test_remember_args_reject_payload_text_outside_consented_content():
    for subject, fact in (("I", "I have lupus"), ("I have lupus", "I prefer tea")):
        with pytest.raises(ValueError, match="payload.*content"):
            _RememberV2Args(
                content="I prefer tea", kind=MemoryKind.SEMANTIC,
                payload=SemanticPayload(
                    subject=subject, fact=fact, category=SemanticCategory.PREFERENCE,
                ),
            )


def test_remember_args_preserve_negation_in_semantic_fact():
    for content, fact in (
        ("I do not have diabetes", "have diabetes"),
        ("I am free of diabetes", "diabetes"),
        ("I am free from diabetes", "diabetes"),
        ("I am a non-smoker", "smoker"),
        ("I am a nonsmoker", "smoker"),
        ("I am a non-drinker", "drinker"),
        ("I am a non-vegetarian", "vegetarian"),
        ("I am a non-driver", "driver"),
        ("I am diabetes-free", "diabetes"),
        ("I am devoid of diabetes", "diabetes"),
        ("There is an absence of diabetes", "diabetes"),
        ("I deny having diabetes", "having diabetes"),
        ("我并非糖尿病患者", "糖尿病患者"),
        ("我未患有糖尿病", "患有糖尿病"),
        ("我不喜欢咖啡", "喜欢咖啡"),
        ("我否认自己患有糖尿病", "患有糖尿病"),
        ("我并不是糖尿病患者", "糖尿病患者"),
        ("我无糖尿病史", "糖尿病史"),
        ("我未诊断为糖尿病", "诊断为糖尿病"),
        ("我没得过糖尿病", "得过糖尿病"),
        ("我无糖尿病", "糖尿病"),
        ("我非糖尿病患者", "糖尿病患者"),
        ("我非吸烟者", "吸烟者"),
        ("我非素食者", "素食者"),
    ):
        with pytest.raises(ValueError, match="negated content"):
            _RememberV2Args(
                content=content, kind=MemoryKind.SEMANTIC,
                payload=SemanticPayload(
                    subject=fact, fact=fact, category=SemanticCategory.PROFILE,
                ),
            )
        args = _RememberV2Args(
            content=content, kind=MemoryKind.SEMANTIC,
            payload=SemanticPayload(
                subject=fact,
                fact=content, category=SemanticCategory.PROFILE,
            ),
        )
        assert args.payload.fact == content

    with pytest.raises(ValueError, match="negated content"):
        _RememberV2Args(
            content="I do not have diabetes", kind=MemoryKind.EPISODIC,
            payload=EpisodicPayload(
                situation="I do not have diabetes", action="I do not have diabetes",
                outcome="I do not have diabetes", lesson="I do not have diabetes",
            ),
        )

    args = _RememberV2Args(
        content="用不锈钢盆揉面，直到面团均匀", kind=MemoryKind.PROCEDURAL,
        payload=ProceduralPayload(
            trigger="揉面", procedure="用不锈钢盆揉面", success_condition="面团均匀",
        ),
    )
    assert args.payload.procedure == "用不锈钢盆揉面"

    args = _RememberV2Args(
        content="在无锡使用工具，直到结果成功", kind=MemoryKind.PROCEDURAL,
        payload=ProceduralPayload(
            trigger="使用工具", procedure="在无锡使用工具", success_condition="结果成功",
        ),
    )
    assert args.payload.procedure == "在无锡使用工具"

    args = _RememberV2Args(
        content="Use a non-stick pan to fry an egg", kind=MemoryKind.PROCEDURAL,
        payload=ProceduralPayload(
            trigger="fry an egg", procedure="Use a non-stick pan to fry an egg",
            success_condition="fry an egg",
        ),
    )
    assert args.payload.procedure == "Use a non-stick pan to fry an egg"

    args = _RememberV2Args(
        content="这位患者非常积极", kind=MemoryKind.SEMANTIC,
        payload=SemanticPayload(
            subject="患者", fact="这位患者非常积极", category=SemanticCategory.PROFILE,
        ),
    )
    assert args.payload.fact == "这位患者非常积极"


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
    assert record.scope is MemoryScope.USER_GLOBAL
    assert record.source_event_ids == [event_id]


@pytest.mark.asyncio
async def test_remember_tool_rejects_unbound_or_negated_content(governance, tmp_path):
    service, store, _index = governance
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    cases = (
        ("negated", "You should not remember this: I prefer tea", "I prefer tea"),
        ("unbound", "Remember I prefer tea. My diagnosis is X.", "My diagnosis is X"),
        ("reported", "我记得我喜欢喝茶", "我喜欢喝茶"),
        ("curly-negation", "You shouldn’t remember this: I prefer tea", "I prefer tea"),
        ("unconsented", "Remember tea, but do not store my diagnosis: I have lupus",
         "I have lupus"),
        ("opt-out", "Remember tea and I opt out of storing my diagnosis in memory: I have lupus",
         "I have lupus"),
        ("passive-optout", "Remember I have lupus. I don't want this remembered.",
         "I have lupus"),
        ("chinese-no-consent", "记住茶，我不同意把病情写入记忆：我患有抑郁症", "我患有抑郁症"),
        ("excluded", "Remember tea and don't include my diagnosis in memory: I have lupus",
         "I have lupus"),
        ("not-to-include", "Remember tea and not to include my diagnosis in memory: I have lupus",
         "I have lupus"),
        ("avoid-including", "Remember tea and avoid including my diagnosis in memory: I have lupus",
         "I have lupus"),
        ("excluded-from-memory", "Remember tea and exclude my diagnosis from memory: I have lupus",
         "I have lupus"),
        ("omit-from-memory", "Remember tea and omit my diagnosis from memory: I have lupus",
         "I have lupus"),
        ("keep-out-of-memory", "Remember tea and keep my diagnosis out of memory: I have lupus",
         "I have lupus"),
        ("no-consent-to-store", "Remember tea and I do not consent to storing my diagnosis in memory: I have lupus",
         "I have lupus"),
        ("do-not-want-include", "Remember tea and I don't want to include my diagnosis in memory: I have lupus",
         "I have lupus"),
        ("no-need-add", "Remember tea and no need to add my diagnosis to memory: I have lupus",
         "I have lupus"),
        ("chinese-excluded", "记住我喜欢茶而且不要把病情写入记忆：我患有抑郁症",
         "我患有抑郁症"),
        ("chinese-do-not-want", "记住我喜欢茶而且我不想把病情写入记忆：我患有抑郁症",
         "我患有抑郁症"),
    )
    tool = RememberMemoryV2Tool(service, sessions)
    identity_token = set_identity_context(IDENTITY)
    try:
        for session_id, text, content in cases:
            await _write_user_turn(sessions, session_id, text)
            binding = memory_session_var.set(session_id)
            try:
                result = await tool.execute(_RememberV2Args(
                    content=content, kind=MemoryKind.SEMANTIC,
                    payload=_semantic_payload(content),
                ))
            finally:
                memory_session_var.reset(binding)
            assert not result.ok
    finally:
        identity_context_var.reset(identity_token)
    assert await store.list_records(TRUSTED) == []


@pytest.mark.asyncio
async def test_remember_tool_rejects_whole_message_when_any_fact_is_opted_out(
    governance, tmp_path,
):
    service, store, _index = governance
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    text = "Remember I prefer tea, and keep my diagnosis out of memory: I have lupus"
    await _write_user_turn(sessions, "safe-fact-session", text)
    await _write_user_turn(sessions, "denied-fact-session", text)
    tool = RememberMemoryV2Tool(service, sessions)
    identity_token = set_identity_context(IDENTITY)
    try:
        binding = memory_session_var.set("safe-fact-session")
        try:
            safe_result = await tool.execute(_RememberV2Args(
                content="I prefer tea", kind=MemoryKind.SEMANTIC,
                payload=_semantic_payload("I prefer tea"),
            ))
        finally:
            memory_session_var.reset(binding)

        binding = memory_session_var.set("denied-fact-session")
        try:
            denied_result = await tool.execute(_RememberV2Args(
                content="I have lupus", kind=MemoryKind.SEMANTIC,
                payload=_semantic_payload("I have lupus"),
            ))
        finally:
            memory_session_var.reset(binding)
    finally:
        identity_context_var.reset(identity_token)

    assert not safe_result.ok
    assert not denied_result.ok
    assert await store.list_records(TRUSTED) == []


@pytest.mark.asyncio
async def test_explicit_remember_uses_the_current_project_scope(governance, tmp_path):
    from types import SimpleNamespace

    service, store, _index = governance
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    session_id = "project-session"
    await _write_user_turn(sessions, session_id, "Remember this project uses pnpm")

    class WorkspaceIndex:
        @staticmethod
        def workspace_of_session(_session_id):
            return SimpleNamespace(id="project-a")

    tool = RememberMemoryV2Tool(service, sessions, workspace_index=WorkspaceIndex())
    binding = memory_session_var.set(session_id)
    identity_token = set_identity_context(IDENTITY)
    try:
        result = await tool.execute(_RememberV2Args(
            content="this project uses pnpm", kind=MemoryKind.SEMANTIC,
            payload=_semantic_payload("this project uses pnpm"),
        ))
    finally:
        identity_context_var.reset(identity_token)
        memory_session_var.reset(binding)

    assert result.ok
    project_identity = TrustedMemoryIdentity("tenant-a", "user-a", project_id="project-a")
    record = await store.get(result.data["memory_id"], project_identity)
    assert record.scope is MemoryScope.PROJECT
    assert record.project_id == "project-a"


@pytest.mark.asyncio
async def test_negated_forget_tool_does_not_delete_matching_memory(governance, tmp_path):
    service, store, _index = governance
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    record = await service.create(make_draft(content="my preferred editor is VS Code"), TRUSTED)
    tool = ForgetMemoryV2Tool(service, sessions)
    cases = (
        ("negated-delete", "Don't delete the memory about my preferred editor"),
        ("curly-negated-delete", "Don’t delete the memory about my preferred editor"),
        ("want-delete", "I don't want you to delete the memory about my preferred editor"),
        ("chinese-want-delete", "我不想让你删除记忆：my preferred editor"),
        ("negated-forget", "You shouldn't forget my preferred editor"),
        ("reported-forget", "我忘记了这条关于 my preferred editor 的记忆"),
    )
    identity_token = set_identity_context(IDENTITY)
    try:
        for session_id, text in cases:
            await _write_user_turn(sessions, session_id, text)
            binding = memory_session_var.set(session_id)
            try:
                result = await tool.execute(_ForgetV2Args(query="preferred editor"))
            finally:
                memory_session_var.reset(binding)
            assert not result.ok
    finally:
        identity_context_var.reset(identity_token)

    assert (await store.get(record.id, TRUSTED)).status is MemoryStatus.ACTIVE


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
