"""#1359 阶段 1：持久目标对象 REST 面（`GET|POST /api/sessions/{id}/goal`）。

口径与 task 面同表（spec 11 §6.1 + `task_delivery` 先例）：**形状非法 422、
状态/CAS 对不上 409、被拒请求零事件**。未定义目标是正常状态（GET 200 +
`defined=false`），不是 404。来源闸只在 POST 上（浏览器可构造跨源写）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import service as service_module
from agent_harness.session.event import GOAL_CHANGED
from agent_harness.web.app import create_app
from tests.conftest import register_workspace_from_build_kwargs

_GOOD = "## Objective\n修好登录页\n\n## Success criteria\n跳转成功\n\n## Verification\npytest tests/auth\n"


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
    return TestClient(app), app, settings


def _create_session(client) -> str:
    resp = client.post("/api/sessions?launch=false", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _post(client, session_id: str, **body):
    return client.post(f"/api/sessions/{session_id}/goal", json=body)


class TestGetGoal:
    def test_404_unknown_session(self, client):
        client, _, _ = client
        assert client.get("/api/sessions/does-not-exist/goal").status_code == 404

    def test_200_undefined_goal_is_normal_state(self, client):
        client, _, _ = client
        session_id = _create_session(client)
        resp = client.get(f"/api/sessions/{session_id}/goal")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["goal"]["defined"] is False
        assert body["goal"]["revision"] == 0
        assert body["goal"]["max_goal_rounds"] == 32
        assert body["activation"] == "disarmed"

    def test_refresh_rebuilds_projection_from_events(self, client):
        client, _, settings = client
        session_id = _create_session(client)
        assert _post(client, session_id, operation="create", objective=_GOOD).status_code == 200
        first = client.get(f"/api/sessions/{session_id}/goal").json()
        # 换一个 app 实例（模拟进程重启后的只读重建）：durable 事实一致，
        # activation 是进程内状态，重置为 disarmed。
        fresh = TestClient(
            create_app(
                Settings(
                    _env_file=None, workspace_dir=settings.workspace_dir,
                    model_api_key="sk-test", enable_cors=False,
                ),
                enable_cors=False,
            )
        )
        second = fresh.get(f"/api/sessions/{session_id}/goal").json()
        assert second["goal"] == first["goal"]
        # activation 是进程内状态（ClassVar 集合）：清空即模拟重启 → disarmed，
        # 而 durable 目标事实仍从事件重建。
        service_module.SessionService._goal_armed.clear()
        third = fresh.get(f"/api/sessions/{session_id}/goal").json()
        assert third["goal"] == first["goal"]
        assert third["activation"] == "disarmed"


class TestGoalCommands:
    def test_create_returns_snapshot_and_arms(self, client):
        client, app, _ = client
        session_id = _create_session(client)
        resp = _post(client, session_id, operation="create", objective=_GOOD)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["goal"]["defined"] is True
        assert body["goal"]["phase"] == "active"
        assert body["goal"]["revision"] == 1
        assert body["goal"]["needs_interview"] is False
        assert body["activation"] == "armed"
        events = app.state.agent.store.read_events(session_id)
        assert [e.type for e in events].count(GOAL_CHANGED) == 1

    def test_lifecycle_commands_update_activation(self, client):
        client, _, _ = client
        session_id = _create_session(client)
        _post(client, session_id, operation="create", objective=_GOOD)
        assert _post(client, session_id, operation="pause").json()["activation"] == "disarmed"
        assert _post(client, session_id, operation="resume").json()["activation"] == "armed"
        assert _post(client, session_id, operation="block", blocked_reason={
            "code": "approval", "message": "等审批"
        }).json()["goal"]["blocked_reason"]["code"] == "approval"
        assert _post(client, session_id, operation="clear").json()["goal"]["defined"] is False

    def test_409_stale_revision_is_actionable(self, client):
        """AC3：并发 edit 后者 409 + 当前 revision，且零新事件。"""
        client, app, _ = client
        session_id = _create_session(client)
        _post(client, session_id, operation="create", objective=_GOOD)
        before = len(app.state.agent.store.read_events(session_id))
        resp = _post(
            client, session_id, operation="edit", objective="新目标", expected_revision=9,
        )
        assert resp.status_code == 409, resp.text
        assert "r9" in resp.json()["detail"] and "r1" in resp.json()["detail"]
        assert len(app.state.agent.store.read_events(session_id)) == before

    def test_422_shape_errors_are_zero_events(self, client):
        client, app, _ = client
        session_id = _create_session(client)
        before = len(app.state.agent.store.read_events(session_id))
        assert _post(client, session_id, operation="create", objective="   ").status_code == 422
        assert _post(client, session_id, operation="create", max_goal_rounds=0).status_code == 422
        # 形状先于状态：空会话上「没有目标」也不会把缺 reason 的 block 说成 409。
        session2 = _create_session(client)
        assert _post(client, session2, operation="block").status_code == 422
        assert _post(
            client, session2, operation="block",
            blocked_reason={"code": "x", "message": "等审批"},
        ).status_code == 409
        assert _post(client, session_id, operation="nope").status_code == 422  # pydantic Literal
        assert _post(client, session_id, operation="create", objective=_GOOD, extra=1).status_code == 422
        assert len(app.state.agent.store.read_events(session_id)) == before

    def test_post_is_behind_trusted_origin(self, client):
        client, _, _ = client
        session_id = _create_session(client)
        resp = client.post(
            f"/api/sessions/{session_id}/goal",
            json={"operation": "create", "objective": _GOOD},
            headers={"Origin": "http://evil.example"},
        )
        assert resp.status_code == 403
