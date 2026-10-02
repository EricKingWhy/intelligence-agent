# #435 设计票 · 会话预算 UI 交互设计稿（max_total_tokens / deadline_at 维度与用量呈现）

- 设计票交付（issue #435，2026-10-02）。状态：**待用户确认**；确认后按 §6 拆实现票。
- 依据：票面（#435）三组设计问题 + `docs/PRD_LONG_TASK_CONTEXT_MANAGEMENT.md` 无关（本票是预算域）；契约权威 = `web/src/lib/amend.ts::toCreateBudget`（#422 wire 契约）、`Composer.tsx:530-578`（现状）、`PausedPanel.tsx`（#312/#313/#314 恢复链）、`lib/runBudget.ts`（取值口径）。
- 方案依据（AGENTS §6.1，2 独立来源）：Claude Code 用量语言（"(13s · 28 tokens)"，本仓 TopBar.tsx:132 已同构）+ zcode 预算/恢复多端实测（PRD 长任务调研 P）；档位/预设形态参考 Claude Code `/model` 档位选择器与 Gemini CLI settings 档位（settings.json 枚举而非自由输入）。

## 1. 现状盘点（设计输入）

- **入口**：Composer 控制行在**新建会话**（`!permissionInSession`）时平铺三个输入——turns（number）、tokens（number）、deadline（datetime-local）；`budget.run.*` 只随 `launch=true` 提交（#422），会话内（续聊）不显示。
- **映射**：`toCreateBudget`（amend.ts:96）——空/非法 = 不发键 = 后端默认；键按固定顺序构造（wire 字节可复现）。
- **用量**：TopBar 只显示 run 累计 tokens（`(13s · 28 tokens)` 语言）；**没有"已用/剩余"与 ceiling 的对照**——预算设了但用户看不到还剩多少。
- **恢复**：PausedPanel 四维清单 + 唯一恢复动作（抬高卡住维的绝对 ceiling，同 run_id 续跑）；被拒不动输入。
- **缺口（票面三组问题的落点）**：①tokens 大数量级无档位；②deadline 是裸 datetime-local，无"时长→绝对截止"映射、无过期/跨午夜显示；③无用量对照与预警；④"默认值即将生效"无表达。

## 2. 设计决策（逐题给取舍）

### 2.1 tokens 大数量级 → 档位选择器 + 自由输入双形态（推荐：档位为主）

- **形态**：number 输入换 `OptionPicker` 档位（100k / 250k / 500k / 1M / 自定义）。选"自定义"展开既有 number 输入（千分位由 `inputMode="numeric"` + 提交前 `toLocaleString` 只影响显示，wire 值不变）。
- **取舍**：纯自由输入保灵活性但用户对 token 无感（票面原话）；纯档位丢长尾。双形态里档位是**默认路径**、自定义是**逃生门**，与既有 OptionPicker 组件（reasoning effort 同款）复用，不加新交互范式。
- **单位换算（"约多少次对话"）**：**不做**。依据：次数换算的基准（每次对话多少 token）随任务形态波动两个数量级，换算出来的数字是伪精度；档位本身已经给出量级锚点。票面是"要不要给"——答：不给，避免伪造度量（zero-fake-metrics 同源纪律）。

### 2.2 deadline → 时长选择器 + 绝对时刻双形态，跨午夜/过期按绝对时刻显示

- **形态**：datetime-local 保留（高级路径），前置**时长档**（30m / 1h / 2h / 4h / 自定义时长）。选时长 ⇒ 以提交时刻换算绝对 `deadline_at`（`Date` 语义，#476 已裁决：ECMAScript `Date` 对歧义本地时刻给出唯一确定瞬时，可复现——映射在提交侧 `toCreateBudget` 一处做，不在 UI 侧新造第二份换算）。
- **显示**：输入框下方一行**预览**「≈ 10-02 15:30 截止（2 小时后）」——用户心智是"最多跑 2 小时"，预览把绝对契约翻译回心智；跨午夜显示完整日期（本地时区），预览随草稿实时更新、不提交不落库。
- **过期**：预览行在所选时刻早于当前时刻时变 danger 色（`--danger`）+「已过期」——提交侧 `parseDeadlineDraft` 已拒绝过去时刻（amend.ts），UI 预警只是前置提示，不做第二份校验。
- **取舍**：时长是主路径（心智直白），绝对时刻保留（恢复面板/CLI 显示的是绝对值，两处语言要能对上）；不引入日历选择器（datetime-local 原生控件够用，不为单维引入新依赖）。

### 2.3 三维并存版式 → 收进 popover（"预算"单入口），不平铺

- **形态**：三个输入收进一个 `预算` OptionPicker 式按钮（显示当前设置摘要：「turns 默认 · 500k · 2h」或「默认」），点击展开 popover 承载三维输入（既有 `OptionPicker` 的展开交互复用）。控制行常驻**一个**入口。
- **取舍**：平铺三个输入挤占主输入行（票面原话的担心成立——现状已占三格）；收进 popover 后控制行只多一格，三维互相关（都是"本次 run 的边界"）语义也更聚合。会话内该入口隐藏（#422：续聊不带 budget，现状已如此）。
- **iOS 反例排除**：不做抽屉（drawer）——抽屉是导航范式，这里是表单，popover 够了。

### 2.4 用量呈现 → TopBar 徽标扩为「已用/上限」+ 80% 预警（唯一改动面）

- **形态**：TopBar 既有 tokens 计数（TopBar.tsx:135）扩为「28k / 500k tok」（设了 `max_total_tokens` 时）；数据源 = `run/started.data.budget` 回显（`conversation.run_paused` 同一投影族，**不新造第二套对账**——不变量 #22；`run_limits` 已在 `useSession` 1475 行附近的恢复链消费，预算回显走同一事件）。turns/requests/cost 维同理各出「已用/上限」，只在**设了上限**的维显示（没设不显示"无上限"——陈述没发生过的事）。
- **预警**：任一维 ≥80% 时徽标转 `--warning` 色（无动画，静止态）；到顶 → PausedPanel 衔接（已有，不动）。80% 是常数不是配置（YAGNI：一个阈值，W-30 真实模型 Gate 后再议）。
- **取数口径**：与 `lib/runBudget.ts::pauseFacts` 同一取值函数族（复用 `DimensionFact.consumedText/ceilingText`），徽标和暂停面板显示**同一份真值**——两处数字不一致 = 第二套真相，禁止。

### 2.5 编辑与默认状态机

- run 启动后预算锁定：composer 预算入口在会话内**不渲染**（现状已如此，#422 契约）；PausedPanel 抬高是唯一修改通道（现状已如此）。
- "默认值即将生效"：**在 popover 内每个输入的 placeholder 表达**（现状已是「默认」placeholder），不加常驻文案——留空即默认的语义已经由 placeholder + title 承载，再加大字说明是重复。

## 3. 不变量与纪律对照

- #22：用量一律消费事件流投影（run/started 回显 + 既有 run_limits），不 fetch 第二份。
- #422 契约：budget 只随 launch 提交；六维全参数化测试已锁，实现票不动 API。
- AGENTS §15 + web/PRODUCT.md：新增 CSS 全部复用既有语义 token（`--warning`/`--danger`/`--text-secondary`/`--border-subtle`），不新增 :root 变量（PlanList 同款纪律）。
- Scope Lock：本设计不触碰 PausedPanel 恢复链、不动 toCreateBudget 键序。

## 4. 验收对照（票面三条）

- [x] 交互稿落档（本文件，覆盖三组问题 + 取舍理由）；
- [ ] 用户确认设计 → 拆实现票（§6）；
- [ ] #426「常用三项」验收在全维度闭环（实现票交付时核对，#426 关票条件不变）。

## 5. 明确不做（非目标）

- 不做 token→次数换算（§2.1）；不做日历选择器（§2.2）；不做预算编辑 API/会话内改预算（#422 契约外）；不做 per-tool 配额的 composer 入口（#314 的配额是恢复面板域）；不做 80% 阈值配置化（YAGNI）。

## 6. 拆票建议（用户确认后提交）

1. **实现票 A（输入形态）**：composer 预算入口收 popover + tokens 档位 + deadline 时长/预览（§2.1-2.3，5.1）；验收 = Composer.test 扩展 + 三维提交 wire 契约不变（toCreateBudget 既有测试全绿）。
2. **实现票 B（用量呈现）**：TopBar「已用/上限」徽标 + 80% 预警（§2.4，5.1）；验收 = TopBar.test 扩展 + 与 pauseFacts 同源取数断言。

两票独立可交付，A 先 B 后（B 依赖 A 的「设了上限才显示」语义）。
