# #1177 方案：项目菜单「新建会话」直达入口 + 「新建任务」对话框化简

- 日期：2026-10-16
- 范围：`web/` 前端（`SessionList.tsx` / `App.tsx` / `TaskCreationDialog.tsx`）；不改后端契约、不动「创建即启动」语义。
- GitHub：[#1177](https://github.com/EricKingWhy/intelligence-agent/issues/1177)（标签 `10-15-UI优化` / `in-progress`）。
- 基线：`origin/main` = `772d05452346468b59bc46392e29a58c5e8ac48d`（PR #1259 merge）。
- 效果图：分支 `docs/ux-mockup-2026-10-15-new-task` → `docs/ux-mockups/2026-10-15-new-task-dialog/`（用户 2026-10-15 已审批；**素材不合入 main**）。

---

## 1. 前端调用链（§2 读透，file:line 为基线 `772d0545` 实测）

### 1.1 「项目内新建任务」现状链路（modal-first）

| 环节 | 位置 | 事实 |
|---|---|---|
| 菜单入口（kebab 第一项） | `web/src/components/SessionList.tsx:478-483` | `onSelect={() => setStartTaskFor(project)}`；注释标「本票的主入口，放最前」。 |
| 空项目占位入口 | `SessionList.tsx:511-519` | 「在此项目中新建任务 →」按钮，同一 `setStartTaskFor`。 |
| 对话框挂载 | `SessionList.tsx:669-677`（基线行号附近） | `<TaskCreationDialog project={startTaskFor} models sandboxBackends onOpenChange onCreateTask={onStartTask} />`。 |
| 表单组件 | `web/src/components/TaskCreationDialog.tsx` | prompt 唯一必填；完成目标可选拼进首条 prompt（`:113-136`）；三档自主度（`:29-47`）；模型（`:259-274`）；运行位置（`:276-300`）；路径警告横幅（`:186-193`）。 |
| 创建回调 | `web/src/App.tsx:835-847` `handleStartTaskInProject` | `submitTask(payload, { ownError: true, onWorktreeCreated })`；失败原因回弹窗就地显示。 |
| 提交 | `web/src/hooks/useSession.ts:1094-1217` `submitTask` | 认领新会话 → `POST /api/sessions` → 接 WS/SSE 流；`ownError:true` 时不写全局横幅、只回传原因。 |
| 请求体 | `web/src/lib/api.ts:579-609` | `START_SESSION_FIELDS` 字段白名单，「有值才带键」；`startSession` = `POST /api/sessions`（默认 `launch=true`）。 |

### 1.2 「通用新建会话」现状链路（composer-first，本票要复用的那一条）

| 环节 | 位置 | 事实 |
|---|---|---|
| 侧栏「+」按钮 | `SessionList.tsx:301-303` | `aria-label="新建会话"`，`onClick={onNew}`。 |
| `onNew` 接线 | `App.tsx:1464` `onNew={handleNew}` | — |
| `handleNew` | `App.tsx:542-556` | 清选中（`selectSession(null)`）、`focusRun()`、切回 Chat 面；**不创建会话**。 |
| 首条消息提交 | `App.tsx:780-822`（`handleSubmit` 新建分支） | `sid===null` 且 `scope==='session'` → `submitTask({ task, auto_approve:true, ...(activeProject?{cwd:activeProject.path}:{}) ... })`；首条消息提交才真正创建会话。 |
| 惰性创建（附图/登记场景） | `App.tsx:793-797` → `ensureSession()` | `POST /api/sessions?launch=false`（`api.ts:332` `createEmptySession`），只建会话、不启动 run。 |

### 1.3 #1028 预设项目语境机制（「新建会话」锚定 cwd 的关键）

- 状态：`App.tsx:189-196` — `activeProjectId`（视图意图，非会话事实，不变量 #22）；`handleSelectProject`。
- 选中项目行 = 记项目语境：`SessionList.tsx:406-419`（`onSelectProject(activeProjectId===id?null:id)`，再次点击可逆取消）。
- 清空时机：打开具体会话即离开新建语境，`App.tsx:562-563` `setActiveProjectId(null)`。
- 消费点（**本票复用的接缝**）：`App.tsx:810` 通用新建提交时 `...(activeProject ? { cwd: activeProject.path } : {})` —— 会话直接诞生在项目目录并自动入组（后端 cwd 契约，ADR-0027 D2）。
- 归属投影：`App.tsx:299-310` `ownership`（项目名来自后端 `SessionSummary.workspace`，cwd 来自 `session/started.data.cwd` 投影 `conversation.session_cwd`）；`lib/ownership.ts` 只拼文案、不做路径比较；`projection.ts:1277-1281`「只认第一条带值 started」。

**结论（不变量 #22 契合）**：「新建会话」直达 = 在**既有 composer-first 建链**上，把 `activeProject` 语境预置好即可——`cwd` 仍由后端按精确相等判归属，前端不新增第二套真相。

### 1.4 后端契约面（不得改）

- `launch=true`（默认）+ `task` 必填；`task` 与 `launch=false` 互斥 422（`src/agent_harness/web/app.py`，见调查报告 §1.1）。
- `autonomy` 三档封闭枚举：`ask`→不自动批准、`plan`→先计划、`auto`→全自动（`app.py`）。
- `on_conflict` 默认 `worktree`（前端不发键 = 默认）。
- `cwd` 与 `workspace` 互斥 422；本票两个入口只可能发 `cwd`。

---

## 2. 方案依据

> 协议 `SDD_WORKFLOW_PROTOCOL.md` §1.3：来源 ≥2 独立来源 / 机制摘要 / 契合点 / 判定 / License。
> 本领域（Web 前端「新建任务」入口交互）在 `docs/agents/reference-sources.md` 原清单**无对应条目**
> （Reuse Matrix `13_OPEN_SOURCE_REUSE_MATRIX.md` 亦无 Web 前端行），属**事后补录**——按 §3.1 规则
> 补入清单，偏差在此披露。

| 独立一手来源 | 机制与证据 | 与本仓契合及判定 |
|---|---|---|
| **Cursor** · [Quickstart](https://cursor.com/docs/get-started/quickstart)（2026-10-15 查阅） | 选目录后 `Cmd I`/`Ctrl I` 直达 Agent 面板，**直接输入第一条指令，无创建表单、无必填字段**；Plan Mode 是会话内开关（`Shift+Tab`）不是创建前置。 | 证明「项目上下文 = 背景，不是闸门」；自主度应可后置。判定：**PORT DESIGN**（新建会话直达空会话）。（依据见调查报告 §②.1） |
| **VS Code GitHub Copilot Chat** · [chat-agent-mode](https://code.visualstudio.com/docs/copilot/chat/chat-agent-mode)（页面更新 2026-10-14，2026-10-15 查阅） | 直接输入即发送，**无创建表单**；会话级控制（Target/Agent/Model/Permissions）是输入框旁控件而非前置表单；可并行多会话。 | 与「设置后置到高级折叠」同形；证明把配置收进折叠区不牺牲能力。判定：**PORT DESIGN**（对话框化简）。（报告 §②.2） |
| **Claude Code** · [interactive-mode](https://code.claude.com/docs/en/interactive-mode)（2026-10-15 查阅） | 命令行带 prompt 即启动，**无必填字段**；权限模式 `Shift+Tab` 会话内循环，**不是创建前置必选**；`/clear` 开新会话。 | 佐证「先开会话、设置随开随改」；本票自主度/模型后置与之同向。判定：**PORT DESIGN**。（报告 §②.3；另见 #367 六家核验 `~/workspace/issue-367-study/mature-products.md` §一.3） |
| **腾讯 CodeBuddy（IDE 智能体模式）** · [Create-Task](https://copilot.tencent.com/docs/ide/User-guide/Agent-Mode/Create-Task)（2026-10-15 查阅） | 「新建任务」页本质是**一页 composer**：需求描述必填、模式有默认且记忆、模型/运行方式「根据需要调整」、回车即执行。 | 与本仓字段集最接近的对照；差异只在**容器与时机**（composer vs 模态）。本票保留模态但收敛为「描述唯一主视觉 + 其余折叠」，向 composer-first 靠拢。判定：**ADAPT**。（报告 §②.4） |
| **苹果 HIG / GNOME 惯例**：省略号 `…` 表示「点击会再问一步」（弹窗/设置） | 票面点名（dispatch §2 目标 2）：`新建任务…` 的省略号承担「下一步弹设置窗」的语义。 | 纯 UI 惯例，无需代码来源；用于菜单文案。判定：**BUILD**（文案）。 |
| **仓内既有机制（本仓一手）** | ① 通用「+ 新建会话」惰性创建链路（`App.tsx:542-556`、`:780-822`）；② #1028 预设项目语境 `activeProject` → `cwd: project.path`（`App.tsx:189-196`、`:810`）；③ `TaskCreationDialog` 既有字段与提交流（`TaskCreationDialog.tsx:113-136`）。 | 「新建会话」**复用**①+②即可锚定项目 cwd，零新增后端契约、守不变量 #22；表单化简是在③上做减法。 |
| 调研报告：`~/workspace/research/ia-新建任务交互调查报告.md`（2026-10-15，只读调查，含 §1 完整调用链 + §② 四家一手核验） | 结论：本仓差异不在「有没有表单」，而在**容器与时机**（modal-first vs composer-first），且项目内空会话路径随 #367 被删。 | 直接支撑本票 §A/§B 两条改动；补齐了可查到的 file:line 与来源链接。判定：**ADAPT**。 |

**机制摘要**：主流 Agent（Cursor / Copilot Chat / Claude Code）把用户直接放进对话面——prompt 即会话首条消息，`cwd`/项目/权限是背景或输入框旁可后置控件；CodeBuddy 的「新建任务」页亦是一页 composer。本仓侧栏「+ 新建会话」早已是 composer-first（首条消息提交才创建），但**项目内**入口被 #367 选项 A 收成 modal-first，且失去了「在项目里先开空会话」的路径。

**契合点**：① 不变量 #22（Web UI 不维护第二套不可对账真相）——「新建会话」不新增客户端归属判定，`cwd` 仍走 `activeProject → payload.cwd`，归属仍由后端精确相等算；② 不变量 #11（Sandbox/Permission 是 Runtime 边界）——不动自主度/权限契约，只挪 UI 位置；③ 复用阶梯（AGENTS §9.5）——优先复用仓内惰性创建链路 + #1028 语境机制，避免重造。

**判定**：**REUSE（新建会话复用既有创建链路 + #1028 语境）+ BUILD（菜单项与表单化简的最小前端改动）**。后端契约、`launch` 语义一字不动。

**License**：无实质复制上游代码；四家均为**交互设计参考**（闭源产品，仅对齐可观察行为，不复制代码/资源）。仓内复用的均为本仓自有实现。

---

## 3. 改动范围（供实现与审查对照）

- `SessionList.tsx`：项目菜单顺序改为 `新建会话` / `新建任务…` / `重命名项目` / `删除项目…`；新增「新建会话」项，点击 = 进入项目语境 + 走通用新建（`onNew` + `onSelectProject(project.id)`）。
- `App.tsx`：`handleNew` 或新增回调——使「从项目菜单新建会话」能同时设 `activeProjectId` 并进入空白新建态（复用 #1028，不重造 cwd 传递）。
- `TaskCreationDialog.tsx`：任务描述为唯一主视觉（多行 ≥3 行）；完成目标/自主度/模型收进「高级设置」默认折叠（`<details>` 或等价 a11y 折叠）；警告横幅收成单行、动态取 `project.path`；底部按钮 `取消 / 创建并启动`。
- 测试：`TaskCreationDialog.sandboxOptions.test.tsx` 随结构化简同步更新（红→绿）；高级设置折叠为**新增用例**；`web/e2e/u-project-task.spec.ts` 与新增菜单项契约同步。
