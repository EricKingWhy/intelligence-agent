"""Live Milvus coverage for the V2 derived index, isolated to memory_gate_test."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agent_harness.config import Settings
from agent_harness.memory.embeddings import create_embeddings
from agent_harness.memory.milvus_vector_store import MilvusVectorStore
from agent_harness.memory.v2._sqlite import connect
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.index import MemoryV2IndexRelay
from agent_harness.memory.v2.milvus_index import MilvusMemoryV2Index
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import MemoryKind, MemoryScope, TrustedMemoryIdentity
from tests.memory.v2._records import make_draft


@pytest.mark.integration
@pytest.mark.live_services
@pytest.mark.asyncio
async def test_v2_milvus_upsert_single_and_bulk_delete(tmp_path) -> None:
    settings = Settings()
    if settings.milvus_collection != "memory_gate_test":
        pytest.skip("live Milvus test requires MILVUS_COLLECTION=memory_gate_test")
    if not settings.milvus_uri or not settings.milvus_token.get_secret_value():
        pytest.skip("live Milvus credentials are not configured")
    if not all((
        settings.embedding_model,
        settings.embedding_base_url,
        settings.embedding_api_key.get_secret_value(),
    )):
        pytest.skip("live embedding credentials are not configured")

    vectors = MilvusVectorStore(settings, create_embeddings(settings))
    identity = TrustedMemoryIdentity("memory-gate-test", f"user-{uuid4().hex}")
    written: list[str] = []
    cleanup_errors: list[Exception] = []
    index = None
    service = None

    def live_draft(label: str):
        source_id = f"event-{uuid4().hex}"
        return make_draft(
            content=f"{label} {source_id}", source_event_ids=[source_id],
        )

    async def is_indexed(memory_id: str) -> bool:
        rows = await vectors._call(
            "query", collection_name=settings.milvus_collection,
            filter=(
                "tenant_id == {tenant} AND user_id == {user} AND scope == {scope} "
                "AND session_id == {route} AND memory_id == {memory}"
            ),
            filter_params={
                "tenant": identity.tenant_id, "user": identity.user_id,
                "scope": MemoryScope.USER_GLOBAL.value, "route": "", "memory": memory_id,
            },
            output_fields=["memory_id"], consistency_level="Strong",
        )
        return bool(rows)

    try:
        await vectors.initialize()
        store = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
        await store.initialize()
        index = MilvusMemoryV2Index(vectors)
        relay = MemoryV2IndexRelay(store, index)
        service = MemoryV2Service(store, index, relay=relay)

        one = await service.create(live_draft("live single"), identity)
        written.append(one.id)
        await relay.flush()
        assert await is_indexed(one.id)
        hits = await service.hybrid_search(
            one.content, identity, scopes=(MemoryScope.USER_GLOBAL,), limit=5,
        )
        by_id = {hit.record.id: hit for hit in hits}
        assert by_id[one.id].explanation["dense"] > 0

        await service.delete(one.id, identity)
        assert not await is_indexed(one.id)
        with pytest.raises(KeyError):
            await store.get(one.id, identity)
        async with connect(store.database_path) as connection:
            tombstones = await connection.execute_fetchall(
                "SELECT * FROM memory_v2_tombstones WHERE memory_id = ?", (one.id,),
            )
        assert len(tombstones) == 1
        assert set(tombstones[0].keys()) == {
            "memory_id", "root_id", "tenant_id", "user_id", "scope", "project_id",
            "version", "deleted_at", "expires_at", "deletion_reason", "content_hashes",
            "source_hashes",
        }

        many = [
            await service.create(live_draft("live bulk"), identity)
            for _ in range(2)
        ]
        written.extend(record.id for record in many)
        await relay.flush()
        assert all([await is_indexed(record.id) for record in many])

        receipts = await service.bulk_delete(identity, kind=MemoryKind.SEMANTIC)
        assert sum(receipt.deleted for receipt in receipts) == len(many)
        assert not any([await is_indexed(record.id) for record in many])
        for record in many:
            with pytest.raises(KeyError):
                await store.get(record.id, identity)
        async with connect(store.database_path) as connection:
            tombstones = await connection.execute_fetchall(
                "SELECT * FROM memory_v2_tombstones WHERE memory_id IN (?, ?)",
                tuple(record.id for record in many),
            )
        assert {row["memory_id"] for row in tombstones} == {record.id for record in many}
    finally:
        try:
            if index is not None:
                for memory_id in written:
                    try:
                        await index.delete(
                            memory_id, tenant_id=identity.tenant_id, user_id=identity.user_id,
                            scope=MemoryScope.USER_GLOBAL, project_id=None,
                        )
                    except Exception as error:  # noqa: BLE001 — continue cleaning each test row.
                        cleanup_errors.append(error)
        finally:
            try:
                if service is not None:
                    await service.aclose()
            finally:
                try:
                    await vectors.drop_created_collection()
                finally:
                    await vectors.close()
        if cleanup_errors:
            kinds = ", ".join(sorted({type(error).__name__ for error in cleanup_errors}))
            raise RuntimeError(f"Milvus row cleanup failed ({kinds})") from None
