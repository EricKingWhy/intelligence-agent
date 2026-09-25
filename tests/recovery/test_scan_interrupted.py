"""T8 #138：崩溃中断扫描（run/interrupted + Ledger reconcile，不盲重跑）。

契约：`docs/integration/PRD_PHASE_MULTITURN_TOTAL.md` §2.5
- 进程重启扫描无终态 run 的 session，追加 `run/interrupted`
  （as-built 字符串按仓库 `run/*` 词汇表：`run/interrupted`，旧稿 `run_interrupted`）
- 强制跑 Ledger reconcile
- UNKNOWN 工具调用标记需人工确认（不变量 #14），不盲重跑

kill 子进程入口见 `_crash_run_child.py`（真实进程崩溃，非模拟）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_harness.recovery import InterruptionScanResult
from agent_harness.recovery.scan import (
    ScanRecovery,
    scan_interrupted_sessions,
)
from agent_harness.session import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_PAUSED,
    RUN_RESUMED,
    SESSION_RESUMED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.interrupt import detect_unterminated_runs
from agent_harness.storage import (
    Operation,
    OperationState,
    SqliteOperationLedger,
)

_CRASH_CHILD = Path(__file__).with_name("_crash_run_child.py")


def _make_store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path / "sessions")


def _open_run_session(store: JsonlSessionStore, session_id: str = "sid") -> Session:
    """run/started → user/message → tool/call，没有终态（= 崩溃现场）。"""
    session = Session.start(store, session_id=session_id)
    run_id, _ = session.begin_run()
    session.append(USER_MESSAGE, {"content": "do the work"}, run_id=run_id)
    session.append(
        TOOL_CALL,
        {"tool_call_id": "call-1", "tool_name": "bash", "args": {"command": "echo hi"}},
        run_id=run_id,
        step_id=1,
    )
    return session


def _pause_data() -> dict:
    """`run/paused.data` 的最小合法形状（`03 §3.4`；字段名取自 `build_pause_data`）。"""
    return {
        "reason": "budget_exhausted",
        "trigger_dimension": "run.max_agent_turns_total",
        "budget_version": 1,
        "consumed": {"agent_turns": 1},
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": 2},
        },
        "continuation": {
            "completed": [], "remaining": ["接着干"], "blockers": [],
            "next_safe_action": "抬高 ceiling 后同 run 恢复",
        },
        "closeout_source": "deterministic",
        "resume_requirements": [],
        "trace_id": None,
    }


def _resume_data() -> dict:
    """`run/resumed.data` 的最小合法形状（同 run 续跑：版本 +1、绝对 ceiling）。"""
    return {
        "from_pause_seq": 3,
        "previous_budget_version": 1,
        "budget_version": 2,
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": 8},
        },
        "consumed": {"agent_turns": 1},
        "resume_basis": "budget_increase",
    }


async def _seed_operation(
    ledger: SqliteOperationLedger,
    *,
    session_id: str,
    tool_call_id: str,
    state: OperationState,
) -> None:
    await ledger.create(Operation(
        tool_call_id=tool_call_id,
        session_id=session_id,
        run_id="run-1",
        agent_id="default",
        tool_name="bash",
        args_identity='{"command": "echo hi"}',
        state=OperationState.PENDING,
        started_at="2026-09-09T00:00:00+00:00",
    ))
    await ledger.update_state(session_id, tool_call_id, OperationState.RUNNING)
    if state is not OperationState.RUNNING:
        await ledger.update_state(session_id, tool_call_id, state)


def _scan(store: JsonlSessionStore, ledger: SqliteOperationLedger, tmp_path: Path):
    return asyncio.run(scan_interrupted_sessions(
        session_store=store,
        operation_ledger=ledger,
        workspace_registry=None,
        database_path=tmp_path / "harness.db",
    ))


class TestDetectUnterminatedRuns:
    def test_no_run_started_is_not_interrupted(self, tmp_path):
        store = _make_store(tmp_path)
        session = Session.start(store, session_id="sid")
        session.append(USER_MESSAGE, {"content": "hi"})

        assert detect_unterminated_runs(store.read_events("sid")) == []

    def test_started_without_terminal_is_interrupted(self, tmp_path):
        store = _make_store(tmp_path)
        session = _open_run_session(store)

        interrupted = detect_unterminated_runs(store.read_events("sid"))

        assert len(interrupted) == 1
        assert interrupted[0].run_id == session.events[1].run_id
        assert interrupted[0].step_id == 1
        # interrupted_seq = 该 run 最后一条事件的 seq（tool/call）
        assert interrupted[0].interrupted_seq == session.events[-1].seq

    def test_completed_run_is_not_interrupted(self, tmp_path):
        store = _make_store(tmp_path)
        session = Session.start(store, session_id="sid")
        run_id, _ = session.begin_run()
        session.append(USER_MESSAGE, {"content": "hi"}, run_id=run_id)
        session.append(RUN_COMPLETED, {"final_text": "ok"}, run_id=run_id)

        assert detect_unterminated_runs(store.read_events("sid")) == []

    def test_failed_run_is_not_interrupted(self, tmp_path):
        store = _make_store(tmp_path)
        session = Session.start(store, session_id="sid")
        run_id, _ = session.begin_run()
        session.append(RUN_FAILED, {"reason": "boom"}, run_id=run_id)

        assert detect_unterminated_runs(store.read_events("sid")) == []

    def test_already_interrupted_run_is_not_detected_again(self, tmp_path):
        store = _make_store(tmp_path)
        session = _open_run_session(store)
        session.append(RUN_INTERRUPTED, {"interrupted_seq": 2, "reason": "process_restart"})

        assert detect_unterminated_runs(store.read_events("sid")) == []

    def test_paused_run_is_not_interrupted(self, tmp_path):
        """尾部 `run/paused` = 本次执行已干净收口 ⇒ **不算**未终结（`#312`）。

        为什么这条不能省：暂停是可恢复的健康状态（`03 §5`），把它当崩溃现场会让下次
        启动给一个正常暂停的 run 补 `run/interrupted` —— 用户看到的"接着跑"就变成
        "上次崩了"，且 pause→resume 的 CAS 版本账会被这条假终态污染。
        """
        store = _make_store(tmp_path)
        session = _open_run_session(store)
        run_id = session.events[1].run_id
        session.append(RUN_PAUSED, _pause_data(), run_id=run_id)

        assert detect_unterminated_runs(store.read_events("sid")) == []

    def test_paused_then_resumed_run_is_open_until_terminal(self, tmp_path):
        """`run/resumed` 把 run **重新开放**：暂停过又续跑的 run 没有终态 ⇒ 仍算未终结。

        另一半的边界：暂停只关闭"那一次执行"，不关闭逻辑 run。若把 pause 当成永久关闭，
        一个正在恢复中崩掉的 run 会被判成"健康"，扫描就不再补 reconcile —— 未确认的
        Tool 副作用会留在 UNKNOWN 里无人认领（不变量 #13/#14）。
        """
        store = _make_store(tmp_path)
        session = _open_run_session(store)
        run_id = session.events[1].run_id
        session.append(RUN_PAUSED, _pause_data(), run_id=run_id)
        session.append(USER_MESSAGE, {"content": "接着干"}, run_id=run_id)

        assert detect_unterminated_runs(store.read_events("sid")) == []

        resumed = session.append(RUN_RESUMED, _resume_data(), run_id=run_id)
        interrupted = detect_unterminated_runs(store.read_events("sid"))

        assert len(interrupted) == 1
        assert interrupted[0].run_id == run_id
        # 识别位置 = 恢复之后最后一条事件（扫描据此写 run/interrupted 的 interrupted_seq）
        assert interrupted[0].interrupted_seq == resumed.seq

    def test_resume_keeps_the_agent_id_across_the_pause(self, tmp_path):
        """pause→resume 之间 `agent_id` 不丢：恢复开的 run 仍归同一个 agent。

        `agent_id` 只在 `run/started` 上（信封），恢复不再落一条 `run/started` ⇒ 它必须
        由这条链自己带过来；丢了会让 `run/interrupted` 的归因面与 `run/started` 不一致
        （ADR-0033 的归因面对账）。
        """
        store = _make_store(tmp_path)
        session = Session.start(store, session_id="sid")
        run_id, _ = session.begin_run(agent_id="worker-7")
        session.append(RUN_PAUSED, _pause_data(), run_id=run_id)
        session.append(RUN_RESUMED, _resume_data(), run_id=run_id)
        session.append(USER_MESSAGE, {"content": "接着干"}, run_id=run_id)

        interrupted = detect_unterminated_runs(store.read_events("sid"))

        assert len(interrupted) == 1
        assert interrupted[0].agent_id == "worker-7"


class TestScanInterruptedSessions:
    def test_open_run_is_marked_and_reconciled(self, tmp_path):
        store = _make_store(tmp_path)
        _open_run_session(store)
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        asyncio.run(ledger.initialize())

        results = _scan(store, ledger, tmp_path)

        assert len(results) == 1
        result: InterruptionScanResult = results[0]
        assert result.session_id == "sid"
        assert result.recovery == ScanRecovery.RECOVERED
        assert len(result.interrupted) == 1

        events = store.read_events("sid")
        assert events[-1].type == SESSION_RESUMED
        interrupted_events = [e for e in events if e.type == RUN_INTERRUPTED]
        assert len(interrupted_events) == 1
        data = interrupted_events[0].data
        assert data["reason"] == "process_restart"
        assert data["interrupted_seq"] == 3
        # run/step 身份挂事件信封（不是 data）——前端按 run 归组用
        assert interrupted_events[0].step_id == 1
        assert interrupted_events[0].run_id is not None

    def test_scan_is_idempotent(self, tmp_path):
        store = _make_store(tmp_path)
        _open_run_session(store)
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        asyncio.run(ledger.initialize())

        _scan(store, ledger, tmp_path)
        second = _scan(store, ledger, tmp_path)

        assert second == []
        assert sum(
            1 for e in store.read_events("sid") if e.type == RUN_INTERRUPTED
        ) == 1

    def test_completed_session_is_untouched(self, tmp_path):
        store = _make_store(tmp_path)
        session = Session.start(store, session_id="done")
        run_id, _ = session.begin_run()
        session.append(USER_MESSAGE, {"content": "hi"}, run_id=run_id)
        session.append(RUN_COMPLETED, {"final_text": "ok"}, run_id=run_id)
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        asyncio.run(ledger.initialize())

        assert _scan(store, ledger, tmp_path) == []
        assert not any(
            e.type == RUN_INTERRUPTED for e in store.read_events("done")
        )

    def test_unknown_operation_needs_manual_reconcile_without_blind_rerun(
        self, tmp_path
    ):
        """不变量 #14：UNKNOWN 不伪造结果、不盲重跑——只标记需人工确认。"""
        store = _make_store(tmp_path)
        _open_run_session(store)
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        asyncio.run(ledger.initialize())
        asyncio.run(_seed_operation(
            ledger, session_id="sid", tool_call_id="call-1",
            state=OperationState.UNKNOWN,
        ))

        results = _scan(store, ledger, tmp_path)

        assert results[0].recovery == ScanRecovery.NEEDS_MANUAL_RECONCILE
        assert "UNKNOWN" in (results[0].detail or "")
        events = store.read_events("sid")
        # 中断事实已记，但没有任何合成 tool/result（不伪造成功）
        assert any(e.type == RUN_INTERRUPTED for e in events)
        assert not any(e.type == TOOL_RESULT for e in events)
        # 也没有 session/resumed：reconcile 未完成，恢复语义不能假装完成
        assert not any(e.type == SESSION_RESUMED for e in events)
        # Ledger 状态不被扫描改动
        operations = asyncio.run(ledger.list_for_session("sid"))
        assert operations[0].state is OperationState.UNKNOWN

    def test_terminal_operation_is_backfilled_from_ledger(self, tmp_path):
        """Ledger 有终态 → reconcile 精确合成 tool/result（不是盲跑）。"""
        store = _make_store(tmp_path)
        _open_run_session(store)
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        asyncio.run(ledger.initialize())
        asyncio.run(_seed_operation(
            ledger, session_id="sid", tool_call_id="call-1",
            state=OperationState.SUCCEEDED,
        ))

        results = _scan(store, ledger, tmp_path)

        assert results[0].recovery == ScanRecovery.RECOVERED
        events = store.read_events("sid")
        results_by_call = {
            e.data["tool_call_id"]: e for e in events if e.type == TOOL_RESULT
        }
        assert "call-1" in results_by_call

    def test_failed_operation_is_backfilled_as_failure(self, tmp_path):
        """Ledger 终态 FAILED → 合成失败结果（与 SUCCEEDED 同样精确，不盲重跑）。"""
        store = _make_store(tmp_path)
        _open_run_session(store)
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        asyncio.run(ledger.initialize())
        asyncio.run(_seed_operation(
            ledger, session_id="sid", tool_call_id="call-1",
            state=OperationState.FAILED,
        ))

        results = _scan(store, ledger, tmp_path)

        assert results[0].recovery == ScanRecovery.RECOVERED
        events = store.read_events("sid")
        result_event = next(
            e for e in events
            if e.type == TOOL_RESULT and e.data["tool_call_id"] == "call-1"
        )
        payload = json.loads(result_event.data["content"])
        assert payload["ok"] is False
        assert "FAILED" in payload["message"]


class TestKillProcessRestart:
    """真实子进程崩溃（os._exit）→ 重启后扫描标记中断（AC: kill test）。"""

    def test_killed_run_is_marked_after_restart(self, tmp_path):
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [sys.executable, str(_CRASH_CHILD), json.dumps({
                "root": str(tmp_path), "session_id": "crashed",
            })],
            timeout=60, env=env,
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, check=False,
        )
        assert completed.returncode == 9, completed.stderr
        assert "READY" in completed.stdout

        # 新进程视角：只依赖磁盘状态
        store = _make_store(tmp_path)
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        asyncio.run(ledger.initialize())
        results = _scan(store, ledger, tmp_path)

        assert [r.session_id for r in results] == ["crashed"]
        assert results[0].recovery in {ScanRecovery.RECOVERED, ScanRecovery.FAILED}
        events = store.read_events("crashed")
        assert any(e.type == RUN_INTERRUPTED for e in events)
        # 会话可 resume（没有重复 seq / 未闭合 run 阻塞）
        resumed = Session.resume(store, "crashed")
        assert resumed.events[-1].type == SESSION_RESUMED
