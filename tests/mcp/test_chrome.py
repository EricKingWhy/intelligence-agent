"""#362 [W-18]：Chrome DevTools MCP 配置预设 + 风险映射测试。"""
import pytest

from agent_harness.mcp.chrome import (
    CHROME_DEVTOOLS_MCP_NPM,
    CHROME_TOOL_PERMISSIONS,
    LOGIN_REUSE_MODES,
    chrome_mcp_server_config,
)
from agent_harness.tooling.contract import ToolPermission


class TestChromeMcpServerConfig:
    def test_isolated_is_default(self):
        cfg = chrome_mcp_server_config()
        assert cfg.transport == "stdio"
        assert cfg.command == "npx"
        assert "--isolated" in cfg.args
        assert CHROME_DEVTOOLS_MCP_NPM in cfg.args
        # 不收集使用统计（隐私：默认启用，显式关掉）
        assert "--no-usage-statistics" in cfg.args

    def test_dedicated_has_no_profile_flags(self):
        cfg = chrome_mcp_server_config(mode="dedicated")
        assert "--isolated" not in cfg.args
        assert "--autoConnect" not in cfg.args

    def test_autoconnect(self):
        cfg = chrome_mcp_server_config(mode="autoconnect")
        assert "--autoConnect" in cfg.args

    def test_browser_url_requires_param(self):
        with pytest.raises(ValueError, match="browser_url"):
            chrome_mcp_server_config(mode="browser_url")
        cfg = chrome_mcp_server_config(
            mode="browser_url", browser_url="http://127.0.0.1:9222"
        )
        assert "--browser-url=http://127.0.0.1:9222" in cfg.args

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="未知 mode"):
            chrome_mcp_server_config(mode="yolo")

    def test_login_reuse_modes_declared(self):
        assert set(LOGIN_REUSE_MODES) == {"autoconnect", "browser_url"}


class TestChromeToolPermissions:
    def test_read_tools_are_read_only(self):
        for tool in (
            "take_snapshot", "take_screenshot", "list_console_messages",
            "list_network_requests", "list_pages", "performance_start_trace",
        ):
            assert CHROME_TOOL_PERMISSIONS[tool] is ToolPermission.READ_ONLY, tool

    def test_write_tools_are_danger(self):
        for tool in (
            "click", "fill", "fill_form", "type_text", "upload_file",
            "handle_dialog",  # 原生弹窗处理是副作用，"Chrome 原生弹窗 ≠ 本仓授权"
            "navigate_page", "new_page", "close_page",
            "evaluate_script",  # 任意 JS 执行
            "install_extension",
        ):
            assert CHROME_TOOL_PERMISSIONS[tool] is ToolPermission.DANGER, tool

    def test_config_carries_permissions(self):
        cfg = chrome_mcp_server_config()
        assert cfg.tool_permissions["click"] is ToolPermission.DANGER
        assert cfg.tool_permissions["take_snapshot"] is ToolPermission.READ_ONLY
