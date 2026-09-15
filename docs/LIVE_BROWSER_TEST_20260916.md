# 真机浏览器逐控件测试记录（2026-09-16）

> **实时写入**：本文件在测试过程中**边测边写**。每条结论都带可复现证据（选择器、接口回执、几何数字、截图）。
> 全部测完后按 `docs/SDD_WORKFLOW_PROTOCOL.md` 的 v2 流程批量审查并修复。

## 0. 测试环境（本记录的前提）

| 项 | 值 |
| --- | --- |
| 前端 | `http://localhost:5174`（vite dev，从 `D:\intelligence-agent\web` 起，PID 5408） |
| 后端 | `http://127.0.0.1:8000`（uvicorn `agent_harness.web.app:create_app`，PID 6200） |
| 浏览器 | **Playwright Chromium**（`@playwright/test`，项目自带联调车道依赖） |
| 仓库 | `D:\intelligence-agent`，`main` @ `71ab0a9` |
| 代理 | `web/vite.config.ts` 把 `/api` 硬编码代理到 `127.0.0.1:8000` |
| 语料 | 88 个会话，0 个归档（测试前后一致，见 §7） |
| 鉴权 | 本地信任模式（`JWT_SECRET` 未配置，后端日志已提示） |

**为什么用 Playwright 而不是内置浏览器面板**：ZCode 内置 IAB 面板的 webview guest
一次都没能稳定附着（`browser guest not attached (webview not ready)`），
唯一一次成功的页面里 `document.visibilityState === 'hidden'`、`document.hasFocus() === false`，
Playwright 风格的 actionability 等待（两次动画帧稳定）因此永不满足，点击一律超时。
改用 Chromium 后全部操作正常 —— 这也让证据可复现、可脚本化。

**为什么另起 5174**：`5173` 上有遗留 vite，`playwright.config.ts` 的
`reuseExistingServer` 会静默复用它，导致跨 clone 假红（issue #209）。

## 1. 当前能力集（决定哪些控件根本不渲染）

`GET /api/capabilities` 实际返回 **3** 条：

| capability | surfaces | actions |
| --- | --- | --- |
| `core` | chat/timeline/changes/terminal = true，**artifacts = false** | permissions/stop/resume = true，retry = false |
| `websearch` | chat/timeline | — |
| `multiagent` | chat/timeline | — |

**`memory` 缺席**：后端启动日志 `WARNING capability 'memory' 初始化失败，按 OPTIONAL_RUNTIME 降级跳过：
VectorStoreError('Memory vector store: unavailable')`（Milvus 不可用）。

⇒ 凡以 `memory` 为渲染前提的控件本环境按设计不渲染，**不是缺陷**（不变量 #21）。

## 2. 问题清单（按发现顺序，逐条实测）

| # | 严重度 | 控件 / 场景 | 预期 | 实际 | 责任方 | 证据 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | P2 | Inspector 头部运行状态徽标 | 单行徽标，与时间线头部一致 | 徽标被 flex 压缩到 48px，中文**逐字折成三行**（高 66px），挤压面板标题 | 前端 CSS | 见 §2.1 | **已修**（§2.5） |
| F2 | P2 | 亮色主题文字对比度 | ≥ WCAG AA（正文 4.5 / 大字 3.0） | **7 处不达标**（暗色 0 处） | 前端 CSS | 见 §2.2 | **已修**（§2.5） |
| F3 | P2 | 上下文容量看板（#200） | 「未采集」应如实说未采集 | 对 **16 个 run / 3865 事件**的会话显示「暂无用量数据**（会话还没有任何运行）**」——归因与事实相反 | 前端文案 + 后端聚合 | 见 §2.3 | 文案**已修**；后端聚合**未动**（§2.5 说明理由） |
| F4 | **P1** | 流式中发消息（Enter / 发送按钮） | 排队到当前会话（按钮 title 明写「Enter 排队」） | **另造一个新会话**；队列条从未渲染，无任何 queue 请求 | 前端提交分派 | 见 §2.4 | **已修**（§2.5） |
| F5 | P2 | Inspector 头部（长档位名） | 头部一行装得下：徽标单行、控制按钮在面板内可点 | 面板 **340 默认宽**下 `scrollWidth 368 > clientWidth 308`：`N runs · M 事件` 被压成 30px 宽 × **90px 高（7 行）竖条**，头部高 36 → 99，**关闭按钮被顶出面板右缘 44px（视口外，点不到）** | 前端 CSS | 见 §2.6 | **已修**（§2.5） |
| F6 | P3 | 续聊时会话已在后端删除 | 说清「会话没了」并给出下一步 | 独立复审发现：`.app-error` 显示 `续聊失败：Send failed: 404`——英文 + 裸状态码 | 前端文案 | 见 §2.7 | **已修**（§2.5） |

### 2.1 F1 — Inspector 状态徽标逐字折行（P2，前端）

`.detail-header`（宽 308px）里 `.run-badge.run-badge-completed` 的实测几何：

```
rect = [1184, 69, 48, 66]        ← 48 宽 × 66 高
css  = { whiteSpace: "normal", display: "block", width: "47.83px", padding: "3px 12px", fontSize: "12px" }
同组件在时间线头部的对照组 .tl-run-header > .run-badge = [1146, 315, 62, 27]   ← 62×27，正常
```

12px 的「已完成」+ 左右各 12px padding 至少需 ~60px；flex 行把它压到 48px，
`white-space: normal` 于是按字折行 → 三行 66px 高，并与
`panel-label`（「Run Inspector」，自身被挤成 77×38 两行）争夺水平空间。

**期望**：`.run-badge` 加 `flex: none`（或 `white-space: nowrap`），
让标题侧收缩/省略，徽标保持一行。

### 2.2 F2 — 亮色主题对比度（P2，前端，§15 双主题纪律）

自动扫描全部可见文本节点的 WCAG 对比度（有效的背景色通过向上遍历祖先求解）：

| 主题 | 不达标数 |
| --- | --- |
| dark | **0** |
| light | **7** |

亮色下最差的几处：

| 元素 | 前景 | 背景 | 对比度 | 需要 | 字号 |
| --- | --- | --- | --- | --- | --- |
| `.run-badge.run-badge-completed`（已完成） | `rgb(47,158,99)` | `rgb(241,241,244)` | **3.01** | 4.5 | 12px |
| `.run-pulse.pulse-completed`（已完成 · 11,126 tok） | `rgb(47,158,99)` | `rgb(241,241,244)` | **3.01** | 4.5 | 12px |
| `.rail-empty-action`（在此项目中新建任务 →） | `rgb(201,105,144)` | `rgb(241,241,244)` | **3.15** | 4.5 | 12px |
| `.act-name`（对话里的 `write` / `bash`） | `rgb(201,105,144)` | `rgb(251,251,252)` | **3.44** | 4.5 | 12px |

这是 §15 描述的典型失效模式：暗色下这几个 token 够亮，亮色沿用同一值时不够深。
**期望**：`--ok` / `--accent` 一类语义色在 `:root[data-theme='light']` 里覆盖为更深的值。

### 2.3 F3 — 上下文容量看板在有用量事实时谎报「没有任何运行」（P2，前端文案 + 后端聚合）

会话 `2a2d03f1-7493-4a61-b23f-3efa8bfaf6a7`：**3865 个事件、16 个 run**。

面板实际文案（点顶栏「上下文容量」）：

```
上下文容量 | 估算值 | 暂无用量数据（会话还没有任何运行）
```

后端 `GET /api/sessions/{id}/context-usage` 的实际回执：

```json
{"estimated":true,"window_tokens":200000,"used_tokens":0,
 "breakdown":{"messages":0,"system_prompt":0,"skills":0,"other":0,"tools":{"system":0,"mcp":0}},
 "cache":{"state":"not_collected","reported_calls":0,"total_calls":0,"avg_hit_rate":null},
 "state":"no_data"}
```

但同一会话的事件流里**用量事实是齐的**：

```
model/completed 条数: 16     带 usage 的条数: 16     令牌合计: 339,182
最后一条: usage = {prompt_tokens:54841, completion_tokens:6501, total_tokens:61342}
```

Inspector 的 Overview 也确实把最后一个 run 的 `tokens 61,342` 显示出来了。

⇒ 两处独立问题：
- **后端**：`/context-usage` 没有从既有的 `model/completed.usage` 聚合，恒为 `no_data`；
- **前端**：把 `no_data` 归因成「会话还没有任何运行」——这是它无从知道的原因，
  而事实恰恰相反。诚实的文案应是「后端未上报用量数据 / 未采集」。

### 2.4 F4 — 流式中发消息会新建会话而不是排队（P1，前端）

**按钮自己声明的语义**（`Composer.tsx:349,363`）：

```tsx
<button className="composer-send" onClick={() => submit('queue')} aria-label="发送"
        title={`发送（Enter 排队 · ${modKey()}+Enter 立即）`}>
```

**实测行为**：会话 `95a5e535` 流式中（`停止` 按钮可见），在输入框输入
「追加一句：只回答 ok」并按 **Enter**：

```
STOP Enter 排队请求=[]                              ← 没有任何 queue / steer / messages 请求
STOP 队列 UI={"queueEl":null,"buttons":[]}           ← queue-bar 从未渲染
```

随后重新查询后端，多出**第二个会话**，其首条用户消息就是排队那句话：

```
{"id":"95a5e535","ev":215,"first":"从 1 数到 500，每行一个数字，不要省略任何数字，不要其他文字。"}
{"id":"a686ace8","ev":6,  "first":"追加一句：只回答 ok"}      ← Enter 造出来的新会话
```

**根因**（`App.tsx:455-473`）：

```tsx
const handleSubmit = useCallback((task: string) => {
  focusRun();
  if (selectedId && !streaming) { sendMessage(selectedId, task, {...}); return; }
  void submitTask({ task, max_steps: 10, auto_approve: true, ... });   // ← 流式中落到这里
}, [...]);
```

`Composer.submit('queue')` 走的是 `onSubmit`（`onSteer` 只接管 `'steer'`），
而 `handleSubmit` 的 `!streaming` 条件把「已有会话 + 流式中」这一情形分流给了
`submitTask`——它的语义是**创建新会话**。于是：

- 与 **ADR-0030 §5.1** 直接冲突（Enter = queue）；
- `Composer` 的队列 UI（`.queue-bar` / 排队 / 引导 / 立即 / 取消）在这条路径上
  **永远渲染不出来**（前端入口不可达）；
- 用户对进行中任务的追问被拆成一个无上下文的新会话。

> 措辞校正（独立复审 P3-3）：上面说的是**前端入口**不可达，不是"后端端点成了死代码"。
> `GET /queue`、`POST /queue/flush`、`POST /queue/{id}/cancel` 是正常的 API 面，
> CLI / 其他客户端始终可调用，它们本就**不经过** `handleSubmit`；当时的实测结论是
> 「浏览器里发不出队列请求」（`queue` 请求计数 0），不是「端点在服务端不存在」。

`api.ts` 侧的通道是齐的：`sendMessage` 的 payload 支持 `mode: 'queue' | 'steer'`，
注释还明写「在途会话 → queued 入队（JSON），消息在下个 run 自然消费」。
缺的只是 `handleSubmit` 把在途情形也交给 `sendMessage`。

### 2.5 修复与回归证据

| # | 改动 | 文件 | 回归证据 |
| --- | --- | --- | --- |
| F1 | `.run-badge` 加 `flex: none; white-space: nowrap` | `web/src/styles/app.css` | 实测几何 48×66 → **62×27**（与时间线头部同值），头部高度 75 → 55 |
| F2 | 亮色块 `--accent` `#c96990 → #b2406e`、`--success` `#2f9e63 → #1f6b42`（并同步 `--accent-strong` / `--accent-soft` 保持层级与色调一致） | `web/src/index.css` | 自动对比度扫描：light **7 → 0** 处不达标；dark 仍 0；新增 `e2e/t-contrast.spec.ts` 语义色锁（双主题 20 用例全绿） |
| F3 | 空态文案 `暂无用量数据（会话还没有任何运行）` → `后端未上报用量数据`；e2e 同步加断言「不得出现『还没有任何运行』」 | `web/src/components/ContextUsagePanel.tsx`、`web/e2e/context-usage.spec.ts` | `context-usage.spec.ts` T6a/T6b/T6c/T6d 全绿；后端取数口径另立 issue **#212**（不在此猜） |
| F4 | `handleSubmit` 去掉 `!streaming` 条件（在途由后端分流：idle→launched、在途→queued） | `web/src/App.tsx` | 新增 `e2e/multiturn-queue.spec.ts` T12e；并在**真实后端**复验，见下 |
| F5 | 头部定长/弹性分工：`.detail-header .panel-label` / `.detail-run-id` 加 `min-width:0 + nowrap + ellipsis`；`.detail-profile-badge` 同上（去掉硬上限，宽面板仍显示全名）；`.detail-header-actions` 加 `flex:none`；窄面板容器查询（<360px）隐藏 `.detail-run-id` | `web/src/styles/app.css` | 实测（面板 340 / 320 / 384 / 480 四档）：`scrollWidth == clientWidth`（368>308 → **308==308**），头部高 **99 → 36**，actions 右缘收回 384 → **324 = 内容右缘**，`run-id` 不再出现 90px 竖条；新增 `e2e/y-inspector-peek.spec.ts` **AC8** 几何锁（**已做红证**：还原 CSS 后 AC8 立刻以 `368 > 309` 失败） |
| F6 | `/messages` 404 分支：`Send failed: 404` → `SESSION_GONE_ERROR_TEXT = 会话已不存在（可能已被删除），请从左侧另选一个会话` | `web/src/hooks/useSession.ts` | 新增 `e2e/continuation.spec.ts`「续聊 404」用例：断言出现「会话已不存在」且**不得出现** `Send failed` / `404` |

**F4 的真机复验**（不是 mock，打的是 127.0.0.1:8000 的真实 API）：

```
QUEUEFIX Enter 请求 = ["POST /api/sessions/68b279d8-.../messages"]   ← 打到 /messages
QUEUEFIX 会话数 89 → 89（新增 0，期望 0）                            ← 没有新建会话
QUEUEFIX 队列 UI = queue-bar :: 排队 | 追加一句：只回答 ok
          buttons = ["编辑排队消息","立即发送","取消排队消息", …]
事件流末段 = queue/consumed → text/delta → model/completed → run/completed
```

即：流式中按 Enter 现在**入队**，排队条渲染出真实条目（修复前它在浏览器里
根本渲染不出来——见上面 F4 的措辞校正），排队项由**同一会话**的下一个 run 消费。

**F3 为什么只改了文案、没动后端聚合**：`used_tokens` 的语义是**当前上下文窗口占用**，
不是「本次会话累计花掉的 token」。后者（339,182）可以从 `model/completed.usage` 求和，
但把它填进上下文容量表是**用错的量纲报对的名字**——那比"未采集"更糟。
正确的修法是后端定义清楚「窗口占用」的取数口径（例如以最后一个 run 的
`prompt_tokens` 近似），而那属于契约决策，按 `AGENTS.md` §9.1 应当先报告再动手，
不在这里替后端猜。**上一句是本次唯一一处"发现问题但故意不修"**，它是待决项而非遗留缺陷。
待决项已开单：[#212](https://github.com/EricKingWhy/intelligence-agent/issues/212)（含四个候选口径与"顺带确认 #200 三态接线是否真的生效"）。

### 2.6 F5 — Inspector 头部长档位名溢出（P2，前端，独立复审追加）

F1 修完之后，复审提出一条我**当时没测**的怀疑：把面板拖到最小、换成长档位名，
头部会不会溢出/被裁。实测（`web/e2e` 临时诊断规格 + 真实后端跑 `agent_profile=research_review`
的 run，面板 340 宽、内容区 308）：

| 头部子项 | 相对面板 x | 高 | 结论 |
| --- | --- | --- | --- |
| `.panel-label`（Run Inspector） | 16..93 | 38 | 折成两行 |
| `.run-badge`（已完成） | 93..155 | 27 | 单行（F1 修复保持） |
| `.detail-profile-badge`（research_review） | 155..281 | 27 | 126px，不可收缩 |
| `.detail-run-id`（1 runs · 3 事件） | 281..**311** | **90** | **被压成 30px 宽 × 7 行竖条** |
| `.detail-header-actions` | 311..**384** | 21 | **越出面板右缘 44px** |

根因是两类宽度需求混在一条 flex 行里、且**两类都没被正确声明**：

- 定长项（状态徽标 / 档位徽标 / 控制按钮）没有 `flex: none`，会被压缩；
- 弹性文本（标题 / `runs·事件`）没有 `min-width: 0`，既截不了自己，又去挤压定长项。
  `runs·事件` 更特殊：中文可**逐字断行**，它的 min-content 只有一个汉字宽，
  于是被压成 7 行竖条，把头部从 36px 撑到 99px，并把 `.detail-header-actions`
  顶出面板——**关闭按钮跑到视口外，物理上点不到**（Playwright 的可操作性检查会直接超时）。

所以这不是"轻微溢出"，而是**默认宽度下就能触发的控件不可达**；
默认档位名短（`main` / 旧数据「档位未知」）刚好把它掩盖住了。

### 2.7 F6 — 续聊时会话已被删除的报错文案（P3，前端，独立复审追加）

会话在别处被删（另一个标签页 / CLI / 硬删）后追问，`/messages` 返回 404，
`useSession` 落到通用分支，用户看到 `续聊失败：Send failed: 404`
——英文 + 裸状态码，既没说"会话没了"，也没说下一步做什么。
422 / 409 早已各有中文分支，404 是同类路径上的漏网。

## 3. 已实测正常（逐条勾掉，避免重复测）

| 区域 | 控件 / 场景 | 实测结论 |
| --- | --- | --- |
| 外壳 | 首屏加载 | 标题正确；会话行 **88** 条；空态「暂无对话」可见 |
| 外壳 | 能力门控 | `Split`/`Preview`/`Context Providers` 均 **0 个**（#182/#201 已删除，不报缺失） |
| 顶栏 | 切换主题 | `data-theme` dark ↔ light 实际翻转 |
| 顶栏 | Trace 密度四档 | 紧凑→`compact`、详细→`detailed`、Raw→`raw`、均衡→`balanced` |
| 顶栏 | 密度持久化 | 刷新后仍 `detailed` |
| 顶栏 | 主题持久化 | 刷新后主题不变 |
| 顶栏 | API 身份令牌设置 | 面板展开、输入框可输入、`保存` 写入 `localStorage.ahi.apiToken`、`清除` 清空 |
| 顶栏 | 记忆管理（memory 降级） | 明确显示「记忆未启用」并解释「这是配置状态而非故障…」，**无假列表**（正确降级） |
| 顶栏 | 收起 / 展开 Inspector | 收起→出现「展开 Inspector」→展开往返正常 |
| 工作区 | 三个页签 | `Chat` 会话记录 / `文件/改动` 真实 diff（左文件列表 + 右差异，`demo_hello.py +1`）/ `输出` 终端回放（`$ python demo_hello.py`、`exit 0`、stdout、不换行开关） |
| Composer | 空输入 | `发送` disabled |
| Composer | 有输入 | `发送` enabled |
| Composer | 模型选择 | 弹层打开：默认链（系统自动选）/ senseaudio 2 个模型 / qwen 2 个模型 / 管理模型入口 |
| Composer | 权限模式 | 4 项（默认/只读/工作区写入/完全访问），文案含风险说明 |
| Composer | Agent Profile | 4 项（默认/通用/编程/研究审查） |
| Composer | Reasoning Effort | 4 项（默认/轻量/标准/深度） |
| 命令面板 | Ctrl+K / Escape | 打开与关闭往返正常 |
| 命令面板 | 搜索并执行「切换主题」 | 主题实际翻转 |
| 会话轨 | 显示已归档会话 | `aria-pressed` false→true→false；归档视图正确反映条数 |
| 会话轨 | 会话行菜单 | `["上移","下移","移出项目","归档","删除会话…"]` |
| 会话轨 | 归档 → 取消归档 | 归档后默认视图 **0** 行；打开归档视图见到「已归档」徽标与「取消归档」项；取消后回默认视图 **1** 行 |
| 会话轨 | 删除确认面 | 文案完整（「这是硬删除，不可恢复…你的项目目录与其中的文件不会被删除」），**初焦 = 「取消」**，Esc 可取消 |
| 会话轨 | 硬删闭环 | 确认后行消失且后端 `include_archived=true` 也不再返回 |
| 会话轨 | 项目操作菜单 | 含「重命名项目」「删除项目」 |
| 会话轨 | 在此项目中新建任务 | 打开对话框（显示项目路径与权限档）→ `创建会话` → `POST /api/sessions?launch=false` → 回执 `{session_id, permission_mode:"workspace-write"}` → 行出现在该项目分组 |
| Inspector | 五个页签 | Overview（RUN/TOOLS/CONTEXT/MODEL/TRACE，tokens 61,342、Trace 哈希）/ Changes（「本次会话未产生文件变更。」）/ Terminal（「本次会话未执行命令。」）/ Artifacts（「本次会话未产生 Artifact。」）/ Timeline |
| Inspector | Timeline 窗口 | 「加载更早 500 条 · 显示最近 200 / 共 3865 条（前段已折叠，真相完整保留）」 |
| 真实运行 | 发送 → 流式 | 新会话建立；`停止` 按钮与 `发送` **并存**（ADR-0030 D9/D10 生效）；流式窗口实测 1s–4s |
| 真实运行 | 模型输出渲染 | 68 个 `text/delta` + `model/completed` + `run/completed`，300 字短文完整渲染进对话 |
| 真实运行 | usage 落库 | `model/completed.usage = {prompt_tokens:3237, completion_tokens:361, total_tokens:3598}` |
| 真实运行 | 续聊（二次发送） | 同一会话累积到 2 条用户消息，末事件 `run/completed` |

## 4. 本环境测不了（如实划界）

| 控件 / 面 | 缺什么条件 |
| --- | --- |
| 鉴权横幅「X 关闭提示」 | 后端 fail-open，未配 `JWT_SECRET`，`authRequired` 永不为真 |
| `恢复会话` | 语料里没有 dangling `tool_call` / 缺终态 run，入口按设计不出现 |
| 审批 `批准` / `拒绝` | 语料零 `tool/approval-requested`；需显式选权限档 + 真实写工具触发 |
| 委派行 / 子会话（Inspect/打开/复制子会话 ID） | 语料零 `agent/delegation-*` |
| `复制 inspect_artifact 引用` / `复制续读 offset` | 需要 >2000 字符被归档的 diff / 带 `continuation` 的 read 结果 |
| 模型/权限/Profile/推理的**搜索框** | 目录规模 4/3/3/3，全部 ≤5，按设计隐藏 |
| Langfuse 页面本身 | `trace_url` 存在并可打开，但无凭据无法核验落地页 |
| 队列条全部控件（立即发送/编辑/取消） | 被 F4 阻断：队列永不产生条目 |

## 5. 刷新一致性专项（用户明确的验收点）

结论：**刷新后会话内容与刷新前逐字节一致，且零点击恢复选中会话。**

| 场景 | 结果 |
| --- | --- |
| 小会话 `665573ed`（34 事件） | 一致（407 字符，`sha256[:16] = 60aa77956df70f73`） |
| 大历史 `2a2d03f1`（3865 事件） | 一致（12080 字符，`sha256[:16] = 02d5ebbb399051d5`） |
| 带 trace 的 `f522d4a9` | 一致（9426 字符） |
| 刷新后零点击 | 无需点击即停在原会话，内容已回填 |
| **真实运行产出**（新会话 + 模型 300 字回复） | 一致（463 字符前后完全相同） |
| 主题 / 密度 | 刷新后保持 |

对比方式：读取 `[role=tabpanel]` 的 `innerText`，**轮询到连续两次相同**才取值
（避开加载中/流式中的中间态），再逐字符比对。

## 6. 数据足迹（测试自身对用户数据的影响，已复原）

| 动作 | 会话 id | 结果 |
| --- | --- | --- |
| 「在此项目中新建任务」+ 创建 | `9fa4cbe4` | 经 UI 硬删，后端已确认消失 |
| 同上 | `a55dfacb` | 归档后经 UI「取消归档 → 硬删」回收 |
| 同上 | `0850c196` | 经 UI 硬删回收 |
| 同上 | `0f91be74` | 归档 → 归档视图 → 取消归档 → 硬删 全链路回收 |
| 真实运行（1+1） | `14d29f7a` | 经 UI 硬删回收 |
| 真实运行（300 字短文） | `2e61f2c6` | 经 UI 硬删回收 |
| F4 复现（数到 500 + Enter） | `95a5e535`、`a686ace8` | 经 UI 硬删回收 |

**测试结束后复核：`/api/sessions?include_archived=true` 返回 88 条、归档 0 条 —— 与测试前完全一致。**

## 7. 清单漂移（不是缺陷，避免后人重复报）

| 现象 | 事实 |
| --- | --- |
| 会话行菜单里没有「重命名」 | 会话**没有**重命名功能：`RowProps` 无 rename，后端也无对应接口；会话标题来自首条用户消息。重命名只存在于**项目**（行内编辑 + 项目菜单「重命名项目」） |
| 「新建会话」点击后没有任何网络请求、也没有新行 | 设计如此：`handleNew` 只做 `selectSession(null)` + `focusRun()`，回到「新任务」空态；会话在**发送首条消息**时创建。会真正建会话的入口是「在此项目中新建任务 →」 |
| 菜单项顺序 | 未分组会话为 `上移/下移/移出项目/归档/删除会话…`；在项目内为 `上移/下移/移出项目/…`，归档视图下变为 `取消归档` |
| 控制台 1 条 error | `Failed to load resource: 503` —— 即 `/api/memories` 在 memory 能力缺席时的正确降级，非脚本错误 |

## 8. 复现方式

审计脚本是**临时文件，已删除**（仓库里不留一次性脚本）。核心手法可直接复用：

- 启动：后端 `.venv/Scripts/python.exe -m uvicorn agent_harness.web.app:create_app --factory --host 127.0.0.1 --port 8000`；
  前端 `web/` 下 `npm run dev -- --port 5174 --strictPort`。
- 驱动：`node <脚本> <phase>`，脚本用 `@playwright/test` 的 `chromium`，
  `getByRole` 一律取自 `page.accessibility`/ARIA 快照而非猜测的选择器。
- 两条纪律来自本次踩坑：
  1. **Radix 模态打开期间，其余内容被 `aria-hidden`**，`getByRole` 会返回 0 ——
     必须用 `waitForFunction` 等模态真正退场，不能把「查不到」当成「按钮不存在」；
  2. **`innerText` 不含 placeholder**，用它探测「搜索框是否出现」必然假阴性。
