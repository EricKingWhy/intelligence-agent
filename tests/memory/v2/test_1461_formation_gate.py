"""#1461：formation 写路径纳入审批（BLOCKED 两段式处置通道）。

票面判据见设计 §8。这一组用例钉的是**后台提炼的写入有没有被人拦下**，不是模型提取质量，
所以真跑的部分是全部编排与落盘：

- `build_memory_v2_service` 装配出的 service（同一 `memory-v2.db` 里的 records / jobs /
  staged / blocked，加上 markdown 权威面）；
- `SqliteMemoryV2JobStore.commit_with_outcome` 的那笔真实终态事务（"blocked 行与 job 终态
  一起落地"是 AC7① 的机制根）；
- 真实 JSONL 会话日志（切片、投影别名、runner 的事件出口）；
- AC7① 的**真子进程 SIGKILL**（`_blocked_write_kill_child.py`，与
  `_protected_fact_extraction_kill_child.py` 同一形态）——门审条件 C1 不接受进程内异常模拟。

唯一的替身是模型：`FakeInvoker` 与既有 T6 契约用例同一套脚本（引用投影发出的 `e1`），
所以"候选能走到写入段"本身就证明切片、投影、契约校验、策略判定四段都真的跑过。

身份统一用 `tenant-a/user-a`：`notify_run_finished` 取的是上下文里的可信身份，所以 runner
那一条用例把上下文设成同一个值（用例结束复位），别的用例直接传 `TrustedMemoryIdentity`。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from agent_harness import cli
from agent_harness.config import Settings
from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.v2 import assembly
from agent_harness.memory.v2._sqlite import write_connection
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.executor import MemoryJobExecutor, MemoryJobResult
from agent_harness.memory.v2.jobs import (
    MemoryJobOutcome,
    MemoryJobStage,
    SqliteMemoryV2JobStore,
)
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import (
    MemoryDraftV2,
    MemoryScope,
    MemoryStatus,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.write_approval import (
    BLOCKED,
    BLOCKED_REASON,
    DISCARDED,
    DRIFT_PREFIX,
    PROMOTED,
    BlockedWrite,
    BlockedWriteStore,
    _identity,
)
from agent_harness.model.config import ModelConfig
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_STARTED,
    USER_MESSAGE,
    Session,
)
from agent_harness.session.event import MEMORY_HUMAN_BLOCKED, MEMORY_UPDATED
from agent_harness.session.store import JsonlSessionStore
from tests.memory.v2._records import make_draft
from tests.memory.v2.test_v2_executor import (
    FakeInvoker,
    RecordingSink,
    _add,
    _adjudication,
    _candidate,
    _formation_candidates,
    _formation_no_memory,
    _update,
)
from tests.web import test_memory_api as _shared
from tests.web.test_memory_api import _ALICE, _BOB, _auth

# pytest 按**模块属性名**注册 fixture：赋值一次即复用真 app 装配（真 JWT、真 wiring）。
memory_app = _shared.memory_app

USER_A = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a")
PROJECT_X = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a", project_id="project-x")
LOCAL = TrustedMemoryIdentity(tenant_id="local", user_id="local")

SESSION = "session-1"
RUN = "run-1"
WORKER = "worker-1461"
USER_TEXT = "请以后都用 pnpm 装依赖"
CONTENT = "用户偏好用 pnpm 安装依赖"
OLD_CONTENT = "用户偏好用 pnpm 安装依赖"
NEW_CONTENT = "用户要求用 pnpm 且锁定版本"

_ROLES = MemoryModelRoles(
    primary=ModelConfig(
        provider="senseaudio", model_name="unit-model", api_key="unit-test-key",
        base_url="http://localhost:1", temperature=0.0,
    ),
    fallback=None,
)


# --------------------------------------------------------------------------------------
# 环境：真装配（SQLite + markdown 权威面 + staged/blocked 同库）
# --------------------------------------------------------------------------------------


@dataclass
class Gate:
    settings: Settings
    database: Path
    service: MemoryV2Service
    jobs: SqliteMemoryV2JobStore
    sessions: JsonlSessionStore


@pytest_asyncio.fixture
async def gate(tmp_path) -> Gate:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path / "workspace"), model_api_key="sk-test")
    service = await assembly.build_memory_v2_service(settings)
    database = Path(settings.workspace_dir) / assembly.MEMORY_V2_DATABASE_NAME
    jobs = SqliteMemoryV2JobStore(database)
    await jobs.initialize()
    return Gate(settings=settings, database=database, service=service, jobs=jobs,
                sessions=JsonlSessionStore(tmp_path / "sessions"))


def _session(gate: Gate, session_id: str = SESSION) -> Session:
    """按盘上现状打开会话：seq 计数器要对得上，否则第二条 append 会撞号。"""
    return Session(session_id, gate.sessions, events=gate.sessions.read_events(session_id))


def _seed_run(
    gate: Gate, *, session_id: str = SESSION, run_id: str = RUN, user_text: str = USER_TEXT,
) -> int:
    """写一轮，顺序与生产 `_drive` 逐条一致（user 消息**不带** run_id）。"""
    session = _session(gate, session_id)
    mark = session.mark()
    session.append(USER_MESSAGE, {"content": user_text})
    session.append(RUN_STARTED, {"turn_index": 1}, run_id=run_id)
    session.append(MODEL_COMPLETED, {"content": "好的"}, run_id=run_id)
    return mark


async def _job(
    gate: Gate, *, key: str = f"memory-v2:{RUN}", trusted: TrustedMemoryIdentity = USER_A,
    session_id: str = SESSION, run_id: str = RUN,
):
    await gate.jobs.enqueue(
        idempotency_key=key, trusted=trusted, session_id=session_id, run_id=run_id)
    job = await gate.jobs.claim(worker_id=WORKER)
    assert job is not None, "freshly enqueued job must be claimable"
    return job


def _scripted(model=None) -> FakeInvoker:
    """一候选 + 一次 ADD——两段各一份新的队列（队列是消费型的）。"""
    return model or FakeInvoker(
        formation=[_formation_candidates(_candidate())],
        adjudication=[_adjudication(_add())],
    )


async def _run(
    gate: Gate, model, *, job=None, write_approval: bool = True,
    sink: RecordingSink | None = None,
) -> tuple[object, MemoryJobResult | None, RecordingSink]:
    job = job if job is not None else await _job(gate)
    sink = sink if sink is not None else RecordingSink()
    executor = MemoryJobExecutor(
        jobs=gate.jobs, writer=gate.service, searcher=gate.service,
        invoker=model, session_store=gate.sessions, blocked=gate.service.blocked,
    )
    result = await executor.run(
        job, worker_id=WORKER, run_events=gate.sessions.read_events(job.session_id),
        roles=_ROLES, sink=sink, write_approval=write_approval,
    )
    return job, result, sink


async def _blocked(gate: Gate) -> list[BlockedWrite]:
    return await gate.service.blocked.list_for(USER_A.tenant_id, USER_A.user_id)


async def _active(gate: Gate, trusted: TrustedMemoryIdentity = USER_A) -> list:
    return await gate.service.list_active(trusted, scope=MemoryScope.USER_GLOBAL, limit=50)


def _memory_file(gate: Gate) -> Path:
    return Path(gate.settings.workspace_dir) / "memories" / "MEMORY.md"


def _record_count(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM memory_v2_records").fetchone()[0])
    finally:
        connection.close()


async def _insert_blocked_row(
    blocked: BlockedWriteStore, *, trusted: TrustedMemoryIdentity, project_id: str | None = None,
    content: str = CONTENT,
) -> str:
    """按真实 store 的写入段插一行（CLI/Web 两侧只验**处置面**，不重跑整条提炼）。"""
    scope = MemoryScope.PROJECT if project_id is not None else MemoryScope.USER_GLOBAL
    payload = {"actions": [{
        "action": "add", "scope": scope.value,
        "draft": make_draft(content=content, scope=scope, project_id=project_id).model_dump(
            mode="json"),
        "pin": None,
    }]}
    async with write_connection(blocked.database_path) as connection:
        blocked_id = await blocked.insert_in(
            connection, job_id="job-manual", identity=_identity(trusted),
            project_id=project_id, payload=payload)
    assert blocked_id is not None, "the manual row must be unique inside this workspace"
    return blocked_id


# --------------------------------------------------------------------------------------
# AC1：门开着 ⇒ 零记录、零 markdown 变更、零 staged，意图整批落 blocked
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac1_gate_on_stages_the_intent_without_writing_any_record(gate: Gate) -> None:
    seeded = await gate.service.create(make_draft(content="用户的编辑器是 Vim"), USER_A)
    markdown = _memory_file(gate)
    bytes_before = markdown.read_bytes()
    staged_before = await gate.service.staged.pending(USER_A)
    _seed_run(gate)

    job, result, sink = await _run(gate, _scripted())

    assert result is not None
    # 具体值：终态 stage 仍是 COMPLETED，语义由 outcome 区分（jobs.MemoryJobOutcome 的注释）
    assert (result.outcome, result.stage) == (
        MemoryJobOutcome.BLOCKED, MemoryJobStage.COMPLETED)
    assert result.reason == BLOCKED_REASON
    assert result.written == ()
    assert [record.content for record in await _active(gate)] == [seeded.content]
    assert _record_count(gate.database) == 1
    assert markdown.read_bytes() == bytes_before, "markdown 权威面不得被拦下的写入改动"
    assert await gate.service.staged.pending(USER_A) == staged_before == []
    # 执行器一条记录都没写 ⇒ 不发 memory/updated（人面事件归 runner 的 BLOCKED 分支）
    assert sink.types() == []

    [row] = await _blocked(gate)
    assert (row.job_id, row.origin, row.reason, row.status) == (
        job.job_id, "background_review", BLOCKED_REASON, BLOCKED)
    assert (row.project_id, row.identity) == (None, _identity(USER_A))
    assert list(row.payload) == ["actions"]
    [item] = row.payload["actions"]
    assert set(item) == {"action", "scope", "draft", "pin"}
    assert (item["action"], item["scope"], item["pin"]) == ("add", "user_global", None)
    assert item["draft"]["content"] == CONTENT

    persisted = await gate.jobs.get(job.job_id)
    assert (persisted.stage, persisted.outcome, persisted.reason) == (
        MemoryJobStage.COMPLETED, MemoryJobOutcome.BLOCKED, BLOCKED_REASON)
    assert persisted.state["blocked_ids"] == [row.id]
    assert persisted.state["memory_ids"] == []


@pytest.mark.asyncio
async def test_ac1_runner_reports_human_blocked_with_counts_only(
    tmp_path, monkeypatch,
) -> None:
    """经 runner 跑：事件出口收到 `memory/human_blocked`，载荷只有 count/blocked_ids/job_id。"""
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session(SESSION, sessions, events=[])
    mark = session.mark()
    session.append(USER_MESSAGE, {"content": USER_TEXT})
    session.append(RUN_STARTED, {"turn_index": 1}, run_id=RUN)
    session.append(MODEL_COMPLETED, {"content": "好的"}, run_id=RUN)

    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path / "workspace"), model_api_key="sk-test")
    review = _scripted()
    monkeypatch.setattr(assembly, "ChatModelInvoker", lambda *args, **kwargs: review)
    runner = await assembly.build_memory_formation(
        settings, sessions=sessions, roles=_ROLES)
    assert runner is not None, "显式给了角色，装配期闸门不该返回 None"
    assert runner.memory_v2 is not None

    # gate 状态是**持久设置**（runner 每个 job 从库里读），不是构造参数。
    await runner.memory_v2.update_settings(USER_A, write_approval=True)
    token = set_identity_context(
        IdentityContext(USER_A.tenant_id, USER_A.user_id, ["user", "session"]))
    try:
        job = await runner.notify_run_finished(
            session_id=SESSION, run_id=RUN, terminal_status="completed",
            events=sessions.read_events(SESSION)[mark:])
        assert job is not None
        await runner.drain()
    finally:
        identity_context_var.reset(token)
        await runner.aclose()

    [event] = [e for e in sessions.read_events(SESSION) if e.type == MEMORY_HUMAN_BLOCKED]
    assert set(event.data) == {"count", "blocked_ids", "job_id"}
    assert event.data["count"] == 1 and event.data["job_id"] == job.job_id
    [blocked_id] = event.data["blocked_ids"]
    assert MEMORY_UPDATED not in [e.type for e in sessions.read_events(SESSION)]

    jobs = SqliteMemoryV2JobStore(Path(settings.workspace_dir) / assembly.MEMORY_V2_DATABASE_NAME)
    await jobs.initialize()
    persisted = await jobs.get(job.job_id)
    assert (persisted.stage, persisted.outcome) == (
        MemoryJobStage.COMPLETED, MemoryJobOutcome.BLOCKED)
    rows = await runner.memory_v2.blocked.list_for(USER_A.tenant_id, USER_A.user_id)
    assert [row.id for row in rows] == [blocked_id]


# --------------------------------------------------------------------------------------
# AC2：门开着但没什么可拦 ⇒ 安静成功（走 `_NothingToApply` 既有路径）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_no_memory_and_a_fully_discarded_batch_stay_quiet(gate: Gate) -> None:
    _seed_run(gate)
    _job1, result, sink = await _run(
        gate, FakeInvoker(formation=[_formation_no_memory()], adjudication=[]))
    assert result is not None
    assert (result.outcome, result.stage) == (
        MemoryJobOutcome.NO_WRITE, MemoryJobStage.COMPLETED)
    assert await _blocked(gate) == [] and sink.types() == []

    # 第二例：裁决指向一条本次不可见的目标 ⇒ 逐条丢弃 ⇒ 什么都没有可拦
    _seed_run(gate, session_id="session-2", run_id="run-2")
    second = await _job(
        gate, key="memory-v2:run-2", session_id="session-2", run_id="run-2")
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())],
        adjudication=[_adjudication(_update("mem_never_seen", content="换一条"))])
    _job2, result2, _sink2 = await _run(gate, invoker, job=second)

    assert result2 is not None
    assert (result2.outcome, result2.stage) == (
        MemoryJobOutcome.NO_WRITE, MemoryJobStage.COMPLETED)
    assert result2.written == ()
    assert await _blocked(gate) == []
    assert (await gate.jobs.get(second.job_id)).state["discarded"] == {
        "target_unauthorized": 1}


# --------------------------------------------------------------------------------------
# AC3：门关着 ⇒ 与基线同形的直落盘
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac3_gate_off_commits_like_the_baseline(gate: Gate) -> None:
    _seed_run(gate)

    _job1, result, sink = await _run(gate, _scripted(), write_approval=False)

    assert result is not None
    assert (result.outcome, result.stage) == (
        MemoryJobOutcome.COMMITTED, MemoryJobStage.COMPLETED)
    assert [record.content for record in result.written] == [CONTENT]
    assert sink.types() == [MEMORY_UPDATED]
    assert set(sink.events[0][1]) == {"count", "memory_ids", "actions", "job_id"}
    assert [record.content for record in await _active(gate)] == [CONTENT]
    assert CONTENT in _memory_file(gate).read_text(encoding="utf-8")
    assert await _blocked(gate) == []


# --------------------------------------------------------------------------------------
# AC4：promote 逐字重放（C2：draft 序列化往返）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac4_promote_replays_the_payload_verbatim(gate: Gate) -> None:
    existing = await gate.service.create(make_draft(content=OLD_CONTENT), USER_A)
    _seed_run(gate)
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())],
        adjudication=[_adjudication(_update(existing.id, content=NEW_CONTENT))])
    _job1, blocked_result, _sink = await _run(gate, invoker)
    assert blocked_result is not None and blocked_result.outcome is MemoryJobOutcome.BLOCKED

    [row] = await _blocked(gate)
    [item] = row.payload["actions"]
    assert (item["action"], item["scope"]) == ("update", "user_global")
    assert item["pin"] == {
        "memory_id": existing.id, "root_id": existing.root_id,
        "version": existing.version, "updated_at": existing.updated_at,
    }
    # 门审条件 C2：落盘的 draft 必须能**往返**回同一个 `MemoryDraftV2`——不能先回报再改形状
    draft = MemoryDraftV2.model_validate(item["draft"])
    assert item["draft"] == draft.model_dump(mode="json")
    assert MemoryDraftV2.model_validate(draft.model_dump(mode="json")) == draft
    assert MemoryDraftV2.model_validate(item["draft"]) == draft

    decision = await gate.service.blocked.promote(row.id, USER_A)

    assert decision.applied is True
    assert decision.reason == "已放行并落盘。"
    [created] = decision.records
    # records 与 payload **逐字一致**：payload 里存的是一份完整的 draft
    for field in ("kind", "tier", "scope", "project_id", "content", "payload", "importance",
                  "strength", "source_type", "source_session_id", "source_event_ids",
                  "evidence"):
        assert getattr(created, field) == getattr(draft, field), field
    assert (created.root_id, created.version) == (existing.root_id, 2)
    assert (await gate.service.read(existing.id, USER_A)).status is MemoryStatus.SUPERSEDED

    text = _memory_file(gate).read_text(encoding="utf-8")
    assert NEW_CONTENT in text, "放行后 markdown 权威面必须新增这条记忆"
    assert OLD_CONTENT not in text, "被取代的旧条目不能留在 markdown 里当僵尸"
    assert [record.content for record in await _active(gate)] == [NEW_CONTENT]

    promoted = await gate.service.blocked.get(row.id)
    assert (promoted.status, promoted.disposition_note) == (PROMOTED, None)
    assert promoted.disposed_at is not None

    again = await gate.service.blocked.promote(row.id, USER_A)
    assert (again.applied, again.reason) == (False, "这条写入已被处置（promoted）。")
    assert again.records == ()
    assert _record_count(gate.database) == 2, "二次 promote 不得新增任何记录"


# --------------------------------------------------------------------------------------
# AC5：pin 漂移 ⇒ 整条拒绝，行保持 blocked 且记下漂移原因
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac5_promote_refuses_a_drifted_target(gate: Gate) -> None:
    existing = await gate.service.create(make_draft(content=OLD_CONTENT), USER_A)
    _seed_run(gate)
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate())],
        adjudication=[_adjudication(_update(existing.id, content=NEW_CONTENT))])
    await _run(gate, invoker)
    [row] = await _blocked(gate)

    # 直接改目标 record 的 version（block 之后有人写了这条记忆）
    bumped = await gate.service.update(
        existing.id, make_draft(content="用户偏好用 yarn 安装依赖"), USER_A)
    assert bumped.version == existing.version + 1

    decision = await gate.service.blocked.promote(row.id, USER_A)

    assert decision.applied is False
    assert decision.reason == f"目标已漂移（{existing.id}），整条拒绝；记录保持 blocked。"
    assert decision.records == ()
    assert _record_count(gate.database) == 2, "被拒绝的 promote 不得写任何记录"
    assert [record.content for record in await _active(gate)] == ["用户偏好用 yarn 安装依赖"]
    assert NEW_CONTENT not in _memory_file(gate).read_text(encoding="utf-8")

    after = await gate.service.blocked.get(row.id)
    assert (after.status, after.disposition_note) == (
        BLOCKED, f"{DRIFT_PREFIX}{existing.id}")


# --------------------------------------------------------------------------------------
# AC6：discard 零落盘，CLI 与 Web 两个入口各留一条审计
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac6_discard_writes_nothing(gate: Gate) -> None:
    _seed_run(gate)
    await _run(gate, _scripted())
    [row] = await _blocked(gate)
    markdown_existed = _memory_file(gate).exists()

    decision = await gate.service.blocked.discard(row.id, USER_A)

    assert (decision.applied, decision.reason) == (True, "已丢弃，未写入任何记忆。")
    assert decision.records == ()
    assert _record_count(gate.database) == 0
    assert _memory_file(gate).exists() is markdown_existed
    assert await _active(gate) == []
    discarded = await gate.service.blocked.get(row.id)
    assert (discarded.status, discarded.disposed_at is not None) == (DISCARDED, True)


@pytest.mark.asyncio
async def test_ac6_cli_discard_is_audited(gate: Gate, caplog) -> None:
    blocked_id = await _insert_blocked_row(gate.service.blocked, trusted=USER_A)
    token = set_identity_context(
        IdentityContext(USER_A.tenant_id, USER_A.user_id, ["user"]))
    try:
        with caplog.at_level(logging.INFO, logger="agent_harness.memory.audit"):
            output, failed = await cli._memory_command(
                gate.settings,
                argparse.Namespace(command="discard", blocked_id=blocked_id))
    finally:
        identity_context_var.reset(token)

    assert (failed, output) == (False, f"{blocked_id}：已丢弃，未写入任何记忆。")
    [record] = [
        item for item in caplog.records
        if getattr(item, "event_type", None) == "memory_governance"]
    assert (record.entry_point, record.action, record.affected_count) == (
        "cli", "discard_blocked", 1)
    assert (await gate.service.blocked.get(blocked_id)).status == DISCARDED


async def _wiring_blocked(client):
    _registry, wiring = await client.app.state.agent.get_wiring()
    assert wiring.memory_v2 is not None and wiring.memory_v2.blocked is not None
    return wiring.memory_v2.blocked


@pytest.mark.asyncio
async def test_ac6_web_discard_is_audited(memory_app, caplog) -> None:
    client, _vectors = memory_app
    blocked = await _wiring_blocked(client)
    blocked_id = await _insert_blocked_row(
        blocked, trusted=TrustedMemoryIdentity("acme", "alice"))

    with caplog.at_level(logging.INFO, logger="agent_harness.memory.audit"):
        response = client.post(
            f"/api/memory-blocked/{blocked_id}/discard", headers=_auth(_ALICE))

    assert response.status_code == 200, response.text
    assert response.json() == {"id": blocked_id, "status": "discarded"}
    [record] = [
        item for item in caplog.records
        if getattr(item, "event_type", None) == "memory_governance"]
    assert (record.entry_point, record.action, record.affected_count) == (
        "api", "discard_blocked", 1)


# --------------------------------------------------------------------------------------
# AC7①：真子进程 SIGKILL —— 提交前被杀 ⇒ 恢复恰好一行 blocked，不多不少
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac7_hard_kill_before_commit_recovers_to_one_blocked_row(
    tmp_path: Path,
) -> None:
    session_id = "blocked-write-recovery"
    run_id = "run-blocked-recovery"
    database = tmp_path / "memory-v2.db"
    sessions = JsonlSessionStore(tmp_path / "sessions")
    session = Session.start(sessions, session_id=session_id)
    session.append(USER_MESSAGE, {"content": USER_TEXT})
    session.append(RUN_STARTED, {"turn_index": 1}, run_id=run_id)
    session.append(MODEL_COMPLETED, {"content": "好的"}, run_id=run_id)

    records = SqliteMemoryV2Store(database)
    await records.initialize()
    jobs = SqliteMemoryV2JobStore(database)
    await jobs.initialize()
    job = await jobs.enqueue(
        idempotency_key=f"memory-v2:{run_id}", trusted=LOCAL,
        session_id=session_id, run_id=run_id)

    child = Path(__file__).with_name("_blocked_write_kill_child.py")
    child_env = dict(os.environ)
    child_env["PYTHONUTF8"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(child), str(tmp_path), session_id,
        cwd=Path(__file__).resolve().parents[3], env=child_env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    barrier = b"BLOCKED_ROW_WRITTEN"
    reached_barrier = False
    output: list[bytes] = []
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            except TimeoutError:
                break
            if not line:
                break
            output.append(line)
            if line.rstrip(b"\r\n") == barrier:
                reached_barrier = True
                break
        if not reached_barrier:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)
            stdout = await process.stdout.read()
            stderr = await process.stderr.read()
            raise AssertionError(
                f"child did not reach {barrier!r}: rc={process.returncode}, "
                f"output={output!r}, stdout={stdout!r}, stderr={stderr!r}")

        # 崩溃窗口的现场：行只在子进程那笔**未提交**事务里，job 也还没终结
        at_barrier = BlockedWriteStore(database, writer=None)
        assert await at_barrier.list_for(LOCAL.tenant_id, LOCAL.user_id) == []
        assert not (await jobs.get(job.job_id)).stage.is_terminal

        process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)
        await process.stdout.read()
        await process.stderr.read()
        assert process.returncode not in (None, 0), "必须是真 SIGKILL，不是正常退出"
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=10)
            await process.stdout.read()
            await process.stderr.read()

    restarted_jobs = SqliteMemoryV2JobStore(database)
    await restarted_jobs.initialize()
    recovered = await restarted_jobs.claim(
        worker_id="recovery-worker", now=datetime.now(UTC) + timedelta(hours=1))
    assert recovered is not None and recovered.job_id == job.job_id

    service = await assembly.build_memory_v2_service(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"))
    executor = MemoryJobExecutor(
        jobs=restarted_jobs, writer=service, searcher=service, invoker=_scripted(),
        session_store=sessions, blocked=service.blocked)
    result = await executor.run(
        recovered, worker_id="recovery-worker",
        run_events=sessions.read_events(session_id), roles=_ROLES, write_approval=True)

    assert result is not None and result.outcome is MemoryJobOutcome.BLOCKED
    rows = await service.blocked.list_for(LOCAL.tenant_id, LOCAL.user_id)
    assert len(rows) == 1, "崩溃恢复必须恰好收敛到一行：0 = 静默丢掉，2 = 重复拦下"
    assert list(result.blocked_ids) == [rows[0].id]
    assert _record_count(database) == 0
    assert (await restarted_jobs.get(job.job_id)).stage is MemoryJobStage.COMPLETED


# --------------------------------------------------------------------------------------
# AC7②：跨 job 查重 —— 第二个 job blocked +0，`suppressed_duplicates`=1
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac7_a_duplicate_intent_is_suppressed_on_the_second_job(gate: Gate) -> None:
    _seed_run(gate)
    first = await _job(gate, key="memory-v2:run-1")
    _job1, result1, _sink1 = await _run(gate, _scripted(), job=first)
    assert result1 is not None and len(result1.blocked_ids) == 1

    # 同一 (session, run) 的第二个 job：存活集逐字相同 ⇒ 同 identity 同 content_hash
    second = await _job(gate, key="duplicate-of-run-1")
    _job2, result2, _sink2 = await _run(gate, _scripted(), job=second)

    assert result2 is not None
    assert result2.outcome is MemoryJobOutcome.BLOCKED
    assert result2.blocked_ids == ()
    state = (await gate.jobs.get(second.job_id)).state
    assert state["suppressed_duplicates"] == 1
    assert state["blocked_ids"] == []
    assert len(await _blocked(gate)) == 1


# --------------------------------------------------------------------------------------
# AC8：PROJECT 作用域的被拦行在两个列表面上都能查到
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac8_project_blocked_row_is_visible_to_the_cli(gate: Gate) -> None:
    _seed_run(gate)
    job = await _job(gate, trusted=PROJECT_X)
    invoker = FakeInvoker(
        formation=[_formation_candidates(_candidate(scope="project"))],
        adjudication=[_adjudication(_add(scope="project"))])
    _job1, result, _sink = await _run(gate, invoker, job=job)

    assert result is not None and result.outcome is MemoryJobOutcome.BLOCKED
    [row] = await _blocked(gate)
    assert row.project_id == PROJECT_X.project_id

    token = set_identity_context(
        IdentityContext(USER_A.tenant_id, USER_A.user_id, ["user"]))
    try:
        output, failed = await cli._memory_command(
            gate.settings, argparse.Namespace(command="blocked", blocked_id=None))
    finally:
        identity_context_var.reset(token)

    assert failed is False
    assert f"id={row.id}" in output
    assert "project=project-x" in output


@pytest.mark.asyncio
async def test_ac8_project_blocked_row_is_visible_over_http(memory_app) -> None:
    client, _vectors = memory_app
    blocked = await _wiring_blocked(client)
    blocked_id = await _insert_blocked_row(
        blocked, trusted=TrustedMemoryIdentity("acme", "alice", "project-x"),
        project_id="project-x")

    response = client.get("/api/memory-blocked", headers=_auth(_ALICE))

    assert response.status_code == 200, response.text
    [item] = response.json()
    assert (item["id"], item["project_id"], item["status"]) == (
        blocked_id, "project-x", BLOCKED)
    assert [action["action"] for action in item["payload"]["actions"]] == ["add"]
    # 归属隔离：同一租户的另一个用户看不到这一行
    assert client.get("/api/memory-blocked", headers=_auth(_BOB)).json() == []


# --------------------------------------------------------------------------------------
# AC9：处置通道与当前 gate 开关无关 —— block 之后关掉 gate，promote 仍可执行
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac9_promote_still_applies_after_the_gate_is_turned_off(gate: Gate) -> None:
    _seed_run(gate)
    await _run(gate, _scripted(), write_approval=True)
    [row] = await _blocked(gate)

    await gate.service.update_settings(USER_A, write_approval=False)
    assert (await gate.service.get_settings(USER_A)).write_approval is False

    decision = await gate.service.blocked.promote(row.id, USER_A)

    assert decision.applied is True
    assert [record.content for record in await _active(gate)] == [CONTENT]
    assert CONTENT in _memory_file(gate).read_text(encoding="utf-8")
