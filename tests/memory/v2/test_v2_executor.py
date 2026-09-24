"""#298 / MEM-V2-2 T6：形成作业执行器的终态、事务与埋雷（AC2 / AC6 / AC7 / AC9 / R12）。

用例全部跑在**真实** SQLite 上（记录表与 job 表同库），只有模型是替身——AC6 的四个
kill 窗口与 AC7 的"重放零重复"都是"磁盘上的状态说了算"的断言，假存储证明不了。

模型的替身是按**调用顺序**回放的脚本：每个元素要么是返回的文本，要么是要抛出的异常。
这样"恰好三次 primary 再切 fallback"是一条可数的断言（`invoker.calls`），而不是对某个
mock 框架调用次数的间接推断。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agent_harness.memory.v2.budget import MemoryBudgetLimits
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.executor import (
    DegradedReason,
    MemoryJobExecutor,
    MemoryModelCall,
    MemoryModelStage,
)
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.jobs import (
    MemoryFormationJob,
    MemoryJobOutcome,
    MemoryJobStage,
    SqliteMemoryV2JobStore,
)
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import (
    MemoryScope,
    MemoryStatus,
    TrustedMemoryIdentity,
)
from agent_harness.model.config import ModelConfig
from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE, SessionEvent
from agent_harness.session.event import MEMORY_DEGRADED, MEMORY_UPDATED
from tests.memory.v2._records import make_draft

USER_A = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a")
PROJECT_X = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a", project_id="project-x")

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)

#: 埋雷用的假凭证。形态命中 `policy._SECRET_PATTERNS` 的 provider token 前缀。
SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz"

#: 证据引用链用例（第 7 组）用**真 UUID** 做事件 id：`u:1` 这种短 id 有被"别名恰好像真 id"
#: 的实现蒙对的可能，UUID 蒙不出来。
RUN_UUID = "6f1a0c62-3e2d-4b8a-9c11-7d5e0a2b4c6d"
REPLY_UUID = "b3d9f0aa-1c44-4e77-8f21-0a9b8c7d6e5f"


# --------------------------------------------------------------------------------------
# 替身与工厂
# --------------------------------------------------------------------------------------


class FakeInvoker:
    """按 `stage` 分队列、按顺序回放的模型替身。

    排空后仍被调用 ⇒ `AssertionError`：执行器多调一次模型是**实现缺陷**，
    必须响亮而不是静默返回空响应。
    """

    def __init__(self, *, formation: list, adjudication: list) -> None:
        self._queues = {
            MemoryModelStage.FORMATION: list(formation),
            MemoryModelStage.ADJUDICATION: list(adjudication),
        }
        self.calls: list[MemoryModelCall] = []

    async def __call__(self, call: MemoryModelCall) -> str:
        self.calls.append(call)
        queue = self._queues[call.stage]
        if not queue:
            raise AssertionError(f"unscripted {call.stage.value} call")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def attempts(self, stage: MemoryModelStage) -> list[tuple[str, int]]:
        """`(role, attempt)` 序列——AC4/AC5 的判据。"""
        return [(c.role.value, c.attempt) for c in self.calls if c.stage is stage]


class RecordingSink:
    """事件端口替身：只记 `(type, data, run_id)`，不落盘。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str | None]] = []

    def emit(self, event_type: str, data: dict, *, run_id: str | None = None) -> None:
        self.events.append((event_type, data, run_id))

    def types(self) -> list[str]:
        return [event_type for event_type, _data, _run in self.events]


class _AuthFailure(Exception):
    """401 语义的 provider 错误：`is_transient_model_error` 如实判 False（既有判据）。"""

    status_code = 401


@dataclass
class Env:
    store: SqliteMemoryV2Store
    jobs: SqliteMemoryV2JobStore
    index: InMemoryMemoryV2Index
    service: MemoryV2Service


@pytest_asyncio.fixture
async def env(tmp_path) -> Env:
    path = tmp_path / "memory-v2.db"
    store = SqliteMemoryV2Store(path)
    await store.initialize()
    jobs = SqliteMemoryV2JobStore(path)
    await jobs.initialize()
    index = InMemoryMemoryV2Index()
    return Env(store=store, jobs=jobs, index=index, service=MemoryV2Service(store, index))


def _model_config(provider: str) -> ModelConfig:
    return ModelConfig(
        provider=provider, model_name=f"{provider}-model", api_key="unit-test-key",
        base_url="http://localhost:1", temperature=0.0,
    )


def _roles(*, fallback: bool = False) -> MemoryModelRoles:
    return MemoryModelRoles(
        primary=_model_config("senseaudio"),
        fallback=_model_config("qwen") if fallback else None,
    )


def _run_events() -> list[SessionEvent]:
    """本轮事件：投影会给它们发别名 `e1`（用户消息）/ `e2`（助手回复）。

    **候选证据必须引这两个别名**，不能引 `u:1` / `m:1`：模型只看得到载荷，而载荷里装的
    就是别名（T6b 修的 P0）。用真 id 写用例等于在测一个模型到不了的世界。
    """
    return [
        SessionEvent(event_id="u:1", seq=1, type=USER_MESSAGE, session_id="session-1",
                     run_id="run-1", data={"content": "请以后都用 pnpm 装依赖"}),
        SessionEvent(event_id="m:1", seq=2, type=MODEL_COMPLETED, session_id="session-1",
                     run_id="run-1", data={"content": "好的"}),
    ]


def _candidate(**overrides) -> dict:
    raw: dict = {
        "kind": "semantic",
        "tier": "collection",
        "scope": "user_global",
        "content": "用户偏好用 pnpm 安装依赖",
        "payload": {"kind": "semantic", "subject": "包管理器", "fact": "偏好 pnpm",
                    "category": "preference"},
        "importance": 0.8,
        "strength": 0.9,
        "evidence": [{"event_id": "e1", "role": "user", "excerpt": "请用 pnpm"}],
        "sensitivity": "ordinary",
    }
    raw.update(overrides)
    return raw


def _content(**overrides) -> dict:
    """`AdjudicationResult.result` 要的是**不含** `sensitivity` 的内容字段（§6.3）。"""
    raw = _candidate(**overrides)
    return {key: value for key, value in raw.items() if key != "sensitivity"}


def _formation_candidates(*candidates: dict) -> str:
    return json.dumps(
        {"decision": "CANDIDATES", "candidates": list(candidates), "skip_reason": None},
        ensure_ascii=False)


def _formation_no_memory(reason: str = "no_durable_value") -> str:
    return json.dumps(
        {"decision": "NO_MEMORY", "candidates": [], "skip_reason": reason}, ensure_ascii=False)


def _adjudication(*results: dict) -> str:
    return json.dumps({"results": list(results)}, ensure_ascii=False)


def _add(**overrides) -> dict:
    return {"action": "ADD", "target_memory_id": None, "reason_code": "durable_new",
            "result": _content(**overrides)}


def _update(target: str, **overrides) -> dict:
    return {"action": "UPDATE", "target_memory_id": target,
            "reason_code": "enrich_existing", "result": _content(**overrides)}


def _invalidate(target: str) -> dict:
    return {"action": "INVALIDATE", "target_memory_id": target,
            "reason_code": "contradicts_existing", "result": None}


def _noop() -> dict:
    return {"action": "NOOP", "target_memory_id": None, "reason_code": "duplicate",
            "result": None}


async def _claimed(
    env: Env, *, key: str = "run-1", trusted: TrustedMemoryIdentity = USER_A,
    worker_id: str = "worker-1",
) -> MemoryFormationJob:
    await env.jobs.enqueue(idempotency_key=key, trusted=trusted, session_id="session-1")
    job = await env.jobs.claim(worker_id=worker_id)
    assert job is not None, "freshly enqueued job must be claimable"
    return job


def _executor(
    env: Env, invoker, *, writer=None, limits: MemoryBudgetLimits | None = None,
    clock=None,
) -> MemoryJobExecutor:
    values: dict = {
        "jobs": env.jobs,
        "writer": writer if writer is not None else env.service,
        "searcher": env.service,
        "invoker": invoker,
    }
    if limits is not None:
        values["limits"] = limits
    if clock is not None:
        values["clock"] = clock
    return MemoryJobExecutor(**values)


async def _run(env: Env, invoker, *, job: MemoryFormationJob | None = None, **kwargs):
    """跑一次执行器；`_fallback` 控制角色表（默认只有 primary）。

    先用 `_executor` 收掉 `limits` / `clock` / `writer`，再交给 `run` —— 顺序不能反：
    `**kwargs` 会把剩下的键原样传给构造函数，多一个键就是一次 TypeError。
    """
    fallback = kwargs.pop("_fallback", False)
    sink = kwargs.pop("sink", None)
    job = job if job is not None else await _claimed(env)
    sink = sink or RecordingSink()
    result = await _executor(env, invoker, **kwargs).run(
        job, worker_id="worker-1", run_events=_run_events(),
        roles=_roles(fallback=fallback), sink=sink)
    return job, result, sink


async def _active(env: Env, trusted: TrustedMemoryIdentity = USER_A) -> list:
    return await env.store.list_active(trusted, scope=MemoryScope.USER_GLOBAL, limit=50)


# --------------------------------------------------------------------------------------
# 第 1 组：安静成功（AC2 / R12）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_memory_completes_quietly(env: Env) -> None:
    """AC2：`NO_MEMORY` ⇒ 无记录、无事件、终态成功。"""
    invoker = FakeInvoker(formation=[_formation_no_memory()], adjudication=[])
    job, result, sink = await _run(env, invoker)

    assert result is not None
    assert result.stage is MemoryJobStage.COMPLETED
    assert result.outcome is MemoryJobOutcome.NO_WRITE
    assert result.reason == "no_durable_value"
    assert result.written == ()
    assert sink.events == []
    assert await _active(env) == []
    stored = await env.jobs.get(job.job_id)
    assert stored.stage is MemoryJobStage.COMPLETED
    assert stored.outcome is MemoryJobOutcome.NO_WRITE
    assert [c.stage for c in invoker.calls] == [MemoryModelStage.FORMATION]


@pytest.mark.asyncio
async def test_every_noop_completes_quietly(env: Env) -> None:
    """AC2：全部 `NOOP` ⇒ 同样安静（不装成错误，也不装成"写过了"）。"""
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_noop())])
    _job, result, sink = await _run(env, invoker)

    assert result is not None
    assert result.stage is MemoryJobStage.COMPLETED
    assert result.outcome is MemoryJobOutcome.NO_WRITE
    assert result.written == ()
    assert sink.events == []
    assert await _active(env) == []


@pytest.mark.asyncio
async def test_policy_rejections_do_not_reach_adjudication(env: Env) -> None:
    """被政策拒掉的候选**不进**裁决：它们不该占用模型调用，也不该被喂回模型。"""
    secret_candidate = _candidate(evidence=[{"event_id": "e1", "role": "user",
                                             "excerpt": f"我的 key 是 {SECRET}"}])
    invoker = FakeInvoker(
        formation=[_formation_candidates(secret_candidate)], adjudication=[])
    _job, result, sink = await _run(env, invoker)

    assert result is not None
    assert result.stage is MemoryJobStage.COMPLETED
    assert result.outcome is MemoryJobOutcome.NO_WRITE
    assert [c.stage for c in invoker.calls] == [MemoryModelStage.FORMATION]
    assert sink.events == []
    assert await _active(env) == []


@pytest.mark.asyncio
async def test_a_candidate_with_unresolvable_evidence_is_not_written(env: Env) -> None:
    """AC3 的 unsupported-source：证据指不到本轮的任何一个事件 ⇒ 该候选不被采用。"""
    ghost = _candidate(
        evidence=[{"event_id": "ghost:1", "role": "user", "excerpt": "谁说的？"}],
        content="用户偏好某件没人说过的事")
    invoker = FakeInvoker(formation=[_formation_candidates(ghost)], adjudication=[])
    _job, result, _sink = await _run(env, invoker)

    assert result is not None
    assert result.outcome is MemoryJobOutcome.NO_WRITE
    assert [c.stage for c in invoker.calls] == [MemoryModelStage.FORMATION]
    assert await _active(env) == []


# --------------------------------------------------------------------------------------
# 第 2 组：写入与事件（R12 / §6.5 / §5.4）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_add_writes_one_record_and_emits_updated(env: Env) -> None:
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    job, result, sink = await _run(env, invoker)

    assert result is not None
    assert result.stage is MemoryJobStage.COMPLETED
    assert result.outcome is MemoryJobOutcome.COMMITTED
    assert len(result.written) == 1
    record = result.written[0]
    assert record.status is MemoryStatus.ACTIVE

    stored = await env.store.get(record.id, USER_A)
    # 身份与 provenance 由**运行时**补齐，不是模型给的
    assert (stored.tenant_id, stored.user_id) == ("tenant-a", "user-a")
    assert stored.source_type.value == "automatic"
    assert stored.source_session_id == "session-1"
    assert stored.source_event_ids == ["u:1"]
    assert stored.evidence[0].hash == hashlib.sha256("请用 pnpm".encode()).hexdigest()

    assert sink.types() == [MEMORY_UPDATED]
    _type, data, run_id = sink.events[0]
    assert set(data) == {"count", "memory_ids", "actions", "job_id"}, "事件不得带内容"
    assert data == {"count": 1, "memory_ids": [record.id], "actions": {"ADD": 1},
                    "job_id": job.job_id}
    assert run_id == "run-1"


@pytest.mark.asyncio
async def test_repeated_evidence_references_are_deduplicated_but_all_kept(env: Env) -> None:
    """§6.1：`source_event_ids` 是**去重保序**的引用集，`evidence` 保留每次引用。

    模型为同一个事件给两条证据是合法的（一条原文、一条补充说明），但 provenance 是
    "这条记忆依据了哪些事件"——同一事件出现两次不是两种依据。两者语义不同，所以一个
    去重、一个不去重；把它们合成一个列表会让"依据了几件事"随模型的啰嗦程度变化。

    证据由**裁决结果**供给（`_apply_one` 走的是 `verdict.result`），所以重复项要放在
    `_add(...)` 里——放在候选上只影响政策，进不了落盘的那条记录。
    """
    duplicated = [
        {"event_id": "e1", "role": "user", "excerpt": "请用 pnpm"},
        {"event_id": "e1", "role": "user", "excerpt": "以后都用 pnpm"},
    ]
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())],
        adjudication=[_adjudication(_add(evidence=duplicated))])
    _job, result, _sink = await _run(env, invoker)

    assert result is not None and len(result.written) == 1
    stored = await env.store.get(result.written[0].id, USER_A)
    assert stored.source_event_ids == ["u:1"]
    assert [item.excerpt for item in stored.evidence] == ["请用 pnpm", "以后都用 pnpm"]
    assert [item.hash for item in stored.evidence] == [
        hashlib.sha256("请用 pnpm".encode()).hexdigest(),
        hashlib.sha256("以后都用 pnpm".encode()).hexdigest(),
    ]


@pytest.mark.asyncio
async def test_an_update_supersedes_the_target(env: Env) -> None:
    existing = await env.service.create(make_draft(content="用户偏好 npm"), USER_A)
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())],
        adjudication=[_adjudication(_update(existing.id, content="用户偏好 pnpm"))])
    _job, result, sink = await _run(env, invoker)

    assert result is not None
    assert result.outcome is MemoryJobOutcome.COMMITTED
    assert len(result.written) == 1
    new = result.written[0]
    assert (new.root_id, new.version) == (existing.root_id, 2)
    assert new.status is MemoryStatus.ACTIVE
    assert (await env.store.get(existing.id, USER_A)).status is MemoryStatus.SUPERSEDED
    assert sink.types() == [MEMORY_UPDATED]
    assert sink.events[0][1]["actions"] == {"UPDATE": 1}
    assert len(await _active(env)) == 1


@pytest.mark.asyncio
async def test_an_invalidate_keeps_the_content_but_drops_it_from_active(env: Env) -> None:
    existing = await env.service.create(make_draft(content="用户在用 npm"), USER_A)
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())],
        adjudication=[_adjudication(_invalidate(existing.id))])
    _job, result, sink = await _run(env, invoker)

    assert result is not None
    assert result.outcome is MemoryJobOutcome.COMMITTED
    stored = await env.store.get(existing.id, USER_A)
    assert stored.status is MemoryStatus.INVALIDATED
    assert stored.content == "用户在用 npm"          # §5.4.2：内容保留
    assert await _active(env) == []
    assert sink.events[0][1]["actions"] == {"INVALIDATE": 1}


@pytest.mark.asyncio
async def test_an_action_naming_someone_elses_memory_is_discarded(env: Env) -> None:
    """§6.3 末句：目标归属由运行时校验。别人的 id 不是"失败"，是**该条动作被丢弃**。"""
    stranger = await env.service.create(
        make_draft(content="别人的记忆"),
        TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-b"))
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())],
        adjudication=[_adjudication(_update(stranger.id, content="改掉别人的"))])
    _job, result, sink = await _run(env, invoker)

    assert result is not None
    assert result.outcome is MemoryJobOutcome.NO_WRITE
    assert result.written == ()
    assert sink.events == []
    assert (await env.store.get(stranger.id, TrustedMemoryIdentity(
        tenant_id="tenant-a", user_id="user-b"))).content == "别人的记忆"


@pytest.mark.asyncio
async def test_a_discarded_action_does_not_take_down_the_rest_of_the_batch(env: Env) -> None:
    """§6.3 末句的**逐条隔离**：同批里一条越权 UPDATE 被丢弃，合法的 ADD 照常写入。

    "丢弃一条"与"整批失败"必须是两件可区分的事：前者是模型选错了目标（运行时校验挡下），
    后者是存储/索引真坏了。若一条越权动作能让整批回滚，一次模型误判就会连累同批里
    完全合法的记忆——那是把"模型犯错"放大成"这次作业白跑"。
    """
    stranger = await env.service.create(
        make_draft(content="别人的记忆"),
        TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-b"))
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate(), _candidate(content="第二条候选"))],
        adjudication=[_adjudication(_update(stranger.id, content="改掉别人的"),
                                    _add(content="用户偏好用 pnpm 装依赖"))])
    job, result, sink = await _run(env, invoker)

    assert result is not None
    assert result.outcome is MemoryJobOutcome.COMMITTED
    assert len(result.written) == 1
    assert result.written[0].content == "用户偏好用 pnpm 装依赖"
    assert sink.types() == [MEMORY_UPDATED]
    assert sink.events[0][1]["actions"] == {"ADD": 1}
    assert (await env.jobs.get(job.job_id)).state["discarded"] == {"target_unauthorized": 1}
    assert (await env.store.get(stranger.id, TrustedMemoryIdentity(
        tenant_id="tenant-a", user_id="user-b"))).content == "别人的记忆"


# --------------------------------------------------------------------------------------
# 第 3 组：重试、切换与预算（R9 / R10 / AC4 / AC5）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_primary_failures_exhaust_three_attempts_then_use_fallback(env: Env) -> None:
    """AC4：primary 恰好 3 次（初次 + 2 重试），然后 fallback 的初次成功。"""
    invoker = FakeInvoker(
        formation=[TimeoutError(), TimeoutError(), TimeoutError(),
                   _formation_candidates(_candidate())],
        adjudication=[_adjudication(_add())])
    _job, result, _sink = await _run(env, invoker, _fallback=True)

    assert invoker.attempts(MemoryModelStage.FORMATION) == [
        ("primary", 1), ("primary", 2), ("primary", 3), ("fallback", 1)]
    assert result is not None
    assert result.outcome is MemoryJobOutcome.COMMITTED
    assert result.fallback_used is True
    # 整作业的调用计数（formation 4 次 + 裁决 1 次），不是 formation 单独的 4 次
    assert result.attempts == 5


@pytest.mark.asyncio
async def test_the_fallback_gets_at_most_two_attempts(env: Env) -> None:
    """AC4 的另一半：fallback 用尽（1 次 + 1 次重试）⇒ 一个终态降级、零写入。"""
    invoker = FakeInvoker(
        formation=[TimeoutError()] * 5, adjudication=[])
    job, result, sink = await _run(env, invoker, _fallback=True)

    assert invoker.attempts(MemoryModelStage.FORMATION) == [
        ("primary", 1), ("primary", 2), ("primary", 3), ("fallback", 1), ("fallback", 2)]
    assert result is not None
    assert result.stage is MemoryJobStage.DEGRADED
    assert result.reason == DegradedReason.TRANSIENT_EXHAUSTED.value
    assert result.written == ()
    assert await _active(env) == []
    assert (await env.jobs.get(job.job_id)).stage is MemoryJobStage.DEGRADED
    assert sink.types() == [MEMORY_DEGRADED]


@pytest.mark.asyncio
async def test_without_a_fallback_three_primary_attempts_end_the_job(env: Env) -> None:
    invoker = FakeInvoker(formation=[TimeoutError()] * 3, adjudication=[])
    _job, result, _sink = await _run(env, invoker)

    assert invoker.attempts(MemoryModelStage.FORMATION) == [
        ("primary", 1), ("primary", 2), ("primary", 3)]
    assert result is not None
    assert result.reason == DegradedReason.TRANSIENT_EXHAUSTED.value
    assert result.fallback_used is False


@pytest.mark.asyncio
async def test_a_non_transient_failure_is_neither_retried_nor_switched(env: Env) -> None:
    """AC5：401 一次就停——既不给 primary 重试，也不切 fallback。"""
    invoker = FakeInvoker(formation=[_AuthFailure("bad key")], adjudication=[])
    _job, result, sink = await _run(env, invoker, _fallback=True)

    assert invoker.attempts(MemoryModelStage.FORMATION) == [("primary", 1)]
    assert result is not None
    assert result.stage is MemoryJobStage.DEGRADED
    assert result.reason == DegradedReason.PROVIDER_ERROR.value
    assert result.written == ()
    assert sink.types() == [MEMORY_DEGRADED]


@pytest.mark.asyncio
async def test_a_schema_failure_is_a_failed_attempt_that_stops(env: Env) -> None:
    """R3 + R9：解析失败是**失败尝试**（不是 abstention），且不重试。"""
    invoker = FakeInvoker(formation=["这不是 JSON", _formation_no_memory()], adjudication=[])
    _job, result, _sink = await _run(env, invoker, _fallback=True)

    assert invoker.attempts(MemoryModelStage.FORMATION) == [("primary", 1)]
    assert result is not None
    assert result.stage is MemoryJobStage.DEGRADED
    assert result.reason == DegradedReason.INVALID_MODEL_OUTPUT.value
    assert result.outcome is None


@pytest.mark.asyncio
async def test_an_adjudication_batch_of_the_wrong_size_degrades(env: Env) -> None:
    """裁决条数必须与候选数一致：不符是契约失败，不是"少写几条"。"""
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate(), _candidate(content="第二条"))],
        adjudication=[_adjudication(_add())])
    _job, result, _sink = await _run(env, invoker)

    assert result is not None
    assert result.stage is MemoryJobStage.DEGRADED
    assert result.reason == DegradedReason.ADJUDICATION_INCOMPLETE.value
    assert await _active(env) == []


@pytest.mark.asyncio
async def test_the_call_budget_stops_the_job_before_any_write(env: Env) -> None:
    """R10：调用次数用尽 ⇒ 一个终态降级、零写入（在这里：formation 用掉唯一一次）。"""
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    _job, result, _sink = await _run(
        env, invoker, limits=MemoryBudgetLimits(max_calls=1))

    assert result is not None
    assert result.stage is MemoryJobStage.DEGRADED
    assert result.reason == DegradedReason.CALLS.value
    assert result.written == ()
    assert await _active(env) == []


@pytest.mark.asyncio
async def test_the_wall_clock_budget_stops_the_job(env: Env) -> None:
    """R10：墙钟用尽 ⇒ `deadline`。时钟由构造方注入（T5 的账本约定）。"""
    ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0])

    def clock() -> float:
        return next(ticks, 999.0)

    invoker = FakeInvoker(formation=[_formation_candidates(_candidate())], adjudication=[])
    _job, result, _sink = await _run(
        env, invoker, limits=MemoryBudgetLimits(timeout_seconds=120.0), clock=clock)

    assert result is not None
    assert result.stage is MemoryJobStage.DEGRADED
    assert result.reason == DegradedReason.DEADLINE.value
    assert result.written == ()


@pytest.mark.asyncio
async def test_a_missing_primary_role_degrades_without_calling_anything(env: Env) -> None:
    """R9 的前置条件（T5 的账本契约第 1 条）：没有 primary 就不该开跑，更不该写。"""
    invoker = FakeInvoker(formation=[], adjudication=[])
    job = await _claimed(env)
    sink = RecordingSink()
    result = await _executor(env, invoker).run(
        job, worker_id="worker-1", run_events=_run_events(),
        roles=MemoryModelRoles(primary=None, fallback=None), sink=sink)

    assert invoker.calls == []
    assert result is not None
    assert result.stage is MemoryJobStage.DEGRADED
    assert result.reason == DegradedReason.NO_PRIMARY_MODEL.value
    assert await _active(env) == []
    assert sink.types() == [MEMORY_DEGRADED]


# --------------------------------------------------------------------------------------
# 第 4 组：事务、重启与重放（AC6 / AC7）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_apply_leaves_zero_records_and_a_degraded_job(env: Env) -> None:
    """AC6 的"提交前"窗口：副作用抛异常 ⇒ 整体回滚 + 一次降级终态（零记录）。"""

    class ExplodingWriter:
        async def create_in(self, connection, draft, trusted):
            raise RuntimeError("index side down")

        async def update_in(self, connection, previous_id, draft, trusted):
            raise RuntimeError("unreachable")

        async def invalidate_in(self, connection, memory_id, trusted):
            raise RuntimeError("unreachable")

    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    job, result, _sink = await _run(env, invoker, writer=ExplodingWriter())

    assert result is not None
    assert result.stage is MemoryJobStage.DEGRADED
    assert result.reason == DegradedReason.APPLY_FAILED.value
    assert result.written == ()
    assert await _active(env) == []
    assert (await env.jobs.get(job.job_id)).stage is MemoryJobStage.DEGRADED


@pytest.mark.asyncio
async def test_a_committed_job_is_never_replayed(env: Env) -> None:
    """AC7：已终结的 job 再跑一次 ⇒ 零新记录、零新事件、零模型调用。"""
    first = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    job, result, sink = await _run(env, first)
    assert result is not None and result.outcome is MemoryJobOutcome.COMMITTED

    second = FakeInvoker(formation=[], adjudication=[])
    replay = await _executor(env, second).run(
        job, worker_id="worker-1", run_events=_run_events(), roles=_roles(),
        sink=(replay_sink := RecordingSink()))

    assert replay is not None
    assert replay.written == ()
    assert second.calls == []
    assert replay_sink.events == []
    assert len(await _active(env)) == 1
    assert sink.types() == [MEMORY_UPDATED]


@pytest.mark.asyncio
async def test_a_terminal_job_is_not_claimable_after_a_restart(env: Env) -> None:
    """AC6 的"已提交"窗口：重启后新 store 看不到可跑的活，记录仍是那一条。"""
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    _job, result, _sink = await _run(env, invoker)
    assert result is not None and result.outcome is MemoryJobOutcome.COMMITTED

    restarted = SqliteMemoryV2JobStore(env.jobs.database_path)
    assert await restarted.claim(worker_id="worker-2", now=T0 + timedelta(hours=1)) is None
    assert len(await _active(env)) == 1


@pytest.mark.asyncio
async def test_an_interrupted_job_recovers_to_exactly_one_outcome(env: Env) -> None:
    """AC6 的"formation 完成后"窗口：崩溃后 lease 到期被接手，收敛到一个终态、一条记录。"""

    class ExplodingWriter:
        async def create_in(self, connection, draft, trusted):
            raise RuntimeError("crash window")

        async def update_in(self, connection, previous_id, draft, trusted):
            raise RuntimeError("unreachable")

        async def invalidate_in(self, connection, memory_id, trusted):
            raise RuntimeError("unreachable")

    first_invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    job = await _claimed(env)
    crashed = await _executor(env, first_invoker, writer=ExplodingWriter()).run(
        job, worker_id="worker-1", run_events=_run_events(), roles=_roles(),
        sink=RecordingSink())
    assert crashed is not None and crashed.stage is MemoryJobStage.DEGRADED

    # 崩溃点更早的形态：job 停在非终态、lease 到期 ⇒ 另一个 worker 接手重跑。
    stalled = await env.jobs.enqueue(
        idempotency_key="run-2", trusted=USER_A, session_id="session-1")
    await env.jobs.claim(worker_id="dead-worker", now=T0)
    recovered = await env.jobs.claim(worker_id="worker-2", now=T0 + timedelta(hours=1))
    assert recovered is not None and recovered.job_id == stalled.job_id

    second_invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    sink = RecordingSink()
    result = await _executor(env, second_invoker).run(
        recovered, worker_id="worker-2", run_events=_run_events(), roles=_roles(), sink=sink)

    assert result is not None
    assert result.outcome is MemoryJobOutcome.COMMITTED
    assert len(await _active(env)) == 1
    assert sink.types() == [MEMORY_UPDATED]


@pytest.mark.asyncio
async def test_a_worker_without_the_lease_applies_nothing(env: Env) -> None:
    """单属主：没拿到 lease 的 worker 连副作用都不执行。"""
    job = await _claimed(env, worker_id="worker-1")
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    result = await _executor(env, invoker).run(
        job, worker_id="worker-2", run_events=_run_events(), roles=_roles(),
        sink=RecordingSink())

    assert result is None
    assert await _active(env) == []
    assert invoker.calls == []


# --------------------------------------------------------------------------------------
# 第 5 组：裁决看得到什么（§5.2.5 / R2）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adjudication_sees_bounded_relevant_active_memories(env: Env) -> None:
    # 进程内索引是**子串匹配**（`InMemoryMemoryV2Index` 的朴素语义，见它的 docstring）：
    # 命中条件是 `query in content`，所以让既有记忆的内容包含候选内容才能命中。检索语义
    # 本身属 MEM-V2-6，这里只钉"执行器把检索结果按最小可见面喂给了裁决"。
    content = "用户偏好用 pnpm 安装依赖"
    existing = await env.service.create(make_draft(content=content), USER_A)
    # 写记录与索引收敛是两件事（`MemoryV2Service.search` 刻意不顺手 flush）：这里显式
    # 扮演 relay 跑过一遍，否则检索看不到刚写的那条。
    await env.index.upsert(existing)
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())],
        adjudication=[_adjudication(_noop())])
    _job, result, _sink = await _run(env, invoker)

    assert result is not None
    adjudication_call = next(c for c in invoker.calls
                             if c.stage is MemoryModelStage.ADJUDICATION)
    memories = adjudication_call.payload["relevant_memories"]
    assert memories == [{"memory_id": existing.id, "kind": "semantic",
                         "scope": "user_global", "content": content}]
    assert "candidates" in adjudication_call.payload


@pytest.mark.asyncio
async def test_the_formation_call_carries_the_safe_projection(env: Env) -> None:
    invoker = FakeInvoker(formation=[_formation_no_memory()], adjudication=[])
    _job, _result, _sink = await _run(env, invoker)

    call = invoker.calls[0]
    assert call.stage is MemoryModelStage.FORMATION
    assert call.payload["current_run"] == [
        {"ref": "e1", "role": "user", "text": "请以后都用 pnpm 装依赖"},
        {"ref": "e2", "role": "assistant", "text": "好的"},
    ]
    # AC9 的另一半：真实事件 id 不进模型输入——模型只能引投影发给它的别名。
    assert "u:1" not in json.dumps(call.payload, ensure_ascii=False)
    assert call.max_output_tokens == 4000
    assert call.timeout_seconds > 0


# --------------------------------------------------------------------------------------
# 第 6 组：AC9 —— 秘密不出现在任何一条通道上
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_secret_never_reaches_the_model_input_record_or_event(env: Env) -> None:
    """AC9：用户消息与候选里的凭证，既进不了模型输入，也进不了记录与事件。"""
    secret_text = f"我的 key 是 {SECRET}，以后就用它"
    events = [
        SessionEvent(event_id="u:1", seq=1, type=USER_MESSAGE, session_id="session-1",
                     run_id="run-1", data={"content": secret_text}),
        SessionEvent(event_id="m:1", seq=2, type=MODEL_COMPLETED, session_id="session-1",
                     run_id="run-1", data={"content": "好的"}),
    ]
    secret_candidate = _candidate(
        content=f"用户的 API key 是 {SECRET}",
        evidence=[{"event_id": "e1", "role": "user", "excerpt": secret_text}])
    invoker = FakeInvoker(
        formation=[_formation_candidates(secret_candidate)], adjudication=[])
    job = await _claimed(env)
    sink = RecordingSink()
    result = await _executor(env, invoker).run(
        job, worker_id="worker-1", run_events=events, roles=_roles(), sink=sink)

    # 1) 模型输入里没有被切断的凭证
    assert invoker.calls, "formation must still be called"
    assert SECRET not in json.dumps(invoker.calls[0].payload, ensure_ascii=False)
    # 2) 零记录 ⇒ 凭证没有落盘
    assert result is not None and result.written == ()
    assert SECRET not in json.dumps(
        [record.model_dump() for record in await _active(env)], ensure_ascii=False)
    assert await _active(env) == []
    # 3) 事件里没有凭证
    assert SECRET not in json.dumps([data for _t, data, _r in sink.events], ensure_ascii=False)


@pytest.mark.asyncio
async def test_the_degraded_event_carries_only_stable_metadata(env: Env) -> None:
    """§6.5：`memory/degraded` 只带 stage / reason_code / job_id / 尝试数 / 是否用过备用。"""
    invoker = FakeInvoker(formation=[_AuthFailure("bad key")], adjudication=[])
    job, result, sink = await _run(env, invoker)

    assert result is not None
    assert sink.types() == [MEMORY_DEGRADED]
    _type, data, run_id = sink.events[0]
    assert set(data) == {"operation", "stage", "reason_code", "job_id", "attempts",
                         "fallback_used"}
    assert data["job_id"] == job.job_id
    assert data["reason_code"] == DegradedReason.PROVIDER_ERROR.value
    assert data["fallback_used"] is False
    assert run_id == "run-1"


# --------------------------------------------------------------------------------------
# 第 7 组：证据引用链（T6b 的 P0 修复）
# --------------------------------------------------------------------------------------
#
# 模型看得到的只有投影载荷，而载荷里的事件引用是**别名**（`e1`…）。这条链有三段，
# 任何一段断开都是一个静默的坏结局：政策段断了 ⇒ 生产路径上每条候选都落
# `unsupported_source`（自动记忆零写入）；落盘段断了 ⇒ 库里存着指向不存在事件的引用。
# 本组把三段各钉一次，并且用**真 UUID** 做事件 id——`u:1` 有被"别名恰好像真 id"蒙对的余地。


@pytest.mark.asyncio
async def test_a_well_cited_candidate_is_accepted_and_reaches_adjudication(env: Env) -> None:
    """P0 的核心断言：证据引投影发的别名 ⇒ 政策**接受**，并且真的走到裁决与落盘。

    修好之前这条会以 `rejected={"unsupported_source": 1}`、零裁决调用、零写入收场。
    有判别力的正是 `state` 里那两个计数——"没写成"这个结果本身不区分原因，
    而这套归因把它钉到具体那一条判据上。
    """
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    job, result, _sink = await _run(env, invoker)

    assert result is not None and result.outcome is MemoryJobOutcome.COMMITTED
    state = (await env.jobs.get(job.job_id)).state
    assert (state["accepted"], state["rejected"]) == (1, {})
    assert [c.stage for c in invoker.calls] == [
        MemoryModelStage.FORMATION, MemoryModelStage.ADJUDICATION]


@pytest.mark.asyncio
async def test_the_alias_is_translated_back_to_the_real_event_id_before_it_is_stored(
    env: Env,
) -> None:
    """落盘段的闭合：模型引 `e1`，写进记录的是**真实事件 id**。

    断言对象是 `source_event_ids`——§6.1 的 provenance。写成别名就是一条指向不存在事件的
    记忆：不可审计、不可撤回，而且读取侧不会有任何报错。
    """
    events = [
        SessionEvent(event_id=RUN_UUID, seq=1, type=USER_MESSAGE, session_id="session-1",
                     run_id="run-1", data={"content": "以后都用 pnpm"}),
        SessionEvent(event_id=REPLY_UUID, seq=2, type=MODEL_COMPLETED, session_id="session-1",
                     run_id="run-1", data={"content": "好的"}),
    ]
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())], adjudication=[_adjudication(_add())])
    job = await _claimed(env)
    result = await _executor(env, invoker).run(
        job, worker_id="worker-1", run_events=events, roles=_roles(), sink=RecordingSink())

    assert result is not None and result.outcome is MemoryJobOutcome.COMMITTED
    stored = await env.store.get(result.written[0].id, USER_A)
    assert stored.source_event_ids == [RUN_UUID]
    assert stored.evidence[0].hash == hashlib.sha256("请用 pnpm".encode()).hexdigest()
    formation_payload = json.dumps(invoker.calls[0].payload, ensure_ascii=False)
    assert RUN_UUID not in formation_payload
    assert "e1" in formation_payload


@pytest.mark.asyncio
async def test_a_candidate_citing_a_ref_the_projection_never_issued_is_rejected(env: Env) -> None:
    """引投影没发过的号（`e9`）与引真实 id（`u:1`）是同一件事：解析不到 ⇒ fail closed。

    只验"被拒"没有判别力（修好之前连合法候选也落同一个归因）；这里验的是**归因落在
    `unsupported_source` 上**，并且模型压根不该被请去裁决一条站不住的候选。
    """
    ghost = _candidate(evidence=[{"event_id": "e9", "role": "user", "excerpt": "查无此号"}],
                       content="用户偏好某件没人说过的事")
    invoker = FakeInvoker(formation=[_formation_candidates(ghost)], adjudication=[])
    job, result, _sink = await _run(env, invoker)

    assert result is not None
    assert result.outcome is MemoryJobOutcome.NO_WRITE
    assert [c.stage for c in invoker.calls] == [MemoryModelStage.FORMATION]
    assert (await env.jobs.get(job.job_id)).state["rejected"] == {"unsupported_source": 1}
    assert await _active(env) == []


@pytest.mark.asyncio
async def test_a_real_event_id_is_not_a_valid_citation_for_the_model(env: Env) -> None:
    """反向锁：模型**不能**靠真 id 引用（它压根看不到真 id），别名是唯一入口。

    这条防的是"为了让老用例继续过"而把两种键都收进键空间——那会让模型凭空拥有引用任意
    会话事件的能力，而它本来只该能引投影发给它的那几个。
    """
    real_id = _candidate(evidence=[{"event_id": "u:1", "role": "user", "excerpt": "请用 pnpm"}],
                         content="用户偏好用 pnpm 安装依赖")
    invoker = FakeInvoker(formation=[_formation_candidates(real_id)], adjudication=[])
    job, result, _sink = await _run(env, invoker)

    assert result is not None
    assert result.outcome is MemoryJobOutcome.NO_WRITE
    assert (await env.jobs.get(job.job_id)).state["rejected"] == {"unsupported_source": 1}


@pytest.mark.asyncio
async def test_a_verdict_whose_evidence_resolves_to_nothing_is_discarded(env: Env) -> None:
    """裁决阶段能重写证据（它看到的是候选的 JSON），重写成解析不到的引用**只废这一条**。

    §6.1 要求 `source_event_ids` 非空，所以这条动作不是"少写几个字段"而是不成立；
    但它不该连累同批里合法的写入（与越权 UPDATE 的逐条隔离同款），归因码也单独一串——
    `evidence_unresolved` 说的是"模型引了个不存在的 ref"，与版本冲突、越权都不是一回事。
    """
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate(), _candidate(content="第二条候选"))],
        adjudication=[_adjudication(
            _add(evidence=[{"event_id": "e404", "role": "user", "excerpt": "查无此号"}]),
            _add(content="用户偏好用 pnpm 装依赖"))])
    job, result, sink = await _run(env, invoker)

    assert result is not None
    assert result.outcome is MemoryJobOutcome.COMMITTED
    assert len(result.written) == 1
    assert sink.events[0][1]["actions"] == {"ADD": 1}
    assert (await env.jobs.get(job.job_id)).state["discarded"] == {"evidence_unresolved": 1}
