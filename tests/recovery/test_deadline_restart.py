"""T7 #315 kill/restart：在真实 deadline 边界上崩溃后，两种 mutating 结局都要可判定。

票面 Verification 明写 "Kill/restart tests with known and unknown mutating operation
outcomes"。这里用**真崩溃**（子进程 `_deadline_restart_child.py` 里的 `os._exit(9)`），
而不是把行手工塞进 Ledger：磁盘上留下的现场就是"进程死掉那一刻"的样子，重启之后
（全新 store / ledger 对象，进程内没有任何可依赖的内存）的判定因此只可能来自 durable 事实。

两条路径是同一次工具调用、同一个 deadline 边界，只差**执行器有没有来得及写终态**：

- 已知：副作用落盘 → 执行器把这次调用落成 SUCCEEDED → tool/result 配对 → 下一个边界
  检查读到 deadline 已到 ⇒ `run/paused(reason=deadline)` → 真崩溃。重启后**不得**被判成
  欠对账，也不得重跑（副作用只有一行）。
- 未知：副作用落盘 → 结论未回填时真崩溃，账上只剩 `RUNNING`。重启后必须进
  NEED_RECONCILE：`RecoveryCoordinator`（`SessionService.recover` 的唯一入口，且不带
  ReconcileCallback）拒绝恢复，且**永不**自动重跑高风险副作用（不变量 #14）。

HTTP / CLI 面上同一拒绝的呈现（409 与提示行）由 `tests/web/test_run_pause_resume_api.py`
与 `tests/test_cli_run_pause_resume.py` 钉住；本文件只钉 durable 判定本身，
以及"重启之后还能不能自己站起来"这一条。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from agent_harness.agent.run_budget import REASON_DEADLINE
from agent_harness.recovery import (
    ReconcileCallback,
    ReconcileRequired,
    ReconcileVerdict,
    RecoveryCoordinator,
)
from agent_harness.session import (
    OPERATION_RECONCILE_REQUIRED,
    RUN_PAUSED,
    JsonlSessionStore,
    detect_dangling,
)
from agent_harness.storage import (
    Operation,
    OperationState,
    SqliteOperationLedger,
    needs_reconcile,
)
from agent_harness.tooling import (
    ReconcileHint,
    Tool,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHILD = Path(__file__).with_name("_deadline_restart_child.py")

#: 与子进程的 `CRASH_EXIT_CODE` 一致。子进程 import 即执行（模块末行是 `asyncio.run`），
#: 所以常量只能抄一份，不能 import 它。
_CRASH_EXIT_CODE = 9

#: 子进程自己算 deadline（`DEADLINE <iso>` 先打出来），这里给的是相对它的偏移量：
#: deadline 必须晚于"工具被接纳"（~0.3s）又早于"副作用落盘"（hold 结束）。
_DEADLINE_SECONDS = 2.5
_HOLD_SECONDS = 4.0

CALL_ID = "call-deadline-restart"
TOOL_NAME = "mutate"


@dataclass(frozen=True)
class _Crash:
    """一次崩溃留下的东西：磁盘现场 + 子进程自报的 deadline。"""

    session_id: str
    deadline: datetime
    stdout: str
    stderr: str


class _NoArgs(BaseModel):
    pass


class _CountingMutateTool(Tool):
    """与子进程同名的 mutating 工具：被调用即记数（用于证明"没有重跑"）。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return "不该被执行：重启后的判定只许读账，不许重跑副作用。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    async def execute(self, args: BaseModel) -> ToolResult:
        self.calls += 1
        return ToolResult.success("rerun")


class _ScriptedCallback(ReconcileCallback):
    """人工裁决的替身：记录调用，返回预设结论。"""

    def __init__(self, verdict: ReconcileVerdict) -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    async def resolve(
        self, operation: Operation, hint: ReconcileHint
    ) -> ReconcileVerdict:
        self.calls.append(operation.tool_call_id)
        return self.verdict


def _parse_deadline(stdout: str) -> datetime:
    for line in stdout.splitlines():
        if line.startswith("DEADLINE "):
            return datetime.fromisoformat(line.split(" ", 1)[1])
    raise AssertionError(f"子进程没有报出 deadline：{stdout!r}")


def _wait_past(deadline: datetime) -> None:
    """等到 deadline 真的过去再"重启"：判定必须发生在到点之后。"""
    remaining = (deadline - datetime.now(UTC)).total_seconds() + 0.2
    if remaining > 0:
        time.sleep(remaining)


def _crash(tmp_path: Path, mode: str) -> _Crash:
    """跑一次"在 deadline 边界上真崩溃"的子进程，并等过它的 deadline。"""
    session_id = f"sess-restart-{mode}"
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            str(_CHILD),
            json.dumps(
                {
                    "root": str(tmp_path),
                    "mode": mode,
                    "session_id": session_id,
                    "deadline_seconds": _DEADLINE_SECONDS,
                    "hold_seconds": _HOLD_SECONDS,
                }
            ),
        ],
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
    deadline = _parse_deadline(completed.stdout)
    _wait_past(deadline)
    return _Crash(
        session_id=session_id,
        deadline=deadline,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _mutations(tmp_path: Path) -> int:
    """副作用行数：追加写，重跑会多出一行。"""
    sentinel = tmp_path / "mutation.log"
    if not sentinel.exists():
        return 0
    return sentinel.read_text(encoding="utf-8").count("mutated")


async def _open(tmp_path: Path) -> tuple[JsonlSessionStore, SqliteOperationLedger]:
    """重启视角：全新对象读同一份磁盘状态（`tmp_path` 之外的东西一概用不上）。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    return store, ledger


async def _operation(ledger: SqliteOperationLedger, session_id: str) -> Operation:
    operation = await ledger.get(session_id, CALL_ID)
    assert operation is not None, "重启后账本里应当有这次调用"
    return operation


def _coordinator(
    store: JsonlSessionStore,
    ledger: SqliteOperationLedger,
    tmp_path: Path,
    *,
    reconcile_callback: ReconcileCallback | None = None,
    counter: _CountingMutateTool | None = None,
) -> RecoveryCoordinator:
    """生产形状的协调器（`SessionService.recover` 就是这么装配的）。

    注册表里放的是**同名同侧效应**的计数工具：如果协调器有任何一条"重跑"路径，
    它就会打到这个计数器上——"没有重跑"因此是可观测的，不是靠缺省推断的。
    """
    registry = ToolRegistry()
    if counter is not None:
        registry.register(counter)
    return RecoveryCoordinator(
        session_store=store,
        workspace_registry=None,
        operation_ledger=ledger,
        database_path=tmp_path / "state.db",
        reconcile_callback=reconcile_callback,
        tool_registry=registry,
        lock_timeout_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_restart_after_a_known_mutation_owes_nothing_and_is_not_rerun(
    tmp_path: Path,
) -> None:
    """已知结局：重启后不欠对账、不重跑，恢复零裁决通过。"""
    crash = _crash(tmp_path, "known")
    store, ledger = await _open(tmp_path)

    operation = await _operation(ledger, crash.session_id)
    assert operation.state is OperationState.SUCCEEDED, "执行器已写终态"
    assert needs_reconcile(operation) is False, "副作用已证完成 ⇒ 不欠对账"

    events = store.read_events(crash.session_id)
    assert detect_dangling(events) == [], "tool/result 已配对，没留下悬空调用"
    assert [e.data.get("reason") for e in events if e.type == RUN_PAUSED] == [
        REASON_DEADLINE
    ], "现场是 deadline 暂停，重启不该改写它"
    assert _mutations(tmp_path) == 1

    counter = _CountingMutateTool()
    recovered = await _coordinator(
        store, ledger, tmp_path, counter=counter
    ).recover(crash.session_id)

    types = [event.type for event in recovered.events]
    assert types.count(RUN_PAUSED) == 1, "恢复不改写历史"
    assert OPERATION_RECONCILE_REQUIRED not in types, "不欠账就不该喊对账"
    assert counter.calls == 0 and _mutations(tmp_path) == 1, "重启没有重跑副作用"


@pytest.mark.asyncio
async def test_restart_after_an_unknown_mutation_refuses_and_never_reruns(
    tmp_path: Path,
) -> None:
    """未知结局：重启后必须人工裁决才允许恢复，且永不自动重跑。"""
    crash = _crash(tmp_path, "unknown")
    store, ledger = await _open(tmp_path)

    operation = await _operation(ledger, crash.session_id)
    assert operation.state is OperationState.RUNNING, "结论未回填 ⇒ 账上停在 RUNNING"
    assert needs_reconcile(operation) is True, "恢复闸门读的就是这个判据"
    assert _mutations(tmp_path) == 1, "副作用确实发生过——这正是'未知'的由来"

    counter = _CountingMutateTool()
    coordinator = _coordinator(store, ledger, tmp_path, counter=counter)
    before = store.read_events(crash.session_id)
    with pytest.raises(ReconcileRequired, match=TOOL_NAME):
        await coordinator.recover(crash.session_id)

    assert counter.calls == 0, "没有裁决不得重跑 mutating 调用"
    assert store.read_events(crash.session_id) == before, "拒绝必须零写入（不伪造结果）"
    assert (await _operation(ledger, crash.session_id)).state is (
        OperationState.RUNNING
    ), "拒绝不该顺手把账改成别的样子"
    assert _mutations(tmp_path) == 1

    # 人工裁决（确认"副作用已完成"）之后才允许继续：账转终态、事件补齐，
    # 而执行器自始至终没有被用过一次。
    callback = _ScriptedCallback(ReconcileVerdict.CONFIRM_SUCCESS)
    resolved = await _coordinator(
        store, ledger, tmp_path, reconcile_callback=callback, counter=counter
    ).recover(crash.session_id)

    assert callback.calls == [CALL_ID]
    final = await _operation(ledger, crash.session_id)
    assert final.state is OperationState.SUCCEEDED
    assert needs_reconcile(final) is False
    assert [e.type for e in resolved.events].count(OPERATION_RECONCILE_REQUIRED) == 1
    assert counter.calls == 0 and _mutations(tmp_path) == 1, "裁决也不重跑副作用"
