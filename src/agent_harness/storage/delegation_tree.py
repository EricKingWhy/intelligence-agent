"""Durable, process-safe accounting for a single multi-agent delegation tree.

`#318` 起同一账本同时承载 **SessionBudget**（`02 §5.1` 第三层）的 durable 真相：
`session_budgets` 表 + append-only `session_budget_events` 审计。预算 key = 树根
会话 id（根 = 自身 session_id；子 = `delegation_root_session_id`），fork 的新会话
天然得到新身份（`03 §7`）。为什么住在这里而不是新模块：`10 §5.1` 把"树内已消耗
多少"的唯一 owner 钉在 #287 的树账本上、明文禁止第二棵树账本——委派计数（树表
`used_delegations`）与 session 计数（本表）同库同事务域，才是同一份事实。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import aiosqlite

from agent_harness.agent.guards import GuardLevel, GuardSignal
from agent_harness.agent.run_budget import (
    INT64_MAX,
    BudgetConflict,
    BudgetRejection,
    SessionAdmission,
    SessionBudgetSnapshot,
    SessionConsumed,
    SessionLimits,
    _deadline_or_none,
    _deadline_text,
    _decimal_or_none,
    _decimal_text,
    session_pause_trigger,
    session_resume_headroom_ok,
    utc_now,
)
from agent_harness.storage.sqlite import retry_on_busy

_SESSION_BUDGET_PROCESS_ID = str(uuid4())


def _session_budget_process_id() -> str:
    """Differentiate workers forked after this module was imported."""
    return f"{os.getpid()}:{_SESSION_BUDGET_PROCESS_ID}"


@dataclass(frozen=True)
class DelegationReservation:
    accepted: bool
    used: int
    limit: int


@dataclass(frozen=True)
class DelegationTreeState:
    tree_id: str
    root_session_id: str
    max_delegations: int
    used_delegations: int
    max_depth: int
    fingerprint: str | None
    consecutive_failures: int
    soft_triggered: bool


@dataclass(frozen=True)
class SessionModelRequestAccounting:
    accounting_id: str
    session_id: str
    run_id: str
    step_id: int
    after_seq: int
    before_seq: int | None
    reserved_requests: int
    owner_id: str


class SessionBudgetRecoveryRequired(RuntimeError):
    """A previous process left a SessionBudget request marker unreconciled."""


class DelegationTreeLedger(Protocol):
    async def reserve(
        self, tree_id: str, *, root_session_id: str,
        max_delegations: int, max_depth: int,
    ) -> DelegationReservation: ...

    async def observe_result(
        self, tree_id: str, fingerprint: str, *, ok: bool,
        soft_threshold: int = 3, hard_threshold: int = 3,
    ) -> GuardSignal: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegation_trees (
    tree_id TEXT PRIMARY KEY,
    root_session_id TEXT NOT NULL,
    max_delegations INTEGER NOT NULL CHECK (max_delegations >= 0),
    used_delegations INTEGER NOT NULL DEFAULT 0 CHECK (used_delegations >= 0),
    max_depth INTEGER NOT NULL CHECK (max_depth >= 0),
    fingerprint TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    soft_triggered INTEGER NOT NULL DEFAULT 0 CHECK (soft_triggered IN (0, 1))
);
CREATE TABLE IF NOT EXISTS delegation_tree_events (
    event_id TEXT PRIMARY KEY,
    tree_id TEXT NOT NULL REFERENCES delegation_trees(tree_id),
    kind TEXT NOT NULL,
    fingerprint TEXT,
    used_delegations INTEGER NOT NULL,
    max_delegations INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_delegation_tree_events_tree
    ON delegation_tree_events(tree_id, created_at);
CREATE TRIGGER IF NOT EXISTS delegation_tree_events_no_update
BEFORE UPDATE ON delegation_tree_events
BEGIN
    SELECT RAISE(ABORT, 'delegation tree audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS delegation_tree_events_no_delete
BEFORE DELETE ON delegation_tree_events
BEGIN
    SELECT RAISE(ABORT, 'delegation tree audit events are append-only');
END;
CREATE TABLE IF NOT EXISTS session_budgets (
    budget_key TEXT PRIMARY KEY,
    root_session_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    max_agent_turns_total INTEGER,
    max_model_requests INTEGER,
    max_total_tokens INTEGER,
    max_cost_usd TEXT,
    deadline_at TEXT,
    max_delegations INTEGER,
    tool_call_limits TEXT NOT NULL DEFAULT '{}',
    agent_turns INTEGER NOT NULL DEFAULT 0 CHECK (agent_turns >= 0),
    model_requests INTEGER NOT NULL DEFAULT 0 CHECK (model_requests >= 0),
    -- `NULL` = 未知（有请求没自报账目之后的粘性）；初始 0 = "空和"（`11 §6.1`：
    -- 只有"一个请求都没有"的空和才是 0）。
    total_tokens INTEGER DEFAULT 0,
    cost_usd TEXT DEFAULT '0',
    tool_calls_by_tool TEXT NOT NULL DEFAULT '{}',
    tool_attempts_by_tool TEXT NOT NULL DEFAULT '{}',
    delegations INTEGER NOT NULL DEFAULT 0 CHECK (delegations >= 0)
);
CREATE INDEX IF NOT EXISTS idx_session_budgets_root
    ON session_budgets(root_session_id);
CREATE TABLE IF NOT EXISTS session_budget_events (
    event_id TEXT PRIMARY KEY,
    budget_key TEXT NOT NULL REFERENCES session_budgets(budget_key),
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_budget_events_key
    ON session_budget_events(budget_key, created_at);
CREATE TRIGGER IF NOT EXISTS session_budget_events_no_update
BEFORE UPDATE ON session_budget_events
BEGIN
    SELECT RAISE(ABORT, 'session budget audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS session_budget_events_no_delete
BEFORE DELETE ON session_budget_events
BEGIN
    SELECT RAISE(ABORT, 'session budget audit events are append-only');
END;
"""


def _tool_map_json(raw: Any) -> str:
    """per-tool 表 → JSON 文本（键排序：字节稳定，跨执行可比）。"""
    mapping = raw if isinstance(raw, Mapping) else {}
    return json.dumps({name: int(mapping[name]) for name in sorted(mapping)})


def _tool_map_from_json(raw: Any) -> dict[str, int] | None:
    """JSON 文本 → per-tool 表；`None` = 未知（粘性，与 `BudgetConsumed` 同纪律）。"""
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    result: dict[str, int] = {}
    for name, value in decoded.items():
        if isinstance(name, str) and isinstance(value, int) and not isinstance(value, bool):
            result[name] = value
    return result


def _row_session_limits(row: aiosqlite.Row | dict[str, Any]) -> SessionLimits:
    """`session_budgets` 行 → `SessionLimits`（`None` 列 = 无 ceiling，不是 0）。"""
    return SessionLimits(
        max_agent_turns_total=row["max_agent_turns_total"],
        max_model_requests=row["max_model_requests"],
        max_total_tokens=row["max_total_tokens"],
        max_cost_usd=_decimal_or_none(row["max_cost_usd"]),
        deadline_at=_deadline_or_none(row["deadline_at"]),
        tool_call_limits=_tool_map_from_json(row["tool_call_limits"]) or {},
        max_delegations=row["max_delegations"],
    )


def _row_session_consumed(row: aiosqlite.Row | dict[str, Any]) -> SessionConsumed:
    """`session_budgets` 行 → `SessionConsumed`（token/cost `NULL` = 未知）。"""
    return SessionConsumed(
        agent_turns=int(row["agent_turns"]),
        model_requests=int(row["model_requests"]),
        total_tokens=row["total_tokens"],
        cost_usd=_decimal_or_none(row["cost_usd"]),
        tool_calls_by_tool=_tool_map_from_json(row["tool_calls_by_tool"]),
        tool_attempts_by_tool=_tool_map_from_json(row["tool_attempts_by_tool"]),
        delegations=int(row["delegations"]),
    )


def _tighten_ceiling(current: Any, requested: Any) -> Any:
    """ensure 的收窄合并：`None` ceiling 遇到声明值 = 加上限（收紧，允许）。"""
    if requested is None:
        return current
    if current is None:
        return requested
    return min(current, requested)


def _reject_out_of_int64_ceilings(limits: SessionLimits) -> None:
    """存储层兜底（#552 B7）：绑定前拒绝超过 int64 的 ceiling。

    领域层 `session_limits_from_request` 已经先校验过一次（正常调用路径）——这一层是
    防未来新调用方绕过领域层：越界整数的 SQL 绑定会抛未分类的 `OverflowError`（事务
    回滚但错误不可识别）。这里提前抛出**域错误** `BudgetRejection`：越界本就是
    "形状非法"（422）语义，与领域层 `_positive_int64_or_none` 同判；因此它经
    `web/domain_errors.py` 的单一映射落 422，而不是裸 `ValueError` 的未分类 500。
    `None` = 无 ceiling，放行。
    """
    for dimension, value in (
        ("max_agent_turns_total", limits.max_agent_turns_total),
        ("max_model_requests", limits.max_model_requests),
        ("max_total_tokens", limits.max_total_tokens),
        ("max_delegations", limits.max_delegations),
    ):
        if value is not None and value > INT64_MAX:
            raise BudgetRejection(
                f"session ceiling {dimension} 超过 int64 上限 {INT64_MAX}：{value!r}"
            )


def _coerce_token_usage(value: Any) -> int | None:
    """usage 的 `total_tokens` → 可入账整数或 `None`（未知）。

    #552：provider 自报的数值**不可信**——越界（> int64）/ 负数 / 布尔 / 浮点 /
    字符串一律**不** `int()` 强转（`True→1`、`1.5→1`、`"100"→100` 都是伪造账目），
    也不 clamp 到上限（audit 明令 MUST NOT clamp），按 `11 §6.1`「不可得 ≠ 0」转
    **未知**（`None`，粘性）。合法值原样返回。
    """
    if type(value) is not int or value < 0 or value > INT64_MAX:
        return None
    return value


class SqliteDelegationTreeLedger:
    """SQLite ledger with serialized budget reservations and append-only audit rows.

    A tree's configured limits are pinned on first use. A later activation can only
    tighten those limits; recreating a Runtime or Store cannot replenish the budget.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._blocked_model_request_accountings: set[tuple[str, str]] = set()

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.database_path, timeout=10)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA busy_timeout = 10000")
            yield connection
        finally:
            await connection.close()

    @retry_on_busy
    async def initialize(self) -> None:
        async with self._connect() as connection:
            await connection.executescript(_SCHEMA)
            await connection.commit()

    @retry_on_busy
    async def reserve(
        self, tree_id: str, *, root_session_id: str,
        max_delegations: int, max_depth: int,
    ) -> DelegationReservation:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await self._ensure_tree(
                    connection, tree_id, root_session_id,
                    max_delegations=max_delegations, max_depth=max_depth,
                )
                used, limit = int(row["used_delegations"]), int(row["max_delegations"])
                accepted = used < limit
                if accepted:
                    used += 1
                    await connection.execute(
                        "UPDATE delegation_trees SET used_delegations = ? WHERE tree_id = ?",
                        (used, tree_id),
                    )
                await self._append_event(
                    connection, tree_id, "attempt_reserved" if accepted else "attempt_rejected",
                    fingerprint=None, used=used, limit=limit,
                )
                await connection.commit()
                return DelegationReservation(accepted, used, limit)
            except BaseException:
                await connection.rollback()
                raise

    @retry_on_busy
    async def observe_result(
        self, tree_id: str, fingerprint: str, *, ok: bool,
        soft_threshold: int = 3, hard_threshold: int = 3,
    ) -> GuardSignal:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                row = await connection.execute_fetchall(
                    "SELECT * FROM delegation_trees WHERE tree_id = ?", (tree_id,),
                )
                if not row:
                    raise KeyError(f"unknown delegation tree: {tree_id}")
                state = row[0]
                previous = state["fingerprint"]
                failures = int(state["consecutive_failures"])
                soft_triggered = bool(state["soft_triggered"])
                if fingerprint != previous:
                    failures, soft_triggered = 0, False
                level = GuardLevel.NONE
                if not ok:
                    failures += 1
                    if not soft_triggered and failures >= soft_threshold:
                        soft_triggered = True
                        level = GuardLevel.SOFT
                    elif soft_triggered and failures >= soft_threshold + hard_threshold:
                        level = GuardLevel.HARD
                await connection.execute(
                    """UPDATE delegation_trees
                       SET fingerprint = ?, consecutive_failures = ?, soft_triggered = ?
                       WHERE tree_id = ?""",
                    (fingerprint, failures, int(soft_triggered), tree_id),
                )
                used, limit = int(state["used_delegations"]), int(state["max_delegations"])
                await self._append_event(
                    connection, tree_id, "result_ok" if ok else "result_failed",
                    fingerprint=fingerprint, used=used, limit=limit,
                )
                if level != GuardLevel.NONE:
                    await self._append_event(
                        connection, tree_id,
                        "guard_soft" if level == GuardLevel.SOFT else "guard_hard",
                        fingerprint=fingerprint, used=used, limit=limit,
                    )
                await connection.commit()
                return GuardSignal(level, "delegate", fingerprint, failures)
            except BaseException:
                await connection.rollback()
                raise

    async def get_state(self, tree_id: str) -> DelegationTreeState:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM delegation_trees WHERE tree_id = ?", (tree_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"unknown delegation tree: {tree_id}")
        return DelegationTreeState(
            tree_id=row["tree_id"], root_session_id=row["root_session_id"],
            max_delegations=row["max_delegations"],
            used_delegations=row["used_delegations"], max_depth=row["max_depth"],
            fingerprint=row["fingerprint"],
            consecutive_failures=row["consecutive_failures"],
            soft_triggered=bool(row["soft_triggered"]),
        )

    async def event_kinds(self, tree_id: str) -> list[str]:
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT kind FROM delegation_tree_events WHERE tree_id = "
                "? ORDER BY rowid", (tree_id,),
            )
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def pending_model_request_accountings(
        self, budget_key: str, *, session_id: str,
    ) -> list[SessionModelRequestAccounting]:
        async with self._connect() as connection:
            markers = await self._pending_model_request_accountings(
                connection, budget_key,
            )
        return [
            marker for marker in markers
            if marker.session_id == session_id
            and marker.owner_id != _session_budget_process_id()
        ]

    def block_model_request_accounting(
        self, budget_key: str, accounting_id: str,
    ) -> None:
        self._blocked_model_request_accountings.add((budget_key, accounting_id))

    def unblock_model_request_accounting(
        self, budget_key: str, accounting_id: str,
    ) -> None:
        self._blocked_model_request_accountings.discard((budget_key, accounting_id))

    # ── SessionBudget（#318；`02 §5.1` / `10 §5.1`）──────────────────────
    #
    # 所有操作都在单个 BEGIN IMMEDIATE 事务里完成"判定 + 计数"：并发兄弟（同进程
    # 的 asyncio 任务或崩溃恢复后的新进程）竞争最后一格时，SQLite 的写事务串行化
    # 保证至多一个被接纳（`10 §13`）。账面持久化在本表 = 崩溃恢复**读回**同一份
    # consumed / limits / version（`03 §5` Crash durability），不靠进程内存。

    @staticmethod
    async def _append_session_event(
        connection: aiosqlite.Connection, budget_key: str, kind: str,
        *, version: int, detail: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str:
        event_id = event_id or str(uuid4())
        await connection.execute(
            """INSERT INTO session_budget_events
               (event_id, budget_key, kind, version, detail)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, budget_key, kind, version,
             json.dumps(detail, sort_keys=True) if detail else None),
        )
        return event_id

    async def _pending_model_request_accountings(
        self, connection: aiosqlite.Connection, budget_key: str,
    ) -> list[SessionModelRequestAccounting]:
        cursor = await connection.execute(
            "SELECT event_id, kind, detail FROM session_budget_events "
            "WHERE budget_key = ? ORDER BY rowid", (budget_key,),
        )
        rows = await cursor.fetchall()
        markers: dict[str, dict[str, Any]] = {}
        resolved: set[str] = set()
        for row in rows:
            try:
                detail = json.loads(row["detail"] or "{}")
            except (TypeError, ValueError):
                if row["kind"] == "model_request_accounting_started":
                    raise SessionBudgetRecoveryRequired(
                        "SessionBudget has a malformed model request accounting marker"
                    )
                continue
            if not isinstance(detail, dict):
                if row["kind"] == "model_request_accounting_started":
                    raise SessionBudgetRecoveryRequired(
                        "SessionBudget has a malformed model request accounting marker"
                    )
                continue
            if row["kind"] == "model_request_accounting_started":
                markers[row["event_id"]] = detail
            elif row["kind"] in {
                "requests_recorded", "request_accounting_empty",
                "request_accounting_unsettled",
            }:
                accounting_id = detail.get("accounting_id")
                if isinstance(accounting_id, str):
                    resolved.add(accounting_id)
        parsed: list[SessionModelRequestAccounting] = []
        for accounting_id, detail in markers.items():
            session_id = detail.get("session_id")
            run_id = detail.get("run_id")
            step_id = detail.get("step_id")
            after_seq = detail.get("after_seq")
            reserved_requests = detail.get("reserved_requests")
            owner_id = detail.get("owner_id")
            if (
                isinstance(session_id, str) and bool(session_id)
                and isinstance(run_id, str) and bool(run_id)
                and type(step_id) is int and step_id >= 0
                and type(after_seq) is int and after_seq >= 0
                and type(reserved_requests) is int and reserved_requests in (0, 1)
                and isinstance(owner_id, str) and bool(owner_id)
            ):
                parsed.append(SessionModelRequestAccounting(
                    accounting_id=accounting_id, session_id=session_id,
                    run_id=run_id, step_id=step_id, after_seq=after_seq,
                    before_seq=None,
                    reserved_requests=reserved_requests, owner_id=owner_id,
                ))
            else:
                raise SessionBudgetRecoveryRequired(
                    "SessionBudget has an incomplete model request accounting marker"
                )
        pending: list[SessionModelRequestAccounting] = []
        for marker in parsed:
            later = [
                item.after_seq for item in parsed
                if item.session_id == marker.session_id
                and item.run_id == marker.run_id
                and item.step_id == marker.step_id
                and item.after_seq > marker.after_seq
            ]
            bounded = SessionModelRequestAccounting(
                accounting_id=marker.accounting_id,
                session_id=marker.session_id,
                run_id=marker.run_id,
                step_id=marker.step_id,
                after_seq=marker.after_seq,
                before_seq=min(later) if later else None,
                reserved_requests=marker.reserved_requests,
                owner_id=marker.owner_id,
            )
            if marker.accounting_id not in resolved:
                pending.append(bounded)
        return pending

    async def _session_snapshot(
        self, connection: aiosqlite.Connection, budget_key: str,
    ) -> SessionBudgetSnapshot:
        cursor = await connection.execute(
            "SELECT * FROM session_budgets WHERE budget_key = ?", (budget_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"unknown session budget: {budget_key}")
        return SessionBudgetSnapshot(
            limits=_row_session_limits(row),
            consumed=_row_session_consumed(row),
            version=int(row["version"]),
        )

    @retry_on_busy
    async def ensure_session_budget(
        self, budget_key: str, *, root_session_id: str, limits: SessionLimits,
    ) -> SessionBudgetSnapshot:
        """首用钉死 + 之后只收窄（与 `_ensure_tree` 同一条纪律，`10 §5.1`）。

        请求没点名的席不落 ceiling（`NULL` = 无上限，不是 0）；已有时取更小者。
        不动 `version`——CAS 版本只属于恢复路径的显式更新
        （`update_session_limits`），装配路径的静默收窄不制造"版本过期"假象。
        """
        _reject_out_of_int64_ceilings(limits)
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """INSERT OR IGNORE INTO session_budgets
                       (budget_key, root_session_id, max_agent_turns_total,
                        max_model_requests, max_total_tokens, max_cost_usd,
                        deadline_at, max_delegations, tool_call_limits)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (budget_key, root_session_id, limits.max_agent_turns_total,
                     limits.max_model_requests, limits.max_total_tokens,
                     _decimal_text(limits.max_cost_usd),
                     _deadline_text(limits.deadline_at),
                     limits.max_delegations, _tool_map_json(limits.tool_call_limits)),
                )
                cursor = await connection.execute(
                    "SELECT * FROM session_budgets WHERE budget_key = ?", (budget_key,),
                )
                row = await cursor.fetchone()
                if row["root_session_id"] != root_session_id:
                    raise ValueError("session budget root identity mismatch")
                current_tools = _tool_map_from_json(row["tool_call_limits"]) or {}
                requested_tools = dict(limits.tool_call_limits)
                merged_tools = dict(current_tools)
                for name, ceiling in requested_tools.items():
                    merged_tools[name] = (
                        ceiling if name not in current_tools
                        else min(int(current_tools[name]), int(ceiling))
                    )
                current_deadline = _deadline_or_none(row["deadline_at"])
                requested_deadline = limits.deadline_at
                if current_deadline is not None and requested_deadline is not None:
                    tightened_deadline = min(current_deadline, requested_deadline)
                else:
                    tightened_deadline = current_deadline or requested_deadline
                current_cost = _decimal_or_none(row["max_cost_usd"])
                requested_cost = limits.max_cost_usd
                if current_cost is not None and requested_cost is not None:
                    tightened_cost = min(current_cost, requested_cost)
                else:
                    tightened_cost = current_cost if requested_cost is None else requested_cost
                effective = {
                    "max_agent_turns_total": _tighten_ceiling(
                        row["max_agent_turns_total"], limits.max_agent_turns_total),
                    "max_model_requests": _tighten_ceiling(
                        row["max_model_requests"], limits.max_model_requests),
                    "max_total_tokens": _tighten_ceiling(
                        row["max_total_tokens"], limits.max_total_tokens),
                    "max_cost_usd": _decimal_text(tightened_cost),
                    "deadline_at": _deadline_text(tightened_deadline),
                    "max_delegations": _tighten_ceiling(
                        row["max_delegations"], limits.max_delegations),
                    "tool_call_limits": _tool_map_json(merged_tools),
                }
                changed = any(
                    effective[column] != row[column] for column in effective
                )
                if changed:
                    await connection.execute(
                        """UPDATE session_budgets
                           SET max_agent_turns_total = ?, max_model_requests = ?,
                               max_total_tokens = ?, max_cost_usd = ?, deadline_at = ?,
                               max_delegations = ?, tool_call_limits = ?
                           WHERE budget_key = ?""",
                        (effective["max_agent_turns_total"],
                         effective["max_model_requests"],
                         effective["max_total_tokens"], effective["max_cost_usd"],
                         effective["deadline_at"], effective["max_delegations"],
                         effective["tool_call_limits"], budget_key),
                    )
                    await self._append_session_event(
                        connection, budget_key, "limits_tightened",
                        version=int(row["version"]),
                        detail={column: effective[column] for column in sorted(effective)},
                    )
                # 快照读必须在 commit 之前（同 admit_session_step 的 #515 P2-B 注）。
                # 本方法重跑本就幂等收敛（INSERT OR IGNORE + changed=False 不重复
                # 落事件，无 CAS 可撞），无行为级缺陷；移入事务内只为消除毒窗、
                # 与全文件其余写路径统一形状（#544 ②，结构统一）。
                updated_snapshot = await self._session_snapshot(connection, budget_key)
                await connection.commit()
                return updated_snapshot
            except BaseException:
                await connection.rollback()
                raise

    @retry_on_busy
    async def admit_session_step(
        self, budget_key: str, *, session_id: str | None = None,
        run_id: str | None = None, step_id: int | None = None,
        after_seq: int | None = None,
    ) -> SessionAdmission:
        """原子准入：判到顶就拒绝（账不变），否则**预留** turns / requests 各一格。

        预留语义（保持 `02 §5.1` 的计数定义）：这一格 turns 在决策被**接纳进 loop**
        后成为真账；决策被拒 / 传输失败 / 取消时由 `refund_session_turn` 退回。
        requests 的预留不退——本步至少会实际发出一次请求并落 `model/request`。
        """
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                pending = await self._pending_model_request_accountings(
                    connection, budget_key,
                )
                blocked = [
                    marker for marker in pending
                    if marker.owner_id != _session_budget_process_id()
                    or (budget_key, marker.accounting_id)
                    in self._blocked_model_request_accountings
                ]
                if blocked:
                    raise SessionBudgetRecoveryRequired(
                        "SessionBudget has an unreconciled model request settlement"
                    )
                accounting_values = (session_id, run_id, step_id, after_seq)
                if any(value is not None for value in accounting_values) and not all(
                    value is not None for value in accounting_values
                ):
                    raise ValueError("model request accounting context must be complete")
                snapshot = await self._session_snapshot(connection, budget_key)
                trigger = session_pause_trigger(
                    consumed=snapshot.consumed, session_limits=snapshot.limits,
                    now=utc_now(),
                )
                if trigger is not None:
                    await connection.commit()
                    return SessionAdmission(False, trigger, snapshot)
                accounting_id = None
                await connection.execute(
                    """UPDATE session_budgets
                       SET agent_turns = agent_turns + 1,
                           model_requests = model_requests + 1
                       WHERE budget_key = ?""",
                    (budget_key,),
                )
                await self._append_session_event(
                    connection, budget_key, "step_admitted",
                    version=snapshot.version,
                    detail={"turns": snapshot.consumed.agent_turns + 1},
                )
                if session_id is not None:
                    accounting_id = str(uuid4())
                    await self._append_session_event(
                        connection, budget_key, "model_request_accounting_started",
                        version=snapshot.version,
                        detail={
                            "session_id": session_id,
                            "run_id": run_id,
                            "step_id": step_id,
                            "after_seq": after_seq,
                            "reserved_requests": 1,
                            "owner_id": _session_budget_process_id(),
                        },
                        event_id=accounting_id,
                    )
                # 快照读必须在 commit 之前（事务内读自己的写）：commit 之后的读若
                # 撞锁超时，retry_on_busy 会整块重跑，而已提交的 +1 预留无法回滚，
                # 重跑即二次预留（#515 审查 P2-B 双计数）。「非幂等增量写 + commit
                # 后同型读」组合已收族（#544 同修 ensure_session_budget /
                # update_session_limits / sqlite.update_state 三处）；剩余 commit
                # 后读走新连接且绝对 SET 幂等（upsert/set_archived/
                # update_last_checkpoint_seq），重跑收敛无害。
                admitted_snapshot = await self._session_snapshot(connection, budget_key)
                await connection.commit()
                return SessionAdmission(
                    True, None, admitted_snapshot, accounting_id=accounting_id,
                )
            except BaseException:
                await connection.rollback()
                raise

    @retry_on_busy
    async def refund_session_turn(self, budget_key: str) -> None:
        """退回预留的 turns 一格（决策未被接纳；下限 0，不为负）。"""
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT version FROM session_budgets WHERE budget_key = ?",
                    (budget_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown session budget: {budget_key}")
                await connection.execute(
                    """UPDATE session_budgets
                       SET agent_turns = MAX(agent_turns - 1, 0) WHERE budget_key = ?""",
                    (budget_key,),
                )
                await self._append_session_event(
                    connection, budget_key, "turn_refunded", version=int(row["version"]),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    @retry_on_busy
    async def begin_session_model_request_accounting(
        self, budget_key: str, *, session_id: str, run_id: str,
        step_id: int, after_seq: int,
    ) -> str:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                pending = await self._pending_model_request_accountings(
                    connection, budget_key,
                )
                if any(
                    marker.owner_id != _session_budget_process_id()
                    or (budget_key, marker.accounting_id)
                    in self._blocked_model_request_accountings
                    for marker in pending
                ):
                    raise SessionBudgetRecoveryRequired(
                        "SessionBudget has an unreconciled model request settlement"
                    )
                cursor = await connection.execute(
                    "SELECT version FROM session_budgets WHERE budget_key = ?",
                    (budget_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown session budget: {budget_key}")
                accounting_id = str(uuid4())
                await self._append_session_event(
                    connection, budget_key, "model_request_accounting_started",
                    version=int(row["version"]),
                    detail={
                        "session_id": session_id,
                        "run_id": run_id,
                        "step_id": step_id,
                        "after_seq": after_seq,
                        "reserved_requests": 0,
                        "owner_id": _session_budget_process_id(),
                    },
                    event_id=accounting_id,
                )
                await connection.commit()
                return accounting_id
            except BaseException:
                await connection.rollback()
                raise

    @retry_on_busy
    async def resolve_empty_session_model_request_accounting(
        self, budget_key: str, accounting_id: str, *, refund_step: bool,
    ) -> None:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT version FROM session_budgets WHERE budget_key = ?",
                    (budget_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown session budget: {budget_key}")
                markers = await self._pending_model_request_accountings(
                    connection, budget_key,
                )
                marker = next(
                    (item for item in markers if item.accounting_id == accounting_id),
                    None,
                )
                if marker is None:
                    await connection.commit()
                    self.unblock_model_request_accounting(budget_key, accounting_id)
                    return
                if refund_step and marker.reserved_requests != 1:
                    raise ValueError("only an admitted model step can refund its reservation")
                if refund_step:
                    await connection.execute(
                        """UPDATE session_budgets
                           SET agent_turns = MAX(agent_turns - 1, 0),
                               model_requests = MAX(model_requests - 1, 0)
                           WHERE budget_key = ?""",
                        (budget_key,),
                    )
                    await self._append_session_event(
                        connection, budget_key, "step_refunded",
                        version=int(row["version"]),
                    )
                await self._append_session_event(
                    connection, budget_key, "request_accounting_empty",
                    version=int(row["version"]),
                    detail={"accounting_id": accounting_id},
                )
                await connection.commit()
                self.unblock_model_request_accounting(budget_key, accounting_id)
            except BaseException:
                await connection.rollback()
                self.block_model_request_accounting(budget_key, accounting_id)
                raise

    @retry_on_busy
    async def resolve_unsettled_session_model_request_accounting(
        self, budget_key: str, accounting_id: str,
    ) -> None:
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT version FROM session_budgets WHERE budget_key = ?",
                    (budget_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown session budget: {budget_key}")
                markers = await self._pending_model_request_accountings(
                    connection, budget_key,
                )
                if not any(item.accounting_id == accounting_id for item in markers):
                    await connection.commit()
                    self.unblock_model_request_accounting(budget_key, accounting_id)
                    return
                await self._append_session_event(
                    connection, budget_key, "request_accounting_unsettled",
                    version=int(row["version"]),
                    detail={"accounting_id": accounting_id},
                )
                await connection.commit()
                self.unblock_model_request_accounting(budget_key, accounting_id)
            except BaseException:
                await connection.rollback()
                self.block_model_request_accounting(budget_key, accounting_id)
                raise

    @retry_on_busy
    async def refund_session_step(
        self, budget_key: str, *, accounting_id: str | None = None,
    ) -> None:
        """退回预留的两格 turns / requests（整步未发生：模型在本轮从未被调用）。"""
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT version FROM session_budgets WHERE budget_key = ?",
                    (budget_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown session budget: {budget_key}")
                await connection.execute(
                    """UPDATE session_budgets
                       SET agent_turns = MAX(agent_turns - 1, 0),
                           model_requests = MAX(model_requests - 1, 0)
                       WHERE budget_key = ?""",
                    (budget_key,),
                )
                await self._append_session_event(
                    connection, budget_key, "step_refunded", version=int(row["version"]),
                )
                if accounting_id is not None:
                    await self._append_session_event(
                        connection, budget_key, "request_accounting_empty",
                        version=int(row["version"]),
                        detail={"accounting_id": accounting_id},
                    )
                await connection.commit()
                if accounting_id is not None:
                    self.unblock_model_request_accounting(budget_key, accounting_id)
            except BaseException:
                await connection.rollback()
                if accounting_id is not None:
                    self.block_model_request_accounting(budget_key, accounting_id)
                raise

    @retry_on_busy
    async def record_session_model_requests(
        self, budget_key: str, *, count: int, usage: dict[str, int] | None,
        cost: Decimal | None, accounting_id: str | None = None,
        request_ids: tuple[str, ...] = (),
    ) -> None:
        """把实际发生的请求落账（`model_requests` 的树级计数点）。

        `count` = 本批 `model/request` 事件数（primary / fallback / closeout 逐条
        对应）。usage / cost 只随**产出响应**的那一次给（缺席实现不伪造）；任一维
        从已知转未知用 `NULL` 粘住——与 run 作用域 `BudgetConsumed.with_usage` 的
        `None` 粘性同一纪律（`11 §6.1`：不可得 ≠ 0）。
        """
        if count < 0:
            raise ValueError(f"request count 不能为负：{count}")
        if accounting_id is not None and (
            not request_ids or any(not isinstance(value, str) or not value for value in request_ids)
            or len(set(request_ids)) != len(request_ids)
        ):
            raise ValueError("idempotent request accounting requires unique request ids")
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT * FROM session_budgets WHERE budget_key = ?", (budget_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown session budget: {budget_key}")
                if accounting_id is not None:
                    marker_cursor = await connection.execute(
                        "SELECT kind FROM session_budget_events "
                        "WHERE event_id = ? AND budget_key = ?",
                        (accounting_id, budget_key),
                    )
                    marker = await marker_cursor.fetchone()
                    if marker is None or marker["kind"] != "model_request_accounting_started":
                        raise KeyError(f"unknown model request accounting marker: {accounting_id}")
                    events_cursor = await connection.execute(
                        "SELECT kind, detail FROM session_budget_events "
                        "WHERE budget_key = ? ORDER BY rowid", (budget_key,),
                    )
                    audit_rows = await events_cursor.fetchall()
                    requested_ids = set(request_ids)
                    for audit_row in audit_rows:
                        try:
                            detail = json.loads(audit_row["detail"] or "{}")
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(detail, dict):
                            continue
                        if detail.get("accounting_id") == accounting_id:
                            if audit_row["kind"] != "requests_recorded":
                                raise ValueError("empty model request accounting cannot be settled")
                            same = (
                                detail.get("request_ids") == list(request_ids)
                                and detail.get("count") == count
                                and detail.get("usage") == usage
                                and detail.get("cost_delta_usd") == _decimal_text(cost)
                            )
                            if not same:
                                raise ValueError("model request accounting replay differs from commit")
                            await connection.commit()
                            self.unblock_model_request_accounting(budget_key, accounting_id)
                            return
                        if audit_row["kind"] == "requests_recorded":
                            previously_recorded = detail.get("request_ids")
                            if (
                                isinstance(previously_recorded, list)
                                and requested_ids.intersection(previously_recorded)
                            ):
                                raise ValueError("model request id was already accounted")
                tokens = row["total_tokens"]
                if tokens is not None:
                    if usage is None or "total_tokens" not in usage:
                        tokens = None
                    else:
                        delta = _coerce_token_usage(usage["total_tokens"])
                        if delta is None:
                            # 越界 / 负数 / bool / 浮点 / 字符串 ⇒ 该维转未知（不 clamp、不记 0）
                            tokens = None
                        else:
                            total = int(tokens) + delta
                            # 合法值相加溢出 int64：存储无法表达 ⇒ 转未知（不 clamp）
                            tokens = total if total <= INT64_MAX else None
                current_cost = _decimal_or_none(row["cost_usd"])
                if current_cost is None or cost is None:
                    new_cost = None
                else:
                    new_cost = current_cost + cost
                await connection.execute(
                    """UPDATE session_budgets
                       SET model_requests = model_requests + ?, total_tokens = ?,
                           cost_usd = ?
                       WHERE budget_key = ?""",
                    (count, tokens, _decimal_text(new_cost), budget_key),
                )
                await self._append_session_event(
                    connection, budget_key, "requests_recorded",
                    version=int(row["version"]),
                    detail={"count": count, "tokens": tokens,
                            "cost_usd": _decimal_text(new_cost),
                            **({
                                "accounting_id": accounting_id,
                                "request_ids": list(request_ids),
                                "usage": usage,
                                "cost_delta_usd": _decimal_text(cost),
                            } if accounting_id is not None else {})},
                )
                await connection.commit()
                if accounting_id is not None:
                    self.unblock_model_request_accounting(budget_key, accounting_id)
            except BaseException:
                await connection.rollback()
                if accounting_id is not None:
                    self.block_model_request_accounting(budget_key, accounting_id)
                raise

    @retry_on_busy
    async def record_session_tools(
        self, budget_key: str, *, calls: Mapping[str, int], attempts: Mapping[str, int],
    ) -> None:
        """把 `tool/result.budget_delta` 的增量并入两张 per-tool 表。"""
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT * FROM session_budgets WHERE budget_key = ?", (budget_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown session budget: {budget_key}")
                calls_table = _tool_map_from_json(row["tool_calls_by_tool"]) or {}
                attempts_table = _tool_map_from_json(row["tool_attempts_by_tool"]) or {}
                for name, value in calls.items():
                    if value > 0:
                        calls_table[name] = calls_table.get(name, 0) + value
                for name, value in attempts.items():
                    if value > 0:
                        attempts_table[name] = attempts_table.get(name, 0) + value
                await connection.execute(
                    """UPDATE session_budgets
                       SET tool_calls_by_tool = ?, tool_attempts_by_tool = ?
                       WHERE budget_key = ?""",
                    (_tool_map_json(calls_table), _tool_map_json(attempts_table),
                     budget_key),
                )
                await self._append_session_event(
                    connection, budget_key, "tools_recorded", version=int(row["version"]),
                    detail={"calls": _tool_map_json(dict(calls)),
                            "attempts": _tool_map_json(dict(attempts))},
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    @retry_on_busy
    async def update_session_limits(
        self, budget_key: str, *, expected_version: int, limits: SessionLimits,
    ) -> SessionBudgetSnapshot:
        """恢复路径的 CAS 更新（`03 §3.4`：绝对 ceiling；`11 §6.1`：409 判据）。

        点名的席取请求值（**绝对**语义，可以抬高），未点名的沿用当前值；
        `version` 不符 ⇒ 409（`BudgetConflict`）；任何点名 ceiling 低于已消耗
        （或消耗未知）⇒ 409——存储层兜底的不变量：**绝不**持久化一个低于已消耗
        的 ceiling。**"放得下一次新准入"（headroom）也在本方法内判**（见事务内
        `session_resume_headroom_ok`）：409 必须零副作用，拆到调用方就会先改账
        再拒绝（`03 §3.4` / `11 §6.1`）。
        """
        _reject_out_of_int64_ceilings(limits)
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT * FROM session_budgets WHERE budget_key = ?", (budget_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown session budget: {budget_key}")
                if expected_version != int(row["version"]):
                    raise BudgetConflict(
                        f"session budget version 过期：expected_version={expected_version}，"
                        f"当前 version={row['version']}（CAS 比较失败，未启动任何工作）"
                    )
                effective_tools = _tool_map_from_json(row["tool_call_limits"]) or {}
                for name, ceiling in limits.tool_call_limits.items():
                    effective_tools[name] = ceiling
                effective = {
                    "max_agent_turns_total": (
                        limits.max_agent_turns_total
                        if limits.max_agent_turns_total is not None
                        else row["max_agent_turns_total"]
                    ),
                    "max_model_requests": (
                        limits.max_model_requests
                        if limits.max_model_requests is not None
                        else row["max_model_requests"]
                    ),
                    "max_total_tokens": (
                        limits.max_total_tokens
                        if limits.max_total_tokens is not None
                        else row["max_total_tokens"]
                    ),
                    "max_cost_usd": _decimal_text(
                        limits.max_cost_usd
                        if limits.max_cost_usd is not None
                        else _decimal_or_none(row["max_cost_usd"])
                    ),
                    "deadline_at": _deadline_text(
                        limits.deadline_at
                        if limits.deadline_at is not None
                        else _deadline_or_none(row["deadline_at"])
                    ),
                    "max_delegations": (
                        limits.max_delegations
                        if limits.max_delegations is not None
                        else row["max_delegations"]
                    ),
                    "tool_call_limits": _tool_map_json(effective_tools),
                }
                if effective["max_agent_turns_total"] is not None \
                        and effective["max_agent_turns_total"] < int(row["agent_turns"]):
                    raise BudgetConflict(
                        f"max_agent_turns_total={effective['max_agent_turns_total']} "
                        f"低于已消耗 {row['agent_turns']}（11 §6.1：ceiling 低于已消耗 "
                        f"⇒ 409，未启动任何工作）"
                    )
                if effective["max_total_tokens"] is not None:
                    if row["total_tokens"] is None:
                        raise BudgetConflict(
                            "token 消耗基数未知（有请求未自报 usage）而恢复点名了 "
                            "max_total_tokens：无法证明到线即停 ⇒ 409，未启动任何工作"
                        )
                    if effective["max_total_tokens"] < int(row["total_tokens"]):
                        raise BudgetConflict(
                            f"max_total_tokens={effective['max_total_tokens']} 低于已消耗 "
                            f"{row['total_tokens']}（11 §6.1 ⇒ 409，未启动任何工作）"
                        )
                if effective["max_cost_usd"] is not None:
                    consumed_cost = _decimal_or_none(row["cost_usd"])
                    if consumed_cost is None:
                        raise BudgetConflict(
                            "cost 消耗基数未知而恢复点名了 max_cost_usd ⇒ 409，"
                            "未启动任何工作"
                        )
                    if Decimal(effective["max_cost_usd"]) < consumed_cost:
                        raise BudgetConflict(
                            f"max_cost_usd={effective['max_cost_usd']} 低于已消耗 "
                            f"{consumed_cost}（11 §6.1 ⇒ 409，未启动任何工作）"
                        )
                for name, ceiling in effective_tools.items():
                    calls_table = _tool_map_from_json(row["tool_calls_by_tool"]) or {}
                    if ceiling < calls_table.get(name, 0):
                        raise BudgetConflict(
                            f"tool_call_limits[{name}]={ceiling} 低于已消耗 "
                            f"{calls_table.get(name, 0)}（11 §6.1 ⇒ 409，未启动任何工作）"
                        )
                if effective["max_delegations"] is not None \
                        and effective["max_delegations"] < int(row["delegations"]):
                    raise BudgetConflict(
                        f"max_delegations={effective['max_delegations']} 低于已消耗 "
                        f"{row['delegations']}（11 §6.1 ⇒ 409，未启动任何工作）"
                    )
                # "放得下一次新准入"的收紧判定（headroom）与 CAS / 低于已消耗同在
                # **这一个事务**里：409 必须零副作用（11 §6.1），拆到调用方就会先改账
                # 再拒绝。账未知 + 点了 ceiling ⇒ 判"放不下"（同一事务内已按未知拒绝，
                # 这里拿到的 effective 若带未知消耗同样过不了 headroom）。
                effective_limits = SessionLimits(
                    max_agent_turns_total=effective["max_agent_turns_total"],
                    max_model_requests=effective["max_model_requests"],
                    max_total_tokens=effective["max_total_tokens"],
                    max_cost_usd=_decimal_or_none(effective["max_cost_usd"]),
                    deadline_at=_deadline_or_none(effective["deadline_at"]),
                    tool_call_limits=_tool_map_from_json(effective["tool_call_limits"]) or {},
                    max_delegations=effective["max_delegations"],
                )
                effective_consumed = SessionConsumed(
                    agent_turns=int(row["agent_turns"]),
                    model_requests=int(row["model_requests"]),
                    total_tokens=row["total_tokens"],
                    cost_usd=_decimal_or_none(row["cost_usd"]),
                    tool_calls_by_tool=_tool_map_from_json(row["tool_calls_by_tool"]),
                    tool_attempts_by_tool=_tool_map_from_json(row["tool_attempts_by_tool"]),
                    delegations=int(row["delegations"]),
                )
                if not session_resume_headroom_ok(
                    consumed=effective_consumed, limits=effective_limits, now=utc_now(),
                ):
                    raise BudgetConflict(
                        "恢复后的生效 ceiling 放不下一次新准入（session 作用域；"
                        "consumed=" f"{effective_consumed.as_projection()}，limits="
                        f"{effective_limits.as_projection()}）——仍到线的维度必须一起抬高"
                        "（11 §6.1 ⇒ 409，未启动任何工作）"
                    )
                new_version = int(row["version"]) + 1
                await connection.execute(
                    """UPDATE session_budgets
                       SET version = ?, max_agent_turns_total = ?, max_model_requests = ?,
                           max_total_tokens = ?, max_cost_usd = ?, deadline_at = ?,
                           max_delegations = ?, tool_call_limits = ?
                       WHERE budget_key = ?""",
                    (new_version, effective["max_agent_turns_total"],
                     effective["max_model_requests"], effective["max_total_tokens"],
                     effective["max_cost_usd"], effective["deadline_at"],
                     effective["max_delegations"], effective["tool_call_limits"],
                     budget_key),
                )
                await self._append_session_event(
                    connection, budget_key, "limits_updated", version=new_version,
                    detail={column: effective[column] for column in sorted(effective)},
                )
                # 快照读必须在 commit 之前（同 admit_session_step 的 #515 P2-B 注）：
                # commit 后读撞锁会触发整方法重跑，此处重跑撞自身 CAS 抛伪 409。
                updated_snapshot = await self._session_snapshot(connection, budget_key)
                await connection.commit()
                return updated_snapshot
            except BaseException:
                await connection.rollback()
                raise

    async def get_session_budget(self, budget_key: str) -> SessionBudgetSnapshot | None:
        """只读读数（投影用）；行不存在（会话还没消费过 / 没配置过）⇒ `None`。"""
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT * FROM session_budgets WHERE budget_key = ?", (budget_key,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return SessionBudgetSnapshot(
                limits=_row_session_limits(row),
                consumed=_row_session_consumed(row),
                version=int(row["version"]),
            )

    @retry_on_busy
    async def consume_session_delegation(
        self, budget_key: str, *, root_session_id: str, max_delegations: int,
    ) -> DelegationReservation:
        """session 作用域的委派接纳（跨 run 聚合的那一格；树 reserve 之前调用）。

        与 #287 树 reserve 的分工：树表计数的是**单棵 delegating run 的树**
        （深度 / 指纹 / guard 的机制域），本表计数的是**整棵会话树跨 run 累计**
        的 `delegations`（`02 §5.1`：SessionBudget 的委派账）。行不存在时用本次
        声明的上限建行（首用钉死，之后收窄-only）——`10 §5.1`：SessionBudget
        默认 `max_delegations=8`，与请求是否显式配置无关。
        """
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """INSERT OR IGNORE INTO session_budgets
                       (budget_key, root_session_id, max_delegations)
                       VALUES (?, ?, ?)""",
                    (budget_key, root_session_id, max_delegations),
                )
                cursor = await connection.execute(
                    "SELECT * FROM session_budgets WHERE budget_key = ?", (budget_key,),
                )
                row = await cursor.fetchone()
                if row["root_session_id"] != root_session_id:
                    raise ValueError("session budget root identity mismatch")
                limit = (
                    min(int(row["max_delegations"]), max_delegations)
                    if row["max_delegations"] is not None else max_delegations
                )
                if limit != row["max_delegations"]:
                    await connection.execute(
                        "UPDATE session_budgets SET max_delegations = ? WHERE budget_key = ?",
                        (limit, budget_key),
                    )
                used = int(row["delegations"])
                accepted = used < limit
                if accepted:
                    used += 1
                    await connection.execute(
                        "UPDATE session_budgets SET delegations = ? WHERE budget_key = ?",
                        (used, budget_key),
                    )
                await self._append_session_event(
                    connection, budget_key,
                    "delegation_consumed" if accepted else "delegation_rejected",
                    version=int(row["version"]), detail={"used": used, "limit": limit},
                )
                await connection.commit()
                return DelegationReservation(accepted, used, limit)
            except BaseException:
                await connection.rollback()
                raise

    @retry_on_busy
    async def refund_session_delegation(self, budget_key: str) -> None:
        """退回 session 委派预留（树 reserve 被拒时的配对退回；下限 0）。"""
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT version FROM session_budgets WHERE budget_key = ?",
                    (budget_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown session budget: {budget_key}")
                await connection.execute(
                    """UPDATE session_budgets
                       SET delegations = MAX(delegations - 1, 0) WHERE budget_key = ?""",
                    (budget_key,),
                )
                await self._append_session_event(
                    connection, budget_key, "delegation_refunded",
                    version=int(row["version"]),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def session_event_kinds(self, budget_key: str) -> list[str]:
        """审计事件序（测试与对账用；append-only 触发器在 schema 上）。"""
        async with self._connect() as connection:
            cursor = await connection.execute(
                "SELECT kind FROM session_budget_events WHERE budget_key = ? "
                "ORDER BY rowid", (budget_key,),
            )
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def _ensure_tree(
        self, connection: aiosqlite.Connection, tree_id: str, root_session_id: str,
        *, max_delegations: int, max_depth: int,
    ):
        await connection.execute(
            """INSERT OR IGNORE INTO delegation_trees
               (tree_id, root_session_id, max_delegations, max_depth)
               VALUES (?, ?, ?, ?)""",
            (tree_id, root_session_id, max_delegations, max_depth),
        )
        cursor = await connection.execute(
            "SELECT * FROM delegation_trees WHERE tree_id = ?", (tree_id,),
        )
        row = await cursor.fetchone()
        if row["root_session_id"] != root_session_id:
            raise ValueError("delegation tree root identity mismatch")
        # Configuration drift during resume is fail-closed: an existing tree may
        # tighten its ceiling, never raise it or change its root identity.
        limit = min(int(row["max_delegations"]), max_delegations)
        depth = min(int(row["max_depth"]), max_depth)
        if limit != row["max_delegations"] or depth != row["max_depth"]:
            await connection.execute(
                "UPDATE delegation_trees SET max_delegations = ?, max_depth = ? "
                "WHERE tree_id = ?", (limit, depth, tree_id),
            )
            row = dict(row)
            row["max_delegations"] = limit
            row["max_depth"] = depth
        return row

    @staticmethod
    async def _append_event(
        connection: aiosqlite.Connection, tree_id: str, kind: str,
        *, fingerprint: str | None, used: int, limit: int,
    ) -> None:
        await connection.execute(
            """INSERT INTO delegation_tree_events
               (event_id, tree_id, kind, fingerprint, used_delegations, max_delegations)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(uuid4()), tree_id, kind, fingerprint, used, limit),
        )


class InMemoryDelegationTreeLedger:
    """Process-local fallback for isolated Runtime constructions without app stores."""

    def __init__(self) -> None:
        self._trees: dict[str, dict[str, object]] = {}
        self._events: dict[str, list[str]] = {}
        self._budgets: dict[str, dict[str, object]] = {}
        self._lock = asyncio.Lock()
        self._model_request_accountings: dict[str, dict[str, dict[str, Any]]] = {}
        self._resolved_model_request_accountings: set[tuple[str, str]] = set()
        self._recorded_model_request_ids: dict[str, set[str]] = {}
        self._settled_model_request_accountings: dict[
            tuple[str, str], dict[str, Any]
        ] = {}

    async def initialize(self) -> None:
        return None

    async def reserve(
        self, tree_id: str, *, root_session_id: str,
        max_delegations: int, max_depth: int,
    ) -> DelegationReservation:
        async with self._lock:
            state = self._ensure(tree_id, root_session_id, max_delegations, max_depth)
            state["max_delegations"] = min(int(state["max_delegations"]), max_delegations)
            state["max_depth"] = min(int(state["max_depth"]), max_depth)
            used, limit = int(state["used_delegations"]), int(state["max_delegations"])
            accepted = used < limit
            if accepted:
                used += 1
                state["used_delegations"] = used
            self._events[tree_id].append("attempt_reserved" if accepted else "attempt_rejected")
            return DelegationReservation(accepted, used, limit)

    async def observe_result(
        self, tree_id: str, fingerprint: str, *, ok: bool,
        soft_threshold: int = 3, hard_threshold: int = 3,
    ) -> GuardSignal:
        async with self._lock:
            state = self._trees[tree_id]
            if fingerprint != state["fingerprint"]:
                state["fingerprint"] = fingerprint
                state["consecutive_failures"] = 0
                state["soft_triggered"] = False
            failures = int(state["consecutive_failures"])
            soft = bool(state["soft_triggered"])
            level = GuardLevel.NONE
            if not ok:
                failures += 1
                if not soft and failures >= soft_threshold:
                    soft, level = True, GuardLevel.SOFT
                elif soft and failures >= soft_threshold + hard_threshold:
                    level = GuardLevel.HARD
            state["consecutive_failures"] = failures
            state["soft_triggered"] = soft
            self._events[tree_id].append("result_ok" if ok else "result_failed")
            if level != GuardLevel.NONE:
                self._events[tree_id].append(
                    "guard_soft" if level == GuardLevel.SOFT else "guard_hard",
                )
            return GuardSignal(level, "delegate", fingerprint, failures)

    async def get_state(self, tree_id: str) -> DelegationTreeState:
        async with self._lock:
            state = self._trees[tree_id]
            return DelegationTreeState(
                tree_id=tree_id,
                root_session_id=str(state["root_session_id"]),
                max_delegations=int(state["max_delegations"]),
                used_delegations=int(state["used_delegations"]),
                max_depth=int(state["max_depth"]),
                fingerprint=state["fingerprint"],
                consecutive_failures=int(state["consecutive_failures"]),
                soft_triggered=bool(state["soft_triggered"]),
            )

    async def event_kinds(self, tree_id: str) -> list[str]:
        async with self._lock:
            return list(self._events[tree_id])

    # ── SessionBudget（#318）：进程内孪生（与 Sqlite 版共享判定与序列化助手，
    # 判定都在 `run_budget.session_pause_trigger` 一处；状态形状与表行一致——
    # cost/deadline 存文本、per-tool 表存 JSON 文本——序列化路径只有一份）。 ──

    def _budget(self, budget_key: str, root_session_id: str) -> dict[str, object]:
        state = self._budgets.get(budget_key)
        if state is None:
            state = {
                "root_session_id": root_session_id, "version": 1,
                "max_agent_turns_total": None, "max_model_requests": None,
                "max_total_tokens": None, "max_cost_usd": None,
                "deadline_at": None, "max_delegations": None,
                "tool_call_limits": "{}",
                "agent_turns": 0, "model_requests": 0,
                "total_tokens": 0, "cost_usd": "0",
                "tool_calls_by_tool": "{}", "tool_attempts_by_tool": "{}",
                "delegations": 0,
            }
            self._budgets[budget_key] = state
        elif state["root_session_id"] != root_session_id:
            raise ValueError("session budget root identity mismatch")
        return state

    def _budget_snapshot(self, budget_key: str) -> SessionBudgetSnapshot:
        state = self._budgets[budget_key]
        return SessionBudgetSnapshot(
            limits=_row_session_limits(state),
            consumed=_row_session_consumed(state),
            version=int(state["version"]),
        )

    async def ensure_session_budget(
        self, budget_key: str, *, root_session_id: str, limits: SessionLimits,
    ) -> SessionBudgetSnapshot:
        async with self._lock:
            _reject_out_of_int64_ceilings(limits)
            state = self._budget(budget_key, root_session_id)
            state["max_agent_turns_total"] = _tighten_ceiling(
                state["max_agent_turns_total"], limits.max_agent_turns_total)
            state["max_model_requests"] = _tighten_ceiling(
                state["max_model_requests"], limits.max_model_requests)
            state["max_total_tokens"] = _tighten_ceiling(
                state["max_total_tokens"], limits.max_total_tokens)
            current_cost = _decimal_or_none(state["max_cost_usd"])
            if current_cost is not None and limits.max_cost_usd is not None:
                state["max_cost_usd"] = _decimal_text(min(current_cost, limits.max_cost_usd))
            elif limits.max_cost_usd is not None:
                state["max_cost_usd"] = _decimal_text(limits.max_cost_usd)
            current_deadline = _deadline_or_none(state["deadline_at"])
            if current_deadline is not None and limits.deadline_at is not None:
                state["deadline_at"] = _deadline_text(min(current_deadline, limits.deadline_at))
            elif limits.deadline_at is not None:
                state["deadline_at"] = _deadline_text(limits.deadline_at)
            state["max_delegations"] = _tighten_ceiling(
                state["max_delegations"], limits.max_delegations)
            current_tools = _tool_map_from_json(state["tool_call_limits"]) or {}
            for name, ceiling in limits.tool_call_limits.items():
                current_tools[name] = (
                    ceiling if name not in current_tools
                    else min(int(current_tools[name]), int(ceiling))
                )
            state["tool_call_limits"] = _tool_map_json(current_tools)
            return self._budget_snapshot(budget_key)

    async def admit_session_step(
        self, budget_key: str, *, session_id: str | None = None,
        run_id: str | None = None, step_id: int | None = None,
        after_seq: int | None = None,
    ) -> SessionAdmission:
        async with self._lock:
            snapshot = self._budget_snapshot(budget_key)
            trigger = session_pause_trigger(
                consumed=snapshot.consumed, session_limits=snapshot.limits,
                now=utc_now(),
            )
            if trigger is not None:
                return SessionAdmission(False, trigger, snapshot)
            state = self._budgets[budget_key]
            state["agent_turns"] = int(state["agent_turns"]) + 1
            state["model_requests"] = int(state["model_requests"]) + 1
            accounting_id = None
            if session_id is not None and run_id is not None \
                    and step_id is not None and after_seq is not None:
                accounting_id = str(uuid4())
                self._model_request_accountings.setdefault(budget_key, {})[
                    accounting_id
                ] = {
                    "session_id": session_id, "run_id": run_id,
                    "step_id": step_id, "after_seq": after_seq,
                    "reserved_requests": 1,
                }
            return SessionAdmission(
                True, None, self._budget_snapshot(budget_key),
                accounting_id=accounting_id,
            )

    async def refund_session_turn(self, budget_key: str) -> None:
        async with self._lock:
            state = self._budgets[budget_key]
            state["agent_turns"] = max(int(state["agent_turns"]) - 1, 0)

    async def refund_session_step(
        self, budget_key: str, *, accounting_id: str | None = None,
    ) -> None:
        async with self._lock:
            state = self._budgets[budget_key]
            state["agent_turns"] = max(int(state["agent_turns"]) - 1, 0)
            state["model_requests"] = max(int(state["model_requests"]) - 1, 0)
            if accounting_id is not None:
                self._resolved_model_request_accountings.add((budget_key, accounting_id))

    async def begin_session_model_request_accounting(
        self, budget_key: str, *, session_id: str, run_id: str,
        step_id: int, after_seq: int,
    ) -> str:
        async with self._lock:
            accounting_id = str(uuid4())
            self._model_request_accountings.setdefault(budget_key, {})[accounting_id] = {
                "session_id": session_id, "run_id": run_id,
                "step_id": step_id, "after_seq": after_seq,
                "reserved_requests": 0,
            }
            return accounting_id

    async def resolve_empty_session_model_request_accounting(
        self, budget_key: str, accounting_id: str, *, refund_step: bool,
    ) -> None:
        async with self._lock:
            if refund_step:
                state = self._budgets[budget_key]
                state["agent_turns"] = max(int(state["agent_turns"]) - 1, 0)
                state["model_requests"] = max(int(state["model_requests"]) - 1, 0)
            self._resolved_model_request_accountings.add((budget_key, accounting_id))

    async def resolve_unsettled_session_model_request_accounting(
        self, budget_key: str, accounting_id: str,
    ) -> None:
        async with self._lock:
            self._resolved_model_request_accountings.add((budget_key, accounting_id))

    async def record_session_model_requests(
        self, budget_key: str, *, count: int, usage: dict[str, int] | None,
        cost: Decimal | None, accounting_id: str | None = None,
        request_ids: tuple[str, ...] = (),
    ) -> None:
        if count < 0:
            raise ValueError(f"request count 不能为负：{count}")
        if accounting_id is not None and (
            not request_ids
            or any(not isinstance(value, str) or not value for value in request_ids)
            or len(set(request_ids)) != len(request_ids)
        ):
            raise ValueError("idempotent request accounting requires unique request ids")
        async with self._lock:
            if accounting_id is not None:
                accounting_key = (budget_key, accounting_id)
                payload = {
                    "request_ids": list(request_ids),
                    "count": count,
                    "usage": usage,
                    "cost_delta_usd": _decimal_text(cost),
                }
                if accounting_key in self._resolved_model_request_accountings:
                    recorded_payload = self._settled_model_request_accountings.get(
                        accounting_key
                    )
                    if recorded_payload is None:
                        raise ValueError("empty model request accounting cannot be settled")
                    if recorded_payload != payload:
                        raise ValueError("model request accounting replay differs from commit")
                    return
                if accounting_id not in self._model_request_accountings.get(budget_key, {}):
                    raise KeyError(f"unknown model request accounting marker: {accounting_id}")
                recorded = self._recorded_model_request_ids.setdefault(budget_key, set())
                if recorded.intersection(request_ids):
                    raise ValueError("model request id was already accounted")
            state = self._budgets[budget_key]
            state["model_requests"] = int(state["model_requests"]) + count
            tokens = state["total_tokens"]
            if tokens is not None:
                if usage is None or "total_tokens" not in usage:
                    tokens = None
                else:
                    delta = _coerce_token_usage(usage["total_tokens"])
                    if delta is None:
                        # 越界 / 负数 / bool / 浮点 / 字符串 ⇒ 转未知（不 clamp、不记 0）
                        tokens = None
                    else:
                        total = int(tokens) + delta
                        tokens = total if total <= INT64_MAX else None
            state["total_tokens"] = tokens
            current_cost = _decimal_or_none(state["cost_usd"])
            if current_cost is None:
                pass
            elif cost is None:
                state["cost_usd"] = None
            else:
                state["cost_usd"] = _decimal_text(current_cost + cost)
            if accounting_id is not None:
                self._recorded_model_request_ids[budget_key].update(request_ids)
                accounting_key = (budget_key, accounting_id)
                self._settled_model_request_accountings[accounting_key] = payload
                self._resolved_model_request_accountings.add(accounting_key)

    async def record_session_tools(
        self, budget_key: str, *, calls: Mapping[str, int], attempts: Mapping[str, int],
    ) -> None:
        async with self._lock:
            state = self._budgets[budget_key]
            calls_table = _tool_map_from_json(state["tool_calls_by_tool"]) or {}
            attempts_table = _tool_map_from_json(state["tool_attempts_by_tool"]) or {}
            for name, value in calls.items():
                if value > 0:
                    calls_table[name] = calls_table.get(name, 0) + value
            for name, value in attempts.items():
                if value > 0:
                    attempts_table[name] = attempts_table.get(name, 0) + value
            state["tool_calls_by_tool"] = _tool_map_json(calls_table)
            state["tool_attempts_by_tool"] = _tool_map_json(attempts_table)

    async def update_session_limits(
        self, budget_key: str, *, expected_version: int, limits: SessionLimits,
    ) -> SessionBudgetSnapshot:
        async with self._lock:
            _reject_out_of_int64_ceilings(limits)
            state = self._budgets[budget_key]
            if expected_version != int(state["version"]):
                raise BudgetConflict(
                    f"session budget version 过期：expected_version={expected_version}，"
                    f"当前 version={state['version']}（CAS 比较失败，未启动任何工作）"
                )
            effective_tools = _tool_map_from_json(state["tool_call_limits"]) or {}
            for name, ceiling in limits.tool_call_limits.items():
                effective_tools[name] = ceiling
            effective: dict[str, Any] = {
                "max_agent_turns_total": (
                    limits.max_agent_turns_total
                    if limits.max_agent_turns_total is not None
                    else state["max_agent_turns_total"]
                ),
                "max_model_requests": (
                    limits.max_model_requests
                    if limits.max_model_requests is not None
                    else state["max_model_requests"]
                ),
                "max_total_tokens": (
                    limits.max_total_tokens
                    if limits.max_total_tokens is not None
                    else state["max_total_tokens"]
                ),
                "max_cost_usd": _decimal_text(
                    limits.max_cost_usd
                    if limits.max_cost_usd is not None
                    else _decimal_or_none(state["max_cost_usd"])
                ),
                "deadline_at": _deadline_text(
                    limits.deadline_at
                    if limits.deadline_at is not None
                    else _deadline_or_none(state["deadline_at"])
                ),
                "max_delegations": (
                    limits.max_delegations
                    if limits.max_delegations is not None
                    else state["max_delegations"]
                ),
                "tool_call_limits": _tool_map_json(effective_tools),
            }
            if effective["max_agent_turns_total"] is not None \
                    and effective["max_agent_turns_total"] < int(state["agent_turns"]):
                raise BudgetConflict("ceiling 低于已消耗（agent_turns）⇒ 409")
            if effective["max_total_tokens"] is not None:
                if state["total_tokens"] is None:
                    raise BudgetConflict("token 基数未知 ⇒ 409")
                if effective["max_total_tokens"] < int(state["total_tokens"]):
                    raise BudgetConflict("ceiling 低于已消耗（total_tokens）⇒ 409")
            if effective["max_cost_usd"] is not None:
                consumed_cost = _decimal_or_none(state["cost_usd"])
                if consumed_cost is None:
                    raise BudgetConflict("cost 基数未知 ⇒ 409")
                if Decimal(effective["max_cost_usd"]) < consumed_cost:
                    raise BudgetConflict("ceiling 低于已消耗（cost_usd）⇒ 409")
            calls_table = _tool_map_from_json(state["tool_calls_by_tool"]) or {}
            for name, ceiling in effective_tools.items():
                if ceiling < calls_table.get(name, 0):
                    raise BudgetConflict(f"tool_call_limits[{name}] 低于已消耗 ⇒ 409")
            if effective["max_delegations"] is not None \
                    and effective["max_delegations"] < int(state["delegations"]):
                raise BudgetConflict("ceiling 低于已消耗（delegations）⇒ 409")
            # headroom（"放得下一次新准入"）与 CAS / 低于已消耗同在**这一个锁**里：
            # 409 零副作用（11 §6.1），state 尚未被 update。账未知 + 点了 ceiling ⇒
            # session_resume_headroom_ok 判"放不下"（与 Sqlite 版同一函数，口径一处）。
            effective_limits = SessionLimits(
                max_agent_turns_total=effective["max_agent_turns_total"],
                max_model_requests=effective["max_model_requests"],
                max_total_tokens=effective["max_total_tokens"],
                max_cost_usd=_decimal_or_none(effective["max_cost_usd"]),
                deadline_at=_deadline_or_none(effective["deadline_at"]),
                tool_call_limits=_tool_map_from_json(effective["tool_call_limits"]) or {},
                max_delegations=effective["max_delegations"],
            )
            effective_consumed = SessionConsumed(
                agent_turns=int(state["agent_turns"]),
                model_requests=int(state["model_requests"]),
                total_tokens=state["total_tokens"],
                cost_usd=_decimal_or_none(state["cost_usd"]),
                tool_calls_by_tool=_tool_map_from_json(state["tool_calls_by_tool"]),
                tool_attempts_by_tool=_tool_map_from_json(state["tool_attempts_by_tool"]),
                delegations=int(state["delegations"]),
            )
            if not session_resume_headroom_ok(
                consumed=effective_consumed, limits=effective_limits, now=utc_now(),
            ):
                raise BudgetConflict(
                    "恢复后的生效 ceiling 放不下一次新准入（session 作用域）⇒ 409"
                )
            state.update(effective)
            state["version"] = int(state["version"]) + 1
            return self._budget_snapshot(budget_key)

    async def get_session_budget(self, budget_key: str) -> SessionBudgetSnapshot | None:
        async with self._lock:
            if budget_key not in self._budgets:
                return None
            return self._budget_snapshot(budget_key)

    async def consume_session_delegation(
        self, budget_key: str, *, root_session_id: str, max_delegations: int,
    ) -> DelegationReservation:
        async with self._lock:
            state = self._budget(budget_key, root_session_id)
            limit = (
                min(int(state["max_delegations"]), max_delegations)
                if state["max_delegations"] is not None else max_delegations
            )
            state["max_delegations"] = limit
            used = int(state["delegations"])
            accepted = used < limit
            if accepted:
                used += 1
                state["delegations"] = used
            return DelegationReservation(accepted, used, limit)

    async def refund_session_delegation(self, budget_key: str) -> None:
        async with self._lock:
            state = self._budgets[budget_key]
            state["delegations"] = max(int(state["delegations"]) - 1, 0)

    async def session_event_kinds(self, budget_key: str) -> list[str]:
        return []

    def _ensure(
        self, tree_id: str, root_session_id: str,
        max_delegations: int, max_depth: int,
    ) -> dict[str, object]:
        if tree_id not in self._trees:
            self._trees[tree_id] = {
                "root_session_id": root_session_id,
                "max_delegations": max_delegations,
                "used_delegations": 0,
                "max_depth": max_depth,
                "fingerprint": None,
                "consecutive_failures": 0,
                "soft_triggered": False,
            }
            self._events[tree_id] = []
        elif self._trees[tree_id]["root_session_id"] != root_session_id:
            raise ValueError("delegation tree root identity mismatch")
        return self._trees[tree_id]


@dataclass(frozen=True)
class SessionBudgetHandle:
    """`SessionBudgetPort` 的实现（`agent/run_budget.py` 的 Protocol；装配层注入）。

    一个 handle 绑定一个预算 key + 一份**请求侧**声明。`admit_step` 前先 ensure：
    行不存在（会话首跑）用声明建行，已存在走收窄-only——重启后 runtime 拿同一份
    声明（来自 `session/started`）重新 ensure，账与 ceiling 都从持久层读回
    （`03 §5` Crash durability：不靠进程内存）。`None` limits = 纯默认形状
    （全部无 ceiling；`max_delegations` 的钉死由 `consume_session_delegation`
    在首用委派时按 profile 声明完成）。
    """

    ledger: SqliteDelegationTreeLedger | InMemoryDelegationTreeLedger
    budget_key: str
    root_session_id: str
    limits: SessionLimits = field(default_factory=SessionLimits)

    async def snapshot(self) -> SessionBudgetSnapshot:
        snapshot = await self.ledger.get_session_budget(self.budget_key)
        if snapshot is None:
            return await self.ledger.ensure_session_budget(
                self.budget_key, root_session_id=self.root_session_id,
                limits=self.limits,
            )
        return snapshot

    async def admit_step(
        self, *, session_id: str | None = None, run_id: str | None = None,
        step_id: int | None = None, after_seq: int | None = None,
    ) -> SessionAdmission:
        await self.ledger.ensure_session_budget(
            self.budget_key, root_session_id=self.root_session_id, limits=self.limits,
        )
        return await self.ledger.admit_session_step(
            self.budget_key, session_id=session_id, run_id=run_id,
            step_id=step_id, after_seq=after_seq,
        )

    async def refund_turn(self) -> None:
        await self.ledger.refund_session_turn(self.budget_key)

    async def refund_step(self, *, accounting_id: str | None = None) -> None:
        await self.ledger.refund_session_step(
            self.budget_key, accounting_id=accounting_id,
        )

    async def begin_model_request_accounting(
        self, *, session_id: str, run_id: str, step_id: int, after_seq: int,
    ) -> str:
        return await self.ledger.begin_session_model_request_accounting(
            self.budget_key, session_id=session_id, run_id=run_id,
            step_id=step_id, after_seq=after_seq,
        )

    async def resolve_empty_model_request_accounting(
        self, accounting_id: str, *, refund_step: bool,
    ) -> None:
        await self.ledger.resolve_empty_session_model_request_accounting(
            self.budget_key, accounting_id, refund_step=refund_step,
        )

    async def resolve_unsettled_model_request_accounting(
        self, accounting_id: str,
    ) -> None:
        await self.ledger.resolve_unsettled_session_model_request_accounting(
            self.budget_key, accounting_id,
        )

    async def record_model_requests(
        self, *, count: int, usage: dict[str, int] | None, cost: Decimal | None,
        accounting_id: str | None = None, request_ids: tuple[str, ...] = (),
    ) -> None:
        await self.ledger.record_session_model_requests(
            self.budget_key, count=count, usage=usage, cost=cost,
            accounting_id=accounting_id, request_ids=request_ids,
        )

    async def record_tools(
        self, *, calls: Mapping[str, int], attempts: Mapping[str, int],
    ) -> None:
        await self.ledger.record_session_tools(
            self.budget_key, calls=calls, attempts=attempts,
        )
