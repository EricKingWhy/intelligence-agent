"""HTTP 层 SSE 断连 → run 取消的端到端契约（批次 0，前端实测回执驱动）。

前端回执（2026-09-05）报告"断连后 run 继续跑到 max_steps、run/failed 无
reason='cancelled'"。经真 uvicorn + 真连接中断复现排查：当前代码在 run 在途
与工具执行中断连都正确取消（本文件两测试）；该回执现象与旧后端进程（取消臂
1cfe795 之前启动）或客户端停止读取但不 abort 连接的行为吻合。此测试钉住
HTTP 层契约，防止装配/中间件层演进时回归。
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import BaseModel

from agent_harness.session import JsonlSessionStore
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint


class SlowStreamModel:
    """20 chunk × 0.1s ≈ 2s 慢流：断连发生时 run 必然在途。"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        for i in range(20):
            await asyncio.sleep(0.1)
            yield AIMessageChunk(content=f"chunk{i} ")


class _SlowArgs(BaseModel):
    command: str


class SlowBashTool(Tool):
    """3s 慢工具：复现前端"工具执行中断连"的精确场景。"""

    def __init__(self, sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "slow bash"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _SlowArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WORKSPACE_WRITE

    @property
    def timeout_seconds(self) -> float:
        return 30.0

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(verifiable=False)

    async def execute(self, args: BaseModel) -> ToolResult:
        await asyncio.sleep(3)
        return ToolResult.success("done")


async def _start_server(tmp_path, monkeypatch, model):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    monkeypatch.setattr(
        "agent_harness.web.app.create_chat_model", lambda config: model())
    app = create_app(Settings(
        _env_file=None, workspace_dir=str(tmp_path),
        model_api_key="sk-test", enable_cors=False,
    ))
    uv_config = uvicorn.Config(app, host="127.0.0.1", port=0,
                               log_level="error", lifespan="on")
    server = uvicorn.Server(uv_config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn 未能在预期时间内启动"
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, serve_task, port


async def _read_until(line_marker: str, port: int) -> None:
    """读 SSE 流直到出现标记帧，然后立刻断开连接——不排空流。"""
    import httpx2

    client = httpx2.AsyncClient(timeout=None)
    try:
        async with client.stream(
            "POST", f"http://127.0.0.1:{port}/api/sessions",
            json={"task": "慢任务"},
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line_marker in line:
                    break
    finally:
        await client.aclose()


async def _wait_for_cancelled(tmp_path, deadline_seconds: float = 6.0) -> None:
    """断连后 run/failed(reason=cancelled) 必须在期限内落盘。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    async with asyncio.timeout(deadline_seconds):
        while True:
            ids = store.list_session_ids()
            if ids:
                events = store.read_events(ids[0])
                if any(e.type == "run/failed"
                       and (e.data or {}).get("reason") == "cancelled"
                       for e in events):
                    return
            await asyncio.sleep(0.1)


async def _shutdown(server, serve_task) -> None:
    server.should_exit = True
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_client_disconnect_mid_run_cancels(tmp_path, monkeypatch):
    """run 在途（流式 delta 已开始）时客户端断连 → run/failed(reason=cancelled)。"""
    server, serve_task, port = await _start_server(
        tmp_path, monkeypatch, SlowStreamModel)
    try:
        await _read_until("model/delta", port)
        await _wait_for_cancelled(tmp_path)
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_client_disconnect_during_tool_execution_cancels(tmp_path, monkeypatch):
    """工具执行中（前端实测的精确场景）断连 → 同样取消，不跑到自然结束。

    ScriptedModel 先发 tool_call（进入 3s 慢工具），断连落在 execute_batch
    在途窗口；若取消失效，run 会继续走完第二个响应（run/completed），
    _wait_for_cancelled 超时变红。
    """
    scripted = ScriptedToolThenDone()
    server, serve_task, port = await _start_server(
        tmp_path, monkeypatch, lambda: scripted)
    try:
        monkeypatch.setattr("agent_harness.web.app.BashTool", SlowBashTool)
        await _read_until("tool/call", port)
        await _wait_for_cancelled(tmp_path)
    finally:
        await _shutdown(server, serve_task)


class ScriptedToolThenDone:
    """第一轮发 tool_call，第二轮给最终回答（取消失效时的"自然结束"路径）。"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        if not any(getattr(m, "tool_calls", None) for m in messages):
            yield AIMessage(content="", tool_calls=[{
                "name": "bash", "args": {"command": "slow"}, "id": "call1",
                "type": "tool_call",
            }])
        else:
            yield AIMessageChunk(content="done")
