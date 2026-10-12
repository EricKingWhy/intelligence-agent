"""#1049：只读端点 ``GET /api/sessions/{id}/recovery`` → ``{pending_decisions}``。

此前待裁决清单的唯一出口是 ``POST /recover`` 的 409（#547 / #596）：前端打开
待裁决会话时无从主动得知「要裁决什么」；后端重启后（``run/interrupted`` 已落盘、
Ledger 仍待裁决）事件流里也没有任何待裁决信号。本端点复用同一个
``SessionService._reconcile_pending``（409 载荷的唯一规则源），因此两处逐字段一致；
零写入；会话有在途 run 时返回空（在途 RUNNING 行不是待裁决）。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import TOOL_CALL, Session
from agent_harness.session.event import RUN_INTERRUPTED, TOOL_RESULT
from agent_harness.storage import Operation, OperationState
from agent_harness.storage.operation import unproven_meta
from agent_harness.web.app import create_app, session_service
from tests.test_web_api import _seed_reconcile_window


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False))


def _snapshot(state, session_id: str):
    """events 原文 + Ledger 行原文：零写入判据的两半。"""
    events = [e.to_dict() for e in state.store.read_events(session_id)]
    rows = asyncio.run(state.stores.operation_ledger.list_for_session(session_id))
    return events, [row.model_dump() for row in rows]


def _recover_409_pending(client, session_id: str) -> list[dict]:
    resp = client.post(f"/api/sessions/{session_id}/recover")
    assert resp.status_code == 409, resp.json()
    return resp.json()["detail"]["pending_decisions"]


def test_restart_scan_pending_listed_and_equals_409_payload(client):
    """AC1：重启扫描后（run/interrupted 落盘、Ledger 仍待裁决），端点列出待裁决项，
    载荷与同会话 409 的 ``pending_decisions`` 逐字段一致。"""
    state = client.app.state.agent
    session = asyncio.run(
        _seed_reconcile_window(state, ["call-r1", "call-r2"], tool_names=["bash", "read"])
    )
    sid = session.session_id
    # 一行先被 DEFER 过（UNKNOWN → NEED_RECONCILE，两步链），另一行保持 UNKNOWN。
    asyncio.run(
        state.stores.operation_ledger.update_state(
            sid, "call-r2", OperationState.NEED_RECONCILE
        )
    )
    asyncio.run(session_service(state).scan_interrupted())
    assert RUN_INTERRUPTED in [e.type for e in state.store.read_events(sid)]

    resp = client.get(f"/api/sessions/{sid}/recovery")

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert set(body) == {"pending_decisions"}
    assert {(p["tool_call_id"], p["state"]) for p in body["pending_decisions"]} == {
        ("call-r1", "UNKNOWN"),
        ("call-r2", "NEED_RECONCILE"),
    }
    assert body["pending_decisions"] == _recover_409_pending(client, sid)


def test_timeout_unproven_unknown_is_listed(client):
    """AC2：MUTATING 工具超时 ⇒ Ledger UNKNOWN + 副作用未证标记，事件里只有一条普通
    失败的 tool/result（不悬空）、run 已终态。这种行只在 Ledger 里，端点必须列出。"""
    state = client.app.state.agent
    ledger = state.stores.operation_ledger
    asyncio.run(ledger.initialize())
    session = Session.start(state.store)
    run_id, _ = session.begin_run()
    session.append(
        TOOL_CALL,
        {"tool_call_id": "call-to", "tool_name": "bash", "args": {}},
        run_id=run_id,
    )
    session.append(
        TOOL_RESULT,
        {"tool_call_id": "call-to", "tool_name": "bash", "content": "timeout", "ok": False},
        run_id=run_id,
    )
    session.end_run(run_id, status="failed", reason="tool_timeout")

    async def _seed():
        await ledger.create(
            Operation(
                tool_call_id="call-to",
                session_id=session.session_id,
                run_id=run_id,
                agent_id=None,
                tool_name="bash",
                args_identity="{}",
                state=OperationState.PENDING,
                started_at="2026-10-12T00:00:00Z",
            )
        )
        await ledger.update_state(session.session_id, "call-to", OperationState.RUNNING)
        await ledger.update_state(
            session.session_id,
            "call-to",
            OperationState.UNKNOWN,
            reconcile_meta=unproven_meta(error_code="TIMEOUT", note="probe"),
        )

    asyncio.run(_seed())

    resp = client.get(f"/api/sessions/{session.session_id}/recovery")

    assert resp.status_code == 200, resp.json()
    pending = resp.json()["pending_decisions"]
    assert [(p["tool_call_id"], p["state"]) for p in pending] == [("call-to", "UNKNOWN")]
    assert pending == _recover_409_pending(client, session.session_id)


def test_in_flight_run_running_rows_are_not_listed(client, monkeypatch):
    """AC3：会话有在途 run 时，它的 RUNNING 行是正常执行中而非待裁决 ⇒ 返回空。"""
    state = client.app.state.agent
    ledger = state.stores.operation_ledger
    asyncio.run(ledger.initialize())
    session = Session.start(state.store)
    run_id, _ = session.begin_run()
    session.append(
        TOOL_CALL,
        {"tool_call_id": "call-live", "tool_name": "bash", "args": {}},
        run_id=run_id,
    )

    async def _seed():
        await ledger.create(
            Operation(
                tool_call_id="call-live",
                session_id=session.session_id,
                run_id=run_id,
                agent_id=None,
                tool_name="bash",
                args_identity="{}",
                state=OperationState.PENDING,
                started_at="2026-10-12T00:00:00Z",
            )
        )
        await ledger.update_state(session.session_id, "call-live", OperationState.RUNNING)

    asyncio.run(_seed())
    sid = session.session_id
    real_get_active = state.run_manager.get_active
    monkeypatch.setattr(
        state.run_manager,
        "get_active",
        lambda session_id: object() if session_id == sid else real_get_active(session_id),
    )

    resp = client.get(f"/api/sessions/{sid}/recovery")

    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"pending_decisions": []}


def test_endpoint_is_zero_write(client):
    """AC4：调用前后 events 与 Ledger 逐字节不变（含重复调用）。"""
    state = client.app.state.agent
    session = asyncio.run(_seed_reconcile_window(state, ["call-z"]))
    sid = session.session_id
    before = _snapshot(state, sid)

    for _ in range(2):
        resp = client.get(f"/api/sessions/{sid}/recovery")
        assert resp.status_code == 200, resp.json()
        assert [p["tool_call_id"] for p in resp.json()["pending_decisions"]] == ["call-z"]

    assert _snapshot(state, sid) == before


def test_unknown_session_is_404_and_invalid_id_422(client):
    """AC5：不存在的会话 404；非法 id 与其他会话端点同口径 422。"""
    assert client.get("/api/sessions/nonexistent-id-123/recovery").status_code == 404
    assert client.get("/api/sessions/bad.id/recovery").status_code == 422


def test_settled_session_returns_empty_list(client):
    """无待裁决（无 Ledger 行）的正常会话 ⇒ 空清单而非错误。"""
    state = client.app.state.agent
    session = Session.start(state.store)
    resp = client.get(f"/api/sessions/{session.session_id}/recovery")
    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"pending_decisions": []}
