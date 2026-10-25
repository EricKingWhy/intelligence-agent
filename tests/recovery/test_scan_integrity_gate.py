"""#1516：启动恢复扫描的**写操作**必须先过既有严格完整性闸。

票面事实：``scan_interrupted_sessions`` 用容错读（``read_events``）拿到事件后
直接追加 ``run/interrupted`` 并跑 reconcile；而 ``SessionService`` 的恢复入口
另有一套严格预检（``read_events_report`` + ``_raise_if_event_log_corrupt``）。
于是「完整坏行 / seq 断层」的日志会被恢复写入污染——坏日志上多出「恢复结论」，
用户看到的中断/恢复事实并不是可信历史。

本文件钉住三件事：

1. 五类输入（完整坏行 / seq 断层 / seq 重复 / 合法尾部半行 / 正常日志）走
   **真实扫描**：损坏三类 → 文件字节与事件数不变 + 恢复写被拒绝 + 拒绝原因
   与 SessionService 同源；半行与正常两类 → 按既有语义正常恢复；
2. 扫描与 SessionService 走**同一个**完整性判定函数（不是第二套宽松读法）；
3. 损坏日志上的 UNKNOWN 高风险工具不被盲重放、Ledger 不被扫描改动。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from agent_harness.recovery.scan import ScanRecovery, scan_interrupted_sessions
from agent_harness.session import (
    OPERATION_RECONCILED,
    RUN_INTERRUPTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.errors import EventLogCorruptError
from agent_harness.session.service import SessionService
from agent_harness.session.store import EventLogIntegrity
from agent_harness.storage import Operation, OperationState, SqliteOperationLedger

#: 恢复入口的拒绝文案锚点（与 `tests/session/test_event_log_integrity.py` 同源）。
_REFUSAL_MARKER = "事件日志损坏，恢复已拒绝"


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


def _scan(store: JsonlSessionStore, tmp_path: Path):
    ledger = SqliteOperationLedger(tmp_path / "harness.db")
    asyncio.run(ledger.initialize())
    results = asyncio.run(scan_interrupted_sessions(
        session_store=store,
        operation_ledger=ledger,
        workspace_registry=None,
        database_path=tmp_path / "harness.db",
    ))
    return results, ledger


def _snapshot(store: JsonlSessionStore, session_id: str) -> tuple[str, int, int]:
    payload = store.events_path(session_id).read_bytes()
    return (
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        len(store.read_events(session_id)),
    )


def _append_bytes(store: JsonlSessionStore, session_id: str, payload: bytes) -> None:
    with store.events_path(session_id).open("ab") as handle:
        handle.write(payload)


def _rewrite_lines(store: JsonlSessionStore, session_id: str, lines: list[bytes]) -> None:
    store.events_path(session_id).write_bytes(b"".join(lines))


def _lines(store: JsonlSessionStore, session_id: str) -> list[bytes]:
    return store.events_path(session_id).read_bytes().splitlines(keepends=True)


def _seed_unknown_operation(
    ledger: SqliteOperationLedger, *, session_id: str
) -> None:
    async def seed() -> None:
        await ledger.create(Operation(
            tool_call_id="call-1",
            session_id=session_id,
            run_id="run-1",
            agent_id="default",
            tool_name="bash",
            args_identity='{"command": "echo hi"}',
            state=OperationState.PENDING,
            started_at="2026-10-23T00:00:00+00:00",
        ))
        await ledger.update_state(session_id, "call-1", OperationState.RUNNING)
        await ledger.update_state(session_id, "call-1", OperationState.UNKNOWN)

    asyncio.run(seed())


def _mutate_complete_bad_line(store: JsonlSessionStore, session_id: str) -> None:
    _append_bytes(store, session_id, b"{not json}\n")


def _mutate_seq_gap(store: JsonlSessionStore, session_id: str) -> None:
    lines = _lines(store, session_id)
    _rewrite_lines(store, session_id, [lines[0], lines[1], lines[3]])


def _mutate_seq_duplicate(store: JsonlSessionStore, session_id: str) -> None:
    lines = _lines(store, session_id)
    _rewrite_lines(store, session_id, [lines[0], lines[1], lines[1], *lines[2:]])


def _mutate_partial_tail(store: JsonlSessionStore, session_id: str) -> None:
    _append_bytes(store, session_id, b'{"seq":4,"type":"user/mess')


def _mutate_nothing(store: JsonlSessionStore, session_id: str) -> None:
    return


class TestCorruptInputsAreRefusedBeforeRecoveryWrite:
    """损坏三类：字节 / 事件数不变 + 恢复写被拒绝 + 原因可见。"""

    @pytest.mark.parametrize(
        ("mutate", "reason_token"),
        [
            (_mutate_complete_bad_line, "reason=bad_json"),
            (_mutate_seq_gap, "seq 缺号 2"),
            (_mutate_seq_duplicate, "seq 重复 1"),
        ],
        ids=["complete_bad_line", "seq_gap", "seq_duplicate"],
    )
    def test_corrupt_log_is_untouched_and_refused(
        self, tmp_path: Path, mutate, reason_token: str
    ):
        store = JsonlSessionStore(root=tmp_path / "sessions")
        _open_run_session(store)
        mutate(store, "sid")
        before = _snapshot(store, "sid")

        results, _ledger = _scan(store, tmp_path)

        # 1. durable 文件零改动（字节 / 事件数都不变）。
        assert _snapshot(store, "sid") == before
        # 2. 扫描明确拒绝恢复写，并把原因交回调用方（不吞）。
        assert [result.session_id for result in results] == ["sid"]
        assert results[0].recovery is ScanRecovery.FAILED
        assert _REFUSAL_MARKER in (results[0].detail or "")
        assert reason_token in (results[0].detail or "")
        # 3. 没有任何恢复事实被写进坏日志。
        assert not any(
            event.type == RUN_INTERRUPTED for event in store.read_events("sid")
        )

    def test_fully_corrupt_log_is_refused_not_silently_skipped(self, tmp_path: Path):
        """零可解析事件的坏日志：同样明确拒绝（不是「没有中断就跳过」）。"""
        store = JsonlSessionStore(root=tmp_path / "sessions")
        path = store.events_path("sid")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"seq":0,"type":\n{"also":"bad"}\n')
        before = _snapshot(store, "sid")

        results, _ledger = _scan(store, tmp_path)

        assert _snapshot(store, "sid") == before
        assert [result.session_id for result in results] == ["sid"]
        assert results[0].recovery is ScanRecovery.FAILED
        assert _REFUSAL_MARKER in (results[0].detail or "")

    def test_unknown_tool_is_not_replayed_on_corrupt_log(self, tmp_path: Path):
        """损坏日志上的 UNKNOWN 高风险工具：不盲重放，Ledger 也不被扫描改动。"""
        store = JsonlSessionStore(root=tmp_path / "sessions")
        _open_run_session(store)
        ledger = SqliteOperationLedger(tmp_path / "harness.db")
        asyncio.run(ledger.initialize())
        _seed_unknown_operation(ledger, session_id="sid")
        _append_bytes(store, "sid", b"{not json}\n")
        before = _snapshot(store, "sid")

        results = asyncio.run(scan_interrupted_sessions(
            session_store=store,
            operation_ledger=ledger,
            workspace_registry=None,
            database_path=tmp_path / "harness.db",
        ))

        assert _snapshot(store, "sid") == before
        assert results[0].recovery is ScanRecovery.FAILED
        events = store.read_events("sid")
        assert not any(event.type == TOOL_RESULT for event in events)
        assert not any(event.type == OPERATION_RECONCILED for event in events)
        operations = asyncio.run(ledger.list_for_session("sid"))
        assert [operation.state for operation in operations] == [OperationState.UNKNOWN]


class TestLegalInputsStillRecover:
    """合法尾部半行与正常日志：按既有语义正常恢复（闸不误伤）。"""

    @pytest.mark.parametrize(
        "mutate",
        [_mutate_partial_tail, _mutate_nothing],
        ids=["partial_tail", "normal"],
    )
    def test_legal_log_is_recovered(self, tmp_path: Path, mutate):
        store = JsonlSessionStore(root=tmp_path / "sessions")
        _open_run_session(store)
        mutate(store, "sid")
        original_types = [event.type for event in store.read_events("sid")]

        results, _ledger = _scan(store, tmp_path)

        assert [result.session_id for result in results] == ["sid"]
        assert results[0].recovery is ScanRecovery.RECOVERED
        events = store.read_events("sid")
        # 合法前缀原样保留，恢复事实追加在后面。
        assert [event.type for event in events[: len(original_types)]] == original_types
        assert any(event.type == RUN_INTERRUPTED for event in events)


class TestSharedIntegrityContract:
    """扫描与 SessionService 走同一个判定函数（不是第二套宽松读法）。"""

    def test_scan_and_service_use_the_same_integrity_judgement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[str] = []
        original = EventLogIntegrity.raise_if_corrupt

        def recording(self: EventLogIntegrity, session_id: str) -> None:
            calls.append(session_id)
            original(self, session_id)

        monkeypatch.setattr(EventLogIntegrity, "raise_if_corrupt", recording)

        store = JsonlSessionStore(root=tmp_path / "sessions")
        _open_run_session(store)
        _append_bytes(store, "sid", b"{not json}\n")
        _scan(store, tmp_path)

        integrity = store.read_events_report("sid")[1]
        with pytest.raises(EventLogCorruptError):
            SessionService._raise_if_event_log_corrupt("sid", integrity)

        assert calls == ["sid", "sid"]

    def test_scan_refuses_when_the_shared_judgement_says_corrupt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """反向证明扫描确实**依赖**该判定：判定改口 → 扫描不再拒绝（红）。"""
        monkeypatch.setattr(EventLogIntegrity, "raise_if_corrupt", lambda self, sid: None)

        store = JsonlSessionStore(root=tmp_path / "sessions")
        _open_run_session(store)
        _append_bytes(store, "sid", b"{not json}\n")
        before = _snapshot(store, "sid")

        results, _ledger = _scan(store, tmp_path)

        assert results[0].recovery is ScanRecovery.RECOVERED
        assert _snapshot(store, "sid") != before
        assert any(
            event.type == RUN_INTERRUPTED for event in store.read_events("sid")
        )
        # 上面三条就是未接入闸时的旧行为：坏日志被恢复写入污染。
        assert results[0].detail is None
