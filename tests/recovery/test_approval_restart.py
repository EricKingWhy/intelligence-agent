"""#337 kill/restart：审批等待中真崩溃 → 恢复层 fail-closed 结清 → 下一次 run 完成。

T8 `#316` 残余 ⑩：进程重启后留下的"无决议审批请求"没有任何写入方能结清（审批
队列纯内存；/approve 对不存在的 id 404），完成闸门谓词 2（`02 §5.4`）把该会话
永久挡在 quiescence_blocked——用户那个会话就废了。本文件用**真崩溃**（子进程
`_approval_kill_child.py` 的 `os._exit(9)`）钉住修复的完整链条：

- 崩溃现场（durable）：`tool/call` + `tool/approval-requested` 无配对；Ledger 无账
  （审批闸门在接纳点之前）；副作用未发生。
- 恢复：`RecoveryCoordinator` 把请求结清为**恰好一条** `permission/resolved(deny)`
  （reason 稳定）；谓词 2 从真变假，且只因那一条事件；零副作用、零重跑。
- 会话复活：结清之后的下一次 run 达成 `run/completed`——不是靠放宽谓词
  （`unresolved_approval_ids` 一行未改，判据与闸门共用同一份）。

HTTP / CLI 面的行为不在此处（web 层恢复入口由 `tests/web/` 既有套件钉住）；
本文件只钉 durable 判定与"重启之后还能不能自己站起来"。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.model.scripted import ScriptedModel
from agent_harness.recovery import RECOVERY_STALE_APPROVAL_REASON, RecoveryCoordinator
from agent_harness.session import (
    RUN_COMPLETED,
    JsonlSessionStore,
    detect_dangling,
)
from agent_harness.session.approval import unresolved_approval_ids
from agent_harness.session.event import PERMISSION_RESOLVED, TOOL_RESULT
from agent_harness.storage import SqliteOperationLedger
from agent_harness.tooling import (
    Tool,
    ToolExecutor,
    ToolPermission,
    ToolRegistry,
    ToolResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD = Path(__file__).with_name("_approval_kill_child.py")

#: 与子进程的 `CRASH_EXIT_CODE` 一致。子进程 import 即执行（模块末行是 `asyncio.run`），
#: 所以常量只能抄一份，不能 import 它。
_CRASH_EXIT_CODE = 9

SESSION_ID = "sess-approval-restart"
CALL_ID = "call-approval-restart"
TOOL_NAME = "danger"


class _NoArgs(BaseModel):
    pass


class _CountingDangerTool(Tool):
    """与子进程同名的 DANGER 工具：被调用即记数（用于证明"没有重跑"）。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return "不该被执行：重启后的判定只许读事件流与账，不许重跑副作用。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER

    async def execute(self, args: BaseModel) -> ToolResult:
        self.calls += 1
        return ToolResult.success("rerun")


def _crash(tmp_path: Path) -> None:
    """跑一次"在审批等待中真崩溃"的子进程，并校验现场前提。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(_CHILD), json.dumps({
            "root": str(tmp_path), "session_id": SESSION_ID,
        })],
        timeout=120,
        env=env,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == _CRASH_EXIT_CODE, (
        f"子进程没有按预期崩溃（rc={completed.returncode}）：{completed.stderr}"
    )
    assert "READY" in completed.stdout, completed.stdout
    assert completed.stderr == "", "子进程的前提自查报了错——现场不成立"


async def _open(tmp_path: Path) -> tuple[JsonlSessionStore, SqliteOperationLedger]:
    """重启视角：全新对象读同一份磁盘状态（`tmp_path` 之外的东西一概用不上）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    return store, ledger


def _coordinator(
    store: JsonlSessionStore,
    ledger: SqliteOperationLedger,
    tmp_path: Path,
    *,
    counter: _CountingDangerTool | None = None,
) -> RecoveryCoordinator:
    """生产形状的协调器（`SessionService.recover` 就是这么装配的）。

    注册表里放同名 DANGER 计数工具：协调器若有任何一条"重跑"路径，它就会打到
    这个计数器上——"没有重跑"因此是可观测的。
    """
    registry = ToolRegistry()
    if counter is not None:
        registry.register(counter)
    return RecoveryCoordinator(
        session_store=store,
        workspace_registry=None,
        operation_ledger=ledger,
        database_path=tmp_path / "state.db",
        tool_registry=registry,
        lock_timeout_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_kill_during_approval_wait_recovers_and_next_run_completes(
    tmp_path: Path,
) -> None:
    """审批等待中被杀 ⇒ 恢复期恰好一条 deny 结清 ⇒ 下一次 run 达成 run/completed。"""
    _crash(tmp_path)
    store, ledger = await _open(tmp_path)

    # 崩溃现场：谓词 2 为真（无决议审批）；悬空 tool/call 在场；账上无行（审批
    # 闸门在接纳点之前 ⇒ 没有 Operation，也就没有"重跑"的账面依据）。
    events = store.read_events(SESSION_ID)
    unresolved = unresolved_approval_ids(events)
    assert len(unresolved) == 1 and unresolved[0], "现场是无决议审批的永久 wedge"
    assert detect_dangling(events) == [CALL_ID]
    assert await ledger.get(SESSION_ID, CALL_ID) is None
    assert not (tmp_path / "mutation.log").exists(), "副作用未发生"

    counter = _CountingDangerTool()
    recovered = await _coordinator(store, ledger, tmp_path, counter=counter).recover(
        SESSION_ID
    )

    # 结清恰好一条：deny + 稳定理由；谓词 2 从真变假。
    settlements = [e for e in recovered.events if e.type == PERMISSION_RESOLVED]
    assert len(settlements) == 1
    assert settlements[0].data["approval_id"] == unresolved[0]
    assert settlements[0].data["decision"] == "deny"
    assert settlements[0].data["reason"] == RECOVERY_STALE_APPROVAL_REASON
    assert unresolved_approval_ids(recovered.events) == []
    assert counter.calls == 0 and not (tmp_path / "mutation.log").exists(), (
        "恢复零 Provider / 零工具请求：没有重跑那条被审批挡住的调用"
    )

    # 会话复活：恢复后的下一个 run（模型不再请求工具）走完完成闸门——结清前的
    # 同一现场会被谓词 2 挡成 quiescence_blocked（.red 现场由上面的 wedge 断言钉住）。
    registry = ToolRegistry()
    registry.register(counter)
    runtime = AgentRuntime(
        model=ScriptedModel(responses=[AIMessage(content="恢复后直接完成")]),
        registry=registry,
        executor=ToolExecutor(registry, operation_ledger=ledger),
        max_agent_turns=10,
    )
    await runtime.run(recovered, "继续")

    final_events = store.read_events(SESSION_ID)
    terminal = [e.type for e in final_events if e.type == RUN_COMPLETED]
    assert terminal, (
        "结清后下一次 run 必须能完成（不放宽谓词的证明：结清前谓词 2 为真）"
    )
    assert counter.calls == 0, "复活路径同样不重跑旧调用"


@pytest.mark.asyncio
async def test_stale_approval_settlement_lands_before_synthesis(
    tmp_path: Path,
) -> None:
    """#337 deny 结清必须先于 UNRESOLVED 悬空合成的 tool/result 落账。

    合成文案「工具未执行（审批未通过）」以 deny 结清为 durable 依据——结清
    事件后落的话，恢复中途被杀的崩溃窗内投影先有文案、依据未落（批次收口
    Standards 轴 P4-1）。正向顺序下窗口反转成「依据已落、文案未落」，下一次
    recover 重新合成即自愈；规格 07 §9 的顺序也是 reconcile → restore
    consistency。结清集合与决策输入不变，只换 append 顺序。
    """
    _crash(tmp_path)
    store, ledger = await _open(tmp_path)

    recovered = await _coordinator(store, ledger, tmp_path).recover(SESSION_ID)

    events = recovered.events
    settlement_idx = next(
        i for i, e in enumerate(events) if e.type == PERMISSION_RESOLVED
    )
    synthesized_idx = next(
        i
        for i, e in enumerate(events)
        if e.type == TOOL_RESULT and e.data.get("tool_call_id") == CALL_ID
    )
    assert settlement_idx < synthesized_idx, (
        "deny 结清事件必须先于「审批未通过」合成 tool/result 落账"
    )
