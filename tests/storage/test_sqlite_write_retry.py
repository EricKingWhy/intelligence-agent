"""#515 BUG-01：SQLite 共享库写路径锁竞争重试（TDD 红→绿）。

soak 实测（#515 票面）：共享同一 harness.db 的各 Store 在持续写竞争下
`busy_timeout=10s` 仍会超时，`sqlite3.OperationalError: database is locked`
原样逃逸，`POST /api/sessions/{id}/archive` 在 480s 内打出 123×500。

本文件钉住两层契约：

1. **应用层重试**：锁超时形态的 OperationalError 按退避梯子做有限次重试。
   重试安全的依据是锁语义本身——OperationalError 抛出时事务未提交，写方法
   都是单语句原子写，整块重跑等价于首次执行。
2. **不碰非锁错误**：坏路径等非锁 OperationalError 原样上抛（一次都不重试）；
   IntegrityError 等约束冲突有自己的语义（#519 的域），不进本票的重试范围。

锁竞争的制造方式：独立连接 `BEGIN IMMEDIATE` 持有写锁（WAL 下读者不受影响、
写者阻塞），与 soak 的竞争形态一致；`_BUSY_TIMEOUT_MS` 调小让每次尝试快速
失败，避免真实 10s 等待拖垮测试墙钟。

覆盖面（审查 P2-3 修正）：票面根因 2 是「同一模式存在于共享 harness.db 的
**所有** Store」——装饰覆盖从最初的 3 张（meta/checkpoint/operation）扩到
共享同一库的全部 6 张（+ delegation_tree / workspace / transport）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_harness.storage import (
    SessionMeta,
    SqliteCheckpointStore,
    SqliteOperationLedger,
    SqliteSessionMetaStore,
)
from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger
from agent_harness.storage.sqlite import StorageBusyError
from agent_harness.transport import SqliteTransportLedger
from agent_harness.workspace.store import SqliteWorkspaceStore


class _LockHolder:
    """独立连接持有写锁（BEGIN IMMEDIATE），模拟并发写者占住 harness.db。

    `hold_seconds` 后自行释放（模拟瞬时竞争窗口）；不传则一直持有到 stop()，
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


def _meta_store(tmp_path: Path, monkeypatch) -> SqliteSessionMetaStore:
    monkeypatch.setattr("agent_harness.storage.sqlite._BUSY_TIMEOUT_MS", 50)
    store = SqliteSessionMetaStore(tmp_path / "state.db")
    return store


@pytest.mark.asyncio
async def test_set_archived_retries_through_transient_lock(tmp_path, monkeypatch) -> None:
    """锁被短暂占用（< 重试预算）→ 退避梯子内锁释放 → 写成功。

    这是 soak 里 123×500 的直接解法：archive 端点的 meta 写在瞬时竞争窗口内
    自愈，不再把 OperationalError 打到 HTTP 面。
    """
    store = _meta_store(tmp_path, monkeypatch)
    await store.initialize()
    await store.upsert(
        SessionMeta(session_id="s1", created_at="2026-10-02T00:00:00+00:00", agent_id="default")
    )

    holder = _LockHolder(tmp_path / "state.db", hold_seconds=0.5)
    holder.start()
    try:
        await store.set_archived("s1", True)
    finally:
        holder.stop()

    meta = await store.get("s1")
    assert meta is not None and meta.archived is True


@pytest.mark.asyncio
async def test_set_archived_exhaustion_raises_storage_busy(tmp_path, monkeypatch) -> None:
    """锁持续占用超过重试预算 → StorageBusyError，cause 保留末次 OperationalError。

    耗尽必须是**显式新类型**而不是继续抛裸 OperationalError：web 层要按类型
    把它映射成结构化 503（可重试的暂时故障），500 会把它伪装成服务端 bug。
    """
    store = _meta_store(tmp_path, monkeypatch)
    await store.initialize()
    await store.upsert(
        SessionMeta(session_id="s1", created_at="2026-10-02T00:00:00+00:00", agent_id="default")
    )

    holder = _LockHolder(tmp_path / "state.db")
    holder.start()
    try:
        with pytest.raises(StorageBusyError) as excinfo:
            await store.set_archived("s1", True)
    finally:
        holder.stop()

    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)


@pytest.mark.asyncio
async def test_non_lock_operational_error_is_not_retried(tmp_path, monkeypatch) -> None:
    """非锁 OperationalError（坏路径等）原样上抛，一次都不重试。

    用「目录当 DB 路径」制造 `unable to open database file`——这不是锁竞争，
    重试只会白等。断言重试的 sleep 一次都没被调过。
    """
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def _spy_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _spy_sleep)

    store = SqliteSessionMetaStore(tmp_path)  # 目录，不是文件
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        await store.set_archived("s1", True)

    assert not isinstance(excinfo.value, StorageBusyError)
    assert "locked" not in str(excinfo.value).lower()
    assert sleeps == []


@pytest.mark.asyncio
async def test_workspace_store_retries_through_transient_lock(
    tmp_path, monkeypatch
) -> None:
    """新覆盖的 workspace store 行为抽检：瞬时锁窗口内自愈（不止包装在场）。

    代表性取 `set_meta`（projects 路由的真实写路径）；装饰器是同一份实现，
    一条行为测试 + 全量包装在场断言即覆盖新三张 Store。
    """
    monkeypatch.setattr("agent_harness.storage.sqlite._BUSY_TIMEOUT_MS", 50)
    store = SqliteWorkspaceStore(tmp_path / "state.db")
    await store.initialize()

    holder = _LockHolder(tmp_path / "state.db", hold_seconds=0.5)
    holder.start()
    try:
        await store.set_meta("k", "v")
    finally:
        holder.stop()

    assert await store.get_meta("k") == "v"


@pytest.mark.parametrize(
    ("store_cls", "method_names"),
    [
        (
            SqliteOperationLedger,
            ("create", "update_state", "delete_for_session"),
        ),
        (SqliteCheckpointStore, ("save", "delete_for_session")),
        (
            SqliteSessionMetaStore,
            (
                "upsert",
                "set_archived",
                "update_last_checkpoint_seq",
                "clear_delegation_parent",
                "cleanup",
            ),
        ),
        (
            SqliteDelegationTreeLedger,
            (
                "initialize",
                "reserve",
                "observe_result",
                "ensure_session_budget",
                "admit_session_step",
                "refund_session_turn",
                "refund_session_step",
                "record_session_model_requests",
                "record_session_tools",
                "update_session_limits",
                "consume_session_delegation",
                "refund_session_delegation",
            ),
        ),
        (
            SqliteWorkspaceStore,
            (
                "initialize",
                "begin_change",
                "write_record",
                "prepend_record",
                "append_record",
                "drop_record",
                "drop_order",
                "drop_sessions",
                "set_title",
                "touch",
                "replace_session_order",
                "replace_workspace_order",
                "set_meta",
            ),
        ),
        (
            SqliteTransportLedger,
            ("initialize", "append", "delete_for_session"),
        ),
    ],
)
def test_write_methods_are_retry_wrapped(store_cls, method_names) -> None:
    """共享 harness.db 的全部六张 Store 的全部写方法都必须挂上重试包装。

    读方法（get / list / latest）不在范围内：WAL 下读者不被写者阻塞，没有
    同样的失败形态。用 `__wrapped__`（functools.wraps 产物）证明包装真的在。
    内存实现（delegation_tree 的 InMemory 类）不共享文件锁，不装饰——
    恰好验证本断言只对 Sqlite 类生效。
    """
    for name in method_names:
        method = getattr(store_cls, name)
        assert getattr(method, "__wrapped__", None) is not None, (
            f"{store_cls.__name__}.{name} 未挂锁竞争重试（#515 BUG-01）"
        )
