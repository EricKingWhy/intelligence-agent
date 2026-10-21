"""#1437：`memory` 工具写入的派生层投影（F-01 甲案 + F-06 重试 + F-06b 全量重投影）。

票面裁决：markdown 仍是权威，工具写入也要投影进 SQLite/FTS，`retrieve_memory` 可检索。
设计稿：`~/workspace/ticket-1437/design.md` §3–§9。用例编号与 §8 表格一一对应。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from agent_harness.memory.v2 import reprojection
from agent_harness.memory.v2._sqlite import connect
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.markdown_store import ENTRY_DELIMITER, MarkdownMemoryStore, entry_digest
from agent_harness.memory.v2.memory_tool import MemoryTool, _MemoryArgs
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import (
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryScope,
    MemoryTier,
    SemanticCategory,
    SemanticPayload,
    SourceType,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.write_approval import StagedWriteStore

pytestmark = pytest.mark.asyncio

TRUSTED = TrustedMemoryIdentity("local", "local")


async def build(tmp_path: Path):
    """与金集 `:150-156` 同形的装配（避免第二套装配语义）。"""
    store = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await store.initialize()
    markdown = MarkdownMemoryStore(tmp_path / "memories")
    markdown.load_from_disk()
    service = MemoryV2Service(store, InMemoryMemoryV2Index(), markdown=markdown)
    return store, markdown, service


def _pending_rows(db_path) -> list[tuple]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT target, content_digest, content, attempts, last_error "
            "FROM memory_v2_projection_pending"
        ).fetchall()


def _projection_columns(db_path) -> list[tuple]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT projection_source, projection_target FROM memory_v2_records"
        ).fetchall()


def _tombstone_hashes(db_path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        return [
            value
            for (raw,) in connection.execute("SELECT content_hashes FROM memory_v2_tombstones")
            for value in __import__("json").loads(raw)
        ]


def _digests(markdown: MarkdownMemoryStore, target: str) -> set[str]:
    return {entry_digest(entry) for entry in markdown.entries(target)}


async def _active_tool_digests(store: SqliteMemoryV2Store, target: str) -> set[str]:
    return set(await store.tool_projection_digests(TRUSTED, target))


def _flaky_create(store: SqliteMemoryV2Store, state: dict, error_type=sqlite3.OperationalError):
    """注入 SQLite 写失败（`store.create` 层面，不是索引层）。"""
    real = store.create

    async def flaky(draft, trusted, **kwargs):
        if state["fail"]:
            raise error_type("disk I/O error")
        return await real(draft, trusted, **kwargs)

    return flaky


# ---------------------------------------------------------------- AC1：add 后可检索


async def test_add_target_memory_is_retrievable(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)

    result = await tool.execute(_MemoryArgs(
        action="add", target="memory", content="用户习惯清晨喝冷萃咖啡",
    ))

    assert result.ok is True, result
    records = await store.list_active(TRUSTED, limit=10)
    assert [record.content for record in records] == ["用户习惯清晨喝冷萃咖啡"]
    hits = await service.hybrid_search(
        "冷萃", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert [hit.record.content for hit in hits] == ["用户习惯清晨喝冷萃咖啡"]


async def test_add_target_user_is_retrievable(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)

    result = await tool.execute(_MemoryArgs(
        action="add", target="user", content="用户养了一只叫芝麻的黑猫",
    ))

    assert result.ok is True, result
    hits = await service.hybrid_search(
        "黑猫", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert [hit.record.content for hit in hits] == ["用户养了一只叫芝麻的黑猫"]


async def test_add_record_shape_matches_the_mapping(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)

    await tool.execute(_MemoryArgs(action="add", target="user", content="用户偏好简洁的回答"))

    records = await store.list_active(TRUSTED, limit=10)
    assert len(records) == 1
    record = records[0]
    assert record.kind is MemoryKind.SEMANTIC
    assert record.tier is MemoryTier.COLLECTION
    assert record.scope is MemoryScope.USER_GLOBAL
    assert record.project_id is None
    assert record.source_type is SourceType.USER_EDIT
    assert record.source_session_id is None
    assert record.source_event_ids == []
    assert record.payload.category is SemanticCategory.PROFILE
    assert record.payload.fact == record.content
    assert record.evidence[0].hash == entry_digest(record.content)
    assert _projection_columns(store.database_path) == [("tool", "user")]


async def test_markdown_entry_survives_a_failed_projection(tmp_path, monkeypatch) -> None:
    store, markdown, service = await build(tmp_path)
    monkeypatch.setattr(store, "create", _flaky_create(store, {"fail": True}))
    tool = MemoryTool(markdown, service=service)

    result = await tool.execute(_MemoryArgs(action="add", target="memory", content="投影会失败"))

    assert result.ok is True, result
    assert result.data["projection"] == "pending"
    assert "投影会失败" in markdown.path_for("memory").read_text(encoding="utf-8")


# ------------------------------------------------------- AC2：失败注入 → 重试收敛


async def test_projection_failure_leaves_a_durable_pending_row(tmp_path, monkeypatch) -> None:
    store, markdown, service = await build(tmp_path)
    monkeypatch.setattr(store, "create", _flaky_create(store, {"fail": True}))
    tool = MemoryTool(markdown, service=service)

    await tool.execute(_MemoryArgs(action="add", target="memory", content="待重试的条目"))

    rows = _pending_rows(store.database_path)
    assert len(rows) == 1
    target, digest, content, attempts, last_error = rows[0]
    assert (target, content) == ("memory", "待重试的条目")
    assert digest == entry_digest("待重试的条目")
    assert attempts == 1
    assert last_error == "OperationalError"
    assert await store.list_active(TRUSTED, limit=10) == []


async def test_retry_converges_without_losing_the_entry(tmp_path, monkeypatch) -> None:
    store, markdown, service = await build(tmp_path)
    state = {"fail": True}
    monkeypatch.setattr(store, "create", _flaky_create(store, state))
    tool = MemoryTool(markdown, service=service)
    await tool.execute(_MemoryArgs(action="add", target="memory", content="重试后必须能搜到"))

    state["fail"] = False
    first = await reprojection.drain_pending(store, markdown, TRUSTED)
    second = await reprojection.drain_pending(store, markdown, TRUSTED)

    assert first["remaining"] == 0, first
    assert second == {"drained": 0, "remaining": 0, "errors": []}, second
    assert _pending_rows(store.database_path) == []
    assert len(await store.list_active(TRUSTED, limit=10)) == 1
    hits = await service.hybrid_search(
        "重试后", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert len(hits) == 1


async def test_pending_row_is_dropped_when_markdown_no_longer_has_it(tmp_path, monkeypatch) -> None:
    store, markdown, service = await build(tmp_path)
    monkeypatch.setattr(store, "create", _flaky_create(store, {"fail": True}))
    tool = MemoryTool(markdown, service=service)
    await tool.execute(_MemoryArgs(action="add", target="memory", content="用户会手删这条"))
    assert len(_pending_rows(store.database_path)) == 1

    markdown.path_for("memory").write_text("", encoding="utf-8")
    markdown.load_from_disk()
    summary = await reprojection.drain_pending(store, markdown, TRUSTED)

    assert summary["remaining"] == 0
    assert _pending_rows(store.database_path) == []
    assert await store.list_active(TRUSTED, limit=10) == []


# --------------------------------------- AC3：漂移备份后全量重投影 + tombstone + 幂等


async def _add_two(store, markdown, service) -> MemoryTool:
    tool = MemoryTool(markdown, service=service)
    for content in ("alpha entry", "beta entry"):
        assert (await tool.execute(_MemoryArgs(
            action="add", target="memory", content=content))).ok is True
    assert await _active_tool_digests(store, "memory") == {
        entry_digest("alpha entry"), entry_digest("beta entry")}
    return tool


async def test_drift_backup_triggers_full_reprojection(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = await _add_two(store, markdown, service)

    markdown.path_for("memory").write_text("alpha entry   \n§\nhand-written fact", encoding="utf-8")
    result = await tool.execute(_MemoryArgs(
        action="replace", target="memory",
        old_text="hand-written fact", content="hand-written fact, updated",
    ))

    assert result.ok is True, result
    assert result.data.get("drift_backup")
    assert await _active_tool_digests(store, "memory") == _digests(markdown, "memory")


async def test_vanished_entries_get_tombstones(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = await _add_two(store, markdown, service)

    # 手删 beta entry，并用条目内尾随空格制造 round-trip 不一致（漂移信号）。
    markdown.path_for("memory").write_text("alpha entry   \n§\nhand note", encoding="utf-8")
    result = await tool.execute(_MemoryArgs(
        action="replace", target="memory", old_text="alpha entry", content="alpha entry, updated",
    ))

    assert result.ok is True, result
    assert result.data.get("drift_backup")
    assert await _active_tool_digests(store, "memory") == _digests(markdown, "memory")
    assert entry_digest("beta entry") in _tombstone_hashes(store.database_path)


async def test_reprojection_is_idempotent(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    await _add_two(store, markdown, service)

    first = await service.reproject(TRUSTED, "memory")
    before = sorted(
        (record.content, record.id) for record in await store.list_active(TRUSTED, limit=10))
    second = await service.reproject(TRUSTED, "memory")
    after = sorted(
        (record.content, record.id) for record in await store.list_active(TRUSTED, limit=10))

    assert (second["created"], second["deleted"]) == (0, 0), second
    assert first["created"] == 0
    assert before == after


async def test_reprojection_leaves_service_rows_alone(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    record = await service.create(MemoryDraftV2(
        kind=MemoryKind.SEMANTIC, tier=MemoryTier.COLLECTION, scope=MemoryScope.USER_GLOBAL,
        content="service row", payload=SemanticPayload(
            subject="s", fact="service row", category=SemanticCategory.PREFERENCE),
        importance=0.8, strength=0.8, source_type=SourceType.AUTOMATIC,
        source_session_id="s-1", source_event_ids=["e-1"],
        evidence=[EvidenceItem(role="user", excerpt="service row", hash="0" * 64)],
    ), TRUSTED)
    markdown.path_for("memory").write_text("", encoding="utf-8")
    markdown.load_from_disk()

    summary = await service.reproject(TRUSTED, "memory")

    assert summary["deleted"] == 0
    kept = await store.get(record.id, TRUSTED)
    assert kept.content == "service row"


async def test_reprojection_dedups_identical_text_across_targets(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    markdown.path_for("memory").write_text("同一句话", encoding="utf-8")
    markdown.path_for("user").write_text("同一句话", encoding="utf-8")
    markdown.load_from_disk()

    await service.reproject(TRUSTED, "memory")
    await service.reproject(TRUSTED, "user")

    records = await store.list_active(TRUSTED, limit=10)
    assert [record.content for record in records] == ["同一句话"]


# ------------------------------------------------------------ AC4 + 边界


async def test_remove_then_add_same_content_is_not_blocked_by_tombstone(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    assert (await tool.execute(_MemoryArgs(
        action="add", target="memory", content="会被删掉再加回"))).ok is True
    assert (await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="会被删掉再加回"))).ok is True
    assert await store.list_active(TRUSTED, limit=10) == []

    result = await tool.execute(_MemoryArgs(
        action="add", target="memory", content="会被删掉再加回"))

    assert result.ok is True, result
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "会被删掉再加回"]


async def test_replace_and_remove_propagate_to_the_derived_layer(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    await tool.execute(_MemoryArgs(action="add", target="memory", content="old fact"))

    assert (await tool.execute(_MemoryArgs(
        action="replace", target="memory", old_text="old fact", content="new fact"))).ok is True
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == ["new fact"]
    assert entry_digest("old fact") in _tombstone_hashes(store.database_path)

    assert (await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="new fact"))).ok is True
    assert await store.list_active(TRUSTED, limit=10) == []
    assert entry_digest("new fact") in _tombstone_hashes(store.database_path)


async def test_batch_propagates_replaced_and_removed_entries(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    for content in ("keep me", "replace me", "remove me"):
        assert (await tool.execute(_MemoryArgs(
            action="add", target="memory", content=content))).ok is True

    result = await tool.execute(_MemoryArgs(target="memory", operations=[
        {"action": "replace", "old_text": "replace me", "content": "replaced content"},
        {"action": "remove", "old_text": "remove me"},
        {"action": "add", "content": "brand new"},
    ]))

    assert result.ok is True, result
    assert await _active_tool_digests(store, "memory") == _digests(markdown, "memory")
    assert {record.content for record in await store.list_active(TRUSTED, limit=10)} == {
        "keep me", "replaced content", "brand new"}


async def test_oversized_entry_is_skipped_not_truncated(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    oversized = "长" * 501

    result = await tool.execute(_MemoryArgs(action="add", target="memory", content=oversized))

    assert result.ok is True, result
    assert oversized in markdown.entries("memory")
    assert await store.list_active(TRUSTED, limit=10) == []
    assert _pending_rows(store.database_path) == []
    assert ["memory", entry_digest(oversized), "content_too_long"] in result.data["projection_skipped"]


async def test_tool_without_service_stays_markdown_only(tmp_path) -> None:
    store, markdown, _service = await build(tmp_path)
    tool = MemoryTool(markdown)

    result = await tool.execute(_MemoryArgs(action="add", target="memory", content="纯 markdown"))

    assert result.ok is True, result
    assert markdown.entries("memory") == ["纯 markdown"]
    assert await store.list_active(TRUSTED, limit=10) == []


# ---------------------------------------------------- 审批重放路径（门审条件 ②）


async def test_approved_write_reaches_the_derived_layer(tmp_path) -> None:
    """stage → approve → 派生层可见（门审条件 ② 的端到端用例）。"""
    store, markdown, service = await build(tmp_path)
    staged = StagedWriteStore(store.database_path, markdown, store=store)
    await staged.initialize()
    await service.update_settings(TRUSTED, write_approval=True)
    tool = MemoryTool(markdown, service=service, staged=staged)

    staged_result = await tool.execute(_MemoryArgs(
        action="add", target="memory", content="审批通过后才落盘"))
    assert staged_result.ok is True, staged_result
    assert staged_result.data["staged"] is True
    assert await store.list_active(TRUSTED, limit=10) == []

    decision, response = await staged.approve(staged_result.data["pending_id"], TRUSTED)

    assert decision.allow is True, decision
    assert response is not None and response["success"], response
    hits = await service.hybrid_search(
        "审批", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert [hit.record.content for hit in hits] == ["审批通过后才落盘"]


async def test_projection_failure_does_not_block_approval(tmp_path, monkeypatch) -> None:
    """投影失败不得回滚已批准的写入（markdown 是权威）。"""
    store, markdown, service = await build(tmp_path)
    staged = StagedWriteStore(store.database_path, markdown, store=store)
    await staged.initialize()
    await service.update_settings(TRUSTED, write_approval=True)
    tool = MemoryTool(markdown, service=service, staged=staged)
    pending = await tool.execute(_MemoryArgs(action="add", target="memory", content="投影失败也批准"))
    monkeypatch.setattr(store, "create", _flaky_create(store, {"fail": True}))

    decision, response = await staged.approve(pending.data["pending_id"], TRUSTED)

    assert decision.allow is True, decision
    assert "投影失败也批准" in markdown.path_for("memory").read_text(encoding="utf-8")
    assert len(_pending_rows(store.database_path)) == 1
