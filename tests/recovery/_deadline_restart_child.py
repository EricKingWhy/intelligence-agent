"""T7 #315 kill 测试子进程：在**真实 deadline 边界**上崩溃，留下两种 mutating 结局。

不是 pytest 收集对象（文件名不带 test_ 前缀）。父进程用 `sys.executable` 启动它，
子进程跑一次**生产接线**的 run（`AgentRuntime` + `ToolExecutor` + `SqliteOperationLedger`
+ `JsonlSessionStore`），唯一的替身是剧本模型（本文件考的是磁盘上的 durable 状态，
不是模型；真实 Provider 的那一份证据在 `#315` 的 Live Gate 场景里）。

两种结局只差**执行器有没有来得及写终态**——正是票面要的"已知 / 未知 mutating 结果"：

- `mode=known`：mutate 工具跨过 deadline 之后落副作用并**正常返回**，执行器把这次
  调用落成 SUCCEEDED、runtime 把 tool/result 写进 JSONL；下一个边界检查读到 deadline
  已到 ⇒ 落 `run/paused(reason=deadline)`。子进程确认暂停与账都 durable 之后
  `os._exit(9)`——"重启前进程死掉"。
- `mode=unknown`：同一个工具在副作用**落盘之后、返回之前**被 `os._exit(9)` 掐死——
  世界状态未知，账上只剩一行 `RUNNING`。

两边都用真实挂钟：deadline 由**子进程自己**按 `deadline_seconds` 算出并先打到 stdout
（父进程据此等过这个时刻），工具 hold 时长 > 该偏移量，所以"在途调用跨过 deadline"
不是靠 monkeypatch 出来的——在途那一次按自己的 timeout 语义收尾（`#315` R2），
死的是"下一次接纳"（R1）。

**前提自查**：两种模式都验证自己的假设成立（known 必须有暂停 + SUCCEEDED + 一条
tool/result；unknown 必须死在工具里），不成立就以 rc=3 退出——父进程宁可看到"现场
不对"，也不接受一份被当成证据的假现场。

argv[1] 是 JSON：`{"root": <目录>, "mode": "known"|"unknown", "session_id": <str>,
"deadline_seconds": <float>, "hold_seconds": <float>}`。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.agent.budget import SOURCE_DEPLOYMENT
from agent_harness.agent.run_budget import (
    REASON_DEADLINE,
    BudgetConsumed,
    LaunchRunBudget,
    RunLimits,
)
from agent_harness.model.scripted import ScriptedModel
from agent_harness.session import (
    RUN_PAUSED,
    TOOL_RESULT,
    JsonlSessionStore,
    Session,
)
from agent_harness.storage import OperationState, SqliteOperationLedger
from agent_harness.tooling import (
    Tool,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
)

CALL_ID = "call-deadline-restart"
TOOL_NAME = "mutate"
SENTINEL_NAME = "mutation.log"
CRASH_EXIT_CODE = 9
PREMISE_EXIT_CODE = 3


class _NoArgs(BaseModel):
    pass


class _MutateTool(Tool):
    """生产形状的 mutating 工具：副作用**一行一次**（追加写——重跑会多出一行）。"""

    def __init__(self, sentinel: Path, mode: str, hold_seconds: float) -> None:
        self._sentinel = sentinel
        self._mode = mode
        self._hold = hold_seconds

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return "把一行标记追加进 mutation.log（副作用）；返回即表示副作用已完成。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    async def execute(self, args: BaseModel) -> ToolResult:
        # 跨过 deadline 再落副作用：这一次是在途调用（R2），收不收得了尾由它自己的
        # timeout 决定；deadline 禁止的是**下一次**接纳（R1）。
        await asyncio.sleep(self._hold)
        with self._sentinel.open("a", encoding="utf-8") as handle:
            handle.write("mutated\n")
        if self._mode == "unknown":
            # 副作用已发生、结论永远不会回填：进程在这里真死。
            sys.stdout.write("READY\n")
            sys.stdout.flush()
            os._exit(CRASH_EXIT_CODE)
        return ToolResult.success("mutated")


def _tool_call_message() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": CALL_ID, "name": TOOL_NAME, "args": {}}],
    )


def _fail_premise(detail: str) -> None:
    sys.stderr.write(f"前提不成立：{detail}\n")
    sys.exit(PREMISE_EXIT_CODE)


async def _main() -> None:
    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    mode = str(config["mode"])
    # deadline 由本进程的挂钟算出并**先**打出去：父进程据此等过这个时刻，
    # 两边的"到了没有"因此是同一个时钟的读数（不跨进程比时钟）。
    deadline = datetime.now(UTC) + timedelta(seconds=float(config["deadline_seconds"]))
    sys.stdout.write(f"DEADLINE {deadline.isoformat()}\n")
    sys.stdout.flush()

    store = JsonlSessionStore(root / "sessions")
    ledger = SqliteOperationLedger(root / "state.db")
    await ledger.initialize()
    registry = ToolRegistry()
    registry.register(_MutateTool(root / SENTINEL_NAME, mode, float(config["hold_seconds"])))
    session = Session.start(store, session_id=config["session_id"])
    runtime = AgentRuntime(
        model=ScriptedModel(responses=[_tool_call_message(), AIMessage(content="done")]),
        registry=registry,
        executor=ToolExecutor(registry, operation_ledger=ledger),
        max_agent_turns=10,
        local_fuse_source=SOURCE_DEPLOYMENT,
        run_budget=LaunchRunBudget(
            version=1,
            limits=RunLimits(deadline_at=deadline),
            consumed=BudgetConsumed(),
        ),
    )
    await runtime.run(session, "把标记写进 mutation.log")

    events = store.read_events(session.session_id)
    if mode == "unknown":
        _fail_premise(f"unknown 模式没有死在工具里：{[e.type for e in events]}")
    pauses = [event for event in events if event.type == RUN_PAUSED]
    if len(pauses) != 1 or pauses[0].data.get("reason") != REASON_DEADLINE:
        _fail_premise(f"没有按 deadline 暂停：{[e.type for e in events]}")
    results = [
        event
        for event in events
        if event.type == TOOL_RESULT and event.data.get("tool_call_id") == CALL_ID
    ]
    if len(results) != 1:
        _fail_premise(f"已知结局应当有且只有一条 tool/result，实际 {len(results)}")
    operation = await ledger.get(session.session_id, CALL_ID)
    if operation is None or operation.state is not OperationState.SUCCEEDED:
        _fail_premise(f"已知结局的账应当是 SUCCEEDED，实际 {operation and operation.state}")
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    os._exit(CRASH_EXIT_CODE)


asyncio.run(_main())
