"""Connection lifecycle contracts for the knowledge and transport stores."""

from __future__ import annotations

import asyncio
import sqlite3

import aiosqlite
import pytest

from agent_harness.knowledge.registry import SqliteKnowledgeSourceRegistry
from agent_harness.knowledge.types import KnowledgeSource
from agent_harness.transport.contract import (
    SqliteTransportLedger,
    TransportLedgerEntry,
    TransportStatus,
)
from agent_harness.transport.contract import (
    _connect as transport_connect,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["knowledge", "transport"])
async def test_connection_timeout_and_close_on_success_and_failure(tmp_path, kind):
    database = tmp_path / "harness.db"
    registry = SqliteKnowledgeSourceRegistry(database)
    open_connection = registry._connect if kind == "knowledge" else lambda: transport_connect(database)

    async with open_connection() as connection:
        cursor = await connection.execute("PRAGMA busy_timeout")
        assert (await cursor.fetchone())[0] == 10000
    with pytest.raises(ValueError, match="no active connection"):
        await connection.execute("SELECT 1")

    with pytest.raises(RuntimeError, match="test failure"):
        async with open_connection() as failed_connection:
            raise RuntimeError("test failure")
    with pytest.raises(ValueError, match="no active connection"):
        await failed_connection.execute("SELECT 1")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["knowledge", "transport"])
async def test_store_waits_for_real_write_lock_and_persists_after_release(tmp_path, kind, monkeypatch):
    database = tmp_path / "harness.db"
    registry = SqliteKnowledgeSourceRegistry(database)
    ledger = SqliteTransportLedger(database)
    store = registry if kind == "knowledge" else ledger
    await store.initialize()

    begin_immediate_reached = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_connect = aiosqlite.connect

    def observe_sql(statement: str) -> None:
        if statement.lstrip().upper().startswith("BEGIN IMMEDIATE"):
            loop.call_soon_threadsafe(begin_immediate_reached.set)

    async def traced_connect(*args, **kwargs):
        connection = await original_connect(*args, **kwargs)
        await connection.set_trace_callback(observe_sql)
        return connection

    monkeypatch.setattr(aiosqlite, "connect", traced_connect)

    source = KnowledgeSource("source-1", "tenant-1", "sample", "hash", 1, "2026", "2026")
    entry = TransportLedgerEntry(
        request_id="request-1",
        session_id="session-1",
        operation_id="operation-1",
        command_summary="status",
        scope="workspace",
        status=TransportStatus.STARTED,
        created_at="2026-01-01T00:00:00+00:00",
    )
    blocker = sqlite3.connect(database)
    blocker.execute("BEGIN IMMEDIATE")
    pending = asyncio.create_task(registry.upsert(source) if kind == "knowledge" else ledger.append(entry))
    try:
        await asyncio.wait_for(begin_immediate_reached.wait(), timeout=5)
        assert not pending.done(), "write must wait while a real SQLite writer owns the lock"
    finally:
        blocker.rollback()
        blocker.close()
        await asyncio.wait_for(pending, timeout=5)

    if kind == "knowledge":
        assert await registry.get("source-1", "tenant-1") == source
    else:
        assert await ledger.list_for_request("request-1") == [entry]
