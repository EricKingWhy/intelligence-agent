# Phase 8 手动验收清单（真实大厂 server 连通性，不进 CI）

> CI 里的 Gate 测试用 in-process fake server（`tests/mcp_client/fake_server.py`）。
> 本清单验证预设四 server 的**真实连通性**——需要凭证/网络，人工逐项执行。
> 配置模板：`docs/adr/0012a-phase8-preset-configs.md`（npx 包名/远程端点在执行时
> 以各官方 README 为准复核）。

## 前置

- [ ] `uv sync`（mcp SDK 2.1.1 已入依赖）
- [ ] 启动后端：`uv run uvicorn agent_harness.web.app:app`（或 CLI）
- [ ] CAPABILITIES env JSON 按模板填入要验收的 server

## 逐 server 验收

### GitHub MCP（远程 + PAT）

- [ ] env：`GITHUB_MCP_TOKEN=<PAT>`（需要 repo scope）
- [ ] 连接成功（启动日志无 mcp 降级告警）
- [ ] `mcp__github__*` 工具出现在模型工具目录（SessionEvent 里模型可见）
- [ ] 一次只读调用成功（如列出 repo issue）
- [ ] DANGER 工具在非 full-access policy 下被审批关卡拦截

### Google chrome-devtools-mcp（stdio + npx）

- [ ] 本机有 Chrome + Node/npx
- [ ] 连接成功（npx 首次拉包可能较慢——timeout_seconds 调大或预热 npx cache）
- [ ] `mcp__chrome-devtools__*` 工具可见
- [ ] 一次调用成功（如 navigate + snapshot）

### Sentry MCP（stdio + token）

- [ ] env：`SENTRY_TOKEN=<auth token>`
- [ ] 连接成功、`mcp__sentry__*` 工具可见
- [ ] 一次只读调用成功（如列出 issue）

### Context7（stdio）

- [ ] 连接成功（无凭证）
- [ ] `mcp__context7__*` 工具可见
- [ ] 一次 resolve-library-id 调用成功

## 通用 Gate 抽查（真实链路）

- [ ] MCP 工具调用出现在 SessionEvent（tool/call + tool/result 成对）
- [ ] `harness.db` operations 表有对应记录（state=SUCCEEDED）
- [ ] 大输出（>50KB）被截断且附标记
- [ ] 拔掉一个 server（如停掉进程）重启后端：该 server 工具缺席、其余 server 与核心正常（失败隔离）
- [ ] 断网重连场景：server 恢复后下一次模型调用走新连接
