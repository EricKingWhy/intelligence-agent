"""#337 kill 测试子进程：在**审批等待中**真崩溃，留下"无决议审批请求"现场。

不是 pytest 收集对象（文件名不带 test_ 前缀）。父进程用 `sys.executable` 启动它，
子进程跑一次**生产接线**的 run：`AgentRuntime` + `ToolExecutor`（默认
WORKSPACE_WRITE policy）+ DANGER 工具 + `InteractiveCallbackHolder`（无限等待）
+ `SqliteOperationLedger` + `JsonlSessionStore`。唯一的替身是剧本模型。

崩溃窗口：审批闸门在接纳点**之前**（executor.py 阶段 2.5），等待期间 durable 流上
已有 `tool/call` 与 `tool/approval-requested`，而 Ledger 里**还没有** Operation。
子进程轮询 durable 事件流，看到请求落盘后立刻 `os._exit(9)`——`os._exit` 不走
finally / 异常臂，取消路径（34a7a1d）的 fail-closed 结清因此**不会**发生，磁盘上
留下的就是"进程死掉那一刻"的样子：一条永远等不到配对的审批请求。

**前提自查**：请求事件必须 durable、副作用必须未发生、Ledger 必须无账（审批在
接纳点之前）——任一不成立以 rc=3 退出，父进程不接受假现场。

argv[1] 是 JSON：`{"root": <目录>, "session_id": <str>}`。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.model.scripted import ScriptedModel
from agent_harness.session import (
    JsonlSessionStore,
    Session,
)
from agent_harness.session.approval import InteractiveCallbackHolder
from agent_harness.session.event import TOOL_APPROVAL_REQUESTED
from agent_harness.storage import SqliteOperationLedger
from agent_harness.tooling import (
    Tool,
    ToolExecutor,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)
from agent_harness.tooling.approval_queue import PendingApprovalQueue

CALL_ID = "call-approval-restart"
TOOL_NAME = "danger"
SENTINEL_NAME = "mutation.log"
CRASH_EXIT_CODE = 9
PREMISE_EXIT_CODE = 3
POLL_SECONDS = 10.0


class _NoArgs(BaseModel):
    pass


class _DangerousTool(Tool):
    """DANGER 工具：默认 policy 下必触发审批；被执行即留标记（用于证明"没跑过"）。"""

    def __init__(self, sentinel: Path) -> None:
        self._sentinel = sentinel

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return "高危操作：执行前必须人工审批。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER

    async def execute(self, args: BaseModel) -> ToolResult:
        with self._sentinel.open("a", encoding="utf-8") as handle:
            handle.write("mutated\n")
        return ToolResult.success("mutated")


def _fail_premise(detail: str) -> None:
    sys.stderr.write(f"前提不成立：{detail}\n")
    sys.exit(PREMISE_EXIT_CODE)


async def _main() -> None:
    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    session_id = str(config["session_id"])

    store = JsonlSessionStore(root / "sessions")
    ledger = SqliteOperationLedger(root / "state.db")
    await ledger.initialize()
    registry = ToolRegistry()
    registry.register(_DangerousTool(root / SENTINEL_NAME))

    # 无限等待（timeout_seconds=0 → None）：审批永远等不到决策，run 卡在等待里。
    holder = InteractiveCallbackHolder(
        queue=PendingApprovalQueue(), timeout_seconds=0
    )
    session = Session.start(store, session_id=session_id)
    holder.bind_session(session)
    runtime = AgentRuntime(
        model=ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"id": CALL_ID, "name": TOOL_NAME, "args": {}}],
                ),
                AIMessage(content="done"),
            ]
        ),
        registry=registry,
        executor=ToolExecutor(
            registry, approval_callback=holder, operation_ledger=ledger
        ),
        max_agent_turns=10,
    )
    _run_task = asyncio.create_task(runtime.run(session, "执行高危操作"))

    # 轮询 durable 事件流：审批请求落盘的那一刻就是合法崩溃点（durable 即证据）。
    seen = False
    for _ in range(int(POLL_SECONDS / 0.05)):
        await asyncio.sleep(0.05)
        events = store.read_events(session_id)
        if any(e.type == TOOL_APPROVAL_REQUESTED for e in events):
            seen = True
            break
    if not seen:
        _fail_premise(f"{POLL_SECONDS}s 内 tool/approval-requested 未落盘")
    if (root / SENTINEL_NAME).exists():
        _fail_premise("副作用已发生——崩溃点不再是'审批等待中'")

    sys.stdout.write("READY\n")
    sys.stdout.flush()
    # os._exit 不走 finally / 异常臂：取消路径的 fail-closed 结清不会发生。
    os._exit(CRASH_EXIT_CODE)


asyncio.run(_main())
