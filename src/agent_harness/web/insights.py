"""上下文洞察模块数据面（#1420）：三组只读端点。

票面（`gh issue view 1420`）把 dsh-context 的三块表面拆成独立模块，**不融进
RUN INSPECTOR**（用户裁决：层级不同 + 故障隔离 + 可单独开关）。本文件只做后端
数据面，前端在 `web/` 另段施工。

    GET /api/insights/overview?range=7d|30d|all&workspace_id=<opt>
    GET /api/sessions/{id}/context-timeline?granularity=step|turn&mode=total|delta
    GET /api/sessions/{id}/fleet

## 口径（票面「已定裁决」，不在此另立）

- **唯一 token 真相 = `SessionEvent` 流**：`model/request.data.usage` 的四个
  token 字段 + `duration_ms`。**不读** `/api/sessions/{id}/usage`（存储字节）、
  **不读** `storage/session_archive.py` 的 token/cost 列（从未写入）。
- 计费 token := `total_tokens`；缓存命中率 := Σ`cached_tokens` ÷ Σ`prompt_tokens`
  （与 `context_usage.cache_summary` 是**同一个求和公式**，不是逐调用算术平均；
  数据源不同——这里只认 `model/request` 事件，`cache_summary` 只认 `model/completed`）；
  活跃时长 := Σ`duration_ms`；按日折叠按事件 `time`（ISO-8601）解析为**本地日期**。
- **v1 无成本**：任何响应不得出现 cost/$ 字段（仓库裁决 `reports_cost=False`）。
- 构成聚合**仅基于有快照会话**的最新 context-usage 快照，分类求和 = 快照总量；
  无快照会话不计入，`coverage` 如实透出（不伪造 0）。
- Fleet 的父/子边取 `session/lineage.py` 的 `parent_session_id`，子代理数据面复用
  `web/delegations.py`（不新建追踪表）。
- 模块故障不得拖垮 Core（不变量 #21）：每个会话的读取都是宽容读——单个坏/冷会话
  让该行退化为无数据，不让整张表失败。

## 为什么是一个独立 router 文件

与 `web/lineage.py` / `web/delegations.py` 同一接缝：`create_app` 里一行注册，
`app.py` 的流式改造不碰本文件。开关（`Settings.insights_enabled`）在注册处判——
关闭时三组路由**根本不存在**（直连 404），而不是运行时返回 404。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import anyio

from agent_harness.session.errors import (
    EventLogCorruptError,
    InvalidSessionId,
    SessionNotFound,
    WorkspaceNotFound,
)
from agent_harness.session.event import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    CONTEXT_WINDOW_CHANGED,
    MODEL_REQUEST,
)
from agent_harness.web.app import session_service
from agent_harness.web.context_usage import resolve_context_usage_payload
from agent_harness.web.delegations import _project_delegations
from agent_harness.web.domain_errors import http_error

if TYPE_CHECKING:
    from fastapi import FastAPI

    from agent_harness.web.app import AppState

#: 构成聚合的固定分类（票面「构成聚合」段：快照口径 5 顶层桶 + tools 两子桶）。
#: 顺序即响应键序，前端环形图按此序上色。
COMPOSITION_KEYS = (
    "system_prompt", "tools_system", "tools_mcp", "messages", "skills", "other",
)

#: 上下文事件列表要展示的事件类型（票面「上下文事件列表」段）。prune 不在其中：
#: 它只有进程内记录、无持久化事件（票面明确「本票不展示」）。
CONTEXT_EVENT_TYPES = frozenset({
    CONTEXT_COMPACTED, COMPACTION_START, COMPACTION_END, CONTEXT_WINDOW_CHANGED,
})

#: 日序列/热力图窗口长度。
_DAILY_DAYS = {"7d": 7, "30d": 30}
_HEATMAP_DAYS = 56  # 近 8 周

RangeKey = Literal["7d", "30d", "all"]
Granularity = Literal["step", "turn"]
Mode = Literal["total", "delta"]


def register_insights_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把三组洞察路由挂到既有 app（`create_app` 侧一行调用的接入面）。"""

    @app.get("/api/insights/overview")
    async def get_insights_overview(
        range: RangeKey = "7d",
        workspace_id: str | None = None,
    ) -> dict:
        state = app.state.agent
        return await _overview(state, range=range, workspace_id=workspace_id)

    @app.get("/api/sessions/{session_id}/context-timeline")
    async def get_context_timeline(
        session_id: str,
        granularity: Granularity = "step",
        mode: Mode = "total",
    ) -> dict:
        state = app.state.agent
        events = await _session_events(state, session_id, validate_session_id)
        return {
            # 窗口构成沿用 context-usage 的**同一个**解析入口（票面：同形状 payload）
            "window": resolve_context_usage_payload(state, session_id, events),
            "requests": _request_series(events, granularity=granularity, mode=mode),
            "events": [
                {"seq": e.seq, "time": e.time, "type": e.type}
                for e in events
                if e.type in CONTEXT_EVENT_TYPES
            ],
        }

    @app.get("/api/sessions/{session_id}/fleet")
    async def get_session_fleet(session_id: str) -> dict:
        state = app.state.agent
        return await _fleet(state, session_id, validate_session_id)


# ── overview ────────────────────────────────────────────────────────


async def _overview(
    state: AppState, *, range: str, workspace_id: str | None
) -> dict:
    """跨会话聚合：一次扫全量会话，逐会话按事件流累计。

    `range` 只裁剪**时间轴上的聚合**（KPI / 日序列）与会话卡范围；热力图恒为
    近 8 周（票面：8 周活动热力图，与 range 三档无关）。
    """
    await state.ensure_stores()
    try:
        summaries = await session_service(state).list_sessions(
            workspace_id=workspace_id, include_archived=False,
        )
    except WorkspaceNotFound as e:
        # 未注册的 workspace_id ⇒ 404（与 `GET /api/sessions` 同一翻译）。
        # `list_sessions` 的契约是「不伪装成空列表」——在这里把它降级成 `[]`
        # 会让洞察页对拼错的 id 显示一张全 0 的表。
        raise http_error(e) from e
    today = datetime.now().astimezone().date()
    start = None if range == "all" else today - timedelta(days=_DAILY_DAYS[range] - 1)
    heat_start = today - timedelta(days=_HEATMAP_DAYS - 1)

    kpi = {
        "sessions": 0, "billed_tokens": 0, "cache_hit_rate": None,
        "turns": 0, "tool_runs": 0, "active_ms": 0,
    }
    daily: dict[date, int] = defaultdict(int)
    heat: dict[date, int] = defaultdict(int)
    composition = dict.fromkeys(COMPOSITION_KEYS, 0)
    snapshotted = 0
    rows: list[dict] = []
    cached_sum = 0
    prompt_sum = 0
    cached_reported = False

    for summary in summaries:
        session_id = summary.session_id
        events = await anyio.to_thread.run_sync(state.store.read_events, session_id)

        # 会话卡与 KPI 的时间轴：只计窗口内的 model/request（turns 按窗口内
        # model/completed 计）。窗口外的事件仍进热力图（8 周独立窗口）。
        in_window = _in_window(events, start)
        window_tokens = 0
        window_turns = 0
        window_tool_runs = 0
        window_active_ms = 0
        window_cached = 0
        window_prompt = 0
        for event in in_window:
            data = event.data if isinstance(event.data, dict) else {}
            if event.type == MODEL_REQUEST:
                usage = data.get("usage")
                if isinstance(usage, dict):
                    total = usage.get("total_tokens")
                    if _is_int(total):
                        window_tokens += total
                    cached = usage.get("cached_tokens")
                    if _is_int(cached):
                        window_cached += cached
                        cached_reported = True
                    prompt = usage.get("prompt_tokens")
                    if _is_int(prompt):
                        window_prompt += prompt
                duration = data.get("duration_ms")
                if _is_int(duration):
                    window_active_ms += duration
            elif event.type == "model/completed":
                window_turns += 1
            elif event.type == "tool/result":
                delta = data.get("budget_delta")
                if isinstance(delta, dict):
                    calls = delta.get("tool_calls")
                    if _is_int(calls) and calls > 0:
                        window_tool_runs += calls

        # 日折叠 / 热力图：扫**全部**事件，不扫 `in_window`——热力图恒为近 8 周，
        # 比 range=7d 宽；daily 多出的窗口外日期由 `_date_series(start=...)` 裁掉。
        for event in events:
            if event.type != MODEL_REQUEST:
                continue
            usage = event.data.get("usage") if isinstance(event.data, dict) else None
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if not _is_int(total):
                continue
            day = _parse_date(event.time)
            if day is None:
                continue
            daily[day] += total
            if day >= heat_start:
                heat[day] += total

        cached_sum += window_cached
        prompt_sum += window_prompt
        if in_window:
            kpi["sessions"] += 1
        kpi["billed_tokens"] += window_tokens
        kpi["turns"] += window_turns
        kpi["tool_runs"] += window_tool_runs
        kpi["active_ms"] += window_active_ms

        rows.append({
            "session_id": session_id,
            # 标题 = 首条用户消息（与 `GET /api/sessions` 的 first_user_message 同源）；
            # 没有则回落 session_id（列表总得有个可读标签，不伪造"未命名"）。
            "title": summary.first_user_message or session_id,
            "workspace": summary.workspace.title if summary.workspace else None,
            "billed_tokens": window_tokens,
            "turns": window_turns,
            "last_active_at": summary.last_event_time,
        })

        # 构成：仅统计有快照会话（票面）。快照与工具定义走与 context-usage 同一个
        # 解析入口 ⇒ 对账时逐桶相等是构造保证，不是巧合。
        snapshot = _composition_of(state, session_id, events)
        if snapshot is not None:
            snapshotted += 1
            for key in COMPOSITION_KEYS:
                composition[key] += snapshot[key]

    # 一次都没带回缓存明细 ⇒ None（诚实口径：不显示 0%，与 context-usage 的
    # `cache.state=not_collected` 同一条纪律）。
    kpi["cache_hit_rate"] = (
        cached_sum / prompt_sum if cached_reported and prompt_sum > 0 else None
    )
    rows.sort(key=lambda r: r["last_active_at"] or "", reverse=True)
    return {
        "kpi": kpi,
        "daily": _date_series(daily, start or _earliest(daily, today), today),
        "heatmap": _date_series(heat, heat_start, today),
        "composition": composition,
        "coverage": {
            "snapshotted_sessions": snapshotted,
            "total_sessions": len(summaries),
        },
        "sessions": rows,
    }


def _composition_of(
    state: AppState, session_id: str, events: list[Any]
) -> dict[str, int] | None:
    """某会话当前窗口构成 → 固定六桶；`state != "ok"` ⇒ None（不计入、不伪造 0）。"""
    payload = resolve_context_usage_payload(state, session_id, events)
    if payload["state"] != "ok":
        return None
    breakdown = payload["breakdown"]
    return {
        "system_prompt": breakdown["system_prompt"],
        "tools_system": breakdown["tools"]["system"],
        "tools_mcp": breakdown["tools"]["mcp"],
        "messages": breakdown["messages"],
        "skills": breakdown["skills"],
        "other": breakdown["other"],
    }


def _in_window(events: list[Any], start: date | None) -> list[Any]:
    if start is None:
        return events
    out = []
    for event in events:
        day = _parse_date(event.time)
        if day is not None and day >= start:
            out.append(event)
    return out


def _date_series(buckets: dict[date, int], start: date, end: date) -> list[dict]:
    """[start, end] 按日补齐（无数据日 = 真 0，不是伪造——它就是那天没请求）。"""
    out = []
    day = start
    while day <= end:
        out.append({"date": day.isoformat(), "tokens": buckets.get(day, 0)})
        day += timedelta(days=1)
    return out


def _earliest(buckets: dict[date, int], fallback: date) -> date:
    return min(buckets) if buckets else fallback


def _parse_date(value: Any) -> date | None:
    """ISO-8601 事件时间 → **本地**日期。解析失败返回 None（该事件不进日折叠）。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # 无时区按本地解释（与 time 写入口径一致）
    return parsed.astimezone().date()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ── context-timeline ────────────────────────────────────────────────


def _request_series(
    events: list[Any], *, granularity: str, mode: str
) -> list[dict]:
    """逐请求 token 序列（票面字段：prompt/completion/cached/total + duration_ms）。

    `granularity=step`：每个 `model/request` 一条。
    `granularity=turn`：同一 `run_id` 的多条折叠成**最后一条**，`duration_ms`
    求和（dsh `aggregateByTurn` 的形状语义：一个 run = 一个 turn，折叠取末条
    的用量快照——它是该轮结束时窗口的真实状态）。
    `mode=delta`：与上一条做**有符号**差；首条无基线记 0。`duration_ms` 不是
    累计量，恒报原值（不参与差分）。

    先过滤**没有 `usage.total_tokens`** 的 `model/request`：那是 provider 失败 /
    fallback 失败写下的尝试（`runtime.py::record_request_failed`），不是一条可画
    的 token 请求——补 0 会画出假 0 柱、delta 出负值、turn 折叠把整轮归零。被过滤
    的尝试其 `duration_ms` 也不计入（它没有用量，不是这一轮的有效观测）。
    """
    series = [
        _request_row(event)
        for event in events
        if event.type == MODEL_REQUEST and _has_usage(event)
    ]
    if granularity == "turn":
        series = _collapse_by_run(events, series)
    if mode == "delta":
        series = _delta(series)
    return series


def _request_row(event: Any) -> dict:
    data = event.data if isinstance(event.data, dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "seq": event.seq,
        "time": event.time,
        "prompt_tokens": _int_or_zero(usage.get("prompt_tokens")),
        "completion_tokens": _int_or_zero(usage.get("completion_tokens")),
        "cached_tokens": _int_or_zero(usage.get("cached_tokens")),
        "total_tokens": _int_or_zero(usage.get("total_tokens")),
        "duration_ms": _int_or_zero(data.get("duration_ms")),
    }


def _collapse_by_run(events: list[Any], series: list[dict]) -> list[dict]:
    """按 `run_id` 折叠：同 run 取最后一条的用量，`duration_ms` 求和。"""
    run_of_seq = {
        e.seq: e.run_id for e in events if e.type == MODEL_REQUEST
    }
    out: list[dict] = []
    index: dict[str, int] = {}
    for row in series:
        run_id = run_of_seq.get(row["seq"])
        if run_id is None:
            # 无 run 归属的请求自成一格（不并进别人的轮次）
            out.append(dict(row))
            continue
        if run_id in index:
            merged = out[index[run_id]]
            merged.update({k: row[k] for k in _TOKEN_KEYS})
            merged["seq"] = row["seq"]
            merged["time"] = row["time"]
            merged["duration_ms"] += row["duration_ms"]
        else:
            index[run_id] = len(out)
            out.append(dict(row))
    return out


_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")


def _delta(series: list[dict]) -> list[dict]:
    """有符号差分（首条基线为空 ⇒ 全 0）。`duration_ms` 原样透出。"""
    out: list[dict] = []
    previous: dict | None = None
    for row in series:
        if previous is None:
            current = {**row, **dict.fromkeys(_TOKEN_KEYS, 0)}
        else:
            current = {
                **row,
                **{k: row[k] - previous[k] for k in _TOKEN_KEYS},
            }
        out.append(current)
        previous = row
    return out


def _has_usage(event: Any) -> bool:
    """该请求是否带可画的用量（`usage.total_tokens` 为 int）。

    失败尝试（`runtime.py::record_request_failed`）只写 `outcome/http_status/
    duration_ms`，没有 `usage` ⇒ 不是 token 请求，不进序列。
    """
    usage = event.data.get("usage") if isinstance(event.data, dict) else None
    return isinstance(usage, dict) and _is_int(usage.get("total_tokens"))


def _int_or_zero(value: Any) -> int:
    return value if _is_int(value) else 0


# ── fleet ───────────────────────────────────────────────────────────


async def _fleet(
    state: AppState, session_id: str, validate_session_id: Callable[[str], str]
) -> dict:
    """当前 agent + 祖先链 + 子代理（票面三段；根会话的 ancestors 为空数组）。"""
    from agent_harness.session.lineage import (
        build_lineage_index,
        build_lineage_tree,
    )

    events = await _session_events(state, session_id, validate_session_id)
    await state.ensure_stores()
    metas = await build_lineage_index(state.store, state.session_meta_store)
    roots = build_lineage_tree(metas)

    ancestors: list[dict] = []
    node = _find(roots, session_id)
    while node is not None and node.parent_session_id is not None:
        parent = _find(roots, node.parent_session_id)
        if parent is None:
            break  # orphan：链在此截断（provenance 仍在本节点字段里）
        ancestors.insert(0, await _card(state, parent.session_id, "tokens"))
        node = parent

    children = []
    for entry in _project_delegations(events):
        child_id = entry["child_session_id"]
        card = await _card(state, child_id, "used_tokens")
        card["status"] = entry["status"]
        children.append(card)

    return {
        "self": await _card(state, session_id, "tokens", events=events),
        "ancestors": ancestors,
        "children": children,
    }


async def _card(
    state: AppState,
    session_id: str,
    tokens_key: str,
    *,
    events: list[Any] | None = None,
) -> dict:
    """一张 fleet 卡：`status` / `<tokens_key>` / `last_active_at`。

    字段清单以票面契约为准（**没有** `state` 位）。`tokens` 数值照抄 context-usage
    的 `used_tokens`：有快照是精确窗口占用，无快照是 usage_only 的最近一次输入规模；
    `no_data` 时该数也是 0——本卡不区分"没有数据"与"占用为 0"（票面未给字段）。
    `last_active_at` 是该会话最后一条事件时间（票面："最近活动"即此字段）。
    """
    if events is None:
        events = await anyio.to_thread.run_sync(state.store.read_events, session_id)
    payload = resolve_context_usage_payload(state, session_id, events)
    return {
        "session_id": session_id,
        "status": "running" if state.run_manager.get_active(session_id) else "idle",
        tokens_key: payload["used_tokens"],
        "last_active_at": events[-1].time if events else None,
    }


def _find(nodes: list[Any], session_id: str) -> Any:
    for node in nodes:
        if node.session_id == session_id:
            return node
        found = _find(node.children, session_id)
        if found is not None:
            return found
    return None


async def _session_events(
    state: AppState, session_id: str, validate_session_id: Callable[[str], str]
) -> list[Any]:
    """会话事件（错误翻译与其余会话读端点同源：422 / 404 / 409）。"""
    try:
        validate_session_id(session_id)
    except InvalidSessionId as e:
        raise http_error(e) from e
    try:
        return await session_service(state).get_events(session_id)
    except (InvalidSessionId, SessionNotFound, EventLogCorruptError) as e:
        raise http_error(e) from e
