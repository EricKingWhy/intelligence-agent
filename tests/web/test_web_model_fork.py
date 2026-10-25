"""T7 (#137) web 契约：``POST /api/sessions/{id}/model`` + ``POST /api/sessions/{id}/forks``。

契约：`docs/integration/PRD_PHASE_MULTITURN_TOTAL.md` §2.3 / §2.4。
service 层语义见 `tests/session/test_model_change.py`。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent_harness.sandbox.registry as sandbox_registry
from agent_harness.config import Settings
from agent_harness.sandbox.capabilities import SandboxCapabilities
from agent_harness.session.event import (
    MODEL_CHANGED,
    RUN_COMPLETED,
    RUN_STARTED,
    USER_MESSAGE,
)
from agent_harness.session.session import Session
from agent_harness.web import domain_errors
from agent_harness.web.app import create_app
from tests.session.registry_doubles import RecordingRegistry

_CATALOG_JSON = (
    '[{"name": "gpt-4o", "provider": "deepseek", "model_name": "gpt-4o-mini"},'
    ' {"name": "mimo-flash", "provider": "mimo", "model_name": "mimo-v2.6-flash"}]'
)


def _make_docker_unavailable(monkeypatch) -> None:
    """让 docker 探针确定性答"不可用"（本机有无 Docker 都不影响结论）。

    同时固定 409 detail 里的可选后端列表：`probe_all_capabilities` 会真探
    daemon，读 `available_backends` 的断言不该依赖本机环境。
    """
    monkeypatch.setattr(
        sandbox_registry,
        "probe_docker_capabilities",
        lambda *a, **k: SandboxCapabilities(
            backend="docker",
            available=False,
            reason="Docker Python SDK 未安装",
            details={"remediation": "pip install docker"},
        ),
    )
    monkeypatch.setattr(
        domain_errors,
        "probe_all_capabilities",
        lambda *a, **k: [
            SandboxCapabilities(backend="local", available=True),
            SandboxCapabilities(
                backend="docker", available=False, reason="Docker Python SDK 未安装"
            ),
        ],
    )


@pytest.fixture
def client(tmp_path):
    # provider_store_path 钉进 tmp_path：自定义 provider 存储默认读 HOME 用户态，
    # 不密封会把宿主真配置（如自定义 provider 条目）泄进 catalog 断言。
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_provider="deepseek",
        model_name="deepseek-chat",
        agent_models=_CATALOG_JSON,
        provider_store_path=str(tmp_path / "model-providers.json"),
    )
    return TestClient(create_app(settings, enable_cors=False, allow_testserver=True))


def _seed(client, session_id: str = "sid-1") -> None:
    Session.start(
        client.app.state.agent.store,
        session_id=session_id,
        workspace_registry=client.app.state.agent.workspace_registry,
        sandbox_backend="local",
        started_data={"provider": "deepseek", "model_id": "gpt-4o"},
    )


def _seed_forkable(client, session_id: str = "sid-1") -> None:
    session = Session.start(
        client.app.state.agent.store,
        session_id=session_id,
        workspace_registry=client.app.state.agent.workspace_registry,
        sandbox_backend="local",
        started_data={"provider": "deepseek", "model_id": "gpt-4o"},
    )
    session.append(USER_MESSAGE, {"content": "first"})
    session.append(RUN_STARTED, {})
    session.append(RUN_COMPLETED, {})


class TestModelEndpoint:
    def test_change_model_returns_changed(self, client):
        _seed(client)

        resp = client.post(
            "/api/sessions/sid-1/model", json={"provider": "mimo", "model_id": "mimo-flash"}
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "changed",
            "provider": "mimo",
            "model_id": "mimo-flash",
        }
        events = client.app.state.agent.store.read_events("sid-1")
        assert events[-1].type == MODEL_CHANGED

    def test_unknown_model_422(self, client):
        _seed(client)

        resp = client.post(
            "/api/sessions/sid-1/model", json={"provider": "mimo", "model_id": "nope"}
        )

        assert resp.status_code == 422

    def test_missing_session_404(self, client):
        resp = client.post(
            "/api/sessions/nope/model", json={"provider": "mimo", "model_id": "mimo-flash"}
        )

        assert resp.status_code == 404

    def test_blank_model_id_422(self, client):
        _seed(client)

        resp = client.post(
            "/api/sessions/sid-1/model", json={"provider": "mimo", "model_id": ""}
        )

        assert resp.status_code == 422

    def test_response_returns_canonical_entry_name(self, client):
        """请求给上游 model_name，响应回 catalog 条目名（与事件 / GET /api/models 对齐）。"""
        _seed(client)

        resp = client.post(
            "/api/sessions/sid-1/model",
            json={"provider": "deepseek", "model_id": "gpt-4o-mini"},
        )

        assert resp.status_code == 200
        assert resp.json()["model_id"] == "gpt-4o"
        events = client.app.state.agent.store.read_events("sid-1")
        assert events[-1].data["to_model_id"] == "gpt-4o"

    def test_default_entry_returns_default_model_name_and_clears_override(self, client):
        _seed(client)

        resp = client.post(
            "/api/sessions/sid-1/model",
            json={"provider": "deepseek", "model_id": "deepseek-chat"},
        )

        assert resp.status_code == 200
        assert resp.json()["model_id"] == "deepseek-chat"
        events = client.app.state.agent.store.read_events("sid-1")
        assert events[-1].data["to_model_id"] is None


class TestForkEndpoint:
    def test_fork_returns_child_session_id(self, client):
        _seed_forkable(client)

        resp = client.post("/api/sessions/sid-1/forks", json={"from_seq": 1})

        assert resp.status_code == 200
        body = resp.json()
        assert body["from_seq"] == 1
        assert body["session_id"] != "sid-1"
        assert client.app.state.agent.store.read_events(body["session_id"])

    def test_missing_session_404(self, client):
        resp = client.post("/api/sessions/nope/forks", json={"from_seq": 0})

        assert resp.status_code == 404

    def test_invalid_boundary_422(self, client):
        _seed(client)

        resp = client.post("/api/sessions/sid-1/forks", json={"from_seq": 999})

        assert resp.status_code == 422

    def test_negative_from_seq_422(self, client):
        _seed(client)

        resp = client.post("/api/sessions/sid-1/forks", json={"from_seq": -1})

        assert resp.status_code == 422

    def test_unknown_sandbox_backend_422(self, client):
        """#1278：ForkRequest 接受 sandbox_backend，未知值在请求层响亮 422。"""
        _seed_forkable(client)

        resp = client.post(
            "/api/sessions/sid-1/forks",
            json={"from_seq": 1, "sandbox_backend": "windows-sandbox"},
        )

        assert resp.status_code == 422
        assert "sandbox_backend must be one of" in resp.text

    def test_fork_inherits_parent_sandbox_backend(self, client):
        """#1278：web 全链（route → service → fork_session）缺省继承父的显式选择。

        fake registry 的缺省 create(backend=None) 与 "local" 可区分——这正是
        Windows「父选 local、child 落到部署默认 docker」的复现形状。
        """
        registry = RecordingRegistry()
        client.app.state.agent.workspace_registry = registry
        store = client.app.state.agent.store
        Session.start(store, session_id="sid-1", sandbox_backend="local")
        session = Session.load(store, "sid-1")
        session.append(USER_MESSAGE, {"content": "first"})
        session.append(RUN_STARTED, {})
        session.append(RUN_COMPLETED, {})

        resp = client.post("/api/sessions/sid-1/forks", json={"from_seq": 1})

        assert resp.status_code == 200
        child_id = resp.json()["session_id"]
        assert registry.created == [(child_id, "local")]
        started = client.app.state.agent.store.read_events(child_id)[0]
        assert started.type == "session/started"
        assert started.data["sandbox_backend"] == "local"

    def test_unavailable_explicit_backend_409(self, client, monkeypatch):
        """#1278 P2-2：显式传 docker 但本机不可用 ⇒ 与 create 同一份结构化 409。"""
        _seed_forkable(client)
        _make_docker_unavailable(monkeypatch)

        resp = client.post(
            "/api/sessions/sid-1/forks",
            json={"from_seq": 1, "sandbox_backend": "docker"},
        )

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["backend"] == "docker"
        assert detail["remediation"] == "pip install docker"
        assert detail["available_backends"] == ["local"]

    def test_unavailable_inherited_backend_409(self, client, monkeypatch):
        """#1278 P2-2：继承来的 docker 已不可用同样 409（不是裸 500）。"""
        store = client.app.state.agent.store
        session = Session.start(
            store,
            session_id="sid-1",
            started_data={
                "provider": "deepseek",
                "model_id": "gpt-4o",
                "sandbox_backend": "docker",
            },
        )
        session.append(USER_MESSAGE, {"content": "first"})
        session.append(RUN_STARTED, {})
        session.append(RUN_COMPLETED, {})
        _make_docker_unavailable(monkeypatch)

        resp = client.post("/api/sessions/sid-1/forks", json={"from_seq": 1})

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["backend"] == "docker"
        assert detail["available_backends"] == ["local"]


class TestModelsEndpointShadowing:
    def test_entry_shadowed_by_default_is_not_listed(self, tmp_path):
        """catalog 条目与默认条目同 provider + 同名 → POST /model 会解析成默认链，
        列表里不该出现这个选不中的死选项。"""
        settings = Settings(
            _env_file=None,
            workspace_dir=str(tmp_path),
            model_api_key="sk-test",
            model_provider="deepseek",
            model_name="deepseek-chat",
            agent_models=(
                '[{"name": "deepseek-chat", "provider": "deepseek",'
                ' "model_name": "deepseek-reasoner"},'
                ' {"name": "gpt-4o", "provider": "deepseek",'
                ' "model_name": "gpt-4o-mini"}]'
            ),
            # provider_store_path 钉进 tmp_path：自定义 provider 存储默认读 HOME
            # 用户态，不密封会把宿主真配置泄进 catalog 断言。
            provider_store_path=str(tmp_path / "model-providers.json"),
        )
        client = TestClient(create_app(settings, enable_cors=False, allow_testserver=True))

        ids = [m["id"] for m in client.get("/api/models").json()["models"]]

        assert ids == ["deepseek-chat", "gpt-4o"]
