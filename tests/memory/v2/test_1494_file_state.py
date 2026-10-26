"""#1494 AC2：五类操作状态的非 SQLite owner、重启读回、SQLite 只剩派生态。

AC2 的判据是"每个操作状态 owner 有非 SQLite 持久实现与重启验证"。这里逐个 owner
做**同一形状**的验证：写入 → 文件在 `memory-state/` 下 → **换一个全新的装配实例**
（模拟重启）→ 状态仍可读回且语义不变。

同一组用例还钉住 ADR-0056 §3 的边界：旧 SQLite 操作状态表即使还存在（兼容旧库），
新写入**一行都不落**；`memory_v2_jobs` 表则根本不再创建。records / outbox / FTS
仍是 SQLite——它们是可重建的派生态，不是操作状态。
"""

from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest
import pytest_asyncio

from agent_harness.config import Settings
from agent_harness.memory.v2 import assembly
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.file_state import (
    CorruptStateFile,
    atomic_write_text,
    read_json,
    safe_id,
)
from agent_harness.memory.v2.jobs import MemoryV2JobStore, job_id_for
from agent_harness.memory.v2.markdown_store import ENTRY_DELIMITER
from agent_harness.memory.v2.receipts import ReceiptState, ReceiptStore
from agent_harness.memory.v2.tombstones import FileTombstoneStore
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from tests.memory.v2._records import make_draft

USER_A = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a")
USER_B = TrustedMemoryIdentity(tenant_id="tenant-b", user_id="user-b")


# --------------------------------------------------------------------------------------
# 环境
# --------------------------------------------------------------------------------------


class Workspace:
    def __init__(self, settings: Settings, service: MemoryV2Service) -> None:
        self.settings = settings
        self.service = service
        self.root = service.state_root
        self.database = Path(settings.workspace_dir) / assembly.MEMORY_V2_DATABASE_NAME

    async def restart(self) -> MemoryV2Service:
        """同一工作目录重新装配：所有状态只能来自磁盘。"""
        return await assembly.build_memory_v2_service(self.settings)

    @property
    def memory_file(self) -> Path:
        return Path(self.settings.workspace_dir) / "memories" / "MEMORY.md"


@pytest_asyncio.fixture
async def workspace(tmp_path: Path) -> Workspace:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path / "workspace"), model_api_key="sk-test")
    return Workspace(settings, await assembly.build_memory_v2_service(settings))


def _table_rows(database: Path, table: str) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _has_table(database: Path, table: str) -> bool:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    finally:
        connection.close()


# --------------------------------------------------------------------------------------
# 文件原语：坏文件不静默当空、id 不越出文件名
# --------------------------------------------------------------------------------------


def test_corrupt_state_file_is_an_error_not_an_empty_object(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptStateFile):
        read_json(path)
    assert read_json(tmp_path / "missing.json") is None


def test_state_file_ids_cannot_escape_the_state_directory() -> None:
    assert safe_id("mem_1.2-3") == "mem_1.2-3"
    for bad in ("../escape", "a/b", "a\\b", "", ".."):
        with pytest.raises(ValueError):
            safe_id(bad, what="memory id")


def test_atomic_write_leaves_no_temporary_files(tmp_path: Path) -> None:
    target = tmp_path / "state" / "one.json"
    atomic_write_text(target, '{"a": 1}\n')
    atomic_write_text(target, '{"a": 2}\n')
    assert read_json(target) == {"a": 2}
    assert [path.name for path in target.parent.iterdir()] == ["one.json"]


# --------------------------------------------------------------------------------------
# settings：单文件、只存显式键、无身份路由
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_owner_is_one_file_with_only_explicit_keys(workspace: Workspace) -> None:
    await workspace.service.update_settings(USER_A, write_approval=True)

    payload = read_json(workspace.root.settings_path)
    assert payload is not None
    assert payload["settings"] == {"write_approval": True}, "只写显式设置的键"

    restarted = await workspace.restart()
    settings = await restarted.get_settings(USER_A)
    assert settings.write_approval is True
    # 缺键仍是默认值：显式设默认与"从没设置过"在文件里可区分
    assert (settings.extraction_enabled, settings.recall_enabled,
            settings.fallback_enabled) == (True, True, True)
    # ADR-0056 裁决 7：本机设置没有身份路由
    assert (await restarted.get_settings(USER_B)).write_approval is True
    assert _table_rows(workspace.database, "memory_v2_settings") == 0


# --------------------------------------------------------------------------------------
# job / lease / 幂等：一 job 一文件、确定性 id、重启可恢复
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_owner_is_one_file_per_deterministic_key(workspace: Workspace) -> None:
    jobs = MemoryV2JobStore(workspace.database, workspace.root)
    await jobs.initialize()
    job = await jobs.enqueue(
        idempotency_key="restart-key", trusted=USER_A, session_id="s-1", run_id="r-1")

    expected = f"job_{sha256(b'restart-key').hexdigest()[:32]}.json"
    assert jobs.path_for(job_id_for("restart-key")).name == expected
    assert (workspace.root.jobs_dir / expected).exists()
    assert job.job_id == job_id_for("restart-key")

    # 同 key 再入队：物理上不可能产生第二个 job
    again = await jobs.enqueue(
        idempotency_key="restart-key", trusted=USER_A, session_id="s-1", run_id="r-1")
    assert again.job_id == job.job_id
    assert len(list(workspace.root.jobs_dir.glob("*.json"))) == 1

    restarted = await workspace.restart()
    restarted_jobs = MemoryV2JobStore(workspace.database, restarted.state_root)
    await restarted_jobs.initialize()
    claimed = await restarted_jobs.claim(worker_id="worker-after-restart")
    assert claimed is not None and claimed.job_id == job.job_id
    assert claimed.lease_owner == "worker-after-restart"
    assert not _has_table(workspace.database, "memory_v2_jobs"), "job 不再有 SQLite 载体"


# --------------------------------------------------------------------------------------
# staged：待审写入一个文件，重启后仍能被批准
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staged_owner_survives_restart_and_can_be_approved(workspace: Workspace) -> None:
    staged = workspace.service.staged
    assert staged is not None
    record = await staged.stage(
        {"target": "memory", "action": "add", "content": "重启后仍待审的偏好"},
        None, USER_A)
    assert (workspace.root.staged_dir / f"{record.id}.json").exists()

    restarted = await workspace.restart()
    pending = await restarted.staged.pending(USER_A)
    assert [item.id for item in pending] == [record.id]

    decision, response = await restarted.staged.approve(record.id, USER_A)
    assert decision.allow is True
    assert response is not None and response.get("success") is True
    assert "重启后仍待审的偏好" in workspace.memory_file.read_text(encoding="utf-8")
    assert _table_rows(workspace.database, "memory_v2_staged") == 0


# --------------------------------------------------------------------------------------
# tombstone：删除标记是文件，重启后仍阻断同源重放
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_owner_survives_restart_and_still_blocks_replay(
    workspace: Workspace,
) -> None:
    draft = make_draft(content="用户偏好用 pnpm 安装依赖")
    created = await workspace.service.create(draft, USER_A)
    await workspace.service.delete(created.id, USER_A)

    path = FileTombstoneStore(workspace.root).path_for(created.id)
    assert path.exists()
    assert path.name == f"{created.id}.json"

    restarted = await workspace.restart()
    view = await restarted.read_tombstone(created.id, USER_A)
    assert (view.id, view.root_id) == (created.id, created.root_id)
    with pytest.raises(ValueError, match="deleted source"):
        await restarted.create(draft, USER_A)
    assert _table_rows(workspace.database, "memory_v2_tombstones") == 0


# --------------------------------------------------------------------------------------
# receipt：内容提交的回执文件，重启后可对账
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receipt_owner_records_every_content_commit(workspace: Workspace) -> None:
    response = await workspace.service.commit.add("memory", "回执可核对的条目")
    operation = response["operation_id"]

    payload = read_json(ReceiptStore(workspace.root).path_for(operation))
    assert payload is not None
    assert payload["state"] == ReceiptState.COMMITTED
    assert payload["targets"]["memory"]["planned"] is True
    assert payload["targets"]["memory"]["published"] is True

    restarted = await workspace.restart()
    store = ReceiptStore(restarted.state_root)
    assert store.load(operation) is not None
    assert store.list_pending() == [], "COMMITTED 回执不参与对账"

    # 非终态回执重启后仍被扫描到（对账入口可见）
    store.prepare(
        "stgp_restart_pending", origin="foreground", targets=["memory"],
        operations=[{"target": "memory", "action": "add", "content": "未发布"}])
    assert [item.operation_id for item in store.list_pending()] == ["stgp_restart_pending"]


# --------------------------------------------------------------------------------------
# 边界：SQLite 只剩派生态，操作状态一行都不落
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_holds_only_derived_state(workspace: Workspace) -> None:
    await workspace.service.update_settings(USER_A, write_approval=True)
    created = await workspace.service.create(make_draft(content="派生层之外的记忆"), USER_A)
    await workspace.service.commit.add("memory", "额外的权威面条目")
    jobs = MemoryV2JobStore(workspace.database, workspace.root)
    await jobs.initialize()
    await jobs.enqueue(idempotency_key="derived-boundary", trusted=USER_A, session_id="s")

    # records / outbox / FTS 是派生态（可由文件权威面重建），保留在 SQLite
    assert _table_rows(workspace.database, "memory_v2_records") == 1
    assert _table_rows(workspace.database, "memory_v2_outbox") >= 1
    assert _table_rows(workspace.database, "memory_v2_fts") == 1
    assert (await workspace.service.read(created.id, USER_A)).content == "派生层之外的记忆"

    # 五类操作状态：SQLite 侧零行（兼容旧库的空表可以存在，但新写入不落）
    for table in (
        "memory_v2_settings", "memory_v2_tombstones",
        "memory_v2_staged", "memory_v2_blocked",
    ):
        assert _table_rows(workspace.database, table) == 0, table
    assert not _has_table(workspace.database, "memory_v2_jobs")

    # 文件侧：每类 owner 都有实体
    assert workspace.root.settings_path.exists()
    assert (workspace.root.jobs_dir / f"{jobs.path_for(job_id_for('derived-boundary')).name}").exists()
    receipts = list(workspace.root.receipts_dir.glob("*.json"))
    assert len(receipts) == 2, "create 的镜像 + 直接 add 各一条回执"
    assert ENTRY_DELIMITER in workspace.memory_file.read_text(encoding="utf-8")
