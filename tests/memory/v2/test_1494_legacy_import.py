"""#1494 AC3/AC4：旧 SQLite 五类操作状态的一次性导入与 completed-but-unpublished 补发布。

升级路径必须做到三件事（ADR-0058 D9）：

1. 旧表**只读**，五类状态逐条写成文件（settings / tombstones / staged / blocked / jobs）；
2. 旧 `completed + committed` job 的 **ACTIVE** 记录按内容补发布（`legp_` 回执）——
   记录早已提交、markdown 因上次进程被杀而没落，这是 #1494 AC3 要求纳入对账的存量窗口；
3. 标记最后写、每步幂等：导入到一半被杀只会让下次启动补完，不会重复导入。

反控：已存在的文件不被覆盖（"先写的那份就是证据"）；没有旧库时不写标记（数据目录可能
在之后的启动里才出现）；`memory_v2_jobs` 表只被读取，导入后运行时仍只读 job 文件。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_harness.memory.v2 import assembly
from agent_harness.memory.v2.commit import build_markdown_commit
from agent_harness.memory.v2.file_state import MemoryStateRoot, read_json
from agent_harness.memory.v2.jobs import MemoryV2JobStore, job_id_for
from agent_harness.memory.v2.legacy_import import (
    _flag_needs_reconcile,
    import_legacy_state,
)
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.receipts import ReceiptState, ReceiptStore
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.memory.v2.write_approval import (
    BLOCKED,
    StagedWriteStore,
    _identity,
    blocked_id_for,
)
from tests.memory.v2._records import make_draft

LOCAL = TrustedMemoryIdentity(tenant_id="local", user_id="local")
NOW = datetime.now(UTC)
CONTENT_ACTIVE = "旧 job 已提交但未发布"
CONTENT_INVALIDATED = "旧 job 里已失效的版本"

#: 只有这张表在 #1494 之后不再由当前 schema 创建；其余旧表仍被创建（兼容旧库），
#: 但运行时不再读写它们。
_LEGACY_JOBS_DDL = """
CREATE TABLE memory_v2_jobs (
    job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, project_id TEXT,
    session_id TEXT NOT NULL, run_id TEXT,
    protected_fact_token_budget INTEGER,
    protected_fact_extraction_state TEXT NOT NULL DEFAULT 'pending',
    protected_fact_extraction_candidates TEXT,
    stage TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT, reason TEXT,
    lease_owner TEXT, lease_expires_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""


def _insert(database: Path, table: str, row: dict) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()))
        connection.commit()


def _seed_legacy(database: Path) -> None:
    """建旧 job 表并写入四类操作状态；records / jobs 由下面两个 helper 补。"""
    with sqlite3.connect(database) as connection:
        connection.executescript(_LEGACY_JOBS_DDL)
        connection.commit()

    _insert(database, "memory_v2_settings", {
        "tenant_id": LOCAL.tenant_id, "user_id": LOCAL.user_id,
        "extraction_enabled": 1, "recall_enabled": 1,
        "fallback_enabled": 0, "write_approval": 1,
        "updated_at": NOW.isoformat(),
    })
    _insert(database, "memory_v2_tombstones", {
        "memory_id": "mem-legacy-deleted", "root_id": "mem-legacy-deleted",
        "tenant_id": LOCAL.tenant_id, "user_id": LOCAL.user_id,
        "scope": "user_global", "project_id": None, "version": 1,
        "deleted_at": (NOW - timedelta(days=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=29)).isoformat(),
        "deletion_reason": "user_request",
        "content_hashes": json.dumps(["hash-content"]),
        "source_hashes": json.dumps(["hash-source"]),
    })
    _insert(database, "memory_v2_staged", {
        "id": "stg_legacy", "identity": _identity(LOCAL),
        "payload": json.dumps(
            {"target": "memory", "action": "add", "content": "旧待审写入"},
            ensure_ascii=False),
        "pinned_entry": None, "status": "pending",
        "created_at": NOW.isoformat(), "claimed_at": None,
    })
    _insert(database, "memory_v2_blocked", {
        "id": "blk_legacy_row", "job_id": "legacy-job-blocked",
        "identity": _identity(LOCAL), "project_id": None,
        "origin": "background_review", "reason": "write_approval_blocked",
        "payload": json.dumps({"actions": []}), "content_hash": "hash-blocked",
        "status": "blocked", "disposition_note": None,
        "created_at": NOW.isoformat(), "disposed_at": None,
    })


async def _seed_records(database: Path) -> tuple[str, str]:
    store = SqliteMemoryV2Store(database)
    await store.initialize()
    active = await store.create(
        make_draft(content=CONTENT_ACTIVE, source_event_ids=["legacy-event-a"]), LOCAL)
    invalidated = await store.create(
        make_draft(content=CONTENT_INVALIDATED, source_event_ids=["legacy-event-b"]), LOCAL)
    await store.invalidate(invalidated.id, LOCAL)
    return active.id, invalidated.id


def _seed_legacy_jobs(database: Path, active_id: str, invalidated_id: str) -> None:
    for legacy_job, key, memory_id in (
        ("legacy-job-active", "legacy-key-active", active_id),
        ("legacy-job-invalidated", "legacy-key-invalidated", invalidated_id),
    ):
        _insert(database, "memory_v2_jobs", {
            "job_id": legacy_job, "idempotency_key": key,
            "tenant_id": LOCAL.tenant_id, "user_id": LOCAL.user_id, "project_id": None,
            "session_id": "legacy-session", "run_id": "legacy-run",
            "protected_fact_token_budget": None,
            "protected_fact_extraction_state": "pending",
            "protected_fact_extraction_candidates": None,
            "stage": "completed", "outcome": "committed",
            "state": json.dumps({"memory_ids": [memory_id]}),
            "reason": None, "lease_owner": None, "lease_expires_at": None,
            "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
        })


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.database = root / assembly.MEMORY_V2_DATABASE_NAME
        self.state = MemoryStateRoot(root / "memory-state")


async def _workspace(tmp_path: Path, *, legacy: bool = True) -> Workspace:
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    store = SqliteMemoryV2Store(workspace.database, state_root=workspace.state)
    await store.initialize()
    if legacy:
        _seed_legacy(workspace.database)
        active, invalidated = await _seed_records(workspace.database)
        _seed_legacy_jobs(workspace.database, active, invalidated)
    return workspace


async def _import(workspace: Workspace) -> dict:
    store = SqliteMemoryV2Store(workspace.database, state_root=workspace.state)
    await store.initialize()
    engine = MarkdownMemoryStore(workspace.root / "memories")
    engine.load_from_disk()
    commit = build_markdown_commit(engine, workspace.root)
    return await import_legacy_state(store, commit)


# --------------------------------------------------------------------------------------
# 五类导入 + 补发布
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_state_is_imported_once_and_unpublished_content_is_republished(
    tmp_path: Path,
) -> None:
    workspace = await _workspace(tmp_path)

    counts = await _import(workspace)

    assert counts["skipped"] is False
    assert (counts["settings"], counts["tombstones"], counts["staged"],
            counts["blocked"], counts["jobs"]) == (1, 1, 1, 1, 2)
    assert counts["republished"] == 1, "只有 ACTIVE 记录需要补发布"

    # settings：四个键全部按旧表的显式值导入（fallback 的 0 不能被默认值吞掉）
    settings = read_json(workspace.state.settings_path)
    assert settings is not None
    assert settings["values"] == {
        "extraction_enabled": True, "recall_enabled": True,
        "fallback_enabled": False, "write_approval": True,
    }

    # tombstone：内容无关的删除标记原样落到文件
    tombstone = read_json(workspace.state.tombstones_dir / "mem-legacy-deleted.json")
    assert tombstone is not None
    assert (tombstone["root_id"], tombstone["content_hashes"]) == (
        "mem-legacy-deleted", ["hash-content"])

    # staged：重启后的 StagedWriteStore 能看到这条旧待审写入
    store = SqliteMemoryV2Store(workspace.database, state_root=workspace.state)
    staged = StagedWriteStore(workspace.state, build_markdown_commit(
        MarkdownMemoryStore(workspace.root / "memories"), workspace.root), store=store)
    pending = await staged.pending(LOCAL)
    assert [(item.id, item.payload["content"]) for item in pending] == [
        ("stg_legacy", "旧待审写入")]

    # blocked：确定性 id + 旧行 id 保留为 legacy_id
    blocked_id = blocked_id_for(_identity(LOCAL), "hash-blocked")
    blocked = read_json(workspace.state.blocked_dir / f"{blocked_id}.json")
    assert blocked is not None
    assert (blocked["legacy_id"], blocked["status"]) == ("blk_legacy_row", BLOCKED)

    # jobs：旧 job id 不再使用，文件按幂等键的确定性 id 落盘
    job_path = workspace.state.jobs_dir / f"{job_id_for('legacy-key-active')}.json"
    job = read_json(job_path)
    assert job is not None
    assert (job["legacy_job_id"], job["stage"], job["outcome"]) == (
        "legacy-job-active", "completed", "committed")

    # 补发布：ACTIVE 内容进权威面，INVALIDATED 不进；回执前缀 legp_ 可对账
    memory_file = workspace.root / "memories" / "MEMORY.md"
    text = memory_file.read_text(encoding="utf-8")
    assert CONTENT_ACTIVE in text
    assert CONTENT_INVALIDATED not in text, "已失效的旧版本不得被补发布会权威面"
    receipts = [
        read_json(path) for path in workspace.state.receipts_dir.glob("*.json")
    ]
    [legacy_receipt] = [item for item in receipts if item["operation_id"].startswith("legp_")]
    assert legacy_receipt["state"] == "COMMITTED"

    # 标记 + 第二次导入完全跳过：不覆盖、不重复
    marker = read_json(workspace.state.legacy_marker_path)
    assert marker is not None and marker["counts"]["jobs"] == 2
    files_before = sorted(
        str(path.relative_to(workspace.state.base))
        for path in workspace.state.base.rglob("*.json"))
    again = await _import(workspace)
    assert again == {"skipped": True, "reason": "already_imported"}
    assert sorted(
        str(path.relative_to(workspace.state.base))
        for path in workspace.state.base.rglob("*.json")) == files_before


@pytest.mark.asyncio
async def test_existing_state_files_are_never_overwritten(tmp_path: Path) -> None:
    """导入到一半被杀后重跑：先写的那份文件就是证据，不能被旧表覆盖。"""
    workspace = await _workspace(tmp_path)
    existing = workspace.state.tombstones_dir / "mem-legacy-deleted.json"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(
        json.dumps({"schema": 1, "memory_id": "mem-legacy-deleted", "note": "先写的那份"}),
        encoding="utf-8")

    counts = await _import(workspace)

    assert counts["tombstones"] == 0
    assert read_json(existing)["note"] == "先写的那份"
    # 其余类仍被导入，标记最后写 ⇒ 下一次启动不会重做
    assert counts["jobs"] == 2
    assert read_json(workspace.state.legacy_marker_path) is not None


@pytest.mark.asyncio
async def test_missing_legacy_database_writes_no_marker(tmp_path: Path) -> None:
    workspace = await _workspace(tmp_path, legacy=False)
    workspace.database.unlink()

    result = await import_legacy_state(
        SqliteMemoryV2Store(workspace.database, state_root=workspace.state),
        build_markdown_commit(MarkdownMemoryStore(workspace.root / "memories"), workspace.root),
    )

    assert result == {"skipped": True, "reason": "no_legacy_database"}
    assert not workspace.state.legacy_marker_path.exists()


@pytest.mark.asyncio
async def test_jobs_are_read_from_files_after_import(tmp_path: Path) -> None:
    """导入完成后运行时只认 job 文件：旧表停在原地，新的 claim 来自文件。"""
    workspace = await _workspace(tmp_path)
    await _import(workspace)

    jobs = MemoryV2JobStore(workspace.database, workspace.state)
    await jobs.initialize()
    recovered = await jobs.get(job_id_for("legacy-key-active"))
    assert recovered.idempotency_key == "legacy-key-active"
    assert recovered.stage.value == "completed", "导入的是终态 job，不会被重新执行"
    assert await jobs.list_recoverable() == [], "终态 job 不会进入恢复扫描的待办"

    with sqlite3.connect(workspace.database) as connection:
        legacy_rows = connection.execute(
            "SELECT COUNT(*) FROM memory_v2_jobs").fetchone()[0]
    assert legacy_rows == 2, "导入只读旧表，不删除（销毁未授权）"


@pytest.mark.asyncio
async def test_settings_conflict_is_not_folded_and_blocks_the_marker(tmp_path: Path) -> None:
    """D9：多行设置不一致时不导入、不折叠，落冲突产物并保持未收敛。"""
    workspace = await _workspace(tmp_path)
    _insert(workspace.database, "memory_v2_settings", {
        "tenant_id": "other", "user_id": "other",
        "extraction_enabled": 0, "recall_enabled": 1,
        "fallback_enabled": 0, "write_approval": 1,
        "updated_at": (NOW - timedelta(days=1)).isoformat(),
    })

    counts = await _import(workspace)

    assert counts["settings"] == 0
    assert counts["settings_conflicts"] == 1
    assert counts["converged"] is False
    assert not workspace.state.settings_path.exists(), "冲突时不得凭猜测写一份设置"
    conflict = read_json(workspace.state.base / "settings-conflict.json")
    assert conflict is not None
    assert conflict["distinct_values"]["extraction_enabled"] == [False, True]
    assert not workspace.state.legacy_marker_path.exists(), "未收敛不得写完成标记"


@pytest.mark.asyncio
async def test_existing_settings_file_wins_over_legacy_rows(tmp_path: Path) -> None:
    """崩溃重跑/用户已在窗口内改过：已有 settings.json 不被旧表覆盖。"""
    workspace = await _workspace(tmp_path)
    workspace.state.ensure()
    workspace.state.settings_path.write_text(
        json.dumps({"schema": 1, "values": {"extraction_enabled": False}}),
        encoding="utf-8")

    counts = await _import(workspace)

    assert counts["settings"] == 0 and counts["settings_existing"] == 1
    settings = read_json(workspace.state.settings_path)
    assert settings["values"] == {"extraction_enabled": False}
    assert workspace.state.legacy_marker_path.exists()


@pytest.mark.asyncio
async def test_corrupt_existing_settings_file_blocks_marker_instead_of_overwriting(
    tmp_path: Path,
) -> None:
    """review P3-3：坏 settings 不是"已存在可跳过"，不得写 marker 把下次启动也挡掉。"""
    workspace = await _workspace(tmp_path)
    workspace.state.ensure()
    workspace.state.settings_path.write_text("{not json", encoding="utf-8")

    counts = await _import(workspace)

    assert counts["settings"] == 0
    assert counts["settings_existing"] == 0
    assert counts["settings_unreadable"] == 1
    assert counts["converged"] is False
    assert workspace.state.settings_path.read_text(
        encoding="utf-8") == "{not json", "坏文件是证据，不得覆盖"
    assert not workspace.state.legacy_marker_path.exists()


@pytest.mark.asyncio
async def test_missing_legacy_record_becomes_needs_reconcile_and_blocks_marker(
    tmp_path: Path,
) -> None:
    """D9：旧 job 指向的记录已不可查 ⇒ NEED_RECONCILE，不编造内容、不写完成标记。"""
    workspace = await _workspace(tmp_path)
    with sqlite3.connect(workspace.database) as connection:
        connection.execute("DELETE FROM memory_v2_jobs")
        connection.commit()
    _seed_legacy_jobs(workspace.database, "mem-does-not-exist", "mem-never-existed")

    counts = await _import(workspace)

    assert counts["republish_needs_reconcile"] == 2
    assert counts["converged"] is False
    assert not workspace.state.legacy_marker_path.exists()
    receipts = [
        read_json(path) for path in workspace.state.receipts_dir.glob("legp_*.json")
    ]
    assert len(receipts) == 2
    assert {item["state"] for item in receipts} == {"NEED_RECONCILE"}


@pytest.mark.asyncio
async def test_expired_legacy_tombstone_is_not_imported(tmp_path: Path) -> None:
    """D9：过期墓碑按既有保留策略失效，导入它等于凭空续期删除抑制。"""
    workspace = await _workspace(tmp_path)
    _insert(workspace.database, "memory_v2_tombstones", {
        "memory_id": "mem-expired", "root_id": "mem-expired",
        "tenant_id": LOCAL.tenant_id, "user_id": LOCAL.user_id,
        "scope": "user_global", "project_id": None, "version": 1,
        "deleted_at": (NOW - timedelta(days=40)).isoformat(),
        "expires_at": (NOW - timedelta(days=10)).isoformat(),
        "deletion_reason": "user_request",
        "content_hashes": json.dumps(["hash-expired"]),
        "source_hashes": json.dumps([]),
    })

    counts = await _import(workspace)

    assert counts["tombstones_expired"] == 1
    assert not (workspace.state.tombstones_dir / "mem-expired.json").exists()


@pytest.mark.asyncio
async def test_flag_needs_reconcile_takes_the_operation_lock_and_writes_a_valid_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review P4/R11-P4-2：同一把锁、落盘形状可读，且阻塞锁不跑在事件循环线程上。"""
    receipts = ReceiptStore(MemoryStateRoot(tmp_path / "memory-state"))
    entered: list[str] = []
    lock_threads: list[threading.Thread] = []
    inside = {"locked": False}
    real_lock = receipts.lock

    @contextmanager
    def _tracked(operation_id: str):
        entered.append(operation_id)
        lock_threads.append(threading.current_thread())
        with real_lock(operation_id):
            inside["locked"] = True
            try:
                yield
            finally:
                inside["locked"] = False

    real_flag = receipts.flag_needs_reconcile

    def _flag_inside(*args, **kwargs):
        assert inside["locked"] is True, "receipt write must happen inside the op lock"
        return real_flag(*args, **kwargs)

    monkeypatch.setattr(receipts, "lock", _tracked)
    monkeypatch.setattr(receipts, "flag_needs_reconcile", _flag_inside)

    await _flag_needs_reconcile(
        receipts, "legp_lock", "legacy record is missing", memory_id="mem-lock")

    assert entered == ["legp_lock"]
    assert lock_threads and lock_threads[0] is not threading.main_thread()
    receipt = receipts.load("legp_lock")
    assert receipt is not None and receipt.state == ReceiptState.NEED_RECONCILE
    assert receipt.targets == {} and receipt.operations == []
    assert "mem-lock" in (receipt.reason or "")


@pytest.mark.asyncio
async def test_flag_needs_reconcile_writes_the_marker_in_one_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review R11-P4-1/R12：标记只落盘一次；prepare 的中间态会让 kill 后 reconcile 误判成功。"""
    receipts = ReceiptStore(MemoryStateRoot(tmp_path / "memory-state"))
    writes: list[str] = []
    real_write = receipts._write

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("legacy marker must not go through prepare")

    def _counted(receipt):
        writes.append(receipt.state)
        return real_write(receipt)

    monkeypatch.setattr(receipts, "prepare", _forbidden)
    monkeypatch.setattr(receipts, "_write", _counted)

    await _flag_needs_reconcile(
        receipts, "legp_atomic", "legacy record is missing", memory_id="mem-atomic")

    assert writes == [ReceiptState.NEED_RECONCILE]
    receipt = receipts.load("legp_atomic")
    assert receipt is not None and receipt.state == ReceiptState.NEED_RECONCILE
    assert receipt.origin == "legacy_import"


@pytest.mark.asyncio
async def test_flag_needs_reconcile_advances_prepared_but_keeps_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review R12：已有 PREPARED 推进到对账；COMMITTED 绝不回退（终态证据保留）。"""
    receipts = ReceiptStore(MemoryStateRoot(tmp_path / "memory-state"))
    receipts.prepare(
        "legp_prepared", origin="legacy_import", targets=[], operations=[])
    receipts.prepare(
        "legp_committed", origin="legacy_import", targets=[], operations=[])
    receipts.finish("legp_committed")

    await _flag_needs_reconcile(
        receipts, "legp_prepared", "legacy record is missing", memory_id="mem-1")
    await _flag_needs_reconcile(
        receipts, "legp_committed", "legacy record is missing", memory_id="mem-2")

    prepared = receipts.load("legp_prepared")
    assert prepared is not None and prepared.state == ReceiptState.NEED_RECONCILE
    committed = receipts.load("legp_committed")
    assert committed is not None and committed.state == ReceiptState.COMMITTED
    assert committed.reason is None
