"""SqliteOperationLedger contract tests."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from agent_harness.storage import (
    Operation,
    OperationState,
    SqliteOperationLedger,
)


@pytest.mark.asyncio
async def test_create_pending_operation_can_be_loaded(tmp_path: Path) -> None:
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    operation = Operation(
        tool_call_id="call-1",
        session_id="session-1",
        run_id="run-1",
        agent_id="agent-1",
        tool_name="write",
        args_identity='{"content": "hello", "path": "note.txt"}',
        state=OperationState.PENDING,
        started_at="2026-09-04T00:00:00+00:00",
    )

    await ledger.create(operation)

    loaded = await ledger.get("call-1")
    assert loaded == operation
    assert loaded is not None
    assert loaded.operation_id == "call-1"
    assert loaded.artifact_ref is None


@pytest.mark.asyncio
async def test_operation_moves_to_terminal_state_with_recovery_data(
    tmp_path: Path,
) -> None:
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    await ledger.create(
        Operation(
            tool_call_id="call-2",
            session_id="session-1",
            tool_name="read",
            args_identity='{"path": "note.txt"}',
            state=OperationState.PENDING,
            started_at="2026-09-04T00:00:00+00:00",
        )
    )

    running = await ledger.update_state("call-2", OperationState.RUNNING)
    succeeded = await ledger.update_state(
        "call-2",
        OperationState.SUCCEEDED,
        result_json='{"ok":true,"message":"read"}',
        artifact_ref="artifact://read-1",
    )

    assert running.state is OperationState.RUNNING
    assert succeeded.state is OperationState.SUCCEEDED
    assert succeeded.result_json == '{"ok":true,"message":"read"}'
    assert succeeded.artifact_ref == "artifact://read-1"
    assert succeeded.finished_at is not None
    with pytest.raises(ValueError, match="SUCCEEDED -> RUNNING"):
        await ledger.update_state("call-2", OperationState.RUNNING)


@pytest.mark.asyncio
async def test_list_for_session_excludes_other_sessions(tmp_path: Path) -> None:
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    for call_id, session_id in (("a", "target"), ("b", "other"), ("c", "target")):
        await ledger.create(
            Operation(
                tool_call_id=call_id,
                session_id=session_id,
                tool_name="read",
                args_identity="{}",
                state=OperationState.PENDING,
                started_at=(
                    "2026-09-04T00:00:01+00:00"
                    if call_id == "c"
                    else "2026-09-04T00:00:00+00:00"
                ),
            )
        )

    operations = await ledger.list_for_session("target")

    assert [operation.tool_call_id for operation in operations] == ["a", "c"]


@pytest.mark.asyncio
async def test_operations_schema_contains_frozen_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    ledger = SqliteOperationLedger(database_path)
    await ledger.initialize()

    async with aiosqlite.connect(database_path) as connection:
        cursor = await connection.execute("PRAGMA table_info(operations)")
        columns = {row[1]: row for row in await cursor.fetchall()}

    assert set(columns) == {
        "tool_call_id",
        "session_id",
        "run_id",
        "agent_id",
        "tool_name",
        "args_identity",
        "state",
        "result_json",
        "artifact_ref",
        "started_at",
        "finished_at",
        "reconcile_meta",
    }
    assert columns["tool_call_id"][5] == 1
    assert columns["artifact_ref"][3] == 0


# ── B 组加固（R4-5）：连接级并发 PRAGMA ──


@pytest.mark.asyncio
async def test_connect_sets_busy_timeout(tmp_path):
    """每操作新连接模式下 busy_timeout 必须随连接设置——默认 0 会让并发写
    立刻抛 database is locked（WAL 是持久 PRAGMA，busy_timeout 不是）。"""
    from agent_harness.storage.sqlite import _connect

    async with _connect(tmp_path / "busy.db") as connection:
        cursor = await connection.execute("PRAGMA busy_timeout")
        (value,) = await cursor.fetchone()
    assert int(value) > 0


@pytest.mark.asyncio
async def test_ledger_methods_route_through_shared_connect(tmp_path, monkeypatch):
    """Ledger 的所有连接必须经 _connect 助手（busy_timeout 在其中设置）——
    直接 aiosqlite.connect 会绕过并发保护。"""
    from contextlib import asynccontextmanager

    from agent_harness.storage import sqlite as sqlite_mod

    used: list[Path] = []
    real_connect = sqlite_mod._connect

    @asynccontextmanager
    async def spy_connect(path):
        used.append(Path(path))
        async with real_connect(path) as connection:
            yield connection

    monkeypatch.setattr(sqlite_mod, "_connect", spy_connect)

    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    operation = Operation(
        tool_call_id="call-route",
        session_id="session-route",
        run_id="run-route",
        agent_id="agent-1",
        tool_name="write",
        args_identity="{}",
        state=OperationState.PENDING,
        started_at="2026-09-05T00:00:00+00:00",
    )
    await ledger.create(operation)
    assert used, "Ledger 连接未经过 _connect 助手"
