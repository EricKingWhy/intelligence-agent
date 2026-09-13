# PRD — Workspace Panel（工作区面板：清单 / 详情 / 能力驱动面）

> 状态：**待批准**（用户批准后进入 implement）。
> 依据与优先级：`docs/spec/Observable_Agent_Workspace_SDD/01_PRODUCT_PRD.md`（下称 PRD）>
> `../goal/.../Intelligence_Agent_Web_UI_Design_Brief.md`（下称 Brief）> `docs/BENCHMARK_SYNTHESIS.md`。
> 产品事实记录：`web/PRODUCT.md`（本批新建，用户已确认）。
> 决策来源：2026-09-13 两轮 grill（用户答复见 §7 决策台账）。

---

## 1. Job and audience

**到达者**：有工程背景的 agent 构建者，**本机自用/小团队**。他刚跑完或正在跑一次 run，发现 agent 行为不对（跑歪、工具调用异常、改动超出预期）。

**他的任务**：在**不离开当前会话**的前提下，回答两个问题——"它到底做了什么"（事件/工具/命令输出）和"能不能看那一段真实内容"（diff、命令输出、被外置的大内容）。

**成功的样子**：能指着某一条事件或某一次工具调用说"就是这里"，并就地看到它的真实数据；不需要去翻日志文件、不需要复制命令到别处执行。

**visitor mode**：Operate（工具界面：可扫读、一致、真实、键盘可达优先）。

**产品差异点**（相邻产品抄不走的那条）：append-only typed SessionEvent 是唯一真相，**每个面板都是它的投影**；所以"看一条事件"与"看它的详情"在架构上是同一件事的两面。

---

## 2. Selected direction（选定方向）

**分工不是"把清单搬到中间"，而是按信息结构分工：**

| 信息结构 | 归属 | 理由 |
| --- | --- | --- |
| **"选中一条 → 看它的详情"**（Timeline / Changes / Artifacts） | **Inspector（右栏）** | 列表与详情同框时，选中→详情的往返成本最低；这是 Linear / Notion / VS Code 一致的模式（见 §3.2） |
| **持续流 & 宽读面**（Terminal 输出、将来的 Research/Finance 面） | **中心列（tab）** | 它们没有"选项→详情"结构，且需要横向宽度 |
| **对话** | **中心列常驻** | 主阅读面；既有代码注释已把它当默认 |

**Inspector 从"五个 tab 的侧栏"升级为"清单 + 选中项详情 + peek 升级链"**，并获得 PRD §12 要求但缺失的 PERMISSION / ARTIFACTS 段。

**`Split` / `Preview` 两个模式名删除**：Brief 要的是 tabs 不是分屏，`BENCHMARK_SYNTHESIS.md` 明确**不采纳**让步链三栏 shell（drag handle）那套。若将来需要 Codex 式"diff 贴在源码旁"，用**内联 peek**（VS Code 做法：就地展开、Esc 收起）实现，不做持久分栏。

### 2.1 目标布局

```
┌────────┬───────────────────────────────────┬──────────────────────┐
│ Rail   │ Chat │ Terminal │ ⟨能力声明的面⟩     │ 详情（可钉住/整页）    │
│ 240px  │ ───────────────────────────────── │ 320 → 可拖到 480      │
│        │ Conversation（主阅读面，常驻）       │ ┌ 清单（Timeline /   │
│        │                                   │ │  Changes / Artifacts）│
│        │                                   │ ├ 选中项详情          │
│        │  Composer                         │ └ 运行摘要（RUN/MODEL/ │
│        │                                   │   TOOLS/CONTEXT/      │
│        │                                   │   PERMISSION/TRACE）  │
└────────┴───────────────────────────────────┴──────────────────────┘
   Esc 关闭 · Space 预览（按住=临时，快按=钉住）· ↑↓ 换条目 · ⤢ 整页
```

现状几何为 `.app-regions { grid-template-columns: 240px minmax(0,1fr) 320px }`（`web/src/styles/app.css:35`），≤1200px 为 280px、窄屏 rail 56px（`:3647`、`:3654`）。本批**不改三区几何**，只让 Inspector 可拖宽。

---

## 3. Interaction and layout（交互与布局）

### 3.1 键位与触发（Q2 = 鼠标默认可用 + 键盘加速）

| 动作 | 鼠标 | 键盘 | 备注 |
| --- | --- | --- | --- |
| 在清单中移动 | 点击某条 | `↑` / `↓`（`j` / `k` 可选） | 移动时**详情实时跟随**（Linear 的做法） |
| 预览（peek） | 点击即选中即预览（现状不变） | `Space` 快按=保持打开 / 按住=松手即关 | Linear 的 peek 语义（`linear.app/docs/peek`） |
| 关闭 | 点关闭按钮 / 点面板外 | `Esc` | 关闭**不卸载**（既有冻结决策，DSH 语义） |
| 钉住（pin） | 面板头部的钉住按钮 | 同左（面板内可达） | 钉住后切换会话/选中不自动收起 |
| 整页打开（⤢） | 面板头部按钮 | 面板内按钮可达；键位待定 | 供长 trace / 大 diff 宽读；Notion 的 Full page、VS Code 的 F12 同族 |
| 跳到命令面板 | — | 既有命令面板（含 `toggle-inspector`） | 已有；本批要求它定位到**对象**（事件/工具调用/产物）时能就地预览 |

**键盘唯一 vs 鼠标可用**：不做 Linear 式"键盘唯一"——PRD 目标 6 是"键盘优先"而非"键盘专属"，且既有 e2e 与使用习惯都是点击驱动。

### 3.2 能力的可见性（Q3 = 按声明显隐；PRD §6.3 原文 "Visible when the active capability/session can produce code/file changes"）

**前端必须消费 `GET /api/capabilities`**（当前前端零消费）。契约已存在：

```json
{"capabilities":[{"id":…, "display_name":…, "version":…, "provider_name":…,
  "surfaces":{"chat":true,"timeline":true,"changes":false,"terminal":false,"artifacts":false},
  "actions":{"permissions":false,"stop":false,"retry":false,"resume":false}}]}
```

构造点 `src/agent_harness/web/app.py:902-943`；缺省语义**保守**：`chat`/`timeline` 默认 true、其余默认 false（`app.py:918-927`）。

规则：

- tab 集 = `chat`（常驻）+ 当前能力声明为 true 的 surfaces；**不硬编码 Coding-only tab**（PRD 非目标 6）。
- 声明为 false ≠ 静默消失：要么不渲染，要么渲染为**如实说明原因的禁用位**（"该能力不产出改动"），**不允许**"点了没事"的路径（沿用 #180 的诚实占位原则）。
- 拉取失败/未就绪：**降级为 PRD 缺省语义**（chat+timeline），不阻塞主流程；不得因此隐藏 Chat。

### 3.3 Inspector 的三段结构（PRD §12）

PRD §12 要求"context 可以是：当前 Run / 选中的 Timeline 事件 / 工具调用 / 产物 / 改动"，并列出 8 段。现状已有 RUN / TOOLS / CONTEXT / TRACE / MODEL（`StepDetail.tsx:270,333,364,414,438`），CHECKPOINT 为诚实占位（`:477-482`）。本批**补齐**：

- **PERMISSION**：权限档 + 待审批请求 + 裁决结果。数据齐：`tool/approval-requested`、`permission/resolved`（`session/approval.py:59,93`）、`GET /api/permission-modes`（`web/app.py:884-900`）、`POST /api/sessions/{id}/approve`（`web/app.py:1228-1274`）。
- **ARTIFACTS**：run 级"计数 / 最新一条"。现状只有 tab、没有 run 级段（`StepDetail.tsx:756`）。
- **CHECKPOINT**：保持诚实占位不动（后端无 API，PRD 原文 "No fake values"）。

### 3.4 响应式与既有约定

- `<1200px`：Inspector 先折叠（PRD §5.1），**保持挂载不卸载**（既有决策）。
- 可拖宽：320 → 480（上限受中心列最小宽度约束；拖宽状态**不持久化**——`App.tsx:12` 的不变量 #22 原文 "Panel geometry is transient — NOT persisted"）。
- 新增面板必须：可 Tab 到达、可 `Esc` 关闭、`aria-*` 如实（既有 `workspace-modes.spec.ts` 已在守 `aria-disabled`/`aria-pressed` 的诚实性）；对比度 ≥4.5、字号 ≥12px（`e2e/t-contrast.spec.ts`）。

---

## 4. States and ranges（状态与数据范围）

真实会话里能拿到的（已实测）：

- 事件流 `GET /api/sessions/{id}/events`；改动以 `tool/result.data.{before,after,truncated}` 携带（**没有独立 diff 事件**）；命令输出经 `tool/output_delta` 合帧推送。
- **终端没有 PTY**（`sandbox/local.py` 是一次性 `subprocess.Popen`）：Terminal 面**只能是命令输出聚合**，不得承诺交互式终端。
- 外置阈值 `artifact_overflow_chars` 默认 2000（`config.py:68-82`）。

**必须如实的四类状态**（PRD：No fake values）：

1. **空**：没有事件/没有改动/没有产物 → 说明"这个会话没产生过"，而不是空白或 0。
2. **不可得**：后端拿不到（如 CHECKPOINT、MinIO 下缺元数据）→ `—` / `Unavailable` / 整段省略。
3. **截断/外置**：内容被截断或被外置 → 明示"已截断，完整内容为 artifact `<id>`"，并给出**可用的**查看入口。
4. **加载/失败**：面板内的加载与错误必须就地显示（后端 detail 原文，既有约定），不得静默。

---

## 5. Scope and boundaries

**本批做**：中心列能力声明 tab（含 Terminal 面）+ Inspector 的清单/详情/peek 升级链 + PERMISSION/ARTIFACTS 段 + diff 单一渲染 + **artifact 内容读取接口**（后端，让你真的能看到产物内容）+ ArtifactsTab 可点开内容。

**本批不做（明确 anti-goals）**：

- 不做持久分栏（Split 不做成真分屏），不做 drag handle 式让步链三栏 shell。
- 不做 PTY / 交互式终端。
- 不做视图/过滤器的保存（Linear Views 那套属于多项目多团队场景，本产品单机自用不成立；除非将来确有需要）。
- 不做移动端适配（PRD §5.2 非目标）。
- 不复制 Linear / ZCode / Raycast 的品牌外观（PRD 非目标 2）——只借交互范式。
- 不改三区几何、不动 Session Rail、不改 Conversation 的聊天行为。
- 不为未实现的未来能力预造抽象（PRD 非目标 6 的另一面）。

---

## 6. Constraints and open decisions

**约束**：Core 是 Python/Async-first，但本批前端为主；后端只增一个只读路由（不改 Agent Runtime、不改 Tool 执行路径、不新增 SessionEvent）。**新增只读路由不会触发事件词汇守卫**（`tests/test_event_types_generated.py` 只在新增事件类型时失败）——本批如无新事件则无需重跑生成器。

**开放决策（implement 前必须定，或按推荐执行）**：

1. **artifact 接口的响应形态**：JSON 切片（带 `truncated`/`total_lines` 标记）还是字节流？→ 推荐 **JSON 切片**（复用 `ArtifactStore.inspect` 的 `start_line/end_line/keyword/max_lines` 能力，且前端已有 `DiffBlock`/`JsonTree` 可渲染文本），并设响应体积上限。
2. **整页打开（⤢）的键位**：→ 推荐面板头部按钮 + 命令面板项，键位留待 implement 时定（避免与浏览器快捷键冲突）。
3. **Terminal 是否进 V1 中心列**：→ 推荐进（用户实际在用；且它是最典型的"流式宽读面"）。
4. **Changes/Artifacts 在 `surfaces` 缺省 false 时的呈现**：不渲染 vs 如实禁用位 → 推荐**不渲染**，但在能力为真时出现（避免为不存在的东西留位置）。

---

## 7. 决策台账（用户 2026-09-13）

| # | 决策 | 结论 |
| --- | --- | --- |
| 1 | 面板交付物定位 | **分工方案（B）**：清单+详情归 Inspector、流式/宽读面归中心列 |
| 2 | peek 触发 | **鼠标默认可用 + 键盘加速（C）** |
| 3 | 能力声明显隐 | **做**（接上悬空的 `/api/capabilities`） |
| 4 | Inspector 缺口 | **补**（PERMISSION + ARTIFACTS 段；CHECKPOINT 保持诚实占位） |
| 5 | diff 渲染收敛 | **DiffBlock 为唯一渲染器** |
| 6 | 本批边界 | **一批做完**（peek 升级 + 能力声明 + 补段 + 收敛 + artifact 接口） |
| 7 | 旧设计可否被取代 | **可以**——"这是个 Demo，一直在开发中，什么设计好就用哪个"；因此 `Split`/`Preview` 可删 |
| 8 | 首要用户 | 自己/小团队自用（本机、无鉴权、单人） |
| 9 | 实际使用的面 | Timeline / Terminal / Overview 用；**Changes / Artifacts 实际不用** |
