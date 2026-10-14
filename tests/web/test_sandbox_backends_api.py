"""#367 [W-23] 选项 A：sandbox 后端列表端点 TDD（先红）。

`probe_all_capabilities` 明确标注"给选择器 UI 用"——选项 A 的"运行位置"
选择器需要真实可用性数据（不画饼：只显示实际探针结果）。
"""

import pytest
from starlette.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "ws"),
        model_api_key="sk-test",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


class TestGetSandboxBackends:
    @pytest.mark.parametrize(
        "path", ["/api/sandbox-backends", "/api/sandbox/capabilities"]
    )
    def test_lists_only_selectable_backends(self, client, path):
        r = client.get(path)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "backends" in body
        backends = {b["backend"]: b for b in body["backends"]}
        # local 恒可用（本机）；docker 按探针结果如实报告
        assert set(backends) == {"local", "docker"}
        assert "local" in backends
        assert backends["local"]["available"] is True
        assert "docker" in backends
        assert isinstance(backends["docker"]["available"], bool)
        # 不可用时必须带原因（前端据此置灰，不画饼）
        for b in body["backends"]:
            if not b["available"]:
                assert b.get("reason"), f"{b['backend']} 不可用却无 reason"

    def test_new_session_cannot_request_legacy_windows_psec(self, client):
        response = client.post(
            "/api/sessions",
            json={"task": "test", "sandbox_backend": "windows_psec"},
        )

        assert response.status_code == 422
        assert "sandbox_backend must be one of: local, docker" in response.text
