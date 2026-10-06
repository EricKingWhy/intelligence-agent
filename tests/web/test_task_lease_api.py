"""W-10（#354）：单目录写入租约的 minimal REST 面（acquire/release/cancel/status）。

fixture 策略对齐 `tests/web/test_task_delivery_api.py`（patch build_runtime/
launch，`POST /api/sessions?launch=false` 造会话再打新端点）。错误码口径：
形状非法 422、冲突 409、未知会话 404；被拒请求零副作用。
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
    return TestClient(app), app, tmp_path


def _create_session(client: TestClient, tmp_path, name: str) -> str:
    """造一个显式 cwd 的空会话（workspace 目录必须已存在，ADR-0025 口径）。"""
    cwd = tmp_path / "dirs" / name
    cwd.mkdir(parents=True, exist_ok=True)
    resp = client.post("/api/sessions?launch=false", json={"cwd": str(cwd)})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def test_acquire_grants_then_queues_same_dir(client) -> None:
    client_obj, _app, tmp_path = client
    sid_a = _create_session(client_obj, tmp_path, "a")
    sid_b = _create_session(client_obj, tmp_path, "b")
    first = client_obj.post(f"/api/sessions/{sid_a}/task/lease/acquire", json={})
    assert first.status_code == 200
    assert first.json()["granted"] is True
    # sid_b 的 cwd 是自己的目录；显式指定 sid_a 的目录 = 排队等待同目录。
    second = client_obj.post(
        f"/api/sessions/{sid_b}/task/lease/acquire",
        json={"path": str(tmp_path / "dirs" / "a")},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["granted"] is False
    assert body["queue_position"] == 1
    # 状态面：持有 vs 排队。
    held = client_obj.get(f"/api/sessions/{sid_a}/task/lease").json()["lease"]
    queued = client_obj.get(f"/api/sessions/{sid_b}/task/lease").json()["lease"]
    assert held["held"]["dir_path"] == str(tmp_path / "dirs" / "a")
    assert queued["queued"]["position"] == 1


def test_release_then_cancel_roundtrip(client) -> None:
    client_obj, _app, tmp_path = client
    sid_a = _create_session(client_obj, tmp_path, "a")
    sid_b = _create_session(client_obj, tmp_path, "b")
    client_obj.post(f"/api/sessions/{sid_a}/task/lease/acquire", json={})
    client_obj.post(
        f"/api/sessions/{sid_b}/task/lease/acquire",
        json={"path": str(tmp_path / "dirs" / "a")},  # 排队等待 sid_a 的目录
    )
    released = client_obj.post(f"/api/sessions/{sid_a}/task/lease/release", json={})
    assert released.status_code == 200
    body = released.json()
    assert body["released"] is True
    assert body["promoted_to"] is None  # 缺省 presence：无人在场不自动启动
    cancelled = client_obj.post(
        f"/api/sessions/{sid_b}/task/lease/queue/cancel", json={}
    )
    assert cancelled.json() == {"cancelled": True}
    again = client_obj.post(
        f"/api/sessions/{sid_b}/task/lease/queue/cancel", json={}
    )
    assert again.json() == {"cancelled": False}  # 幂等


def test_duplicate_release_is_idempotent(client) -> None:
    client_obj, _app, tmp_path = client
    sid = _create_session(client_obj, tmp_path, "a")
    client_obj.post(f"/api/sessions/{sid}/task/lease/acquire", json={})
    assert client_obj.post(f"/api/sessions/{sid}/task/lease/release", json={}).json()[
        "released"
    ]
    assert client_obj.post(f"/api/sessions/{sid}/task/lease/release", json={}).json()[
        "released"
    ] is False


def test_path_errors_map_422_and_409(client) -> None:
    client_obj, _app, tmp_path = client
    sid = _create_session(client_obj, tmp_path, "a")
    dir_a = tmp_path / "dirs" / "a"
    # 不存在的目录（fail-closed）→ 422。
    missing = client_obj.post(
        f"/api/sessions/{sid}/task/lease/acquire",
        json={"path": str(tmp_path / "dirs" / "nope")},
    )
    assert missing.status_code == 422
    # 先取得 dir_a 租约，再对子目录申请 → 409（父子相交，要求另选独立目录）。
    granted = client_obj.post(
        f"/api/sessions/{sid}/task/lease/acquire", json={"path": str(dir_a)}
    )
    assert granted.status_code == 200 and granted.json()["granted"] is True
    child = dir_a / "child"
    child.mkdir()
    conflict = client_obj.post(
        f"/api/sessions/{sid}/task/lease/acquire",
        json={"path": str(child)},
    )
    assert conflict.status_code == 409


def test_unknown_session_maps_404(client) -> None:
    client_obj, _app, _tmp = client
    resp = client_obj.post(
        "/api/sessions/doesnotexistsessionid000000/task/lease/acquire", json={}
    )
    assert resp.status_code == 404


def test_explicit_path_uses_user_chosen_dir(client) -> None:
    """"用户另选独立目录"流程：显式 path 覆盖默认 cwd。"""
    client_obj, _app, tmp_path = client
    sid = _create_session(client_obj, tmp_path, "a")
    other = tmp_path / "dirs" / "chosen"
    other.mkdir()
    resp = client_obj.post(
        f"/api/sessions/{sid}/task/lease/acquire", json={"path": str(other)}
    )
    assert resp.status_code == 200 and resp.json()["granted"] is True
    status = client_obj.get(f"/api/sessions/{sid}/task/lease").json()["lease"]
    assert status["held"]["dir_path"] == str(other)
