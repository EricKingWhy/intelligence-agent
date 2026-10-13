"""#1049：只读端点 ``GET /api/sessions/{id}/recovery`` → ``{pending_decisions}``。

此前待裁决清单的唯一出口是 ``POST /recover`` 的 409（#547 / #596）：前端打开
待裁决会话时无从主动得知「要裁决什么」；后端重启后（``run/interrupted`` 已落盘、
Ledger 仍待裁决）事件流里也没有任何待裁决信号。本端点复用同一个
``SessionService._reconcile_pending``（409 载荷的唯一规则源），因此两处逐字段一致；
零写入；会话有在途 run 时返回空（在途 RUNNING 行不是待裁决）。
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx2
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk

from agent_harness.agent import AgentRuntime
from agent_harness.config import Settings
from agent_harness.session import TOOL_CALL, Session
from agent_harness.session.event import RUN_INTERRUPTED, TOOL_RESULT
from agent_harness.storage import Operation, OperationState
from agent_harness.storage.operation import unproven_meta
from agent_harness.tooling import ToolExecutor, ToolRegistry
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


class _BlockedModel:
    """首个模型回合挂住直到放行：让 run 真实在途（RunManager 已登记、task 未 done）。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        self.entered.set()
        await self.release.wait()
        yield AIMessageChunk(content="完成")


@contextlib.asynccontextmanager
async def _in_flight_run(state, session: Session):
    """用真实 ``RunManager.launch`` 登记一个挂起的 run；退出时放行并等它终态。"""
    model = _BlockedModel()
    registry = ToolRegistry()
    runtime = AgentRuntime(
        model=model, registry=registry,
        executor=ToolExecutor(registry), max_agent_turns=5,
    )
    run, _subscriber = state.run_manager.launch(session, runtime, "hi")
    try:
        async with asyncio.timeout(10):
            await model.entered.wait()
        assert state.run_manager.get_active(session.session_id) is run
        yield run
    finally:
        model.release.set()
        async with asyncio.timeout(10):
            await run.task


async def _seed_ledger_running(
    ledger, session_id: str, run_id: str, call_id: str
) -> None:
    """Ledger 行 PENDING → RUNNING 两步链（与执行器落账的迁移顺序一致）。"""
    await ledger.create(
        Operation(
            tool_call_id=call_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=None,
            tool_name="bash",
            args_identity="{}",
            state=OperationState.PENDING,
            started_at="Mon 2026-10-12 8:00 AM CST (UTC+08:00)",
        )
    )
    await ledger.update_state(session_id, call_id, OperationState.RUNNING)


async def _seed_running_call(state, session: Session, call_id: str) -> None:
    """悬空 tool/call + Ledger RUNNING（PENDING → RUNNING 两步链）。"""
    ledger = state.stores.operation_ledger
    await ledger.initialize()
    run_id, _ = session.begin_run()
    session.append(
        TOOL_CALL,
        {"tool_call_id": call_id, "tool_name": "bash", "args": {}},
        run_id=run_id,
    )
    await _seed_ledger_running(ledger, session.session_id, run_id, call_id)


async def _get_pending(app, session_id: str) -> list[dict]:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.get(f"/api/sessions/{session_id}/recovery")
    assert resp.status_code == 200, resp.text
    return resp.json()["pending_decisions"]


def _child_of(state, root: Session) -> Session:
    """委派子会话：``session/started`` 带 ``delegation_root_session_id``（provider 落的锚）。"""
    return Session.start(
        state.store, started_data={"delegation_root_session_id": root.session_id}
    )


@pytest.mark.asyncio
async def test_in_flight_run_running_rows_are_not_listed(client):
    """AC3：会话有在途 run（真实 RunManager 登记的挂起 task）时，它的 RUNNING 行是
    正常执行中而非待裁决 ⇒ 返回空。"""
    state = client.app.state.agent
    ledger = state.stores.operation_ledger
    await ledger.initialize()
    session = Session.start(state.store)
    sid = session.session_id

    async with _in_flight_run(state, session) as run:
        run_id = await run.wait_run_id()
        assert run_id is not None
        await _seed_ledger_running(ledger, sid, run_id, "call-live")

        assert await _get_pending(client.app, sid) == []


@pytest.mark.asyncio
async def test_child_session_rows_hidden_while_root_run_in_flight(client):
    """#1051：子代理跑在父 run 的 task 里，不单独登记，RunManager 按委派树根会话
    登记 ⇒ 子会话的在途判定也要按树根会话做；父 run 在途时子会话的 RUNNING 行
    不是待裁决。"""
    state = client.app.state.agent
    root = Session.start(state.store)
    child = _child_of(state, root)
    await _seed_running_call(state, child, "call-child")

    async with _in_flight_run(state, root):
        assert state.run_manager.get_active(child.session_id) is None
        assert await _get_pending(client.app, child.session_id) == []


@pytest.mark.asyncio
async def test_child_session_rows_hidden_while_own_run_in_flight(client):
    """#1080：用户直接续聊子会话时，run 按子会话自身 id 登记（树根会话此时无在途）
    ⇒ 子会话自身的在途 run 也要判；它的 RUNNING 行是正在执行，不是待裁决。"""
    state = client.app.state.agent
    ledger = state.stores.operation_ledger
    await ledger.initialize()
    root = Session.start(state.store)
    child = _child_of(state, root)

    async with _in_flight_run(state, child) as run:
        run_id = await run.wait_run_id()
        assert run_id is not None
        await _seed_ledger_running(ledger, child.session_id, run_id, "call-own")
        assert state.run_manager.get_active(root.session_id) is None

        assert await _get_pending(client.app, child.session_id) == []


@pytest.mark.asyncio
async def test_child_session_dangling_rows_listed_after_root_run_ends(client):
    """#1051：父 run 结束后，子会话里真正悬空的行仍要列出（根会话判在途不能把
    子会话一律藏掉）。"""
    state = client.app.state.agent
    root = Session.start(state.store)
    child = _child_of(state, root)
    await _seed_running_call(state, child, "call-child")

    async with _in_flight_run(state, root):
        pass
    assert state.run_manager.get_active(root.session_id) is None

    pending = await _get_pending(client.app, child.session_id)
    assert [(p["tool_call_id"], p["state"]) for p in pending] == [
        ("call-child", "RUNNING")
    ]


@pytest.mark.asyncio
async def test_run_started_during_read_is_not_listed(client, monkeypatch):
    """#1051 F2：判在途与读 Ledger 之间有新 run 启动 ⇒ 读后复查在途，返回空
    （否则会把刚开始执行的 RUNNING 行误报为待裁决）。"""
    state = client.app.state.agent
    session = Session.start(state.store)
    await _seed_running_call(state, session, "call-race")
    ledger = state.stores.operation_ledger
    real_list = ledger.list_for_session

    async with contextlib.AsyncExitStack() as stack:

        async def list_then_launch(session_id):
            rows = await real_list(session_id)
            await stack.enter_async_context(_in_flight_run(state, session))
            return rows

        monkeypatch.setattr(ledger, "list_for_session", list_then_launch)

        assert await _get_pending(client.app, session.session_id) == []
        assert state.run_manager.get_active(session.session_id) is not None


@pytest.mark.asyncio
async def test_rows_read_while_run_in_flight_are_not_listed_after_it_ends(
    client, monkeypatch
):
    """#1051：读 Ledger 时 run 还在途、读完它才结束 ⇒ 读到的是在途快照（其中的
    RUNNING 行此刻已 SUCCEEDED），不能当待裁决返回——在途判定必须先于读取。

    本用例守的是**读前判定**：正确实现在读 Ledger 之前就因在途返回空，补丁
    ``list_then_finish`` 根本走不到（调用计数 == 0）；撤掉读前判定后补丁才会被
    调用，读到 RUNNING 行、读后复查时 run 已结束 ⇒ 误列。"""
    state = client.app.state.agent
    ledger = state.stores.operation_ledger
    await ledger.initialize()
    session = Session.start(state.store)
    sid = session.session_id
    real_list = ledger.list_for_session

    async with contextlib.AsyncExitStack() as stack:
        run = await stack.enter_async_context(_in_flight_run(state, session))
        run_id = await run.wait_run_id()
        assert run_id is not None
        await _seed_ledger_running(ledger, sid, run_id, "call-live")
        list_calls: list[str] = []

        async def list_then_finish(session_id):
            list_calls.append(session_id)
            rows = await real_list(session_id)
            await ledger.update_state(
                sid, "call-live", OperationState.SUCCEEDED, result_json="{}"
            )
            await stack.aclose()
            return rows

        monkeypatch.setattr(ledger, "list_for_session", list_then_finish)

        assert await _get_pending(client.app, sid) == []
        assert list_calls == []


def test_corrupt_event_log_is_409(client):
    """F4：日志完整坏行 ⇒ 409（与 ``recover`` 同一前导），不返回空清单冒充无待裁决。"""
    state = client.app.state.agent
    session = Session.start(state.store)
    with state.store.events_path(session.session_id).open("ab") as fh:
        fh.write(b"{not json}\n")

    resp = client.get(f"/api/sessions/{session.session_id}/recovery")

    assert resp.status_code == 409, resp.json()


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


def test_unknown_session_is_404(client):
    """AC5：不存在的会话 404。"""
    assert client.get("/api/sessions/nonexistent-id-123/recovery").status_code == 404


def test_invalid_session_id_is_422(client):
    """非法 id 先于存在性判定 ⇒ 422（与其他会话端点同口径，防路径穿越）。"""
    assert client.get("/api/sessions/bad.id/recovery").status_code == 422


def test_settled_session_returns_empty_list(client):
    """无待裁决（无 Ledger 行）的正常会话 ⇒ 空清单而非错误。"""
    state = client.app.state.agent
    session = Session.start(state.store)
    resp = client.get(f"/api/sessions/{session.session_id}/recovery")
    assert resp.status_code == 200, resp.json()
    assert resp.json() == {"pending_decisions": []}
