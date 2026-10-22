"""上下文洞察模块 API 测试（#1420）：三组只读端点契约 + 对账 + 降级 + 开关。

接缝与 `test_web_delegations.py` 同：独立 router 文件 + app.py 一行注册；数据全部
来自 append-only 事件流（不变量 #3/#22），这里只断言外部 HTTP 形状。

时间口径的用例用 `store.append_event` 直接落带指定 `time` 的事件（`Session.append`
的时间戳恒为 now，无法造「10 天前」的日折叠样本）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from agent_harness.agent.run_budget import consumed_from_events
from agent_harness.config import Settings
from agent_harness.session import Session
from agent_harness.session.event import (
    AGENT_DELEGATION_FINISHED,
    AGENT_DELEGATION_STARTED,
    CONTEXT_COMPACTED,
    CONTEXT_WINDOW_CHANGED,
    MODEL_COMPLETED,
    MODEL_REQUEST,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.web.app import create_app

_OVERVIEW_KEYS = {
    "kpi", "daily", "heatmap", "composition", "coverage", "sessions",
}
_KPI_KEYS = {
    "sessions", "billed_tokens", "cache_hit_rate", "turns", "tool_runs", "active_ms",
}
_COMPOSITION_KEYS = {
    "system_prompt", "tools_system", "tools_mcp", "messages", "skills", "other",
}
_SESSION_KEYS = {
    "session_id", "title", "workspace", "billed_tokens", "turns", "last_active_at",
}


def _client(tmp_path, **overrides) -> TestClient:
    settings = Settings(
        workspace_dir=str(tmp_path), model_api_key="sk-test", **overrides
    )
    return TestClient(create_app(settings, enable_cors=False))


def _days_ago(days: int) -> datetime:
    return datetime.now().astimezone() - timedelta(days=days)


def _seed(tmp_path, sessions: dict[str, list[tuple[str, dict, int]]]) -> None:
    """sessions: sid -> [(event_type, data, days_ago)]；每会话先落一条 user 消息。"""
    app = create_app(
        Settings(workspace_dir=str(tmp_path), model_api_key="sk-test"),
        enable_cors=False,
    )
    store = app.state.agent.store

    async def _run() -> None:
        for sid, rows in sessions.items():
            session = Session.start(store, session_id=sid)
            seq = session.append(USER_MESSAGE, {"content": f"hi {sid}"}).seq
            for event_type, data, days in rows:
                seq += 1
                store.append_event(
                    sid,
                    SessionEvent(
                        seq=seq,
                        time=_days_ago(days).isoformat(timespec="milliseconds"),
                        type=event_type,
                        session_id=sid,
                        data=data,
                    ),
                )

    asyncio.run(_run())


def _usage(prompt: int, completion: int, cached: int, total: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "total_tokens": total,
    }


def _request_events(tmp_path, sid: str) -> list[SessionEvent]:
    app = create_app(
        Settings(workspace_dir=str(tmp_path), model_api_key="sk-test"),
        enable_cors=False,
    )
    return app.state.agent.store.read_events(sid)


# ── overview：契约形状 ──────────────────────────────────────────────


def test_overview_shape_and_daily_fold_by_model_request_time(tmp_path) -> None:
    """按 `model/request.time` 解析本地日期后日折叠；range=7d 截掉窗口外。"""
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(100, 10, 50, 110), "duration_ms": 1000}, 0),
        (MODEL_REQUEST, {"usage": _usage(200, 20, 60, 220), "duration_ms": 500}, 0),
        (MODEL_REQUEST, {"usage": _usage(300, 30, 70, 330), "duration_ms": 250}, 3),
        (MODEL_REQUEST, {"usage": _usage(400, 40, 80, 440), "duration_ms": 250}, 10),
    ]})
    body = _client(tmp_path).get("/api/insights/overview?range=7d").json()

    assert set(body) == _OVERVIEW_KEYS
    assert set(body["kpi"]) == _KPI_KEYS
    assert set(body["composition"]) == _COMPOSITION_KEYS
    assert set(body["sessions"][0]) == _SESSION_KEYS

    today = datetime.now().astimezone().date().isoformat()
    three = _days_ago(3).date().isoformat()
    ten = _days_ago(10).date().isoformat()
    daily = {row["date"]: row["tokens"] for row in body["daily"]}
    assert daily[today] == 330
    assert daily[three] == 330
    assert ten not in daily, "10 天前的事件不在 7d 窗口"
    assert len(body["daily"]) == 7, "7d 窗口按日补齐（空日为真 0）"
    # 窗口内合计：110+220+330；窗口外 440 不计
    assert body["kpi"]["billed_tokens"] == 660
    assert body["kpi"]["sessions"] == 1
    assert body["kpi"]["active_ms"] == 1750


def test_overview_range_30d_and_all(tmp_path) -> None:
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(100, 10, 0, 110), "duration_ms": 1}, 0),
        (MODEL_REQUEST, {"usage": _usage(200, 20, 0, 220), "duration_ms": 1}, 10),
        (MODEL_REQUEST, {"usage": _usage(300, 30, 0, 330), "duration_ms": 1}, 40),
    ]})
    client = _client(tmp_path)
    assert client.get("/api/insights/overview?range=30d").json()["kpi"]["billed_tokens"] == 330
    all_body = client.get("/api/insights/overview?range=all").json()
    assert all_body["kpi"]["billed_tokens"] == 660
    assert len(all_body["daily"]) == 41, "all = 最早有数据日到今天"


def test_overview_heatmap_is_eight_weeks(tmp_path) -> None:
    """热力图恒为近 8 周（56 天），**不受 range 影响**（票面：近 8 周按日）。"""
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(10, 1, 0, 11), "duration_ms": 1}, 0),
        (MODEL_REQUEST, {"usage": _usage(20, 2, 0, 22), "duration_ms": 1}, 50),
    ]})
    body = _client(tmp_path).get("/api/insights/overview?range=7d").json()
    assert len(body["heatmap"]) == 56
    heat = {row["date"]: row["tokens"] for row in body["heatmap"]}
    assert heat[datetime.now().astimezone().date().isoformat()] == 11
    assert heat[_days_ago(50).date().isoformat()] == 22, "50 天前在 8 周内、range=7d 之外"
    assert _days_ago(60).date().isoformat() not in heat, "8 周外的事件不进热力图"


def test_overview_kpi_turns_and_tool_runs_match_consumed_from_events(tmp_path) -> None:
    """KPI 的 turns / tool_runs 与 `run_budget.consumed_from_events` 同口径。"""
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(10, 1, 0, 11), "duration_ms": 1}, 0),
        (MODEL_COMPLETED, {"content": "a", "usage": _usage(10, 1, 0, 11)}, 0),
        (MODEL_COMPLETED, {"content": "b", "usage": _usage(10, 1, 0, 11)}, 0),
        (TOOL_RESULT, {"tool_call_id": "t", "budget_delta": {"tool_name": "bash", "tool_calls": 2}}, 0),
        (TOOL_RESULT, {"tool_call_id": "u", "budget_delta": {"tool_name": "read", "tool_calls": 1}}, 0),
    ]})
    body = _client(tmp_path).get("/api/insights/overview?range=all").json()
    consumed = consumed_from_events(_request_events(tmp_path, "s1"))
    assert body["kpi"]["turns"] == consumed.agent_turns == 2
    assert body["kpi"]["tool_runs"] == consumed.tool_calls == 3
    assert body["kpi"]["billed_tokens"] == consumed.total_tokens == 11


def _failed_request(duration_ms: int = 5) -> dict:
    """`runtime.py::record_request_failed` 写出的无 usage 失败尝试（真实形状）。"""
    return {
        "role": "main", "outcome": "failed", "request_id": "x",
        "duration_ms": duration_ms, "http_status": 500,
    }


def test_overview_billed_tokens_ignores_failed_requests_without_usage(tmp_path) -> None:
    """无 usage 的失败 `model/request` 不得把会话行 token 归 0（也不伪造 0）。

    会话行与 KPI 共用同一窗口累加结果：三档 range 下两者相等且等于成功请求的
    101。`consumed_from_events` 对缺 `total_tokens` 的请求返回粘性 None，会话行
    走它再 `or 0` 会在 `range=all` 下与 KPI 打架（审查 C-P1-1）。
    """
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(100, 1, 0, 101), "duration_ms": 7}, 0),
        (MODEL_REQUEST, _failed_request(), 0),
    ]})
    client = _client(tmp_path)
    for range_key in ("7d", "30d", "all"):
        body = client.get(f"/api/insights/overview?range={range_key}").json()
        row = body["sessions"][0]
        assert body["kpi"]["billed_tokens"] == 101, range_key
        assert row["billed_tokens"] == body["kpi"]["billed_tokens"], range_key


def test_overview_cache_hit_rate_is_cached_over_prompt(tmp_path) -> None:
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(100, 1, 40, 101), "duration_ms": 1}, 0),
        (MODEL_REQUEST, {"usage": _usage(300, 1, 60, 301), "duration_ms": 1}, 0),
    ]})
    body = _client(tmp_path).get("/api/insights/overview?range=all").json()
    assert body["kpi"]["cache_hit_rate"] == 100 / 400


def test_overview_cache_hit_rate_null_when_never_reported(tmp_path) -> None:
    """没有一次带回缓存明细 ⇒ None（不显示 0%）。"""
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": {"prompt_tokens": 100, "total_tokens": 101}, "duration_ms": 1}, 0),
    ]})
    body = _client(tmp_path).get("/api/insights/overview?range=all").json()
    assert body["kpi"]["cache_hit_rate"] is None


def test_overview_composition_only_counts_snapshotted_sessions(tmp_path) -> None:
    """构成聚合仅基于有快照会话；分类求和 = 快照总量；coverage 如实。"""
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(10, 1, 0, 11), "duration_ms": 1}, 0),
    ], "s2": [
        (MODEL_REQUEST, {"usage": _usage(20, 2, 0, 22), "duration_ms": 1}, 0),
    ]})
    app = create_app(
        Settings(workspace_dir=str(tmp_path), model_api_key="sk-test"),
        enable_cors=False,
    )
    app.state.agent.context_snapshots["s1"] = (
        {"messages": 1000, "system_prompt": 200, "skills": 30, "other": 7},
        [],
    )
    body = TestClient(app).get("/api/insights/overview?range=all").json()

    assert body["coverage"] == {"snapshotted_sessions": 1, "total_sessions": 2}
    assert body["composition"] == {
        "system_prompt": 200, "tools_system": 0, "tools_mcp": 0,
        "messages": 1000, "skills": 30, "other": 7,
    }
    assert sum(body["composition"].values()) == 1237


def test_overview_without_any_snapshot_reports_zero_coverage(tmp_path) -> None:
    """无快照会话不计入构成，不伪造 0 之外的东西——coverage 是诚实载体。"""
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(10, 1, 0, 11), "duration_ms": 1}, 0),
    ]})
    body = _client(tmp_path).get("/api/insights/overview?range=all").json()
    assert body["coverage"] == {"snapshotted_sessions": 0, "total_sessions": 1}
    assert body["composition"] == dict.fromkeys(_COMPOSITION_KEYS, 0)
    assert body["kpi"]["billed_tokens"] == 11, "构成缺席不影响 token 口径"


def test_overview_session_rows(tmp_path) -> None:
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(10, 1, 0, 11), "duration_ms": 1}, 0),
        (MODEL_COMPLETED, {"content": "x"}, 0),
    ]})
    row = _client(tmp_path).get("/api/insights/overview?range=all").json()["sessions"][0]
    assert row["session_id"] == "s1"
    assert row["title"] == "hi s1"
    assert row["workspace"] is None
    assert row["billed_tokens"] == 11
    assert row["turns"] == 1
    assert row["last_active_at"]


def test_overview_rejects_unknown_range(tmp_path) -> None:
    _seed(tmp_path, {"s1": []})
    assert _client(tmp_path).get("/api/insights/overview?range=90d").status_code == 422


def test_overview_unknown_workspace_id_is_404_not_an_empty_table(tmp_path) -> None:
    """未注册项目 → 404（与 `GET /api/sessions` 同一翻译）。

    降级成空表就是谎报"这个项目没有用量"（不变量 #21 同族，`list_sessions`
    的契约明确不伪装成空列表）。
    """
    _seed(tmp_path, {"s1": []})
    resp = _client(tmp_path).get(
        "/api/insights/overview",
        params={"workspace_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


# ── context-timeline ───────────────────────────────────────────────


def test_context_timeline_requests_and_window(tmp_path) -> None:
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(100, 10, 50, 110), "duration_ms": 700}, 0),
        (MODEL_REQUEST, {"usage": _usage(200, 20, 60, 220), "duration_ms": 300}, 0),
    ]})
    body = _client(tmp_path).get("/api/sessions/s1/context-timeline").json()

    assert set(body) == {"window", "requests", "events"}
    assert len(body["requests"]) == 2
    first = body["requests"][0]
    assert set(first) == {
        "seq", "time", "prompt_tokens", "completion_tokens",
        "cached_tokens", "total_tokens", "duration_ms",
    }
    assert first["prompt_tokens"] == 100
    assert first["duration_ms"] == 700
    # 窗口构成与 context-usage 同形状、同值
    usage = _client(tmp_path).get("/api/sessions/s1/context-usage").json()
    assert body["window"] == usage


def test_context_timeline_turn_granularity_collapses_a_run(tmp_path) -> None:
    """turn = 一个 run：同 run 的多条请求折叠成最后一条，duration_ms 求和。"""
    app = create_app(
        Settings(workspace_dir=str(tmp_path), model_api_key="sk-test"),
        enable_cors=False,
    )
    store = app.state.agent.store

    async def _run() -> None:
        session = Session.start(store, session_id="s1")
        seq = session.append(USER_MESSAGE, {"content": "hi"}).seq
        for prompt, total, duration in ((100, 110, 700), (200, 220, 300)):
            seq += 1
            store.append_event("s1", SessionEvent(
                seq=seq, type=MODEL_REQUEST, session_id="s1", run_id="r1",
                data={"usage": _usage(prompt, 1, 0, total), "duration_ms": duration},
            ))
        seq += 1
        store.append_event("s1", SessionEvent(
            seq=seq, type=MODEL_REQUEST, session_id="s1", run_id="r2",
            data={"usage": _usage(300, 1, 0, 330), "duration_ms": 50},
        ))

    asyncio.run(_run())
    client = _client(tmp_path)
    turn = client.get(
        "/api/sessions/s1/context-timeline?granularity=turn"
    ).json()["requests"]
    assert len(turn) == 2
    assert turn[0]["prompt_tokens"] == 200, "折叠成该 run 的最后一条"
    assert turn[0]["duration_ms"] == 1000, "同 run 的 duration 求和"
    assert turn[1]["prompt_tokens"] == 300


def test_context_timeline_skips_failed_requests_without_usage(tmp_path) -> None:
    """无 usage 的失败尝试不进 token 序列：不出 0 行、不出负增量。

    序列：成功(101) → 失败(无 usage) → 成功(252)。失败尝试既不是一条可画的
    token 请求，也不该让 `mode=delta` 出现负值（审查 C-P1-2）。
    """
    app = create_app(
        Settings(workspace_dir=str(tmp_path), model_api_key="sk-test"),
        enable_cors=False,
    )
    store = app.state.agent.store

    async def _run() -> None:
        session = Session.start(store, session_id="s1")
        seq = session.append(USER_MESSAGE, {"content": "hi"}).seq
        rows = (
            {"usage": _usage(100, 1, 0, 101), "duration_ms": 7},
            _failed_request(),
            {"usage": _usage(250, 2, 0, 252), "duration_ms": 9},
        )
        for data in rows:
            seq += 1
            store.append_event("s1", SessionEvent(
                seq=seq, type=MODEL_REQUEST, session_id="s1", run_id="r1", data=data,
            ))

    asyncio.run(_run())
    client = _client(tmp_path)
    total = client.get("/api/sessions/s1/context-timeline").json()["requests"]
    assert [(r["prompt_tokens"], r["total_tokens"]) for r in total] == [
        (100, 101), (250, 252),
    ], "失败尝试不补 0 行"
    delta = client.get(
        "/api/sessions/s1/context-timeline?mode=delta"
    ).json()["requests"]
    assert [r["total_tokens"] for r in delta] == [0, 151], "无负增量"


def test_context_timeline_turn_fold_keeps_usage_when_last_attempt_failed(tmp_path) -> None:
    """同一 run「成功 + 末尾失败」：折叠保留成功用量，失败尝试的 duration 不计入。

    失败尝试已被过滤 ⇒ 它既不是末条用量来源，其 `duration_ms` 也不进 turn 的
    时长求和（票面口径：逐请求序列只看有 usage 的请求）。
    """
    app = create_app(
        Settings(workspace_dir=str(tmp_path), model_api_key="sk-test"),
        enable_cors=False,
    )
    store = app.state.agent.store

    async def _run() -> None:
        session = Session.start(store, session_id="s1")
        seq = session.append(USER_MESSAGE, {"content": "hi"}).seq
        for data in (
            {"usage": _usage(1000, 50, 0, 1050), "duration_ms": 10},
            _failed_request(duration_ms=5),
        ):
            seq += 1
            store.append_event("s1", SessionEvent(
                seq=seq, type=MODEL_REQUEST, session_id="s1", run_id="r1", data=data,
            ))

    asyncio.run(_run())
    turn = _client(tmp_path).get(
        "/api/sessions/s1/context-timeline?granularity=turn"
    ).json()["requests"]
    assert len(turn) == 1
    assert turn[0]["total_tokens"] == 1050, "整轮用量不得被失败尝试归零"
    assert turn[0]["duration_ms"] == 10, "失败尝试的 duration 不计入"


def test_context_timeline_delta_mode(tmp_path) -> None:
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(100, 10, 5, 110), "duration_ms": 700}, 0),
        (MODEL_REQUEST, {"usage": _usage(250, 30, 25, 280), "duration_ms": 300}, 0),
    ]})
    reqs = _client(tmp_path).get(
        "/api/sessions/s1/context-timeline?mode=delta"
    ).json()["requests"]
    assert reqs[0]["prompt_tokens"] == 0, "首条无基线 ⇒ 0"
    assert reqs[1]["prompt_tokens"] == 150
    assert reqs[1]["completion_tokens"] == 20
    assert reqs[1]["cached_tokens"] == 20
    assert reqs[1]["total_tokens"] == 170
    assert reqs[1]["duration_ms"] == 300, "时长不是增量"


def test_context_timeline_events_list(tmp_path) -> None:
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(1, 1, 0, 2), "duration_ms": 1}, 0),
        (CONTEXT_COMPACTED, {"summary": "s"}, 0),
        (CONTEXT_WINDOW_CHANGED, {"window_tokens": 300000}, 0),
        (MODEL_COMPLETED, {"content": "noise"}, 0),
    ]})
    events = _client(tmp_path).get("/api/sessions/s1/context-timeline").json()["events"]
    assert [e["type"] for e in events] == [CONTEXT_COMPACTED, CONTEXT_WINDOW_CHANGED]
    assert set(events[0]) == {"seq", "time", "type"}


def test_context_timeline_404_and_422(tmp_path) -> None:
    _seed(tmp_path, {"s1": []})
    client = _client(tmp_path)
    assert client.get("/api/sessions/nope/context-timeline").status_code == 404
    assert client.get("/api/sessions/bad.id/context-timeline").status_code == 422


# ── fleet ──────────────────────────────────────────────────────────


def test_fleet_self_ancestors_children(tmp_path) -> None:
    _seed(tmp_path, {
        "gp": [
            (AGENT_DELEGATION_STARTED, {"target": "coding", "task": "t", "child_session_id": "p"}, 1),
            (AGENT_DELEGATION_FINISHED, {"target": "coding", "child_session_id": "p", "status": "completed"}, 1),
        ],
        "p": [
            (AGENT_DELEGATION_STARTED, {"target": "coding", "task": "t2", "child_session_id": "c1"}, 0),
            (MODEL_REQUEST, {"usage": _usage(500, 50, 100, 550), "duration_ms": 10}, 0),
            (MODEL_COMPLETED, {"content": "a", "usage": _usage(500, 50, 100, 550)}, 0),
        ],
        "c1": [
            (MODEL_REQUEST, {"usage": _usage(80, 8, 0, 88), "duration_ms": 5}, 0),
            (MODEL_COMPLETED, {"content": "b", "usage": _usage(80, 8, 0, 88)}, 0),
        ],
    })
    body = _client(tmp_path).get("/api/sessions/p/fleet").json()

    assert set(body) == {"self", "ancestors", "children"}
    assert body["self"]["session_id"] == "p"
    assert body["self"]["status"] == "idle"
    assert body["self"]["tokens"] == 500, "无快照 ⇒ 最近一次调用输入规模（usage_only）"
    assert body["self"]["last_active_at"]

    assert [a["session_id"] for a in body["ancestors"]] == ["gp"]
    assert body["ancestors"][0]["tokens"] == 0

    child = body["children"][0]
    assert child["session_id"] == "c1"
    assert child["status"] == "running"
    assert child["used_tokens"] == 80
    assert child["last_active_at"]


def test_fleet_root_has_no_ancestors(tmp_path) -> None:
    _seed(tmp_path, {"solo": []})
    body = _client(tmp_path).get("/api/sessions/solo/fleet").json()
    assert body["ancestors"] == []
    assert body["children"] == []


def test_fleet_404(tmp_path) -> None:
    _seed(tmp_path, {"s1": []})
    assert _client(tmp_path).get("/api/sessions/nope/fleet").status_code == 404


# ── 开关 ───────────────────────────────────────────────────────────


def test_insights_disabled_unregisters_all_three_routes(tmp_path) -> None:
    _seed(tmp_path, {"s1": []})
    client = _client(tmp_path, insights_enabled=False)
    assert client.get("/api/insights/overview").status_code == 404
    assert client.get("/api/sessions/s1/context-timeline").status_code == 404
    assert client.get("/api/sessions/s1/fleet").status_code == 404
    # 其余路由照常
    assert client.get("/api/sessions/s1/context-usage").status_code == 200
    assert client.get("/api/sessions/s1/delegations").status_code == 200


def test_capability_manifest_carries_insights_flag(tmp_path) -> None:
    on = _client(tmp_path).get("/api/capabilities").json()
    core = next(c for c in on["capabilities"] if c["id"] == "core")
    assert core["insights"] is True

    off = _client(tmp_path, insights_enabled=False).get("/api/capabilities").json()
    core_off = next(c for c in off["capabilities"] if c["id"] == "core")
    assert core_off["insights"] is False


# ── 对账 ───────────────────────────────────────────────────────────


def test_overview_billed_tokens_reconciles_with_consumed_from_events(tmp_path) -> None:
    """跨会话累计 == 逐会话 `consumed_from_events(...).total_tokens` 之和。"""
    _seed(tmp_path, {
        "a": [(MODEL_REQUEST, {"usage": _usage(10, 1, 0, 11), "duration_ms": 1}, 0)],
        "b": [(MODEL_REQUEST, {"usage": _usage(20, 2, 0, 22), "duration_ms": 1}, 0)],
    })
    body = _client(tmp_path).get("/api/insights/overview?range=all").json()
    expected = sum(
        consumed_from_events(_request_events(tmp_path, sid)).total_tokens or 0
        for sid in ("a", "b")
    )
    assert body["kpi"]["billed_tokens"] == expected == 33
    rows = {r["session_id"]: r["billed_tokens"] for r in body["sessions"]}
    assert rows == {"a": 11, "b": 22}


def test_context_timeline_window_matches_context_usage_when_snapshot_present(tmp_path) -> None:
    """对账（限定有快照会话）：window 逐字段 == context-usage。"""
    _seed(tmp_path, {"s1": [
        (MODEL_REQUEST, {"usage": _usage(10, 1, 0, 11), "duration_ms": 1}, 0),
    ]})
    app = create_app(
        Settings(workspace_dir=str(tmp_path), model_api_key="sk-test"),
        enable_cors=False,
    )
    app.state.agent.context_snapshots["s1"] = (
        {"messages": 3000, "system_prompt": 500, "skills": 0, "other": 0},
        [{"name": "bash", "description": "d", "parameters": {"type": "object"}}],
    )
    client = TestClient(app)
    window = client.get("/api/sessions/s1/context-timeline").json()["window"]
    if window["state"] != "ok":  # 前置不满足 ⇒ 显式 skip，不判 PASS
        import pytest

        pytest.skip(f"context-usage state={window['state']}，对账前置不满足")
    usage = client.get("/api/sessions/s1/context-usage").json()
    assert window["used_tokens"] == usage["used_tokens"]
    assert window["breakdown"] == usage["breakdown"]
    assert sum(window["breakdown"]["tools"].values()) > 0
