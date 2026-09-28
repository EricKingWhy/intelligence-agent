# 个人长任务工作台：施工票索引（2026-09-27）

**权威需求**：`docs/PRD_RECOVERABLE_REVIEWABLE_AGENT_WORKBENCH.md`。本目录是给施工 Agent 的窄票；每票须独立读票面、对应 Engineering Spec、Reuse Matrix 和 `docs/SDD_WORKFLOW_PROTOCOL.md`。本批最终审阅基线为 `origin/main` commit `1774f4fb`（含 #342 合入修复）；实施时仍须核对目标 clone 的 HEAD 和工作区。票面若与冻结 Spec、既有 Issue 验收发生实质冲突，按 AGENTS.md §9.1.1 报告，不自行改旧票。

**共同施工规则**：Python Core 不换 TS Runtime；SessionEvent append-only，产品状态由服务端重建；MCP/Chrome 只走 MCPToolAdapter→ToolExecutor；高风险 UNKNOWN 必须 reconcile；证据不打印密钥值；进度文件不自动 `git add`；不自动 commit/push；V3.1-lite 已有工程 Gate 不复制。每票的失败路径必须实际测，不能只测成功。

**仓库路径约定**：`intelligence-agent-backend` 是 Python Core/API 仓库，`src/agent_harness/...` 为源码根；票面省略该前缀的 `session/...`、`context/...`、`sandbox/...` 等模块路径均相对 `src/agent_harness/`。`intelligence-agent-frontend` 是前端仓库，现有 React 文件以 `web/...` 为根；新增 Electron/TUI 包仅按对应票建目录。`intelligence-agent` 是 Spec/文档/集成证据仓库。跨仓票依票面注明的顺序分别提交与验证，不在未检查状态时改另一 clone。

| ID | 目标仓库 | 可独立验收的交付 | 依赖 | 票面 |
| --- | --- | --- | --- | --- |
| W-01 | Backend | 摘要失败后重载不丢旧约束 | 现有 Phase 5 | [W-01](W-01-compaction-reload.md) |
| W-02 | Backend | 事件来源可追的任务保护事实 | W-01 | [W-02](W-02-protected-task-facts.md) |
| W-03 | Backend | 可回读 Artifact 前提下的确定性旧输出裁剪 | W-01 | [W-03](W-03-deterministic-tool-pruning.md) |
| W-04 | Backend | 摘要重试、存储失败与硬护栏暂停 | W-01、W-03 | [W-04](W-04-compaction-failure-guard.md) |
| W-05 | Backend | 项目可见进度文件的原子生成 | W-02 | [W-05](W-05-progress-file-writer.md) |
| W-06 | Backend | 进度文件冲突核对、重读与 Fork 分家 | W-05 | [W-06](W-06-progress-reconcile-fork.md) |
| W-07 | Backend | Task/Run/验证/接受分轴状态契约 | W-02、#342 | [W-07](W-07-task-delivery-state.md) |
| W-08 | Backend | 验收项与真实证据的服务端投影 | W-07、W-06 | [W-08](W-08-evidence-projection.md) |
| W-09 | Frontend | 任务审阅界面与 diff/证据缺项展示 | W-08 | [W-09](W-09-review-ui.md) |
| W-10 | Backend | 同目录写入租约及持久排队 | W-07 | [W-10](W-10-workspace-lease-queue.md) |
| W-11 | Backend | 唯一本机服务、鉴权、旧数据复用 | #342 | [W-11](W-11-local-host-service.md) |
| W-12 | Backend + Frontend | 按 Task 客户端在场与安全暂停 | W-11、W-22、#341、#342 | [W-12](W-12-client-presence-pause.md) |
| W-13 | Backend + Frontend | 重启后 reconcile 与手动续跑体验 | W-06、W-12、#337 | [W-13](W-13-restart-reconcile-ui.md) |
| W-14 | Backend + Frontend | 桌面/TUI/Web 统一权限默认值迁移 | W-11 | [W-14](W-14-permission-migration.md) |
| W-15 | Frontend | Electron 薄宿主、托盘与受限桥 | W-11、W-12、W-14 | [W-15](W-15-electron-shell.md) |
| W-16 | Frontend + Integration | Windows 安装/更新/卸载与数据回退 | W-15、W-17、W-13 | [W-16](W-16-windows-release.md) |
| W-17 | Frontend | Pi 组件驱动的 TS TUI 客户端 | W-11、W-07、W-12、W-14 | [W-17](W-17-typescript-tui.md) |
| W-18 | Backend | 官方 Chrome DevTools MCP 可选接入 | W-14、W-08 | [W-18](W-18-chrome-mcp.md) |
| W-19 | Backend + Frontend | Windows Sandbox 显式选择与缺依赖提示 | W-14 | [W-19](W-19-sandbox-choice.md) |
| W-20 | Backend | 跨窗口导入故障挑战夹具与判定器 | W-01–W-10 | [W-20](W-20-long-task-challenge.md) |
| W-21 | Integration | 两次独立真实模型/桌面/TUI 发布 Gate | W-09–W-20、W-22–W-24、#319、#337、#341、#342 | [W-21](W-21-real-model-release-gate.md) |
| W-22 | Backend | `client_absent` 持久暂停/恢复契约 | #305 既有 pause 基础、#342；W-12 前置 | [W-22](W-22-client-absence-pause-contract.md) |
| W-23 | Backend + Frontend | Task 创建、验收项和排队操作入口 | W-07、W-10、W-14、W-19 | [W-23](W-23-task-creation-ui.md) |
| W-24 | Backend + Frontend | 证据占用空间、保留及显式清理预览 | W-08、W-09 | [W-24](W-24-evidence-retention-cleanup.md) |
| W-25 | Backend | 压缩阈值对齐规格（0.80/0.90 → 0.70/0.85） | 无 | [W-25](W-25-compaction-threshold-align.md) |
| W-26 | Backend | 进度清单服务端契约（schema·整表覆盖·硬校验） | W-02 | [W-26](W-26-plan-list-server-contract.md) |
| W-27 | Frontend | 清单 Web+桌面渲染（四件套，同一 React 组件） | W-26 | [W-27](W-27-plan-list-web-render.md) |
| W-28 | Frontend | 清单 TUI 渲染（Pi 独立 TUI 包复用） | W-26、W-17 | [W-28](W-28-plan-list-tui-render.md) |
| W-29 | Backend | 清单↔压缩锚点集成（重注入落地） | W-26、W-04 增量 | [W-29](W-29-plan-compaction-anchor.md) |
| W-30 | Spec + Backend | W-21 真实模型 Gate 增补判据（压缩接班·清单·失败方案） | W-29、W-21 | [W-30](W-30-gate-plan-compaction-evidence.md) |

> **2026-09-27 修订批**（来源：`docs/PRD_LONG_TASK_CONTEXT_MANAGEMENT.md`，三轮 grill-me 确认）：W-25~W-30 新票 6 张（issue #379~#384，已挂 #344 子票）；W-01/W-02/W-03/W-04 票面增量（摘要 8 节契约与校验闸门、失败方案 fact、切点规则、混合式摘要、接近护栏 warning、用户消息逐字），各票正文有 ⚠️ 修订标记与 [增量] 标注，#345~#348 body 已同步至修订后票面。

**并行边界**：W-03 与 W-05 可在 W-02 契约定稿后分线；W-09 与 W-10 仅共享 W-07 API 契约；W-15 与 W-17 共享 W-11 连接协议，不各自启动 Python Core。W-16 不能在 W-17 未可独立运行时宣称安装包完成。W-21 失败时保留失败事实，修复后重新取得两次完整通过。

**GitHub 追踪**：[父 issue #344](https://github.com/EricKingWhy/intelligence-agent/issues/344)；子票已按一票一 issue 建立原生父子关系：

| 本地 Ticket | GitHub Issue | 本地 Ticket | GitHub Issue | 本地 Ticket | GitHub Issue |
| --- | --- | --- | --- | --- | --- |
| W-01 | [#345](https://github.com/EricKingWhy/intelligence-agent/issues/345) | W-02 | [#346](https://github.com/EricKingWhy/intelligence-agent/issues/346) | W-03 | [#347](https://github.com/EricKingWhy/intelligence-agent/issues/347) |
| W-04 | [#348](https://github.com/EricKingWhy/intelligence-agent/issues/348) | W-05 | [#349](https://github.com/EricKingWhy/intelligence-agent/issues/349) | W-06 | [#350](https://github.com/EricKingWhy/intelligence-agent/issues/350) |
| W-07 | [#351](https://github.com/EricKingWhy/intelligence-agent/issues/351) | W-08 | [#352](https://github.com/EricKingWhy/intelligence-agent/issues/352) | W-09 | [#353](https://github.com/EricKingWhy/intelligence-agent/issues/353) |
| W-10 | [#354](https://github.com/EricKingWhy/intelligence-agent/issues/354) | W-11 | [#355](https://github.com/EricKingWhy/intelligence-agent/issues/355) | W-12 | [#356](https://github.com/EricKingWhy/intelligence-agent/issues/356) |
| W-13 | [#357](https://github.com/EricKingWhy/intelligence-agent/issues/357) | W-14 | [#358](https://github.com/EricKingWhy/intelligence-agent/issues/358) | W-15 | [#359](https://github.com/EricKingWhy/intelligence-agent/issues/359) |
| W-16 | [#361](https://github.com/EricKingWhy/intelligence-agent/issues/361) | W-17 | [#360](https://github.com/EricKingWhy/intelligence-agent/issues/360) | W-18 | [#362](https://github.com/EricKingWhy/intelligence-agent/issues/362) |
| W-19 | [#363](https://github.com/EricKingWhy/intelligence-agent/issues/363) | W-20 | [#364](https://github.com/EricKingWhy/intelligence-agent/issues/364) | W-21 | [#365](https://github.com/EricKingWhy/intelligence-agent/issues/365) |
| W-22 | [#366](https://github.com/EricKingWhy/intelligence-agent/issues/366) | W-23 | [#367](https://github.com/EricKingWhy/intelligence-agent/issues/367) | W-24 | [#368](https://github.com/EricKingWhy/intelligence-agent/issues/368) |
| W-25 | [#379](https://github.com/EricKingWhy/intelligence-agent/issues/379) | W-26 | [#380](https://github.com/EricKingWhy/intelligence-agent/issues/380) | W-27 | [#381](https://github.com/EricKingWhy/intelligence-agent/issues/381) |
| W-28 | [#382](https://github.com/EricKingWhy/intelligence-agent/issues/382) | W-29 | [#383](https://github.com/EricKingWhy/intelligence-agent/issues/383) | W-30 | [#384](https://github.com/EricKingWhy/intelligence-agent/issues/384) |

**已占范围**：[当前去重审计](../../research/2026-09-27-product-scope-collision-audit.md)。#305/#317/#318/#320 负责 Runtime 预算、stuck、alias；#319 负责五个原 Live Gate；#337 负责重启陈旧审批；#341 负责 relay cleanup；#342 负责并发 resume CAS；#296/#303/#304/#338 属 Memory V2。W-20/W-21 只补本产品链路，不能关闭或替代它们。
