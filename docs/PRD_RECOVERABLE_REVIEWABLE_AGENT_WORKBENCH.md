# PRD：个人可恢复、可审阅的长任务工作台（Windows 首版）

**状态**：用户决策已确认；供拆票与实现。**日期**：2026-09-27。**性质**：现有通用 Agent Harness 的首款个人产品层，不替代冻结 Engineering Specification、[#305](https://github.com/EricKingWhy/intelligence-agent/issues/305) 长任务 Runtime 规格或 [V3.1-lite](SDD_WORKFLOW_PROTOCOL.md) 工程门禁。当前源码核查锚点为 detached `063487a`；开工时必须重新核对目标分支。

## 1. 问题、用户与产品承诺

首版主要服务一名 Windows 开发者：让 Agent 在真实项目里跨数小时定位复杂问题、修改、验证、审阅与中断续做。短任务也从相同入口运行，但不强制长任务仪式。用户需要在桌面和 TUI 间切换，查看确切目标、已做工作、失败尝试、证据和下一步；系统不能仅凭模型一句“完成了”宣布交付。

首版的差异化是**原始目标与约束跨窗口保全、真实副作用可对账、证据可审阅**。Electron 是既有 Web/React 的 Windows 宿主；TypeScript TUI 是同一 Python 服务的客户端。唯一的 Python/Async Core、SessionEvent、ToolExecutor、Operation Ledger、ArtifactStore、MCP Adapter 和 V3.1-lite 流程继续拥有各自既有责任。首版不做多人协作、桌面 GUI 自动操作、自动 Git worktree、自动 commit/push、Docker Engine/Desktop 管理、远程访问个人桌面服务或首字延迟优化。

### 成功判据

1. 干净 Windows x64 机器安装后，无需预装 Python/Node 即可打开桌面或独立启动 TUI；两者接同一唯一服务与现有模型配置、会话、证据。
2. 已选目录的任务能保留原目标、授权、验收项、失败结论与副作用状态；至少一次压缩和一次重启后仍可核对来源事件。
3. `run/completed`、实际验证、用户接受各有独立事实。无证据时显示“待验证”，证据足够时“可交付”；用户可带原因接受未验证结果，界面持续显示缺项。
4. 同目录最多一个写入任务；其他写入任务持久排队或使用用户指定独立目录。桌面窗口隐藏、TUI 退出、异常断线和更新安装都满足下述暂停规则。
5. 补充挑战由真实模型至少两次**完整独立**通过，包含真实 UI、真实文件与数据库检查、压缩/接班、进程 kill 与 Ledger reconcile；失败尝试全部保留。

## 2. 现有资产与明确缺口

已有：Python AgentRuntime、append-only typed SessionEvent、Run/Resume/Fork/Replay、ToolExecutor 单一路径、Operation Ledger/reconcile、Local/Docker Sandbox、ArtifactStore、ContextBuilder/Compactor、FastAPI SSE/WS、React Web、模型配置与 Windows keyring、CLI Renderer、V3.1-lite 的测试/真实入口验证/diff review/review ledger/Gate-0。详细归属见[碰撞审计](research/2026-09-27-product-scope-collision-audit.md)。

目前缺口：Web 入口仍有 `auto_approve=True` 缺省；CLI 与 Web 分别持有同一数据根 `InstanceLock`，不能同时独立运行；RunManager 零订阅者超时会写 `run/failed(reason=orphaned)`；没有产品任务交付状态、持久目录队列、桌面宿主、项目可见进度文件或 TS TUI。当前摘要失败的机械 fallback 只供当轮使用，持久化 `context/compacted.summary` 可能为空，重载时旧约束被遮蔽。现有 [#341](https://github.com/EricKingWhy/intelligence-agent/issues/341) 负责 WebSocket relay cleanup，[#342](https://github.com/EricKingWhy/intelligence-agent/issues/342) 负责并发 resume CAS，不能在本 PRD 重建相同修复。

## 3. 领域模型与状态

| 概念 | 唯一真相 | 首版规则 |
| --- | --- | --- |
| 产品任务 Task | 一个 Session 与其追加事件 | 同一 Task 可有多个 Run；Fork 在既有合法边界创建独立 Session 与独立进度文件。 |
| Runtime Run 状态 | 现有 Run/SessionEvent | `active/paused/completed/failed/interrupted/needs_reconcile` 不被产品 UI 改写；Operation Ledger 的 `NEED_RECONCILE` 是操作状态，不是 Run 状态。 |
| 验证状态 | 验收项与证据事件的服务端投影 | `未开始/进行中/通过/失败/受阻/未完成`；单项与任务总览均可解释。 |
| 接受状态 | 用户确认事件 | `未接受/已接受/带缺项接受`；接受不把失败或未验证变成“通过”。 |
| 交付展示 | 上述事实的投影 | `执行中/待验证/可交付/已接受`；不得仅以 `run/completed` 推定“可交付”。 |
| 写入租约 | 持久任务目录所有权与有序队列 | 创建有写入意图的 Task 时获得；待审批/暂停/待审阅不释放；用户接受、归档或明确释放后释放。 |
| 在场客户端 | 服务端登记的 Task 订阅/托管关系 | 桌面窗口和托盘、TUI、本机 Web 分别登记；在场按 Task 算，不按全局进程算。 |
| 进度文件 | `agent-progress/<session-id>/progress.md` | 可读、可进 Git diff、不自动 `git add`；带来源 seq/版本，是会话事实的可核对副本，不是另一事实源。 |

### 状态转移硬规则

- 创建 Task 时保存原目标、工作目录、写入意图、用户约束/授权和初始验收项。用户未提供验收项时 Agent 提出可验证清单；普通低风险工作可推进，关键目标模糊或高风险操作先确认。验收项修改记录来源与时间。
- Agent Run 结束且没有证据时，Task 展示“待验证”；所有必需验收项的真实证据与当前代码版本匹配、没有未决副作用时，展示“可交付”。用户确认后“已接受”。显式带缺项接受保留原因和未通过项。
- 任何 UI 刷新、换客户端、服务重启都由 SessionEvent/服务端投影重建；客户端缓存、进度文件和长期 Memory 均不可单独改变上述状态。
- Task 的写入租约不因 `run/completed`、客户端退出、等待用户审批或证据缺失自动释放。任务释放后队首有在场客户端才启动；否则保持“等待用户回来”，不会自行消耗模型 token。

## 4. 关键用户流程

### 4.1 创建与执行

用户选择目录、给出目标和可选验收项，界面明确显示“只读/拟写入”、当前 Sandbox、权限范围、模型与预算。只有已选择的工作目录可被普通写入。创建写入 Task 时服务端先取得持久租约；失败则选择排队或指定另一个目录，不能让模型开始后才发现冲突。创建后初始化进度文件并验证可读；写文件失败显示阻塞，不伪称初始化成功。只读/问答 Task 不占写锁；执行中升级成写入任务前必须取得租约。

默认工作目录内普通编辑可进行；危险 Bash、越界写入、敏感站点与有副作用 MCP 操作逐次走 ToolExecutor 审批，用户可拒绝并提供原因。Docker 不存在时仍可**显式**选择现有 LocalSandbox，界面展示隔离差异；Docker 模式不可用时不得偷偷切成本机。个人服务只监听 loopback，桌面、TUI、本机 Web 通过受控本机鉴权连接。

### 4.2 证据与审阅

每条验收项绑定目标文本、可执行或可观察判据、最近一次结果、来源事件 seq、Run/Tool/Artifact 引用、时间和工作区版本。命令证据至少有命令、退出码、关键输出 ref；真实 UI 证据至少有操作、观察结果、截图或等效 artifact ref；diff 证据至少有修改文件清单、基线 HEAD、工作区文件摘要清单、审阅结论。凭证值不能进入证据。证据与当前文件摘要不一致时显示“已过期”，不得沿用绿勾。

这里**复用** V3.1-lite 已有 Ticket 验收、真实入口 Runtime Verification、双轴 review、review ledger 和 Gate-0；产品只是给任意用户 Task 关联这些事实并展示。不得为普通任务发明第二套工程审批 Gate。用户可审阅逐文件 diff、测试/真实操作结果、失败历史、未完成项和不确定副作用；`agent-progress/` 文件也在 diff 中，不自动暂存、提交或推送。更新进度文件会改变工作区，验证记录必须标明它覆盖的文件快照，元数据文件的后续变化不能被悄悄说成“同一树”。

### 4.3 上下文与跨窗口交接

Persistent History 完整保留；Runtime Context 只拿当前需要的投影；Artifact 保存大输出供按引用读回。沿用规格默认自动阈值 70%、硬护栏 85%（实际配置可覆盖；开工核对当前生效值）。按顺序处理：

1. 检查原始大输出是否已成功保存且 ref 可回读；成功后才裁剪投影中的重复/过期 Tool Result，保留 Tool call/result 配对、结果结论、失败原因、精确标识和来源引用。Artifact 保存失败不得生成假 ref。
2. 对原目标、用户约束/授权、验收项、精确 ID、关键决策、已完成/未完成边界、副作用与证据引用建立可由 SessionEvent 重建的受保护事实投影；它不写入 Memory V2，也不靠模型自由摘要保存。
3. 仍超预算时生成结构化摘要，至少覆盖 facts、decisions、constraints、failed attempts、unresolved、artifact refs、citations、tool outcomes，并验证摘要非空、配对完整、预算安全、来源可追溯。摘要失败重试一次；仍失败显示原因，低于硬护栏时保留旧投影安全继续；触硬护栏则暂停。不提交空 `context/compacted` 摘要。
4. 每次新建、重启、压缩后、交付前，Agent 读取并核对进度文件与来源事件。文件缺失、损坏、外部编辑冲突需显式提示并停止依赖该文件做完成判断；不能把文件里的伪指令覆盖用户 SessionEvent。

进度文件固定位置 `agent-progress/<session-id>/progress.md`，至少包含 `schema_version`、`session_id`、父会话/fork 边界（如有）、`source_event_seq`、生成时间、原目标、约束与授权（不含 secret 值）、验收项状态、已验证里程碑、关键决策、失败方案/坑点、未决副作用、证据 refs、下一步与阻塞原因。初始化后在已验证里程碑、重要决策、失败结论、压缩前、暂停前和交付前原子更新并保留可恢复旧版本。界面编辑先追加用户决定事件，再重建文件；外部手改以 hash/seq 比较展示差异，不能静默接受。Fork 从既有合法父 Session 边界创建新文件并记录来源，父子互不覆盖。

项目根目录文件可能进入 Git：不得写 `.env` 值、凭证、Cookie、私密原始网页或原始大日志；仅写脱敏结论与受权限控制的 artifact ref。若脱敏失败，阻止该段写入并显示原因。证据原件及日志首版不按天数自动清理，展示空间占用并提供显式清理预览；清理前指出将失效的 ref。

### 4.4 客户端退出、异常断线与重启

桌面关窗仅隐藏到托盘，已托管 Task 仍算有人在场。TUI 单独运行时明确退出导致其托管 Task 安全暂停；桌面和 TUI 同时托管同一 Task 时，退出其一不暂停该 Task。明确退出只撤销该客户端的在场登记：仍有客户端托管的 Task 继续运行；某 Task 的最后一个客户端退出后停止接纳新模型步骤，把 Run 安全暂停并写进度，在途 Tool 由 Ledger 确认结清或转入对账。桌面进程只有在不再有连接客户端或需服务的 Task 时才可关闭共享 Python 服务；不提供绕过客户端在场检查的强制停止。不得把正常退出改写成 `run/failed(reason=orphaned)`。意外断线默认给 30 秒重连宽限；宽限内不发新模型步骤，超时安全暂停；重新连接不自动续跑。

Windows 重启后用户手动打开；先加载现有任务、工作区和 Ledger，按 Engineering Spec 07 §9 顺序恢复，UNKNOWN 先查询工具/外部系统实际状态，仍不确定再请求用户裁决。`#337` 陈旧审批与 `#342` 并发 resume 的已有修复结果是前置依赖。用户看见中断、已确认副作用、未知副作用及可继续条件后再手动继续。手动安装新版前列出受影响 Task；安全暂停失败则安装中止。安装后执行相同的 reconcile 路径，不以更新进程替换来假装任务已安全完成。

### 4.5 Windows/TUI/浏览器扩展

Electron 只负责单实例、窗口/托盘、受限原生目录选择、本机服务进程与更新前协调；复用当前 React 页面，Renderer 不直接取得文件系统、凭证或裸 IPC。安装包含 Python 服务、Web 静态资源、TS TUI 的运行依赖；离线干净 Windows x64 安装、启动 TUI、退出/卸载、数据备份/迁移回退必须实测。首个个人版本可未签名，但需 SHA-256 与来源记录。桌面/TUI 共用旧会话、模型配置、Windows 凭据管理器和 Artifact；CLI 必须**附着**现有服务，不能抢同一个 InstanceLock 再启动第二 Core。

TUI 首版包含 Pi 风格终端滚动会话、编辑器、工具状态卡、Task/Session 切换、审批、进度/证据入口、最小 diff 查看入口。命令与已有 CLI/多轮 REPL 对账，只补未产品化缺口，不重做已交付的 [#133](https://github.com/EricKingWhy/intelligence-agent/issues/133)。长任务暂停/恢复、Fork、压缩、Artifact inspect 等现有 Spec 11 CLI 必需面不得丢失。

浏览器只采用可选 [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp)；默认专用 profile，连接用户日常 Chrome 登录态需要用户手动启用并展示“可访问所选 profile 的所有窗口”，不是“仅当前标签页”。每次工具调用继续进入 MCPToolAdapter → ToolExecutor；敏感站点及有副作用表单额外逐次确认。无 Chrome 时开发任务能运行，但需要浏览器的验收项显示缺证据。

## 5. 用户可见的失败与不可妥协边界

| 失败/冲突 | 必须显示和执行的行为 |
| --- | --- |
| 模型摘要失败两次 | 原因和重试结果可见；不落空摘要，硬护栏前安全继续，硬护栏时暂停。 |
| Artifact 保存或读回失败 | 不生成/采用不可回读 ref；保留可安全承载的原始内容，越窗前暂停。 |
| 进度文件写入失败或外部修改 | 显示路径、来源 seq 和冲突；不把文件当真相，不宣称交付。 |
| 验证缺失/失败 | 保持“待验证”，指出缺项；用户带原因接受也保留“验证未完成”。 |
| 工作区版本变化 | 对应证据变“已过期”，指出变更文件与需重跑步骤。 |
| 同目录另一个写任务 | 后者持久排队或用户换独立目录；不靠进程内布尔锁。 |
| 最后客户端退出或超时断连 | 停止新模型请求、安全暂停；保留 Ledger 与 progress，回归时手动续跑。 |
| UPDATE/重启遇未知副作用 | 先查外部事实；仍未知时 Operation 进入 `NEED_RECONCILE`、Run 投影为 `needs_reconcile`，用户裁决前绝不盲重做。 |
| Docker 或 Chrome 缺失 | 仅相应能力缺项；不得静默改变 Sandbox 或伪造浏览器验证。 |
| 旧数据迁移失败 | 原目录与备份仍可用；不初始化空目录来冒充迁移成功。 |

## 6. 验证计划与发布闸门

每张施工票按 [V3.1-lite](SDD_WORKFLOW_PROTOCOL.md) 走 focused test、相关真实入口验证、必要独立双轴审查、冻结树全量 Gate 与 review coverage；不得将“单元测试绿”直接当用户任务成功。真实模型补充挑战在[挑战规格](research/2026-09-27-long-task-resilience-challenge.md)：CSV 批量导入请求重试造成重复写入，错误行被 UI 谎报完成，含旧数据保护、不可信仓库指令、压缩失败/重载、数据库提交后 ToolResult 前 kill、Ledger reconcile、同 request_id 幂等、真实浏览器/数据库/测试/diff 审阅。它是新**产品链路** Gate，不取代 #319 五场景或 #304 Memory Gate。

发布必须取得至少两次独立真实模型完整通过；任一次失败必须保留，并在修复后重新得到两次完整通过。证据绑定运行 ID、模型 ID（不含密钥）、准确代码/工作区快照、命令/退出码、浏览器观察、数据库查询结果、diff 审阅、恢复状态、用量和失败日志引用。需要人工裁决 UNKNOWN 时可如实记为受阻，但不能计作通过。Windows 安装后的真实桌面与 TUI 各至少走一次，服务仅本机访问、旧会话可见、双客户端在场规则、显式退出/更新安全暂停均须实测。Docker/Chrome 为可选依赖，测试报告必须区分“缺依赖/未测”和“通过”。

## 7. 拆票顺序与去重

建议 24 张原子施工票，按依赖而非日历排序；详细票面见 [ticket 包](tickets/workbench-2026-09-27/README.md)。先修可复现的 compaction 重载 Bug，再做受保护事实与进度文件；产品 Task/证据契约稳定后做目录队列与共享服务；`client_absent` 的用户批准规格扩展由 W-22 实现并作为 W-12 前置；W-23 补创建入口、W-24 补证据显式清理；桌面/TUI/Chrome 接入后做真实挑战与发布 Gate。可并行的 UI/宿主/测试仅在契约稳定后并行。

施工仓库沿用项目既有三 clone：Python Core / API / Runtime 票以 `intelligence-agent-backend` 为目标仓库，源码路径从仓库根的 `src/agent_harness/` 开始；React、Electron、TypeScript TUI 票以 `intelligence-agent-frontend` 为目标仓库，现有 React 路径从 `web/` 开始；`intelligence-agent` 集成仓库保存本 PRD、Spec、票据和跨仓发布证据。每张票都标明目标仓库；跨仓票先冻结服务端 Contract，再按票面次序改客户端。实施前检查该 clone 的实际分支与状态。

| 既有 Issue | 本 PRD 只消费什么 |
| --- | --- |
| [#305](https://github.com/EricKingWhy/intelligence-agent/issues/305)、[#317](https://github.com/EricKingWhy/intelligence-agent/issues/317)、[#318](https://github.com/EricKingWhy/intelligence-agent/issues/318)、[#320](https://github.com/EricKingWhy/intelligence-agent/issues/320) | 分层预算、stuck、共享预算、`max_steps` 迁移；不重开这些 Runtime 算法。 |
| [#319](https://github.com/EricKingWhy/intelligence-agent/issues/319) | 五个真实长任务 Live Gate；产品挑战补交接/证据链，不把五场景记作新票通过。 |
| [#337](https://github.com/EricKingWhy/intelligence-agent/issues/337)、[#341](https://github.com/EricKingWhy/intelligence-agent/issues/341)、[#342](https://github.com/EricKingWhy/intelligence-agent/issues/342) | 陈旧审批、relay cleanup、resume CAS 修复；新的客户端/重启票复用其结果并列依赖。 |
| [#296](https://github.com/EricKingWhy/intelligence-agent/issues/296)、[#303](https://github.com/EricKingWhy/intelligence-agent/issues/303)、[#304](https://github.com/EricKingWhy/intelligence-agent/issues/304)、[#338](https://github.com/EricKingWhy/intelligence-agent/issues/338) | Memory V2、清旧数据、Memory Gate 与已知间歇红；不得误删 SessionEvent/Artifact/workspace/凭证或另建 Memory 票。[#302](https://github.com/EricKingWhy/intelligence-agent/issues/302) 已关闭。 |

## 8. Reuse First：具体上游与许可

| 能力 | 可用成熟实现 | 本仓选择与不得复制的边界 |
| --- | --- | --- |
| Windows Electron 生命周期 | [DeepSeek desktop](https://github.com/deepseek-ai/deepseek-harness/tree/master/apps/desktop) MIT；其中 [`single-instance.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/src/single-instance.ts)、[`tray.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/src/tray.ts)、[`backend-controller.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/src/backend-controller.ts) 是独立度较高的候选。 | 优先 REUSE/ADAPT 小模块，保留 MIT 声明与来源版本；其 `host-process.ts` 启 Node Host，须 PORT DESIGN 为 Python 子进程/鉴权/健康检查，不拉入 DSH Desktop Host/Cordis Runtime。 |
| 桌面 Python 服务启动参考 | [OpenHands `electron/main.mjs`](https://github.com/OpenHands/OpenHands/blob/main/electron/main.mjs)、[`electron-builder.config.mjs`](https://github.com/OpenHands/OpenHands/blob/main/electron-builder.config.mjs) MIT。 | PORT DESIGN 健康检查、首启错误 UI 和安装测试。OpenHands 捆 uv/Node，但首启可能下载 Python 服务；**不能**把它当成本 PRD 离线 Python 打包已解决的证据。 |
| TypeScript TUI | [Pi 独立 `@earendil-works/pi-tui`](https://github.com/earendil-works/pi/tree/main/packages/tui) MIT；[oh-my-pi TUI](https://github.com/can1357/oh-my-pi/tree/main/packages/tui) MIT；[Cline CLI](https://github.com/cline/cline/tree/main/apps/cli) Apache-2.0。 | 首选 REUSE Pi 独立包并写本仓 API/事件 adapter；OMP 与 Cline 整体包强依赖各自 Runtime，先 PORT DESIGN 交互。不得接入 Pi/OMP/Cline Agent Loop。 |
| 压缩与输出保留 | [Pi compaction](https://pi.dev/docs/latest/compaction)、[DeepSeek Tool Result Pruner](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)、[DSH output-retention](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/util/output-retention/src/index.ts) MIT。 | PORT DESIGN 到现有 Python ContextBuilder/ArtifactStore；DSH TS `output-retention` 可直接用于 **TS TUI 终端输出**，不可拿它替换 Python 的事件/副作用链。 |
| 长任务交接 | [Anthropic harness 实验](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)、[Codex long-horizon 实践](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex)。 | PORT DESIGN 进度、可验证清单、跨窗口重读与实际 E2E；本仓 SessionEvent 保持权威。 |
| Chrome 登录态 | [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) Apache-2.0。 | REUSE 官方 MCP 进程/工具，通过现有 MCPToolAdapter 与 ToolExecutor；不重写 CDP bridge，不声称只授权一个标签页。 |

施工前必须重新检查上游当前版本、许可证、依赖和 Windows 行为；实质复制时保留 license/NOTICE、源码 URL 与 commit，记录改动。不因“能复制”引入第二套 Agent Runtime。

## 9. 未纳入首版但已选的后续设计

自动 Git worktree 仅在产品首版之后另列 Phase，并对冻结 Spec 05 §7/路线图作**有范围的修订**。默认只给 Git 开发任务创建，从用户选定 checkout 的 HEAD 精确 SHA 起步；脏改动不自动复制；归档前保存可恢复快照。首版只实现单目录写入租约与队列。多人/小团队协作、桌面其他软件控制、Host Docker 管理、自动签名发布、远程桌面服务和 TTFT 优化均不进入本批票。
