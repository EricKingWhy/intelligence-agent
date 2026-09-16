# 设计稿 — Web UI 批次重设计（#194 / #197 / #199 / #201 / #204）

- **Status**: 设计冻结，待实现
- **Date**: 2026-09-13
- **读者**：接下来实现这批的前端 agent（**很可能不是写这份文档的人**）
- **Related**：Issue #194 / #197 / #199 / #201 / #204；`docs/FRONTEND_ISSUES_LOG.md`（前端 worktree 的四轮裁决记录）；AGENTS §15（主题变量双份）、§16.6（前端门禁）

---

## 0 硬性要求（先读这一节）

1. **UI 必须用 `impeccable` skill 做**（用户明确要求："UI 设计要是用 impeccable 这个 skills 来设计，品味要高"）。
   实现时在前端 worktree（`D:\intelligence-agent-frontend`）里按其 Setup 步骤执行：运行 `<skill-base-dir>/scripts/impeccable context --target <改动文件/路由>` → 加载 `reference/operate.md`（本批全部是 **Operate** 模式：用户是来完成任务的，不是来被说服的）→ **动手改任何 UI 之前**读 `reference/craft-floor.md`。
   若 launcher 不可用：先告知用户"context 未加载"，再按 SKILL.md 的降级路径推进，并在 PR 里说明。
2. **本批的模式 = Operate**：可扫描性、一致性、原生预期、真实使用场景优先于表达欲；品牌感落在精确的细节里，不靠大动效。
3. **不引入新依赖**：不装 UI 库、不装图标库（沿用现有内联 SVG 与现有 CSS 类）；分段条/菜单全部用手写 flex + 现有 token。
4. **主题纪律（AGENTS §15）**：任何新增颜色/尺寸 token 必须在 `:root` **与** `:root[data-theme='light']` 双份定义。
5. **e2e 定位器优先保持**：优先保持 DOM 契约（`role` / `aria-label` / 既有类名），把 e2e 改动量压到最小；确需改类名时，同一 PR 内同步更新对应 spec（§6 给了清单）。
6. **只动本票范围内的东西**：不顺手重构、不改无关样式（AGENTS §8/§9.3）。

---

## 1 #194 — 流式期间「Esc 停止」提示压在左下角档位控件上

### 现状
`.composer-esc-hint`（`Composer.tsx:173-175`）用绝对定位贴在输入区左下角，与档位控件（模型/权限/档位/深度）在同一区域重叠。

### 设计（从简，不突兀）
- **去掉绝对定位**，把提示放进 Composer 的 **footer 行内**：与发送/停止按钮同一行、靠右对齐（`Esc 停止`），仅在 `streaming` 为真时渲染。
- 视觉：`12px` 次级文字色 + 现有 mono 字重；`Esc` 用一个 `<kbd>` 样式的小胶囊（复用现有 token，不新造颜色）。
- 与 #195/#196 的队列条位置规则：**队列条在输入框上方**、Esc 提示在按钮行 → 两者不再有可能重叠。

### 验收
| 检查 | 方式 |
| --- | --- |
| 与档位控件、模型选择器均不重叠（≥1200px 与 ≤1200px 折叠两种布局） | e2e：取 `.composer-esc-hint` 与各控件 `boundingBox()` 断言矩形不相交 |
| 只在 streaming 时出现 | e2e：非流式下 `toHaveCount(0)` |
| 键盘路径不变（Esc 仍能停止） | 既有停止用例保持绿 |

---

## 2 #197 — Inspector 水平拖拽方向反了（+ 默认宽度 340）

### 现状（已定位到行）
| 位置 | 现状 | 问题 |
| --- | --- | --- |
| `StepDetail.tsx:279` | `clampInspectorWidth(drag.startW + (e.clientX - drag.startX), drag.available)` | 面板在**右侧**：把手右移对应面板**变窄**，所以这里符号反了 |
| `StepDetail.tsx:297` | `panel.width + (e.key === 'ArrowRight' ? step : -step)` | 键盘同源反转 |
| `App.tsx:260` | `width: INSPECTOR_MIN_W`（=320） | 初始宽度等于下限；用户裁定**默认 340** |

### 改动（用户原话："仅修改拖拽逻辑，保留原有 UI 样式、把手↔箭头，不要改动面板里面的业务代码"）
1. 指针路径：`drag.startW + Δ` → **`drag.startW − Δ`**。
2. 键盘路径：`ArrowRight` 应让面板**变窄** → `step` 取负；即 `panel.width + (e.key === 'ArrowRight' ? -step : step)`。
3. `web/src/lib/inspectorPanel.ts` 新增并导出 **`INSPECTOR_DEFAULT_W = 340`**（`MIN=320` / `MAX=480` 保持不变）；`App.tsx:260` 改用 `INSPECTOR_DEFAULT_W`；`:40` 的 import 同步。
4. **不动**：`clampInspectorWidth` 的夹取公式（中心列保护语义不变）、把手 DOM/`role="separator"`/`aria-valuetext`、面板内部业务代码。

### 语义确认（写进代码注释，避免下一个人又"修回去"）
> 面板位于三栏布局的最右列。把手向右移动 ⇒ 中心列变宽 ⇒ 面板变窄。因此宽度 = `startW − (clientX − startX)`。

### 必须同步的断言（逐条对照，勿漏）
| 文件:行 | 现值 | 改为 |
| --- | --- | --- |
| `e2e/y-inspector-peek.spec.ts:198-200` | `aria-valuenow=320`、min 320、max 480 | `aria-valuenow=340`（min/max 不变） |
| `:222-223` | 向右拖 600 ⇒ 480（变大） | 向右拖 600 ⇒ **320**（到下限）；向左拖 600 ⇒ **480** |
| `:229-230` | 向左拖 ⇒ 320 | 向左拖 ⇒ 480 |
| `:233-234` | 拖 80 后 > 320 | 拖 80（向右）后 **< 340 且 ≥ 320** |
| `:237` | reload 后回到 320 | reload 后回到 **340** |
| `:249-251` | `ArrowRight` ⇒ 336（320+16） | `ArrowRight` ⇒ **324**（340−16）；`ArrowLeft` ⇒ 回到 **340**（再按到 356 也可，按实现语义断言） |
| `lib/inspectorPanel.test.ts` | `clampInspectorWidth(480, 700) === 340` | 该夹取用例**不变**（它与方向无关）；新增 `INSPECTOR_DEFAULT_W === 340` 的常量断言 |

> 注：`:249` 的期望值取决于"步长 16 且从 340 出发"，实现后按真实值对齐；**不要**为了让旧断言通过而保留反转方向。

---

## 3 #199 — 模型选择器改两级飞出（对齐 ZCode 图 2/图 3）

### 用户裁定
- **不要搜索框**（"才几个模型没有必要使用搜索框"），**只要两级**。
- 形态：参考 ZCode 的两级飞出菜单——第一级 provider，悬停/右移展开第二级 model。

### 结构
```
[模型选择器触发]  →  ┌ 第一级：provider / 默认链 ─┐
                     │ ● 默认链                  │
                     │ ● DeepSeek        18 个 ▸ │──┐
                     │ ● 自建代理        3 个  ▸ │  │
                     │ ─────────────────────────  │  │
                     │ ⚙ 管理模型（#203 入口）     │  │
                     └───────────────────────────┘  │
                        ┌ 第二级：该 provider 的模型 ─┘
                        │ ✓ deepseek-chat            │
                        │   deepseek-reasoner        │
                        └────────────────────────────┘
```

### 要求
| 项 | 要求 |
| --- | --- |
| 两级导航 | 键盘：`→`/`Enter` 进第二级，`←`/`Esc` 回第一级/关闭；鼠标：hover 展开（带 ~80ms 迟滞，防抖动）；都要能 Tab 到达 |
| 搜索框 | **删除**（含 `ModelPicker.tsx:67` 的搜索阈值逻辑与相关样式） |
| 保留 | `groupByProvider()`（`:42-51`）产出的 provider 分组语义、`默认链`伪选项（`:135-145`）、`commitSelection` 的守卫（`:79-86`） |
| 选中态 | 当前模型：勾选图标 + 加重字重 + 左侧 2px 高亮条；当前 provider 在第一级同样高亮 |
| 不可用 provider | 置灰但仍可展开（不要隐藏）；行尾显示原因（`unavailable_reason` 的文案映射在 `web/src/lib/modelAvailability.ts`，属 #199 本次新增；#203 只交付了后端机器码本身），无 reason 时显示「未配置」。**已落地（#199，2026-09-17）**，判定口径写在这里：①「不可用」= 后端明确说 `is_available: false`（缺字段是"没说"不是"不可用"；`getModels` 对 `is_available` 做**三态**解析，不用 `!== false` 归一，否则"没说"被伪造成"可用"）；② 只有**整组都不可用**才把 provider 行置灰（组里还有一个可用模型 ⇒ 不置灰，把一个可用项说成不可用比不置灰更糟）；③ 原因取组内第一条非空；④ 已知码 `missing_api_key`/`credential_unavailable` 翻成人话，**未知码回落「不可用」**（「未配置」是具体诊断，只留给"后端没给原因"那一态；机器码绝不原样打给用户）；⑤ 一级与**二级**行都写出原因文本（只靠颜色区分，色觉障碍/截图里就退化成"和别的一样"），逐条判定与文案都在 `web/src/lib/modelAvailability.ts`（纯函数，单测直测），DOM 接线由 `e2e/model-picker.spec.ts` 锁。**⚠ 本部署可达性前置**：`is_available` 只有在**自定义供应商**条目上才是真实判定（内置 preset/catalog 条目恒 `true`，见 `app.py:1044-1093`），所以"置灰 + 行尾原因"在**默认部署（未配任何自定义供应商）里看不到**——要复现得先加一个**没有 API Key 的自定义供应商**（这也是 e2e 用夹具而非真后端的原因）。**已实测的可见性配方（2026-09-17，走真端点 `TestClient`，不需要改任何代码）**：①建一个自定义供应商且**不填** API Key（`POST /api/model-providers` → 201）→ `GET /api/models` 该条目给出 `is_available: false` + `unavailable_reason: missing_api_key` ⇒ 置灰行 + 行尾原因出现；②能力徽标里 `deepseek`/`qwen`/`zhipu` 三家 preset **本来就声明 `supports_tools`** ⇒ 用这三家的默认部署「工具」徽标可见（实测 `MODEL_PROVIDER=deepseek` 的默认部署：1 个模型、`supports_tools: true`）；③要「视觉 / 思考」徽标需在 `AGENT_MODELS` 条目里**声明** `supports_vision` / `supports_reasoning_summary`（实测声明后三个徽标齐出）。**⚠ 照此配方做完要能回收，而当前回收不了**：`DELETE /api/model-providers/{id}` 对**没有 API Key** 的供应商会抛 `CredentialError: 凭据删除失败: PasswordDeleteError`，条目留在 `~/.agent-harness/model-providers.json`（已开票 **#215**）。残留不只是"删不掉"——它是**全局**可见的：会让 `tests/web/test_web_models.py` / `test_web_model_fork.py` 共 3 条内置目录断言变红（`Left contains one more item: 'myprov:some-model'`），这正是 2026-09-17 #213 收尾时实测撞到的（当时已手改 JSON 回收，`providers: []`）。做这个实验前先确认你接受手改那个文件来收尾。**不要**为了"让默认部署也看得到"去让前端替后端猜能力位、或猜凭据状态——那是编造，且会把用户引到一个用不了的模型上 |
| 信息密度 | 第二级行 = 模型名（主）+ provider 名（次级小字）；不显示价格/上下文窗口（看板负责上下文，不在选择器里堆数据）。**#199 能力徽标**（工具/视觉/思考，只在后端声明为 `true` 时出）走同一行的行尾槽位，与不可用原因共处（两者可以同时在）。**⚠ 徽标也有可达性前置**：后端只在 provider preset 或 `AGENT_MODELS` 目录条目**声明过**能力位时才下发（`app.py:1048-1092` 的 `_pick_capabilities` 只透传已声明键；`PROVIDER_PRESETS` 里只有 `supports_tools`），**自定义供应商条目恒无徽标**（只传 `display_name`）。于是"视觉 / 思考"两个徽标要有人手写 `AGENT_MODELS` 才看得到，而"置灰 + 行尾原因"只在自定义供应商上成立——**两个特性的可达域互斥**，`e2e/fixtures.ts` 里那组"zhipu 三徽标齐全 + custom 置灰"的同一画面，真后端一个部署都做不出来（夹具是为覆盖纯函数分支手工构造的） |
| 管理入口 | 第一级底部「管理模型」→ 打开 #203 的供应商管理弹层 |
| CSS | 复用 `.model-picker-content` / `.model-picker-item` 等（`app.css:5644-5731`）；`.model-picker-group-label`（`:5697-5705`）**当前是死 CSS**：本次结构替换后若不再需要就删除（属本票 scope 内的清理），若作为第一级组标题启用则在两个主题下都校验对比度 |

---

## 4 #201 — 权限 / Agent 档位 / 深度 三个下拉统一重设计（+ 合并共享组件）

### 用户裁定
- 三个下拉**合并成一个共享组件**（授权），范式对齐图 6：**图标 + 标题 + 描述 + 明确选中态**。
- 合并后 **`ContextProviderPicker` 直接删除**（其职责被 #200 看板与 #203 供应商管理取代；记忆是自动注入的不需要选）。
- `aria-label` 可统一成中文，**除非**会破坏 e2e 定位器（若破坏则保留原值，并在 PR 里注明原因）。

### 组件契约（新）
`web/src/components/OptionPicker.tsx`（共享）：

```ts
type Option = { value: string; title: string; description?: string; icon?: ReactNode };
type Props = {
  label: string;            // 触发按钮上的短标签
  icon: ReactNode;          // 触发按钮上的图标
  value: string;            // '__default__' = 未选（沿用 ControlPicker 现状）
  options: Option[];
  onChange: (v: string) => void;
  ariaLabel?: string;       // 显式传入；组件不内置 aria-label 默认值
  footer?: ReactNode;       // 例如档位收窄提示（见下）
};
```

要求：
- 每行 = 图标槽（20px 固定宽，保证多行对齐）+ 标题（主）+ 描述（次级 2 行内截断）+ 右侧选中标记。
  - **已落地（2026-09-17）**：槽**恒渲染**（`.picker-item-icon`，没有图标的行也占 20px，否则标题左边界会随图标有无跳动）；图标来源 = 前端 `lib/catalogIcons.ts` 的**内置 id → 字形**映射（三份目录 id 空间不重叠，一张表够）。
  - **未知 id 留空槽，不编字形**：目录是可扩展的（后端新增档位、夹具里的 `mode-0…mode-5`），给未知 id 配一个字形正是被禁的「编占位」（PRODUCT.md 原则 3）——槽仍在，所以对齐不破。
  - **残留缺口（已开 issue #214）**：条目契约里没有 per-option 图标数据，所以**部署自定义/后端扩展**的条目永远拿不到图标。要让部署自己声明，需要后端在条目上给一个可选 `icon` 名（前端把已知名映射成字形、未知名留空槽）。
- 选中态 = 勾选 + 加重 + 左侧 2px 高亮条（与 #199 第二级同一视觉语言）。
- `__default__` 行文案沿用「默认（未选）」；不得把"未选"渲染成空白。
- **组件不内置 `aria-label`**：由调用方传（避免三处默认值打架，也避免统一中文时误伤 e2e）。

### 三处替换
| 原组件 | 处置 |
| --- | --- |
| `ControlPicker.tsx`（权限/档位/深度共用） | 改为 `OptionPicker` 的薄包装或直接用；`DEFAULT_VALUE='__default__'`（`:42`）与「默认（未选）」行（`:108-120`）语义保持不变 |
| `ContextProviderPicker.tsx` | **删除组件文件与其专属测试**；后端 `context_providers` 契约**一字不动**（请求字段仍被接受；只是没有 UI 去选它）。若要显式关闭全部 provider，仍可用 API（见 #200 的范围界定） |
| `ModelPicker.tsx` | 保留两级结构，但**行样式与选中态复用** `OptionPicker` 的行实现（抽出行组件，不复制视觉） |

### 档位收窄提示（用户裁定："要提示，但从简，不能突兀"）
- 位置：档位 picker 弹层的 `footer`（选中行下方），一行 12px 次级文字：
  「该档位声明开放 N 个工具（全部档位声明 M 个）」
  - **措辞是「声明」口径，不是「你现在有 N 个工具」**（批 2 Spec 轴 P1）：N/M 数的是
    **声明的工具面**，本部署**实际注册**的工具是另一个集合，两个方向都会差——
    最小 harness（`CAPABILITIES=""`、无 session_store、无 Tavily key）实测注册数
    main **10** / coding **9** / research_review **3**，声明是 17/12/7
    （`retrieve_knowledge` / `web_search` / `retrieve_memory` 一类根本不在 registry
    里）⇒ 属**高报**；反过来本地 artifact 存储下 `read_artifact` 会被收窄掉却不在
    `excluded` 里（属**漏报**）。
    注册数甚至**不是同一个 CAPABILITIES 下的常量**：同一个 harness 里 websearch 缺
    `TAVILY_API_KEY`、multiagent 缺 session_store 时都按 optional 降级缺席（装配日志
    里各有一句 warning），补上它们数字就变。所以任何"本部署 = N 个工具"的说法都不
    稳；把数说成部署事实就是 UI 断言后端做不到的事，故取声明口径（逐字为真），仍完成
    用户要的那件事：让人看见"选了这个档位，工具面被收窄了"。
- hover/聚焦该行时用 `title` 列出被收窄掉的工具名（**带档位名归属**，最多 6 个 + 「、…」）。
  - 带档位名是因为 footer 描述的是**当前生效档位**，而列表里高亮的那一行可能是别的档位
    （鼠标移动即高亮）；只说「未开放：…」会被读成"这是高亮那一行的信息"。
- 该行**可聚焦**（`tabIndex=0` + `aria-label`）：键盘用户同样要能拿到 tooltip，
  否则这条提示只对鼠标用户存在。
- **不用** toast、不用 banner、不用一次性弹窗。
- 这条同时缓解 #198 的现象（用户选档位后看不到工具集被收窄），但在 UI 上只说事实，不解释原因。

**已落地（#201，2026-09-17）**，三处口径在此写明（实现里另有注释，这里是与设计稿对齐的那一份）：
1. **数据源**：`GET /api/agent-profiles` 每条带 `tool_scope {open, total, excluded}`，
   值来自 `agent/profiles.py::tool_scope_summary`。口径 = **档位声明的工具面**
   （`BUILTIN_PROFILES[*].tool_scope`），`total` = 所有内置档位声明面的**并集**；
   **不是**运行时实际注册集（那取决于本部署启用了哪些 capability，且 catalog 端点
   没有 session 上下文、不会为了数数去 build_runtime）。将来若要把 N 做成"实际开放数"，
   得在会话上下文里算（另一张票）。
2. **什么时候说**：只在 `excluded` 非空时显示。未被收窄的档位（`main`/通用）与
   未选档位（后端默认落 `main`）都**不显示**——没被收窄就没有事实要披露，
   「全部 17 个里声明了 17 个」只是噪音。
3. **披露对象**：**当前生效档位**（未选 = 后端默认档位）。跟着"当前选中的那一个"走，
   不是"刚刚 hover 的那一个"——`footer` 是调用方传入的静态内容，与 cmdk 的高亮值
   无关（要跟着 hover 走得改组件契约，收益不足）；名字归属靠 `title` 里的档位名补上。
4. **残留缺口（已知，未修）**：句子里的 N/M 是**声明面**数字，真机部署注册数与它不等。
   实测（本仓最小 harness：`Settings(capabilities="")` + `assemble_wiring` +
   `build_runtime`，之后数 `registry.list()`）：

   | 档位 | 声明（本文件口径） | 实测注册 |
   | --- | --- | --- |
   | main / None | 17 | 10 |
   | coding | 12 | 9 |
   | research_review | 7 | 3 |

   ⚠ 这张表**只说明"两个面不相等"这件事**，不要当成"另一套 N/M"：
   注册数随 wiring 与运行期前置变化（同一 harness 里 websearch 缺 key、multiagent 缺
   session_store 都会按 optional 降级缺席，补上就变），所以它**没有**一个可写进文档的
   固定值；声明口径只有一套（17/17、12/17、7/17，见 `docs/SDD_TICKET_TRACKER.md` 与
   `tests/web/test_web_phase5_staged_endpoints.py` 的镜像）。文案已改为不宣称部署事实，
   故不构成"UI 骗人"；但用户若想知道"我现在到底有几个工具"，这一步还得去别处看。
   修它需要在会话上下文里数收窄前后的 registry（`assembly.py:277-287` 已有
   `dropped_tools`），另开一张票。

---

## 5 #204 — 项目弹窗：去掉「任务内容」+ 权限选择重设计

### 用户裁定
1. 弹窗**不应有**「任务内容」输入框：用户只需"创建文件 + 设好默认权限"，然后在 chat 输入框里发消息。
2. 图 3 的权限设置**不美观** → 用 §4 的 `OptionPicker` 重做。
3. 「完全访问」这类选择要与 composer 的权限 pill **保持一致**（弹窗里的选择**只影响第一次**）。

### 5.1 前端改动
- 删除 `StartTaskInProjectDialog.tsx:154-166` 的任务内容 textarea。
- 提交守卫（`:87-101` 的 `if (!text || pending) return;` 与 `:178-184` 的 `disabled={!text || pending}`）改为**只看 `pending`**。
- 创建成功后：弹出层关闭，焦点落到 chat 输入框（用户立刻可以打字）。**不再**自动发起 run。

### 5.2 后端前置（**必须先做，否则前端无从调用**）
| 现状 | 证据 | 需要 |
| --- | --- | --- |
| `CreateSessionRequest.task` 必填 `min_length=1` | `web/app.py:198-238`（`:204`） | 允许"只建会话、不启动 run" |
| `POST /api/sessions` 总是 `create_and_launch` | `web/app.py:1041-1094` | 同上 |
| 已有"空会话"能力 | `tests/test_web_api.py:96-108`（`Session.start` 空会话，`first_user_message is None`） | 复用，不新增会话模型 |

**契约**：`CreateSessionRequest` 新增 `launch: bool = True`（默认 true ⇒ 既有行为逐字不变）。

- `launch=True`（默认）：现有路径，SSE 直驱 run。
- `launch=False`：只写 `session/started`（+ 既有 session 元数据），**不**启动 run、**不**返回 SSE，返回会话 JSON（刻意小形状：`{session_id, permission_mode}`——前端初始化 composer 状态只需这两项；仓库里不存在 `GET /api/sessions/{sid}` 单会话路由，且事件数/标题是列表页投影字段，这里没有数据来源，不伪造）。此时 `task` 可省略（若同时给了 task 而 `launch=False`，按 422 拒绝，避免"给了任务却静默不执行"）。

### 5.3 权限一致性（用户裁定 11）
- 弹窗里的权限选择 = **首次**创建会话时写入的会话级权限。
- 创建响应必须回传 `permission_mode`；前端用它**初始化** composer 权限 pill 的状态（不要各自取默认值——那就是不一致的来源）。
- 之后在 composer 改权限 ⇒ 走既有会话级更新路径，弹窗不再参与。
- **验收**：弹窗选「完全访问」⇒ 创建后 composer 的权限 pill 必须显示「完全访问」（e2e 断言两处文本一致）。

---

## 6 测试影响清单（实现时逐条核对，勿漏）

| 文件 | 影响 | 处理 |
| --- | --- | --- |
| `web/e2e/y-inspector-peek.spec.ts` | #197 拖拽方向 + 默认宽度 | 按 §2 表格逐条翻转 |
| `web/src/lib/inspectorPanel.test.ts` | 新增 `INSPECTOR_DEFAULT_W` | 加常量断言；夹取用例不变 |
| `web/e2e/u-project-task.spec.ts` | #204 去掉任务内容 + `launch=false` + 权限一致性 | 改造创建流程用例（`:7/:94/:122` 的三个 title 也要改文案） |
| `web/e2e/fixtures.ts` | `:359,371` mock 从 `body.task` 派生 `first_user_message`；`:768-795`/`:853-859` catalog；`:889-927` `pickControl`/`pickFirstModel` | mock 需支持 `launch=false`（无 run、空会话）；picker 定位器若因合并组件改名，**同步更新这两个 helper**（改一处即可覆盖多处用例） |
| `web/src/hooks/useSession.ts` | `:842` `mode:'queue'` 调用、`:869-872` queued ack → `setMode('viewing')` | #204 的 `launch=false` 不走 SSE；确保空会话创建后不进入 `live` 模式 |
| 既有 `ContextProviderPicker` 相关测试 | 组件删除 | 一并删除；后端 `context_providers` 契约测试**保留**（不受影响） |

**前端门禁（AGENTS §16.6，缺一不可）**：
```
cd web && npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build
```
（e2e 必须 `--workers=2`：4 worker 全量并行有资源竞争型抖动。）

---

## 7 视觉方向（给 `impeccable` 的起点，不是终点）

- **模式**：Operate。信息密度与一致性优先；动效只用于状态转换（展开/选中/hover），时长与缓动沿用现有 motion token。
- **层级**：一级用 1px 边框 + 背景色差区分弹层；二级飞出用同色系更浅一档，**不用阴影堆叠**（三层阴影会脏）。
- **选中**：勾选图标 + 2px 高亮条 + 字重变化，三者同时给出（只靠颜色不足以表达选中）。
- **留白**：沿用现有 spacing token（不引入半像素值）；菜单行统一 32px 高（图标行 40px）。
- **禁止**：纯装饰渐变、发光边框、圆角不一致、emoji 当图标、把工具名/模型名截断到不可辨识（截断要保留可辨识前缀 + `title` 全文）。
- **空态**（#203/#204 共用）：一行说明 + 一个主行动按钮，不放大插画。

---

## 8 Out of scope

- 不做搜索框（用户明确否掉）。
- 不改后端 `context_providers` 契约（只删前端选择器）。
- 不改 Inspector 面板内部业务代码与既有样式（#197 只改方向与默认宽度）。
- 不引入 UI 库/图标库/图表库。
- 不做移动端专属布局（沿用现有 <1200px 折叠行为）。
- 不改权限的后端语义（只改默认值与 UI 一致性）。
