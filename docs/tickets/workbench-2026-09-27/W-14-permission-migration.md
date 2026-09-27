# W-14 · Desktop/TUI/Web 统一安全默认权限

**目标仓库**：`intelligence-agent-backend` + `intelligence-agent-frontend`；先在 backend 固定请求/Policy Contract，再在 frontend 接入同一权限投影。**类型/优先级**：P0 安全迁移。**依赖**：W-11。**范围**：backend 的 `src/agent_harness/web/app.py` 请求默认值、`src/agent_harness/session/approval.py`、ToolExecutor/PermissionPolicy 装配；frontend 权限展示与兼容测试。先读 Spec 04 §8 与 `AGENTS.md` 凭证/Host 边界。

## 目标权限表

| 操作 | 缺省结果 |
| --- | --- |
| 已选工作目录内读、普通文件编辑 | ALLOW；仍受 Sandbox/path guard。 |
| 破坏性 Bash、越界写入、特权/系统配置、外部副作用 | REQUIRE_APPROVAL 或更严格 DENY；逐 Tool Call 范围，不默许整个 Session。 |
| Chrome/MCP 敏感站点、提交表单/删除/发送 | 额外明确审批；MCP 服务声称安全不能替代 ToolExecutor。 |
| 用户拒绝/审批过期/重启陈旧 | 不执行，显示原因并由事件对账。 |

## 工作指令

先测当前 Web `permission_mode="workspace-write"` 与 `auto_approve=True` 的实际优先级和老 API 客户端行为，再把“缺省”迁为安全统一策略；显式老参数要有清晰兼容/弃用语义，冲突请求 422 而非暗选宽松值。桌面/TUI/Web 使用同一服务端生效权限投影；用户改档留 SessionEvent，不靠提示词。把样例配置/文档改成与实码一致，禁止秘密值进入日志。高风险权限不因用户关窗、客户端切换或 Fork 静默扩大。

**验收**：三入口同一 Task 读/普通编辑/危险 Bash/越界路径/MCP 副作用逐项比较 Policy 决策；老请求显式/隐式组合、审批拒绝/过期、重启后权限投影、Fork 收窄均有测试。使用真实 ToolExecutor 与 LocalSandbox 至少跑一轮安全边界；不测试真正破坏性外部操作。**不做**：新第二条 Tool 执行路径、#337 陈旧审批算法。

**成熟参考/复用**：[Claude Code 权限文档](https://code.claude.com/docs/en/permissions)展示工作区内编辑与逐工具规则分层；[Chrome 权限说明](https://support.claude.com/en/articles/12902446-claude-in-chrome-permissions-guide)区分站点授权/敏感操作。策略 `PORT DESIGN`，Runtime `REUSE` 本仓 PermissionPolicy/ToolExecutor。
