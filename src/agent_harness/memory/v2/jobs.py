"""#298 / MEM-V2-2 的 durable formation job：幂等入队、单属主认领、阶段持久化。

# 为什么是同一库里的新表（PRD §7.2 第 3 条）

约束是"复用既有 SQLite/Event/Outbox 基底，允许在内加记忆专属表，**不允许**独立队列
服务或只存在于内存的属主"。所以 job 表与 `memory_v2_records` / `memory_v2_outbox`
落在同一个 `memory-v2.db`：崩溃之后"这个 run 的记忆作业谁该继续跑"由磁盘回答，
而不是由某个进程的字典回答——后者重启即失忆，AC6 的四个 kill 窗口一个都过不了。

# 幂等（R1）

`idempotency_key` 上的 UNIQUE 是唯一去重机制：同一个 key 反复入队（终结路径重入、
重启后的恢复扫描重入）恒得到同一行、同一个 `job_id`；已经终结的 job 也不会被"重开"。
入队方因此**不需要**先查后写——先查后写在并发下会漏（两个终结者都查到"不存在"）。

# 单属主与串行（R11 + AC6）

认领是一次 `BEGIN IMMEDIATE` 内的 CAS：只有"无属主或 lease 已到期"的行才会被写上属主。
所以"谁在跑"有唯一答案，而且答案是数据库给的结论，不是进程内纪律。
lease 到期即视为属主已死 → 另一个 worker 直接接手，这同时就是崩溃恢复入口
（`list_recoverable` 只是给 runner 一个有序待办视图，真正决定归属的仍是 `claim`）。
R11 的"按用户串行"在同一处强制：同 (tenant, user) 有在途 lease 时该用户的其它 job
不被认领；别人的 job 不受影响。

# 阶段与中间态（AC6）

`stage` 只由属主推进，`state` 是该阶段的持久化布局（自由格式 JSON，由后续票填入）。
崩溃重启的 worker 从 `stage` + `state` 续跑，而不是从零重来。
硬约束：`state` 只能放 id / 计数 / 结构化决策摘要——**不得**放提示词、模型原始响应、
隐藏推理、凭据、大块工具输出或 artifact 内容（ticket Must Not Do 与 R2）。
本模块不校验这一点：这是内部契约而非信任边界，AC9 的探针负责兜底。

# 刻意不做的事

- 不实现心跳续租。lease 时长的约定是"长于 job 的 120 秒硬截止"，见 `DEFAULT_LEASE_SECONDS`。
- 不在这里写记录行或 outbox（那是 apply 阶段的事，属 MEM-V2-2 的执行器）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_harness.memory.v2._sqlite import connect, stamp
from agent_harness.memory.v2.types import TrustedMemoryIdentity

logger = logging.getLogger(__name__)

#: lease 默认时长。取舍：取一个明显长于 PRD §5.3 第 5 条 120 秒硬截止的值，于是
#: 正常在跑（即便很慢）的 job 不会被别人抢走，也就不需要心跳续租这个额外机制。
#: ponytail: 固定 lease + 无心跳；若将来单 job 预算超过它，再上心跳续租。
DEFAULT_LEASE_SECONDS = 300.0

_JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_v2_jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, project_id TEXT,
    session_id TEXT NOT NULL,
    stage TEXT NOT NULL, state TEXT NOT NULL,
    outcome TEXT, reason TEXT,
    lease_owner TEXT, lease_expires_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_v2_jobs_recoverable
    ON memory_v2_jobs(stage, lease_expires_at);
CREATE INDEX IF NOT EXISTS memory_v2_jobs_owner
    ON memory_v2_jobs(tenant_id, user_id, stage);
"""


class MemoryJobStage(str, Enum):
    """job 的阶段状态机。顺序即推进方向；`COMPLETED` / `DEGRADED` 是终态。"""

    QUEUED = "queued"
    FORMING = "forming"
    ADJUDICATING = "adjudicating"
    APPLYING = "applying"
    #: 跑完且**成功**——含 `NO_MEMORY` / `NOOP` 这类"安静地什么都没写"（AC2）。
    COMPLETED = "completed"
    #: 跑完但失败——不写任何记忆，理由记录在 `reason`。
    DEGRADED = "degraded"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STAGES


TERMINAL_STAGES: tuple[MemoryJobStage, ...] = (MemoryJobStage.COMPLETED, MemoryJobStage.DEGRADED)

_TERMINAL_VALUES: tuple[str, ...] = tuple(stage.value for stage in TERMINAL_STAGES)
#: `stage NOT IN (?, ?)` 的占位符，从枚举派生——避免 DDL / SQL 里再抄一份状态名。
_NOT_TERMINAL = ", ".join("?" for _ in _TERMINAL_VALUES)


class MemoryJobOutcome(str, Enum):
    """成功终态的两种形态（R12：安静成功不能装成错误，也不能装成"写过了"）。"""

    #: 至少提交了一次逻辑变更（`memory/updated` 的来源）。
    COMMITTED = "committed"
    #: 明确不写：`NO_MEMORY`、全部 `NOOP`（无 degraded 事件）。
    NO_WRITE = "no_write"


@dataclass(frozen=True, slots=True)
class MemoryFormationJob:
    """一行 durable job 的完整视图。"""

    job_id: str
    idempotency_key: str
    tenant_id: str
    user_id: str
    project_id: str | None
    session_id: str
    stage: MemoryJobStage
    state: dict[str, Any]
    outcome: MemoryJobOutcome | None
    reason: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str

    @property
    def trusted(self) -> TrustedMemoryIdentity:
        """本 job 的可信身份——由入队时写入的**可信上下文**还原，不由 job 内容自述。"""
        return TrustedMemoryIdentity(
            tenant_id=self.tenant_id, user_id=self.user_id, project_id=self.project_id)


class SqliteMemoryV2JobStore:
    """formation job 的 SQLite 权威存储（与记录存储共用一个库文件）。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with connect(self.database_path) as connection:
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.executescript(_JOB_SCHEMA)
            await connection.commit()

    # ----------------------------------------------------------------------------------
    # 入队
    # ----------------------------------------------------------------------------------

    async def enqueue(
        self, *, idempotency_key: str, trusted: TrustedMemoryIdentity, session_id: str,
        now: datetime | None = None,
    ) -> MemoryFormationJob:
        """按幂等键入队；键已存在就返回**已有**那一行（含已终结的），不新建。

        `INSERT OR IGNORE` 而非"先 SELECT 再 INSERT"：后者在并发终结下会双方都查到
        不存在、双双插入，而 UNIQUE 又会把其中一个变成异常——那等于把幂等性交给调用方
        重试。这里让"重复"成为一个静默成功的返回。
        """
        moment = stamp(now)
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "INSERT OR IGNORE INTO memory_v2_jobs "
                "(job_id, idempotency_key, tenant_id, user_id, project_id, session_id, "
                " stage, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)",
                (str(uuid4()), idempotency_key, trusted.tenant_id, trusted.user_id,
                 trusted.project_id, session_id, MemoryJobStage.QUEUED.value, moment, moment))
            cursor = await connection.execute(
                "SELECT * FROM memory_v2_jobs WHERE idempotency_key=?", (idempotency_key,))
            row = await cursor.fetchone()
            await connection.commit()
        return _to_job(_require(row, idempotency_key))

    # ----------------------------------------------------------------------------------
    # 认领（单属主 + 按用户串行）
    # ----------------------------------------------------------------------------------

    async def claim(
        self, *, worker_id: str, lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> MemoryFormationJob | None:
        """认领一个待跑/待恢复的 job；没有可跑的返回 `None`。

        "可跑"= 未终结 ∧（无属主 ∨ lease 已到期）∧ 同用户没有别的在途 lease。
        最后一条把 R11 的按用户串行也放进同一次 CAS——放进程内存里做会随重启失效，
        而且两个进程会各串各的。
        """
        base = now if now is not None else datetime.now(UTC)
        now_stamp = stamp(base)
        expiry_stamp = stamp(base + timedelta(seconds=lease_seconds))
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            # 候选查询里不需要排除"自己"：候选行的 lease 已被外层条件限定为"无属主或已到期"，
            # 而下面的自占用子查询要求 lease_owner IS NOT NULL 且未到期，两者不可能同时成立。
            cursor = await connection.execute(
                f"SELECT * FROM memory_v2_jobs AS candidate "
                f"WHERE candidate.stage NOT IN ({_NOT_TERMINAL}) "
                "  AND (candidate.lease_owner IS NULL OR candidate.lease_expires_at <= ?) "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM memory_v2_jobs AS busy "
                "      WHERE busy.tenant_id = candidate.tenant_id "
                "        AND busy.user_id = candidate.user_id "
                f"        AND busy.stage NOT IN ({_NOT_TERMINAL}) "
                "        AND busy.lease_owner IS NOT NULL AND busy.lease_expires_at > ?) "
                "ORDER BY candidate.created_at, candidate.job_id LIMIT 1",
                (*_TERMINAL_VALUES, now_stamp, *_TERMINAL_VALUES, now_stamp))
            row = await cursor.fetchone()
            if row is None:
                await connection.commit()
                return None
            updated = await connection.execute(
                f"UPDATE memory_v2_jobs SET lease_owner=?, lease_expires_at=?, updated_at=? "
                f"WHERE job_id=? AND stage NOT IN ({_NOT_TERMINAL}) "
                "  AND (lease_owner IS NULL OR lease_expires_at <= ?)",
                (worker_id, expiry_stamp, now_stamp, row["job_id"],
                 *_TERMINAL_VALUES, now_stamp))
            if updated.rowcount != 1:
                # 同一事务里刚选出来的行不可能被别人改走；行数不等于 1 说明前提被破坏，
                # 宁可什么都不认领也不认领一个自己不拥有的 job。
                logger.error("Memory V2 job %s could not be claimed after selection", row["job_id"])
                await connection.commit()
                return None
            cursor = await connection.execute(
                "SELECT * FROM memory_v2_jobs WHERE job_id=?", (row["job_id"],))
            claimed = await cursor.fetchone()
            await connection.commit()
        return _to_job(_require(claimed, str(row["job_id"])))

    async def list_recoverable(self, *, limit: int = 100) -> list[MemoryFormationJob]:
        """尚未终结的 job（旧→新）。给 runner 做启动期恢复扫描用的待办视图。"""
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                f"SELECT * FROM memory_v2_jobs WHERE stage NOT IN ({_NOT_TERMINAL}) "
                "ORDER BY created_at, job_id LIMIT ?",
                (*_TERMINAL_VALUES, max(0, limit))) as cursor,
        ):
            rows = await cursor.fetchall()
        return [_to_job(row) for row in rows]

    # ----------------------------------------------------------------------------------
    # 阶段推进
    # ----------------------------------------------------------------------------------

    async def transition(
        self, *, job_id: str, worker_id: str, stage: MemoryJobStage,
        state: dict[str, Any] | None = None, outcome: MemoryJobOutcome | None = None,
        reason: str | None = None, now: datetime | None = None,
    ) -> MemoryFormationJob | None:
        """属主推进阶段（或终结）；不满足前提时返回 `None`，不抛异常。

        → `None` 的三种情形都是**正常竞争结果**而不是错误：属主不是我、我的 lease 已到期、
        job 已终结。调用方（恢复扫描里的僵尸 worker）据此安静退出即可。

        参数纪律（把"调用方写错"和"竞争失败"分开，前者必须响亮）：
        - 终态 `COMPLETED` 必须给 `outcome`（`reason` 可选，用来记 `NO_MEMORY` 的 skip reason）；
        - 终态 `DEGRADED` 必须给 `reason`，且不得给 `outcome`；
        - 中间态不得给 `outcome` / `reason`。
        """
        if stage.is_terminal:
            if stage is MemoryJobStage.COMPLETED and outcome is None:
                raise ValueError("completing a job requires an outcome")
            if stage is MemoryJobStage.DEGRADED:
                if reason is None:
                    raise ValueError("degrading a job requires a reason")
                if outcome is not None:
                    raise ValueError("a degraded job must not carry an outcome")
        elif outcome is not None or reason is not None:
            raise ValueError("outcome/reason are only meaningful for a terminal stage")

        now_stamp = stamp(now)
        assignments = ["stage=?", "updated_at=?"]
        values: list[Any] = [stage.value, now_stamp]
        if state is not None:
            assignments.append("state=?")
            values.append(json.dumps(state, ensure_ascii=False, sort_keys=True))
        if stage.is_terminal:
            # 终结时释放 lease：终态 job 不该再占着"该用户的在途槽位"（R11）。
            assignments.extend(["outcome=?", "reason=?", "lease_owner=NULL",
                                "lease_expires_at=NULL"])
            values.extend([outcome.value if outcome is not None else None, reason])
        values.extend([job_id, worker_id, now_stamp, *_TERMINAL_VALUES])
        async with connect(self.database_path) as connection:
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                f"UPDATE memory_v2_jobs SET {', '.join(assignments)} "
                f"WHERE job_id=? AND lease_owner=? AND lease_expires_at > ? "
                f"  AND stage NOT IN ({_NOT_TERMINAL})",
                values)
            moved = cursor.rowcount == 1
            row = None
            if moved:
                rows = await connection.execute(
                    "SELECT * FROM memory_v2_jobs WHERE job_id=?", (job_id,))
                row = await rows.fetchone()
            await connection.commit()
        return _to_job(row) if row is not None else None

    # ----------------------------------------------------------------------------------
    # 读
    # ----------------------------------------------------------------------------------

    async def get(self, job_id: str) -> MemoryFormationJob:
        async with (
            connect(self.database_path) as connection,
            connection.execute(
                "SELECT * FROM memory_v2_jobs WHERE job_id=?", (job_id,)) as cursor,
        ):
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(job_id)
        return _to_job(row)


def _require(row: Any, key: str) -> Any:
    """事务内写过的行读不回来 = 前提被破坏，必须响亮而不是返回半成品。"""
    if row is None:
        raise RuntimeError(f"memory_v2_jobs row disappeared for {key}")
    return row


def _to_job(row: Any) -> MemoryFormationJob:
    return MemoryFormationJob(
        job_id=row["job_id"],
        idempotency_key=row["idempotency_key"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        project_id=row["project_id"],
        session_id=row["session_id"],
        stage=MemoryJobStage(row["stage"]),
        state=json.loads(row["state"]),
        outcome=MemoryJobOutcome(row["outcome"]) if row["outcome"] is not None else None,
        reason=row["reason"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
