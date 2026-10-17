"""Web 子智能体 roster API 测试（#1256）：只读形状契约。

接缝与 test_web_lineage.py 同：独立 router 文件接入面 + app.py 一行注册。
数据全部来自父会话 append-only 事件流（不变量 #3/#22），这里只断言外部 HTTP 形状，
不碰 service / 投影内部。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import Session
from agent_harness.session.event import (
    AGENT_DELEGATION_FINISHED,
    AGENT_DELEGATION_STARTED,
    MODEL_COMPLETED,
    RUN_INTERRUPTED,
    RUN_PAUSED,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.web.app import create_app


def _client(tmp_path) -> TestClient:
    settings = Settings(workspace_dir=str(tmp_path), model_api_key="sk-test")
    app = create_app(settings, enable_cors=False)
    return TestClient(app)


def _seed(
    tmp_path,
    *,
    finish: bool = True,
    finish_status: str = "completed",
    interrupted: bool = False,
    paused: bool = False,
    child_usage: dict | None = None,
    create_child: bool = True,
) -> SessionEvent | None:
    """父会话 'p' 委派给子会话 'c1'（可调配对/终态/用量）。

    返回把在途委派翻 stopped 的那条触发事件（RUN_INTERRUPTED / RUN_PAUSED），
    无则 None——用例据此拿**真实** time 断言 completed_at（不手编时间戳）。
    """
    settings = Settings(workspace_dir=str(tmp_path), model_api_key="sk-test")
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store

    async def _run() -> SessionEvent | None:
        parent = Session.start(store, session_id="p")
        parent.append(USER_MESSAGE, {"content": "帮我派个活"})
        if create_child:
            child = Session.start(store, session_id="c1")
            child.append(USER_MESSAGE, {"content": "子任务"})
            if child_usage is not None:
                child.append(MODEL_COMPLETED, {"content": "done", "usage": child_usage})
        parent.append(AGENT_DELEGATION_STARTED, {
            "target": "coding", "task": "实现 /export 删除逻辑",
            "child_session_id": "c1",
        })
        if finish:
            parent.append(AGENT_DELEGATION_FINISHED, {
                "target": "coding", "child_session_id": "c1",
                "status": finish_status, "summary": "已改好",
            })
        if interrupted:
            return parent.append(RUN_INTERRUPTED, {"reason": "killed"})
        if paused:
            return parent.append(RUN_PAUSED, {"reason": "budget_exhausted"})
        return None

    return asyncio.run(_run())


def _one(body: list[dict]) -> dict:
    assert len(body) == 1, body
    return body[0]


def test_delegations_empty_when_no_delegation(tmp_path) -> None:
    settings = Settings(workspace_dir=str(tmp_path), model_api_key="sk-test")
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store

    async def _run() -> None:
        s = Session.start(store, session_id="solo")
        s.append(USER_MESSAGE, {"content": "没有委派"})

    asyncio.run(_run())
    resp = _client(tmp_path).get("/api/sessions/solo/delegations")
    assert resp.status_code == 200
    assert resp.json() == []


def test_delegations_completed_pair_shape(tmp_path) -> None:
    _seed(tmp_path)
    resp = _client(tmp_path).get("/api/sessions/p/delegations")
    assert resp.status_code == 200
    entry = _one(resp.json())
    assert entry["child_session_id"] == "c1"
    assert entry["target"] == "coding"
    assert entry["task"] == "实现 /export 删除逻辑"
    assert entry["status"] == "completed"
    assert entry["started_at"]  # 事件 time
    assert entry["completed_at"]
    assert entry["summary"] == "已改好"


def test_delegations_running_when_unpaired(tmp_path) -> None:
    _seed(tmp_path, finish=False)
    entry = _one(_client(tmp_path).get("/api/sessions/p/delegations").json())
    assert entry["status"] == "running"
    assert entry["completed_at"] is None


def test_delegations_failed_status_normalized(tmp_path) -> None:
    _seed(tmp_path, finish_status="failed")
    entry = _one(_client(tmp_path).get("/api/sessions/p/delegations").json())
    assert entry["status"] == "failed"


def test_delegations_stopped_on_run_interrupt(tmp_path) -> None:
    """父 run 被中断、委派未回填 finished ⇒ stopped（≠ failed，≠ running）。

    completed_at 冻结在**中断那条事件**的 time：函数契约「起点→终点」要求终态条目
    有真终点，与前端投影 finalizeRun settle 语义同源（不是 return null）。"""
    trigger = _seed(tmp_path, finish=False, interrupted=True)
    entry = _one(_client(tmp_path).get("/api/sessions/p/delegations").json())
    assert entry["status"] == "stopped"
    assert entry["completed_at"]
    assert entry["completed_at"] == trigger.time


def test_delegations_stopped_on_run_pause(tmp_path) -> None:
    """父 run 暂停、委派未回填 finished ⇒ stopped + completed_at = 暂停事件 time。

    paused 不是 run 终态（event.py:40-42），但**前端投影** projectRunPaused 同样把
    在途委派翻 stopped（finalizeRun 共用清扫），roster 判据必须与投影同源——仓库既有
    同构先例：session/interrupt.py 与 session/fork.py 的
    ``event.type in RUN_TERMINAL_TYPES or event.type == RUN_PAUSED``。"""
    trigger = _seed(tmp_path, finish=False, paused=True)
    entry = _one(_client(tmp_path).get("/api/sessions/p/delegations").json())
    assert entry["status"] == "stopped"
    assert entry["completed_at"] == trigger.time


def test_delegations_usage_only_from_child_events(tmp_path) -> None:
    """子会话事件流有 usage ⇒ usage_only 且 used_tokens = 最近一次调用输入规模。"""
    _seed(tmp_path, child_usage={"prompt_tokens": 12842, "total_tokens": 13000})
    entry = _one(_client(tmp_path).get("/api/sessions/p/delegations").json())
    assert entry["used_tokens"] == 12842
    assert entry["usage_state"] == "usage_only"


def test_delegations_no_data_when_child_has_no_usage(tmp_path) -> None:
    _seed(tmp_path)
    entry = _one(_client(tmp_path).get("/api/sessions/p/delegations").json())
    assert entry["used_tokens"] == 0
    assert entry["usage_state"] == "no_data"


def test_delegations_cold_child_missing_is_still_listed(tmp_path) -> None:
    """子会话日志缺席（cold / 已清理）仍列出该委派，用量如实落 no_data。"""
    _seed(tmp_path, create_child=False)
    entry = _one(_client(tmp_path).get("/api/sessions/p/delegations").json())
    assert entry["child_session_id"] == "c1"
    assert entry["usage_state"] == "no_data"
    assert entry["used_tokens"] == 0


def test_delegations_404_when_parent_missing(tmp_path) -> None:
    _seed(tmp_path)
    resp = _client(tmp_path).get("/api/sessions/nonexistent/delegations")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_delegations_rejects_path_traversal(tmp_path) -> None:
    _client(tmp_path)
    resp = _client(tmp_path).get("/api/sessions/..%2F..%2Fetc%2Fpasswd/delegations")
    assert resp.status_code in (400, 404, 422)


def _seed_parallel(tmp_path) -> None:
    """父会话 'p' 并行委派 c1/c2/c3：started/finished 交错、乱序 finish
    （c2 的 finish 先于 c1 落盘）、c1 的 started 重复一次（幂等），c3 保持在途。"""
    settings = Settings(workspace_dir=str(tmp_path), model_api_key="sk-test")
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store

    async def _run() -> None:
        parent = Session.start(store, session_id="p")
        for cid in ("c1", "c2", "c3"):
            Session.start(store, session_id=cid)
        parent.append(AGENT_DELEGATION_STARTED, {
            "target": "coding", "task": "t1", "child_session_id": "c1"})
        parent.append(AGENT_DELEGATION_STARTED, {
            "target": "research_review", "task": "t2", "child_session_id": "c2"})
        # 重复 started：只认首条（task 不被 "t1-dup" 覆盖）
        parent.append(AGENT_DELEGATION_STARTED, {
            "target": "coding", "task": "t1-dup", "child_session_id": "c1"})
        # 乱序 finish：c2 的 finish 先落盘（位置不可假设，按 child_session_id 键控）
        parent.append(AGENT_DELEGATION_FINISHED, {
            "target": "research_review", "child_session_id": "c2",
            "status": "completed", "summary": "c2 done"})
        parent.append(AGENT_DELEGATION_STARTED, {
            "target": "coding", "task": "t3", "child_session_id": "c3"})
        parent.append(AGENT_DELEGATION_FINISHED, {
            "target": "coding", "child_session_id": "c1",
            "status": "completed", "summary": "c1 done"})

    asyncio.run(_run())


def _by_child(body: list[dict]) -> dict[str, dict]:
    return {e["child_session_id"]: e for e in body}


def test_delegations_parallel_out_of_order_finish_pairs_by_child(tmp_path) -> None:
    """Correctness P2-1：乱序 finish 按 child_session_id 配对，不按事件位置配对。

    按位置配对的错误实现会把 c2 的 finish（summary "c2 done"）配给 c1——
    两个条目的 summary/task/target 必须各归其主。"""
    _seed_parallel(tmp_path)
    body = _client(tmp_path).get("/api/sessions/p/delegations").json()
    assert len(body) == 3, body
    by_child = _by_child(body)
    assert by_child["c1"]["status"] == "completed"
    assert by_child["c1"]["summary"] == "c1 done"
    assert by_child["c1"]["task"] == "t1"
    assert by_child["c2"]["status"] == "completed"
    assert by_child["c2"]["summary"] == "c2 done"
    assert by_child["c2"]["task"] == "t2"
    assert by_child["c1"]["completed_at"] and by_child["c2"]["completed_at"]


def test_delegations_parallel_interleaved_keeps_running(tmp_path) -> None:
    """Correctness P2-1：交错 started 之间未回填 finish 的委派保持 running，
    completed_at 为空——c2/c1 的 finish 不得误配给 c3。"""
    _seed_parallel(tmp_path)
    by_child = _by_child(
        _client(tmp_path).get("/api/sessions/p/delegations").json())
    assert by_child["c3"]["status"] == "running"
    assert by_child["c3"]["completed_at"] is None
    assert by_child["c3"]["task"] == "t3"


def test_delegations_duplicate_started_is_idempotent(tmp_path) -> None:
    """Correctness P2-1：重复 started 幂等——只认首条（task 仍是 "t1" 而非
    "t1-dup"），不产生第二条目（与投影 some() 幂等一致）。"""
    _seed_parallel(tmp_path)
    body = _client(tmp_path).get("/api/sessions/p/delegations").json()
    assert [e["child_session_id"] for e in body] == ["c1", "c2", "c3"]
    assert _by_child(body)["c1"]["task"] == "t1"


def test_delegations_prefers_live_builder_snapshot_over_events(tmp_path, monkeypatch) -> None:
    """Correctness P3-1：子会话有在途 run ⇒ roster 的 used_tokens/usage_state 与
    context-usage 端点同源（live builder 快照 → state ok），而不是 usage_only
    估算（AC2 口径一致）。"""
    _seed(tmp_path, child_usage={"prompt_tokens": 12842, "total_tokens": 13000})
    settings = Settings(workspace_dir=str(tmp_path), model_api_key="sk-test")
    app = create_app(settings, enable_cors=False)

    snapshot = {"messages": 4000, "system_prompt": 1000, "skills": 200, "other": 100}

    class _Builder:
        def usage_snapshot(self, session):
            return dict(snapshot)

    class _Registry:
        def export_model_definitions(self):
            return []

    active = SimpleNamespace(
        runtime=SimpleNamespace(_context_builder=_Builder(), registry=_Registry()),
        session=object(),
    )
    monkeypatch.setattr(
        app.state.agent.run_manager,
        "get_active",
        lambda session_id: active if session_id == "c1" else None,
    )

    entry = _one(TestClient(app).get("/api/sessions/p/delegations").json())
    assert entry["usage_state"] == "ok"
    assert entry["used_tokens"] == 4000 + 1000 + 200 + 100


def test_delegations_child_id_traversal_is_neutralized(tmp_path) -> None:
    """Correctness P3-2：父日志 data 里的 child_session_id 只做过 isinstance 检查就进
    read_events → _events_path（store.py:288-292 无净化）——含 ../ 的穿越 id 不得读到
    store 根之外的 decoy 日志（usage_state 如实 no_data，不 brick 整张 roster）。"""
    # decoy 放在 store 根（<workspace>/sessions）的**上一级**："../evil" 从 sessions/
    # 出发正好落到 <workspace>/evil（store._events_path 实测为
    # `<workspace>/sessions/../evil/events.jsonl`）。
    decoy_dir = tmp_path / "evil"
    decoy_dir.mkdir(parents=True, exist_ok=True)
    evil = SessionEvent(
        seq=1, type=MODEL_COMPLETED, session_id="evil",
        data={"content": "x", "usage": {"prompt_tokens": 99999, "total_tokens": 100000}},
    )
    (decoy_dir / "events.jsonl").write_text(
        json.dumps(evil.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    settings = Settings(workspace_dir=str(tmp_path), model_api_key="sk-test")
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store

    async def _run() -> None:
        parent = Session.start(store, session_id="p")
        parent.append(AGENT_DELEGATION_STARTED, {
            "target": "coding", "task": "t", "child_session_id": "../evil"})

    asyncio.run(_run())
    entry = _one(TestClient(app).get("/api/sessions/p/delegations").json())
    assert entry["child_session_id"] == "../evil"
    assert entry["usage_state"] == "no_data"
    assert entry["used_tokens"] == 0
