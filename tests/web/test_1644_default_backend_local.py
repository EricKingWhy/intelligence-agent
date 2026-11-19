"""#1644：Windows 默认后端改 local，docker 改为显式可选（后端验收）。

判据句（票面「不做」后）：**显式选择**（`sandbox_backend` 出现在请求体里）=
用户意志，不回落，仍 409；**部署默认**（未显式）= 没有选择，才落 local。

本文件钉 HTTP 边界上的三件事（前端 409 卡片/e2e 由另派执行器做）：

1. 部署默认路径（含模拟 Windows 平台默认）建会话成功、映射 `backend == "local"`、
   **不调用** `probe_docker_capabilities`；`session/started` 不含 `sandbox_backend`
   键（守住 `Session.start` 的「显式/默认两档」语义）。
2. 显式 `docker` + 不可用 ⇒ 409 + 结构化诊断（`backend` / `remediation` /
   `available_backends` 含 `"local"`）；显式 `local` 建得成且映射 local。
3. fork 默认会话（父没显式选）走部署默认 → local。

Watchtower 规则 `sandbox_backend=None` 的对应主张在
`tests/watchtower/test_service.py::test_rule_without_backend_passes_none_to_deployment_default`
（规则层只管「不替用户做选择」，落 local 由部署默认保证）。

「模拟 Windows 部署默认」用 `WorkspaceRegistry(root, backend=default_backend_for_platform("nt"))`
——那正是 `AppState.__init__` 在 `os.name == "nt"` 时构造的东西（同一个函数、
同一条注入缝），因此本文件在 Linux 上也能真实判红/判绿。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import agent_harness.sandbox.registry as sandbox_registry
from agent_harness.config import Settings
from agent_harness.sandbox.capabilities import (
    SandboxCapabilities,
    default_backend_for_platform,
)
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session.event import RUN_COMPLETED, RUN_STARTED, USER_MESSAGE
from agent_harness.session.session import Session
from agent_harness.web import domain_errors
from agent_harness.web.app import create_app


def _app(tmp_path: Path):
    return create_app(
        Settings(
            _env_file=None,
            workspace_dir=str(tmp_path),
            model_api_key="sk-test",
            model_provider="deepseek",
            model_name="deepseek-chat",
        ),
        enable_cors=False,
        allow_testserver=True,
    )


def _windows_default_registry(tmp_path: Path) -> WorkspaceRegistry:
    """`AppState.__init__` 在 Windows 上会构造的那个 registry（同一注入缝）。"""
    return WorkspaceRegistry(
        root=tmp_path, backend=default_backend_for_platform("nt")
    )


def _counting_probe(monkeypatch) -> list[str]:
    """把 docker 探针换成计数桩（真探针会读 daemon，断言不该依赖本机环境）。"""
    calls: list[str] = []

    def probe(*_a, **_k):
        calls.append("probe")
        return SandboxCapabilities(
            backend="docker",
            available=False,
            reason="Docker Python SDK 未安装",
            details={"remediation": "pip install docker"},
        )

    monkeypatch.setattr(sandbox_registry, "probe_docker_capabilities", probe)
    return calls


def _make_docker_unavailable(monkeypatch) -> None:
    """显式 docker 时确定性答「不可用」，并固定 409 的 available_backends。"""
    _counting_probe(monkeypatch)
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


def _mapping(tmp_path: Path, session_id: str) -> dict:
    path = tmp_path / "workspaces" / f"{session_id}.json"
    assert path.is_file(), f"映射文件不存在：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _started(client: TestClient, session_id: str):
    events = client.app.state.agent.store.read_events(session_id)
    return next(e for e in events if e.type == "session/started")


class TestDeploymentDefaultIsLocal:
    def test_default_create_uses_local_without_probing_docker(
        self, tmp_path, monkeypatch
    ):
        """AC1：默认创建落 local、零探针、session/started 不写 sandbox_backend。"""
        calls = _counting_probe(monkeypatch)
        app = _app(tmp_path)
        app.state.agent.workspace_registry = _windows_default_registry(tmp_path)
        client = TestClient(app)

        resp = client.post("/api/sessions?launch=false", json={})

        assert resp.status_code == 200, resp.text
        session_id = resp.json()["session_id"]
        assert calls == [], "默认路径不该探 docker（registry 只对 docker 后端探针）"
        assert _mapping(tmp_path, session_id)["backend"] == "local"
        assert "sandbox_backend" not in _started(client, session_id).data

    def test_explicit_local_create_succeeds(self, tmp_path, monkeypatch):
        """AC2 后半：显式 local 建得成，映射 local（显式也记进 session/started）。"""
        _counting_probe(monkeypatch)
        client = TestClient(_app(tmp_path))

        resp = client.post(
            "/api/sessions?launch=false", json={"sandbox_backend": "local"}
        )

        assert resp.status_code == 200, resp.text
        session_id = resp.json()["session_id"]
        assert _mapping(tmp_path, session_id)["backend"] == "local"
        assert _started(client, session_id).data["sandbox_backend"] == "local"


class TestExplicitDockerStaysFailClosed:
    def test_explicit_docker_unavailable_is_409_with_diagnostics(
        self, tmp_path, monkeypatch
    ):
        """AC2：显式 docker 不可用 ⇒ 409 + 结构化诊断（不静默回落 local）。"""
        _make_docker_unavailable(monkeypatch)
        client = TestClient(_app(tmp_path))

        resp = client.post(
            "/api/sessions?launch=false", json={"sandbox_backend": "docker"}
        )

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["backend"] == "docker"
        assert detail["remediation"]
        assert "local" in detail["available_backends"]


class TestForkDefaultInheritsDeploymentDefault:
    def test_fork_of_default_parent_lands_local(self, tmp_path, monkeypatch):
        """AC1 后半：父没显式选 ⇒ child 走部署默认（nt 上 = local）。"""
        calls = _counting_probe(monkeypatch)
        app = _app(tmp_path)
        app.state.agent.workspace_registry = _windows_default_registry(tmp_path)
        client = TestClient(app)
        store = app.state.agent.store
        parent = Session.start(
            store,
            session_id="sid-1",
            workspace_registry=app.state.agent.workspace_registry,
            started_data={"provider": "deepseek", "model_id": "deepseek-chat"},
        )
        parent.append(USER_MESSAGE, {"content": "first"})
        parent.append(RUN_STARTED, {})
        parent.append(RUN_COMPLETED, {})

        resp = client.post("/api/sessions/sid-1/forks", json={"from_seq": 1})

        assert resp.status_code == 200, resp.text
        child_id = resp.json()["session_id"]
        assert _mapping(tmp_path, child_id)["backend"] == "local"
        assert calls == [], "fork 的默认路径也不该探 docker"
        assert "sandbox_backend" not in _started(client, child_id).data
