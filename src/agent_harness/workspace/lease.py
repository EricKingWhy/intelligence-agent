"""W-10（#354）单目录写入租约与持久 FIFO 队列——语义层。

域契约：PRD §3「写入租约 = 持久任务目录所有权与有序队列」、§4.1（创建写入
Task 先取得持久租约；只读 Task 不占；升级须先取得）、票面精确行为（持有者
暂停/run 终态/断线/待审批/待审阅不放锁；仅用户接受/归档/显式释放可释放；
释放原子选出队首、队首有在场客户端才开始）。

**不在本层的责任**（票面"不做"）：SessionBudget 锁（#318）、自动 worktree、
分布式锁、ToolExecutor 的按调用资源冲突（`tooling/resource_locks.py` 是另一域）。
Run 状态在这里**完全不可见**——租约不随 run 暂停/完成/断线变化，只有
`release`/会话失效（重启对账）会改它。

**presence 只读 seam（票面核查增强）**：队首是否"开始"取决于该 Task 是否有
在场客户端；在场登记协议属 W-12，而 W-12 又消费租约状态——拆环：本模块只
依赖 `TaskPresenceReader` 只读合同（某 Task 是否有在场客户端），W-12 落地后
提供真实现。缺省 `NoPresenceReader` 恒 False：无在场信息 ⇒ 队首**不**自动
开始（fail-safe，不偷跑模型 token）。用户回来后的转授走 `grant_awaited`
（W-12 在场事件的接入点）或队首 Task 自己重跑 `acquire`。

**写串行化**（ADR-0025 D4b 同型）：acquire/release/cancel/grant 都是
"读 → 判定 → 写库"的复合操作，进程内并发由一把 `asyncio.Lock` 串行；跨进程
由 `InstanceLock`（单实例）保证；SQLite 事务 + PK/部分唯一索引是最后一道
CAS（两个同时创建至多一个 INSERT 成功）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from agent_harness.workspace.lease_paths import (
    normalize_dir_key,
    paths_conflict,
)
from agent_harness.workspace.lease_store import (
    LEASE_AWAITING_PRESENCE,
    LEASE_HELD,
    QUEUE_CANCELLED,
    QUEUE_WAITING,
    LeaseRow,
    QueueRow,
    SqliteLeaseStore,
)

logger = logging.getLogger("agent_harness.workspace.lease")


class TaskPresenceReader(Protocol):
    """W-12 在场信息的**只读**合同（服务端 presence 合同，票面核查增强）。

    只有一个问题：这个 Task（session）当前有没有在场客户端（桌面窗口/托盘、
    TUI、本机 Web 按 Task 登记的托管关系）。登记/心跳/宽限都是 W-12 的写侧；
    租约层只读，绝不反向登记，避免 W-10 ↔ W-12 依赖环。
    """

    def has_present_client(self, session_id: str) -> bool: ...


class NoPresenceReader:
    """W-12 落地前的缺省实现：无人登记 ⇒ 恒无在场。

    fail-safe 方向：无在场信息时队首**不**自动开始（PRD §3："队首有在场
    客户端才启动；否则保持等待用户回来，不自行消耗模型 token"）。
    """

    def has_present_client(self, session_id: str) -> bool:
        _ = session_id  # 合同即全部信息；缺省实现无人可判在场
        return False


class LeaseError(Exception):
    """租约层的域错误基类。"""


class LeasePathConflict(LeaseError):
    """目标目录与既有租约**相交但不相同**（父/子目录等）。

    不能并发写也不能简单排队（排队语义只对"同一目录键"成立）——拒绝并要求
    用户另选独立目录（票面工作指令："不能安全证明两个路径独立"）。
    """


class LeaseAlreadyHeldElsewhere(LeaseError):
    """同一 Task 已持有另一个目录的租约（一个 Task 一个工作目录）。"""


class LeaseQueuedElsewhere(LeaseError):
    """同一 Task 已在另一个目录的队列里排队（等待对象同样只有一个）。

    想换目录：先 `cancel_queued` 撤销旧排队项，再对新目录 acquire——显式
    动作换目标，不允许一个 Task 同时挂在两条队列上。
    """


@dataclass(frozen=True)
class AcquireOutcome:
    """acquire 的结果：`granted`（拿到/已持有/被转授）或 `queued`（持久 FIFO）。

    `position` 从 1 起（1 = 队首 = 下一个将接管的等待者）；`holder` 是当前
    持有者（排队时可见，持有重入时即自己）。
    """

    granted: bool
    dir_key: str
    holder: str | None
    position: int | None

    @property
    def queued(self) -> bool:
        return not self.granted


@dataclass(frozen=True)
class ReleaseOutcome:
    """release 的结果：`released=False` = 调用方本来就不是持有者（幂等）；
    `promoted_to` = 同事务原子转授的队首 Task（None = 无队列或队首无在场）。
    """

    released: bool
    promoted_to: str | None


@dataclass(frozen=True)
class LeaseStatus:
    """一个 Task 的租约视角快照（held / queued 互斥，可同时为无）。"""

    held_dir_key: str | None
    held_dir_path: str | None
    queued_dir_key: str | None
    queue_position: int | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorkspaceLeaseManager:
    """单目录写入租约 + 持久 FIFO 队列（无内存缓存；真相在 harness.db）。

    释放语义（票面）：持有者 run/completed、暂停、断线、待审批、待证据审阅
    都**不**经过本层——本层不观察 Run 状态，租约保持；只有 `release`
    （用户接受/归档/显式释放）、排队取消、重启对账（会话失效）会改变租约。
    释放后原 Task 再次写入须重新 `acquire`（排到队尾，无任何豁免）。
    """

    def __init__(
        self,
        store: SqliteLeaseStore,
        presence: TaskPresenceReader | None = None,
    ) -> None:
        self._store = store
        self._presence = presence if presence is not None else NoPresenceReader()
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._store.initialize()

    # —— 查询（读方法不加锁：单行读写由 SQLite 事务保证无半态，ADR-0025 D4b） ——

    async def status(self, session_id: str) -> LeaseStatus:
        held = await self._held_by(session_id)
        queued = await self._waiting_of(session_id)
        position = (
            await self._waiting_position(queued.dir_key, queued.session_id)
            if queued is not None
            else None
        )
        return LeaseStatus(
            held_dir_key=held.dir_key if held else None,
            held_dir_path=held.dir_path if held else None,
            queued_dir_key=queued.dir_key if queued is not None else None,
            queue_position=position,
        )

    async def leases(self) -> list[LeaseRow]:
        return await self._store.load_leases()

    async def queue(self) -> list[QueueRow]:
        return await self._store.load_queue()

    # —— 命令 ——

    async def acquire(self, session_id: str, path: str) -> AcquireOutcome:
        """Task `session_id` 试图取得 `path`（规范化后）的排他写入租约。

        - 目录空闲且无相交租约 → `granted`（纯 INSERT，PK 即 CAS）；
        - 已是持有者（同键幂等重入）→ `granted`；
        - 已持有**其他**目录 → `LeaseAlreadyHeldElsewhere`；
        - 相交但不同键（父/子目录）→ `LeasePathConflict`，要求另选目录；
        - 同键被他人持有 / awaiting（队首未在场）→ 持久 FIFO 排队 →
          `queued`（结果即"未获准写"信号：调用方不得启动模型写任务）；
        - 路径不能安全规范化 → `LeasePathError`（fail-closed）。
        """
        dir_key = normalize_dir_key(path)
        async with self._write_lock:
            held = await self._held_by(session_id)
            if held is not None:
                if held.dir_key == dir_key:
                    return AcquireOutcome(True, dir_key, session_id, None)
                raise LeaseAlreadyHeldElsewhere(
                    f"session {session_id} 已持有目录租约 {held.dir_path!r}，"
                    f"不能同时申请 {path!r}"
                )
            # awaiting 租约：队首出现在场客户端则先原子转授（acquire 重入即
            # "用户回来后队首重跑"路径），随后本次请求按普通规则处理。
            promoted = await self._grant_awaited_locked(dir_key)
            # 相交不相等（父/子目录）：不能并发写也不能排队（排队语义只对
            # 同一键成立）→ 拒绝并要求用户另选独立目录（fail-closed）。
            for lease in await self._store.load_leases():
                if lease.dir_key != dir_key and paths_conflict(lease.dir_key, dir_key):
                    raise LeasePathConflict(
                        f"目录 {path!r} 与既有租约目录 {lease.dir_path!r} 相交"
                        f"（不能安全证明并发写独立），请另选独立目录"
                    )
            holder = await self._holder_of(dir_key)
            if holder is None:
                await self._store.insert_lease(dir_key, path, session_id, _now())
                return AcquireOutcome(True, dir_key, session_id, None)
            if holder.session_id == session_id:
                return AcquireOutcome(True, dir_key, session_id, None)
            waiting = await self._waiting_of(session_id)
            if waiting is not None and waiting.dir_key != dir_key:
                raise LeaseQueuedElsewhere(
                    f"session {session_id} 已在目录 {waiting.dir_key!r} 的队列中"
                    f"排队；换目录请先 cancel_queued 再重新 acquire"
                )
            if waiting is None:
                await self._store.insert_waiting(dir_key, session_id, _now())
            position = await self._waiting_position(dir_key, session_id)
            return AcquireOutcome(
                False, dir_key, holder.session_id or promoted, position
            )

    async def release(self, session_id: str) -> ReleaseOutcome:
        """显式释放（用户接受/归档/显式释放入口调用；重复调用幂等 no-op）。

        释放与"原子选出队首"在同一个事务里（`lease_store.release_lease`）：
        队首有在场客户端（presence 只读合同）→ 同事务转授；否则租约转
        `awaiting_presence`，队首保持 waiting 等用户回来。
        """
        async with self._write_lock:
            held = await self._held_by(session_id)
            if held is None:
                return ReleaseOutcome(False, None)
            head = await self._first_waiting(held.dir_key)
            promote = (
                head.session_id
                if head is not None
                and self._presence.has_present_client(head.session_id)
                else None
            )
            result = await self._store.release_lease(session_id, promote, _now())
            if result == "promoted":
                logger.info(
                    "workspace lease %s released by %s, promoted to %s",
                    held.dir_key, session_id, promote,
                )
            elif result == "awaiting":
                logger.info(
                    "workspace lease %s released by %s, queue head waits for "
                    "client presence", held.dir_key, session_id,
                )
            return ReleaseOutcome(True, promote if result == "promoted" else None)

    async def grant_awaited(self, dir_key: str) -> str | None:
        """`awaiting_presence` 租约的显式转授口：队首在场 → 转授并返回其 id。

        W-12 在场事件落地后由它触发"用户回来 ⇒ 队首开始"；无队列/队首无在场
        → None（保持等待）。
        """
        async with self._write_lock:
            return await self._grant_awaited_locked(normalize_dir_key(dir_key))

    async def cancel_queued(self, session_id: str) -> bool:
        """撤销本 Task 的排队项（不影响当前持有者；无排队项为幂等 no-op）。

        队列因此变空且租约处于 `awaiting_presence`（没有任何可等对象）→
        摘除租约行，目录回到完全空闲。
        """
        async with self._write_lock:
            waiting = await self._waiting_of(session_id)
            if waiting is None:
                return False
            await self._store.set_queue_state(waiting.queue_id, QUEUE_CANCELLED)
            lease = await self._holder_of(waiting.dir_key)
            if (
                lease is not None
                and lease.state == LEASE_AWAITING_PRESENCE
                and await self._first_waiting(waiting.dir_key) is None
            ):
                await self._store.drop_awaiting(waiting.dir_key)
            return True

    async def reconcile_on_restart(self, valid_session_ids: set[str]) -> int:
        """实例重启扫描：租约/队列与 Session 状态对账。

        - 已不存在会话的持有行/waiting 行删除（会话已删，租约无人继承）；
        - 有效会话的持有租约**原样保留**（PRD §3：租约不因重启释放）；
        - `awaiting_presence` ⇔ 队列非空的不变量被破坏 → 大声失败（事务保证
          它不该发生，出现即损坏，按 AC13 哲学不静默修补）。

        返回删除的行数（对账证据）。
        """
        async with self._write_lock:
            dropped = 0
            for lease in await self._store.load_leases():
                if (
                    lease.session_id is not None
                    and lease.session_id not in valid_session_ids
                ):
                    dropped += await self._store.drop_session_rows(lease.session_id)
            for row in await self._store.load_queue():
                if (
                    row.state == QUEUE_WAITING
                    and row.session_id not in valid_session_ids
                ):
                    dropped += await self._store.drop_session_rows(row.session_id)
            for lease in await self._store.load_leases():
                if lease.state == LEASE_AWAITING_PRESENCE:
                    head = await self._first_waiting(lease.dir_key)
                    if head is None:
                        raise LeaseError(
                            "租约损坏：awaiting_presence 但队列无 waiting 行"
                            f"（dir_key={lease.dir_key!r}），请人工检查 "
                            "workspace_leases / workspace_lease_queue"
                        )
            return dropped

    # —— 内部（调用方持锁） ——

    async def _grant_awaited_locked(self, dir_key: str) -> str | None:
        lease = await self._holder_of(dir_key)
        if lease is None or lease.state != LEASE_AWAITING_PRESENCE:
            return None
        head = await self._first_waiting(dir_key)
        if head is None:
            raise LeaseError(f"租约损坏：awaiting_presence 无队首（{dir_key!r}）")
        if not self._presence.has_present_client(head.session_id):
            return None
        await self._store.grant_awaiting(dir_key, head.session_id, _now())
        logger.info(
            "workspace lease %s granted to awaited head %s", dir_key, head.session_id
        )
        return head.session_id

    async def _held_by(self, session_id: str) -> LeaseRow | None:
        for lease in await self._store.load_leases():
            if lease.session_id == session_id and lease.state == LEASE_HELD:
                return lease
        return None

    async def _holder_of(self, dir_key: str) -> LeaseRow | None:
        for lease in await self._store.load_leases():
            if lease.dir_key == dir_key:
                return lease
        return None

    async def _waiting_of(self, session_id: str) -> QueueRow | None:
        for row in await self._store.load_queue():
            if row.session_id == session_id and row.state == QUEUE_WAITING:
                return row
        return None

    async def _first_waiting(self, dir_key: str) -> QueueRow | None:
        for row in await self._store.load_queue():
            if row.dir_key == dir_key and row.state == QUEUE_WAITING:
                return row
        return None

    async def _waiting_position(self, dir_key: str, session_id: str) -> int:
        position = 0
        for row in await self._store.load_queue():
            if row.dir_key != dir_key or row.state != QUEUE_WAITING:
                continue
            position += 1
            if row.session_id == session_id:
                return position
        raise LeaseError(f"waiting 行丢失：{session_id!r} @ {dir_key!r}")
