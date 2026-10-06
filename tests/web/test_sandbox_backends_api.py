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
    def test_lists_backends(self, client):
        r = client.get("/api/sandbox-backends")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "backends" in body
        backends = {b["backend"]: b for b in body["backends"]}
        # local 恒可用（本机）；docker 按探针结果如实报告
        assert "local" in backends
        assert backends["local"]["available"] is True
        assert "docker" in backends
        assert isinstance(backends["docker"]["available"], bool)
        # 不可用时必须带原因（前端据此置灰，不画饼）
        for b in body["backends"]:
            if not b["available"]:
                assert b.get("reason"), f"{b['backend']} 不可用却无 reason"
