from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_harness.config import Settings
from agent_harness.identity import IdentityContext
from agent_harness.instance_lock import (
    CUTOVER_FENCE_FILENAME,
    InstanceLock,
    InstanceLockError,
)
from agent_harness.memory.outbox_relay import OutboxRelay
from agent_harness.memory.record_store import MemoryEntry
from agent_harness.memory.sqlite_record_store import SqliteMemoryRecordStore
from agent_harness.memory.types import MemoryScope
from agent_harness.memory.v2 import cutover
from agent_harness.memory.v2.assembly import (
    MEMORY_V2_DATABASE_NAME,
    build_memory_v2_service,
)
from agent_harness.memory.v2.jobs import SqliteMemoryV2JobStore
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from tests.memory.v2._records import make_draft

_MEMORY_FIELDS = [
    {"name": "id", "type": "VARCHAR", "params": {"max_length": 64}, "is_primary": True},
    {"name": "memory_id", "type": "VARCHAR", "params": {"max_length": 2048}},
    {"name": "tenant_id", "type": "VARCHAR", "params": {"max_length": 2048}, "is_partition_key": True},
    {"name": "user_id", "type": "VARCHAR", "params": {"max_length": 2048}},
    {"name": "scope", "type": "VARCHAR", "params": {"max_length": 2048}},
    {"name": "session_id", "type": "VARCHAR", "params": {"max_length": 2048}},
    {"name": "content", "type": "VARCHAR", "params": {"max_length": 65535}},
    {"name": "metadata", "type": "JSON", "params": {}},
    {"name": "vector", "type": "FLOAT_VECTOR", "params": {"dim": 2}},
]


class FakeMilvus:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._settings = settings
        self.collections = {
            settings.milvus_collection: [{"id": "old-memory-row"}],
            settings.knowledge_collection: [{"id": "preserved-knowledge-row"}],
        }
        self.dropped: list[str] = []
        self.closed = False
        self.fail_initialize_once = False
        self.fields = [{**field, "params": dict(field["params"])} for field in _MEMORY_FIELDS]
        self.auto_id = False
        self.enable_dynamic_field = False

    async def connect(self) -> list[str]:
        return list(self.collections)

    async def initialize(self) -> None:
        if self.fail_initialize_once:
            self.fail_initialize_once = False
            raise RuntimeError("simulated adapter outage")
        self.collections.setdefault(self.settings.milvus_collection, [])

    async def close(self) -> None:
        self.closed = True

    async def _embed(self, _text: str, *, document: bool = False) -> list[float]:
        return [0.25, 0.75]

    async def _call(self, operation: str, **kwargs):
        name = kwargs["collection_name"]
        if operation == "describe_collection":
            return {
                "auto_id": self.auto_id,
                "enable_dynamic_field": self.enable_dynamic_field,
                "fields": self.fields,
            }
        if operation == "query":
            return [{"count(*)": len(self.collections[name])}]
        if operation == "upsert":
            self.collections[name].extend(kwargs["data"])
            return None
        if operation == "drop_collection":
            self.dropped.append(name)
            self.collections.pop(name, None)
            return None
        raise AssertionError(f"unexpected fake operation: {operation}")


def _settings(tmp_path) -> Settings:
    root = tmp_path / "workspace"
    root.mkdir()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    return Settings(
        _env_file=None,
        workspace_dir=str(root),
        artifact_dir=str(artifact_root),
        milvus_uri="https://milvus.example.test",
        milvus_token="test-only",
        milvus_collection="memory_target",
        knowledge_collection="knowledge_target",
        embedding_model="test-model",
        embedding_base_url="https://embedding.example.test",
        embedding_api_key="test-only",
    )


async def _seed_databases(settings: Settings) -> None:
    legacy_records = SqliteMemoryRecordStore(Path(settings.workspace_dir) / "memory.db")
    await legacy_records.initialize()
    await legacy_records.store(
        MemoryEntry(
            id="old-v1-memory", content="old-memory-content-marker", metadata={},
            created_at="2026-09-26T00:00:00+00:00", scope=MemoryScope.USER,
        ),
        IdentityContext("tenant", "user", ["user"]),
    )
    v2_service = await build_memory_v2_service(settings)
    await v2_service.create(
        make_draft(content="old-memory-content-marker", source_event_ids=["old-event"]),
        TrustedMemoryIdentity("tenant", "user"),
    )
    await v2_service.aclose()
    await SqliteMemoryV2JobStore(
        Path(settings.workspace_dir) / MEMORY_V2_DATABASE_NAME,
    ).initialize()


def _install_fake(monkeypatch, fake: FakeMilvus) -> None:
    monkeypatch.setattr(cutover, "build_memory_vector_client", lambda _settings: fake)


def test_interrupted_fence_write_does_not_publish_a_broken_marker(tmp_path, monkeypatch):
    plan = {
        "plan_sha256": "a" * 64,
        "workspace_identity_sha256": "b" * 64,
        "milvus_memory_collection": {"identity_sha256": "c" * 64},
    }

    def interrupted_dump(_record, stream, **_kwargs):
        stream.write("{")
        raise OSError("simulated partial fence write")

    monkeypatch.setattr(cutover.json, "dump", interrupted_dump)
    with pytest.raises(cutover.CutoverRefused, match="cutover_fence_create_failed"):
        cutover._write_fence(tmp_path, plan)

    assert not (tmp_path / CUTOVER_FENCE_FILENAME).exists()
    assert list(tmp_path.glob(f".{CUTOVER_FENCE_FILENAME}.*.tmp")) == []


def _sqlite_bytes(settings: Settings) -> dict[str, bytes]:
    root = Path(settings.workspace_dir)
    return {
        name: (root / name).read_bytes()
        for name in ("memory.db", MEMORY_V2_DATABASE_NAME)
    }


@pytest.mark.asyncio
async def test_dry_run_is_read_only_and_contains_counts_without_memory_content(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    await _seed_databases(settings)
    fake = FakeMilvus(settings)
    _install_fake(monkeypatch, fake)
    before = _sqlite_bytes(settings)

    plan = await cutover.build_plan(settings)

    assert _sqlite_bytes(settings) == before
    assert plan["sqlite_targets"][0]["row_counts"] == {
        "memory_outbox": 1, "memory_records": 1,
    }
    serialized = json.dumps(plan)
    assert "old-memory-content-marker" not in serialized
    assert "test-only" not in serialized
    assert str(settings.workspace_dir) not in serialized
    assert plan["milvus_memory_collection"]["row_count"] == 1
    assert plan["knowledge_collection"]["row_count"] == 1
    assert fake.dropped == []


@pytest.mark.asyncio
async def test_dry_run_plan_binds_milvus_target_without_emitting_credentials_or_uri(
    tmp_path, monkeypatch,
):
    settings = _settings(tmp_path)
    fake = FakeMilvus(settings)
    _install_fake(monkeypatch, fake)

    plan = await cutover.build_plan(settings)
    uri_plan = await cutover.build_plan(
        settings.model_copy(update={"milvus_uri": "https://different.example.test"}),
    )
    credential_plan = await cutover.build_plan(
        settings.model_copy(update={"milvus_token": SecretStr("different-test-only")}),
    )

    assert plan["milvus_target_identity_sha256"] != uri_plan["milvus_target_identity_sha256"]
    assert plan["milvus_target_identity_sha256"] != credential_plan[
        "milvus_target_identity_sha256"
    ]
    assert plan["plan_sha256"] != uri_plan["plan_sha256"]
    assert plan["plan_sha256"] != credential_plan["plan_sha256"]
    serialized = json.dumps(plan)
    assert settings.milvus_uri not in serialized
    assert settings.milvus_token.get_secret_value() not in serialized


@pytest.mark.asyncio
async def test_dry_run_refuses_missing_configured_memory_collection(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    fake = FakeMilvus(settings)
    fake.collections.pop(settings.milvus_collection)
    _install_fake(monkeypatch, fake)

    with pytest.raises(cutover.CutoverRefused, match="configured_memory_collection_not_found"):
        await cutover.build_plan(settings)

    assert settings.milvus_collection not in fake.collections
    assert fake.dropped == []


@pytest.mark.asyncio
async def test_cutover_clears_only_memory_tables_and_collection_and_proves_preservation(
    tmp_path, monkeypatch,
):
    from pathlib import Path

    settings = _settings(tmp_path)
    await _seed_databases(settings)
    root = Path(settings.workspace_dir)
    (root / "sessions" / "s1").mkdir(parents=True)
    (root / "sessions" / "s1" / "events.jsonl").write_text("session-event", encoding="utf-8")
    (root / "workspaces").mkdir()
    (root / "workspaces" / "project.json").write_text("workspace-state", encoding="utf-8")
    (root / "harness.db").write_bytes(b"checkpoint-and-session-metadata")
    (Path(settings.artifact_dir) / "artifact.bin").write_bytes(b"preserved-artifact")
    fake = FakeMilvus(settings)
    _install_fake(monkeypatch, fake)
    plan = await cutover.build_plan(settings)
    report_path = tmp_path / "cutover-report.json"

    report = await cutover.apply_plan(
        settings, expected_plan_sha256=plan["plan_sha256"], report_path=report_path,
    )

    assert report["status"] == "completed"
    assert report["preserved_domains_unchanged"] is True
    assert report["knowledge_unchanged"] is True
    assert report["milvus_memory_collection"]["row_count"] == 0
    assert report["milvus_memory_collection"]["recreated"] is True
    assert fake.dropped == [settings.milvus_collection]
    assert len(fake.collections[settings.knowledge_collection]) == 1
    assert all(not any(item["after"].values()) for item in report["sqlite_results"])
    assert all(item["freelist_pages"] == 0 for item in report["sqlite_results"] if item["exists"])
    assert not (root / CUTOVER_FENCE_FILENAME).exists()
    assert report_path.exists()
    assert "old-memory-content-marker" not in report_path.read_text(encoding="utf-8")
    assert "test-only" not in report_path.read_text(encoding="utf-8")
    with sqlite3.connect(root / "memory.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 0
    with sqlite3.connect(root / MEMORY_V2_DATABASE_NAME) as connection:
        assert all(
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
            for table in cutover._DATABASE_TABLES[MEMORY_V2_DATABASE_NAME]
        )

    class ReplayProbe:
        def __init__(self):
            self.upserts = []

        async def upsert(self, *args, **kwargs):
            self.upserts.append((args, kwargs))

        async def delete(self, *args, **kwargs):
            raise AssertionError("an empty outbox must not request a vector delete")

    legacy_records = SqliteMemoryRecordStore(root / "memory.db")
    replay_probe = ReplayProbe()
    assert await OutboxRelay(legacy_records, replay_probe).flush() == 0
    assert replay_probe.upserts == []

    service = await build_memory_v2_service(settings, vector_store=fake)
    trusted = TrustedMemoryIdentity("tenant", "new-user")
    created = await service.create(
        make_draft(content="post-cutover V2 smoke", source_event_ids=["new-event"]), trusted,
    )
    await service._relay.flush()
    assert (await service.read(created.id, trusted)).content == "post-cutover V2 smoke"
    assert await cutover._milvus_collection_inventory(
        fake, settings, settings.milvus_collection,
    ) == {
        "identity_sha256": report["milvus_memory_collection"]["identity_sha256"],
        "exists": True,
        "row_count": 1,
        "schema_sha256": report["milvus_memory_collection"]["schema_sha256"],
    }
    await service.aclose()


@pytest.mark.asyncio
async def test_interrupted_cutover_stays_fenced_and_can_resume_same_plan(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    await _seed_databases(settings)
    fake = FakeMilvus(settings)
    fake.fail_initialize_once = True
    _install_fake(monkeypatch, fake)
    plan = await cutover.build_plan(settings)
    first_report = tmp_path / "first-report.json"

    with pytest.raises(RuntimeError, match="simulated adapter outage"):
        await cutover.apply_plan(
            settings, expected_plan_sha256=plan["plan_sha256"], report_path=first_report,
        )

    root = Path(settings.workspace_dir)
    assert (root / CUTOVER_FENCE_FILENAME).exists()
    with pytest.raises(InstanceLockError, match="reset is incomplete"):
        InstanceLock(root).acquire()

    with pytest.raises(cutover.CutoverRefused, match="interrupted_cutover_requires_resume"):
        await cutover.apply_plan(
            settings,
            expected_plan_sha256=plan["plan_sha256"],
            report_path=tmp_path / "apply-must-not-resume.json",
        )
    assert (root / CUTOVER_FENCE_FILENAME).exists()

    report = await cutover.apply_plan(
        settings,
        expected_plan_sha256=plan["plan_sha256"],
        report_path=tmp_path / "resumed-report.json",
        resume=True,
    )

    assert report["status"] == "completed"
    assert report["preserved_domains_unchanged"] is True
    assert not (root / CUTOVER_FENCE_FILENAME).exists()
    assert fake.dropped == [settings.milvus_collection]


@pytest.mark.asyncio
async def test_apply_cli_refuses_to_resume_existing_fence(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)
    fake = FakeMilvus(settings)
    _install_fake(monkeypatch, fake)
    plan = await cutover.build_plan(settings)
    root = Path(settings.workspace_dir)
    cutover._write_fence(root, plan)
    monkeypatch.setattr(cutover, "Settings", lambda: settings)

    exit_code = await cutover._main([
        "--apply",
        "--confirm-plan-sha256",
        plan["plan_sha256"],
        "--report",
        str(tmp_path / "report.json"),
    ])

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "interrupted_cutover_requires_resume"
    assert (root / CUTOVER_FENCE_FILENAME).exists()
    assert fake.dropped == []


@pytest.mark.asyncio
async def test_cutover_publishes_startup_fence_before_locked_inventory_await(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    await _seed_databases(settings)
    fake = FakeMilvus(settings)
    _install_fake(monkeypatch, fake)
    plan = await cutover.build_plan(settings)
    root = Path(settings.workspace_dir)
    locked_inventory_started = asyncio.Event()
    resume_locked_inventory = asyncio.Event()
    original_build_plan = cutover.build_plan
    call_count = 0

    async def paused_build_plan(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            locked_inventory_started.set()
            await resume_locked_inventory.wait()
        return await original_build_plan(*args, **kwargs)

    monkeypatch.setattr(cutover, "build_plan", paused_build_plan)
    task = asyncio.create_task(cutover.apply_plan(
        settings,
        expected_plan_sha256=plan["plan_sha256"],
        report_path=tmp_path / "fenced-report.json",
    ))
    try:
        await asyncio.wait_for(locked_inventory_started.wait(), timeout=3)
        assert (root / CUTOVER_FENCE_FILENAME).exists()
        with pytest.raises(InstanceLockError, match="reset is incomplete"):
            InstanceLock(root).acquire()
    finally:
        resume_locked_inventory.set()
    report = await task

    assert report["status"] == "completed"
    assert not (root / CUTOVER_FENCE_FILENAME).exists()


@pytest.mark.asyncio
async def test_cutover_refuses_shared_root_writer_before_mutation(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    await _seed_databases(settings)
    fake = FakeMilvus(settings)
    _install_fake(monkeypatch, fake)
    plan = await cutover.build_plan(settings)
    before = _sqlite_bytes(settings)

    def active_shared_writer(_lock):
        raise InstanceLockError("A shared-root workspace writer is active")

    monkeypatch.setattr(InstanceLock, "assert_no_shared_root_writers", active_shared_writer)

    with pytest.raises(cutover.CutoverRefused, match="workspace_writer_is_active"):
        await cutover.apply_plan(
            settings,
            expected_plan_sha256=plan["plan_sha256"],
            report_path=tmp_path / "refused-report.json",
        )

    assert _sqlite_bytes(settings) == before
    assert fake.dropped == []
    assert not (Path(settings.workspace_dir) / CUTOVER_FENCE_FILENAME).exists()


@pytest.mark.asyncio
async def test_cutover_refuses_unknown_sqlite_tables_without_mutation(tmp_path, monkeypatch):
    import sqlite3
    from pathlib import Path

    settings = _settings(tmp_path)
    path = Path(settings.workspace_dir) / "memory.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE memory_records (content TEXT)")
        connection.execute("CREATE TABLE unrelated_business_data (content TEXT)")
        connection.execute("INSERT INTO unrelated_business_data VALUES ('untouched')")
    fake = FakeMilvus(settings)
    _install_fake(monkeypatch, fake)

    with pytest.raises(cutover.CutoverRefused, match="schema_contains_unapproved_objects"):
        await cutover.build_plan(settings)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT content FROM unrelated_business_data").fetchone()[0] == "untouched"
    assert fake.dropped == []


@pytest.mark.asyncio
async def test_cutover_rejects_knowledge_collection_as_memory_target(tmp_path, monkeypatch):
    settings = _settings(tmp_path).model_copy(update={"milvus_collection": "knowledge_target"})
    fake = FakeMilvus(settings)
    _install_fake(monkeypatch, fake)

    with pytest.raises(cutover.CutoverRefused, match="conflicts_with_knowledge"):
        await cutover.build_plan(settings)

    assert fake.dropped == []


@pytest.mark.parametrize("name", ["", "*", "memory*", "memory_target.other"])
@pytest.mark.asyncio
async def test_cutover_rejects_broad_or_unresolved_collection_names(tmp_path, monkeypatch, name):
    settings = _settings(tmp_path).model_copy(update={"milvus_collection": name})
    fake = FakeMilvus(settings)
    _install_fake(monkeypatch, fake)

    with pytest.raises(cutover.CutoverRefused, match="memory_collection_target_unresolved_or_broad"):
        await cutover.build_plan(settings)

    assert fake.dropped == []


@pytest.mark.asyncio
async def test_cutover_refuses_collection_with_extra_non_memory_fields(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    fake = FakeMilvus(settings)
    fake.fields.append({"name": "unrelated_payload", "type": "VARCHAR", "params": {}})
    _install_fake(monkeypatch, fake)

    with pytest.raises(cutover.CutoverRefused, match="configured_collection_schema_not_memory"):
        await cutover.build_plan(settings)

    assert fake.dropped == []


@pytest.mark.parametrize(
    "mismatch",
    [
        "varchar_length", "dynamic_field", "field_type", "auto_id",
        "primary_field", "partition_field",
    ],
)
@pytest.mark.asyncio
async def test_cutover_refuses_collection_with_non_v2_schema_shape(tmp_path, monkeypatch, mismatch):
    settings = _settings(tmp_path)
    fake = FakeMilvus(settings)
    if mismatch == "varchar_length":
        fake.fields[1]["params"]["max_length"] = 64
    elif mismatch == "dynamic_field":
        fake.enable_dynamic_field = True
    elif mismatch == "field_type":
        fake.fields[1]["type"] = "JSON"
    elif mismatch == "auto_id":
        fake.auto_id = True
    elif mismatch == "primary_field":
        fake.fields[0]["is_primary"] = False
    else:
        fake.fields[2]["is_partition_key"] = False
    _install_fake(monkeypatch, fake)

    with pytest.raises(cutover.CutoverRefused, match="configured_collection_schema_not_memory"):
        await cutover.build_plan(settings)

    assert fake.dropped == []
