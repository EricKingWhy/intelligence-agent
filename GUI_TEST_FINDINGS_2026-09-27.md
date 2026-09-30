# Agent Harness Inspector — Web UI 真实用户 GUI 测试报告

- 测试日期：2026-09-27 00:00–01:30（本机时间）
- 测试方式：**黑盒 GUI 测试**（chrome-devtools 受控浏览器，模拟真实用户逐个功能操作）+ 后端 API 只读取证（定位异常根因用）+ impeccable 审美检查（critique 方法论 + detect 源码扫描）
- 被测对象：Agent Harness Inspector（`web/` Vite dev :5173 + FastAPI :8000，`main` 分支当前工作树）
- 浏览器环境：Chromium 154（受控实例，干净 profile），视口约 1998×976
- 结论速览：**核心链路（建会话→发消息→流式回答→工具调用→视图切换→会话管理）整体可用且渲染质量好；但存在 2 个 P0 级行为问题（页面加载自动执行遗留排队消息、加载过程自动改写模型与权限）和一批一致性/无障碍问题。**

---

## 一、环境准备记录（非测试操作）

1. 启动后端 `uv run uvicorn agent_harness.web.app:create_prod_app --factory --reload --host 127.0.0.1 --port 8000`（后台）。
2. 启动前端 `cd web && pnpm dev`（后台）。
3. 浏览器为受控 Chromium 干净 profile（无历史 localStorage）。
4. 未修改任何被测代码；所有问题仅记录。

**测试产生的数据**（均为本机开发数据）：
- 新会话 ×2：`80fa7176`（FizzBuzz 失败→换 qwen-plus 成功 2 轮→被归档又取消归档→恢复原状）、`06e37abf`（由 80fa7176 第 3 轮分叉产生）。
- 页面加载自动执行的历史排队消息 ×2（见 F1，非我发送）。
- 后端服务与前端 Vite 目前仍在后台运行（会话结束即停；如需手动停止：结束对应 node/uvicorn 进程）。

---

## 二、问题清单（按严重度）

### P0（阻断性 / 安全相关）

#### F1【P0·安全相关】页面加载后自动执行历史排队消息，无需用户确认
- 现象：刷新页面数秒后，会话 `0adc85eb` 在无任何交互的情况下自动收到用户消息并执行。
- 证据（后端事件 + 本页面网络请求双重印证）：
  - `16:14:26 UTC session/resumed`（页面刷新重连）
  - `16:14:32 UTC user/message {"content":"当前项目做到哪个阶段了？"}` → run/started → 39×text/delta → run/completed
  - `16:15:59 UTC user/message {"content":"当前项目是个什么项目？"}` → run/started → 7 轮模型调用 + 11 次工具调用 → run/completed（16:17:02）
  - 本页面网络记录：`GET /api/sessions/0adc85eb…/queue` 之后自动 `POST /api/sessions/0adc85eb…/messages` ×2，请求体带 `"mode":"queue"`。
- 影响：4 天前遗留的排队消息在用户下次打开页面时被**静默执行**，消耗真实模型 API 费用；用户无感知、无确认步骤。若遗留消息含写操作，等同无人监督执行。
- 截图：`gui-test-screenshots/t05_auto_restored_session.png`

#### F2【P0·安全相关】同一加载过程自动改写会话模型与权限，最终停在 danger-full-access + 自动批准
- 现象：与 F1 同一加载过程，页面自动发出 `POST /model` ×1（qwen-plus→qwen3.8-27b）与 `POST /permission` ×6，权限在 danger-full-access → workspace-write → danger-full-access → read-only → workspace-write → danger-full-access 间循环改写（16:15:07–16:15:28 UTC），全部 `auto_approve: true`，最终停在 `danger-full-access + auto_approve=true`。
- 证据：网络请求 reqid=381/382/385/388/391/394/397；后端事件 307（model/changed）+ 308–313（permission/changed）。
- 影响：F1 自动执行的第二条消息正是在该权限下运行了 11 次工具调用（本次全部为 dir/type/glob 等只读命令，无破坏性副作用，属侥幸），且直接读到了宿主机 `D:\intelligence-agent` 仓库内容。
- 连锁后果：之后新建的会话默认继承 qwen3.8-27b（F9）。

### P1（显著影响使用）

#### F8【P1】运行失败时聊天区不显示任何错误信息
- 现象：新会话发送「写一个 FizzBuzz 脚本并运行验证」，运行立即失败；聊天区只有用户消息孤零零挂着，**没有错误文案、没有重试入口**，仅页头状态徽章变红色「失败」。
- 根因数据（后端事件里有、UI 没渲染）：`model/failed {"message": "模型供应商账户不可用（欠费 / 配额耗尽 / 账户被冻结），请到供应商控制台检查计费与配额", "reason": "provider_account_unavailable"}` + `run/failed`（含 Langfuse trace_url）。
- 缓解线索：错误详情其实在右侧 **Inspector 面板的事件时间线里可见**（run/failed 行显示了完整中文原因）——但 Inspector 默认收起，普通用户找不到。
- 截图：`t03_send_failed_no_error.png`、`t04_running_auto_approved_bash.png`（Inspector 时间线可见于暗色主题截图）

#### F9【P1】失败后无法从 UI 恢复：被自动改坏的模型默认值 + 换模型才能重试
- 新会话继承 F2 中被自动改成 qwen3.8-27b 的模型，而该供应商账户不可用；手动切回 qwen-plus 后发送成功（回答"2"正常）。与 F8 叠加后，普通用户极可能卡死且不知原因。

#### F10【P1】权限模式下拉：无当前档位指示 + 选项不进无障碍树 + 键盘高亮不可见
- 「工具调用如何批准？」弹窗三个选项（只读/工作区写入/完全访问）**没有任何"当前已选"标记**；composer 的权限按钮也只显示"权限"二字（对比：模型按钮显示 qwen-plus、Agent 显示 通用、推理显示 深度——这三个弹窗都有 ✓ 选中指示，唯独权限没有）。首访时弹窗还出现过第 4 个选项「默认（未选）」，会话中再打开就没有了，选项集合不一致。
- 无障碍树中该弹窗的 listbox **不包含任何选项节点**，屏幕阅读器用户无法选择权限档。
- 键盘 ArrowDown 后焦点圈套在**整个选项组**上而非单个选项；按 Enter 直接命中**最后一项「完全访问」**。所幸升级有二次确认（"确认升级？" + 升级/取消——好设计），Esc 可取消。
- 成熟产品参考：VS Code 设置、Linear、Raycast 的单选列表总是标当前项且键盘高亮画在单项上；ARIA combobox/listbox 模式要求选项可被辅助技术枚举。
- 截图：`t04_permission_dialog.png`

#### F11【P1·安全相关】默认权限状态下 bash 工具未经审批直接执行
- 权限档为"默认"（按钮只显示"权限"）的新会话里，agent 执行 `bash echo gui-test-ok` **未弹出任何审批卡片直接执行成功**（162ms，回答正确）；会话 `session/started` 事件里 `"auto_approve": true`。
- 结合 F2（页面加载会自动把权限改到 danger-full-access + auto_approve=true），权限体系"何时需要人审批"的表现混乱：UI 看不出当前档位，实际行为接近默认不审批。

#### F15【P1】会话视图被自动切换，覆盖用户选择
- 无操作情况下，主视图从会话 `06e37abf` 自动切回 `f522d4a9`（两次独立观察到 UI 状态被自动覆盖：另见 F4 高亮错位、F5 首帧主题突变）。用户正在看的会话会被应用擅自换掉。

#### F16【P1】会话切换后 composer 失联：有内容但发送禁用；草稿跨会话残留
- 复现路径：在会话 A 输入文本 → 会话被自动切到会话 B（F15）→ 输入框内容**跟着出现在 B 会话**（草稿跨会话泄漏，可能误发给错误会话）→ 发送按钮保持禁用、Enter/Ctrl+Enter 均无效，必须清空重输才能发送。
- 证据：快照中 textbox `value="【边界测试】…"` 而 button 发送带 `disabled`；两次 fill + Ctrl+Enter 均无效。
- 影响：既丢效率又有误发风险。

### P2（一般问题）

#### F3【P2】加载时对已不存在的会话发起请求（404）
- 每次加载 `GET /api/sessions/dd757a3b-…/events` ×2 → 404（"上次会话"指针指向已删除会话，残留引用未清理）。

#### F4【P2】左侧列表高亮与当前会话短暂不一致（一次性）
- 自动恢复会话后：主视图为 0adc85eb，但列表高亮定位到 e718cae0；事件更新后自愈为正确高亮。疑与 F15 同源（自动恢复的定位逻辑）。

#### F5【P2】首帧主题与持久化主题不一致 + 首次加载弹层自动展开
- 本浏览器实例首次导航：暗色主题 + "工具调用如何批准?"权限弹层无交互自动展开；两次刷新后为亮色（localStorage `ahi.theme=light`）且弹层不出现。UI 状态（主题/会话/设置）异步恢复导致首帧与最终状态不一致。
- 截图：`t01_first_load_dark_popup.png` vs `t01_reload_light_no_popup.png`

#### F6【观察】记忆系统持续降级运行
- 事件流多次 `memory/degraded`：extraction/consolidation `heuristic_fallback: NotFoundError`、formation `provider_error`。同时记忆形成功能实际有产出（自动运行后形成了 2 条记忆，见记忆管理弹窗）。降级提示未在 UI 上呈现给用户。

#### F7【观察】排队消息的 SSE 流被客户端中止但执行未受影响
- 第二条自动执行消息的 `POST /messages` SSE 流以 `net::ERR_ABORTED` 结束，但后端 run 正常完成、事件全部落库，前端也完整渲染了回答。

#### F12【P2】前端控制台错误（React Hooks 用法错误）
- `State updates from the useState() and useReducer() Hooks don't support the second callback argument…` ×2 —— 有组件在给 state setter 传第二个回调参数，真实代码缺陷。
- 另有 WS 重连警告 `WebSocket is closed before the connection is established`（瞬时）。

#### F13【P2】弹层会自行关闭（一次）
- 记忆管理弹窗打开后未做任何操作自行关闭一次（点击其"关闭"时报"元素已不存在"）。疑与 WS 重渲染/焦点管理有关，置信度中。

#### F17【P2】无会话搜索
- 93 个未分组会话只能滚动查找，没有搜索框 / Cmd+K 快速跳转。项目已引入 cmdk（权限弹窗在用），但未用于会话检索。参考：Linear/Slack/VS Code 的命令面板。

### P3（打磨项，见审美清单）

---

## 三、审美检查（impeccable critique 方法论）

方法说明：本节为单上下文精简版 critique（无浏览器 overlay 注入；`impeccable detect` 已对 `web/src` 做确定性扫描，共 **4 处发现**，全部为 side-tab 反模式）。模式判定：**Operate**（工具型 UI：可扫读性、一致性、桌面键盘优先 > 表现力）。Nielsen 启发式打分见文末。

| # | 问题 | 位置 | 原因 | 置信度 | 成熟产品参考 |
|---|------|------|------|--------|--------------|
| A1 | 权限弹窗无"当前档"指示，与 Agent Profile/Reasoning 弹窗（有 ✓+左侧高亮条）不一致 | composer 权限弹窗 | 同类单选弹窗应遵循同一视觉模式；用户无法确认当前生效档位 | 高（有截图对照） | VS Code 设置、Linear、Raycast：单选菜单必有当前态 |
| A2 | 权限弹窗选项未暴露给无障碍树；键盘高亮套整个选项组 | 同上 | 辅助技术不可达 + 键盘用户无法辨认所选项 | 高（a11y 快照实测） | ARIA listbox/option 模式；Radix 官方示例 |
| A3 | 记忆管理弹窗右侧「详细信息」按钮文字被挤压成竖排 | 记忆弹窗记忆卡片右侧 | 弹性布局未给按钮留出最小宽度 | 高（有截图） | Notion/Arc 设置弹窗：操作按钮固定最小宽度或折叠为图标 |
| A4 | 侧栏会话行"标题+ID+事件数+日期"中 ID 全量展示，占据第二行主要视觉 | 左侧会话列表 | ID 是调试信息，对浏览场景是噪音，弱化标题可读性 | 中 | ChatGPT/Claude 侧栏：仅标题+相对时间，元数据入 tooltip |
| A5 | 消息操作按钮（复制/编辑/分叉）常态常驻，视觉密度偏高 | 聊天区每条消息 | 低频操作常驻增加噪音；与"克制密度"的设计目标（DESIGN.md）相悖 | 中 | ChatGPT/Claude：hover 才显示操作按钮 |
| A6 | 短回答（如"2"）的气泡占满整行宽度，上方「折叠」链接孤立悬空 | 聊天区回答块 | 内容与容器比例失衡，视线跳转远 | 中 | ChatGPT：回答块宽度随内容收敛 |
| A7 | side-tab 左边框 accent ×4（app.css L2324/2834/3001/5663） | 全局样式 | impeccable detector 判定为"AI 生成 UI 最典型特征"，建议更含蓄的强调方式 | 高（确定性扫描） | Linear/Vercel Dashboard：用背景色块或图标着色，不用厚左框 |
| A8 | 运行中"Esc 停止"快捷键提示与红色停止按钮并排，功能重复 | composer 运行态 | 同一动作两个入口并排，弱化彼此 | 低（设计取舍） | Claude Code / Cursor：快捷键提示弱化为 tooltip |
| A9 | 亮色主题整体可用但质感弱于暗色主题（暗色是设计主世界：blossom 粉签名、液态玻璃质感） | 全局 | 亮色下部分组件（弹出层、状态徽章）对比度与层次感下降 | 中 | 双主题产品（GitHub/Radix Themes）对双主题做同权重打磨 |

**做得好的**（避免只挑刺）：暗色主题整体克制统一、blossom 签名色使用有节制；运行中状态徽章（空闲/思考中·Ns/已完成/失败）与停止按钮的形态变化清晰；Inspector 的时间线/Overview/Terminal 把"事件流是本体"的产品定位落得很实在；诚实空态（上下文容量显示"后端未上报用量数据"而非伪造数字，Terminal 空态、文件/改动空态文案准确）。

**Nielsen 启发式打分**（0–4，Operate 模式）：

| # | 启发式 | 分 | 关键问题 |
|---|--------|----|----------|
| 1 | 系统状态可见性 | 2 | 徽章/停止按钮好；失败原因与当前权限档不可见 |
| 2 | 贴近真实世界 | 3 | 中文文案自然（"改档从下一轮 run 起生效"） |
| 3 | 用户控制与自由 | 3 | 编辑/分叉/归档可逆；Esc 停止 |
| 4 | 一致性与标准 | 2 | 弹窗选中态不一致；Esc 关闭行为不一致 |
| 5 | 错误预防 | 2 | 权限升级有确认好；但应用会自行改写权限、默认 auto-approve |
| 6 | 识别而非回忆 | 2 | 无会话搜索；权限档靠记忆 |
| 7 | 灵活与效率 | 3 | 键盘优先；权限菜单键盘不可用 |
| 8 | 美学与极简 | 3 | 暗色克制统一；A3/A6 小瑕疵 |
| 9 | 错误恢复 | 1 | 失败无内联错误与重试 |
| 10 | 帮助与文档 | 2 | tooltip 丰富；无帮助入口 |
| **合计** | | **23/40** | **Acceptable——有扎实的底子，显著改进空间** |

**Persona 红旗**：
- **Alex（急性子高手）**：无会话搜索/Cmd+K；composer 失联迫使他重输整段文字；权限菜单键盘不可用；会话被自动切换打断心流。
- **Sam（无障碍依赖）**：权限弹窗选项不在 a11y 树、键盘高亮不可见——这两个点直接让权限功能对屏幕阅读器用户不可用。
- **Riley（压力测试者）**：刷新页面=旧排队消息被静默执行（P0 惊吓）；会话自动切换；草稿跨会话泄漏。Riley 会把这三个都录成视频发出去。

---

## 四、功能测试覆盖记录（通过 ✅ / 失败 ❌ / 受阻 ⛔）

| 测试点 | 结果 | 备注 |
|--------|------|------|
| 初始加载/布局/空态 | ✅ | 首访状态异常见 F5 |
| 新建会话 | ✅ | |
| 建议 chip 填充输入框 | ✅ | |
| 空输入禁用发送/输入后启用 | ✅ | 但见 F16 失联场景 |
| 发送消息→流式回答→状态徽章（空闲/思考中·Ns/已完成） | ✅ | qwen-plus |
| 发送失败（供应商不可用） | ❌ | F8：无错误提示；F9：默认模型不可用 |
| 工具调用渲染（bash 行+耗时+折叠） | ✅ | |
| 工具审批 UI | ⛔→❌ | 默认档下 bash 自动执行，未见审批卡（F11）；审批卡 UI 无法在默认路径触达 |
| 视图模式 紧凑/均衡/详细/Raw | ✅ | Raw 模式底部出现深色 Raw 块 |
| Tab：Chat/文件改动/输出 | ✅ | 输出 Tab 命令历史+exit code 准确 |
| Inspector：Timeline/Overview/Terminal + 钉住/整页/关闭 | ✅ | 失败 run 的错误详情在这里可见 |
| 模型选择菜单（供应商子菜单） | ✅ | |
| Agent Profile / Reasoning 菜单 | ✅ | 有选中指示（对照 A1） |
| 权限模式弹窗 | ❌ | F10/A1/A2；升级确认与 Esc 取消 ✅ |
| 记忆管理弹窗 | ✅ | A3 竖排文字；自动形成记忆 ×2 可见；弹窗自关一次（F13） |
| 上下文容量弹窗 | ✅(空态) | 常显"后端未上报用量数据" |
| API 令牌弹层 | ✅ | Esc 不关（小问题），再次点击按钮可关 |
| 新建项目弹窗 | ✅ | 目录浏览器+说明文案，未实际注册 |
| 会话操作菜单（加入项目/新建项目/归档/删除会话） | ✅ | 归档→列表消失→显示已归档→取消归档 全闭环 ✅；未实测删除 |
| 编辑消息（仅最新一条可编辑） | ✅ | Esc 取消 ✅ |
| 分叉 | ✅ | 子会话正确继承历史前缀 |
| 主题切换（亮↔暗） | ✅ | 暗色为主世界，质感好 |
| 收起/展开 Inspector | ✅ | |
| 显示已归档会话 | ✅ | |
| 历史大会话加载（2348 事件） | ✅ | 约 8s 内完成渲染，无卡死 |
| 超长文本+emoji+RTL+特殊字符输入 | ⛔ | 受 F16 阻断：文本可入框但无法发送 |
| 运行中 Enter 排队 | ⛔ | 受 F16 阻断（排队端点存在：GET /queue 已在加载时观察到） |
| 运行中停止（红按钮/Esc 停止） | ⛔ | 按钮与快捷键提示存在，未实测打断效果 |
| 控制台错误收集 | ✅ | F12 |

---

## 五、修复优先级建议（供后续排期参考，本次未做任何修改）

1. **F1+F2（P0）**：页面加载不得自动执行遗留排队消息、不得自动改写模型/权限——队列 flush 需要显式确认；这两条是安全边界问题。
2. **F8+F9（P1）**：run 失败时在 Chat 内联渲染 `model/failed` 的中文原因 + 重试/切换模型入口。
3. **F10/A1/A2（P1）**：权限弹窗补当前档指示、修 a11y 与键盘高亮。
4. **F15+F16（P1）**：去掉会话自动切换；composer 状态随会话切换重置（草稿要么随会话保存，要么清空并提示）。
5. **F17（P2）**：Cmd+K 会话搜索（cmdk 已在依赖里）。
6. 其余 P2/P3 打磨。

## 六、证据文件

`gui-test-screenshots/`：
- `t01_first_load_dark_popup.png` — 首访：暗色+权限弹层自动展开（F5）
- `t01_reload_light_no_popup.png` — 刷新后：亮色+无弹层（F5 对照）
- `t03_send_failed_no_error.png` — 运行失败聊天区无错误（F8）
- `t04_permission_dialog.png` — 权限弹窗无选中指示（F10/A1）
- `t04_running_auto_approved_bash.png` — bash 自动执行+运行态（F11）
- `t05_auto_restored_session.png` — 自动恢复会话+侧栏高亮错位（F1/F4）
- `t13_memory_dialog.png` — 记忆弹窗+A3 竖排文字（F6 观察的 UI 侧）

---

# 第二、三轮：暴力测试报告（前端全控件 + 后端 API 直测）

> 测试方式：CDP 真实点击/键入 + 原生 DOM 探针（只读）+ curl/HTTP 直打后端 API（黑盒）。
> 遵守约束：未修改任何代码；未删除任何用户真实数据（记忆 4 条原样、设置原样、用户会话未动）；
> 测试残留：自建测试会话 94bc3fcf / a84b54a8 / 88109bad（fork 子会话），已把 94bc3fcf 权限还原 workspace-write。

---

## 七、第二轮：前端暴力测试

### 7.1 新实锤问题

**F27（P0）切走再切回运行中的会话，UI 彻底失联**
- 现象：会话 run 执行中（后端 seq 持续前进，bash sleep 60 已调用）时切到别的会话再切回：顶部状态显示「已完成」、composer 不出现停止按钮、后续新事件完全不渲染（模型最终回答 done2 不出现、token 计数不涨）。刷新页面后才恢复并补齐全部内容。
- 复现：UI 发起慢任务 → 切会话 → 切回 → 观察 60s+。
- 置信度：高（后端事件流与 UI 状态对拍，三次采样一致）。
- 危害：用户在此状态下既看不到进度也无法停止 run；状态栏在撒谎。
- 成熟产品做法：ChatGPT / Claude.ai 切回运行中的对话会立即恢复 streaming 状态并保留停止按钮；Linear 的实时面板在重新聚焦时无条件 resync。至少应在会话切换回来时做一次「GET events 全量对账 + 恢复订阅」。

**F25（P1）侧栏会话列表不自动刷新**
- 现象：后端 API 新建的会话（fork 子会话、API 建会话）在已打开的页面里永远不出现，手动刷新后才出现在列表并计数正确（刷新前 91/93，刷新后 93/93）。
- 置信度：高（后端列表与侧栏列表对拍 + 刷新前后对照）。
- 成熟产品做法：Slack / Linear / ChatGPT 的会话列表经 SSE/WebSocket 推送即时更新；本应用已有 per-session 事件流通道，列表级变更却要靠刷新。

**F18（P1）Inspector「整页」切换行为不稳定**
- 现象：第一次 JS 点击「整页」→ timeline 撑到 1422px ✓；第二次同位置点击无任何效果；换 CDP 真实点击 → 面板反而整体收起。同一控件三种结果。
- 置信度：高（宽度实测 340→1422→无变化→面板宽度 0）。
- 成熟产品做法：VS Code 面板 maximize 切换是一个确定性 toggle，图标随状态变化，再点一次回到原状。

**F19（P1）事件行点击无任何反应**
- 现象：Timeline 里 `tool/call`（bash）与 `run/failed` 行被点击后 DOM 零变化（对比：消息气泡可展开）。
- 期望：点击事件行至少在 Inspector 内聚焦/展开该事件详情。
- 置信度：高（点击前后 innerHTML/尺寸 diff 为空）。
- 成熟产品做法：任何可观测性工具（Datadog、Sentry trace 视图）中事件行都可点击展开详情——不可点行的 hover 光标应为 default 而非 pointer。

**F20（P2）消息 Inspect 按钮反而收起 Inspector**
- 现象：消息卡片「Inspect」点击 → 右侧 Inspector 面板整体收起（宽度 0），详情其实已写入但用户看到的是"面板消失"。
- 置信度：高。
- 成熟产品做法：Inspect 类按钮的目标态是"面板打开且定位到对象"；若面板已关应先展开。

**F21（P2）Inspector 分隔条键盘 resize 完全无效**
- 现象：分隔条 role=separator，aria-valuenow=340，焦点后连按 ArrowRight ×2 值纹丝不动（aria-valuenow 仍 340，宽度不变）。
- 置信度：高（属性实测）。
- 成熟产品做法：WAI-ARIA separator 模式要求方向键以固定步进改值；VS Code / Figma 的可拖拽分隔条都支持键盘。

**F22（P2）模型菜单分组标题不可展开**
- 现象：模型选择菜单里 qwen / mimo 分组标题是死的（无子菜单、无折叠、点击无反应），分组下模型已直接平铺——标题纯装饰却长得像可交互。
- 置信度：中高。
- 成熟产品做法：要么去掉假分组头，要么分组可折叠（macOS 菜单分组头从不做成可点击的样子）。

**F23（P2）权限对话框：选择后不自动关闭 + 无当前档标记（补强第一轮 F10）**
- 现象：四个选项是裸 `role=option` DIV，无当前档标记、cursor:default；选定后对话框停留（对比：Agent Profile / Reasoning Effort 选完即关）；composer 按钮只显示「权限」二字不带当前档名。
- 置信度：高。
- 成熟产品做法：单选对话框选定即关（本应用其他三个 picker 均如此）；RadioGroup 需有可见选中态；当前档名应回显在触发按钮上。

**F24（P3）API 令牌 popout 保存后自动关闭**
- 现象：保存成功 → popout 自动关闭，导致「清除」按钮必须重新打开 popout 才能点到；保存与清除是两个不对称的路径。
- 置信度：中。
- 成熟产品做法：保存后停留在表单并给 toast 成功态，或关闭后把「已设置/清除」入口态显示在触发按钮上。

### 7.2 边界输入与键盘扫描（通过项 ✅）

| 用例 | 结果 |
| --- | --- |
| `<script>alert('xss')</script>` + `<b>` + emoji + `{{template}}` + `<%=injection%>` 发送 | ✅ 0 个 script 元素、HTML 全部转义为纯文本显示、alert 未触发（XSS 安全） |
| 纯空白（空格/Tab/换行）输入 | ✅ 发送按钮保持禁用 |
| Shift+Enter | ✅ 换行不发送（textarea 值 `l1\nl2`） |
| Enter（无选中会话的空态） | ✅ 自动创建新会话并排队发送 |
| Esc 在 composer | 不清空也不发送（中性；与 ChatGPT 一致，非缺陷） |
| run 执行中切换主题 ×2 | ✅ 主题即时切换，布局无抖动 |
| 控制台 | ✅ 0 error / 0 warn（仅 vite 连接与 React DevTools 提示） |
| 网络 | ✅ 0 个 4xx/5xx（dev 模式下目录类接口初始化重复请求两次，是 React StrictMode 双挂载，生产构建不存在） |

### 7.3 对第一轮结论的修正

1. **撤回**：「Esc 无法关闭 API 令牌 popout」——第二轮无法复现，Esc 行为正常。
2. **修正**：「上下文容量永远显示无数据」——不成立。会话含 usage 元数据时正常显示（实测 6,563/200,000 tokens、3.3%、缓存命中 94.2%、分段明细齐全）。原观察的会话缺 usage 数据。
3. **破案**：第一轮「侧栏会话计数 93→90、3 条会话查无踪迹」——主因是 F25（列表陈旧）+ API 新建会话不进列表；刷新后计数与后端完全一致。

---

## 八、第三轮：后端暴力测试（API 直测）

### 8.1 重大问题

**B-fork-1（P0）fork 自动重放历史运行，且继承父会话的坏模型链**
- 现象：`POST /api/sessions/{id}/forks`（只带 `from_seq`，不带 task）创建子会话后，子会话**自动开始执行 agent run**：turn 1 用 qwen3.8-27b 失败（供应商欠费）→ 自动切 qwen-plus 成功——两次真实 LLM 调用，消耗真实配额。而父会话该轮早已完成过。
- 语义细节：`from_seq=16`（锚点消息）→ 子会话复制到 seq 15，`boundary_user_message_seq=16 / fork_point_seq=15`，lineage 正确。
- 问题点：(a) fork 不应重放已完成的历史轮次（这是「恢复执行」不是「分叉」）；(b) 子会话继承父会话的模型链而非当前默认（mimo-v2.6-flash），对旧会话 fork 必然先失败一次再 fallback，每次 fork 白烧一次失败调用。
- 置信度：高（子会话事件流完整记录两次 run）。
- 成熟产品做法：ChatGPT / Claude.ai 的「从这条消息分支」只复制历史上下文，绝不自动执行；n8n / GitLab fork 语义均为「得到副本，等待输入」。若产品语义是「fork 后继续未竟任务」，必须在 UI 明示并让用户确认执行。

**B-resume-1（P0）同 run 续跑是 catch-22，通道实际不可达**
- 现象：`POST /api/sessions/{id}/resume` 带 `run_id` → 422，错误信息要求「run_id + expected_version + resume_basis（03 §5）」；但 `ResumeRequest` schema **没有 expected_version 字段**，把它塞进 body 会被静默丢弃，永远 422。错误信息要求的参数是 API 拒绝接受的参数。
- 对照：PATCH /api/memories/{id} 的 body 里确实有 `expected_version` 字段——说明该概念存在，只是 resume 的 schema 漏了。
- 不受影响：task-only resume（新 turn）正常工作，SSE 流式返回，取消→续跑→完成事件连续性完好（seq 15 run/failed → seq 18 user/message → seq 27 run/completed，无缺号）。
- 置信度：高（三种 payload 组合 + schema 比对）。
- 成熟产品做法：OpenAI Assistants 的 run 生命周期 API、Temporal 的 resume 信号都保证「报错信息里引用的字段一定是 schema 接受的字段」；这是 API 自洽性的底线。

**B-memory-1（P0）记忆形成/召回全线降级，且降级事件无细节**
- 现象：`extraction_enabled=true` 且 recall_enabled=true，但每个完成的 run 之后都出现 `memory/degraded {operation:'formation', reason_code:'provider_error'}`——跨会话验证（80fa7176 累计 6 次、88109bad 1 次、94bc3fcf 多次），**0 次成功**。recall 路径同样出现 `retrieval_timeout`。
- 可观测性缺口：事件只有 reason_code，无 provider 名、无错误消息、无 job 详情——用户和开发者都无法从事件知道"为什么挂"。
- 影响：整个记忆子系统（写+读）实际不可用，但设置面板显示一切正常（无健康状态暴露）。
- 成熟产品做法：LangMem / Mem0 的失败回调带 provider 与异常栈；ChatGPT 记忆失败在 UI 有「记忆暂时不可用」提示；健康度应暴露在设置页。

**B-memory-2（P0）记忆 item 端点与集合端点读的不是同一份数据**
- 现象：`GET /api/memories?limit=200` 返回的记忆（如 `f2162c12-…`，共 4 条），用同一个 ID 访问 `GET /api/memories/{id}` 与 `GET /api/memories/{id}/versions` 全部 404 "memory not found"。
- 危害：列表里显示的记忆无法单查、无法查版本历史——UI 记忆管理弹窗的任何单条操作都会失败；PATCH/DELETE 以 item 端点为入口，同样打不中集合端点返回的数据。
- 置信度：高（同 ID 两种端点对拍，畸形/不存在 UUID 返回一致 404，说明 404 不是格式问题）。
- 成熟产品做法：CRUD 不变量——集合端点返回的每个 ID 必须能被 item 端点 GET；这是任何 REST API 冒烟测试的第一条。

**B-memory-3（P2）记忆没有创建 API**
- 现象：`/api/memories` 集合只有 GET；PATCH 要求 `expected_version + content + payload` 全部必填。记忆只能由系统自动形成——但自动形成 100% 失败（B-memory-1），等于记忆子系统没有任何可用写入路径。
- 判断：可能是设计（记忆只许系统形成）。若为设计，需在 API 文档/UI 说明；与 B-memory-1 叠加后应按「写入通道整体不可用」处理。
- 成熟产品做法：ChatGPT 记忆可由用户手动添加/编辑；Mem0 提供 add/update API 与系统自动抽取并存。

**B-model-1（P2）无效模型的 422「可选」列表与真实目录不同步**
- 现象：`POST /api/sessions/{id}/model` 传垃圾值 → 422 提示可选 `[('senseaudio','glm-5.3-flash'), ('qwen','qwen3.8-27b'), ('qwen','qwen-plus')]`，**漏掉当前默认模型 mimo-v2.6-flash**（`/api/models` 中 `is_default:true, is_available:true`）。
- 成熟产品做法：错误提示里的枚举应从与校验相同的目录源生成（Stripe / GitHub API 的错误枚举永远与真实目录一致）。

### 8.2 通过项（后端工程质量好的部分 ✅）

| 用例 | 结果 |
| --- | --- |
| 取消运行中 run（`/cancel`） | ✅ `{"status":"cancelling"}`，事件流记录 `run/failed reason=cancelled`，工具调用即断 |
| 取消后再 resume（task-only） | ✅ 新 turn 事件连续（seq 无缺号），模型正常应答 |
| `/recover` | ✅ 200 返回全量事件（幂等恢复语义） |
| 无 task / 无参数的 resume | ✅ 422 + 引用规格 03 §5 的中文说明（报错质量高，虽然撞上 B-resume-1） |
| 3 条消息 rapid 排队 | ✅ 全部入队（`/queue` 可见），run 结束后自动逐条消费，`queue/consumed` 事件可观测 |
| 队列单项取消 | ✅ 从队列移除；不存在的 queue_id → 404 带明确解释（"not found, already consumed, or cancelled"） |
| run 活跃时 flush 队列 | ✅ 409 "session has an active run"（正确的冲突语义） |
| 100001 字符 content | ✅ 422 明确上限 100000 |
| 不存在的会话 | ✅ 404 `"session '…' not found"` |
| 假 approval_id 的 /approve | ✅ 404 解释"无交互审批队列（权限档非交互或 run 已终止）" |
| 无效权限档 | ✅ 422 列出全部合法值 `['read-only','workspace-write','danger-full-access']` |
| 无效模型 | ✅ 422（但见 B-model-1） |
| `limit=1000` 查记忆 | ✅ 422 上限 200 |
| bulk-delete 语义 | ✅ 必须携带口令 `confirmation:"DELETE"`；零匹配 kind 返回 `{affected_count:0}` 不误伤 |
| memory-settings 校验 | ✅ `"yes"`→true（Pydantic 宽松转换，标准行为），`"abc"`→422 |
| `auto_approve:true` + danger 级 bash | ✅ 仍要求审批（"工具授权级别为 danger，此操作需要审批"）——安全正确；但参数名有误导性，文档应写明 auto_approve 不豁免 danger 级工具 |
| POST /api/sessions 带 task | ✅ SSE 流式返回事件（`data:` 行），不是阻塞式 HTTP |
| fork 边界 payload | ✅ 无效会话 404；负数/超大/非整数 seq 422 |

### 8.3 前后端一致性备注

- 422 错误消息中出现「permission_mode not interactive」措辞，但合法档位是 `read-only/workspace-write/danger-full-access`，没有 `interactive`——措辞含糊（形容词用法），建议改为「权限档非交互模式」。
- `/api/model-providers` 返回 `{"providers":[]}` 而运行时模型来自 `/api/models` 预置目录——两个端点职责并存但命名易混（providers=用户自管供应商，models=可用目录），UI 已正确使用 `/api/models`。

---

## 九、两轮合并后的修复优先级（更新第一轮第五节）

**P0（用户可稳定踩中、且有数据/配额/状态正确性风险）**
1. F1+F2：页面加载自动执行遗留排队消息、自动改写模型/权限（第一轮）。
2. F27：切回运行中会话 UI 失联（无法停止、状态撒谎、事件断流）。
3. B-resume-1：同 run 续跑 catch-22（schema 与错误信息互相矛盾）。
4. B-memory-1：记忆形成/召回 100% 降级且无健康暴露。
5. B-memory-2：记忆 item 端点对集合端点返回的 ID 一律 404。

**P1**
6. B-fork-1：fork 自动重放 + 坏模型继承（每次 fork 烧一次失败调用）。
7. F25：侧栏会话列表不自动刷新。
8. F18/F19/F20：Inspector 整页不稳定 / 事件行无反应 / Inspect 反向收起。
9. F8+F9（第一轮）：run 失败无内联错误与重试入口。
10. F10+F23：权限对话框 a11y/选中态/自动关闭/档名回显。

**P2/P3**：F21 键盘 resize、F22 假分组头、F24 令牌 popout 时序、B-memory-3 创建通道、B-model-1 错误枚举同步、F17 Cmd+K 搜索。

---

## 十、第二轮/三轮证据摘要（可复跑）

- fork 重放：`POST /api/sessions/80fa7176-…/forks {"from_seq":16}` → 子会话 88109bad 事件 seq 2 run/started(qwen3.8-27b) → seq 4 model/failed → seq 6 model/changed → seq 13 run/completed(final_text "2")。
- resume catch-22：`POST /api/sessions/{id}/resume {"run_id":"6d514ad5-…"}` 与 `{"run_id":…,"resume_basis":"auto","expected_version":1}` 均 422 同一条错误；`GET /openapi.json` ResumeRequest 无 expected_version。
- 记忆降级：`GET /api/sessions/{id}/events` 过滤 `type=memory/degraded`——formation provider_error（多会话 0 成功）、recall retrieval_timeout。
- 记忆 404：`GET /api/memories?limit=200` → id `f2162c12-…` 在列；`GET /api/memories/f2162c12-…` 与 `/versions` → 404。
- 切回失联：UI 与 `GET /api/sessions/94bc3fcf-…/events` 对拍（后端 seq 116 tool/call 进行中，UI 头部「已完成」、无 composer-stop、done2 不渲染）；刷新后 done2 出现、token 10,003→10,608。
- XSS：会话 a84b54a8 seq 1 user/message 含完整攻击串，`document.querySelectorAll('main script').length === 0`，气泡 textContent 为转义文本。
- 最终状态核查：memories 4 条原样；memory-settings 原样；94bc3fcf 权限已还原 workspace-write；控制台 0 错误。
