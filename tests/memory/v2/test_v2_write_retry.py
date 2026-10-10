"""#376-1：memory-v2 权威库写路径接入 #515 的锁竞争重试模式（TDD 红→绿）。

背景
----

#515（commit `99de6d6e`）给共享 `harness.db` 的各 Store 加了 `StorageBusyError`
（`storage/sqlite.py:63`）+ `retry_on_busy`（`storage/sqlite.py:77`），并让 web 层
把重试耗尽映射成结构化 503（`web/domain_errors.py:374`）。memory-v2 的权威库
（`memory-v2.db`）写路径同样是 `BEGIN IMMEDIATE`，却**零挂载**该模式——锁超时以裸
`sqlite3.OperationalError` 逃逸，经 `PATCH /api/memory-settings` 变成 500
（#376 票面症状：`store.py:632 update_settings` 的 `BEGIN IMMEDIATE`）。

本文件钉住两层契约（HTTP 的第三层在 `tests/web/test_memory_api.py`）：

1. **应用层重试**：锁超时形态的 `OperationalError` 按退避梯子做有限次重试，瞬时竞争
   窗口内自愈。重试安全的依据与 #515 相同——`OperationalError` 抛出时事务未提交，
   被包装的写方法整块重跑等价于首次执行。
2. **耗尽 → 结构化错误**：重试预算耗尽抛 `StorageBusyError`（`__cause__` 保留末次
   `OperationalError`），不再裸抛；非锁 `OperationalError`（坏路径等）一次都不重试。

锁竞争的制造方式与 `tests/storage/test_sqlite_write_retry.py` 同款：独立连接
`BEGIN IMMEDIATE` 持有写锁（WAL 下读者不受影响、写者阻塞）；把 memory-v2 的
`BUSY_TIMEOUT_MS` 调小让每次尝试快速失败，避免真实 10s 等待拖垮墙钟。

覆盖面：`SqliteMemoryV2Store` 与 `SqliteMemoryV2JobStore` 里**请求 / 后台写路径上、
自开写事务的公开方法**——既含直接 `BEGIN IMMEDIATE` 的（`delete` / `bulk_delete` /
`update_settings` 等），也含经 `write_connection` 自开事务的（`create` / `update` /
`invalidate`）。两处**故意不挂**，不是遗漏（复审 F3 / F1）：

- `initialize`：装配期 DDL（`PRAGMA journal_mode=WAL` + 建表），不属请求 / 后台写路径；
  它的失败在装配期被折成 `DegradedReason.INIT_FAILED`，与 #376 的 HTTP 500 不同形。
- `commit_with_outcome`（`jobs.py`）：它在事务里执行**调用方传入的** `work`（executor
  的 apply 回调会改写调用方的 `_RunState`）。整块重跑会重复那份外部副作用，不满足
  「整块重跑等价首次」的前提——见该方法处的注释与
  `test_commit_with_outcome_is_not_retry_wrapped`。它与 #376 的 HTTP 失败路径无关。

`create` / `update` / `invalidate` 另有一条 `connection=` 借用路径（供 `*_in` 在
调用方的事务里写）：同一装饰器包住这条路径并无害——写锁在事务拥有者
`BEGIN IMMEDIATE` 时已由**同一条连接**取得，事务内的语句不可能再撞
`database is locked`，装饰器在此是 no-op；而 `connection=None` 时它们自开事务，正是
需要这层重试的地方。重试安全的依据同 #515：`OperationalError` 抛出时事务未提交，
被包方法整块重跑等价于首次执行。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from agent_harness.memory.v2.jobs import MemoryJobOutcome, SqliteMemoryV2JobStore
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.storage.sqlite import StorageBusyError
from tests.memory.v2._records import make_draft

USER_A = TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a")


class _LockHolder:
    """独立连接持写锁（`BEGIN IMMEDIATE`），模拟并发写者占住 memory-v2.db。

    `hold_seconds` 后自行释放（模拟瞬时竞争窗口）；不传则一直持有到 `stop()`，
    用于制造"竞争持续超过重试预算"的耗尽场景。
    """

    def __init__(self, database_path: Path, *, hold_seconds: float = 10.0) -> None:
        self._path = database_path
        self._hold_seconds = hold_seconds
        self._acquired = threading.Event()
        self._release = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        con = sqlite3.connect(self._path, timeout=2.0)
        try:
            con.execute("BEGIN IMMEDIATE")
            self._acquired.set()
            self._release.wait(timeout=self._hold_seconds)
        finally:
            con.rollback()
            con.close()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._acquired.wait(timeout=10.0), "锁持有线程没能拿到写锁"

    def stop(self) -> None:
        self._release.set()
        assert self._thread is not None
        self._thread.join(timeout=10.0)


@pytest_asyncio.fixture
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SqliteMemoryV2Store:
    # patch memory-v2 自己的常量（`_sqlite.connect` 在连接时读它）；patch 错模块会让
    # 首尝试的 10s busy_timeout 直接把测试等穿，测试空转绿。
    monkeypatch.setattr("agent_harness.memory.v2._sqlite.BUSY_TIMEOUT_MS", 50)
    instance = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await instance.initialize()
    return instance


@pytest_asyncio.fixture
async def jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SqliteMemoryV2JobStore:
    monkeypatch.setattr("agent_harness.memory.v2._sqlite.BUSY_TIMEOUT_MS", 50)
    instance = SqliteMemoryV2JobStore(tmp_path / "memory-v2.db")
    await instance.initialize()
    return instance


# --------------------------------------------------------------------------------------
# 应用层重试：瞬时锁窗口内自愈
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_settings_retries_through_transient_lock(store: SqliteMemoryV2Store) -> None:
    """锁被短暂占用（< 重试预算）→ 退避梯子内锁释放 → 写成功。

    这是 #376 票面 500 的直接解法：`update_settings` 的 `BEGIN IMMEDIATE` 在瞬时
    竞争窗口内自愈，不再把 `OperationalError` 打到 HTTP 面。
    """
    holder = _LockHolder(store.database_path, hold_seconds=0.2)
    holder.start()
    try:
        settings = await store.update_settings(USER_A, recall_enabled=False)
    finally:
        holder.stop()

    assert settings.recall_enabled is False
    assert (await store.get_settings(USER_A)).recall_enabled is False


@pytest.mark.asyncio
async def test_create_retries_through_transient_lock(store: SqliteMemoryV2Store) -> None:
    """自持写事务的另一条代表路径（`write_connection` 自开 `BEGIN IMMEDIATE`）。

    证明重试覆盖的不止 `update_settings` 那条直接 `BEGIN IMMEDIATE`，`create` 的
    自开事务路径同样在场。
    """
    holder = _LockHolder(store.database_path, hold_seconds=0.2)
    holder.start()
    try:
        created = await store.create(make_draft(), USER_A)
    finally:
        holder.stop()

    assert (await store.get(created.id, USER_A)).id == created.id


@pytest.mark.asyncio
async def test_enqueue_retries_through_transient_lock(jobs: SqliteMemoryV2JobStore) -> None:
    """job 存储的写路径同样自愈（formation job 入队是请求/后台热路径）。"""
    holder = _LockHolder(jobs.database_path, hold_seconds=0.2)
    holder.start()
    try:
        job = await jobs.enqueue(idempotency_key="retry-1", trusted=USER_A, session_id="retry-1")
    finally:
        holder.stop()

    assert job.idempotency_key == "retry-1"


# --------------------------------------------------------------------------------------
# 耗尽 → StorageBusyError（结构化错误，web 层据此翻 503）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_settings_exhaustion_raises_storage_busy(store: SqliteMemoryV2Store) -> None:
    """锁持续占用超过重试预算 → `StorageBusyError`，cause 保留末次 OperationalError。

    耗尽必须是显式新类型而不是继续抛裸 `OperationalError`：web 层按类型把它映射成
    结构化 503（可重试的暂时故障），500 会把它伪装成服务端 bug。
    """
    holder = _LockHolder(store.database_path)
    holder.start()
    try:
        with pytest.raises(StorageBusyError) as excinfo:
            await store.update_settings(USER_A, recall_enabled=False)
    finally:
        holder.stop()

    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)


# --------------------------------------------------------------------------------------
# 不碰非锁错误
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_lock_operational_error_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    """非锁 OperationalError（坏路径等）原样上抛，一次都不重试。

    用「目录当 DB 路径」制造 `unable to open database file`——这不是锁竞争，重试只会
    白等。断言重试的 sleep 一次都没被调过。
    """
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def _spy_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _spy_sleep)

    store = SqliteMemoryV2Store(tmp_path)  # 目录，不是文件
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        await store.update_settings(USER_A, recall_enabled=False)

    assert not isinstance(excinfo.value, StorageBusyError)
    assert "locked" not in str(excinfo.value).lower()
    assert sleeps == []


# --------------------------------------------------------------------------------------
# 包装在场（机械断言：所有自持写事务方法都挂了重试）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("store_cls", "method_names"),
    [
        (
            SqliteMemoryV2Store,
            (
                "create",
                "update",
                "invalidate",
                "delete",
                "bulk_delete",
                "update_settings",
                "purge_expired_tombstones",
                "enqueue_active_index_rebuild",
                "acknowledge",
            ),
        ),
        (
            SqliteMemoryV2JobStore,
            (
                # `commit_with_outcome` **故意不在列**：它执行调用方传入的 `work`，整块
                # 重跑会重复外部副作用（复审 F1）——由
                # `test_commit_with_outcome_is_not_retry_wrapped` 反向钉住。
                "enqueue",
                "claim",
                "transition",
                "start_protected_fact_extraction",
                "save_protected_fact_candidates",
                "finish_protected_fact_extraction",
            ),
        ),
    ],
)
def test_write_methods_are_retry_wrapped(store_cls, method_names) -> None:
    """memory-v2 两个 Store 请求 / 后台写路径上的自持写事务方法都必须挂上重试包装。

    读方法（get / list / latest / pending / get_settings）不在范围内：WAL 下读者不被
    写者阻塞，没有同样的失败形态；`initialize`（装配期 DDL）与 `commit_with_outcome`
    （非幂等 `work`）是**故意排除**，见模块 docstring。用 `__wrapped__`（functools.wraps
    产物）证明包装真的在。本用例是**在场性**检查（列出的必须挂），不是完备性闸门——
    新增写方法仍需在实现时自行挂上并补进列表。
    """
    for name in method_names:
        method = getattr(store_cls, name)
        assert getattr(method, "__wrapped__", None) is not None, (
            f"{store_cls.__name__}.{name} 未挂锁竞争重试（#376-1 / #515 模式）"
        )


# --------------------------------------------------------------------------------------
# 路径类别覆盖：耗尽分支不止 update_settings 一条（复审 STD-5）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_exhaustion_raises_storage_busy(store: SqliteMemoryV2Store) -> None:
    """`write_connection` 自开事务这一类路径的耗尽分支（对照 `update_settings` 的直开）。

    `create` 经 `write_connection` 自开 `BEGIN IMMEDIATE`，与 `update_settings` 直接
    `BEGIN IMMEDIATE` 是两类不同的落点——耗尽映射两类都必须成立，否则只测一处会漏。
    """
    holder = _LockHolder(store.database_path)
    holder.start()
    try:
        with pytest.raises(StorageBusyError) as excinfo:
            await store.create(make_draft(), USER_A)
    finally:
        holder.stop()
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)


@pytest.mark.asyncio
async def test_enqueue_exhaustion_raises_storage_busy(jobs: SqliteMemoryV2JobStore) -> None:
    """job 存储这一类路径的耗尽分支。"""
    holder = _LockHolder(jobs.database_path)
    holder.start()
    try:
        with pytest.raises(StorageBusyError):
            await jobs.enqueue(
                idempotency_key="exhaust", trusted=USER_A, session_id="exhaust")
    finally:
        holder.stop()


# --------------------------------------------------------------------------------------
# 边界：commit_with_outcome 故意不挂重试（复审 F1）
# --------------------------------------------------------------------------------------


def test_commit_with_outcome_is_not_retry_wrapped() -> None:
    """`commit_with_outcome` 不得被重试包装：它在事务里执行调用方传入的 `work`，整块
    重跑会重复那份外部副作用。反向断言 `__wrapped__` 缺席，与正向清单互补。"""
    assert getattr(
        SqliteMemoryV2JobStore.commit_with_outcome, "__wrapped__", None
    ) is None, (
        "commit_with_outcome 挂了重试——它执行调用方传入的 work，重跑会重复副作用（#376-1 F1）"
    )


@pytest.mark.asyncio
async def test_commit_with_outcome_work_runs_once_under_commit_lock_error(
    jobs: SqliteMemoryV2JobStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁错误落在 `work` 之后（强制首个 `commit()` 抛 `database is locked`）时，`work`
    只跑一次、异常原样上抛——不重试、不重复副作用。

    这直接钉住 F1：若把本方法挂上 `retry_on_busy`，同一次 `work` 会被执行两遍
    （executor 的 apply 回调用它改写调用方的 `_RunState`，计数与 memory_ids 会翻倍）。
    """
    await jobs.enqueue(idempotency_key="f1", trusted=USER_A, session_id="f1")
    claimed = await jobs.claim(worker_id="w1")
    assert claimed is not None

    calls = {"work": 0, "commit": 0}
    real_commit = aiosqlite.Connection.commit

    async def flaky_commit(self) -> None:
        calls["commit"] += 1
        if calls["commit"] == 1:
            raise sqlite3.OperationalError("database is locked")
        await real_commit(self)

    async def work(connection) -> str:
        calls["work"] += 1
        return "ok"

    monkeypatch.setattr(aiosqlite.Connection, "commit", flaky_commit)
    with pytest.raises(sqlite3.OperationalError):
        await jobs.commit_with_outcome(
            job_id=claimed.job_id, worker_id="w1",
            outcome=MemoryJobOutcome.COMMITTED, work=work)

    assert calls["work"] == 1, f"work 被执行了 {calls['work']} 次（重试重复副作用）"
