"""Web 子智能体 roster 数据面（#1256）：只读端点
``GET /api/sessions/{session_id}/delegations``。

roster（某父会话的子智能体列表）是 UI 查询，不是运行事实 ⇒ 不进 SessionEvent
（不变量 #4：Event ≠ Diagnostic Log），只经本端点暴露。数据全部来自**父会话的
append-only 事件流**（不变量 #3/#22：界面是投影，不建第二套真相）：

- 配对 ``agent/delegation-started`` / ``agent/delegation-finished``（按
  ``child_session_id`` 键控——并行委派下 start/finish 按完成顺序落盘，位置不可
  假设，只有它是稳定配对键，与 ``projection.ts`` 同一口径）得
  target/task/status/时间戳/summary；
- ``used_tokens`` / ``usage_state`` **复用上下文容量看板数据面**
  （``build_context_usage_payload``），逐子会话批量产出——**不做前端 N+1 轮询**；
- ``usage_state`` 诚实透出：``ok``（有进程内快照的精确分类）/ ``usage_only``
  （事件流有 usage ⇒ 窗口占用下界）/ ``no_data``（两者都没有 / 子会话日志不可读）。

零新存储：本端点不写任何事件、不建任何表/文件。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import anyio

from agent_harness.context.tokens import estimate_tokens
from agent_harness.session.errors import (
    EventLogCorruptError,
    InvalidSessionId,
    SessionNotFound,
)
from agent_harness.session.event import (
    AGENT_DELEGATION_FINISHED,
    AGENT_DELEGATION_STARTED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
)
from agent_harness.web.app import session_service
from agent_harness.web.context_usage import build_context_usage_payload
from agent_harness.web.domain_errors import http_error

if TYPE_CHECKING:
    from fastapi import FastAPI

    from agent_harness.web.app import AppState

# run 终态（= 前端投影触发「未回填委派翻 stopped」的那组，projection.ts:2139-2170）。
_RUN_TERMINAL = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED})


def register_delegation_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把 roster 路由挂到既有 app（app.py 侧一行调用的接入面）。"""

    @app.get("/api/sessions/{session_id}/delegations")
    async def get_session_delegations(session_id: str) -> list[dict]:
        """某父会话的子智能体列表（只读）。404 = 父会话不存在；409 = 父日志损坏。

        校验/错误翻译与其余会话读端点同源（``http_error`` 单一映射表）。
        """
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        state = app.state.agent
        service = session_service(state)
        try:
            events = await service.get_events(session_id)
        except (InvalidSessionId, SessionNotFound, EventLogCorruptError) as e:
            raise http_error(e) from e

        entries = _project_delegations(events)
        for entry in entries:
            await _attach_usage(entry, state)
        return entries


def _project_delegations(events: list[Any]) -> list[dict]:
    """父事件流 → 委派条目列表（真事件序）。``_start_pos`` 仅用于判 stopped，出口剔除。"""
    entries: list[dict] = []
    by_child: dict[str, dict] = {}
    terminal_positions = [
        pos for pos, event in enumerate(events) if event.type in _RUN_TERMINAL
    ]

    for pos, event in enumerate(events):
        data = event.data if isinstance(event.data, dict) else {}
        child = data.get("child_session_id")
        if not isinstance(child, str) or not child:
            continue  # 契约必有；缺失不造条目（事件仍在日志里）
        if event.type == AGENT_DELEGATION_STARTED:
            if child in by_child:
                continue  # 重复 start 只认首条（与投影 some() 幂等一致）
            entry = {
                "child_session_id": child,
                "target": _as_str(data.get("target")),
                "task": _as_str(data.get("task")),
                "status": "running",
                "started_at": event.time,
                "completed_at": None,
                "_start_pos": pos,
            }
            by_child[child] = entry
            entries.append(entry)
        elif event.type == AGENT_DELEGATION_FINISHED:
            status = "failed" if data.get("status") == "failed" else "completed"
            entry = by_child.get(child)
            if entry is None:
                # 防御：finished 先于 started（截断历史/乱序持久化）——按 finish 真值
                # 建终态节点（target/status/summary 事件自带），不虚构 task/started。
                entry = {
                    "child_session_id": child,
                    "target": _as_str(data.get("target")),
                    "task": "",
                    "status": status,
                    "started_at": None,
                    "completed_at": event.time,
                    "_start_pos": pos,
                }
                by_child[child] = entry
                entries.append(entry)
            else:
                entry["status"] = status
                entry["completed_at"] = event.time
            summary = data.get("summary")
            if isinstance(summary, str) and summary:
                entry["summary"] = summary

    # 未回填的 running：父 run 在 started **之后**到达终态 ⇒ stopped（中断 ≠ 失败，
    # 与投影同一四态语言；阻塞串行模型下不常见，但 kill/崩溃路径真实存在）。
    for entry in entries:
        if (
            entry["status"] == "running"
            and any(tp > entry["_start_pos"] for tp in terminal_positions)
        ):
            entry["status"] = "stopped"
        entry.pop("_start_pos", None)
    return entries


async def _attach_usage(entry: dict, state: AppState) -> None:
    """逐子会话产 ``used_tokens`` / ``usage_state``（复用看板数据面，不重写口径）。

    子会话事件用**宽容读**（缺文件 → 空列表 → ``no_data``）：一个已清理/cold 的
    子会话不能让整张 roster 失败（对齐 lineage 对孤儿节点的处置纪律）。
    """
    child_id = entry["child_session_id"]
    child_events = await anyio.to_thread.run_sync(state.store.read_events, child_id)
    builder_snapshot: dict[str, Any] | None = None
    tool_definitions: list[dict[str, Any]] = []
    cached = state.context_snapshots.get(child_id)
    if cached is not None:
        builder_snapshot, tool_definitions = cached
    payload = build_context_usage_payload(
        settings=state.settings,
        builder_snapshot=builder_snapshot,
        tool_definitions=tool_definitions,
        estimate_tokens=estimate_tokens,
        events=child_events,
    )
    entry["used_tokens"] = payload["used_tokens"]
    entry["usage_state"] = payload["state"]


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""
