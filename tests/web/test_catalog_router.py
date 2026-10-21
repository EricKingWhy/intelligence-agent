"""#255：只读 Catalog/Profile router 的依赖接缝与注册契约。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_harness.capability.base import CapabilityRegistry
from agent_harness.capability.wiring import CapabilityWiring
from agent_harness.config import Settings
from agent_harness.web.catalog import (
    get_catalog_state,
    register_catalog_routes,
)


class _FakeState:
    # `CatalogState` 协议声明 settings 是必需字段（#1420 起 `/api/capabilities`
    # 读 `settings.insights_enabled`）；fake 只实现 get_wiring 会让冒烟打成
    # AttributeError——补上最小实现，而不是让生产代码用 getattr 兜 fake 的缺。
    settings = Settings()

    async def get_wiring(self):
        return CapabilityRegistry(), CapabilityWiring()


def test_catalog_router_is_independently_smoke_testable_with_fake_dependency():
    app = FastAPI()
    app.state.agent = object()
    app.dependency_overrides[get_catalog_state] = lambda: _FakeState()
    register_catalog_routes(app)

    client = TestClient(app)
    assert client.get("/api/reasoning-efforts").status_code == 200
    assert client.get("/api/agent-profiles").status_code == 200
    assert client.get("/api/capabilities").status_code == 200
    assert client.get("/api/context-providers").json() == {"providers": []}


def test_catalog_router_registers_stable_read_only_operations():
    app = FastAPI()
    register_catalog_routes(app)
    expected_paths = {
        "/api/models",
        "/api/permission-modes",
        "/api/capabilities",
        "/api/reasoning-efforts",
        "/api/agent-profiles",
        "/api/context-providers",
    }
    assert set(app.openapi()["paths"]) >= expected_paths
    assert all(
        set(app.openapi()["paths"][path]) == {"get"}
        for path in expected_paths
    )
