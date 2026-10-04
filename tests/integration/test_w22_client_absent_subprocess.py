"""W-22（#366）真实子进程探针：离开后零新请求 + 真实 Tool 退出 + 崩溃窗口。

AC 原文三件事，各对应一条测试：

1. **admission 计数**（真实子进程 + FakeModel）：客户端离开、在途真实 bash
   按现有路径退出后，模型请求数停在缺席前那一次（新请求数 0）——不是
   in-process mock 的自证：闸门跑在独立进程的全栈装配里（build_runtime：
   真实 bash 工具 / Ledger / Checkpoint / workspace 映射）。
2. **事件 replay/重启投影一致**：父进程是天然的"重启后的新进程"——只依赖
   磁盘事件派生（`derive_run_budget` / `latest_paused_run`），与子进程落盘的
   契约值逐格对账。
3. **「服务在暂停事件落盘前 crash」**：缺席已置位、真实 bash 仍在执行时
   os._exit(137)——磁盘上没有 run/paused，恢复必须走**既有** interrupted/
   reconcile 路径（RUNNING → NEED_RECONCILE + 人工关卡事件），不得出现任何
   client_absent 痕迹（闸门只在内存，崩溃即消失；不伪造可恢复暂停）。

子进程入口：`_w22_absence_child.py`（模式与剧本见该文件 docstring）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_harness.agent.run_budget import derive_run_budget, latest_paused_run
from agent_harness.recovery import (
    ReconcileCallback,
    ReconcileRequired,
    ReconcileVerdict,
    RecoveryCoordinator,
)
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session import (
    MODEL_REQUEST,
    OPERATION_RECONCILE_REQUIRED,
    RUN_FAILED,
    RUN_PAUSED,
    TOOL_RESULT,
    JsonlSessionStore,
    detect_dangling,
)
from agent_harness.storage import OperationState, SqliteOperationLedger
from agent_harness.tooling import ToolResult

_CHILD = Path(__file__).with_name("_w22_absence_child.py")
_CHILD_TIMEOUT_SECONDS = 90
_RECOVER_TIMEOUT_SECONDS = 30


def _run_child(root: Path, mode: str, *, command: str = 'python -c "import time; time.sleep(1)"') -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(_CHILD),
         json.dumps({"root": str(root), "mode": mode, "command": command})],
        timeout=_CHILD_TIMEOUT_SECONDS,
        env=env,
        cwd=Path(__file__).parent.parent.parent,
        capture_output=True,
        text=True,
        check=False,  # 137（crash 模式）与断言失败（非 0）都由调用方判
    )


def _discover_session_id(root: Path) -> str:
    """从磁盘发现子进程创建的 session（与 kill_resume 同一恢复纪律）。"""
    mapping_files = list((root / "workspaces").glob("*.json"))
    assert len(mapping_files) == 1, "子进程应恰好留下一个 workspace 映射"
    return mapping_files[0].stem


def _events(root: Path, session_id: str) -> list:
    """父进程全新 store 实例：只依赖磁盘（重启投影一致的"重启"侧）。"""
    return JsonlSessionStore(root / "sessions").read_events(session_id)


# ── 场景 1+2：干净缺席暂停——admission 计数 + 真实 Tool 退出 + 重启投影 ──


def test_absence_pauses_with_zero_new_requests_and_real_tool_exit(tmp_path: Path) -> None:
    completed = _run_child(tmp_path, "pause")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["model_requests"] == 1, (
        "子进程内自证：缺席后新请求数 0（总请求数=缺席前那一次）"
    )
    session_id = result["session_id"]
    run_id = result["run_id"]

    events = _events(tmp_path, session_id)
    types = [e.type for e in events]

    # 真实 Tool 退出探针：在途 bash 按现有路径收口（SUCCEEDED，副作用已发生）。
    tool_results = [e for e in events if e.type == TOOL_RESULT]
    assert len(tool_results) == 1
    synthesized = ToolResult.model_validate_json(tool_results[0].data["content"])
    assert synthesized.ok is True, tool_results[0].data

    # 恰好一条 run/paused，契约值逐格对（02 §5.2.1 / 03 §3.4）。
    paused_events = [e for e in events if e.type == RUN_PAUSED]
    assert len(paused_events) == 1
    paused = paused_events[0]
    assert paused.run_id == run_id
    assert paused.data["reason"] == "client_absent"
    assert paused.data["trigger_dimension"] == "client_presence"
    assert paused.data["closeout_source"] == "deterministic"
    assert paused.data["budget_version"] == 1
    assert RUN_FAILED not in types, "不走 run/failed(orphaned) 旧路径"

    # 暂停后零新工作：没有事件再排在 run/paused 之后（closeout 模型请求也没有）。
    after_pause = [e for e in events if e.seq > paused.seq]
    assert after_pause == [], f"暂停后不得再落任何事件：{[e.type for e in after_pause]}"

    # 模型请求账：缺席前恰好一次成功请求。
    requests = [e for e in events if e.type == MODEL_REQUEST]
    assert len(requests) == 1
    assert requests[0].data.get("outcome") == "completed"

    # 重启投影一致：父进程（新进程）只凭磁盘事件派生，与子进程落盘事实一致。
    state = derive_run_budget(events, run_id)
    assert state.version == 1
    assert state.consumed.agent_turns == 1
    assert state.consumed.tool_calls_by_tool == {"bash": 1}
    paused_run = latest_paused_run(events)
    assert paused_run is not None
    assert paused_run.reason == "client_absent"
    assert paused_run.closeout_source == "deterministic"


# ── 场景 3：暂停事件落盘前 crash——既有 interrupted/reconcile 恢复 ────────


def test_crash_before_pause_lands_recovers_via_existing_reconcile(tmp_path: Path) -> None:
    completed = _run_child(
        tmp_path, "crash_before_pause",
        command='python -c "import time; time.sleep(10)"',
    )
    assert completed.returncode == 137, completed.stdout + completed.stderr
    session_id = _discover_session_id(tmp_path)

    events = _events(tmp_path, session_id)
    types = [e.type for e in events]
    assert RUN_PAUSED not in types, "暂停事件没有落盘——这是本场景的崩溃窗口"
    assert not any(
        "client_absent" in json.dumps(e.data, default=str) for e in events
    ), "闸门只在内存：崩溃后的磁盘上不得出现 client_absent 痕迹"

    # 崩溃现场：bash 的 Operation 停在 RUNNING（真实子进程命令被硬崩打断）。
    async def _running_state() -> OperationState:
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        await ledger.initialize()
        operation = await ledger.get(session_id, "call-1")
        assert operation is not None
        return operation.state

    state = asyncio.run(asyncio.wait_for(_running_state(), timeout=10))
    assert state is OperationState.RUNNING

    # 新进程按既有 RecoveryCoordinator 恢复。第一段：无 callback ⇒ 响亮拒绝
    # （ReconcileRequired）——不伪造结果、不盲重跑高风险副作用（不变量 #14）。
    # 第二段：注入裁决（ABANDON）后恢复推进——这就是既有 interrupted/reconcile
    # 路径的全部；client_absent 在其中没有任何特判。
    async def _recover(callback=None):
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        await ledger.initialize()
        coordinator = RecoveryCoordinator(
            session_store=JsonlSessionStore(tmp_path / "sessions"),
            workspace_registry=WorkspaceRegistry(root=tmp_path, backend="local"),
            operation_ledger=ledger,
            database_path=tmp_path / "harness.db",
            reconcile_callback=callback,
        )
        return await asyncio.wait_for(
            coordinator.recover(session_id), timeout=_RECOVER_TIMEOUT_SECONDS
        )

    with pytest.raises(ReconcileRequired):
        asyncio.run(_recover())

    class _Abandon(ReconcileCallback):
        async def resolve(self, operation, hint) -> ReconcileVerdict:
            return ReconcileVerdict.ABANDON

    recovered = asyncio.run(_recover(_Abandon()))

    assert detect_dangling(recovered.events) == []
    recovered_types = [e.type for e in recovered.events]
    assert recovered_types.count(OPERATION_RECONCILE_REQUIRED) == 1, (
        "RUNNING 操作按既有语义升 NEED_RECONCILE（人工裁决），不是伪造暂停"
    )
    assert not any(
        "client_absent" in json.dumps(e.data, default=str)
        for e in recovered.events
    )
    # 投影面：恢复后仍没有可续跑的 client_absent 暂停态。
    assert latest_paused_run(recovered.events) is None
