"""RecoveryCoordinator：崩溃后按冻结顺序恢复 Session 的唯一编排入口。

07 §9 冻结的 8 步恢复顺序（ADR-0004 Round 4 §Q12）：
    1. load SessionEvent → Session（原始事件，不先做 dangling 占位）
    2. load Session-Sandbox mapping → WorkspaceRegistry.get()
    3. ensure Sandbox started（registry.get() 内部调用 ensure_started）
    4. load Operation Ledger → 未终止 Operation
    5. reconcile 未终止 operations（#29：终态 3 种 + PENDING skip；UNKNOWN/RUNNING 留给 #30）
    6. restore tool result / message consistency → append 合成事件
    7. rebuild Runtime Context（derive_messages 从恢复后事件重新投影）
    8. return Session，交给 AgentRuntime 继续

关键设计（均来自 ADR-0004 / Issue #26）：
- Ledger-first 顺序保证 Ledger 永远比 SessionEvent 更完整：崩溃时 Ledger 有终态但
  SessionEvent 缺配对 → 本协调器用原 tool_call_id 合成 Recovery ToolResult。
- 先决策后写结果：reconcile 决策（读 Ledger + 投影）全部完成后再 append 事件；
  任何失败向上抛原异常（锁在 finally 释放），不写"恢复完成"标记，重试安全——
  重试重新读状态重新决策，已合成的结果事件让剩余计划自然缩小（幂等）。
- 并发恢复串行化：SQLite 无行级锁——用 BEGIN EXCLUSIVE 事务在【数据库级】悲观串行化，
  第二个恢复方阻塞直到第一个完成或 busy_timeout 超时。WAL 模式下读者不被阻塞，
  恢复期间经 OperationLedger 的只读查询正常进行。
- Session.resume() 保持现状（只管 events）；本协调器自行组装 Session，
  避免 resume 的 Phase 1 dangling 占位抢在 Ledger reconcile 之前污染事件流。
  Ledger 不知道的 dangling call（如 validation 失败未建 Operation）仍回退
  Phase 1 占位语义（07 §8 不留 dangling call）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session import (
    MODEL_COMPLETED,
    SESSION_RESUMED,
    TOOL_CALL,
    TOOL_RESULT,
    JsonlSessionStore,
    Session,
    SessionEvent,
)
from agent_harness.session.derive import DANGLING_TOOL_CONTENT
from agent_harness.storage import Operation, OperationLedger, OperationState
from agent_harness.tooling import ErrorCode, ToolResult

#: RUNNING / UNKNOWN / NEED_RECONCILE 是 #30（ReconcileCallback 人工裁决）的输入，
#: #29 协调器一律不碰——不能为副作用状态未知的调用伪造结果（不变量 #14）。
_UNRESOLVED_FOR_TICKET_30 = frozenset(
    {OperationState.RUNNING, OperationState.UNKNOWN, OperationState.NEED_RECONCILE}
)

#: Ledger 终态：result_json 就是准确的恢复事实来源（07 §6）。
_TERMINAL_STATES = frozenset(
    {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.CANCELLED,
    }
)


class RecoveryError(Exception):
    """恢复入口的确定性失败（如锁超时）。调用方可安全重试。"""


class PendingPolicy(ABC):
    """PENDING Operation 的恢复策略 seam（ADR-0004 Round 3 §Q13）。

    Ledger-first 顺序下 PENDING 极罕见（微秒级窗口）；默认 skip 最安全，
    未来需要改 retry 时替换实现即可。
    """

    @abstractmethod
    def result_for(self, operation: Operation) -> ToolResult:
        """为 PENDING Operation 决定恢复时合成的 ToolResult（只做决策，不执行）。"""


class SkipPendingPolicy(PendingPolicy):
    """默认策略：skip——合成显式"未启动即跳过"结果，不自动执行。"""

    def result_for(self, operation: Operation) -> ToolResult:
        return ToolResult.failure(
            message=(
                f"操作 '{operation.tool_name}' 在进程崩溃前尚未启动，"
                "恢复时按策略跳过，未自动重新执行。"
            ),
            error_code=ErrorCode.CANCELLED,
            retryable=False,
        )


@dataclass(frozen=True)
class _Synthesis:
    """一条已决策待写入的恢复结果（决策与写入分离的载体）。

    content 是 tool/result 事件的最终载荷文本：Ledger 终态 / PENDING 路径是
    ToolResult JSON；Ledger 无记录的占位路径与 Phase 1 逐字一致（原始文本）。
    """

    tool_call_id: str
    tool_name: str
    content: str
    needs_call_event: bool  # tool/call 事件缺失时一并补齐
    run_id: str | None
    agent_id: str | None


class RecoveryCoordinator:
    """按 07 §9 冻结顺序恢复 Session 的唯一编排入口。

    注入：SessionStore + WorkspaceRegistry + OperationLedger + PendingPolicy。
    database_path 提供共享存储 SQLite 文件时启用悲观恢复锁（BEGIN EXCLUSIVE，
    数据库级串行化——SQLite 无行级锁）；为 None 时不加锁（单进程/测试场景）。
    """

    def __init__(
        self,
        *,
        session_store: JsonlSessionStore,
        workspace_registry: WorkspaceRegistry | None,
        operation_ledger: OperationLedger,
        pending_policy: PendingPolicy | None = None,
        database_path: str | Path | None = None,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self._session_store = session_store
        self._workspace_registry = workspace_registry
        self._operation_ledger = operation_ledger
        self._pending_policy = pending_policy or SkipPendingPolicy()
        self._database_path = Path(database_path) if database_path else None
        self._lock_timeout_seconds = lock_timeout_seconds

    async def recover(self, session_id: str) -> Session:
        """恢复一个 Session：8 步顺序执行，返回可直接交给 AgentRuntime 的 Session。"""
        async with self._recovery_lock():
            # 步骤 1：load SessionEvent（原始事件；dangling 占位留到步骤 6，
            # 避免 Phase 1 占位抢在 Ledger reconcile 之前）。
            events = self._session_store.read_events(session_id)
            if not events:
                raise RecoveryError(f"Session '{session_id}' 不存在或事件日志为空")

            # 步骤 2-3：load Session-Sandbox mapping + ensure Sandbox started。
            # 无映射记录（纯对话 session）优雅降级为 None，不让恢复失败。
            sandbox = None
            if self._workspace_registry is not None and self._workspace_registry.exists(
                session_id
            ):
                sandbox = self._workspace_registry.get(session_id)

            session = Session(
                session_id, self._session_store, list(events), sandbox=sandbox
            )

            # 步骤 4：load Operation Ledger。
            operations = await self._operation_ledger.list_for_session(session_id)
            operations_by_call_id = {op.tool_call_id: op for op in operations}

            # 步骤 5（决策阶段）：对每个 dangling tool_call 决定恢复结果。
            # 决策全部完成前不写任何事件（先决策后写结果）。
            dangling_ids, call_event_ids = self._dangling_state(session.events)
            plan: list[_Synthesis] = []
            for tool_call_id in sorted(dangling_ids):
                synthesis = self._decide(
                    tool_call_id,
                    operations_by_call_id.get(tool_call_id),
                    needs_call_event=tool_call_id not in call_event_ids,
                )
                if synthesis is not None:
                    plan.append(synthesis)

            # 步骤 6（写入阶段）：按决策 append 合成事件，restore 一致性。
            for item in plan:
                if item.needs_call_event:
                    session.append(
                        TOOL_CALL,
                        {
                            "tool_call_id": item.tool_call_id,
                            "tool_name": item.tool_name,
                            "args": {},
                        },
                        run_id=item.run_id,
                        agent_id=item.agent_id,
                    )
                session.append(
                    TOOL_RESULT,
                    {"tool_call_id": item.tool_call_id, "content": item.content},
                    run_id=item.run_id,
                    agent_id=item.agent_id,
                )
            # 与 Session.resume 的事件契约对齐：恢复完成标记 session/resumed。
            session.append(SESSION_RESUMED, {})

            # 步骤 7：rebuild Runtime Context——从恢复后的持久事件重新投影。
            # derive_messages 是纯函数；此处调用一次验证投影一致（不留 dangling 警告）。
            session.derive_messages()

            # 步骤 8：返回 Session，交给 AgentRuntime 继续。
            return session

    # ── 内部实现 ──

    @asynccontextmanager
    async def _recovery_lock(self) -> AsyncIterator[None]:
        """SQLite 悲观恢复锁。

        诚实声明（#29 AC 明确要求）：SQLite 没有行级锁。BEGIN EXCLUSIVE 在
        【数据库级】串行化写者——同一 session 的并发恢复因此被串行化；
        不同 session 的恢复在单机 SQLite 下同样互斥，这是可接受的保守行为。
        WAL 模式下读者不被阻塞，恢复期间经 Ledger 的只读查询正常进行。
        第二个恢复方阻塞直到第一个完成（commit）或 busy_timeout 超时（抛 RecoveryError）。

        崩溃安全：持锁进程死亡时 SQLite 回滚其事务，锁自动释放——无陈旧锁问题。
        """
        if self._database_path is None:
            yield
            return
        connection = await aiosqlite.connect(
            self._database_path, timeout=self._lock_timeout_seconds
        )
        try:
            await connection.execute("PRAGMA journal_mode=WAL")
            try:
                await connection.execute("BEGIN EXCLUSIVE TRANSACTION")
            except aiosqlite.OperationalError as e:
                raise RecoveryError(
                    f"无法获取恢复锁（另一个恢复可能正在进行）：{e}。可安全重试。"
                ) from e
            try:
                yield
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        finally:
            await connection.close()

    @staticmethod
    def _dangling_state(
        events: list[SessionEvent],
    ) -> tuple[set[str], set[str]]:
        """返回 (dangling tool_call_ids, 已有 tool/call 事件的 ids)。

        dangling 的判定必须同时覆盖两个事实源：
        - tool/call 事件（detect_dangling / resume 一致性用）；
        - model/completed 的 tool_calls 字段（derive_messages 的 AIMessage 投影用）。
        缺 tool/call 事件时恢复要一并补齐，否则两个视角不一致。
        """
        requested: set[str] = set()
        call_event_ids: set[str] = set()
        resolved: set[str] = set()
        for event in events:
            if event.type == TOOL_CALL:
                tc_id = event.data.get("tool_call_id", "")
                if tc_id:
                    requested.add(tc_id)
                    call_event_ids.add(tc_id)
            elif event.type == MODEL_COMPLETED:
                for tc in event.data.get("tool_calls", []):
                    tc_id = tc.get("id", "")
                    if tc_id:
                        requested.add(tc_id)
            elif event.type == TOOL_RESULT:
                tc_id = event.data.get("tool_call_id", "")
                if tc_id:
                    resolved.add(tc_id)
        return requested - resolved, call_event_ids

    def _decide(
        self,
        tool_call_id: str,
        operation: Operation | None,
        *,
        needs_call_event: bool,
    ) -> _Synthesis | None:
        """对一个 dangling tool_call 做恢复决策（纯决策，不写任何状态）。

        - Ledger 终态 → 用 result_json 精确合成；
        - PENDING → PendingPolicy 决策（默认 skip）；
        - RUNNING/UNKNOWN/NEED_RECONCILE → 不碰（#30 的输入）；
        - Ledger 无记录 → Phase 1 占位（不留 dangling call）。
        """
        if operation is not None and operation.state in _UNRESOLVED_FOR_TICKET_30:
            return None

        run_id = operation.run_id if operation else None
        agent_id = operation.agent_id if operation else None

        if operation is None:
            # Phase 1 占位语义：Session.resume 的 content 就是 DANGLING_TOOL_CONTENT 原文。
            return _Synthesis(
                tool_call_id=tool_call_id,
                tool_name="unknown",
                content=DANGLING_TOOL_CONTENT,
                needs_call_event=needs_call_event,
                run_id=None,
                agent_id=None,
            )

        if operation.state in _TERMINAL_STATES:
            return _Synthesis(
                tool_call_id=tool_call_id,
                tool_name=operation.tool_name,
                content=self._content_from_ledger(operation),
                needs_call_event=needs_call_event,
                run_id=run_id,
                agent_id=agent_id,
            )

        if operation.state is OperationState.PENDING:
            result = self._pending_policy.result_for(operation)
            return _Synthesis(
                tool_call_id=tool_call_id,
                tool_name=operation.tool_name,
                content=result.model_dump_json(),
                needs_call_event=needs_call_event,
                run_id=run_id,
                agent_id=agent_id,
            )

        return None

    @staticmethod
    def _content_from_ledger(operation: Operation) -> str:
        """从 Ledger 终态取恢复内容：result_json 优先；缺失时按状态降级合成。"""
        if operation.result_json:
            return operation.result_json
        if operation.state is OperationState.SUCCEEDED:
            return ToolResult.success(
                f"操作 '{operation.tool_name}' 已确认成功（Ledger 记录），"
                "但结果详情缺失。"
            ).model_dump_json()
        return ToolResult.failure(
            message=(
                f"操作 '{operation.tool_name}' 终态为 {operation.state.value}"
                "（Ledger 记录），结果详情缺失。"
            ),
            error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            retryable=False,
        ).model_dump_json()
