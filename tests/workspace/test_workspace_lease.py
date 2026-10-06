"""W-10（#354）：单目录写入租约 + 持久 FIFO 队列——行为面（票面验收逐项）。

覆盖：并发创建恰一获租约、不同目录可并发、持有者 run 终态/暂停/待审阅不放
锁、释放原子选出队首（presence 只读合同判定在场）、取消队列项与重启后顺序
不变、路径形态绕过（junction/UNC/大小写/尾分隔符/父子）、同目录同时持有计数
≤1、重启对账。SQLite 真实落盘（tmp_path 下的 harness.db），重启 = 同库重建
manager 实例。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agent_harness.workspace.lease import (
    LeaseAlreadyHeldElsewhere,
    LeaseError,
    LeasePathConflict,
    NoPresenceReader,
    WorkspaceLeaseManager,
)
from agent_harness.workspace.lease_paths import LeasePathError, normalize_dir_key
from agent_harness.workspace.lease_store import (
    LEASE_AWAITING_PRESENCE,
    LEASE_HELD,
    QUEUE_WAITING,
    SqliteLeaseStore,
)
from tests.symlink_capability import make_dir_link, needs_dir_link


class FakePresence:
    """按 session 集合回答的 presence 只读合同替身（W-12 真实现的形状）。"""

    def __init__(self, present: set[str] | None = None) -> None:
        self.present: set[str] = present or set()

    def has_present_client(self, session_id: str) -> bool:
        return session_id in self.present


async def new_manager(tmp_path: Path, presence=None) -> WorkspaceLeaseManager:
    manager = WorkspaceLeaseManager(
        SqliteLeaseStore(tmp_path / "harness.db"), presence
    )
    await manager.initialize()
    return manager


# —— 并发与互斥 ——


@pytest.mark.asyncio
async def test_concurrent_same_dir_acquire_exactly_one_granted(tmp_path) -> None:
    """票面 AC：同目录两个 Task 并发创建仅一个获租约（≤1 计数断言）。"""
    manager = await new_manager(tmp_path)
    outcomes = await asyncio.gather(
        manager.acquire("task-a", str(tmp_path)),
        manager.acquire("task-b", str(tmp_path)),
    )
    granted = [o for o in outcomes if o.granted]
    assert len(granted) == 1
    loser = outcomes[0] if granted[0] is outcomes[1] else outcomes[1]
    assert loser.queued and loser.position == 1
    # ≤1 计数不变量：同键 held 行恰好 1。
    held = [l for l in await manager.leases() if l.state == LEASE_HELD]
    assert len(held) == 1


@pytest.mark.asyncio
async def test_different_dirs_grant_concurrently(tmp_path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    manager = await new_manager(tmp_path)
    first = await manager.acquire("task-a", str(a))
    second = await manager.acquire("task-b", str(b))
    assert first.granted and second.granted
    assert first.dir_key != second.dir_key


@pytest.mark.asyncio
async def test_reacquire_same_dir_is_idempotent_grant(tmp_path) -> None:
    manager = await new_manager(tmp_path)
    first = await manager.acquire("task-a", str(tmp_path))
    again = await manager.acquire("task-a", str(tmp_path))
    assert first.granted and again.granted


@pytest.mark.asyncio
async def test_same_dir_second_task_queues_not_starts(tmp_path) -> None:
    """同键已被持有：第二任务进持久 FIFO，不得被视为获准写。"""
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(tmp_path))
    outcome = await manager.acquire("task-b", str(tmp_path))
    assert not outcome.granted
    assert outcome.holder == "task-a"
    assert outcome.position == 1


@pytest.mark.asyncio
async def test_holder_keeps_lease_across_run_states(tmp_path) -> None:
    """持有者暂停 / run/completed / 等待审阅不放锁：本层不观察 Run 状态，
    释放只经显式 release——持有者"完成"后第二任务仍在队列等待。"""
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(tmp_path))
    # 模拟 holder run/completed / paused / awaiting review：这些状态变化不
    # 经过租约层（没有可调用的状态入口），唯一事实 = 租约仍 held。
    outcome = await manager.acquire("task-b", str(tmp_path))
    assert not outcome.granted
    held = [l for l in await manager.leases() if l.state == LEASE_HELD]
    assert [l.session_id for l in held] == ["task-a"]


# —— 释放与队首 promotion ——


@pytest.mark.asyncio
async def test_release_without_presence_leaves_lease_unowned(tmp_path) -> None:
    """释放后队首无在场客户端：不启动（awaiting_presence），队列保持。"""
    manager = await new_manager(tmp_path, NoPresenceReader())
    await manager.acquire("task-a", str(tmp_path))
    await manager.acquire("task-b", str(tmp_path))
    outcome = await manager.release("task-a")
    assert outcome.released and outcome.promoted_to is None
    leases = await manager.leases()
    assert len(leases) == 1
    assert leases[0].state == LEASE_AWAITING_PRESENCE
    assert leases[0].session_id is None
    # 队首仍在队列第一位；新来者也只排队。
    assert (await manager.status("task-b")).queue_position == 1
    third = await manager.acquire("task-c", str(tmp_path))
    assert not third.granted and third.position == 2


@pytest.mark.asyncio
async def test_release_with_head_present_promotes_head_only(tmp_path) -> None:
    """释放后队首有在场客户端：只启动队首（第二名不越位）。"""
    presence = FakePresence(present={"task-b", "task-c"})
    manager = await new_manager(tmp_path, presence)
    await manager.acquire("task-a", str(tmp_path))
    await manager.acquire("task-b", str(tmp_path))
    await manager.acquire("task-c", str(tmp_path))
    outcome = await manager.release("task-a")
    assert outcome.promoted_to == "task-b"
    held = [l for l in await manager.leases() if l.state == LEASE_HELD]
    assert [(l.session_id, l.state) for l in held] == [("task-b", LEASE_HELD)]
    assert (await manager.status("task-c")).queue_position == 1


@pytest.mark.asyncio
async def test_grant_awaited_when_client_returns(tmp_path) -> None:
    """用户回来（presence 出现）→ awaiting 租约显式转授队首（W-12 接入点）。"""
    presence = FakePresence()
    manager = await new_manager(tmp_path, presence)
    await manager.acquire("task-a", str(tmp_path))
    await manager.acquire("task-b", str(tmp_path))
    await manager.release("task-a")
    assert await manager.grant_awaited(str(tmp_path)) is None
    presence.present.add("task-b")
    assert await manager.grant_awaited(str(tmp_path)) == "task-b"
    held = [l for l in await manager.leases() if l.state == LEASE_HELD]
    assert [l.session_id for l in held] == ["task-b"]


@pytest.mark.asyncio
async def test_release_without_queue_frees_dir(tmp_path) -> None:
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(tmp_path))
    outcome = await manager.release("task-a")
    assert outcome.released and outcome.promoted_to is None
    assert await manager.leases() == []
    # 目录回到空闲：新任务直接获租约。
    assert (await manager.acquire("task-b", str(tmp_path))).granted


@pytest.mark.asyncio
async def test_duplicate_release_is_idempotent(tmp_path) -> None:
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(tmp_path))
    assert (await manager.release("task-a")).released
    second = await manager.release("task-a")
    assert not second.released and second.promoted_to is None


@pytest.mark.asyncio
async def test_released_holder_must_reacquire(tmp_path) -> None:
    """显式释放后原 Task 再次写须重新取得租约（无豁免，排到队尾）。"""
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(tmp_path))
    await manager.acquire("task-b", str(tmp_path))
    await manager.release("task-a")
    # task-a 想再写：同键已无主（awaiting），但队首 task-b 未在场 → 排队。
    outcome = await manager.acquire("task-a", str(tmp_path))
    assert not outcome.granted
    assert (await manager.status("task-a")).queue_position == 2  # task-b 之后


# —— 队列取消与顺序 ——


@pytest.mark.asyncio
async def test_cancel_middle_queue_item_preserves_order(tmp_path) -> None:
    manager = await new_manager(tmp_path)
    for name in ("task-a", "task-b", "task-c", "task-d"):
        await manager.acquire(name, str(tmp_path))
    assert await manager.cancel_queued("task-c")
    assert not await manager.cancel_queued("task-c")  # 幂等
    assert (await manager.status("task-b")).queue_position == 1
    assert (await manager.status("task-d")).queue_position == 2
    assert (await manager.status("task-c")).queue_position is None
    # 取消不影响当前持有者。
    held = [l for l in await manager.leases() if l.state == LEASE_HELD]
    assert [l.session_id for l in held] == ["task-a"]


@pytest.mark.asyncio
async def test_cancel_all_waiting_frees_awaiting_lease(tmp_path) -> None:
    """awaiting 租约的等待对象全撤 → 租约摘除，目录完全空闲。"""
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(tmp_path))
    await manager.acquire("task-b", str(tmp_path))
    await manager.release("task-a")
    assert await manager.cancel_queued("task-b")
    assert await manager.leases() == []


# —— 路径形态不能绕过互斥 ——


@pytest.mark.asyncio
async def test_case_and_trailing_separator_cannot_bypass(tmp_path) -> None:
    real = tmp_path / "proj"
    real.mkdir()
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(real))
    same = await manager.acquire("task-b", str(real) + "/")
    assert not same.granted  # 同键（realpath 吸收尾分隔符）→ 排队而非新租约


@pytest.mark.asyncio
@needs_dir_link
async def test_symlink_cannot_bypass(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    # Windows 无特权宿主退回 junction（mklink /J）——realpath 同样解析到
    # real，互斥绕过断言语义不变（#731，先例 tests/web/test_host_dirs_api.py）。
    if not make_dir_link(link, real):
        pytest.skip("宿主无法创建目录链接（探测与使用间能力变化）")
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(real))
    outcome = await manager.acquire("task-b", str(link))
    assert not outcome.granted
    assert outcome.dir_key == normalize_dir_key(str(real))


@pytest.mark.asyncio
async def test_parent_child_directory_rejected(tmp_path) -> None:
    """父子目录相交：拒绝并要求用户另选独立目录，不能并发写。"""
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "child").mkdir()
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(parent))
    with pytest.raises(LeasePathConflict):
        await manager.acquire("task-b", str(parent / "child"))
    # 反向（对父目录本身申请）：task-a 持有的就是 parent 键 → 同键排队而非冲突。
    outcome = await manager.acquire("task-b", str(parent))
    assert not outcome.granted
    assert outcome.holder == "task-a"


# 真实 Windows 宿主上合成路径被 #726 存在性校验 fail-closed 拒绝；
# Windows 形态词法等价只能由 POSIX 宿主等价层钉住（同 TestWindowsFormLexical）。
@pytest.mark.skipif(os.name == "nt", reason="Windows 宿主路径需真实存在（#726 fail-closed），合成键词法等价在 POSIX 宿主钉住")
@pytest.mark.asyncio
async def test_windows_form_cannot_bypass(tmp_path) -> None:
    """盘符大小写 / 分隔符方向 / 尾分隔符 / .. 段 / UNC 写法同键。"""
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", "D:\\code\\proj")
    for i, variant in enumerate(
        (
            "d:/code/proj",
            "D:\\code\\proj\\",
            "d:\\code\\.\\proj",
            "D:/code/other/../proj",
        )
    ):
        outcome = await manager.acquire(f"task-b{i}", variant)
        assert not outcome.granted, variant
    await manager.acquire("task-c0", "\\\\server\\share\\dir")
    for i, variant in enumerate(
        ("//server/share/dir", "\\\\SERVER\\share\\dir")
    ):
        outcome = await manager.acquire(f"task-c{i + 1}", variant)
        assert not outcome.granted, variant


@pytest.mark.skipif(os.name == "nt", reason="Windows 宿主路径需真实存在（#726 fail-closed），合成键词法等价在 POSIX 宿主钉住")
@pytest.mark.asyncio
async def test_windows_parent_child_rejected(tmp_path) -> None:
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", "D:\\code")
    with pytest.raises(LeasePathConflict):
        await manager.acquire("task-b", "d:/code/proj")


@pytest.mark.asyncio
async def test_fail_closed_missing_path_rejected(tmp_path) -> None:
    """manager 级：缺失路径 fail-closed。不依赖链接能力，全宿主执行
    （#731 拆分：原用例捆绑链接环半边，无特权宿主整测跳过丢失本覆盖）。"""
    manager = await new_manager(tmp_path)
    with pytest.raises(LeasePathError):
        await manager.acquire("task-a", str(tmp_path / "missing"))


@pytest.mark.asyncio
@needs_dir_link
async def test_fail_closed_link_loop_rejected(tmp_path) -> None:
    """manager 级：链接环 fail-closed（junction 环语义同 symlink 环，#731）。"""
    manager = await new_manager(tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    # 悬空端构造：_dir_link_created 用 lstat 核验 reparse point，不依赖目标存在。
    if not make_dir_link(a, b) or not make_dir_link(b, a):
        pytest.skip("宿主无法创建目录链接（探测与使用间能力变化）")
    with pytest.raises(LeasePathError):
        await manager.acquire("task-a", str(a))


# —— 重启（crash/restart）与对账 ——


@pytest.mark.asyncio
async def test_restart_preserves_holder_and_queue_order(tmp_path) -> None:
    """crash/restart 后：持有租约与队列顺序不变（SQLite 持久 + 新实例重放）。"""
    manager = await new_manager(tmp_path)
    for name in ("task-a", "task-b", "task-c"):
        await manager.acquire(name, str(tmp_path))
    # 模拟进程崩溃：同一 DB 文件上重建 manager（无内存状态可继承）。
    revived = await new_manager(tmp_path, NoPresenceReader())
    held = [l for l in await revived.leases() if l.state == LEASE_HELD]
    assert [(l.session_id, l.state) for l in held] == [("task-a", LEASE_HELD)]
    assert (await revived.status("task-b")).queue_position == 1
    assert (await revived.status("task-c")).queue_position == 2
    # 释放后队首（无在场）不启动，顺序依旧成立。
    outcome = await revived.release("task-a")
    assert outcome.promoted_to is None
    assert (await revived.status("task-b")).queue_position == 1
    assert (await revived.status("task-c")).queue_position == 2


@pytest.mark.asyncio
async def test_reconcile_drops_deleted_sessions_keeps_valid(tmp_path) -> None:
    """重启对账：已删会话的持有/排队行删除；有效会话租约原样保留。"""
    db, wa, wb, wg = (
        tmp_path / "db",
        tmp_path / "wa",
        tmp_path / "wb",
        tmp_path / "wg",
    )
    for d in (wa, wb, wg):
        d.mkdir()
    manager = await new_manager(db)
    await manager.acquire("task-a", str(wa))
    await manager.acquire("task-b", str(wb))
    await manager.acquire("task-gone", str(wg))  # wg 队列 + 持有
    dropped = await manager.reconcile_on_restart({"task-a", "task-b"})
    assert dropped == 1  # task-gone 的持有行（wg 无人排队，单行）
    held = [l for l in await manager.leases() if l.state == LEASE_HELD]
    assert sorted(l.session_id for l in held) == ["task-a", "task-b"]
    assert (await manager.status("task-gone")).held_dir_key is None
    assert (await manager.status("task-gone")).queued_dir_key is None


@pytest.mark.asyncio
async def test_reconcile_detects_corrupt_awaiting_without_queue(tmp_path) -> None:
    """不变量破坏（awaiting 无队首）大声失败，不静默修补。"""
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(tmp_path))
    await manager.acquire("task-b", str(tmp_path))
    await manager.release("task-a")
    # 直接改库制造损坏（绕过语义层的事务保证）。
    import aiosqlite

    async with aiosqlite.connect(tmp_path / "harness.db") as conn:
        await conn.execute("DELETE FROM workspace_lease_queue")
        await conn.commit()
    with pytest.raises(LeaseError):
        await manager.reconcile_on_restart({"task-a", "task-b"})


# —— 存储约束（唯一写者的最后一道 CAS）——


@pytest.mark.asyncio
async def test_unique_partial_index_blocks_duplicate_waiting(tmp_path) -> None:
    """主体是 SQLite 部分唯一索引（平台无关）：用真实目录保持全宿主可跑。"""
    real = tmp_path / "real"
    real.mkdir()
    store = SqliteLeaseStore(tmp_path / "harness.db")
    manager = WorkspaceLeaseManager(store)
    await manager.initialize()
    await manager.acquire("task-a", str(real))
    await manager.acquire("task-b", str(real))  # 同键 → task-b 排队（waiting 行）
    import aiosqlite

    with pytest.raises(aiosqlite.IntegrityError):  # 部分唯一索引：同 session 只能有一个 waiting 行
        await store.insert_waiting(normalize_dir_key(str(real)), "task-b", "now")


@pytest.mark.asyncio
async def test_session_holding_other_dir_rejected(tmp_path) -> None:
    """一个 Task 只持有一个目录的租约；跨目录申请被拒（409 语义）。"""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    manager = await new_manager(tmp_path)
    await manager.acquire("task-a", str(a))
    with pytest.raises(LeaseAlreadyHeldElsewhere):
        await manager.acquire("task-a", str(b))
    # 原租约不受影响。
    held = [l for l in await manager.leases() if l.state == LEASE_HELD]
    assert [(l.session_id, normalize_dir_key(str(a)) == l.dir_key) for l in held] == [
        ("task-a", True)
    ]


@pytest.mark.asyncio
async def test_two_creates_cas_single_winner(tmp_path) -> None:
    """两个同时创建（不同事件循环轮次交错）：至多一个 held 行。"""
    manager = await new_manager(tmp_path)
    results = await asyncio.gather(
        *[manager.acquire(f"task-{i}", str(tmp_path)) for i in range(8)]
    )
    assert sum(1 for r in results if r.granted) == 1
    held = [l for l in await manager.leases() if l.state == LEASE_HELD]
    assert len(held) == 1
    waiting = [q for q in await manager.queue() if q.state == QUEUE_WAITING]
    assert len(waiting) == 7
    # FIFO 顺序 = 入队顺序（queue_id 升序）。
    assert [q.session_id for q in waiting] == [f"task-{i}" for i in range(1, 8)]
