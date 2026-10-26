"""#298 / MEM-V2-2 的 durable formation job；#1494 起由文件承载操作状态。

# 为什么不再放在 memory-v2.db（#1494 / ADR-0056 §3.2 + ADR-0058 D4）

ADR-0056 把「job / lease / 幂等」判给非 SQLite 的持久 owner：操作状态与内容的派生层
（records / outbox / FTS）是两种生命周期，把它们放同一张库里，终态就只能在"同一笔
SQLite 事务"里达成——而那个事务恰好是 #1494 要拆掉的耦合。现在每个 job 是
``<workspace>/memory-state/jobs/<job_id>.json``：

- **幂等**：`job_id` 由 `idempotency_key` 确定性派生（sha256），文件存在即已入队；
  同一 key 在物理上不可能产生两个 job。旧 `UNIQUE(idempotency_key)` 的语义完整保留，
  且不再需要"先查后写"。
- **单属主**：`claim` 在全局认领锁内选候选、在 job 锁内写 lease（CAS）；`transition`
  与 `commit_with_outcome` 都在 job 锁内复核 `lease_owner + lease_expires_at`。
  flock 由内核在进程死亡时释放，因此"属主死了"不需要过期判定就能被接管；lease 仍是
  判定"还能不能推进"的显式事实（AC4：lease 丢失零 commit）。
- **阶段与终态**：`stage` / `state` / `outcome` / `reason` 就是文件字段；写入是单文件
  原子替换（`file_state.write_json`）。

# 与记录层的关系（ADR-0058 D8）

records / outbox 仍是 SQLite（可重建的派生态）。`commit_with_outcome` 的顺序是
**work（记录事务）→ 终态（job 文件）**，并在提交前复核 lease：

- work 抛异常 ⇒ 记录事务回滚、零写入、job 保持非终态（与旧实现同语义）；
- lease 在 work 期间被抢走 ⇒ 提交前复核失败 ⇒ 记录事务回滚、零写入（AC4）；
- 记录已提交、终态写失败/被杀 ⇒ 恢复扫描重跑：记录写入按调用方给出的确定性
  `memory_id` 幂等（见 `executor._apply_one`），内容写入按 `jobp_` 回执回放。
  这里**不冒称**两份载体跨文件原子。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from agent_harness.memory.v2._sqlite import stamp, write_connection
from agent_harness.memory.v2.file_state import (
    MEMORY_STATE_DIRECTORY_NAME,
    CorruptStateFile,
    MemoryStateRoot,
    async_file_lock,
    file_lock,
    read_json,
    safe_id,
    write_json,
)
from agent_harness.memory.v2.types import TrustedMemoryIdentity

#: `commit_with_outcome` 里 `work` 的返回类型（调用方自己决定要带回什么）。
T = TypeVar("T")

logger = logging.getLogger(__name__)

#: lease 默认时长。取舍：取一个明显长于 PRD §5.3 第 5 条 120 秒硬截止的值，于是
#: 正常在跑（即便很慢）的 job 不会被别人抢走，也就不需要心跳续租这个额外机制。
#: ponytail: 固定 lease + 无心跳；若将来单 job 预算超过它，再上心跳续租。
DEFAULT_LEASE_SECONDS = 300.0

SCHEMA_VERSION = 1


class MemoryJobStage(str, Enum):
    """job 的阶段状态机。顺序即推进方向；`COMPLETED` / `DEGRADED` 是终态。"""

    QUEUED = "queued"
    FORMING = "forming"
    ADJUDICATING = "adjudicating"
    APPLYING = "applying"
    #: 跑完且**成功**——含 `NO_MEMORY` / `NOOP` 这类"安静地什么都没写"（AC2）。
    #: #1461：被审批门拦下的 job 也停在这里——终态 stage 相同，语义由 `outcome` 区分
    #: COMMITTED / NO_WRITE / BLOCKED。
    COMPLETED = "completed"
    #: 跑完但失败——不写任何记忆，理由记录在 `reason`。
    DEGRADED = "degraded"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STAGES


TERMINAL_STAGES: tuple[MemoryJobStage, ...] = (MemoryJobStage.COMPLETED, MemoryJobStage.DEGRADED)


class MemoryJobOutcome(str, Enum):
    """成功终态的三种形态（R12：安静成功不能装成错误，也不能装成"写过了"）。"""

    #: 至少提交了一次逻辑变更（`memory/updated` 的来源）。
    COMMITTED = "committed"
    #: 明确不写：`NO_MEMORY`、全部 `NOOP`（无 degraded 事件）。
    NO_WRITE = "no_write"
    #: #1461：审批门开着，写入被拦下并落进 blocked 文件等人处置。
    BLOCKED = "blocked"


class ProtectedFactExtractionState(str, Enum):
    PENDING = "pending"
    STARTED = "started"
    READY = "ready"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class MemoryFormationJob:
    """一个 durable job 的完整视图。"""

    job_id: str
    idempotency_key: str
    tenant_id: str
    user_id: str
    project_id: str | None
    session_id: str
    #: 本 job 属于哪一次 run（T7 新增）。恢复扫描只能靠 `(session_id, run_id)` 从会话
    #: 日志里切出这一次 run 的事件；没有它就无法在重启后重建模型输入。
    run_id: str | None
    stage: MemoryJobStage
    state: dict[str, Any]
    outcome: MemoryJobOutcome | None
    reason: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    protected_fact_token_budget: int | None = None
    protected_fact_extraction_state: ProtectedFactExtractionState = (
        ProtectedFactExtractionState.PENDING
    )
    protected_fact_extraction_candidates: list[dict[str, str]] | None = None

    @property
    def trusted(self) -> TrustedMemoryIdentity:
        """本 job 的可信身份——由入队时写入的**可信上下文**还原，不由 job 内容自述。"""
        return TrustedMemoryIdentity(
            tenant_id=self.tenant_id, user_id=self.user_id, project_id=self.project_id)

    @property
    def is_terminal(self) -> bool:
        return self.stage.is_terminal


class _LeaseLost(Exception):
    """提交前复核失败：lease 已不属于本 worker。记录事务必须回滚（零写入）。"""


def job_id_for(idempotency_key: str) -> str:
    """幂等键 → 确定性 job id（文件名安全）。

    取 sha256 的前 32 个十六进制字符：碰撞概率与 UUIDv4 同量级，换来的性质是"同 key
    永远同一个 job id"——这正是物理幂等（文件存在即已入队）的前提。
    """
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"job_{digest}"


class MemoryV2JobStore:
    """formation job 的文件权威存储。

    ``database_path`` 只用于 `commit_with_outcome` 打开记录层事务（records / outbox
    仍是 SQLite 派生态）；job 自身的事实全部在 ``state_root`` 下。
    """

    def __init__(
        self, database_path: str | Path, state_root: MemoryStateRoot | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.root = state_root or MemoryStateRoot(
            self.database_path.parent / MEMORY_STATE_DIRECTORY_NAME
        )

    async def initialize(self) -> None:
        self.root.ensure()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 路径

    def path_for(self, job_id: str) -> Path:
        return self.root.jobs_dir / f"{safe_id(job_id, what='job id')}.json"

    def _lock_path(self, job_id: str) -> Path:
        return self.root.jobs_dir / safe_id(job_id, what="job id")

    @property
    def _claim_lock_path(self) -> Path:
        # 一次认领 = 选候选 + 写 lease。全局一把锁让"同用户只有一个在途 job"在
        # 跨进程下也成立；认领是低频动作，不值得为它维护按用户的多把锁。
        return self.root.jobs_dir / "claim"

    # ------------------------------------------------------------------ 读写原语

    def _read(self, job_id: str) -> MemoryFormationJob:
        payload = read_json(self.path_for(job_id))
        if payload is None:
            raise KeyError(job_id)
        return _to_job(payload)

    def _try_read(self, job_id: str) -> MemoryFormationJob | None:
        try:
            return self._read(job_id)
        except KeyError:
            return None

    def _write(self, job: MemoryFormationJob, *, updated_at: str) -> MemoryFormationJob:
        payload = _to_payload(job, updated_at=updated_at)
        write_json(self.path_for(job.job_id), payload)
        return _to_job(payload)

    async def _read_async(self, job_id: str) -> MemoryFormationJob:
        return await asyncio.to_thread(self._read, job_id)

    async def _try_read_async(self, job_id: str) -> MemoryFormationJob | None:
        return await asyncio.to_thread(self._try_read, job_id)

    async def _write_async(self, job: MemoryFormationJob, *, updated_at: str) -> MemoryFormationJob:
        return await asyncio.to_thread(self._write, job, updated_at=updated_at)

    @staticmethod
    def _owned_and_live(job: MemoryFormationJob, worker_id: str, now_stamp: str) -> bool:
        return (
            job.stage not in TERMINAL_STAGES
            and job.lease_owner == worker_id
            and job.lease_expires_at is not None
            and job.lease_expires_at > now_stamp
        )

    def _scan(self) -> list[MemoryFormationJob]:
        directory = self.root.jobs_dir
        if not directory.exists():
            return []
        jobs: list[MemoryFormationJob] = []
        for path in sorted(directory.glob("*.json")):
            payload = read_json(path)
            if payload is None:
                continue
            try:
                jobs.append(_to_job(payload))
            except (CorruptStateFile, KeyError, ValueError) as error:
                logger.warning("unreadable job file skipped (%s): %s", path.name, error)
        return jobs

    # ------------------------------------------------------------------ 入队

    async def enqueue(
        self, *, idempotency_key: str, trusted: TrustedMemoryIdentity, session_id: str,
        run_id: str | None = None, protected_fact_token_budget: int | None = None,
        now: datetime | None = None,
    ) -> MemoryFormationJob:
        """按幂等键入队；键已存在就返回**已有**那个 job（含已终结的），不新建。"""
        moment = stamp(now)
        job_id = job_id_for(idempotency_key)

        def _enqueue() -> MemoryFormationJob:
            with file_lock(self._claim_lock_path):
                existing = self._try_read(job_id)
                if existing is not None:
                    return existing
                job = MemoryFormationJob(
                    job_id=job_id, idempotency_key=idempotency_key,
                    tenant_id=trusted.tenant_id, user_id=trusted.user_id,
                    project_id=trusted.project_id, session_id=session_id, run_id=run_id,
                    stage=MemoryJobStage.QUEUED, state={}, outcome=None, reason=None,
                    lease_owner=None, lease_expires_at=None,
                    created_at=moment, updated_at=moment,
                    protected_fact_token_budget=protected_fact_token_budget,
                )
                return self._write(job, updated_at=moment)

        return await asyncio.to_thread(_enqueue)

    # ------------------------------------------------------------------ 认领

    async def claim(
        self, *, worker_id: str, lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> MemoryFormationJob | None:
        """认领一个待跑/待恢复的 job；没有可跑的返回 `None`。

        "可跑" = 未终结 ∧（无属主 ∨ lease 已到期）∧ 同用户没有别的在途 lease。
        候选选择与写入都在认领锁内，写入再叠加 job 锁（固定锁序：认领锁 → job 锁）。
        """
        base = now if now is not None else datetime.now(UTC)
        now_stamp = stamp(base)
        expiry_stamp = stamp(base + timedelta(seconds=lease_seconds))

        def _claim() -> MemoryFormationJob | None:
            with file_lock(self._claim_lock_path):
                jobs = self._scan()
                live_owners = {
                    (job.tenant_id, job.user_id)
                    for job in jobs
                    if job.stage not in TERMINAL_STAGES
                    and job.lease_owner is not None
                    and (job.lease_expires_at or "") > now_stamp
                }
                candidates = sorted(
                    (
                        job for job in jobs
                        if job.stage not in TERMINAL_STAGES
                        and (job.lease_owner is None or (job.lease_expires_at or "") <= now_stamp)
                    ),
                    key=lambda job: (job.created_at, job.job_id),
                )
                for candidate in candidates:
                    if (candidate.tenant_id, candidate.user_id) in live_owners:
                        continue
                    with file_lock(self._lock_path(candidate.job_id)):
                        current = self._try_read(candidate.job_id)
                        if current is None:
                            continue
                        if not (
                            current.stage not in TERMINAL_STAGES
                            and (current.lease_owner is None
                                 or (current.lease_expires_at or "") <= now_stamp)
                        ):
                            continue
                        claimed = MemoryFormationJob(
                            **{
                                **_job_fields(current),
                                "lease_owner": worker_id,
                                "lease_expires_at": expiry_stamp,
                            }
                        )
                        return self._write(claimed, updated_at=now_stamp)
            return None

        return await asyncio.to_thread(_claim)

    async def list_recoverable(self, *, limit: int = 100) -> list[MemoryFormationJob]:
        """尚未终结的 job（旧→新）。给 runner 做启动期恢复扫描用的待办视图。"""
        jobs = [
            job for job in await asyncio.to_thread(self._scan)
            if job.stage not in TERMINAL_STAGES
        ]
        jobs.sort(key=lambda job: (job.created_at, job.job_id))
        return jobs[: max(0, limit)]

    # ------------------------------------------------------------------ 阶段推进

    async def transition(
        self, *, job_id: str, worker_id: str, stage: MemoryJobStage,
        state: dict[str, Any] | None = None, outcome: MemoryJobOutcome | None = None,
        reason: str | None = None, now: datetime | None = None,
    ) -> MemoryFormationJob | None:
        """属主推进阶段（或终结）；不满足前提时返回 `None`，不抛异常。

        参数纪律（把"调用方写错"和"竞争失败"分开，前者必须响亮）：
        - 终态 `COMPLETED` 必须给 `outcome`（`reason` 可选）；
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
        async with async_file_lock(self._lock_path(job_id)):
            job = await self._try_read_async(job_id)
            if job is None or not self._owned_and_live(job, worker_id, now_stamp):
                return None
            return await self._write_async(
                _advanced(job, stage=stage, state=state, outcome=outcome, reason=reason),
                updated_at=now_stamp,
            )

    async def commit_with_outcome(
        self, *, job_id: str, worker_id: str, outcome: MemoryJobOutcome,
        work: Callable[[Any], Awaitable[T]],
        state: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
        reason: str | None = None, now: datetime | None = None,
        stage: MemoryJobStage = MemoryJobStage.COMPLETED,
        after_work: Callable[[T], Awaitable[None]] | None = None,
    ) -> T | None:
        """执行 `work(connection)`（记录层事务），随后写下成功终态。

        返回 `None` = 竞争失败（属主不是我 / lease 过期 / 已终结）——此时 `work`
        **根本不会被执行**，或（提交前复核失败时）其写入被回滚。

        顺序与保证见模块 docstring。`work` 里必须只用**确定性 id** 写派生层，
        这样"记录已提交、终态未落"的窗口重跑不会产生第二条逻辑记忆。

        `after_work`（#1494）在记录事务**已提交**、终态**未写**之间执行：markdown
        权威面的补落只能落在这个窗口（它不能参与 SQLite 事务）。它抛异常时终态不写，
        job 仍是未终结 ⇒ 恢复扫描重跑；重跑用同一批确定性 id / operation id，不会
        产生第二次逻辑写入。这正是"替换后、回执前"kill 窗口的可对账形态。
        """
        now_stamp = stamp(now)
        async with async_file_lock(self._lock_path(job_id)):
            job = await self._try_read_async(job_id)
            if job is None or not self._owned_and_live(job, worker_id, now_stamp):
                return None
            try:
                async with write_connection(self.database_path) as connection:
                    result = await work(connection)
                    # 提交前复核：work 期间 lease 被抢走 ⇒ 抛出去触发回滚（零写入）。
                    latest = await self._try_read_async(job_id)
                    if latest is None or not self._owned_and_live(latest, worker_id, now_stamp):
                        raise _LeaseLost
            except _LeaseLost:
                logger.warning("memory job %s lost its lease during apply; rolled back", job_id)
                return None
            if after_work is not None:
                await after_work(result)
            final_state = state() if callable(state) else state
            terminal = _advanced(
                latest, stage=stage, state=final_state, outcome=outcome, reason=reason)
            written = await self._write_async(terminal, updated_at=now_stamp)
            if not written.stage.is_terminal:  # pragma: no cover — _advanced 的防线
                raise RuntimeError(f"memory job {job_id} did not reach a terminal stage")
            return result

    # ------------------------------------------------------------------ 读

    async def get(self, job_id: str) -> MemoryFormationJob:
        return await self._read_async(job_id)

    # ------------------------------------------------------------------ protected fact 提取

    async def start_protected_fact_extraction(
        self, *, job_id: str, worker_id: str, now: datetime | None = None,
    ) -> MemoryFormationJob | None:
        """Durably claim the job's single protected-fact model request."""
        now_stamp = stamp(now)
        async with async_file_lock(self._lock_path(job_id)):
            job = await self._try_read_async(job_id)
            if job is None or not self._owned_and_live(job, worker_id, now_stamp):
                return None
            if job.protected_fact_extraction_state is ProtectedFactExtractionState.PENDING:
                job = MemoryFormationJob(**{
                    **_job_fields(job),
                    "protected_fact_extraction_state": ProtectedFactExtractionState.STARTED,
                    "protected_fact_extraction_candidates": None,
                })
                return await self._write_async(job, updated_at=now_stamp)
            return job

    async def save_protected_fact_candidates(
        self, *, job_id: str, worker_id: str, candidates: list[dict[str, str]],
        now: datetime | None = None,
    ) -> MemoryFormationJob | None:
        """Persist extraction output so a recovered job need not make the call twice."""
        if any(
            set(candidate) != {"source", "value"}
            or not isinstance(candidate.get("source"), str)
            or not isinstance(candidate.get("value"), str)
            for candidate in candidates
        ):
            raise ValueError("protected fact candidates must contain only source and value")
        now_stamp = stamp(now)
        async with async_file_lock(self._lock_path(job_id)):
            job = await self._try_read_async(job_id)
            if job is None or not self._owned_and_live(job, worker_id, now_stamp):
                return None
            if job.protected_fact_extraction_state is not ProtectedFactExtractionState.STARTED:
                return job
            job = MemoryFormationJob(**{
                **_job_fields(job),
                "protected_fact_extraction_state": ProtectedFactExtractionState.READY,
                "protected_fact_extraction_candidates": list(candidates),
            })
            return await self._write_async(job, updated_at=now_stamp)

    async def finish_protected_fact_extraction(
        self, *, job_id: str, worker_id: str, now: datetime | None = None,
    ) -> bool:
        """Drop raw candidate text after idempotent registration has been attempted."""
        now_stamp = stamp(now)
        async with async_file_lock(self._lock_path(job_id)):
            job = await self._try_read_async(job_id)
            if job is None or not self._owned_and_live(job, worker_id, now_stamp):
                return False
            job = MemoryFormationJob(**{
                **_job_fields(job),
                "protected_fact_extraction_state": ProtectedFactExtractionState.DONE,
                "protected_fact_extraction_candidates": None,
            })
            await self._write_async(job, updated_at=now_stamp)
            return True


# ---------------------------------------------------------------------- 纯函数


def _advanced(
    job: MemoryFormationJob, *, stage: MemoryJobStage, state: dict[str, Any] | None,
    outcome: MemoryJobOutcome | None, reason: str | None,
) -> MemoryFormationJob:
    """`transition` 与 `commit_with_outcome` 共用的状态迁移（不含锁与写盘）。"""
    changes: dict[str, Any] = {"stage": stage}
    if state is not None:
        changes["state"] = state
    if stage.is_terminal:
        # 终结时释放 lease：终态 job 不该再占着"该用户的在途槽位"（R11）。
        changes.update({
            "outcome": outcome, "reason": reason, "lease_owner": None,
            "lease_expires_at": None,
            "protected_fact_extraction_state": ProtectedFactExtractionState.DONE,
            "protected_fact_extraction_candidates": None,
        })
    return MemoryFormationJob(**{**_job_fields(job), **changes})


def _job_fields(job: MemoryFormationJob) -> dict[str, Any]:
    """dataclass 的字段快照（构造新实例时避免手抄字段名）。"""
    return {
        "job_id": job.job_id, "idempotency_key": job.idempotency_key,
        "tenant_id": job.tenant_id, "user_id": job.user_id,
        "project_id": job.project_id, "session_id": job.session_id, "run_id": job.run_id,
        "stage": job.stage, "state": job.state, "outcome": job.outcome,
        "reason": job.reason, "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at, "created_at": job.created_at,
        "updated_at": job.updated_at,
        "protected_fact_token_budget": job.protected_fact_token_budget,
        "protected_fact_extraction_state": job.protected_fact_extraction_state,
        "protected_fact_extraction_candidates": job.protected_fact_extraction_candidates,
    }


def _to_payload(job: MemoryFormationJob, *, updated_at: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "job_id": job.job_id,
        "idempotency_key": job.idempotency_key,
        "tenant_id": job.tenant_id,
        "user_id": job.user_id,
        "project_id": job.project_id,
        "session_id": job.session_id,
        "run_id": job.run_id,
        "protected_fact_token_budget": job.protected_fact_token_budget,
        "protected_fact_extraction_state": job.protected_fact_extraction_state.value,
        "protected_fact_extraction_candidates": job.protected_fact_extraction_candidates,
        "stage": job.stage.value,
        "state": job.state,
        "outcome": job.outcome.value if job.outcome is not None else None,
        "reason": job.reason,
        "lease_owner": job.lease_owner,
        "lease_expires_at": job.lease_expires_at,
        "created_at": job.created_at,
        "updated_at": updated_at,
    }


def _to_job(payload: dict[str, Any]) -> MemoryFormationJob:
    try:
        return MemoryFormationJob(
            job_id=str(payload["job_id"]),
            idempotency_key=str(payload["idempotency_key"]),
            tenant_id=str(payload["tenant_id"]),
            user_id=str(payload["user_id"]),
            project_id=payload.get("project_id"),
            session_id=str(payload["session_id"]),
            run_id=payload.get("run_id"),
            stage=MemoryJobStage(payload["stage"]),
            state=dict(payload.get("state") or {}),
            outcome=(
                MemoryJobOutcome(payload["outcome"])
                if payload.get("outcome") is not None else None
            ),
            reason=payload.get("reason"),
            lease_owner=payload.get("lease_owner"),
            lease_expires_at=payload.get("lease_expires_at"),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            protected_fact_token_budget=payload.get("protected_fact_token_budget"),
            protected_fact_extraction_state=ProtectedFactExtractionState(
                payload.get("protected_fact_extraction_state") or "pending"),
            protected_fact_extraction_candidates=payload.get(
                "protected_fact_extraction_candidates"),
        )
    except (KeyError, ValueError) as error:
        raise CorruptStateFile(f"invalid job file: {error}") from error


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "TERMINAL_STAGES",
    "MemoryFormationJob",
    "MemoryJobOutcome",
    "MemoryJobStage",
    "MemoryV2JobStore",
    "ProtectedFactExtractionState",
    "job_id_for",
]
