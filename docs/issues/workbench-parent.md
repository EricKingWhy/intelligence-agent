# [Spec][Product] 个人可恢复、可审阅的长任务工作台（Windows Desktop + TS TUI）

## 用户与交付承诺

首版供一名 Windows 开发者完成真实项目里的长/短任务。保留通用 Python/Async Agent Core，以 Electron 承载既有 React Web，以 TypeScript TUI 提供独立终端入口；两个客户端与本机 Web 附着同一个受鉴权的 loopback Python 服务。首版的产品能力是跨窗口保住原目标/约束/未完成项，重启后对账副作用，并把测试、真实 UI 操作和 diff 与验收项绑定供用户审阅。当前仓内完整 PRD：`docs/PRD_RECOVERABLE_REVIEWABLE_AGENT_WORKBENCH.md`；施工票目录：`docs/tickets/workbench-2026-09-27/`。子 issue 均包含独立完整票面，即使文档尚未推送也可施工。

施工遵循项目的三 clone：Python Core/API 在 `intelligence-agent-backend`，React/Electron/TUI 在 `intelligence-agent-frontend`，Spec/PRD/集成证据在 `intelligence-agent`。每张子票标目标仓库；跨仓票先固定 backend API/事件 Contract，再按票面依赖更新客户端。

## 冻结的主要产品决定

- Task = Session，允许多个 Run；Fork = 独立 Session。`run/completed`、验证通过、用户接受分轴记录。“待验证/可交付/已接受”由服务端事件投影，不由客户端私存。
- 同一工作目录最多一个写入 Task；创建时占租约，第二任务持久排队或用户换目录。暂停/待审阅不释放；接受、归档或明确释放才结束。自动 worktree 属后续产品阶段。
- 桌面关窗到托盘继续；明确退出只撤销该客户端在场登记。仍有客户端托管同一 Task 时任务继续；最后一个客户端离开该 Task 后安全暂停。共享 Python 服务仅在没有连接客户端或需服务的 Task 后随最后客户端退出，不提供绕过在场检查的强制停止。意外断线给 30 秒重连宽限，期间无新模型步骤；超时暂停；重启/更新后先 Ledger reconcile，用户手动续跑。
- 用户已批准定向扩展冻结暂停契约：受产品客户端在场协议管理的 Run 使用 `run/paused(reason=client_absent)` / 显式 `resume_basis=client_return`（Spec 02 §5.2.1、03 §3.4/§5、11 §6.2，ADR-0046）；#305 既有三类暂停与旧入口 orphaned 验收不改。W-22 是 W-12 前置。
- `agent-progress/<session-id>/progress.md` 位于项目根目录，可见且可进 Git diff，但不自动 `git add`。SessionEvent 用户指令/决策是权威；文件有 seq/版本并需重读、核对、脱敏。原始大输出进入 ArtifactStore，压缩先做可回读的确定性裁剪，再做带保护事实的摘要；摘要失败有界重试，硬护栏暂停。
- Windows x64 安装包内带 Python/Web/TUI 运行依赖，首次使用不要求用户预装 Python/Node；首个个人版本可未签名，但有 SHA-256 和干净机器安装/升级/卸载验证。旧 Session/Artifact/模型配置与凭证原位复用且迁移可回退。
- 权限由现有 ToolExecutor 统一管理：已选工作目录内普通编辑可继续，危险/越界/敏感 MCP 动作逐次审批。LocalSandbox 与 DockerSandbox 显式选择，绝不静默降级。Chrome 登录态仅通过用户手动启用的官方 Chrome DevTools MCP；无插件时开发仍可进行，浏览器验收项显示缺证据。
- 发布 Gate：固定挑战场景用于复现，**真实模型**从全新样例/Session 至少两次独立完整通过；覆盖压缩接班、SQLite 提交后 ToolResult 前 kill、Ledger reconcile、真实页面验证、数据库查询、测试与 diff 审阅。失败保留，修后重新得到两次完整通过。

## 去重与前置依赖

本父 issue 不修改冻结 Spec 的 Runtime 算法。[#305](https://github.com/EricKingWhy/intelligence-agent/issues/305)/[#317](https://github.com/EricKingWhy/intelligence-agent/issues/317)/[#318](https://github.com/EricKingWhy/intelligence-agent/issues/318)/[#320](https://github.com/EricKingWhy/intelligence-agent/issues/320) 已占预算、暂停、stuck 和 alias；[#319](https://github.com/EricKingWhy/intelligence-agent/issues/319) 已占五个 Live Gate。[#337](https://github.com/EricKingWhy/intelligence-agent/issues/337) 陈旧审批、[#341](https://github.com/EricKingWhy/intelligence-agent/issues/341) relay 清理、[#342](https://github.com/EricKingWhy/intelligence-agent/issues/342) 并发 resume CAS 是客户端/恢复票的依赖，不新开同一故障修复。Memory V2 [#296](https://github.com/EricKingWhy/intelligence-agent/issues/296)/[#303](https://github.com/EricKingWhy/intelligence-agent/issues/303)/[#304](https://github.com/EricKingWhy/intelligence-agent/issues/304)/[#338](https://github.com/EricKingWhy/intelligence-agent/issues/338) 独立；迁移绝不能删 SessionEvent、Artifact、workspace 或凭证。

## Reuse First（每张子票另有准确路径）

- [Pi 独立 TUI 包](https://github.com/earendil-works/pi/tree/main/packages/tui) MIT：优先直接依赖，只写现有 Python API/SessionEvent adapter；Pi Agent Runtime 不接入。
- [DeepSeek Harness Desktop](https://github.com/deepseek-ai/deepseek-harness/tree/master/apps/desktop) MIT：小型 Electron 单实例/托盘/进程生命周期组件可在检查依赖后复用；其 Node Host/Cordis 不接管本项目 Core。
- [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) Apache-2.0：直接作为可选 MCP server；本仓 ToolExecutor 保留权限与副作用责任。
- [Anthropic 长任务实验](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)与[Codex long-horizon 实践](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex/)：借鉴进度文件、可验证清单和实际端到端验收。

## 父 issue 完成条件

子票按依赖闭合，现有相关 issue 前置状态已对账，V3.1-lite 工程门禁与 W-21 两次真实模型 Gate 通过；Windows 安装包、桌面、TUI、旧数据、安全退出/更新和证据审阅均有准确产物引用。不得以 mock 成功、一个 run 结束或 UI 绿勾代替这些证据。
