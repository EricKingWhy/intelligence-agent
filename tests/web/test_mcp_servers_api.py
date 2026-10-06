"""#362 [W-18]：MCP server 状态查询 + 按名断开 API 测试。"""
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "ws"),
        model_api_key="test-key",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


class TestListMcpServers:
    def test_no_mcp_configured_returns_empty(self, client):
        # "没配 MCP" 是缺省态，不是故障——空列表不 503
        r = client.get("/api/mcp/servers")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["servers"] == []


class TestDisconnectMcpServer:
    def test_unknown_server_is_idempotent(self, client):
        r = client.post("/api/mcp/servers/nonexistent/disconnect")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "nonexistent"
        assert body["disconnected"] is False


class TestMcpCapabilityDisconnect:
    """MCPCapability.disconnect_server 单元测试（用假连接，不起真实 server）。"""

    @pytest.mark.anyio
    async def test_disconnect_removes_and_closes(self):
        from agent_harness.mcp.capability import MCPCapability

        closed = []

        class FakeConnection:
            config_name = "chrome-devtools"
            connected = True

            async def aclose(self):
                closed.append(self.config_name)
                self.connected = False

        conn = FakeConnection()
        cap = MCPCapability(connections=[conn], tools=[], errors=[])
        assert cap.is_connected("chrome-devtools") is True
        assert await cap.disconnect_server("chrome-devtools") is True
        assert closed == ["chrome-devtools"]
        assert cap.is_connected("chrome-devtools") is False
        assert cap.server_names() == []

    @pytest.mark.anyio
    async def test_disconnect_unknown_is_false(self):
        from agent_harness.mcp.capability import MCPCapability

        cap = MCPCapability(connections=[], tools=[], errors=[])
        assert await cap.disconnect_server("nope") is False
