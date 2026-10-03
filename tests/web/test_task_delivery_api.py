"""W-07（#351）：Task 交付状态最小 REST 查询/命令面（S2/S3）。

票面工作指令 3：「API 返回任务状态以及每项验证/接受依据，不返回凭证值。刷新后
由 Event 重建同一结果，TUI/Web 不各算一套。」——服务端投影只有
``derive_task_state`` 一份（session/task.py），REST 只是把它搬过传输边界；
错误码口径 = spec 11 §6.1 + T4/#312 先例：**形状非法 422、状态对不上 409**，
都在任何 model/tool/child 工作之前判定（被拒请求零副作用）。

领域层见 `tests/session/test_task_delivery.py`；fixture 策略对齐
`tests/web/test_web_session_permission.py`（patch build_runtime/launch，
`POST /api/sessions?launch=false` 造空会话再打新端点）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import service as service_module
from agent_harness.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def _fake_build(**kwargs):
        return object()

    monkeypatch.setattr(service_module, "build_runtime", _fake_build)

    from agent_harness.session.runmanager import RunManager, Subscriber

    launched: list = []

    class _FakeRun:
        task = None

        def unsubscribe(self, _sub):
            pass

    def _fake_launch(self, session, runtime, user_input, user_input_metadata=None):
        launched.append(session)
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
    app.state._launched_sessions = launched
    return TestClient(app), app


def _create_session(client, app) -> str:
    resp = client.post("/api/sessions?launch=false", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _define(client, session_id: str, **overrides) -> dict:
    body = {
        "task_text": "把登录页修好",
        "criteria": [{"text": "登录成功跳转"}],
        **overrides,
    }
    return client.post(f"/api/sessions/{session_id}/task/definition", json=body)


class TestGetTask:
    def test_404_unknown_session(self, client):
        client, _ = client
        resp = client.get("/api/sessions/does-not-exist/task")
        assert resp.status_code == 404, resp.text

    def test_404_when_task_not_defined(self, client):
        """未定义任务：无交付状态可言（404，不伪装成空状态）。"""
        client, app = client
        session_id = _create_session(client, app)
        resp = client.get(f"/api/sessions/{session_id}/task")
        assert resp.status_code == 404, resp.text

    def test_200_returns_full_projection(self, client):
        client, app = client
        session_id = _create_session(client, app)
        resp = _define(client, session_id)
        assert resp.status_code == 200, resp.text

        got = client.get(f"/api/sessions/{session_id}/task")
        assert got.status_code == 200, got.text
        payload = got.json()["task"]
        assert payload["defined"] is True
        assert payload["task_text"] == "把登录页修好"
        assert payload["cwd"] is not None, "cwd 从 session/started 单源锚读取"
        assert payload["product_state"] == "pending_verification"
        assert payload["version"] == 0
        assert payload["acceptance"] is None
        item_id = payload["criteria"][0]["item_id"]
        assert item_id.startswith("ac-")
        # 命令回执与查询是同一投影（不各算一套）
        assert resp.json()["task"] == payload


class TestDefineCommand:
    def test_200_appends_durable_definition(self, client):
        client, app = client
        session_id = _create_session(client, app)
        resp = _define(client, session_id, read_write_intent="只读 src/auth")
        assert resp.status_code == 200, resp.text
        events = app.state.agent.store.read_events(session_id)
        defined = [e for e in events if e.type == "task/defined"]
        assert len(defined) == 1
        assert defined[0].data["task_text"] == "把登录页修好"
        assert defined[0].data["read_write_intent"] == "只读 src/auth"

    def test_409_on_second_definition(self, client):
        client, app = client
        session_id = _create_session(client, app)
        assert _define(client, session_id).status_code == 200
        events_before = len(app.state.agent.store.read_events(session_id))
        resp = _define(client, session_id)
        assert resp.status_code == 409, resp.text
        assert len(app.state.agent.store.read_events(session_id)) == events_before, (
            "被拒请求零副作用"
        )

    def test_422_on_shape_error(self, client):
        client, app = client
        session_id = _create_session(client, app)
        resp = _define(client, session_id, task_text="   ")
        assert resp.status_code == 422, resp.text
        resp = _define(client, session_id, criteria=[{"text": "A", "origin": "model"}])
        assert resp.status_code == 422, resp.text


class TestAcceptanceRevisionCommand:
    def test_200_replaces_criteria_and_keeps_source(self, client):
        client, app = client
        session_id = _create_session(client, app)
        assert _define(client, session_id).status_code == 200
        resp = client.post(
            f"/api/sessions/{session_id}/task/acceptance-revision",
            json={"criteria": [{"text": "新判据", "origin": "agent"}]},
        )
        assert resp.status_code == 200, resp.text
        events = app.state.agent.store.read_events(session_id)
        revised = [e for e in events if e.type == "task/acceptance-revised"]
        assert len(revised) == 1
        assert revised[0].source_event_ids is not None, "保留旧版来源"
        assert resp.json()["task"]["criteria"][0]["confirmed"] is False

    def test_409_when_not_defined(self, client):
        client, app = client
        session_id = _create_session(client, app)
        resp = client.post(
            f"/api/sessions/{session_id}/task/acceptance-revision",
            json={"criteria": [{"text": "A"}]},
        )
        assert resp.status_code == 409, resp.text


class TestVerificationCommand:
    def test_200_appends_and_projects(self, client):
        client, app = client
        session_id = _create_session(client, app)
        item_id = _define(client, session_id).json()["task"]["criteria"][0]["item_id"]
        resp = client.post(
            f"/api/sessions/{session_id}/task/verification",
            json={"item_id": item_id, "value": "passed", "evidence": "12 passed"},
        )
        assert resp.status_code == 200, resp.text
        events = app.state.agent.store.read_events(session_id)
        assert events[-1].type == "verification/updated"
        assert events[-1].data == {
            "item_id": item_id,
            "value": "passed",
            "evidence": "12 passed",
        }
        assert resp.json()["task"]["verification"][item_id]["value"] == "passed"

    def test_422_unknown_item_and_bad_value(self, client):
        client, app = client
        session_id = _create_session(client, app)
        assert _define(client, session_id).status_code == 200
        resp = client.post(
            f"/api/sessions/{session_id}/task/verification",
            json={"item_id": "ac-none", "value": "passed"},
        )
        assert resp.status_code == 422, resp.text
        resp = client.post(
            f"/api/sessions/{session_id}/task/verification",
            json={"item_id": "ac-also-none", "value": "skipped"},
        )
        assert resp.status_code == 422, resp.text


class TestAcceptanceCommand:
    def _accept(self, client, session_id: str, **overrides) -> object:
        body = {"decision": "accepted", "expected_version": 0, **overrides}
        return client.post(f"/api/sessions/{session_id}/task/acceptance", json=body)

    def test_200_accept_then_409_double_write_and_stale(self, client):
        client, app = client
        session_id = _create_session(client, app)
        assert _define(client, session_id).status_code == 200
        resp = self._accept(client, session_id)
        assert resp.status_code == 200, resp.text
        assert resp.json()["task"]["product_state"] == "accepted"
        assert resp.json()["task"]["version"] == 1
        # 不能双写（明确 409，不是幂等假成功）：接受事件全流恰一条
        again = self._accept(client, session_id, expected_version=1)
        assert again.status_code == 409, again.text
        stale = self._accept(client, session_id, expected_version=0)
        assert stale.status_code == 409, stale.text
        events = app.state.agent.store.read_events(session_id)
        assert len([e for e in events if e.type == "task/accepted"]) == 1

    def test_422_gaps_requires_reason(self, client):
        client, app = client
        session_id = _create_session(client, app)
        assert _define(client, session_id).status_code == 200
        resp = self._accept(client, session_id, decision="accepted_with_gaps")
        assert resp.status_code == 422, resp.text

    def test_200_accept_with_gaps_keeps_verification_values(self, client):
        """带原因接受失败结果，验证值保持原样（票面「状态契约」）。"""
        client, app = client
        session_id = _create_session(client, app)
        task = _define(client, session_id).json()["task"]
        item_id = task["criteria"][0]["item_id"]
        assert (
            client.post(
                f"/api/sessions/{session_id}/task/verification",
                json={"item_id": item_id, "value": "failed", "evidence": "3 failed"},
            ).status_code
            == 200
        )
        resp = self._accept(
            client,
            session_id,
            decision="accepted_with_gaps",
            reason="失败项留到下批",
            expected_version=0,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()["task"]
        assert body["acceptance"] == {
            "decision": "accepted_with_gaps",
            "reason": "失败项留到下批",
        }
        assert body["verification"][item_id]["value"] == "failed"


class TestAcceptanceReleaseCommand:
    def _release(self, client, session_id: str, **overrides) -> object:
        body = {"expected_version": 1, **overrides}
        return client.post(
            f"/api/sessions/{session_id}/task/acceptance/release", json=body
        )

    def test_release_roundtrip_and_conflicts(self, client):
        client, app = client
        session_id = _create_session(client, app)
        assert _define(client, session_id).status_code == 200
        # 从未接受过 → 409（没有可释放的接受事实）
        never = self._release(client, session_id, expected_version=0)
        assert never.status_code == 409, never.text
        assert (
            client.post(
                f"/api/sessions/{session_id}/task/acceptance",
                json={"decision": "accepted", "expected_version": 0},
            ).status_code
            == 200
        )
        stale = self._release(client, session_id, expected_version=0)
        assert stale.status_code == 409, stale.text
        resp = self._release(client, session_id, reason="还要补测试")
        assert resp.status_code == 200, resp.text
        body = resp.json()["task"]
        assert body["acceptance"] is None
        assert body["version"] == 2
        events = app.state.agent.store.read_events(session_id)
        released = [e for e in events if e.type == "task/acceptance-released"]
        assert len(released) == 1
        assert released[0].source_event_ids is not None


class TestGetTaskRebuildsFromEvents:
    def test_projection_is_stateless_rebuild(self, client):
        """刷新 = 再 derive 一次：GET 两次同一结果，事件流是唯一事实。"""
        client, app = client
        session_id = _create_session(client, app)
        item_id = _define(client, session_id).json()["task"]["criteria"][0]["item_id"]
        client.post(
            f"/api/sessions/{session_id}/task/verification",
            json={"item_id": item_id, "value": "passed"},
        )
        client.post(
            f"/api/sessions/{session_id}/task/acceptance",
            json={"decision": "accepted", "expected_version": 0},
        )
        first = client.get(f"/api/sessions/{session_id}/task").json()
        second = client.get(f"/api/sessions/{session_id}/task").json()
        assert first == second
        assert first["task"]["product_state"] == "accepted"

class TestCreationPathWiring:
    def test_create_with_task_appends_definition(self, client):
        """票面 AC「真实现有 run 接入」：创建带任务的会话 → task/defined 落盘，
        Task 身份 = Session ID（一 Task 多 Run 的第一根锚）。"""
        client, app = client
        resp = client.post("/api/sessions", json={"task": "把登录页修好"})
        assert resp.status_code == 200, resp.text
        session = app.state._launched_sessions[0]
        events = app.state.agent.store.read_events(session.session_id)
        defined = [e for e in events if e.type == "task/defined"]
        assert len(defined) == 1
        assert defined[0].data["task_text"] == "把登录页修好"
        got = client.get(f"/api/sessions/{session.session_id}/task")
        assert got.status_code == 200, got.text
        payload = got.json()["task"]
        assert payload["task_text"] == "把登录页修好"
        assert payload["criteria"] == [], "验收清单缺省为空，Agent 稍后提出"
        assert payload["product_state"] == "pending_verification"
        assert payload["version"] == 0

    def test_blank_task_skips_definition_but_session_still_launches(self, client):
        """空白任务维持既有行为照常起跑；定义缺失如实可见（GET /task → 404）。"""
        client, app = client
        resp = client.post("/api/sessions", json={"task": "   "})
        assert resp.status_code == 200, resp.text
        session = app.state._launched_sessions[0]
        events = app.state.agent.store.read_events(session.session_id)
        assert not [e for e in events if e.type == "task/defined"]
        got = client.get(f"/api/sessions/{session.session_id}/task")
        assert got.status_code == 404

