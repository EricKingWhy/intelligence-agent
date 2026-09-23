"""#298 / MEM-V2-2 的 durable formation job 存储契约测试。

Seam：`SqliteMemoryV2JobStore` 的公开方法（§8.1 第 2 个 approved seam 的持久化半边）。
按垂直切片推进：一片测试 → 一片实现 → 下一片；每一片先红后绿。

本片只钉住 AC6 四个 kill 窗口能恢复所依赖的四件事：
**幂等入队（R1）/ 单属主 claim / lease 到期回收 / 阶段与中间态持久化**。
形成与裁决的契约、预算、事件在各自的 seam 上另测。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agent_harness.memory.v2 import TrustedMemoryIdentity
from agent_harness.memory.v2.jobs import (
    MemoryJobOutcome,
    MemoryJobStage,
    SqliteMemoryV2JobStore,
)
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from tests.memory.v2._records import make_draft

USER_A = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a")
PROJECT_X = TrustedMemoryIdentity(
    tenant_id="tenant-a", user_id="user-a", project_id="project-x")
USER_B = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-b")

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def jobs(tmp_path) -> SqliteMemoryV2JobStore:
    instance = SqliteMemoryV2JobStore(tmp_path / "memory-v2.db")
    await instance.initialize()
    return instance


async def _enqueue(store: SqliteMemoryV2JobStore, key: str = "session-1",
                   trusted: TrustedMemoryIdentity = USER_A):
    return await store.enqueue(idempotency_key=key, trusted=trusted, session_id=key)


# --------------------------------------------------------------------------------------
# 切片 1：入队 + 读取往返（R1 前半）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_persists_a_queued_job_with_no_owner(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    job = await _enqueue(jobs, "session-1")

    assert job.stage is MemoryJobStage.QUEUED
    assert job.outcome is None and job.reason is None
    assert job.lease_owner is None and job.lease_expires_at is None
    assert job.state == {}
    assert (job.tenant_id, job.user_id, job.project_id) == ("tenant-a", "user-a", None)
    assert job.session_id == "session-1"
    assert await jobs.get(job.job_id) == job


@pytest.mark.asyncio
async def test_a_project_scoped_run_keeps_its_trusted_project(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    """项目 id 只能来自可信身份——入队方给不了，也给不错。"""
    job = await jobs.enqueue(idempotency_key="s", trusted=PROJECT_X, session_id="s")

    assert job.project_id == "project-x"
    assert job.trusted == PROJECT_X


@pytest.mark.asyncio
async def test_get_raises_for_an_unknown_job(jobs: SqliteMemoryV2JobStore) -> None:
    with pytest.raises(KeyError):
        await jobs.get("no-such-job")


# --------------------------------------------------------------------------------------
# 切片 2：幂等入队（R1）——重复终结 / 恢复扫描不得造出第二个 job
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_twice_on_one_key_yields_one_job(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    first = await _enqueue(jobs, "session-1")
    second = await _enqueue(jobs, "session-1")

    assert second.job_id == first.job_id
    assert [item.job_id for item in await jobs.list_recoverable()] == [first.job_id]


@pytest.mark.asyncio
async def test_enqueue_on_the_same_key_from_a_restarted_store_reuses_the_job(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    """重复终结的第二个来源是"进程重启后重放终结路径"——幂等键必须跨实例成立。"""
    first = await _enqueue(jobs, "session-1")
    restarted = SqliteMemoryV2JobStore(jobs.database_path)
    await restarted.initialize()

    assert (await _enqueue(restarted, "session-1")).job_id == first.job_id


@pytest.mark.asyncio
async def test_a_terminal_job_is_never_re_enqueued(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    """R1：一条 eligible run 恒映射到同一个 job——已经终结的也不能被"重开"。"""
    job = await _enqueue(jobs, "session-1")
    await jobs.claim(worker_id="w1", lease_seconds=3600)
    await jobs.transition(
        job_id=job.job_id, worker_id="w1", stage=MemoryJobStage.COMPLETED,
        outcome=MemoryJobOutcome.NO_WRITE, reason="no_durable_value")

    again = await _enqueue(jobs, "session-1")

    assert again.job_id == job.job_id
    assert again.stage is MemoryJobStage.COMPLETED
    assert again.outcome is MemoryJobOutcome.NO_WRITE
    assert again.reason == "no_durable_value"
    assert await jobs.list_recoverable() == []


# --------------------------------------------------------------------------------------
# 切片 3：claim 的单属主与 lease（AC6 的"恢复"入口）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_grants_exclusive_ownership_while_the_lease_is_live(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    job = await _enqueue(jobs, "session-1")

    claimed = await jobs.claim(worker_id="w1", lease_seconds=3600, now=T0)

    assert claimed is not None and claimed.job_id == job.job_id
    assert claimed.lease_owner == "w1"
    assert claimed.lease_expires_at is not None
    # 属主仍持有 → 第二个 worker 拿不到（"单属主"就是这一条）
    assert await jobs.claim(worker_id="w2", lease_seconds=3600, now=T0) is None


@pytest.mark.asyncio
async def test_an_expired_lease_is_reclaimable_by_another_worker(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    """崩溃恢复的机制半边：属主消失 → lease 到期 → 另一个 worker 接手。"""
    job = await _enqueue(jobs, "session-1")
    await jobs.claim(worker_id="w1", lease_seconds=60, now=T0)

    later = T0 + timedelta(seconds=120)
    recovered = await jobs.claim(worker_id="w2", lease_seconds=60, now=later)

    assert recovered is not None and recovered.job_id == job.job_id
    assert recovered.lease_owner == "w2"


@pytest.mark.asyncio
async def test_a_sub_second_lease_is_still_honoured(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    """到期判断是在 SQL 里做的字符串比较 ⇒ 同一秒内的先后也必须排在正确的一侧。

    这是"时间戳归一到 UTC 且字典序 == 时间序"这条约定的判别性用例：若把 `stamp`
    换成不带时区的 `datetime.now()` 或非 UTC 偏移，这里就会在边界上判反。
    """
    await _enqueue(jobs, "session-1")
    await jobs.claim(worker_id="w1", lease_seconds=0.0004, now=T0)

    assert await jobs.claim(worker_id="w2", lease_seconds=60, now=T0) is None
    assert await jobs.claim(
        worker_id="w3", lease_seconds=60, now=T0 + timedelta(microseconds=100)) is None

    expired = await jobs.claim(
        worker_id="w4", lease_seconds=60, now=T0 + timedelta(microseconds=500))
    assert expired is not None and expired.lease_owner == "w4"


@pytest.mark.asyncio
async def test_claim_skips_terminal_jobs(jobs: SqliteMemoryV2JobStore) -> None:
    job = await _enqueue(jobs, "session-1")
    await jobs.claim(worker_id="w1", lease_seconds=3600, now=T0)
    await jobs.transition(
        job_id=job.job_id, worker_id="w1", stage=MemoryJobStage.DEGRADED,
        reason="budget_exhausted", now=T0)
    # 终结时释放 lease → 即使没人独占也不能再被认领
    assert await jobs.claim(worker_id="w2", lease_seconds=3600, now=T0) is None


@pytest.mark.asyncio
async def test_jobs_serialize_per_user_while_other_users_proceed(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    """R11：同一用户的 job 串行；不同用户互不阻塞。"""
    await _enqueue(jobs, "a-1", USER_A)
    await _enqueue(jobs, "a-2", USER_A)
    await _enqueue(jobs, "b-1", USER_B)

    first = await jobs.claim(worker_id="w1", lease_seconds=3600, now=T0)
    assert first is not None and first.user_id == "user-a"

    # A 的第二条仍被前一条的在途 lease 挡住，但 B 可以并行推进
    second = await jobs.claim(worker_id="w2", lease_seconds=3600, now=T0)
    assert second is not None and second.user_id == "user-b"
    assert await jobs.claim(worker_id="w3", lease_seconds=3600, now=T0) is None


@pytest.mark.asyncio
async def test_concurrent_claims_have_exactly_one_winner(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    """单属主是数据库 CAS 的保证，不是进程内纪律：并发认领只能有一个成功。"""
    await _enqueue(jobs, "session-1")

    results = await asyncio.gather(*[
        jobs.claim(worker_id=f"w{index}", lease_seconds=3600, now=T0) for index in range(4)
    ])

    winners = [item for item in results if item is not None]
    assert len(winners) == 1


# --------------------------------------------------------------------------------------
# 切片 4：阶段迁移只属于属主，且必须持久化（AC6 的"从中间态续跑"）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_is_owner_only_and_persists_stage_and_state(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    job = await _enqueue(jobs, "session-1")
    await jobs.claim(worker_id="w1", lease_seconds=3600, now=T0)

    assert await jobs.transition(
        job_id=job.job_id, worker_id="w2", stage=MemoryJobStage.FORMING, now=T0) is None

    moved = await jobs.transition(
        job_id=job.job_id, worker_id="w1", stage=MemoryJobStage.FORMING,
        state={"candidates": ["c1"]}, now=T0)

    assert moved is not None and moved.stage is MemoryJobStage.FORMING
    assert moved.state == {"candidates": ["c1"]}
    assert (await jobs.get(job.job_id)).state == {"candidates": ["c1"]}


@pytest.mark.asyncio
async def test_a_lapsed_lease_may_not_transition(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    """僵尸 worker 的迟到写入必须被拒——否则会把已回收的 job 改坏。"""
    job = await _enqueue(jobs, "session-1")
    await jobs.claim(worker_id="w1", lease_seconds=60, now=T0)

    late = T0 + timedelta(seconds=120)
    assert await jobs.transition(
        job_id=job.job_id, worker_id="w1", stage=MemoryJobStage.FORMING, now=late) is None


@pytest.mark.asyncio
async def test_completing_a_job_records_its_outcome_and_releases_the_lease(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    job = await _enqueue(jobs, "session-1")
    await jobs.claim(worker_id="w1", lease_seconds=3600, now=T0)

    done = await jobs.transition(
        job_id=job.job_id, worker_id="w1", stage=MemoryJobStage.COMPLETED,
        outcome=MemoryJobOutcome.COMMITTED, state={"committed": ["mem-1"]}, now=T0)

    assert done is not None and done.outcome is MemoryJobOutcome.COMMITTED
    assert done.lease_owner is None and done.lease_expires_at is None
    assert await jobs.transition(
        job_id=job.job_id, worker_id="w1", stage=MemoryJobStage.FORMING, now=T0) is None


@pytest.mark.asyncio
async def test_a_crashed_job_resumes_from_its_persisted_stage(
    jobs: SqliteMemoryV2JobStore,
) -> None:
    """AC6 的"Formation 完成后被杀"窗口：重启后必须从持久化阶段续跑，而不是从零重来。"""
    job = await _enqueue(jobs, "session-1")
    await jobs.claim(worker_id="w1", lease_seconds=60, now=T0)
    await jobs.transition(
        job_id=job.job_id, worker_id="w1", stage=MemoryJobStage.ADJUDICATING,
        state={"calls_used": 2, "formation": {"decision": "CANDIDATES"}}, now=T0)

    restarted = SqliteMemoryV2JobStore(jobs.database_path)
    await restarted.initialize()
    later = T0 + timedelta(seconds=120)

    assert [item.job_id for item in await restarted.list_recoverable()] == [job.job_id]
    resumed = await restarted.claim(worker_id="w2", lease_seconds=60, now=later)

    assert resumed is not None
    assert resumed.stage is MemoryJobStage.ADJUDICATING
    assert resumed.state == {"calls_used": 2, "formation": {"decision": "CANDIDATES"}}


# --------------------------------------------------------------------------------------
# 切片 5：与记录存储共用同一个 SQLite 基底（§7.2 第 3 条 / §6.6）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_job_table_coexists_with_record_writes_in_one_database(
    tmp_path,
) -> None:
    path = tmp_path / "memory-v2.db"
    records = SqliteMemoryV2Store(path)
    await records.initialize()
    job_store = SqliteMemoryV2JobStore(path)
    await job_store.initialize()

    record = await records.create(make_draft(), USER_A)
    job = await job_store.enqueue(idempotency_key="s", trusted=USER_A, session_id="s")

    assert record.id
    assert (await job_store.get(job.job_id)).stage is MemoryJobStage.QUEUED
    assert (await records.get(record.id, USER_A)).id == record.id
