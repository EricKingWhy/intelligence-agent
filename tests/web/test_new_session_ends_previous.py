"""#1418：`/new` 携带 `previous_session_id`，服务端同一请求内结束前任会话。

路线乙（用户 2026-10-20 13:44 亲定）：**不加事件类型**；结束事实由存储层
`SessionArchive.end_session()` 写归档行（抄 Hermes `hermes_state_sessions.py:483-490`
首写获胜语义）。`end_reason="new_session"` 与 #1389 的
`_FRESH_RESET_END_REASONS` 逐字对齐。

顺序与失败策略（票面）：先建新会话、成功后才 end 旧会话（建失败不许误杀旧会话）；
end 失败只记 warning（归档是派生索引，fail-open）；previous 不存在静默跳过、不 500。

判据 = 请求里是否带 `previous_session_id`：`/sessions` 切换、`/quit`、Web
`createEmptySession` 都不带（各自的否定性测试见 TUI 与 web 侧）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import service as service_module
from agent_harness.storage.session_archive import SessionArchive
from agent_harness.web.app import create_app
from tests.conftest import register_workspace_from_build_kwargs


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def _fake_build(**kwargs):
        register_workspace_from_build_kwargs(kwargs)
        return object()

    monkeypatch.setattr(service_module, "build_runtime", _fake_build)

    from agent_harness.session.runmanager import RunManager, Subscriber

    class _FakeRun:
        task = None

        def unsubscribe(self, _sub):
            pass

    def _fake_launch(self, session, runtime, user_input, user_input_metadata=None):
        sub = Subscriber()
        sub.queue.put_nowait(self.DONE)
        return _FakeRun(), sub

    monkeypatch.setattr(RunManager, "launch", _fake_launch)

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        enable_cors=False,
    )
    app = create_app(settings, enable_cors=False)
    return TestClient(app), app


def _create(client: TestClient, **body: object) -> str:
    resp = client.post("/api/sessions?launch=false", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _archive(app):
    return app.state.agent.session_archive_observer.archive


def test_new_request_ends_previous_session(client):
    """AC1 正向：带 previous_session_id 的创建请求同请求内写旧会话归档行。"""
    client, app = client
    old = _create(client)
    # 观测者已为该会话建行（session/started 是归档写入点）。
    before = _archive(app).get_session(old)
    assert before is not None, "旧会话必须先有归档行，否则断言会假绿"
    assert before["end_reason"] is None

    new = _create(client, previous_session_id=old)

    assert new != old
    # 新会话真的建出来了（不是只写旧行就返回）。
    assert client.get(f"/api/sessions/{new}/events").status_code == 200
    meta = _archive(app).get_session(old)
    assert meta is not None
    assert meta["end_reason"] == "new_session"
    assert meta["ended_at"] is not None


def test_create_without_previous_session_id_leaves_old_row_open(client):
    """AC1 否定：不带字段的创建入口（/sessions、/quit、Web createEmptySession）
    不结束任何会话。"""
    client, app = client
    old = _create(client)
    _create(client)
    meta = _archive(app).get_session(old)
    assert meta is not None
    assert meta["end_reason"] is None, "只有 /new 路径才允许结束前任会话"


def test_unknown_previous_session_id_is_silently_skipped(client):
    """AC2：previous 不存在 → UPDATE 命中 0 行，接口照常 200，不 500。"""
    client, app = client
    resp = client.post(
        "/api/sessions?launch=false", json={"previous_session_id": "does-not-exist"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_id"]
    assert _archive(app).get_session("does-not-exist") is None


def test_repeated_new_is_idempotent(client):
    """AC2：重复 /new（同一 previous 再结束一次）不炸，首写值不变。"""
    client, app = client
    old = _create(client)
    _create(client, previous_session_id=old)
    first = _archive(app).get_session(old)
    _create(client, previous_session_id=old)
    assert _archive(app).get_session(old) == first


def test_create_failure_does_not_end_previous_session(client):
    """顺序不变量：**先建新会话、成功后才 end 旧会话**。

    非法 workspace 在落盘前就被 `_validate_workspace_name` 拒绝（422），
    此时前任会话必须原封不动——建失败不许误杀旧会话。
    """
    client, app = client
    old = _create(client)

    resp = client.post(
        "/api/sessions?launch=false",
        json={"previous_session_id": old, "workspace": "/abs/path"},
    )
    assert resp.status_code == 422, resp.text
    meta = _archive(app).get_session(old)
    assert meta is not None
    assert meta["end_reason"] is None, "建会话失败时不得结束前任"
    assert meta["ended_at"] is None


@pytest.mark.parametrize("bad_id", ["bad/id", "a" * 129])
def test_invalid_previous_session_id_is_silently_skipped(client, monkeypatch, bad_id):
    """非法 id 等同「不存在」：静默跳过，请求照常 200 建新会话。

    票面 AC2 的契约是 previous 不存在静默跳过；非法格式不能变成 422/500，
    也不得落到归档写入（`validate_session_id` 不通过 ⇒ 不调 end）。
    """
    client, app = client
    ended: list[str] = []
    real_end = SessionArchive.end_session

    def _spy(self, session_id, reason):
        ended.append(session_id)
        return real_end(self, session_id, reason)

    monkeypatch.setattr(SessionArchive, "end_session", _spy)

    resp = client.post(
        "/api/sessions?launch=false", json={"previous_session_id": bad_id},
    )
    assert resp.status_code == 200, resp.text
    new = resp.json()["session_id"]
    assert client.get(f"/api/sessions/{new}/events").status_code == 200
    assert ended == [], "非法 id 不得触达归档结束写入"
    assert _archive(app).get_session(bad_id) is None


def test_end_failure_is_fail_open(client, monkeypatch):
    """归档写入失败只记 warning：建会话请求照常成功（派生索引不反噬主路径）。"""
    client, app = client
    old = _create(client)

    def _boom(self, session_id, reason):
        raise RuntimeError("archive down")

    monkeypatch.setattr(SessionArchive, "end_session", _boom)

    resp = client.post("/api/sessions?launch=false", json={"previous_session_id": old})
    assert resp.status_code == 200, resp.text
    new = resp.json()["session_id"]
    assert client.get(f"/api/sessions/{new}/events").status_code == 200
    assert _archive(app).get_session(old)["end_reason"] is None
