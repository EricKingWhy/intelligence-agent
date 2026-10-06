"""真实子进程 Kill + 新进程恢复 integration tests（#32）。

每个场景独立一条测试：真实 Python 子进程在精确故障注入点 os._exit（Ledger
状态已持久化、后续步骤未发生的真实崩溃窗口），父进程以全新 store/ledger/
registry 实例（除磁盘外零共享状态）执行 RecoveryCoordinator 恢复。

覆盖 #32 全部 AC：
- Tool 成功、Ledger 已写终态、tool/call 已持久化但 result event 未写 → 真实 Kill；
- 新进程 recover 后生成正确 Recovery ToolResult，dangling tool call = 0；
- write 已成功副作用不重复（父进程 monkeypatch write_text 为禁写哨兵）；
- PENDING Operation 恢复中默认 skip，不调用真实 Tool（文件保持不存在）；
- 多 Tool 部分完成后崩溃仍恢复所有已知 Operation 并保持配对；
- Session Workspace 恢复到原映射；
- 每个场景有超时保护（subprocess timeout + asyncio.wait_for），测试不挂死。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_harness.recovery import (
    ReconcileCallback,
    ReconcileVerdict,
    RecoveryCoordinator,
)
from agent_harness.recovery.coordinator import DecisionsReconcileCallback
from agent_harness.sandbox.local import LocalSubprocessSandbox
from agent_harness.session import (
    OPERATION_RECONCILE_REQUIRED,
    TOOL_CALL,
    TOOL_RESULT,
    JsonlSessionStore,
    Session,
    detect_dangling,
)
from agent_harness.storage import Operation, OperationState, SqliteOperationLedger
from agent_harness.tooling import ErrorCode, ToolResult

_CHILD = Path(__file__).with_name("_kill_child.py")
_CHILD_TIMEOUT_SECONDS = 60
_RECOVER_TIMEOUT_SECONDS = 30


def _run_child(root: Path, config: dict) -> int:
    """启动真实子进程并等待其在注入点崩溃；返回 exit code（137 = kill 生效）。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(_CHILD), json.dumps(config)],
        timeout=_CHILD_TIMEOUT_SECONDS,
        env=env,
        cwd=Path(__file__).parent.parent.parent,
        capture_output=True,
        text=True,
        check=False,  # 137 是预期结果，由调用方断言
    )
    return completed.returncode


def _discover_session_id(root: Path) -> str:
    """从磁盘发现子进程创建的 session（子进程被 kill，无法经 stdout 传递）。"""
    mapping_files = list((root / "ws" / "workspaces").glob("*.json"))
    assert len(mapping_files) == 1, "子进程应恰好留下一个 workspace 映射"
    return mapping_files[0].stem


def _crash_state(
    root: Path, session_id: str
) -> tuple[SqliteOperationLedger, list]:
    """父进程内全新实例（模拟新进程）：只依赖磁盘上的持久状态。"""
    store = JsonlSessionStore(root / "sessions")
    events = store.read_events(session_id)
    ledger = SqliteOperationLedger(root / "state.db")
    return ledger, events


async def _recover(root: Path, session_id: str, **kwargs):
    ledger = SqliteOperationLedger(root / "state.db")
    await ledger.initialize()
    from agent_harness.sandbox.registry import WorkspaceRegistry

    coordinator = RecoveryCoordinator(
        session_store=JsonlSessionStore(root / "sessions"),
        workspace_registry=WorkspaceRegistry(root / "ws", backend="local"),
        operation_ledger=ledger,
        database_path=root / "state.db",
        **kwargs,
    )
    return await asyncio.wait_for(
        coordinator.recover(session_id), timeout=_RECOVER_TIMEOUT_SECONDS
    )


def _result_events(session: Session) -> dict[str, str]:
    return {
        event.data["tool_call_id"]: event.data["content"]
        for event in session.events
        if event.type == TOOL_RESULT
    }


@pytest.fixture()
def _forbid_side_effects(monkeypatch: pytest.MonkeyPatch):
    """恢复进程内的禁写哨兵：任何重复副作用尝试都会让测试当场失败。"""

    def _forbidden(self, path: str, content: str) -> None:
        raise AssertionError(
            f"恢复期间不得重复执行副作用（write_text 被再次调用：{path}）"
        )

    monkeypatch.setattr(LocalSubprocessSandbox, "write_text", _forbidden)


# ── 场景 A：Tool 成功、Ledger 终态已写、result event 未写时真实 Kill ──


@pytest.mark.asyncio
async def test_kill_after_terminal_write_recovers_without_duplicate_side_effect(
    tmp_path: Path, _forbid_side_effects
) -> None:
    root = tmp_path / "root"
    returncode = _run_child(
        root,
        {
            "root": str(root),
            "calls": [
                {
                    "id": "call-1",
                    "name": "write",
                    "args": {"path": "hello.txt", "content": "written-by-child"},
                }
            ],
            "kill_stage": "terminal",
            "kill_call_id": "call-1",
        },
    )
    assert returncode == 137
    session_id = _discover_session_id(root)

    # 崩溃现场：Ledger 终态已写、tool/call 已持久化、result event 未写、副作用已发生。
    ledger, crashed_events = _crash_state(root, session_id)
    operation = await ledger.get(session_id, "call-1")
    assert operation is not None and operation.state is OperationState.SUCCEEDED
    assert [e.type for e in crashed_events].count(TOOL_CALL) == 1
    assert not [e for e in crashed_events if e.type == TOOL_RESULT]
    workspace_file = root / "ws" / "workspaces" / session_id / "hello.txt"
    assert workspace_file.read_text(encoding="utf-8") == "written-by-child"

    # 新进程恢复：Ledger result_json → Recovery ToolResult；副作用不重复。
    recovered = await _recover(root, session_id)

    results = _result_events(recovered)
    assert set(results) == {"call-1"}
    synthesized = ToolResult.model_validate_json(results["call-1"])
    assert synthesized.ok is True
    assert "已写入" in synthesized.message
    assert detect_dangling(recovered.events) == []
    # 禁写哨兵未被触发（恢复零副作用）；文件内容仍是子进程写入的那一份。
    assert workspace_file.read_text(encoding="utf-8") == "written-by-child"


# ── 场景 B：Tool 尚未开始（PENDING）时真实 Kill ──


@pytest.mark.asyncio
async def test_kill_while_pending_skips_operation_without_real_tool(
    tmp_path: Path, _forbid_side_effects
) -> None:
    root = tmp_path / "root"
    returncode = _run_child(
        root,
        {
            "root": str(root),
            "calls": [
                {
                    "id": "call-1",
                    "name": "write",
                    "args": {"path": "never.txt", "content": "should-not-exist"},
                }
            ],
            "kill_stage": "pending",
            "kill_call_id": "call-1",
        },
    )
    assert returncode == 137
    session_id = _discover_session_id(root)

    # 崩溃现场：Operation 停在 PENDING，真实 Tool 根本没跑（文件不存在）。
    ledger, _crashed_events = _crash_state(root, session_id)
    operation = await ledger.get(session_id, "call-1")
    assert operation is not None and operation.state is OperationState.PENDING
    workspace_file = root / "ws" / "workspaces" / session_id / "never.txt"
    assert not workspace_file.exists()

    # 新进程恢复：默认 skip——不调用真实 Tool，文件保持不存在。
    recovered = await _recover(root, session_id)

    synthesized = ToolResult.model_validate_json(_result_events(recovered)["call-1"])
    assert synthesized.ok is False
    assert synthesized.error_code is ErrorCode.CANCELLED
    assert "跳过" in synthesized.message
    assert not workspace_file.exists()  # 副作用为零：恢复没有执行 write
    assert detect_dangling(recovered.events) == []
    # Ledger 推进到 CANCELLED（Round 8 契约修订，同 recovery 套件）：skip 已把
    # CANCELLED 语义结果合成进 session，Ledger 同步记录"恢复时放弃、从未执行"
    # ——不伪造执行结果（不写 SUCCEEDED/FAILED），但不再与 session 永久不一致。
    still_pending = await ledger.get(session_id, "call-1")
    assert still_pending is not None and still_pending.state is OperationState.CANCELLED


# ── 场景 C：多 Tool 部分完成后崩溃 ──


class _AbandonCallback(ReconcileCallback):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(self, operation, hint) -> ReconcileVerdict:
        self.calls.append(operation.tool_call_id)
        return ReconcileVerdict.ABANDON


@pytest.mark.asyncio
async def test_multi_tool_partial_completion_crash_recovers_all_pairings(
    tmp_path: Path, _forbid_side_effects
) -> None:
    root = tmp_path / "root"
    returncode = _run_child(
        root,
        {
            "root": str(root),
            "calls": [
                {
                    "id": "call-a",
                    "name": "write",
                    "args": {"path": "a.txt", "content": "aaa"},
                },
                {
                    "id": "call-b",
                    "name": "write",
                    "args": {"path": "b.txt", "content": "bbb"},
                },
            ],
            "kill_stage": "running",
            "kill_call_id": "call-b",
        },
    )
    assert returncode == 137
    session_id = _discover_session_id(root)

    # 崩溃现场：call-a 终态（副作用已发生），call-b RUNNING（未执行），b.txt 不存在。
    ledger, _crashed_events = _crash_state(root, session_id)
    assert (await ledger.get(session_id, "call-a")).state is OperationState.SUCCEEDED
    assert (await ledger.get(session_id, "call-b")).state is OperationState.RUNNING
    workspaces = root / "ws" / "workspaces" / session_id
    assert (workspaces / "a.txt").read_text(encoding="utf-8") == "aaa"
    assert not (workspaces / "b.txt").exists()

    callback = _AbandonCallback()
    recovered = await _recover(root, session_id, reconcile_callback=callback)

    # 所有已知 Operation 都被恢复，结果与原 tool_call_id 配对。
    results = _result_events(recovered)
    assert set(results) == {"call-a", "call-b"}
    assert ToolResult.model_validate_json(results["call-a"]).ok is True
    abandoned = ToolResult.model_validate_json(results["call-b"])
    assert abandoned.ok is False and abandoned.retryable is False
    assert detect_dangling(recovered.events) == []
    assert callback.calls == ["call-b"]  # 只有 RUNNING 的 call-b 需要人工裁决
    assert (workspaces / "b.txt").exists() is False  # 恢复没有替 call-b 执行 write
    # reconcile-required 事件恰一条（call-b 进入 NEED_RECONCILE）。
    assert (
        len([e for e in recovered.events if e.type == OPERATION_RECONCILE_REQUIRED])
        == 1
    )


# ── 场景 E（#357 W-13 R19）：终态写入后真实 Kill + UNKNOWN 窗口经用户裁决恢复 ──


@pytest.mark.asyncio
async def test_kill_after_terminal_write_recovers_via_user_decision_exactly_once(
    tmp_path: Path, _forbid_side_effects
) -> None:
    """R19：真实 kill（SQLite 终态写入成功后、``tool/result`` append 前）+
    ``decisions=[{tool_call_id, CONFIRM_SUCCESS, source}]`` 恢复，配对恰一次。

    场景构成：call-a 是真实子进程 ``terminal`` 探针窗口（Ledger SUCCEEDED +
    原始 result_json 已持久化、tool/result 未 append、副作用恰一份）；call-b
    是同会话的 UNKNOWN 悬空窗口（确定性种子，同 ``test_web_api`` 的
    ``_seed_reconcile_window`` 形态——终态悬空行不可能再接纳裁决，见 422 预检，
    故裁决证据（``reconcile_meta``）必须由非终态窗口承载）。重启后**先查 DB**
    （账行事实 + 恰一份副作用），再一次恢复同时结清两行。

    证据：SessionEvent seq 单调、Operation ID、operations 行 state +
    ``reconcile_meta``（verdict + source 逐字）；每调用配对恰一次（dangling=0）；
    CONFIRM_SUCCESS 是用户断言不是重跑——恢复零副作用。
    """
    root = tmp_path / "root"
    returncode = _run_child(
        root,
        {
            "root": str(root),
            "calls": [
                {
                    "id": "call-a",
                    "name": "write",
                    "args": {"path": "a.txt", "content": "written-by-child"},
                }
            ],
            "kill_stage": "terminal",
            "kill_call_id": "call-a",
        },
    )
    assert returncode == 137
    session_id = _discover_session_id(root)

    # 父进程内确定性补一个 UNKNOWN 悬空窗口（同会话、新 run）。
    store = JsonlSessionStore(root / "sessions")
    session = Session(
        session_id, store, events=store.read_events(session_id)
    )
    run_id, _ = session.begin_run()
    session.append(
        TOOL_CALL,
        {"tool_call_id": "call-b", "tool_name": "write", "args": {}},
        run_id=run_id,
    )
    ledger = SqliteOperationLedger(root / "state.db")
    await ledger.initialize()
    await ledger.create(
        Operation(
            tool_call_id="call-b",
            session_id=session_id,
            run_id=run_id,
            agent_id="default",
            tool_name="write",
            args_identity="{}",
            state=OperationState.PENDING,
            started_at="2026-10-06T00:00:00Z",
        )
    )
    await ledger.update_state(session_id, "call-b", OperationState.RUNNING)
    await ledger.update_state(session_id, "call-b", OperationState.UNKNOWN)

    # 重启后先查 DB（只依赖磁盘上的持久状态）：账行事实 + 恰一份副作用。
    ledger, crashed_events = _crash_state(root, session_id)
    op_a = await ledger.get(session_id, "call-a")
    assert op_a is not None and op_a.state is OperationState.SUCCEEDED
    assert op_a.result_json is not None, "终态行必须携带原 ToolResult"
    op_b = await ledger.get(session_id, "call-b")
    assert op_b is not None and op_b.state is OperationState.UNKNOWN
    workspaces = root / "ws" / "workspaces" / session_id
    assert (workspaces / "a.txt").read_text(encoding="utf-8") == "written-by-child"
    assert not [e for e in crashed_events if e.type == TOOL_RESULT]

    recovered = await _recover(
        root,
        session_id,
        reconcile_callback=DecisionsReconcileCallback(
            {"call-b": ReconcileVerdict.CONFIRM_SUCCESS},
            sources={"call-b": "我查了文件/命令输出：a.txt 已落盘"},
        ),
    )

    # 配对恰一次：每调用恰一条 tool/result，seq 严格递增，dangling = 0。
    results = _result_events(recovered)
    assert set(results) == {"call-a", "call-b"}
    seqs = [e.seq for e in recovered.events if e.seq is not None]
    assert seqs == sorted(seqs), "SessionEvent seq 必须单调"
    assert detect_dangling(recovered.events) == []
    # call-a 走 Ledger result_json 精确回填（原 ToolResult 恢复，07 §6）。
    assert ToolResult.model_validate_json(results["call-a"]).ok is True
    assert "已写入" in results["call-a"]
    # call-b 经用户裁决结清：先有 reconcile-required 标记，配对恰一次。
    required = [
        e for e in recovered.events if e.type == OPERATION_RECONCILE_REQUIRED
    ]
    assert [e.data["tool_call_id"] for e in required] == ["call-b"]
    assert "人工 reconcile" in results["call-b"]

    # DB 证据：call-b SUCCEEDED + reconcile_meta（verdict + source 逐字）。
    settled = await ledger.get(session_id, "call-b")
    assert settled is not None and settled.state is OperationState.SUCCEEDED
    meta = json.loads(settled.reconcile_meta)
    assert meta["verdict"] == "CONFIRM_SUCCESS"
    assert meta["source"] == "我查了文件/命令输出：a.txt 已落盘"
    # CONFIRM_SUCCESS 不重跑工具：恢复零副作用（禁写哨兵未触发、无新文件）。
    assert not (workspaces / "b.txt").exists()
    assert (workspaces / "a.txt").read_text(encoding="utf-8") == "written-by-child"


# ── 场景 D：Session Workspace 恢复到原映射 ──


@pytest.mark.asyncio
async def test_workspace_restored_to_original_mapping_in_new_process(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    returncode = _run_child(
        root,
        {
            "root": str(root),
            "calls": [
                {
                    "id": "call-1",
                    "name": "write",
                    "args": {"path": "hello.txt", "content": "written-by-child"},
                }
            ],
            "kill_stage": "terminal",
            "kill_call_id": "call-1",
        },
    )
    assert returncode == 137
    session_id = _discover_session_id(root)

    mapping = json.loads(
        (root / "ws" / "workspaces" / f"{session_id}.json").read_text(
            encoding="utf-8"
        )
    )

    recovered = await _recover(root, session_id)

    # 新进程按持久映射重建 Sandbox，workspace 落回原子目录（不新造、不漂移）。
    assert recovered.sandbox is not None
    assert isinstance(recovered.sandbox, LocalSubprocessSandbox)
    assert Path(recovered.sandbox.workspace_root) == Path(mapping["workspace_root"])
    assert (Path(mapping["workspace_root"]) / "hello.txt").exists()
