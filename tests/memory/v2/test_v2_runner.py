"""#298 / MEM-V2-2 T7：runner 的入队、恢复、并发，以及两个具体实现（AC1 / AC6 / AC10 / R1 / R11 / R12）。

存储全部是真的（同一 `memory-v2.db` + 真的 JSONL 会话日志），只有模型是脚本替身。
理由是这几组断言的性质：

- AC6/恢复：判据是"磁盘上的状态说了算"（job 终态 + 记录行），假存储证明不了；
- AC10：判据是**调用是否返回**（形成被卡住时 `notify_run_finished` 仍必须返回），
  这件事只有真的把执行放到后台任务里才成立；
- R11：判据是**同时在飞的任务峰值**，而它由 `Semaphore` 与"认领即派发"两件事共同决定——
  T7 施工中正是靠这组读数发现泵写成串行会让并发上限静默退化成 1。

会话日志也不是替身：`runner` 刻意**只从日志重建事件切片**（新鲜路径与恢复路径同一段代码），
所以"候选能引到 `e1`"这件事本身就证明切片是从日志切出来的——投影的别名只发给它看到的事件。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.v2.budget import MemoryModelRole
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.executor import (
    DegradedReason,
    MemoryJobExecutor,
    MemoryModelCall,
    MemoryModelStage,
)
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.jobs import (
    MemoryJobOutcome,
    MemoryJobStage,
    SqliteMemoryV2JobStore,
)
from agent_harness.memory.v2.projection import build_formation_input
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.memory.v2.runner import (
    DEFAULT_MAX_CONCURRENCY,
    ChatModelInvoker,
    MemoryJobRunner,
    SessionEventSink,
    _seconds_until_claimable,
    idempotency_key,
    run_slice_bounds,
)
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.model.config import ModelConfig
from agent_harness.model.fallback import is_transient_model_error
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_STARTED,
    USER_MESSAGE,
    Session,
)
from agent_harness.session.errors import SessionNotFound
from agent_harness.session.event import MEMORY_UPDATED
from agent_harness.session.store import JsonlSessionStore

SESSION = "session-1"
RUN = "run-1"


# --------------------------------------------------------------------------------------
# 环境
# --------------------------------------------------------------------------------------


@dataclass
class Env:
    path: object
    store: SqliteMemoryV2Store
    jobs: SqliteMemoryV2JobStore
    index: InMemoryMemoryV2Index
    service: MemoryV2Service
    sessions: JsonlSessionStore
    #: `(session_id, run_id)` → 本轮起点（= runtime 的 `arms.memory_event_start`）。
    #: 由 `_seed_run` 在写 user 消息**之前**记下，`_notify` 据此复现生产口径的
    #: 事件切片。刻意**不**存事件列表本身：那样每次 notify 都会拿到一份过期的快照。
    marks: dict[tuple[str, str], int] = field(default_factory=dict)


@pytest_asyncio.fixture
async def env(tmp_path) -> Env:
    path = tmp_path / "memory-v2.db"
    store = SqliteMemoryV2Store(path)
    await store.initialize()
    jobs = SqliteMemoryV2JobStore(path)
    await jobs.initialize()
    index = InMemoryMemoryV2Index()
    return Env(
        path=path, store=store, jobs=jobs, index=index,
        service=MemoryV2Service(store, index),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
    )


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


def _open_session(env: Env, session_id: str = SESSION) -> Session:
    """按**盘上现状**打开会话：seq 计数器要对得上，否则第二条 append 会撞号。"""
    return Session(session_id, env.sessions, events=env.sessions.read_events(session_id))


def _seed_run(
    env: Env, *, session_id: str = SESSION, run_id: str = RUN, user_text: str = "请以后都用 pnpm 装依赖",
    assistant_text: str = "好的", opt_out: bool = False, with_reply: bool = True,
) -> None:
    """往会话日志里写一轮，**顺序与 `_drive` 逐条一致**。

    生产顺序是：先写 user 消息，再 `begin_run`（写 `run/started`），最后模型回复。
    关键的一条：写 user 消息时**不传 `run_id`**（`Session.append` 的 `run_id` 默认就是
    `None`），所以本轮 user 事件的 `run_id` 是 `None`。

    早先这里给 user 消息补了 `run_id`，于是 fixture 描述了一个**产出方到不了的世界**：
    "本轮用户发言属于本轮"这件事靠一个生产里不存在的字段成立。后果是 `_slice` 按
    `run_id` 过滤的缺陷被完全遮住（T7b 实测，见 `run_slice_bounds` 的 docstring）。
    改 fixture 而不是改断言——断言本身是对的。
    """
    session = _open_session(env, session_id)
    env.marks[(session_id, run_id)] = session.mark()
    data: dict = {"content": user_text}
    if opt_out:
        data["memory_opt_out"] = True
    session.append(USER_MESSAGE, data)
    session.append(RUN_STARTED, {"turn_index": 1}, run_id=run_id)
    if with_reply:
        session.append(MODEL_COMPLETED, {"content": assistant_text}, run_id=run_id)


# --------------------------------------------------------------------------------------
# 模型替身
# --------------------------------------------------------------------------------------


class FakeInvoker:
    """按 `stage` 分队列、按顺序回放的脚本替身（与 T6 的用例同款：调用顺序可数）。"""

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


def _formation_candidate() -> str:
    """证据引 `e1`：那是**投影**发给本轮用户消息的别名，只能从日志切出的切片里得到。"""
    return json.dumps({
        "decision": "CANDIDATES",
        "candidates": [{
            "kind": "semantic", "tier": "collection", "scope": "user_global",
            "content": "用户偏好用 pnpm 安装依赖",
            "payload": {"kind": "semantic", "subject": "包管理器", "fact": "偏好 pnpm",
                        "category": "preference"},
            "importance": 0.8, "strength": 0.9,
            "evidence": [{"event_id": "e1", "role": "user", "excerpt": "请用 pnpm"}],
            "sensitivity": "ordinary",
        }],
        "skip_reason": None,
    }, ensure_ascii=False)


def _formation_no_memory() -> str:
    return json.dumps(
        {"decision": "NO_MEMORY", "candidates": [], "skip_reason": "no_durable_value"},
        ensure_ascii=False)


def _add_verdict() -> str:
    return json.dumps({"results": [{
        "action": "ADD", "target_memory_id": None, "reason_code": "durable_new",
        "result": {
            "kind": "semantic", "tier": "collection", "scope": "user_global",
            "content": "用户偏好用 pnpm 安装依赖",
            "payload": {"kind": "semantic", "subject": "包管理器", "fact": "偏好 pnpm",
                        "category": "preference"},
            "importance": 0.8, "strength": 0.9,
            "evidence": [{"event_id": "e1", "role": "user", "excerpt": "请用 pnpm"}],
        },
    }]}, ensure_ascii=False)


def _executor(env: Env, invoker) -> MemoryJobExecutor:
    return MemoryJobExecutor(
        jobs=env.jobs, writer=env.service, searcher=env.service, invoker=invoker)


def _unscripted() -> FakeInvoker:
    """**被调用就会响亮失败**的替身——当"这条路不该有模型调用"的用例的默认值。

    比"返回一个空对象"硬：后者会让"多调了一次模型"变成安静的多写一条记忆。
    """
    return FakeInvoker(formation=[], adjudication=[])


def _working(*, calls: int = 1) -> FakeInvoker:
    """一条能走完全程的脚本：每个 job 一次 formation + 一次 adjudication。"""
    return FakeInvoker(formation=[_formation_candidate()] * calls,
                       adjudication=[_add_verdict()] * calls)


def _runner(env: Env, invoker=None, *, executor=None, **kwargs) -> MemoryJobRunner:
    values: dict = {
        "jobs": env.jobs, "sessions": env.sessions,
        "executor": executor if executor is not None else _executor(
            env, invoker if invoker is not None else _unscripted()),
        "roles": _roles(),
    }
    values.update(kwargs)
    return MemoryJobRunner(**values)


async def _notify(runner: MemoryJobRunner, env: Env, **overrides) -> object:
    """按**生产口径**调用一次终结通知（默认是合格的那一种）。

    `events` 取 `session.since(run_mark)`——runtime 交给宿主的就是这个（见
    `AgentRuntime._notify_memory_formation`）。它**不等于**"筛出本 run 的事件"：
    本轮 user 消息的 `run_id` 是 `None`，按 run_id 筛会把它筛掉，于是资格判定
    看不到用户输入、切片里也拿不到它的别名。两者在日志上的差别就是本文件第 5 节
    那两条回归锁要钉的东西。
    """
    session_id = overrides.pop("session_id", SESSION)
    run_id = overrides.pop("run_id", RUN)
    if "events" not in overrides:
        mark = env.marks[(session_id, run_id)]
        overrides["events"] = env.sessions.read_events(session_id)[mark:]
    values: dict = {
        "session_id": session_id, "run_id": run_id,
        "terminal_status": overrides.pop("terminal_status", "completed"),
        **overrides,
    }
    return await runner.notify_run_finished(**values)


# --------------------------------------------------------------------------------------
# 直接查库（终态行不在 `list_recoverable` 里，只能这样数）
# --------------------------------------------------------------------------------------


def _rows(env: Env, table: str) -> list[dict]:
    connection = sqlite3.connect(env.path)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
    finally:
        # 必须显式关：`with sqlite3.connect(...)` 只管事务，不管连接生命周期，
        # 泄漏的读连接会一直握着 WAL 的读锁。
        connection.close()


def _job_rows(env: Env) -> list[dict]:
    return _rows(env, "memory_v2_jobs")


def _memory_rows(env: Env) -> list[dict]:
    return _rows(env, "memory_v2_records")


# --------------------------------------------------------------------------------------
# 1. 入队与资格（AC1 / R1）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_eligible_run_maps_to_one_job(env: Env) -> None:
    """R1：同一个 run 反复终结 ⇒ 同一行（不是同一个键入队两次）。"""
    _seed_run(env)
    runner = _runner(env, _working())
    first = await _notify(runner, env)
    second = await _notify(runner, env)
    await runner.drain()

    assert first is not None and second is not None
    assert first.job_id == second.job_id, "the same run must not create a second job"
    assert len(_job_rows(env)) == 1


@pytest.mark.asyncio
async def test_job_row_carries_the_run_id(env: Env) -> None:
    """`run_id` 必须**落成一列**：恢复只能靠 `(session_id, run_id)` 切事件。

    这条是 T7 与 T5 的接口变更的判据。塞进 `state` 的实现在这里也过——但它会在第一次
    `transition` 时被整体覆写掉，所以下面第二条断言（终态行仍带 run_id）比第一条更硬。
    """
    _seed_run(env)
    runner = _runner(env, _working())
    await _notify(runner, env)
    await runner.drain()

    rows = _job_rows(env)
    assert [row["run_id"] for row in rows] == [RUN]
    assert rows[0]["stage"] == MemoryJobStage.COMPLETED.value


@pytest.mark.asyncio
async def test_two_runs_in_one_session_are_two_jobs(env: Env) -> None:
    """同一会话的两轮各自一个 job：两条 job 的 `session_id` 相同，只有 `run_id` 能区分。"""
    _seed_run(env, run_id="run-1")
    _seed_run(env, run_id="run-2")
    runner = _runner(env, _working(calls=2))
    await _notify(runner, env, run_id="run-1")
    await _notify(runner, env, run_id="run-2")
    await runner.drain()

    assert sorted(row["run_id"] for row in _job_rows(env)) == ["run-1", "run-2"]


@pytest.mark.asyncio
async def test_the_job_identity_comes_from_the_request_context(env: Env) -> None:
    """job 的归属取**可信身份**，不取调用方自述、也不取事件内容（AC3 的伪造身份那一档）。

    身份在 `notify_run_finished` 里取而不是在后台任务里取：`create_task` 快照的是**此刻**
    的上下文，而请求中间件会在 run 收尾之后重置它。用例把上下文换成一个非常见值，所以
    "取的是哪一份"是可判定的（默认值是 local/local）。
    """
    _seed_run(env)
    token = set_identity_context(
        IdentityContext(tenant_id="tenant-z", user_id="user-z", scopes=["user", "session"]))
    try:
        runner = _runner(env, _working())
        await _notify(runner, env)
        await runner.drain()
    finally:
        identity_context_var.reset(token)

    row = _job_rows(env)[0]
    assert (row["tenant_id"], row["user_id"]) == ("tenant-z", "user-z")
    # 记录行的归属同源（不是从 job 内容或模型输出里读来的第二份）。
    assert _memory_rows(env)[0]["user_id"] == "user-z"


@pytest.mark.asyncio
async def test_the_job_project_comes_from_the_trusted_session_binding(env: Env) -> None:
    _seed_run(env)

    class WorkspaceIndex:
        def workspace_of_session(self, session_id: str):
            assert session_id == SESSION
            return type("Workspace", (), {"id": "project-x"})()

    runner = _runner(env, _unscripted(), workspace_index=WorkspaceIndex())
    await _notify(runner, env)
    await runner.drain()

    assert _job_rows(env)[0]["project_id"] == "project-x"


@pytest.mark.asyncio
async def test_a_failed_project_binding_narrows_the_job_to_user_global(env: Env) -> None:
    _seed_run(env)

    class BrokenWorkspaceIndex:
        def workspace_of_session(self, _session_id: str):
            raise RuntimeError("do not expose this detail")

    runner = _runner(env, _unscripted(), workspace_index=BrokenWorkspaceIndex())
    await _notify(runner, env)
    await runner.drain()

    assert _job_rows(env)[0]["project_id"] is None


@pytest.mark.parametrize(
    ("case", "overrides", "runner_kwargs"),
    [
        ("cancelled", {"terminal_status": "cancelled"}, {}),
        ("orphaned", {"terminal_status": "orphaned"}, {}),
        ("unsupported_failure", {"terminal_status": "failed"}, {}),
        ("no_user_input", {"events": []}, {}),
        ("globally_disabled", {}, {"extraction_enabled": False}),
    ],
)
@pytest.mark.asyncio
async def test_ineligible_shapes_enqueue_nothing(env: Env, case: str, overrides: dict,
                                                 runner_kwargs: dict) -> None:
    """AC1 的另一半：被排除的终态**零入队、零写入**（不是"入队了但没写"）。"""
    _seed_run(env)
    runner = _runner(env, **runner_kwargs)
    result = await _notify(runner, env, **overrides)
    await runner.drain()

    assert result is None, case
    assert _job_rows(env) == [], case
    assert _memory_rows(env) == [], case


@pytest.mark.asyncio
async def test_opt_out_and_missing_model_reply_enqueue_nothing(env: Env) -> None:
    """两条只看事件内容的不合格形态：逐轮退出标记、以及没有成功的模型回复。"""
    _seed_run(env, session_id="opted-out", opt_out=True)
    _seed_run(env, session_id="no-reply", with_reply=False)
    runner = _runner(env)

    for session_id in ("opted-out", "no-reply"):
        # 事件切片走 `_notify` 的默认口径（= 生产口径）。此前这里自行按 run_id 筛，
        # 在本轮 user 消息不带 run_id 之后会把它筛掉——那条断言仍会通过，但通过的理由
        # 变成了"没有用户输入"，而不是用例声称的"退出标记 / 没有成功回复"。
        assert await _notify(runner, env, session_id=session_id) is None
    await runner.drain()
    assert _job_rows(env) == []


@pytest.mark.asyncio
async def test_durable_extraction_setting_gates_each_subsequent_run(env: Env) -> None:
    identity = IdentityContext(tenant_id="local", user_id="local", scopes=["user", "session"])
    token = set_identity_context(identity)
    trusted = TrustedMemoryIdentity("local", "local")
    try:
        await env.service.update_settings(trusted, extraction_enabled=False)
        runner = _runner(env, _working(calls=1), memory_v2=env.service)
        try:
            _seed_run(env, run_id="disabled-by-setting")
            assert await _notify(runner, env, run_id="disabled-by-setting") is None
            assert _job_rows(env) == []

            await env.service.update_settings(trusted, extraction_enabled=True)
            _seed_run(env, run_id="enabled-by-setting")
            assert await _notify(runner, env, run_id="enabled-by-setting") is not None
            await runner.drain()
            assert len(_job_rows(env)) == 1
            assert len(_memory_rows(env)) == 1
        finally:
            await runner.aclose()
    finally:
        identity_context_var.reset(token)


# --------------------------------------------------------------------------------------
# 2. 新鲜路径：入队 → 服务循环 → 写盘 → 事件（R12 / AC10）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_then_pump_writes_one_memory_and_one_update_event(env: Env) -> None:
    """端到端：切片从**会话日志**重建（否则候选的 `e1` 解析不到 ⇒ 零写入）。"""
    _seed_run(env)
    runner = _runner(env, FakeInvoker(
        formation=[_formation_candidate()], adjudication=[_add_verdict()]))
    await _notify(runner, env)
    await runner.drain()

    records = _memory_rows(env)
    assert len(records) == 1
    assert records[0]["user_id"] == "local"
    assert _job_rows(env)[0]["outcome"] == MemoryJobOutcome.COMMITTED.value

    appended = [event for event in env.sessions.read_events(SESSION)
                if event.type == MEMORY_UPDATED]
    assert len(appended) == 1
    assert appended[0].run_id == RUN
    assert appended[0].data["count"] == 1
    assert appended[0].data["memory_ids"] == [records[0]["memory_id"]]


@pytest.mark.asyncio
async def test_no_memory_completes_without_an_update_event(env: Env) -> None:
    """AC2：`NO_MEMORY` 是**安静**成功——终态成功、零记录、不发事件。"""
    _seed_run(env)
    runner = _runner(env, FakeInvoker(formation=[_formation_no_memory()], adjudication=[]))
    await _notify(runner, env)
    await runner.drain()

    row = _job_rows(env)[0]
    assert row["stage"] == MemoryJobStage.COMPLETED.value
    assert row["outcome"] == MemoryJobOutcome.NO_WRITE.value
    assert _memory_rows(env) == []
    assert [event for event in env.sessions.read_events(SESSION)
            if event.type == MEMORY_UPDATED] == []


@pytest.mark.asyncio
async def test_the_visible_answer_does_not_wait_for_formation(env: Env) -> None:
    """AC10：形成被卡住时 `notify_run_finished` 仍必须返回。

    判据就是"它返回了"——若入队与执行没分开，这个 `wait_for` 会超时。顺带钉住
    `aclose` 是有界的（形成卡住时它不能无限等）。
    """
    _seed_run(env)

    class BlockedInvoker:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def __call__(self, call: MemoryModelCall) -> str:
            self.entered.set()
            await asyncio.Event().wait()   # 永不返回：模拟一个很慢的记忆模型
            raise AssertionError("unreachable")

    invoker = BlockedInvoker()
    runner = _runner(env, invoker)

    job = await asyncio.wait_for(_notify(runner, env), timeout=2.0)
    assert job is not None, "the job must be persisted before the answer is released"
    # 后台确实已经跑起来了（否则下一条断言只是"什么都没发生"）。
    await asyncio.wait_for(invoker.entered.wait(), timeout=2.0)

    await asyncio.wait_for(runner.aclose(timeout_seconds=0.5), timeout=5.0)


# --------------------------------------------------------------------------------------
# 3. 恢复（AC6 的入队窗口 / 迁移前旧行）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_job_persisted_before_a_crash_recovers_from_the_log(env: Env) -> None:
    """AC6 的第一个 kill 窗口：入队之后进程死掉 ⇒ 下一次启动把它跑完。

    直接 `enqueue` 模拟"还来不及跑就没了"，然后用一个**新建的** runner 做启动期恢复。
    """
    _seed_run(env)
    await env.jobs.enqueue(
        idempotency_key=idempotency_key(RUN), trusted=TrustedMemoryIdentity(
            tenant_id="local", user_id="local"),
        session_id=SESSION, run_id=RUN)

    runner = _runner(env, _working())
    assert await runner.recover() == 1
    await runner.drain()

    assert len(_memory_rows(env)) == 1
    assert _job_rows(env)[0]["stage"] == MemoryJobStage.COMPLETED.value


@pytest.mark.asyncio
async def test_a_run_that_vanished_from_the_log_degrades_instead_of_looping(env: Env) -> None:
    """切不出事件切片 ⇒ 降级终态（不是"留着下次再试"）。

    留着会让服务循环每轮认领它、每轮再失败一次：僵尸 job 空转并堵住队列。所以判据有两条——
    终态是 DEGRADED + `run_events_unavailable`，**且**再 `recover()` 一次不会又认领到它。
    """
    runner = _runner(env)  # 不该被调用（默认替身被调用即响亮失败）
    await env.jobs.enqueue(
        idempotency_key=idempotency_key(RUN), trusted=TrustedMemoryIdentity(
            tenant_id="local", user_id="local"),
        session_id="never-written", run_id=RUN)

    assert await runner.recover() == 1
    await runner.drain()

    row = _job_rows(env)[0]
    assert row["stage"] == MemoryJobStage.DEGRADED.value
    assert row["reason"] == DegradedReason.RUN_EVENTS_UNAVAILABLE.value
    assert _memory_rows(env) == []
    assert await runner.recover() == 0, "a terminal job must not be picked up again"


@pytest.mark.asyncio
async def test_a_pre_migration_job_without_run_id_degrades(env: Env) -> None:
    """T7 之前入队的行没有 `run_id`（迁移补列给 NULL）⇒ 同样收口成降级，而不是空转。"""
    runner = _runner(env)
    await env.jobs.enqueue(
        idempotency_key="legacy", trusted=TrustedMemoryIdentity(
            tenant_id="local", user_id="local"),
        session_id=SESSION)

    assert await runner.recover() == 1
    await runner.drain()

    assert _job_rows(env)[0]["reason"] == DegradedReason.RUN_EVENTS_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_an_error_outside_the_executor_contract_degrades_the_job(env: Env) -> None:
    """契约之外的异常（这里是检索层挂掉）必须**收口**，且归因不能借用取不到切片那一条。

    两条理由：留着非终态这个 job 就永远终结不了（AC6 要求收敛到一个终态），而且每次被
    认领都会再烧一次模型调用；归因合并会让"会话日志读不出来"与"检索层坏了"在观测上
    无从区分，而这两件事的处置方向正好相反。
    """

    class ExplodingSearcher:
        async def search(self, query, trusted, *, scope, limit):
            raise RuntimeError("the search layer is down")

    _seed_run(env)
    invoker = _working()
    runner = _runner(env, invoker, executor=MemoryJobExecutor(
        jobs=env.jobs, writer=env.service, searcher=ExplodingSearcher(), invoker=invoker))
    await _notify(runner, env)
    await runner.drain()

    row = _job_rows(env)[0]
    assert row["stage"] == MemoryJobStage.DEGRADED.value
    assert row["reason"] == DegradedReason.JOB_FAILED.value
    assert _memory_rows(env) == []
    assert invoker.calls == [], "检索先坏了，就不该再有模型调用"


# --------------------------------------------------------------------------------------
# 4. R11 全局并发
# --------------------------------------------------------------------------------------


class ConcurrencyProbe:
    """把"同时在飞几个"变成**确定**读数：等到 `expect` 个调用同时在飞才放行。

    用固定时长睡眠的版本是**量不准的仪器**：0.02 秒比一次 SQLite 往返还短，于是任务之间
    几乎不重叠，测出来的峰值（实测 2）远小于真实上限。那读数是"仪器不够灵"，会被误读成
    "并发被限住了"。等到齐再走，`peak` 就只由上限决定。

    等不到齐时按 `timeout` 放行而不是死等：上限若真的小于 `expect`，用例要在有限时间内
    **失败**（`peak < expect`），而不是挂住。
    """

    def __init__(self, *, expect: int, timeout: float = 1.0) -> None:
        self._expect = expect
        self._timeout = timeout
        self._reached = asyncio.Event()
        self.live = 0
        self.peak = 0

    async def __call__(self, call: MemoryModelCall) -> str:
        self.live += 1
        self.peak = max(self.peak, self.live)
        if self.live >= self._expect:
            self._reached.set()
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._reached.wait(), timeout=self._timeout)
        try:
            return _formation_candidate() if call.stage is MemoryModelStage.FORMATION \
                else _add_verdict()
        finally:
            self.live -= 1


async def _seed_independent_jobs(env: Env, count: int) -> None:
    """`count` 条互不干扰的待办：**不同用户**（否则 R11 的按用户串行会把它们排成一条线）。

    同一条会话里放 `count` 轮——`run_id` 不同即可，会话日志是共用的那条路径，正好也压一下
    "按 run 切切片"在多轮会话上的正确性。身份在 `enqueue` 上显式给，不走请求上下文。
    """
    session_id = "session-conc"
    for index in range(count):
        _seed_run(env, session_id=session_id, run_id=f"run-{index}")
        await env.jobs.enqueue(
            idempotency_key=idempotency_key(f"run-{index}"),
            trusted=TrustedMemoryIdentity(tenant_id="t", user_id=f"u{index}"),
            session_id=session_id, run_id=f"run-{index}")


@pytest.mark.asyncio
async def test_global_concurrency_defaults_to_four(env: Env) -> None:
    """R11 的默认值：不传就是 4——**夹在两边的判别性断言**。

    `peak > 1` 排除"泵写成串行"（那种实现让上限静默退化成 1，而 `Semaphore` 永不竞争）；
    `peak == 4` 排除"没有上限"。只断一侧的实现都能蒙过去。
    """
    await _seed_independent_jobs(env, 8)
    probe = ConcurrencyProbe(expect=DEFAULT_MAX_CONCURRENCY)
    runner = _runner(env, probe)
    assert await runner.recover() == 8
    await runner.drain()

    assert probe.peak == DEFAULT_MAX_CONCURRENCY == 4
    assert len(_memory_rows(env)) == 8


@pytest.mark.asyncio
async def test_global_concurrency_is_configurable(env: Env) -> None:
    """R11 的"configurable"：传 2 就真的是 2（不是"传了但没人读"）。"""
    await _seed_independent_jobs(env, 6)
    probe = ConcurrencyProbe(expect=2)
    runner = _runner(env, probe, max_concurrency=2)
    await runner.recover()
    await runner.drain()

    assert probe.peak == 2
    assert len(_memory_rows(env)) == 6


@pytest.mark.asyncio
async def test_a_zero_concurrency_limit_is_rejected(env: Env) -> None:
    """0 会让"所有 job 都拿不到槽位"变成静默的永不执行，必须在构造期响亮失败。"""
    with pytest.raises(ValueError):
        _runner(env, max_concurrency=0)


# --------------------------------------------------------------------------------------
# 5. 事件出口：seq 必须按**盘上现状**取（否则事件静默丢失）
# --------------------------------------------------------------------------------------


def test_the_sink_uses_the_seq_that_is_on_disk_now(env: Env) -> None:
    """R12 的 `memory/updated` 不能因为"会话又长了"就丢。

    `Session.append` 用的是它**构造时**算出的计数器，而 store 对撞号是拒写（`SeqConflict`）。
    后台 job 跑完时会话早已可能被新的一轮追加过——所以 sink 每次都得重读。判据是"事件确实
    落进去了、且 seq 是当前的下一个"，用旧快照的实现会撞号（异常被 `executor._emit` 吞掉）
    于是这里读回 0 条。
    """
    _seed_run(env)
    sink = SessionEventSink(env.sessions, SESSION)
    # 拿一个**陈旧**的快照，模拟"job 开始时看到的世界"。
    stale = _open_session(env)
    _open_session(env).append(MODEL_COMPLETED, {"content": "又一轮"})
    _open_session(env).append(MODEL_COMPLETED, {"content": "再一轮"})

    assert len(stale.events) < len(env.sessions.read_events(SESSION)), \
        "the snapshot must really be stale, otherwise this test proves nothing"
    sink.emit(MEMORY_UPDATED, {"count": 1, "memory_ids": ["m-1"], "job_id": "job-1"},
              run_id=RUN)

    events = env.sessions.read_events(SESSION)
    assert [event.seq for event in events] == list(range(len(events))), "no gaps, no clash"
    updates = [event for event in events if event.type == MEMORY_UPDATED]
    assert len(updates) == 1
    assert updates[0].data["job_id"] == "job-1"


def test_the_sink_does_not_resurrect_a_deleted_session(env: Env) -> None:
    """ADR-0036：硬删之后不得重建。sink 只吞 `SeqConflict`（重试条件），不吞这个。"""
    _seed_run(env)
    env.sessions.delete_session(SESSION)
    with pytest.raises(SessionNotFound):
        SessionEventSink(env.sessions, SESSION).emit(MEMORY_UPDATED, {"count": 0})


# --------------------------------------------------------------------------------------
# 6. 真实模型调用器（R10 的两条数字）
# --------------------------------------------------------------------------------------


class FakeChatModel:
    def __init__(self) -> None:
        self.calls: list[tuple[list, dict]] = []
        self.content: object = "{}"

    async def ainvoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return type("Response", (), {"content": self.content})()


def _call(**overrides) -> MemoryModelCall:
    values: dict = {
        "stage": MemoryModelStage.FORMATION, "role": MemoryModelRole.PRIMARY, "attempt": 1,
        "model": _model_config("senseaudio"), "system_prompt": "SYSTEM",
        "payload": {"a": 1}, "max_output_tokens": 4096, "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return MemoryModelCall(**values)


@pytest.mark.asyncio
async def test_the_invoker_passes_max_tokens_and_the_two_messages() -> None:
    """R10 的"每次调用 4k 输出"必须**真的**进请求参数。

    交付前实测过这条杠杆：`ChatOpenAI` 把它改名成 `max_completion_tokens` 放进请求体，
    而 `model.bind(max_tokens=…)` 那条路读不到——所以这里钉住"走的是调用参数"。
    """
    model = FakeChatModel()
    built: list[ModelConfig] = []

    def factory(config, **kwargs):
        built.append(config)
        return model

    invoker = ChatModelInvoker(factory=factory)
    assert await invoker(_call(max_output_tokens=123)) == "{}"

    messages, kwargs = model.calls[0]
    assert kwargs == {"max_tokens": 123}
    assert [message.content for message in messages] == ["SYSTEM", '{"a": 1}']
    assert len(built) == 1


@pytest.mark.asyncio
async def test_the_invoker_reuses_one_model_per_config() -> None:
    """一次 job 最多 5 次调用；每次都新建会重建 httpx client（连接池 + TLS 握手）。"""
    model = FakeChatModel()
    built: list[ModelConfig] = []
    invoker = ChatModelInvoker(factory=lambda config, **kwargs: (built.append(config), model)[1])

    await invoker(_call())
    await invoker(_call(stage=MemoryModelStage.ADJUDICATION, attempt=2))
    assert len(built) == 1


@pytest.mark.asyncio
async def test_the_invoker_turns_a_slow_call_into_a_transient_timeout() -> None:
    """R10 的 120 秒硬截止在**调用边界**上兑现，且必须是**瞬时**错（走 R9 的序列）。"""

    class SlowModel:
        async def ainvoke(self, messages, **kwargs):
            await asyncio.sleep(5.0)

    invoker = ChatModelInvoker(factory=lambda config, **kwargs: SlowModel())
    with pytest.raises(TimeoutError) as failure:
        await invoker(_call(timeout_seconds=0.05))
    assert is_transient_model_error(failure.value)


@pytest.mark.asyncio
async def test_the_invoker_rejects_non_text_content() -> None:
    """非文本形状是 provider 没按契约回话（`provider_error`），不是解析失败——两者归因要分开。"""
    model = FakeChatModel()
    model.content = [{"type": "image"}]
    invoker = ChatModelInvoker(factory=lambda config, **kwargs: model)

    with pytest.raises(TypeError):
        await invoker(_call())


# --------------------------------------------------------------------------------------
# 7. 唤醒时刻（等 lease 到期）
# --------------------------------------------------------------------------------------


class _Job:
    """`_seconds_until_claimable` 只读两个字段，用一个瘦身替身比造整行更清楚。"""

    def __init__(self, *, owner: str | None, expires: str | None) -> None:
        self.lease_owner = owner
        self.lease_expires_at = expires
        self.job_id = "job"


NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


def test_no_wakeup_without_pending_jobs() -> None:
    assert _seconds_until_claimable([], NOW) is None


def test_wakeup_happens_at_the_earliest_expiry() -> None:
    """取最早的一条：晚的那条那时自然也被同一轮认领走。"""
    pending = [
        _Job(owner="dead-1", expires=(NOW + timedelta(seconds=90)).isoformat()),
        _Job(owner="dead-2", expires=(NOW + timedelta(seconds=30)).isoformat()),
    ]
    assert _seconds_until_claimable(pending, NOW) == pytest.approx(30.0)


def test_no_wakeup_when_nothing_is_waiting_on_a_lease() -> None:
    """无属主（或已到期）的行意味着"被**按用户串行**挡住"——等时间是白等。

    那种情况由同用户那个在途 job 的终结来解除，而它终结时本进程的泵会自己再跑一轮。
    """
    pending = [
        _Job(owner=None, expires=None),
        _Job(owner="live", expires=(NOW - timedelta(seconds=5)).isoformat()),
    ]
    assert _seconds_until_claimable(pending, NOW) is None


# --------------------------------------------------------------------------------------
# 5. run 切片：本轮用户发言在日志里**不带 run_id**（T7b 实测修正）
# --------------------------------------------------------------------------------------


def test_the_users_own_message_lands_in_the_run_slice(env: Env) -> None:
    """本轮用户发言必须在 `run_events` 里、并且**拿到别名**。

    判据取别名而不是"文本出现过"：那句话同时在 `earlier_messages` 里（投影会截最近 8 条
    user/assistant 消息），文本断言分不出"被当成本轮输入"与"被当成历史"。只有别名集合
    能分——别名只发给 `run_events`，而 R6 要求 USER/profile 事实必须有可解析的用户证据，
    所以别名缺失等于"用户事实结构上写不出来"，而全链路不报错（与 T6b 的 `event_id`
    缺失同一类静默失效）。
    """
    _seed_run(env)
    events = env.sessions.read_events(SESSION)
    user_event = next(event for event in events if event.type == USER_MESSAGE)
    assert user_event.run_id is None, (
        "生产事实：`_drive` 先写 user 消息、再 begin_run，且写它时不传 run_id"
        "（`Session.append` 的 run_id 默认就是 `None`）"
    )

    bounds = run_slice_bounds(events, RUN)
    assert bounds is not None
    start, stop = bounds
    formation = build_formation_input(events[start:stop], history=events[:start])

    assert user_event.event_id in set(formation.refs.values())
    assert [message.role for message in formation.current_run] == ["user", "assistant"]


def test_the_second_runs_slice_starts_at_its_own_user_message(env: Env) -> None:
    """多轮会话：第 2 轮的切片只含它自己的用户发言——不吞掉第 1 轮，也不漏掉自己。

    "向前吞紧邻的、不带 run_id 的 user/message"这条规则到第 2 轮才显出边界：第 1 轮的
    user 消息同样不带 run_id。若停止条件写成"一直向前到没有 user 消息"，两轮会被并成
    一轮（同一句话既当输入又当历史）。
    """
    _seed_run(env, run_id="run-1", user_text="第一轮的话")
    _seed_run(env, run_id="run-2", user_text="第二轮的话")
    events = env.sessions.read_events(SESSION)

    bounds = run_slice_bounds(events, "run-2")
    assert bounds is not None
    start, stop = bounds
    run_events = events[start:stop]

    texts = [event.data["content"] for event in run_events if event.type == USER_MESSAGE]
    assert texts == ["第二轮的话"]
    assert all(event.run_id in (None, "run-2") for event in run_events)
    assert sum(1 for event in events[:start] if event.type == RUN_STARTED) == 1, (
        "第 1 轮整轮都必须算历史（含它的 run/started）"
    )


@pytest.mark.asyncio
async def test_history_is_not_merged_back_into_the_run_events(env: Env) -> None:
    """`_slice` 的第二个返回值是历史——**不能**同时并进本轮事件。

    **这条是变异红证补出来的**（T7b 的 M3：把 `_slice` 改成
    `events[start:stop] + events[:start]`，区间不变、结果全错，而当时 42 条用例全绿）。
    已有三条切片锁只问纯函数 `run_slice_bounds`（"区间是哪一段"），而"第一段里放什么"
    是 `_slice` 的返回值决定的——所以缺口正好落在纯函数与返回值之间。

    为什么要紧：投影只给 `run_events` 发别名，而别名表就是 `policy.select_candidates`
    的**键空间**（"模型能引什么"的唯一开关）。并进去等于把可引证范围从"这一轮"悄悄放宽
    到"整段会话"，而候选文案仍声称证据来自本轮——一门只会更宽松、不会响的证据门
    （T6b 那一课的同族）。
    """
    _seed_run(env, run_id="run-1", user_text="第一轮的话", assistant_text="第一轮的回话")
    _seed_run(env, run_id="run-2", user_text="第二轮的话", assistant_text="第二轮的回话")
    events = env.sessions.read_events(SESSION)
    first_run = {event.event_id for event in events if event.run_id == "run-1"}
    assert first_run, "正控：第 1 轮的事件必须存在，否则这条用例什么都没证"

    runner = _runner(env)
    job = await env.jobs.enqueue(
        idempotency_key="probe-history", trusted=TrustedMemoryIdentity(
            tenant_id="local", user_id="local"),
        session_id=SESSION, run_id="run-2")
    run_events, history = await runner._slice(job)

    assert first_run.isdisjoint({event.event_id for event in run_events}), \
        "第 1 轮的事件不得出现在本轮事件里"
    assert first_run & {event.event_id for event in history} == first_run, \
        "第 1 轮整轮都必须算历史（它是上下文，不是本轮输入）"
    assert set(build_formation_input(run_events, history=history).refs.values()) \
        .isdisjoint(first_run), "别名只发给本轮——发到历史上就是把可引证范围放宽到整段会话"


def test_a_run_that_is_not_in_the_log_has_no_bounds(env: Env) -> None:
    """日志里没有这一次 run ⇒ `None`：调用方据此走降级，而不是拿一个错的区间去跑。"""
    _seed_run(env)
    assert run_slice_bounds(env.sessions.read_events(SESSION), "run-missing") is None


class _RecordingSearcher:
    """记录 query 的检索替身（形成期的 `_relevant` 与裁决期的检索都走它）。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query, trusted, *, scope, limit):
        self.queries.append(query)
        return []


@pytest.mark.asyncio
async def test_the_production_slice_grounds_formation_in_the_users_own_words(
    env: Env,
) -> None:
    """**走 `_slice`（生产切片路径）**：用户发言要在切片里拿到别名，且成为检索的 query。

    三件事同一个根因，且都静默——切片若按 `run_id` 过滤（T7a 的原形），本轮 user 消息
    被推进 `history`，于是：

    1. `current_run` 里没有用户原话（它退化成无别名的 `earlier_messages`）；
    2. 它的 `event_id` 拿不到别名 ⇒ R5 的"用户明确陈述规则"豁免与 `_is_user_fact` 的
       用户权威路径**结构上不可达**（模型没有可引的用户 ref）；
    3. `executor._run_query` 只取 `user/message` 的文本 ⇒ 拿到 `""` ⇒ `_relevant` 对空
       query 直接返回 `[]` ⇒ **R2 的"最多十条相似 active 记忆"整段为空**：模型看不到任何
       既有记忆，也就给不出 UPDATE，只能一路 ADD。

    第 ③ 条用**检索替身收到的 query** 来钉：它证明的是"检索真的被问了什么"，比断言某个
    私有函数的返回值更硬，也不必把执行器的内部函数搬进用例。
    """
    _seed_run(env)
    user_event = next(
        event for event in env.sessions.read_events(SESSION) if event.type == USER_MESSAGE
    )
    searcher = _RecordingSearcher()
    invoker = _working()
    runner = _runner(env, invoker, executor=MemoryJobExecutor(
        jobs=env.jobs, writer=env.service, searcher=searcher, invoker=invoker))

    await _notify(runner, env)
    await runner.drain()

    # ① + ②：直接问 runner 要切片（走 `_slice` 本身，不绕过它重算一遍区间）
    sealed = await env.jobs.enqueue(
        idempotency_key="probe-slice", trusted=TrustedMemoryIdentity(
            tenant_id="local", user_id="local"),
        session_id=SESSION, run_id=RUN)
    run_events, history = await runner._slice(sealed)
    formation = build_formation_input(run_events, history=history)
    assert user_event.event_id in set(formation.refs.values())
    assert [message.role for message in formation.current_run] == ["user", "assistant"]

    # ③：形成期的相似记忆检索问的是**用户那句话**
    assert searcher.queries, "检索根本没被调用"
    assert searcher.queries[0] == "请以后都用 pnpm 装依赖", searcher.queries[:2]
