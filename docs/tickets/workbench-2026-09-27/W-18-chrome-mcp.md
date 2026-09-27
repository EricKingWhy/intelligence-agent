# W-18 · 可选 Chrome DevTools MCP 与现有登录态审批
**目标仓库**：intelligence-agent-backend（MCP Provider / ToolAdapter）。

**类型/优先级**：P1 可选浏览器 Capability。**依赖**：W-14、W-08。**范围**：已有 `mcp/config.py`、`mcp/client.py`、`mcp/adapter.py` 的配置/权限接线，以及 UI 插件设置/证据入口；不得自己写 CDP bridge。

## 两种连接模式

默认运行官方 Chrome DevTools MCP 的独立 profile，不接触日常 Chrome 登录态。若用户要复用已登录的 Chrome，必须手动启用官方支持的连接方式、明确展示其可访问选定 profile 的**所有窗口**，用户确认后才启用。首版不承诺“只开放当前 tab”。连接失败、Chrome 未装或拒绝连接时，普通开发任务继续；浏览器验证项留“缺工具/待验证”。

## 工作指令

作为可选 MCP server 配置/启动/断开，工具发现后仍经本仓 MCPToolAdapter→ToolExecutor。对站点导航/读取与表单提交、删除、上传、发送等副作用做独立风险映射；敏感站点或写动作逐次请求用户审批并记录来源事件，不能把 Chrome 原生弹窗当成本仓授权。关闭连接后销毁相关会话 token，不写进 progress.md、Artifact 摘要或 UI 本地存储。真实浏览器操作结果按 W-08 写证据 ref/截图并链接验收项。

**验收**：专用 profile、手动连既有 Chrome、拒绝连接、未装 Chrome、断开重连、敏感表单审批、没有可用浏览器时 Task 仍运行；至少一次真实登录态页面只读验证与一次有副作用动作**审批前阻断**实测；日志/进度/事件均无 Cookie 值。确认 MCP 工具只通过单一 ToolExecutor，未出现双 retry。**不做**：自动桌面操控、自己维护 Chrome CDP、默读用户全部 tab。

**成熟参考/复用**：[Chrome DevTools MCP 官方高级用法](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md) Apache-2.0 `REUSE` 进程/协议；[Claude in Chrome 权限说明](https://support.claude.com/en/articles/12902446-claude-in-chrome-permissions-guide) `PORT DESIGN` 站点/敏感动作提示；本仓 MCPToolAdapter/ToolExecutor `REUSE`。
