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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import anyio.to_thread

from agent_harness.agent.run_budget import (
    session_budget_key,
    session_model_request_accounting,
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
    MODEL_REQUEST,
    MODEL_REQUEST_STARTED,
    RUN_INTERRUPTED,
    SESSION_FORKED,
    SessionEvent,
)
from agent_harness.session.interrupt import InterruptedRun, detect_unterminated_runs
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger
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


async def scan_interrupted_sessions(
    *,
    session_store: JsonlSessionStore,
    operation_ledger: OperationLedger,
    workspace_registry: WorkspaceRegistry | None,
    database_path: str | Path | None,
    reconcile_callback: ReconcileCallback | None = None,
    session_budget_ledger: SqliteDelegationTreeLedger | None = None,
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
            interrupted = await anyio.to_thread.run_sync(
                _mark_interrupted, session_store, session_id
            )
            budget_error = None
            if session_budget_ledger is not None:
                events = await anyio.to_thread.run_sync(
                    session_store.read_events, session_id,
                )
                try:
                    await _recover_session_model_requests(
                        session_id, events, session_budget_ledger,
                    )
                except Exception as error:  # per-marker failure remains fail-closed
                    logger.exception(
                        "启动扫描：session=%s SessionBudget model request reconcile failed",
                        session_id,
                    )
                    budget_key = session_budget_key(events, session_id=session_id)
                    markers = await session_budget_ledger.pending_model_request_accountings(
                        budget_key, session_id=session_id,
                    )
                    for marker in markers:
                        session_budget_ledger.block_model_request_accounting(
                            budget_key, marker.accounting_id,
                        )
                    budget_error = str(error)
            if not interrupted:
                if budget_error is not None:
                    results.append(InterruptionScanResult(
                        session_id=session_id, recovery=ScanRecovery.FAILED,
                        detail=f"SessionBudget reconcile failed: {budget_error}",
                    ))
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
            operation_recovery = ScanRecovery.RECOVERED
            operation_detail = None
            try:
                await coordinator.recover(session_id)
            except ReconcileRequired as error:
                # UNKNOWN 工具调用需人工裁决——如实标记，不伪造、不重跑。
                logger.warning(
                    "启动扫描：session=%s 需人工 reconcile：%s", session_id, error
                )
                operation_recovery = ScanRecovery.NEEDS_MANUAL_RECONCILE
                operation_detail = str(error)
            except RecoveryError as error:
                logger.exception(
                    "启动扫描：session=%s reconcile 失败", session_id,
                )
                operation_recovery = ScanRecovery.FAILED
                operation_detail = str(error)
            if budget_error is not None:
                results.append(InterruptionScanResult(
                    session_id=session_id, interrupted=interrupted,
                    recovery=ScanRecovery.FAILED,
                    detail=f"SessionBudget reconcile failed: {budget_error}; "
                           f"operation recovery={operation_recovery.value}: "
                           f"{operation_detail or 'ok'}",
                ))
            else:
                results.append(InterruptionScanResult(
                    session_id=session_id, interrupted=interrupted,
                    recovery=operation_recovery, detail=operation_detail,
                ))
        except Exception as error:  # 单会话失败不阻断整轮扫描
            logger.exception("启动扫描：session=%s 处理异常", session_id)
            results.append(InterruptionScanResult(
                session_id=session_id,
                recovery=ScanRecovery.FAILED,
                detail=str(error),
            ))
    return results


async def _recover_session_model_requests(
    session_id: str, events: list[SessionEvent], ledger: SqliteDelegationTreeLedger,
) -> None:
    budget_key = session_budget_key(events, session_id=session_id)
    markers = await ledger.pending_model_request_accountings(
        budget_key, session_id=session_id,
    )
    for marker in markers:
        def in_interval(
            event, *, after_seq=marker.after_seq, before_seq=marker.before_seq,
            run_id=marker.run_id, step_id=marker.step_id,
        ) -> bool:
            return (
                event.seq is not None and event.seq >= after_seq
                and (before_seq is None or event.seq < before_seq)
                and event.run_id == run_id
                and event.step_id == step_id
            )

        requests = [
            event for event in events
            if event.type == MODEL_REQUEST and in_interval(event)
        ]
        started = [
            event for event in events
            if event.type == MODEL_REQUEST_STARTED and in_interval(event)
        ]
        if not requests:
            if started:
                await ledger.resolve_unsettled_session_model_request_accounting(
                    budget_key, marker.accounting_id,
                )
            else:
                await ledger.resolve_empty_session_model_request_accounting(
                    budget_key, marker.accounting_id,
                    refund_step=marker.reserved_requests == 1,
                )
            continue

        request_ids, usage, cost = session_model_request_accounting(requests)
        await ledger.record_session_model_requests(
            budget_key,
            count=max(len(requests) - marker.reserved_requests, 0),
            usage=usage,
            cost=cost,
            accounting_id=marker.accounting_id,
            request_ids=request_ids,
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
