"""启动崩溃扫描（Phase Multiturn T8 / #138）。

进程重启时对每个 session 检查「无终态 run」：追加 ``run/interrupted``（诚实
记录中断事实），再强制跑一遍 RecoveryCoordinator（Ledger reconcile：终态精确
回填、PENDING 按策略、UNKNOWN 需人工裁决）。

顺序固定为「先标记中断，再 reconcile」：
- 标记中断只写 run 级事实，不猜工具终态（不变量 #12：Checkpoint ≠ 副作用恢复）；
- reconcile 才判定工具调用结果，且 UNKNOWN 在没有 ReconcileCallback 时**安全
  拒绝**（``ReconcileRequired``），扫描把它记为「需人工确认」，不伪造结果、
  不盲目重跑（不变量 #14）。

**单进程假设（V1）**：扫描在「持有会话的进程」启动时执行一次（web lifespan）。
不变量 #22 要求事件流只有一个真相，而 ``RunManager`` 的在途 run 只存在于
本进程内存里——另一个进程无法区分「run 在跑」与「run 被崩溃打断」，所以
**不要**在会与长驻服务并发的短命命令（CLI run / 子命令）里扫描：那会把别的
进程在途 run 误标中断，进而让两边各自推算 seq 撞号。多进程/多 worker 需要
跨进程 run lease，属后续 Phase。

该单进程假设已提升为正式架构约束，见 ``CONTEXT.md`` 的
**StartupInterruptionScan** 条目——它是本扫描**不能**被吸收进
``RecoveryCoordinator`` 的根本原因（另一原因是职责层次不同：扫描是
「批量 + 三分类结论 + 单会话隔离」，recover 是「单 session 8 步编排」）。

单个 session 失败不阻断整轮扫描（一个坏会话不该拖垮进程启动）；失败以
``ScanRecovery.FAILED`` + detail 如实上报。``run/interrupted`` 是终态，所以
失败的 session 不会被下一轮扫描重试——但它的悬空 tool_call 仍在，续聊/``/recover``
会再次进入 reconcile（Ledger reconcile 可安全重试），因此不会永久卡死。
同步 JSONL 读写（含 fsync）统一用 ``anyio.to_thread.run_sync`` 下放线程，
不阻塞事件循环。

#555 在同一 lifespan 里并列第二条扫描：``scan_unfinished_forks``——fork 是
多步物理过程（child 文件 → workspace 拷贝 → seed 移植 → provenance → 索引），
进程 kill 留下的「有 ``fork/in-progress`` 标记、无 ``session/forked``」现场
由它回收（只回收 harness 自建工件，见函数 docstring）。单进程假设相同。
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import anyio.to_thread

from agent_harness.agent.run_budget import (
    CLOSEOUT_DETERMINISTIC,
    REASON_USER_INPUT,
    TRIGGER_USER_INPUT,
    SessionBudgetSnapshot,
    build_pause_data,
    derive_run_budget,
    deterministic_continuation,
    latest_paused_run,
    latest_run_id,
    session_budget_key,
)
from agent_harness.recovery.coordinator import (
    ReconcileRequired,
    RecoveryCoordinator,
    RecoveryError,
)
from agent_harness.recovery.reconcile import ReconcileCallback
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session.event import (
    FORK_IN_PROGRESS,
    RUN_INTERRUPTED,
    RUN_PAUSED,
    SESSION_FORKED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.session.interrupt import InterruptedRun, detect_unterminated_runs
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage.operation import OperationLedger

logger = logging.getLogger("agent_harness.recovery.scan")


class ScanRecovery(str, Enum):
    """单个 session 的扫描结论。"""

    RECOVERED = "recovered"
    NEEDS_MANUAL_RECONCILE = "needs_manual_reconcile"
    FAILED = "failed"


@dataclass(frozen=True)
class InterruptionScanResult:
    """一个被打断 session 的扫描结论。

    ``interrupted`` = 追加 ``run/interrupted`` 的 run 列表；``recovery`` =
    recovered / needs_manual_reconcile / failed；``detail`` 仅在非 recovered 时有值。
    """

    session_id: str
    interrupted: list[InterruptedRun] = field(default_factory=list)
    recovery: ScanRecovery = ScanRecovery.RECOVERED
    detail: str | None = None
    budget_recovery_failed: bool = False


async def scan_interrupted_sessions(
    *,
    session_store: JsonlSessionStore,
    operation_ledger: OperationLedger,
    workspace_registry: WorkspaceRegistry | None,
    database_path: str | Path | None,
    reconcile_callback: ReconcileCallback | None = None,
    session_budget_reader: Callable[[str], Awaitable[SessionBudgetSnapshot | None]]
    | None = None,
    session_tool_result_recorder: Callable[..., Awaitable[bool]] | None = None,
    lock_timeout_seconds: float = 5.0,
) -> list[InterruptionScanResult]:
    """扫描全部 session，标记中断并 reconcile；返回被处理 session 的结论。

    无终态 run 的 session 才处理（幂等：``run/interrupted`` 本身是终态，
    重复扫描不会重复追加）。无中断的 session 不出现在返回值里。
    """
    results: list[InterruptionScanResult] = []
    session_ids = await anyio.to_thread.run_sync(session_store.list_session_ids)
    for session_id in session_ids:
        try:
            events = await anyio.to_thread.run_sync(
                session_store.read_events, session_id,
            )
            budget_key = session_budget_key(events, session_id=session_id)
            try:
                await _replay_constraint_tool_budget(
                    events,
                    budget_key=budget_key,
                    recorder=session_tool_result_recorder,
                    reader_configured=session_budget_reader is not None,
                )
            except Exception as error:  # noqa: BLE001 - budget replay must fail closed
                results.append(InterruptionScanResult(
                    session_id=session_id,
                    recovery=ScanRecovery.FAILED,
                    detail=f"protected-constraint session budget replay failed: {error}",
                    budget_recovery_failed=True,
                ))
                continue

            pending_input = await anyio.to_thread.run_sync(
                _pending_unpaused_user_input, events,
            )
            if pending_input is not None:
                coordinator = RecoveryCoordinator(
                    session_store=session_store,
                    workspace_registry=workspace_registry,
                    operation_ledger=operation_ledger,
                    reconcile_callback=reconcile_callback,
                    database_path=database_path,
                    lock_timeout_seconds=lock_timeout_seconds,
                )
                recovery_error: Exception | None = None
                try:
                    await coordinator.recover(session_id)
                except (ReconcileRequired, RecoveryError) as error:
                    recovery_error = error
                try:
                    events = await anyio.to_thread.run_sync(
                        session_store.read_events, session_id,
                    )
                    await _replay_constraint_tool_budget(
                        events,
                        budget_key=budget_key,
                        recorder=session_tool_result_recorder,
                        reader_configured=session_budget_reader is not None,
                    )
                    session_budget = (
                        await session_budget_reader(budget_key)
                        if session_budget_reader is not None else None
                    )
                    if session_budget_reader is not None and session_budget is None:
                        raise RecoveryError(
                            "待处理用户问题缺少 session budget 快照；拒绝伪造暂停状态"
                        )
                except Exception as error:  # noqa: BLE001 - budget replay must fail closed
                    results.append(InterruptionScanResult(
                        session_id=session_id,
                        recovery=ScanRecovery.FAILED,
                        detail=str(error),
                        budget_recovery_failed=True,
                    ))
                    continue
                await anyio.to_thread.run_sync(
                    _append_pending_user_input_pause,
                    session_store, session_id, session_budget,
                )
                if isinstance(recovery_error, ReconcileRequired):
                    results.append(InterruptionScanResult(
                        session_id=session_id,
                        recovery=ScanRecovery.NEEDS_MANUAL_RECONCILE,
                        detail=str(recovery_error),
                    ))
                elif recovery_error is not None:
                    results.append(InterruptionScanResult(
                        session_id=session_id,
                        recovery=ScanRecovery.FAILED,
                        detail=str(recovery_error),
                    ))
                else:
                    results.append(InterruptionScanResult(
                        session_id=session_id,
                        recovery=ScanRecovery.RECOVERED,
                    ))
                continue
            interrupted = await anyio.to_thread.run_sync(
                _mark_interrupted, session_store, session_id
            )
            if not interrupted:
                continue
            logger.warning(
                "启动扫描：session=%s 标记 %d 个中断 run（seq=%s）",
                session_id, len(interrupted),
                [r.interrupted_seq for r in interrupted],
            )
            coordinator = RecoveryCoordinator(
                session_store=session_store,
                workspace_registry=workspace_registry,
                operation_ledger=operation_ledger,
                reconcile_callback=reconcile_callback,
                database_path=database_path,
                lock_timeout_seconds=lock_timeout_seconds,
            )
            try:
                await coordinator.recover(session_id)
            except ReconcileRequired as error:
                # UNKNOWN 工具调用需人工裁决——如实标记，不伪造、不重跑。
                logger.warning(
                    "启动扫描：session=%s 需人工 reconcile：%s", session_id, error
                )
                results.append(InterruptionScanResult(
                    session_id=session_id, interrupted=interrupted,
                    recovery=ScanRecovery.NEEDS_MANUAL_RECONCILE,
                    detail=str(error),
                ))
            except RecoveryError as error:
                logger.exception(
                    "启动扫描：session=%s reconcile 失败", session_id,
                )
                results.append(InterruptionScanResult(
                    session_id=session_id, interrupted=interrupted,
                    recovery=ScanRecovery.FAILED, detail=str(error),
                ))
            else:
                try:
                    events = await anyio.to_thread.run_sync(
                        session_store.read_events, session_id,
                    )
                    await _replay_constraint_tool_budget(
                        events,
                        budget_key=budget_key,
                        recorder=session_tool_result_recorder,
                        reader_configured=session_budget_reader is not None,
                    )
                except Exception as error:  # noqa: BLE001 - replay after recovery must fail closed
                    results.append(InterruptionScanResult(
                        session_id=session_id,
                        interrupted=interrupted,
                        recovery=ScanRecovery.FAILED,
                        detail=f"protected-constraint session budget replay failed: {error}",
                        budget_recovery_failed=True,
                    ))
                    continue
                results.append(InterruptionScanResult(
                    session_id=session_id, interrupted=interrupted,
                ))
        except Exception as error:  # 单会话失败不阻断整轮扫描
            logger.exception("启动扫描：session=%s 处理异常", session_id)
            results.append(InterruptionScanResult(
                session_id=session_id,
                recovery=ScanRecovery.FAILED,
                detail=str(error),
            ))
    return results


async def _record_constraint_tool_results(
    events: list[SessionEvent], *, budget_key: str,
    recorder: Callable[..., Awaitable[bool]],
) -> int:
    """Replay the protected-fact question tool's durable delta before pause projection."""
    candidates = _constraint_tool_result_candidates(events)
    calls_by_id: dict[str, list[SessionEvent]] = {}
    for event in events:
        tool_call_id = event.data.get("tool_call_id")
        if event.type == TOOL_CALL and isinstance(tool_call_id, str) and tool_call_id:
            calls_by_id.setdefault(tool_call_id, []).append(event)
    seen_result_ids: set[str] = set()
    recorded = 0
    for event in candidates:
        tool_call_id = event.data.get("tool_call_id")
        delta = event.data.get("budget_delta")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise RecoveryError("澄清工具结果缺少有效 tool_call_id")
        matching_calls = calls_by_id.get(tool_call_id, [])
        if len(matching_calls) != 1:
            raise RecoveryError(
                "澄清工具结果必须匹配唯一的 tool_call"
                f"（tool_call_id={tool_call_id}）"
            )
        call = matching_calls[0]
        if (
            call.data.get("tool_name") != "request_constraint_resolution"
            or call.run_id != event.run_id
            or (
                event.step_id is not None
                and call.step_id != event.step_id
            )
        ):
            raise RecoveryError(
                "澄清工具结果与 tool_call 的名称或运行位置不匹配"
                f"（tool_call_id={tool_call_id}）"
            )
        if tool_call_id in seen_result_ids:
            raise RecoveryError(
                f"澄清工具结果重复（tool_call_id={tool_call_id}）"
            )
        seen_result_ids.add(tool_call_id)
        if not isinstance(event.data.get("content"), str):
            raise RecoveryError(
                f"澄清工具结果缺少有效 content（tool_call_id={tool_call_id}）"
            )
        if (
            not isinstance(delta, dict)
            or delta.get("tool_name") != "request_constraint_resolution"
        ):
            raise RecoveryError(
                f"澄清工具结果缺少匹配的预算增量（tool_call_id={tool_call_id}）"
            )
        calls = delta.get("tool_calls")
        attempts = delta.get("tool_attempts")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (calls, attempts)
        ):
            raise RecoveryError(
                f"澄清工具结果的预算增量无效（tool_call_id={tool_call_id}）"
            )
        await recorder(
            budget_key,
            tool_call_id=tool_call_id,
            tool_name="request_constraint_resolution",
            calls=calls,
            attempts=attempts,
        )
        recorded += 1
    return recorded


def _constraint_tool_result_candidates(
    events: list[SessionEvent],
) -> list[SessionEvent]:
    """Identify result events that claim or could be a constraint tool result.

    Pairing validation belongs to the replay path. This shared candidate predicate also
    makes the no-recorder guard fail closed for malformed target deltas.
    """
    constraint_calls = [
        event for event in events
        if event.type == TOOL_CALL
        and event.data.get("tool_name") == "request_constraint_resolution"
    ]
    constraint_ids = {
        event.data.get("tool_call_id")
        for event in constraint_calls
        if isinstance(event.data.get("tool_call_id"), str)
        and event.data["tool_call_id"]
    }
    constraint_contexts = {
        (event.run_id, event.step_id) for event in constraint_calls
    }
    all_call_ids = {
        event.data.get("tool_call_id")
        for event in events
        if event.type == TOOL_CALL
        and isinstance(event.data.get("tool_call_id"), str)
        and event.data["tool_call_id"]
    }
    candidates: list[SessionEvent] = []
    for event in events:
        if event.type != TOOL_RESULT:
            continue
        tool_call_id = event.data.get("tool_call_id")
        delta = event.data.get("budget_delta")
        claims_constraint_budget = (
            isinstance(delta, dict)
            and delta.get("tool_name") == "request_constraint_resolution"
        )
        matches_constraint_call = (
            isinstance(tool_call_id, str) and tool_call_id in constraint_ids
        )
        is_unidentified_or_orphaned_in_constraint_step = (
            (not isinstance(tool_call_id, str) or not tool_call_id
             or tool_call_id not in all_call_ids)
            and (event.run_id, event.step_id) in constraint_contexts
        )
        if (
            claims_constraint_budget
            or matches_constraint_call
            or is_unidentified_or_orphaned_in_constraint_step
        ):
            candidates.append(event)
    return candidates


async def record_constraint_tool_results(
    events: list[SessionEvent], *, budget_key: str,
    recorder: Callable[..., Awaitable[bool]],
) -> int:
    """Public idempotent replay entry for durable protected-constraint results."""
    return await _record_constraint_tool_results(
        events, budget_key=budget_key, recorder=recorder,
    )


async def _replay_constraint_tool_budget(
    events: list[SessionEvent],
    *,
    budget_key: str,
    recorder: Callable[..., Awaitable[bool]] | None,
    reader_configured: bool,
) -> None:
    if recorder is not None:
        await _record_constraint_tool_results(
            events, budget_key=budget_key, recorder=recorder,
        )
    elif reader_configured and _has_constraint_tool_result(events):
        raise RecoveryError(
            "constraint tool result has no idempotent session budget recorder"
        )


def _pending_unpaused_user_input(
    events: list[SessionEvent],
) -> str | None:
    run_id = latest_run_id(events)
    if run_id is None or latest_paused_run(events) is not None:
        return None
    if not any(item.run_id == run_id for item in detect_unterminated_runs(events)):
        return None
    answered = {
        event.data.get("input_request_id")
        for event in events
        if event.type == USER_MESSAGE
        and isinstance(event.data.get("input_request_id"), str)
    }
    request = next(
        (
            event for event in reversed(events)
            if event.type == USER_INPUT_REQUESTED
            and event.run_id == run_id
            and isinstance(event.data.get("request_id"), str)
            and event.data["request_id"] not in answered
        ),
        None,
    )
    return request.data["request_id"] if request is not None else None


def _has_constraint_tool_result(events: list[SessionEvent]) -> bool:
    return bool(_constraint_tool_result_candidates(events))


def _append_pending_user_input_pause(
    session_store: JsonlSessionStore,
    session_id: str,
    session_budget: SessionBudgetSnapshot | None = None,
) -> None:
    events = session_store.read_events(session_id)
    run_id = latest_run_id(events)
    if run_id is None or latest_paused_run(events) is not None:
        return
    answered = {
        event.data.get("input_request_id")
        for event in events
        if event.type == USER_MESSAGE
        and isinstance(event.data.get("input_request_id"), str)
    }
    request = next(
        (
            event for event in reversed(events)
            if event.type == USER_INPUT_REQUESTED
            and event.run_id == run_id
            and isinstance(event.data.get("request_id"), str)
            and event.data["request_id"] not in answered
        ),
        None,
    )
    if request is None:
        return
    state = derive_run_budget(events, run_id)
    request_id = request.data["request_id"]
    continuation = deterministic_continuation(
        events=events, run_id=run_id, trigger_dimension=TRIGGER_USER_INPUT,
        limits=state.limits, consumed=state.consumed,
        reason=REASON_USER_INPUT, input_request_id=request_id,
    )
    limits = {"local": None, "run": state.limits.as_projection()}
    if session_budget is not None:
        limits["session"] = session_budget.limits.as_projection()
    data = build_pause_data(
        reason=REASON_USER_INPUT, trigger_dimension=TRIGGER_USER_INPUT,
        version=state.version, consumed=state.consumed,
        limits=limits,
        continuation=continuation, closeout_source=CLOSEOUT_DETERMINISTIC,
        input_request_id=request_id,
    )
    if session_budget is not None:
        data["session"] = {
            "version": session_budget.version,
            "consumed": session_budget.consumed.as_projection(),
        }
    Session.append_event(
        session_store, session_id, RUN_PAUSED, data,
        run_id=run_id, step_id=request.step_id,
    )


def _mark_interrupted(
    session_store: JsonlSessionStore, session_id: str
) -> list[InterruptedRun]:
    """同步块：读事件 → 检测无终态 run → 逐个补记 run/interrupted。

    整块（含 append 的 fsync）在调用方下放线程执行。
    """
    events = session_store.read_events(session_id)
    interrupted = detect_unterminated_runs(events)
    for run in interrupted:
        Session.append_event(
            session_store,
            session_id,
            RUN_INTERRUPTED,
            {
                "interrupted_seq": run.interrupted_seq,
                "reason": "process_restart",
            },
            run_id=run.run_id,
            agent_id=run.agent_id,
            step_id=run.step_id,
        )
    return interrupted


@dataclass(frozen=True)
class ForkScanResult:
    """一个未完成 fork（child）的扫描结论。

    ``reclaimed`` = 本次是否执行了工件回收；``detail`` 仅在扫描/回收失败时有值。
    """

    session_id: str
    reclaimed: bool = False
    detail: str | None = None


async def scan_unfinished_forks(
    *,
    session_store: JsonlSessionStore,
    workspace_registry: WorkspaceRegistry | None,
) -> list[ForkScanResult]:
    """启动时按 ``fork/in-progress`` 标记回收未完成 fork 的残留（#555）。

    判定（durable、以 child 文件为准）：child 事件含 ``fork/in-progress`` 且
    无 ``session/forked``——fork 多步物理过程被打断的充分条件（标记在 workspace
    拷贝之前 fsync，拷贝不可能在标记缺席时启动；``session/forked`` 是终点）。

    回收范围**只有 harness 自建工件**：

    - ``<root>/.fork-tmp/``（fork 暂存根整体清空——单进程假设下启动时无在途
      fork，残留即垃圾；放 registry 根直下正是为了让"整体清空"安全，不会碰到
      用户命名 workspace）；
    - 每个未完成 child 的**默认形态**工作区（``<root>/workspaces/<sid>``——
      构造规则白名单，与 ``discard_session_artifacts`` 同源，映射指向用户
      目录时绝不碰）。

    child 的 JSONL（含标记）与映射文件**保留**——「fork 未完成」是可读事实，
    续聊对账（``_reconcile_workspace_binding``）按它给 fork 专属提示；父会话
    文件零改动。回收幂等（重复扫描不会重复增长残留）；未完成 child 本身持续
    可见（直到用户删除该会话），不是扫描的错误。

    与 ``scan_interrupted_sessions`` 相同的单进程假设：只在持有会话的进程
    启动时执行（web lifespan），不在与长驻服务并发的短命命令里跑。单个
    child 读取失败不阻断整轮（FAIL 细节如实在 ``ForkScanResult.detail``）。
    """
    unfinished, failures = await anyio.to_thread.run_sync(
        _detect_unfinished_forks, session_store
    )
    results = [
        ForkScanResult(session_id=sid, reclaimed=False, detail=detail)
        for sid, detail in failures
    ]
    for sid in unfinished:
        logger.warning(
            "启动扫描：session=%s 是未完成的 fork（fork/in-progress 无 session/forked）",
            sid,
        )
    if workspace_registry is not None:
        reclaimed = await anyio.to_thread.run_sync(
            _reclaim_fork_residue, workspace_registry, unfinished
        )
        for sid in unfinished:
            if reclaimed:
                logger.warning(
                    "启动扫描：未完成 fork child=%s 的暂存/工作区残留已回收", sid
                )
                results.append(ForkScanResult(session_id=sid, reclaimed=True))
            else:
                results.append(ForkScanResult(session_id=sid, reclaimed=False))
    else:
        # 无注册表（纯事件层部署）：只报告可见性，无工件可回收。
        results.extend(ForkScanResult(session_id=sid) for sid in unfinished)
    return results


def _detect_unfinished_forks(
    session_store: JsonlSessionStore,
) -> tuple[list[str], list[tuple[str, str]]]:
    """同步块：逐 child 读事件，判「有 fork/in-progress 无 session/forked」。

    返回（未完成 child 列表, 读取失败 [(sid, detail)]）；单文件失败不阻断其余。
    """
    unfinished: list[str] = []
    failures: list[tuple[str, str]] = []
    for sid in session_store.list_session_ids():
        try:
            events = session_store.read_events(sid)
        except Exception as error:
            logger.warning(
                "启动 fork 扫描：child=%s 事件读取失败", sid, exc_info=True
            )
            failures.append((sid, str(error)))
            continue
        has_intent = any(e.type == FORK_IN_PROGRESS for e in events)
        if not has_intent:
            continue
        if any(e.type == SESSION_FORKED for e in events):
            continue
        unfinished.append(sid)
    return unfinished, failures


def _reclaim_fork_residue(
    registry: WorkspaceRegistry, unfinished: list[str]
) -> bool:
    """同步块：清空暂存根 + 回收未完成 child 的默认形态工作区（幂等，best-effort）。

    任一步失败只记日志不抛——启动路径的回收失败留给下一次启动重试（标记仍在）。
    返回是否至少尝试了回收。
    """
    try:
        staging_root = registry.fork_staging_root()
        if staging_root.is_dir():
            shutil.rmtree(staging_root, ignore_errors=True)
    except Exception:
        logger.warning("启动扫描：fork 暂存根清空失败", exc_info=True)
    for sid in unfinished:
        try:
            default_workspace = registry.default_workspace_root(sid)
            if default_workspace.is_dir():
                shutil.rmtree(default_workspace, ignore_errors=True)
        except Exception:
            logger.warning(
                "启动扫描：未完成 fork child=%s 工作区回收失败", sid, exc_info=True
            )
    return True
