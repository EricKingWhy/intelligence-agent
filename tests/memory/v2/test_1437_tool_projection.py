"""#1437：`memory` 工具写入的派生层投影（F-01 甲案 + F-06 重试 + F-06b 全量重投影）。

票面裁决：markdown 仍是权威，工具写入也要投影进 SQLite/FTS，`retrieve_memory` 可检索。
设计稿：`~/workspace/ticket-1437/design.md` §3–§9。用例编号与 §8 表格一一对应。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.types import memory_session_var
from agent_harness.memory.v2 import origin, reprojection
from agent_harness.memory.v2 import store as store_module
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore, entry_digest
from agent_harness.memory.v2.memory_tool import MemoryTool, _MemoryArgs
from agent_harness.memory.v2.search_tool import RetrieveMemoryV2Tool
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
            for value in json.loads(raw)
        ]


def _index_names(db_path) -> dict[str, bool]:
    with sqlite3.connect(db_path) as connection:
        return {
            name: bool(unique)
            for name, unique in connection.execute(
                "SELECT name, \"unique\" FROM pragma_index_list('memory_v2_records')")
        }


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


def _flaky_delete(store: SqliteMemoryV2Store, state: dict, error_type=sqlite3.OperationalError):
    """注入删除失败（撤回路径的 write-ahead 用例）。"""
    real = store.delete

    async def flaky(memory_id, trusted, **kwargs):
        if state["fail"]:
            raise error_type("disk I/O error")
        return await real(memory_id, trusted, **kwargs)

    return flaky


# ---------------------------------------------------------------- AC1：add 后可检索


async def test_add_target_memory_is_retrievable(tmp_path) -> None:
    """AC1 端到端：`memory` 工具写入 → `retrieve_memory` 工具能搜到（审查 Corr P4-2）。"""
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)

    result = await tool.execute(_MemoryArgs(
        action="add", target="memory", content="用户习惯清晨喝冷萃咖啡",
    ))

    assert result.ok is True, result
    records = await store.list_active(TRUSTED, limit=10)
    assert [record.content for record in records] == ["用户习惯清晨喝冷萃咖啡"]
    search = RetrieveMemoryV2Tool(service)
    identity_token = set_identity_context(IdentityContext("local", "local", ["user"]))
    try:
        found = await search.execute(search.args_schema(query="冷萃", limit=5))
    finally:
        identity_context_var.reset(identity_token)
    assert found.ok is True, found
    assert [item["content"] for item in found.data["memories"]] == ["用户习惯清晨喝冷萃咖啡"]


async def test_add_target_user_is_retrievable(tmp_path) -> None:
    _, markdown, service = await build(tmp_path)
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


async def test_remove_keeps_the_row_while_the_other_file_still_has_it(tmp_path) -> None:
    """审查 Corr P2-1 回归：同一文本在两个文件里时，删一个文件不得删掉共享的派生行。"""
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    assert (await tool.execute(_MemoryArgs(
        action="add", target="memory", content="两个文件都有这句话"))).ok is True
    await markdown.add("user", "两个文件都有这句话")
    await service.reproject(TRUSTED, "user")
    assert len(await store.list_active(TRUSTED, limit=10)) == 1

    result = await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="两个文件都有这句话"))
    assert result.ok is True
    # 票面 AC3 第三句：真 `kept` 不是"没收敛"，回执必须是 done（#1482 F5）。
    assert result.data["projection"] == "done"

    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "两个文件都有这句话"]
    hits = await service.hybrid_search(
        "两个文件", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert len(hits) == 1
    assert _pending_rows(store.database_path) == []

    # 两个文件都删掉后，共享行才该消失。
    assert (await tool.execute(_MemoryArgs(
        action="remove", target="user", old_text="两个文件都有这句话"))).ok is True
    assert await store.list_active(TRUSTED, limit=10) == []


async def test_concurrent_identical_creates_collapse_to_one_row(tmp_path) -> None:
    """审查 Corr P2-2 回归：对账键上的部分唯一索引兜住并发双写。"""
    store, _markdown, _service = await build(tmp_path)
    draft = reprojection.draft_for("memory", "并发同内容")

    first = await store.create(draft, TRUSTED, projection_source="tool",
                               projection_target="memory")
    with pytest.raises(ValueError, match="conflicts with an existing row"):
        await store.create(draft, TRUSTED, projection_source="tool",
                           projection_target="memory")

    assert [record.id for record in await store.list_active(TRUSTED, limit=10)] == [first.id]
    assert set(await store.tool_projection_digests(TRUSTED, "memory")) == {
        entry_digest("并发同内容")}


async def test_project_scope_service_row_does_not_suppress_the_tool_projection(tmp_path) -> None:
    """审查 Corr P2-3 回归：去重域只含工具行，PROJECT scope 的服务行不得挡住投影。"""
    store, markdown, service = await build(tmp_path)
    trusted = TrustedMemoryIdentity("local", "local", "project-a")
    await service.create(MemoryDraftV2(
        kind=MemoryKind.SEMANTIC, tier=MemoryTier.COLLECTION, scope=MemoryScope.PROJECT,
        project_id="project-a", content="用户偏好简洁的回答",
        payload=SemanticPayload(
            subject="s", fact="用户偏好简洁的回答", category=SemanticCategory.PREFERENCE),
        importance=0.8, strength=0.8, source_type=SourceType.AUTOMATIC,
        source_session_id="s-1", source_event_ids=["e-1"],
        evidence=[EvidenceItem(role="user", excerpt="简洁", hash="0" * 64)],
    ), trusted)
    markdown.path_for("user").write_text("用户偏好简洁的回答", encoding="utf-8")
    markdown.load_from_disk()

    summary = await service.reproject(trusted, "user")

    assert (summary["created"], summary["desired"]) == (1, 1), summary
    assert set(await store.tool_projection_digests(trusted, "user")) == {
        entry_digest("用户偏好简洁的回答")}
    second = await service.reproject(trusted, "user")
    assert (second["created"], second["deleted"]) == (0, 0), second


async def test_failed_retraction_leaves_a_pending_row_that_drains_to_deletion(
    tmp_path, monkeypatch,
) -> None:
    """审查 Std P2-3 回归：删除失败也要落账本；drain 之后该条目不再可检索。"""
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    assert (await tool.execute(_MemoryArgs(
        action="add", target="memory", content="先写进去再删不掉"))).ok is True
    state = {"fail": True}
    monkeypatch.setattr(store, "delete", _flaky_delete(store, state))

    result = await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="先写进去再删不掉"))

    assert result.ok is True, result
    assert result.data["projection"] == "pending"
    rows = _pending_rows(store.database_path)
    assert [(target, content, attempts, last_error) for target, _d, content, attempts,
            last_error in rows] == [("memory", "先写进去再删不掉", 1, "OperationalError")]
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "先写进去再删不掉"]

    state["fail"] = False
    summary = await reprojection.drain_pending(store, markdown, TRUSTED)

    assert summary == {"drained": 1, "remaining": 0, "errors": []}, summary
    assert await store.list_active(TRUSTED, limit=10) == []
    hits = await service.hybrid_search(
        "删不掉", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert hits == []
    assert entry_digest("先写进去再删不掉") in _tombstone_hashes(store.database_path)


async def test_session_read_failure_still_projects(tmp_path, monkeypatch) -> None:
    """审查 Corr P3-1 回归：会话存储读失败只丢 provenance，不丢投影。"""
    store, markdown, service = await build(tmp_path)

    class BrokenSessions:
        def read_events(self, session_id):
            raise OSError("session store unavailable")

    tool = MemoryTool(markdown, service=service, sessions=BrokenSessions())
    token = memory_session_var.set("session-1")
    # 后台来源不跑 add 软校验（它自己也读会话），这里只隔离投影路径的会话读取。
    origin_token = origin.set_origin(origin.BACKGROUND_REVIEW)
    try:
        result = await tool.execute(_MemoryArgs(
            action="add", target="memory", content="会话挂了也要投影"))
    finally:
        origin.reset_origin(origin_token)
        memory_session_var.reset(token)

    assert result.ok is True, result
    assert result.data["projection"] == "done"
    assert "会话挂了也要投影" in markdown.path_for("memory").read_text(encoding="utf-8")
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "会话挂了也要投影"]
    assert _pending_rows(store.database_path) == []


async def test_replace_and_remove_propagate_to_the_derived_layer(tmp_path) -> None:
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    await tool.execute(_MemoryArgs(action="add", target="memory", content="old fact"))
    old_root = (await store.list_active(TRUSTED, limit=10))[0].root_id

    assert (await tool.execute(_MemoryArgs(
        action="replace", target="memory", old_text="old fact", content="new fact"))).ok is True
    records = await store.list_active(TRUSTED, limit=10)
    assert [record.content for record in records] == ["new fact"]
    # 门审条件 ④：工具行的 replace 是「删旧行 + 建新 root」，不是同一 root 的 v2。
    assert (records[0].version, records[0].root_id != old_root) == (1, True)
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

    decision, _ = await staged.approve(pending.data["pending_id"], TRUSTED)

    assert decision.allow is True, decision
    assert "投影失败也批准" in markdown.path_for("memory").read_text(encoding="utf-8")
    assert len(_pending_rows(store.database_path)) == 1


# ------------------------------------------------- Phase B 复审回归（Corr N1–N5）


async def test_legacy_database_without_projection_columns_still_initializes(tmp_path) -> None:
    """审查 Corr N1 回归：`#1437` 之前建的库要能升级，不能在建索引时抛 no such column。"""
    db_path = tmp_path / "legacy.db"
    legacy = SqliteMemoryV2Store(db_path)
    await legacy.initialize()
    await legacy.create(reprojection.draft_for("memory", "旧库里的记忆"), TRUSTED)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX memory_v2_one_tool_projection")
        connection.execute("ALTER TABLE memory_v2_records DROP COLUMN projection_source")
        connection.execute("ALTER TABLE memory_v2_records DROP COLUMN projection_target")

    store = SqliteMemoryV2Store(db_path)
    await store.initialize()

    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "旧库里的记忆"]
    assert _index_names(db_path)["memory_v2_one_tool_projection"] is True
    assert _projection_columns(db_path) == [("service", None)]


async def test_legacy_duplicate_tool_rows_degrade_to_a_plain_index(tmp_path) -> None:
    """审查 Corr N1b 回归：撞过 P2-2 竞态的旧库要能启动（降级为普通索引），不丢行。"""
    store, markdown, _service = await build(tmp_path)
    db_path = store.database_path
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX memory_v2_one_tool_projection")
    draft = reprojection.draft_for("memory", "历史重复行")
    for _ in range(2):
        await store.create(draft, TRUSTED, projection_source="tool", projection_target="memory")

    restarted = SqliteMemoryV2Store(db_path)
    await restarted.initialize()

    assert _index_names(db_path)["memory_v2_one_tool_projection"] is False
    assert len(await restarted.list_active(TRUSTED, limit=10)) == 2

    # 审查 Std R3-1：warning 里"跑一次 reproject 就收敛"是假的——文本还在 markdown 里
    # 时一条也清不掉（`tool_projection_digests` 按摘要建字典，两条同文本塌成一条，
    # 差集为空）。钉死这个行为，免得文案再漂回"能自愈"。
    await markdown.add("memory", "历史重复行")
    for _ in range(3):
        await reprojection.reproject_target(restarted, markdown, TRUSTED, "memory")
        assert len(await restarted.list_active(TRUSTED, limit=10)) == 2


async def test_failed_index_rebuild_restores_a_plain_index(tmp_path, monkeypatch) -> None:
    """审查 Corr C5 回归：`DROP` 之后重建失败，必须补回普通索引再抛。

    只捕获 `IntegrityError` 的话，其它失败会让这个库连查询索引都没有——比降级态更差。
    """
    store, _markdown, _service = await build(tmp_path)
    db_path = store.database_path
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX memory_v2_one_tool_projection")
    monkeypatch.setattr(
        store_module, "_TOOL_PROJECTION_INDEX",
        "CREATE UNIQUE INDEX memory_v2_one_tool_projection ON memory_v2_records(no_such_col)")

    with pytest.raises(sqlite3.OperationalError):
        await SqliteMemoryV2Store(db_path).initialize()

    assert _index_names(db_path)["memory_v2_one_tool_projection"] is False


async def test_service_row_with_the_same_text_does_not_duplicate_the_projection(tmp_path) -> None:
    """审查 Corr N2 回归：同文本的服务行在场时，工具写入不得造第二行（设计 §5.3 第 2 条）。"""
    store, markdown, service = await build(tmp_path)
    await service.create(MemoryDraftV2(
        kind=MemoryKind.SEMANTIC, tier=MemoryTier.COLLECTION, scope=MemoryScope.USER_GLOBAL,
        content="用户喜欢喝乌龙茶", payload=SemanticPayload(
            subject="s", fact="用户喜欢喝乌龙茶", category=SemanticCategory.PREFERENCE),
        importance=0.8, strength=0.8, source_type=SourceType.AUTOMATIC,
        source_session_id="s-1", source_event_ids=["e-1"],
        evidence=[EvidenceItem(role="user", excerpt="乌龙茶", hash="0" * 64)],
    ), TRUSTED)

    result = await MemoryTool(markdown, service=service).execute(_MemoryArgs(
        action="add", target="user", content="用户喜欢喝乌龙茶"))

    assert result.ok is True, result
    assert result.data["projection"] == "done"
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "用户喜欢喝乌龙茶"]
    hits = await service.hybrid_search(
        "乌龙茶", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert [hit.record.content for hit in hits] == ["用户喜欢喝乌龙茶"]


async def test_profile_service_row_does_not_suppress_the_projection(tmp_path) -> None:
    """审查 Corr C1 回归：profile 行在 `keyword_search` 的可见域之外，不得抑制工具投影。

    它满足 `scope=USER_GLOBAL` 却被 `tier='collection'` 硬过滤——让它抑制就等于这段
    文本在派生层彻底搜不到，而工具还报 `projection: done`。
    """
    store, markdown, service = await build(tmp_path)
    await service.create(MemoryDraftV2(
        kind=MemoryKind.SEMANTIC, tier=MemoryTier.PROFILE, scope=MemoryScope.USER_GLOBAL,
        content="用户偏好深色主题", payload=SemanticPayload(
            subject="s", fact="用户偏好深色主题", category=SemanticCategory.PREFERENCE),
        importance=0.8, strength=0.8, source_type=SourceType.AUTOMATIC,
        source_session_id="s-1", source_event_ids=["e-1"],
        evidence=[EvidenceItem(role="user", excerpt="深色主题", hash="0" * 64)],
    ), TRUSTED)

    result = await MemoryTool(markdown, service=service).execute(_MemoryArgs(
        action="add", target="user", content="用户偏好深色主题"))

    assert result.ok is True, result
    assert result.data["projection"] == "done"
    assert sorted(record.content for record in await store.list_active(TRUSTED, limit=10)) == [
        "用户偏好深色主题", "用户偏好深色主题"]
    hits = await service.hybrid_search(
        "深色主题", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert [hit.record.content for hit in hits] == ["用户偏好深色主题"]


async def test_retraction_reads_the_other_file_from_disk_not_the_stale_snapshot(tmp_path) -> None:
    """审查 Corr N3 回归：另一进程写进另一个文件的同文本，本会话撤回时不得被误删。"""
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    assert (await tool.execute(_MemoryArgs(
        action="add", target="memory", content="两个进程都写了这句"))).ok is True
    # 另一个进程直接写 USER.md：本进程的快照里没有它。
    other = MarkdownMemoryStore(markdown.directory)
    await other.add("user", "两个进程都写了这句")
    assert markdown.entries("user") == []

    assert (await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="两个进程都写了这句"))).ok is True

    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "两个进程都写了这句"]
    hits = await service.hybrid_search(
        "两个进程", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert len(hits) == 1


async def test_unreadable_other_file_blocks_retraction_instead_of_trusting_the_snapshot(
    tmp_path, monkeypatch,
) -> None:
    """审查 Corr C2 回归：另一个文件读不出来时判"未知"并跳过删除，不得退回本会话快照。

    快照只刷新被写的 target，另一个文件可能还是空列表——拿它当"文件已经空了"会删掉
    另一个进程刚写进去的文本，方向与"宁可少删"相反。
    """
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    assert (await tool.execute(_MemoryArgs(
        action="add", target="memory", content="不可读时的句子"))).ok is True
    other = MarkdownMemoryStore(markdown.directory)
    await other.add("user", "不可读时的句子")
    assert markdown.entries("user") == []  # 本会话快照里没有（另一进程写的）

    user_path = markdown.path_for("user")
    real = MarkdownMemoryStore.read_raw_checked

    def flaky(path):
        return ("", False) if Path(path) == user_path else real(path)

    monkeypatch.setattr(MarkdownMemoryStore, "read_raw_checked", staticmethod(flaky))

    assert (await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="不可读时的句子"))).ok is True
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "不可读时的句子"]

    # 全量对账同理：读不出来就别删。
    assert await reprojection.reproject_target(store, markdown, TRUSTED, "user") == {
        "created": 0, "deleted": 0, "skipped": [], "pending": False, "desired": 0,
        # #1482 Finding 二：「没读到」与「已经一致」必须可区分，CLI 才有非零退出的依据。
        "unreadable": ["user"]}
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "不可读时的句子"]


async def test_reproject_kept_does_not_clear_the_pending_row(tmp_path) -> None:
    """审查 Corr C3 / Std R3-2 回归：`reproject_target` 的 `kept` 也不得清账本。

    与 `retract_entry`、`drain_pending` 同口径——`kept` 只说明"这次别删"。
    """
    store, markdown, _service = await build(tmp_path)
    await store.create(
        reprojection.draft_for("memory", "跨文件条目"), TRUSTED,
        projection_source="tool", projection_target="memory")
    await store.enqueue_projection(TRUSTED, "memory", "跨文件条目")
    # 文本只还在 USER.md 里：memory 侧对账会判 `kept`（不是"已经一致"）。
    await markdown.add("user", "跨文件条目")

    summary = await reprojection.reproject_target(store, markdown, TRUSTED, "memory")

    assert summary["deleted"] == 0, summary
    assert [row[0] for row in _pending_rows(store.database_path)] == ["memory"]


async def test_kept_retraction_keeps_the_pending_row(tmp_path, monkeypatch) -> None:
    """审查 Corr N4 回归：`kept` 只说明"这次别删"，不得清掉仍待收敛的账本行。"""
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    monkeypatch.setattr(store, "create", _flaky_create(store, {"fail": True}))
    assert (await tool.execute(_MemoryArgs(
        action="add", target="memory", content="双文件条目"))).ok is True
    monkeypatch.undo()
    assert [row[0] for row in _pending_rows(store.database_path)] == ["memory"]
    # 同文本也出现在 USER.md（另一个进程写的，本进程快照里没有）。
    other = MarkdownMemoryStore(markdown.directory)
    await other.add("user", "双文件条目")

    assert (await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="双文件条目"))).ok is True

    assert [row[0] for row in _pending_rows(store.database_path)] == ["memory"]


async def test_reproject_on_either_target_reclaims_a_shared_row(tmp_path) -> None:
    """审查 Corr N5 回归：共享行从两个文件都消失后，任一 target 的全量对账都能回收它。"""
    store, markdown, service = await build(tmp_path)
    markdown.path_for("memory").write_text("共享行", encoding="utf-8")
    markdown.path_for("user").write_text("共享行", encoding="utf-8")
    markdown.load_from_disk()
    await service.reproject(TRUSTED, "memory")
    assert len(await store.list_active(TRUSTED, limit=10)) == 1

    markdown.path_for("memory").write_text("", encoding="utf-8")
    markdown.path_for("user").write_text("", encoding="utf-8")
    markdown.load_from_disk()

    summary = await service.reproject(TRUSTED, "user")

    assert summary["deleted"] == 1, summary
    assert await store.list_active(TRUSTED, limit=10) == []


# --------------------------------- #1482 Finding 一：撤回判不出时留账本、不谎报 done


def _unreadable(path: Path):
    """把指定文件钉成"存在但读不出来"（`disk_entries` 因此返回 ``None``）。"""
    real = MarkdownMemoryStore.read_raw_checked

    def flaky(candidate):
        return ("", False) if Path(candidate) == Path(path) else real(candidate)

    return staticmethod(flaky)


async def _seed_shared_entry_with_unreadable_user(tmp_path, monkeypatch):
    """造法同 :746：另一进程把同一句写进 USER.md，再把 USER.md 钉成不可读。"""
    store, markdown, service = await build(tmp_path)
    tool = MemoryTool(markdown, service=service)
    assert (await tool.execute(_MemoryArgs(
        action="add", target="memory", content="瞬时不可读句"))).ok is True
    other = MarkdownMemoryStore(markdown.directory)
    await other.add("user", "瞬时不可读句")
    monkeypatch.setattr(
        MarkdownMemoryStore, "read_raw_checked",
        _unreadable(markdown.path_for("user")),
    )
    return store, markdown, service, tool


async def test_unreadable_other_file_leaves_a_ledger_row_and_reports_pending(
    tmp_path, monkeypatch,
) -> None:
    """#1482 AC1：判不出（另一个文件读不出来）必须留账本并回执 pending，不许冒充 kept/done。

    (a) 派生行仍在（不误删，真 `kept` 语义不回归）；(b) 直查账本表见到
    `UnreadableMarkdown` / `attempts == 1`（公开 API 的 SELECT 不含 `last_error`，不能拿它断）；
    (c) 工具回执 `projection == "pending"`。
    """
    store, _markdown, _service, tool = await _seed_shared_entry_with_unreadable_user(
        tmp_path, monkeypatch)

    result = await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="瞬时不可读句"))

    assert result.ok is True, result
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "瞬时不可读句"]
    rows = _pending_rows(store.database_path)
    assert len(rows) == 1, rows
    target, digest, content, attempts, last_error = rows[0]
    assert (target, content, digest) == ("memory", "瞬时不可读句", entry_digest("瞬时不可读句"))
    assert last_error == "UnreadableMarkdown"
    assert attempts == 1
    assert result.data["projection"] == "pending"


async def test_recovered_file_converges_the_orphan_on_the_next_drain(tmp_path, monkeypatch) -> None:
    """#1482 AC2：文件恢复可读后，一次成功的写入触发既有 drain ⇒ 派生行删除、账本清空。"""
    store, markdown, service, tool = await _seed_shared_entry_with_unreadable_user(
        tmp_path, monkeypatch)
    assert (await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="瞬时不可读句"))).ok is True
    assert len(_pending_rows(store.database_path)) == 1

    monkeypatch.undo()  # USER.md 恢复可读；它里面那句此刻也已被另一进程删掉
    MarkdownMemoryStore(markdown.directory).path_for("user").write_text("", encoding="utf-8")
    markdown.load_from_disk()

    assert (await tool.execute(_MemoryArgs(
        action="add", target="memory", content="另一句"))).ok is True

    assert _pending_rows(store.database_path) == []
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == ["另一句"]
    hits = await service.hybrid_search(
        "瞬时不可读句", TRUSTED, scopes=[MemoryScope.USER_GLOBAL], limit=5,
    )
    assert hits == []


async def test_repeated_drain_against_an_unreadable_file_keeps_the_ledger_row(
    tmp_path, monkeypatch,
) -> None:
    """#1482 AC2b：文件持续读不出来时连跑两次 drain ⇒ 账本行仍在、attempts 递增、不抛异常。

    remove 自己那次撤回已经记过一笔（attempts=1），所以两轮 drain 之后是 3——票面的
    「递增到 2」是按账本从 0 起算；这里按实际读数断言**每轮 +1**。
    """
    store, markdown, _service, tool = await _seed_shared_entry_with_unreadable_user(
        tmp_path, monkeypatch)
    assert (await tool.execute(_MemoryArgs(
        action="remove", target="memory", old_text="瞬时不可读句"))).ok is True
    assert _pending_rows(store.database_path)[0][3] == 1

    first = await reprojection.drain_pending(store, markdown, TRUSTED)
    assert _pending_rows(store.database_path)[0][3] == 2
    second = await reprojection.drain_pending(store, markdown, TRUSTED)

    assert first["remaining"] == 1, first
    assert second["remaining"] == 1, second
    rows = _pending_rows(store.database_path)
    assert len(rows) == 1, rows
    assert rows[0][0] == "memory"
    assert rows[0][3] == 3  # 撤回 1 次 + 两次 drain 各 1 次；行仍在
    assert rows[0][4] == "UnreadableMarkdown"
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "瞬时不可读句"]


async def test_reproject_on_a_readable_target_keeps_the_ledger_when_the_other_file_is_unreadable(
    tmp_path, monkeypatch,
) -> None:
    """#1482 F1：**目标文件可读**、另一文件读不出来时，`reproject_target` 也不得清账本。

    既有用例（`:746`）走的是目标文件本身不可读的早退分支，碰不到删行循环里的清账本判据。
    这里让 memory 侧可读、USER.md 不可读，并对一个 memory target 的孤儿行做全量对账：
    判不出「另一个文件还有没有」就不能删行、不能清账（否则文件恢复后 drain 无行可做，
    孤儿派生行永久存活）。
    """
    store, markdown, _service = await build(tmp_path)
    await store.create(
        reprojection.draft_for("memory", "孤儿句"), TRUSTED,
        projection_source="tool", projection_target="memory")
    await store.enqueue_projection(TRUSTED, "memory", "孤儿句")
    monkeypatch.setattr(
        MarkdownMemoryStore, "read_raw_checked",
        _unreadable(markdown.path_for("user")),
    )

    summary = await reprojection.reproject_target(store, markdown, TRUSTED, "memory")

    assert summary["deleted"] == 0, summary
    assert summary["unreadable"] == ["user"], summary  # 归因给真正读不出的文件
    rows = _pending_rows(store.database_path)
    assert len(rows) == 1, rows
    assert (rows[0][0], rows[0][1], rows[0][4]) == (
        "memory", entry_digest("孤儿句"), "UnreadableMarkdown")
    assert [record.content for record in await store.list_active(TRUSTED, limit=10)] == [
        "孤儿句"]
