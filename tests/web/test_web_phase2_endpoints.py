"""Phase 2 只读端点测试（SDD 03 §10 + §17）。

覆盖：
- GET /api/permission-modes：返回三个真实 PermissionPolicy + 描述；
- GET /api/capabilities：默认空（CAPABILITIES=""）→ {"capabilities": []}；
- GET /api/capabilities：注入 fake descriptor → 投影 manifest（含 surfaces 默认值）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_harness.capability.base import (
    CapabilityDescriptor,
    CapabilityRegistry,
    Degradation,
)
from agent_harness.config import Settings
from agent_harness.web.app import create_app


@pytest.fixture
def bare_client(tmp_path):
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path),
        model_api_key="sk-test", enable_cors=False,
    )
    return TestClient(create_app(settings, enable_cors=False))


# ── GET /api/permission-modes ──

class TestPermissionModes:
    def test_lists_real_policies(self, bare_client):
        """返回三个真实 PermissionPolicy + 人类可读描述。"""
        resp = bare_client.get("/api/permission-modes")
        assert resp.status_code == 200
        body = resp.json()
        modes = body["modes"]
        ids = [m["id"] for m in modes]
        assert ids == ["read-only", "workspace-write", "danger-full-access"]
        for m in modes:
            assert m["display_name"], "每个 mode 必须有 display_name"
            assert m["description"], "每个 mode 必须有 description"


# ── GET /api/capabilities ──

class TestCapabilities:
    def test_empty_when_not_configured(self, bare_client):
        """默认 CAPABILITIES="" → registry 空 → {"capabilities": []}。

        空就是空（SDD 决策）：不假装有基础能力，前端据空列表自行 fallback。
        """
        resp = bare_client.get("/api/capabilities")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"capabilities": []}

    def test_reflects_registry(self, tmp_path, monkeypatch):
        """注入一个 fake descriptor → 端点投影出 manifest（含 surfaces 默认值）。"""
        fake_descriptor = CapabilityDescriptor(
            name="memory",
            version="1.0.0",
            provider_name="builtin",
            degradation=Degradation.OPTIONAL_RUNTIME,
        )
        fake_registry = CapabilityRegistry()
        fake_registry.register(fake_descriptor, provider=object())

        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path),
            model_api_key="sk-test", enable_cors=False,
            # 用一个真实 capability 配置触发装配（但我们会替换 registry）
            capabilities='{"memory": {"provider": "builtin"}}',
        )
        app = create_app(settings, enable_cors=False)

        # 注入 fake registry：绕过真实装配，直接替换 get_wiring 的产物。
        async def stub_get_wiring():
            app.state.agent._registry = fake_registry
            # wiring 字段必须非 None 才能跳过真实装配路径
            from agent_harness.capability.wiring import CapabilityWiring
            app.state.agent._wiring = CapabilityWiring()
            return app.state.agent._registry, app.state.agent._wiring

        app.state.agent.get_wiring = stub_get_wiring
        client = TestClient(app)

        resp = client.get("/api/capabilities")
        assert resp.status_code == 200
        body = resp.json()
        caps = body["capabilities"]
        assert len(caps) == 1
        cap = caps[0]
        assert cap["id"] == "memory"
        assert cap["display_name"] == "memory"  # display_name 缺省回落 name
        assert cap["version"] == "1.0.0"
        assert cap["provider_name"] == "builtin"
        # 未声明 surfaces → 保守默认：chat/timeline=true，余=false
        assert cap["surfaces"] == {
            "chat": True, "timeline": True,
            "changes": False, "terminal": False, "artifacts": False,
        }
        # 未声明 actions → 全 false
        assert cap["actions"] == {
            "permissions": False, "stop": False,
            "retry": False, "resume": False,
        }

    def test_descriptor_surfaces_override_default(self, tmp_path):
        """descriptor 显式声明 surfaces → 以声明为准（不替换为默认）。"""
        fake_descriptor = CapabilityDescriptor(
            name="memory",
            version="1.0.0",
            provider_name="builtin",
            degradation=Degradation.OPTIONAL_RUNTIME,
            surfaces={"chat": True, "timeline": False, "artifacts": True},
        )
        fake_registry = CapabilityRegistry()
        fake_registry.register(fake_descriptor, provider=object())

        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path),
            model_api_key="sk-test", enable_cors=False,
            capabilities='{"memory": {"provider": "builtin"}}',
        )
        app = create_app(settings, enable_cors=False)

        async def stub_get_wiring():
            from agent_harness.capability.wiring import CapabilityWiring
            app.state.agent._registry = fake_registry
            app.state.agent._wiring = CapabilityWiring()
            return app.state.agent._registry, app.state.agent._wiring

        app.state.agent.get_wiring = stub_get_wiring
        client = TestClient(app)

        resp = client.get("/api/capabilities")
        body = resp.json()
        cap = body["capabilities"][0]
        # 显式声明覆盖默认：timeline=false（而非默认 true），artifacts=true
        assert cap["surfaces"]["timeline"] is False
        assert cap["surfaces"]["artifacts"] is True
        # 未声明的键（changes/terminal）仍回落保守默认 false
        assert cap["surfaces"]["changes"] is False
        assert cap["surfaces"]["terminal"] is False
