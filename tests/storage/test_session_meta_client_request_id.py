"""SessionMeta.client_request_id —— #1333 认领关联值的持久化与精确查询。

票面（2026-10-20 已批准方案）：
- 后端把 `client_request_id` 持久化到 SQLite `SessionMeta` 索引并提供索引查询；
- 存量库迁移后旧行保持 NULL；
- 关联查询只返回请求值**精确匹配**的会话（多条精确匹配由调用方 fail-closed）；
- 普通列表响应不暴露该字段（那是 web 层的 AC，见 tests/web/test_1333_client_request_id_claim.py）。

关键设计事实：`SessionMeta` 行历来是**惰性**建的（首次 checkpoint / fork /
lineage / 归档才补）——认领查询等不了那么久，所以创建路径必须**主动**落行
（由 `SessionService.create_and_launch` 负责；本文件只锁 store 契约）。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from agent_harness.storage import SessionMeta, SqliteSessionMetaStore


def _meta(session_id: str, **overrides: object) -> SessionMeta:
    defaults: dict[str, object] = {
        "session_id": session_id,
        "created_at": f"2026-10-25T00:00:0{session_id[-1]}+00:00",
    }
    defaults.update(overrides)
    return SessionMeta.model_validate(defaults)


@pytest.mark.asyncio
async def test_client_request_id_roundtrip_and_exact_query(tmp_path: Path) -> None:
    store = SqliteSessionMetaStore(tmp_path / "state.db")
    await store.initialize()

    await store.upsert(_meta("s-a", client_request_id="cid-1"))
    await store.upsert(_meta("s-b", client_request_id="cid-2"))
    await store.upsert(_meta("s-c"))  # 无关联值（NULL）

    hits = await store.get_by_client_request_id("cid-1")
    assert [m.session_id for m in hits] == ["s-a"]
    assert hits[0].client_request_id == "cid-1"

    # 精确匹配：前缀/子串都不命中（SQL = 全等，不是 LIKE）
    assert await store.get_by_client_request_id("cid") == []
    assert await store.get_by_client_request_id("cid-10") == []
    # NULL 行永不命中（SQL 语义；也不允许把 NULL 行当通配）
    assert await store.get_by_client_request_id("missing") == []


@pytest.mark.asyncio
async def test_duplicate_client_request_id_rows_are_all_returned(tmp_path: Path) -> None:
    """同一关联值多条行 = fail-closed 的数据前提：store 原样全量返回，
    「不任意挑选」的裁决由调用方执行。"""
    store = SqliteSessionMetaStore(tmp_path / "state.db")
    await store.initialize()

    await store.upsert(_meta("s-1", client_request_id="dup"))
    await store.upsert(_meta("s-2", client_request_id="dup"))

    hits = await store.get_by_client_request_id("dup")
    assert [m.session_id for m in hits] == ["s-1", "s-2"]


@pytest.mark.asyncio
async def test_upsert_conflict_preserves_client_request_id(tmp_path: Path) -> None:
    """client_request_id 是**创建时一次性**事实（与 `project_key` 同一写法）：
    既有调用方的不带该字段的全行 upsert（runtime 首次 checkpoint 惰性补建、
    archive 懒补）不得把它抹成 NULL。"""
    store = SqliteSessionMetaStore(tmp_path / "state.db")
    await store.initialize()

    await store.upsert(_meta("s-1", client_request_id="cid-keep"))
    # 模拟 runtime 的惰性补建：不带 client_request_id 的全行 upsert
    await store.upsert(_meta("s-1", agent_id="default", last_checkpoint_seq=7))

    row = await store.get("s-1")
    assert row is not None
    assert row.client_request_id == "cid-keep"
    assert row.last_checkpoint_seq == 7


@pytest.mark.asyncio
async def test_upsert_conflict_with_different_cid_keeps_first_write(tmp_path: Path) -> None:
    """write-once 的另一半：同 session_id 二次 upsert **带不同 cid** 也保持首个——
    ON CONFLICT UPDATE SET 不含该列，不是「None 才保留」。"""
    store = SqliteSessionMetaStore(tmp_path / "state.db")
    await store.initialize()

    await store.upsert(_meta("s-1", client_request_id="cid-first"))
    await store.upsert(_meta("s-1", client_request_id="cid-second"))

    row = await store.get("s-1")
    assert row is not None
    assert row.client_request_id == "cid-first"


@pytest.mark.asyncio
async def test_old_schema_migration_adds_column_and_index(tmp_path: Path) -> None:
    """存量库迁移：旧 schema（无 client_request_id 列）的库经 initialize 后
    补列（旧行 NULL）+ 建索引；查询照常工作。"""
    database_path = tmp_path / "legacy.db"
    old_ddl = """
    CREATE TABLE IF NOT EXISTS session_meta (
        session_id          TEXT PRIMARY KEY,
        created_at          TEXT NOT NULL,
        agent_id            TEXT,
        last_checkpoint_seq INTEGER,
        archived            BOOLEAN DEFAULT 0,
        parent_session_id   TEXT,
        origin              TEXT,
        fork_point_seq      INTEGER,
        project_key         TEXT
    )
    """
    async with aiosqlite.connect(database_path) as connection:
        await connection.execute(old_ddl)
        await connection.execute(
            "INSERT INTO session_meta (session_id, created_at) VALUES ('legacy-1', '2026-01-01T00:00:00+00:00')"
        )
        await connection.commit()

    store = SqliteSessionMetaStore(database_path)
    await store.initialize()

    async with aiosqlite.connect(database_path) as connection:
        cursor = await connection.execute("PRAGMA table_info(session_meta)")
        columns = {row[1] for row in await cursor.fetchall()}
        idx = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='session_meta'"
        )
        index_names = {row[0] for row in await idx.fetchall()}

    assert "client_request_id" in columns
    assert "idx_session_meta_client_request_id" in index_names

    legacy = await store.get("legacy-1")
    assert legacy is not None
    assert legacy.client_request_id is None
    # 迁移后的库上查询照常工作（旧行 NULL 不命中；新行命中）
    assert await store.get_by_client_request_id("anything") == []
    await store.upsert(_meta("new-1", client_request_id="cid-new"))
    hits = await store.get_by_client_request_id("cid-new")
    assert [m.session_id for m in hits] == ["new-1"]
