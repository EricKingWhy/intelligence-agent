"""Web API 契约测试（Phase 9/10 精简版）。

覆盖范围：
- /api/health 总是 200
- /api/sessions 列历史（空 + 非空）
- /api/sessions/{id}/events 读历史（404 + ok）
- /api/sessions POST 起新 session 并拿到 SSE 流
- /api/sessions/{id}/approve 返回 202 seam

用 TestClient（同步接口）+ 一个 stub runtime 工厂注入，
避免测试里真起模型 + 真跑工具——契约测试只关心 HTTP 行为和事件形状。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentEvent, AgentRuntime
from agent_harness.config import Settings
from agent_harness.session import (
    RUN_STARTED,
    SESSION_STARTED,
    USER_MESSAGE,
    Session,
)
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel


@pytest.fixture
def app(tmp_path):
    """用 tmp workspace 创建 app，避免污染真实 .agent/。"""
    settings = Settings(workspace_dir=str(tmp_path))
    return create_app(settings, enable_cors=False)


@pytest.fixture
def client(app):
    return TestClient(app)


# ── health ──


def test_health_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── sessions list ──


def test_list_sessions_empty(client):
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_sessions_returns_existing(tmp_path):
    """先往 store 写一个 session，列表应返回。"""
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store
    session = Session.start(store)
    session.append(event_type=SESSION_STARTED, data={"reason": "test"})
    client = TestClient(app)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["session_id"] == session.session_id
    assert body[0]["event_count"] >= 1


def test_list_sessions_carries_first_user_message(tmp_path):
    """Gap 3 (P0)：列表 payload 每行带首条 user/message content（截断 128），
    前端零额外请求渲染标题；无 user message 返回 null。"""
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store
    long_text = "帮我写一个 Python 函数" * 30  # 330 字符，应被截断到 128
    session = Session.start(store)
    session.append(event_type=SESSION_STARTED, data={"reason": "test"})
    session.append(event_type=USER_MESSAGE, data={"content": long_text})
    session.append(event_type=USER_MESSAGE, data={"content": "第二条不算"})
    empty = Session.start(store)  # 无任何 user/message 的 session
    client = TestClient(app)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    by_id = {row["session_id"]: row for row in resp.json()}
    first = by_id[session.session_id]["first_user_message"]
    assert first == long_text[:128]
    assert len(first) == 128
    assert by_id[empty.session_id]["first_user_message"] is None


# ── session events ──


def test_get_events_404(client):
    resp = client.get("/api/sessions/nonexistent/events")
    assert resp.status_code == 404


def test_get_events_ok(tmp_path):
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store
    session = Session.start(store)
    client = TestClient(app)
    resp = client.get(f"/api/sessions/{session.session_id}/events")
    assert resp.status_code == 200
    events = resp.json()
    assert isinstance(events, list)
    assert all("type" in e for e in events)
    assert any(e["type"] == SESSION_STARTED for e in events)


# ── create session (SSE) ──


def test_create_session_streams_sse(client):
    """POST /api/sessions 应返回 text/event-stream 并流多帧 AgentEvent。"""
    # 用 stub runtime 避免 LLM 调用：patch _build_runtime 返回一个轻量 runtime
    _settings = Settings(
        workspace_dir=str(client.app.state.agent.settings.workspace_dir)
    )
    reg = ToolRegistry()
    exe = ToolExecutor(reg)
    stub_runtime = AgentRuntime(
        model=ScriptedModel(responses=[AIMessage(content="hello from stub")]),
        registry=reg,
        executor=exe,
        max_steps=2,
    )
    with patch("agent_harness.web.app._build_runtime", return_value=stub_runtime):
        resp = client.post(
            "/api/sessions",
            json={"task": "say hi", "max_steps": 2},
        )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    body = resp.text
    # SSE 帧以 "data: " 开头；至少应有一个 model/completed
    frames = [line[len("data:"):].strip() for line in body.splitlines() if line.startswith("data:")]
    assert len(frames) > 0
    parsed = [json.loads(f) for f in frames]
    types = [e["type"] for e in parsed]
    assert "run/started" in types
    assert "model/completed" in types
    # 每个 durable event 都带 seq
    durable = [e for e in parsed if e.get("seq") is not None]
    assert durable, "expected at least one durable event with seq"
    # 每帧都带 session_id（前端依赖它在首帧就切换 selectedId）
    session_ids = {e.get("session_id") for e in parsed}
    assert len(session_ids) == 1, f"expected one consistent session_id, got {session_ids}"
    assert next(iter(session_ids)), "session_id must be non-null"
    # 每帧都带 time（issue #43：前端用事件真值时间，不再退化到客户端时钟）
    times = [e.get("time") for e in parsed]
    assert all(t for t in times), f"every SSE frame must carry a non-empty time, got {times}"
    # durable 事件的 time 应是 ISO 格式（由 SessionEvent.time 透传）
    durable_times = [e["time"] for e in parsed if e.get("seq") is not None]
    assert all("T" in t for t in durable_times), f"durable event times should be ISO, got {durable_times}"


# ── create session：workspace 安全边界（名字，不是路径）──
#
# POST /api/sessions 的 workspace 字段 V1 只接受单个路径段的目录名：
# 客户端若能直接传路径（绝对路径 / 盘符 / ".." 逃逸），Bash / Write 工具就会
# 以任意宿主目录为 sandbox 根执行——这是路径逃逸漏洞。所有拒绝必须发生在
# 任何 mkdir / Session 落盘之前，被拒请求不留任何痕迹。


def _assert_rejection_left_no_trace(app: Any) -> None:
    """被拒请求不应创建任何 workspace 目录或孤儿 session JSONL。"""
    assert list(app.state.agent.workspaces_root.iterdir()) == []
    assert list(app.state.agent.sessions_root.iterdir()) == []


def test_create_session_rejects_absolute_workspace_path(tmp_path):
    """绝对路径（workspaces_root 外 / 盘符路径）→ 422，且任何位置都不建目录。"""
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    client = TestClient(app)
    outside_abs = str(tmp_path / "outside-abs")  # workspace_dir 内、workspaces_root 外
    drive_abs = tmp_path.drive + "\\evil-drive-abs"  # 盘符绝对路径（如 D:\evil-drive-abs）
    for candidate in (outside_abs, drive_abs):
        resp = client.post("/api/sessions", json={"task": "hi", "workspace": candidate})
        assert resp.status_code == 422, f"{candidate!r} 应被 422 拒绝"
        assert not Path(outside_abs).exists()
        assert not Path(drive_abs).exists()
    _assert_rejection_left_no_trace(app)


def test_create_session_rejects_parent_escape_workspace(app):
    """"../escape" 这类 .. 逃逸 → 422。"""
    client = TestClient(app)
    resp = client.post("/api/sessions", json={"task": "hi", "workspace": "../escape"})
    assert resp.status_code == 422
    workspaces_root = app.state.agent.workspaces_root
    assert not (workspaces_root.parent / "escape").exists()
    _assert_rejection_left_no_trace(app)


def test_create_session_accepts_plain_workspace_name(client):
    """单段相对名 "my-task" 保持可用：200，目录建在 workspaces_root 下。"""
    stub_runtime = AgentRuntime(
        model=ScriptedModel(responses=[AIMessage(content="ok")]),
        registry=ToolRegistry(),
        executor=ToolExecutor(ToolRegistry()),
        max_steps=2,
    )
    with patch("agent_harness.web.app._build_runtime", return_value=stub_runtime):
        resp = client.post("/api/sessions", json={"task": "hi", "workspace": "my-task"})
    assert resp.status_code == 200
    assert (client.app.state.agent.workspaces_root / "my-task").is_dir()


def test_create_session_rejects_nested_workspace_name(app):
    """多段名 "a/b"（含反斜杠 "a\\\\b"）→ 422。

    V1 取最简单的安全规则：只收单段名字。多段相对路径虽然解析后可能仍在
    workspaces_root 内，但放行它等于维护一套路径语义——没有必要（接缝点：
    未来要嵌套时再显式放开）。
    """
    client = TestClient(app)
    for candidate in ("a/b", "a\\b"):
        resp = client.post("/api/sessions", json={"task": "hi", "workspace": candidate})
        assert resp.status_code == 422, f"{candidate!r} 应被 422 拒绝"
    _assert_rejection_left_no_trace(app)


# ── create session：请求体校验（FastAPI/pydantic 自动 422）──


def test_create_session_rejects_empty_task(tmp_path):
    """task="" → 422，且不留任何 session JSONL / workspace 目录。"""
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    client = TestClient(app)
    resp = client.post("/api/sessions", json={"task": ""})
    assert resp.status_code == 422
    _assert_rejection_left_no_trace(app)


def test_create_session_rejects_invalid_max_steps(tmp_path):
    """max_steps=0（非正数）和 1000（超上限 200）→ 422，且不留任何落盘痕迹。"""
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    client = TestClient(app)
    for max_steps in (0, 1000):
        resp = client.post("/api/sessions", json={"task": "hi", "max_steps": max_steps})
        assert resp.status_code == 422, f"max_steps={max_steps} 应被 422 拒绝"
    _assert_rejection_left_no_trace(app)


# ── AppState.shutdown 与惰性装配的竞争（FIX 3）──
#
# get_wiring() 惰性装配 Capability 子系统；shutdown() 若不拿 _wiring_lock 就把
# _wiring 换成 None，一个在途的 get_wiring 可以在 shutdown 之后才完成装配——
# 装配出的 wiring 永远不会被 close（泄露 Milvus / embedding 连接）。契约：
# 在途装配以 RuntimeError 失败，但装配产物仍由 shutdown 关闭（恰好一次）。


@pytest.mark.asyncio
async def test_shutdown_closes_in_flight_wiring_exactly_once(tmp_path, monkeypatch):
    """shutdown 与在途 get_wiring 竞争：在途调用 RuntimeError，wiring 仍被关闭。"""
    import agent_harness.web.app as web_app

    settings = Settings(workspace_dir=str(tmp_path))
    state = web_app.AppState(settings)

    close_calls: list[str] = []

    class _StubMemory:
        async def close(self) -> None:
            close_calls.append("closed")

    class _StubWiring:
        def __init__(self) -> None:
            self.context_providers = []
            self.tools = []
            self.memory_writer = None
            self.memory = _StubMemory()

    wire_started = asyncio.Event()
    release_wire = asyncio.Event()

    async def slow_wire(registry, config, settings=None):
        wire_started.set()
        await release_wire.wait()  # 模拟慢装配（真实场景是连 Milvus / embedding）
        return _StubWiring()

    monkeypatch.setattr(web_app, "wire_capabilities", slow_wire)

    wiring_task = asyncio.create_task(state.get_wiring())
    await asyncio.wait_for(wire_started.wait(), timeout=2.0)
    shutdown_task = asyncio.create_task(state.shutdown())
    await asyncio.sleep(0.01)  # 让 shutdown 置位关闭标记并阻塞在 _wiring_lock 上
    release_wire.set()

    with pytest.raises(RuntimeError, match="AppState is shut down"):
        await wiring_task
    await asyncio.wait_for(shutdown_task, timeout=2.0)
    # 在途装配的 wiring 必须被 shutdown 拿去关闭——不能静默泄露连接。
    assert close_calls == ["closed"]
    # 幂等：重复 shutdown 不重复 close。
    await state.shutdown()
    assert close_calls == ["closed"]


@pytest.mark.asyncio
async def test_get_wiring_after_shutdown_raises(tmp_path):
    """直接用例：shutdown 之后再 get_wiring → RuntimeError（绝不新装配）。"""
    from agent_harness.web.app import AppState

    state = AppState(Settings(workspace_dir=str(tmp_path)))
    await state.shutdown()
    with pytest.raises(RuntimeError, match="AppState is shut down"):
        await state.get_wiring()


# ── 读端点的磁盘 I/O 卸载（FIX 4）──
#
# list_sessions / get_session_events 直接内联调用同步的 JsonlSessionStore 读，
# 一份大 JSONL 就能卡住整个事件循环。契约：store 读走 anyio.to_thread 卸载，
# 响应形状不变（不加分页）。


def test_read_endpoints_with_multi_event_session(tmp_path):
    """回归锚：多事件 session 下两个读端点仍 200 且响应形状不变。"""
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store
    session = Session.start(store)
    for i in range(5):
        session.append(event_type=USER_MESSAGE, data={"content": f"msg {i}"})
    client = TestClient(app)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["session_id"] == session.session_id)
    assert row["event_count"] == 6  # session/started + 5 条 user/message
    resp = client.get(f"/api/sessions/{session.session_id}/events")
    assert resp.status_code == 200
    assert [e["type"] for e in resp.json()] == [SESSION_STARTED] + [USER_MESSAGE] * 5


def test_read_endpoints_offload_store_reads_off_event_loop(tmp_path, monkeypatch):
    """store 的同步读必须发生在 worker 线程上（不在事件循环线程内联执行）。

    探针：read_events 替身里 asyncio.get_running_loop() 在 worker 线程上抛
    RuntimeError（没有 running loop）；若在事件循环线程内联调用，它会成功——
    以此区分「卸载了」和「没卸载」。
    """
    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store
    session = Session.start(store)
    session.append(event_type=USER_MESSAGE, data={"content": "hi"})

    real_read_events = store.read_events

    def spy_read_events(session_id):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return real_read_events(session_id)  # worker 线程：符合契约
        raise AssertionError(
            "read_events 在事件循环线程内联执行——同步磁盘 I/O 未卸载，大日志会卡住 loop"
        )

    monkeypatch.setattr(store, "read_events", spy_read_events)

    client = TestClient(app)
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    resp = client.get(f"/api/sessions/{session.session_id}/events")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_create_session_sse_emits_run_failed_on_model_error(client):
    """模型抛异常时 SSE 不能静默断流（FIX 5 集成锚）。

    runtime 顶层兜底（agent/runtime.py 新契约）补 model/failed + run/failed
    终结帧后正常 return——SSE 消费者必须收到 200 + 完整终止帧，而不是连接
    半途而死。这里用真实 AgentRuntime + 空剧本模型（首次调用即抛 RuntimeError）。
    """
    stub_runtime = AgentRuntime(
        model=ScriptedModel(responses=[]),  # 空剧本 → 首次模型调用即抛 RuntimeError
        registry=ToolRegistry(),
        executor=ToolExecutor(ToolRegistry()),
        max_steps=2,
    )
    with patch("agent_harness.web.app._build_runtime", return_value=stub_runtime):
        resp = client.post("/api/sessions", json={"task": "boom", "max_steps": 2})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    frames = [line[len("data:"):].strip() for line in resp.text.splitlines() if line.startswith("data:")]
    parsed = [json.loads(f) for f in frames]
    types = [e["type"] for e in parsed]
    assert "run/failed" in types, f"SSE 流缺少 run/failed 终结帧，实际事件：{types}"
    assert types[-1] == "run/failed", f"run/failed 必须是最后一帧，实际：{types}"
    # 模型在途抛错必须归因到 model/failed（供 resume / 审计区分故障源）。
    assert "model/failed" in types, f"模型在途失败应补 model/failed，实际事件：{types}"


# ── approve seam ──


def test_approve_endpoint_returns_seam(client):
    resp = client.post("/api/sessions/abc/approve", json={"approved": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "received"
    assert "auto-approve" in body["note"]


# ── client disconnect → producer 取消（spec 11 §4）──
#
# 断连契约：客户端断开连接后，server 端必须让 SSE producer 收到取消信号，
# 不能让 runtime.run_stream() 的 async generator 永远挂住——否则在真实 uvicorn 下
# 是泄漏的 task + 持有中的 session 锁。EventSourceResponse 内部把 disconnect 翻译成
# generator 的 GeneratorExit / CancelledError，本测试用 stub runtime 观察 finally 触发。
#
# 不能用 starlette TestClient / httpx ASGITransport：两者都是 await app(...) 整体跑完
# 才返回，且 http.disconnect 只在 response 完成后才投递——挂住的 producer 会直接死锁。
# 下面是一个最小 ASGI 替身：像真实 uvicorn 一样，客户端断开时在 receive 通道投递
# http.disconnect，EventSourceResponse 收到后取消它 spawn 出来的 event_generator task。


class _DisconnectingASGI:
    """模拟 uvicorn 在客户端中途断连时的 ASGI 行为。

    只实现能驱动 EventSourceResponse 跑起来的最小路径：app 发出 http.response.start
    后，置位 disconnected，下一次 receive() 投递 http.disconnect。
    receive/send 参数按 ASGI 签名保留但弃用——替身内部自产自销这两个通道。
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict) -> None:
        request_sent = asyncio.Event()
        disconnected = asyncio.Event()

        async def downstream_receive() -> dict:
            # 先把 request body 一次性发完（POST /api/sessions 有 JSON body），
            # 之后等 start 发出、disconnected 置位，再投递 http.disconnect。
            if not request_sent.is_set():
                request_sent.set()
                return {"type": "http.request", "body": b'{"task":"hi"}', "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def upstream_send(message: dict) -> None:
            # 首帧一出就触发断连——给 app 一点时间让 EventSourceResponse 进入
            # 它的接收循环，然后置位。丢弃 body（我们不关心内容）。
            if message["type"] == "http.response.start":
                await asyncio.sleep(0)  # 让控制权回到事件循环
                disconnected.set()

        app_task = asyncio.create_task(self._app(scope, downstream_receive, upstream_send))
        try:
            await asyncio.wait_for(app_task, timeout=5.0)
        except TimeoutError:
            app_task.cancel()
            with contextlib.suppress(BaseException):
                await app_task
            raise AssertionError(
                "app 未在 5s 内响应 disconnect——EventSourceResponse 没有正确取消 producer"
            )


@pytest.mark.asyncio
async def test_disconnect_cancels_sse_producer(tmp_path):
    """客户端断连后 SSE producer 必须被取消（spec 11 §4 第 1-3 条）。

    用一个永不自然完成的 stub runtime：它只在被取消（GeneratorExit/CancelledError）
    时置位 finally 旗标。如果断连信号没传到 generator，旗标永远是 False，断言失败。
    """

    class HangingRuntime:
        """模拟真实 runtime：先落盘一条事实事件，再挂起等模型流；只有被取消才退 finally。"""

        def __init__(self) -> None:
            self.cancelled = asyncio.Event()
            self.persisted_session_id: str | None = None

        async def run_stream(self, session: Any, task: str) -> AsyncIterator[AgentEvent]:
            self.persisted_session_id = session.session_id
            session.append(event_type=RUN_STARTED, data={"reason": "disconnect-test"})
            try:
                await asyncio.Event().wait()  # 永不 set，模拟 agent loop 永远在等模型
                yield AgentEvent(type=RUN_STARTED)  # pragma: no cover — 永不执行到这里
            finally:
                self.cancelled.set()

    settings = Settings(workspace_dir=str(tmp_path))
    app = create_app(settings, enable_cors=False)

    hanging = HangingRuntime()
    with patch("agent_harness.web.app._build_runtime", return_value=hanging):
        transport = _DisconnectingASGI(app)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/sessions",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "query_string": b"",
        }
        await transport(scope)

    # 断言 1（§4 第 2/3 条）：producer 的 finally 已触发——断连信号确实传到了 generator。
    assert hanging.cancelled.is_set(), (
        "客户端断连后 SSE producer 未被取消——spec 11 §4 的 disconnect 清理未实现，"
        "这会在真实 uvicorn 下泄漏 task + 持有 session 锁。"
    )

    # 断言 2（§4 第 4 条）：断连清理不破坏 Session 一致性——取消前已落盘的事实
    # 必须完整可读、可重建，清理路径不得增删改持久化事件。
    # 2 条 = Session.start 的 session/started 初始事实 + stub 落盘的 run/started。
    assert hanging.persisted_session_id, "runtime 未创建 session"
    events = app.state.agent.store.read_events(hanging.persisted_session_id)
    assert len(events) == 2, f"断连后 session 事实应恰好 2 条，实际 {len(events)}"
    assert [e.type for e in events] == [SESSION_STARTED, RUN_STARTED]
