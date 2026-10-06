"""Chrome DevTools MCP server 配置预设（#362 / W-18）。

官方 `ChromeDevTools/chrome-devtools-mcp`（Apache-2.0）的三种连接模式，
一手来源 `docs/advanced-usage.md`（main commit `5ddb0a3`）：

- **isolated**（默认）：`--isolated` 临时 profile，浏览器关闭后自动清除——
  不碰用户日常 Chrome 登录态（票面"默认独立 profile"）。
- **dedicated**：官方默认行为，`$HOME/.cache/chrome-devtools-mcp/chrome-profile`
  持久 profile（Windows：`%USERPROFILE%\\.cache\\chrome-devtools-mcp\\chrome-profile`）。
- **autoconnect**：`--autoConnect`（Chrome 144+），连已运行 Chrome；官方原话
  "The MCP server has access to all open windows for the selected profile"——
  启用前必须向用户明确展示这一点并经确认（票面铁律）。
- **browser_url**：`--browser-url=http://127.0.0.1:9222` 手动远程调试端口；
  官方安全警告："Any application on your machine can connect to this port and
  control the browser."——启用前同样需要用户确认。

所有预设都走本仓 `MCPServerConfig`（stdio transport），经既有
`build_mcp_capability` 接线 → `MCPToolAdapter` → 统一 `ToolExecutor`，
不另起路径（不变量 #7）。
"""

from __future__ import annotations

from agent_harness.mcp.config import MCPServerConfig
from agent_harness.tooling.contract import ToolPermission

#: npm 包（官方 README 指定）。
CHROME_DEVTOOLS_MCP_NPM = "chrome-devtools-mcp@latest"

#: 复用已登录 Chrome 的两种模式（启用前必须用户确认，见模块 docstring）。
LOGIN_REUSE_MODES = ("autoconnect", "browser_url")


def chrome_mcp_server_config(
    *,
    name: str = "chrome-devtools",
    mode: str = "isolated",
    browser_url: str | None = None,
) -> MCPServerConfig:
    """构造 Chrome DevTools MCP 的 server 配置。

    mode：
    - `"isolated"`（默认）：临时 profile，不碰日常登录态。
    - `"dedicated"`：官方默认持久 profile。
    - `"autoconnect"`：连已运行 Chrome（144+），需用户确认"可访问所有窗口"。
    - `"browser_url"`：手动 `--browser-url`，需 `browser_url` 参数，需用户确认。

    风险映射见 `CHROME_TOOL_PERMISSIONS`：写动作默认 DANGER（逐次审批）。
    """
    args = ["-y", CHROME_DEVTOOLS_MCP_NPM, "--no-usage-statistics"]
    if mode == "isolated":
        args.append("--isolated")
    elif mode == "dedicated":
        pass  # 官方默认行为，不加旗标
    elif mode == "autoconnect":
        args.append("--autoConnect")
    elif mode == "browser_url":
        if not browser_url:
            raise ValueError("mode='browser_url' 需要 browser_url 参数")
        args.append(f"--browser-url={browser_url}")
    else:
        raise ValueError(
            f"未知 mode: {mode!r}（可选: isolated/dedicated/autoconnect/browser_url）"
        )
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command="npx",
        args=args,
        tool_permissions=dict(CHROME_TOOL_PERMISSIONS),
    )


#: Chrome DevTools MCP 工具风险映射（官方 `docs/tool-reference.md` 分类）。
#:
#: - READ_ONLY：纯读取/观测，无副作用。
#: - DANGER：写动作/导航/对话框处理/任意 JS 执行——逐次审批（票面铁律；
#:   `handle_dialog` 点掉原生弹窗是副作用，"Chrome 原生弹窗 ≠ 本仓授权"）。
#: - 未列出的工具：adapter 默认 DANGER（最严默认，不猜）。
#:
#: 命名是 MCP 侧 bare tool 名（adapter 拼成 `mcp__{server}__{tool}`）。
CHROME_TOOL_PERMISSIONS: dict[str, ToolPermission] = {
    # —— 只读/观测 ——
    "take_snapshot": ToolPermission.READ_ONLY,
    "take_screenshot": ToolPermission.READ_ONLY,
    "list_console_messages": ToolPermission.READ_ONLY,
    "get_console_message": ToolPermission.READ_ONLY,
    "list_network_requests": ToolPermission.READ_ONLY,
    "get_network_request": ToolPermission.READ_ONLY,
    "list_pages": ToolPermission.READ_ONLY,
    "get_css_styles": ToolPermission.READ_ONLY,
    "performance_analyze_insight": ToolPermission.READ_ONLY,
    "performance_start_trace": ToolPermission.READ_ONLY,
    "performance_stop_trace": ToolPermission.READ_ONLY,
    "take_heapsnapshot": ToolPermission.READ_ONLY,
    "analyze_heapsnapshot_contexts": ToolPermission.READ_ONLY,
    "close_heapsnapshot": ToolPermission.READ_ONLY,
    "compare_heapsnapshots": ToolPermission.READ_ONLY,
    "get_heapsnapshot_class_nodes": ToolPermission.READ_ONLY,
    "get_heapsnapshot_details": ToolPermission.READ_ONLY,
    "get_heapsnapshot_dominators": ToolPermission.READ_ONLY,
    "get_heapsnapshot_duplicate_strings": ToolPermission.READ_ONLY,
    "get_heapsnapshot_edges": ToolPermission.READ_ONLY,
    "get_heapsnapshot_object_details": ToolPermission.READ_ONLY,
    "get_heapsnapshot_retainers": ToolPermission.READ_ONLY,
    "get_heapsnapshot_retaining_paths": ToolPermission.READ_ONLY,
    "get_heapsnapshot_summary": ToolPermission.READ_ONLY,
    "query_heapsnapshot_objects": ToolPermission.READ_ONLY,
    "list_extensions": ToolPermission.READ_ONLY,
    "list_3p_developer_tools": ToolPermission.READ_ONLY,
    "list_webmcp_tools": ToolPermission.READ_ONLY,
    "get_os_app_state": ToolPermission.READ_ONLY,
    "emulate": ToolPermission.READ_ONLY,
    "resize_page": ToolPermission.READ_ONLY,
    "wait_for": ToolPermission.READ_ONLY,
    # —— 写动作/副作用（逐次审批） ——
    "click": ToolPermission.DANGER,
    "click_at": ToolPermission.DANGER,
    "drag": ToolPermission.DANGER,
    "fill": ToolPermission.DANGER,
    "fill_form": ToolPermission.DANGER,
    "type_text": ToolPermission.DANGER,
    "press_key": ToolPermission.DANGER,
    "upload_file": ToolPermission.DANGER,
    "handle_dialog": ToolPermission.DANGER,
    "navigate_page": ToolPermission.DANGER,
    "new_page": ToolPermission.DANGER,
    "close_page": ToolPermission.DANGER,
    "select_page": ToolPermission.DANGER,
    "evaluate_script": ToolPermission.DANGER,  # 任意 JS 执行，最高风险
    "install_extension": ToolPermission.DANGER,
    "uninstall_extension": ToolPermission.DANGER,
    "reload_extension": ToolPermission.DANGER,
    "trigger_extension_action": ToolPermission.DANGER,
    "execute_3p_developer_tool": ToolPermission.DANGER,
    "execute_webmcp_tool": ToolPermission.DANGER,
    "install_pwa": ToolPermission.DANGER,
    "uninstall_pwa": ToolPermission.DANGER,
    "launch_pwa": ToolPermission.DANGER,
    "screencast_start": ToolPermission.DANGER,
    "screencast_stop": ToolPermission.DANGER,
}
