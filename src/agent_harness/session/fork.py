"""Fork：从父 Session 事件前缀派生新独立会话（Phase 14, ADR-0017）。

file-per-lineage（决策 1）：fork = 新 session 文件；每个 session 保持
append-only 线性 JSONL，树是 SessionMetaStore 索引层的元数据关系。

- boundary（决策 2）：机制上 seed 前缀必须止于 run 终态之后（child 文件
  绝不以悬空 run 开头）；UX 选择器 = 「从第 N 条用户消息分叉」——锚点
  消息本身不进 seed（child 侧由用户重新发送，pi /fork 同款语义）。
- seed（决策 3）：事件前缀逐字复制进 child（Session.adopt_history 重编
  seq、保留原 event_id），child 自包含可读，不依赖父文件存活。
- provenance（决策 8）：session/forked 只落 child 文件，父文件一字不改
  （父会话以 store.read_events 只读加载——绝不能 resume，那会写父）。
- 索引（决策 7）：fork 同时 upsert SessionMeta（origin=fork）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    SESSION_FORKED,
    SESSION_STARTED,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.session.session import Session
from agent_harness.storage.session_meta import SessionMeta

if TYPE_CHECKING:
    from agent_harness.sandbox.registry import WorkspaceRegistry
    from agent_harness.session.store import JsonlSessionStore
    from agent_harness.storage.session_meta import SessionMetaStore


class ForkBoundaryError(ValueError):
    """非法 fork 边界：锚点不存在 / 不是用户消息 / 前缀含未终态 run。"""


def find_fork_boundaries(events: list[SessionEvent]) -> list[int]:
    """列出合法 fork 锚点（用户消息 seq，锚点语义：seed = [0, seq)）。

    规则：锚点处的 seed 前缀必须 run 完整——逐事件跟踪 run/started 与
    run/completed|failed 的开合计数，计数为 0 时遇到的用户消息才是合法切点。
    """
    boundaries: list[int] = []
    open_runs = 0
    for event in events:
        if event.type == RUN_STARTED:
            open_runs += 1
        elif event.type in (RUN_COMPLETED, RUN_FAILED):
            open_runs -= 1
        elif event.type == USER_MESSAGE and open_runs == 0:
            boundaries.append(event.seq)
    return boundaries


async def fork_session(
    store: JsonlSessionStore,
    meta_store: SessionMetaStore,
    parent_session_id: str,
    *,
    boundary_user_message_seq: int,
    child_session_id: str | None = None,
    agent_id: str = "default",
    workspace_registry: WorkspaceRegistry | None = None,
) -> Session:
    """从父会话的第 boundary_user_message_seq 条用户消息处 fork 出 child。

    锚点消息不进 seed；seed = 锚点之前的全部事件（父的 session/started 除
    外——child 写自己的身份事件）。失败时不留任何 child 侧孤儿（先校验后
    落盘）。
    """
    # 父会话只读加载（§7 父不可改：绝不能 Session.resume，那会追加 resumed）
    parent_events = store.read_events(parent_session_id)
    if not parent_events:
        raise ForkBoundaryError(
            f"Session '{parent_session_id}' 不存在或事件日志为空"
        )

    anchor = next(
        (
            e
            for e in parent_events
            if e.seq == boundary_user_message_seq
        ),
        None,
    )
    if anchor is None or anchor.type != USER_MESSAGE:
        available = find_fork_boundaries(parent_events)
        raise ForkBoundaryError(
            f"fork 边界 seq={boundary_user_message_seq} 不是父会话中的用户消息"
            f"（可用边界: {available}）"
        )

    seed = [
        e
        for e in parent_events
        if e.seq < anchor.seq and e.type != SESSION_STARTED
    ]
    _validate_run_complete(seed, parent_session_id)

    # 校验全部通过后才落盘：先建 child，再移植 seed，再写 provenance 与索引
    child = Session.start(
        store, agent_id=agent_id, session_id=child_session_id,
        workspace_registry=workspace_registry,
    )
    child.adopt_history(seed)
    fork_point_seq = seed[-1].seq if seed else None
    child.append(
        SESSION_FORKED,
        {
            "parent_session_id": parent_session_id,
            "boundary_user_message_seq": anchor.seq,
            "fork_point_seq": fork_point_seq,
        },
        agent_id=agent_id,
    )

    await meta_store.upsert(
        SessionMeta(
            session_id=child.session_id,
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            origin="fork",
            fork_point_seq=fork_point_seq,
        )
    )
    return child


def _validate_run_complete(
    seed: list[SessionEvent], parent_session_id: str
) -> None:
    open_runs = 0
    for event in seed:
        if event.type == RUN_STARTED:
            open_runs += 1
        elif event.type in (RUN_COMPLETED, RUN_FAILED):
            open_runs -= 1
        if open_runs < 0:
            raise ForkBoundaryError(
                f"父会话 '{parent_session_id}' 前缀 run 事件序非法（负计数）"
            )
    if open_runs > 0:
        raise ForkBoundaryError(
            f"父会话 '{parent_session_id}' 前缀以未终态 run 结尾"
            "（child 不允许以悬空 run 开头）"
        )
