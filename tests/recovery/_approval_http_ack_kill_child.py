"""#553 实验子进程：收到 /approve 决策后，在 callback 继续前等待父进程硬杀。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import BaseModel

from agent_harness.tooling import Tool, ToolPermission, ToolResult
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.contract import ToolSideEffect


class _BashArgs(BaseModel):
    command: str


class _ObservedBash(Tool):
    """替代真实 bash；若审批屏障意外放行，写出可观察的执行标记。"""

    marker: Path

    def __init__(self, sandbox, *, timeout_seconds: float | None = None) -> None:
        pass

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "测试用高危工具；只有审批回调返回后才允许执行。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _BashArgs

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    async def execute(self, args: BaseModel) -> ToolResult:
        self.marker.write_text("executed\n", encoding="utf-8")
        return ToolResult.success("executed")


class _ApprovalModel:
    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        if not any(getattr(message, "tool_calls", None) for message in messages):
            yield AIMessage(content="", tool_calls=[{
                "name": "bash",
                "args": {"command": "echo approval-barrier"},
                "id": "call-approval-http-ack",
                "type": "tool_call",
            }])
        else:
            yield AIMessageChunk(content="done")


async def _main() -> None:
    import uvicorn

    from agent_harness import assembly
    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    _ObservedBash.marker = root / "tool-executed.txt"
    original_bash = assembly.BashTool
    assembly.BashTool = _ObservedBash
    assembly.BUILTIN_LOCAL_TOOLS = tuple(
        _ObservedBash if tool_cls is original_bash else tool_cls
        for tool_cls in assembly.BUILTIN_LOCAL_TOOLS
    )
    assembly.create_chat_model = lambda _config, **_kwargs: _ApprovalModel()

    original_wait_for = PendingApprovalQueue.wait_for

    async def _barrier_after_resolution(self, approval_id, timeout=None):
        response = await original_wait_for(self, approval_id, timeout=timeout)
        print(f"BARRIER:{approval_id}", flush=True)
        await asyncio.Event().wait()
        return response

    PendingApprovalQueue.wait_for = _barrier_after_resolution

    app = create_app(Settings(
        _env_file=None,
        workspace_dir=str(root),
        model_api_key="sk-test",
        enable_cors=False,
    ))
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="error",
        access_log=False,
        lifespan="on",
    ))
    server_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        if server_task.done():
            await server_task
        await asyncio.sleep(0.05)
    if not server.started:
        raise RuntimeError("Uvicorn did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    print(f"READY:{port}", flush=True)
    await server_task


asyncio.run(_main())
