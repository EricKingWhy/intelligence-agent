# W-23 · Task 创建入口、验收项和持久队列操作界面
**目标仓库**：intelligence-agent-backend（Task 创建 API）+ intelligence-agent-frontend（创建表单与排队状态）；先固定服务端 Contract。

**类型/优先级**：P1 产品入口。**依赖**：W-07、W-10、W-14、W-19。**范围**：现有 React/Web 创建会话界面、`web/src/lib/api.ts`、TUI 同一 API 投影；不重新实现 Session 创建、目录选择或模型目录。

## 用户操作顺序

选已有项目/目录 → 输入原目标 → 选择“只读/拟写入” → 编辑用户验收项（可空；Agent 提议时显示其可验证判据）→ 选择 Local/Docker Sandbox、现有模型与权限范围 → 展示当前同目录写者和排队位置 → 确认创建。关键目标模糊/高风险先确认；普通低风险可创建并在任务页标明“验收项待确认”。若同目录写锁被占，用户可选“加入队列”或“选另一个目录”，没有自动 worktree 选项。只读任务请求编辑时回到授权/租约流程。

## 工作指令

只调用 W-07/W-10/W-14/W-19 的服务端契约。表单显示实际生效模型、权限/Sandbox 状态和 Docker/Chrome 可用性，不能从客户端本地布尔值猜运行资格。409/CAS 冲突刷新当前写者/队列并保留用户草稿。创建成功后跳到 W-09 审阅页，显式展示 `agent-progress/<session-id>/progress.md` 路径及 Task ID；文件创建失败不得显示为已准备就绪。TUI 需同一创建字段与错误码，复杂可先用逐步提示，不复制校验算法。

**验收**：无验收项、关键模糊任务、高风险操作、Docker 不可用、同目录占锁/排队/换目录、409 后草稿保留、只读升级写入、旧会话导入后创建新 Task 各有 Playwright/PTY 路径；真实 Windows 目录含中文/空格/junction 时服务器最终目录与 UI 一致。前端 typecheck/vitest/Playwright + TUI 交互探针。**不做**：重造 API、自动 worktree、多用户共享列表。

**成熟参考/复用**：[GitHub Copilot app 创建 Session](https://docs.github.com/en/copilot/how-tos/github-copilot-app/agent-sessions)把项目、运行位置、模式、模型与 prompt 放在同一入口；本仓复用现有目录选择/模型配置组件，交互 `PORT DESIGN`，目录队列是本产品决定而非 Copilot 原功能。
