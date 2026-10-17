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
    RUN_PAUSED,
    RUN_TERMINAL_TYPES,
)
from agent_harness.web.app import session_service
from agent_harness.web.context_usage import build_context_usage_payload
from agent_harness.web.domain_errors import http_error

if TYPE_CHECKING:
    from fastapi import FastAPI

    from agent_harness.web.app import AppState

# 「把在途委派翻 stopped」的触发事件集 = run 终态（RUN_TERMINAL_TYPES，单一事实源，§9.5）
# ∪ run/paused。paused **不是** run 终态，但前端投影的 projectRunPaused 同样走 finalizeRun
# 把 running 委派一并翻 stopped ⇒ roster 判据必须与投影同源。仓库既有同构先例：
# session/interrupt.py 与 session/fork.py 均写 ``event.type in RUN_TERMINAL_TYPES or event.type == RUN_PAUSED``。
_STOPPED_RUN_TRIGGERS = RUN_TERMINAL_TYPES | {RUN_PAUSED}


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
            await _attach_usage(entry, state, validate_session_id=validate_session_id)
        return entries


def _project_delegations(events: list[Any]) -> list[dict]:
    """父事件流 → 委派条目列表（真事件序）。``_start_pos`` 仅用于判 stopped，出口剔除。"""
    entries: list[dict] = []
    by_child: dict[str, dict] = {}
    # 停止触发事件（run 终态 ∪ paused）的**位置**：供尾部判「在途委派是否被其收口」，
    # 并按其 time 结算 completed_at。
    stopped_trigger_positions = [
        pos for pos, event in enumerate(events) if event.type in _STOPPED_RUN_TRIGGERS
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

    # 未回填的 running：父 run 在 started **之后**到达停止触发（终态或暂停）⇒ stopped
    # （中断 ≠ 失败，与投影同一四态语言；阻塞串行模型下不常见，但 kill/崩溃/预算暂停
    # 路径真实存在）。completed_at 冻结在**第一条**触发事件的 time——函数契约「其余用
    # 起点→终点」要求终态条目有真终点，且与前端 finalizeRun settle 同源（不是 return null）。
    for entry in entries:
        if entry["status"] == "running":
            trigger_pos = next(
                (tp for tp in stopped_trigger_positions if tp > entry["_start_pos"]),
                None,
            )
            if trigger_pos is not None:
                entry["status"] = "stopped"
                entry["completed_at"] = events[trigger_pos].time
        entry.pop("_start_pos", None)
    return entries


async def _attach_usage(
    entry: dict, state: AppState, *, validate_session_id: Callable[[str], str]
) -> None:
    """逐子会话产 ``used_tokens`` / ``usage_state``（复用看板数据面，不重写口径）。

    子会话事件用**宽容读**（缺文件 → 空列表 → ``no_data``）：一个已清理/cold 的
    子会话不能让整张 roster 失败（对齐 lineage 对孤儿节点的处置纪律）。
    """
    child_id = entry["child_session_id"]
    # P3-2 纵深：父日志 data 里的 child_session_id 此前只做过 isinstance 检查就进
    # read_events（其内部经 store 的 _events_path 拼路径，无净化）——含 ``../`` 会读到
    # store 根之外。非法 id ⇒ 用量如实 no_data（roster 行照列，不 brick 整张表）。
    try:
        validate_session_id(child_id)
    except InvalidSessionId:
        entry["used_tokens"] = 0
        entry["usage_state"] = "no_data"
        return
    # P3-1：与 context-usage 端点同源（同一个 run 的 builder + registry）——优先在途 run
    # 的 live builder 快照（→ ok 精确值）；run 已收口才退 context_snapshots 缓存；都没有
    # 才按事件流落 usage_only/no_data。roster 的运行中行与看板数值必须同源（AC2）。
    builder_snapshot: dict[str, Any] | None = None
    tool_definitions: list[dict[str, Any]] = []
    active = state.run_manager.get_active(child_id)
    if active is not None and active.runtime is not None:
        builder = active.runtime._context_builder
        if builder is not None:
            builder_snapshot = builder.usage_snapshot(active.session)
        tool_definitions = active.runtime.registry.export_model_definitions()
    else:
        cached = state.context_snapshots.get(child_id)
        if cached is not None:
            builder_snapshot, tool_definitions = cached
    child_events = await anyio.to_thread.run_sync(state.store.read_events, child_id)
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
