"""#566 kill 测试子进程（窗口 B）：**批准已落盘、接纳建账之前**真崩溃。

不是 pytest 收集对象（文件名不带 test_ 前缀）。与 `_approval_kill_child.py`
同型：生产接线的 run + 剧本模型，唯一差别是审批 callback **立即批准**（与生产
holder 同样把 `tool/approval-requested` / `permission/resolved` 成对落盘），
并且 ToolExecutor 被替换为在接纳点（`_create_pending_operation`，Ledger 建账
那一刻）直接 `os._exit(9)` 的子类——kill 窗口落在 resolved 落盘之后、Ledger
建账之前（`04 §9.1` 顺序：审批闸门 → 接纳点 → execute）。

**前提自查**：resolved(approve) 必须 durable、Ledger 必须无账、副作用必须未
发生——任一不成立以 rc=3 退出，父进程不接受假现场。
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
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import PERMISSION_RESOLVED, TOOL_APPROVAL_REQUESTED
from agent_harness.storage import SqliteOperationLedger
from agent_harness.tooling import (
    Tool,
    ToolExecutor,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)
from agent_harness.tooling.approval import ApprovalResponse, PermissionDecision

CALL_ID = "call-window-a"
TOOL_NAME = "danger"
SENTINEL_NAME = "mutation.log"
CRASH_EXIT_CODE = 9
PREMISE_EXIT_CODE = 3


class _NoArgs(BaseModel):
    pass


class _DangerousTool(Tool):
    """DANGER 工具：被执行即留标记（用于证明"没跑过"）。"""

    def __init__(self, sentinel: Path) -> None:
        self._sentinel = sentinel

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return "高危操作：执行前必须人工审批（本场景立即批准）。"

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


class _AutoApproveCallback:
    """立即批准的审批 callback：与生产 holder 同样把事件对落 durable 流。"""

    def __init__(self) -> None:
        self._session: Session | None = None

    def bind_session(self, session: Session) -> None:
        self._session = session

    async def __call__(self, req) -> ApprovalResponse:
        approval_id = "approval-window-b"
        self._session.append(
            TOOL_APPROVAL_REQUESTED,
            {
                "approval_id": approval_id,
                "tool_name": req.tool_name,
                "tool_call_id": req.tool_call_id,
                "action_type": req.permission.value,
                "permission": req.permission.value,
                "policy": req.policy.value,
                "reason": req.reason,
            },
        )
        self._session.append(
            PERMISSION_RESOLVED,
            {
                "approval_id": approval_id,
                "decision": PermissionDecision.APPROVE_ONCE.value,
                "reason": "kill 窗口注入：立即批准",
            },
        )
        return ApprovalResponse(
            approved=True,
            reason="kill 窗口注入：立即批准",
            decision=PermissionDecision.APPROVE_ONCE,
        )


class _KillBeforeAdmission(ToolExecutor):
    """在接纳点（Ledger 建账）处硬杀：resolved 已落盘、账还没有行的窗口。"""

    def __init__(self, *args, store: JsonlSessionStore, session_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._store = store
        self._session_id = session_id

    async def _create_pending_operation(self, **kwargs) -> None:
        events = self._store.read_events(self._session_id)
        approved = any(
            e.type == PERMISSION_RESOLVED
            and e.data.get("decision") == PermissionDecision.APPROVE_ONCE.value
            for e in events
        )
        if not approved:
            _fail_premise("接纳点已到但 resolved(approve) 未落盘")
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        os._exit(CRASH_EXIT_CODE)


async def _main() -> None:
    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    session_id = str(config["session_id"])

    store = JsonlSessionStore(root / "sessions")
    ledger = SqliteOperationLedger(root / "state.db")
    await ledger.initialize()
    registry = ToolRegistry()
    registry.register(_DangerousTool(root / SENTINEL_NAME))

    callback = _AutoApproveCallback()
    session = Session.start(store, session_id=session_id)
    callback.bind_session(session)
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
        executor=_KillBeforeAdmission(
            registry,
            approval_callback=callback,
            operation_ledger=ledger,
            store=store,
            session_id=session_id,
        ),
        max_agent_turns=10,
    )
    await runtime.run(session, "执行高危操作")
    _fail_premise("run 正常结束——kill 窗口没有命中")


asyncio.run(_main())
