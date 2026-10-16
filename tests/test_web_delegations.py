"""Web 子智能体 roster API 测试（#1256）：只读形状契约。

接缝与 test_web_lineage.py 同：独立 router 文件接入面 + app.py 一行注册。
数据全部来自父会话 append-only 事件流（不变量 #3/#22），这里只断言外部 HTTP 形状，
不碰 service / 投影内部。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import Session
from agent_harness.session.event import (
    AGENT_DELEGATION_FINISHED,
    AGENT_DELEGATION_STARTED,
    MODEL_COMPLETED,
    RUN_INTERRUPTED,
    USER_MESSAGE,
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
    child_usage: dict | None = None,
    create_child: bool = True,
) -> None:
    """父会话 'p' 委派给子会话 'c1'（可调配对/终态/用量）。"""
    settings = Settings(workspace_dir=str(tmp_path), model_api_key="sk-test")
    app = create_app(settings, enable_cors=False)
    store = app.state.agent.store

    async def _run() -> None:
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
            parent.append(RUN_INTERRUPTED, {"reason": "killed"})

    asyncio.run(_run())


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
    """父 run 被中断、委派未回填 finished ⇒ stopped（≠ failed，≠ running）。"""
    _seed(tmp_path, finish=False, interrupted=True)
    entry = _one(_client(tmp_path).get("/api/sessions/p/delegations").json())
    assert entry["status"] == "stopped"


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
