# Agent Harness Inspector — 第四轮暴力测试文档

> 测试日期：2026-09-28。测试工具：真实浏览器驱动（CDP——真实点击/键入/DOM 快照，经
> browser-use In-App Browser；说明见 §八方法学）+ HTTP 直打后端 API（黑盒）。
> 约束：**未修改任何代码**；未删除/修改用户真实数据（记忆只读、真实会话不删）；
> `.env` 值零泄漏（只列过 key 名）。
> 问题逐条记录于 `GUI_BUG_LIST_2026-09-28.md`（本轮新发现 E1 / B28-B31 / B33-B35）；
> 前三轮报告见 `GUI_TEST_FINDINGS_2026-09-27.md`。

## 一、测试目标

在三轮测试（功能遍历 → 前端暴力 → 后端暴力）基础上补齐尚未覆盖的功能面，并对用户点名强调的
维度做专项测试：记忆、fork、断点恢复、**长任务、稳定性、响应速度/延迟**，以及其他未提及维度
（归档、项目、工作区文件/git、artifact、交互式审批、命令面板、删除/重建等）。

## 二、环境（重要：端口改道的由来）

- 回到项目时前端(:5173)/后端(:8000)均已停止，且 **:8000 被用户另一个 uvicorn 应用占用**
  （auth/news/research 路由，需凭据）。`dev.sh` 与 `web/vite.config.ts:12` 均硬编码 8000/5173，
  没有任何环境变量覆盖手段 → 记为 **E1**。
- 本次测试的零侵入恢复：后端起 `127.0.0.1:8001`（`uv run uvicorn agent_harness.web.app:create_prod_app`），
  前端用**仓库外**临时配置 `D:\tmp\vite.test.config.ts`（root 指向 `web/`，代理 `/api → 8001`，
  `ws: true`）起 :5175。仓库文件零改动。

## 三、覆盖矩阵（✅=本轮完成，🔁=前三轮已覆盖，⏸=未测+原因）

| 区域 | 端点/控件 | 状态 | 关键证据 |
| --- | --- | --- | --- |
| 后端-会话 | 列表/创建/事件流/queue | ✅ | 95 会话；`task:null` 422（B28）；事件流 seq 对拍贯穿全程 |
| 后端-项目 | projects CRUD、项目下建会话、加入项目 | ✅ | 加入不存在项目 409、前端内联报错正常 |
| 后端-工作区 | file/files/git status/git diff | ✅ | 真实仓库文件树可读；路径越界防护见第三轮 |
| 后端-artifact | 列表 + `/artifacts/{id}` | ✅ | 86.4 KB 大 artifact 正常取回；三态设计（见正面发现） |
| 后端-budget/context | budget、context-usage | ✅ | 读写正常，数值与会话事件一致 |
| 后端-归档 | archive POST/DELETE、include_archived | ✅ | 归档→隐藏→恢复→再现 全链路 UI+API 对拍 |
| 后端-模型切换 | /model 有效值切换 | ✅ | mimo/glm/qwen 目录与切换正常（第三轮 B-model-1 残留已闭环） |
| 后端-审批 | 审批生命周期 | ✅ | 300s 超时 fail-closed：`tool/approval-requested`→`permission/resolved(deny)`→`tool/result(拒绝说明)`；但发现 B33 |
| 后端-取消/恢复 | /cancel、/recover | ✅ | cancel 闭环（run/failed(cancelled)）；recover 只回填不落终态（B33 第 7 条） |
| 后端-记忆 | formation/recall/embeddings | ✅ | formation 401 全挂（B29）；recall retrieval_timeout；siliconflow embeddings 200 |
| 后端-延迟 | 关键端点计时 | ✅ | 见 §六 |
| 前端-命令面板 | Ctrl+K | ✅ | 开关/过滤/↑↓循环/Enter 执行/空态/焦点（B30） |
| 前端-会话侧栏 | 选中/快速切换/大会话 | ✅ | 连续 5 次 250ms 间隔切换无错误；690/2477 事件、228k tok 会话切换 header 284ms |
| 前端-会话菜单 | 分叉/归档/删除/重命名 | ✅ | fork 实测（子会话自动跳转，B-fork-1 语义）；归档往返；删除对话框安全文案 |
| 前端-项目控件 | 新建/删除/加入项目 | ✅ | 删除对话框不自动关（B31）；409 内联提示 |
| 前端-审批卡 | 失效卡/活跃卡/断连行为 | ✅ | 失效卡 disabled+解释文案；断连后卡可点击但点击静默无效（B34） |
| 前端-Composer | 发送/排队/Esc 停止 | ✅ | 空输入禁用；流式中「Esc 停止」提示+中断闭环；取消后「已取消」 |
| 前端-Inspector 面板 | Timeline/Overview/Changes/Terminal/Artifacts | ✅ | 改动面板（文件列表+diff 前后对比+行选中）；输出面板（只读明示+exit 0+stdout）；ArtifactViewer 就地展开 |
| 前端-记忆面板 | 记忆管理浮层 | ✅ | 筛选器/范围切换/空态如实/预览删除空态显示「0 条记忆」 |
| 长任务 | 长流式+中断 | ✅ | 数到 300：流式渲染、token 增长、Esc 中断成功（B35 计数口径问题） |
| 稳定性 | 断连/快速操作/重复开关 | ✅ | WS 断连冻结（B34）；僵尸审批变砖（B33）；快速切换 PASS |
| 前端-diff | DiffBlock | ✅ | 改动面板 diff 前后对比渲染正确 |
| MCP/Skills/Knowledge | 能力页深测 | ⏸ | capabilities 枚举已验证（冷启动 4.35s 已记）；逐能力深测需真实外部服务 |
| eval/Langfuse | 观测面 | ⏸ | 本地未部署 Langfuse（第三轮已确认故障隔离正常） |
| SubAgent 委派 UI | DelegationNode | ⏸ | 样本会话无委派事件（组件存在、无数据驱动场景） |
| 移动端/窄屏 | 响应式 | ⏸ | 桌面 IAB 视口固定 |

## 四、专项测试记录

### 4.1 交互式审批全流程（本轮核心）

1. 发送「用 bash 执行 echo ui-approval-test」→ 后端 seq 183 `tool/call(bash)` + seq 184
   `tool/approval-requested`（danger 工具在 workspace-write 策略下需审批，卡面明示
   「工具授权级别为 danger，当前策略为 workspace-write」）。
2. 审批卡 UI：活跃卡「需要审批」带可用「批准 Ctrl+⏎ / 拒绝 Ctrl+⌫」；历史卡「审批已失效」
   带禁用按钮 + 解释「该审批已失效——所在运行已结束或服务已重启，决策无法再提交」。
3. 等待期间浏览器页签后台数分钟 → **WS 断连**（B34）；审批 300s 超时被后端自动 deny
   （fail-closed，日志时间戳 11:12:25.986 恰好 = requested+300s），模型如实向用户报告
   「审批超时（300 秒内无决策），按 fail-closed 策略拒绝」。
4. 断连期间点击「批准」→ **静默无效**（后端事件数不变、无请求记录、UI 无报错）——B34。
5. 刷新后：已决议审批不再渲染卡片（正确）；但运行因 B33 永不落终态，头部永远「思考中」。

### 4.2 断点恢复 / 取消

- Esc 中断：流式中有效——`POST /cancel 200` → seq 261 `run/failed(reason=cancelled)` →
  头部「已取消」。流式停止后（悬挂态）Esc 无效（streaming=false 时处理器未注册，B33 第 7 条）。
- 「恢复会话」按钮：正确检测「缺 run 终态」，但 recover 只回填悬空项 + 落 `session/resumed`，
  **不写 run 终态** → 对 B33 卡死的会话无效，横幅自述「仍缺 run 终态，可重试」但重试永远同结果。

### 4.3 长任务与稳定性

- 长流式（数到 300）：流式渲染正常、token 计数增长、中断闭环；刷新前后计数 10,608→21,400
  凭空翻倍（B35）。
- 快速连续切换：5 个会话 250ms 间隔连点 → 无错误横幅、终态一致。
- 大会话：690 事件/2477 事件/228,165 tok 三个大会话切换 header 284ms，事件拉取 ~100ms。
- WS 断连：重试 3 次失败后永久放弃，UI 冻结（B34）。

### 4.4 记忆（第三轮 B29 的本轮补充证据）

- 日志链完整：`siliconflow embeddings 200` → `senseaudio (glm-5.3-flash) 401` →
  `memory/degraded (formation provider_error)`——每次运行后记忆形成必挂。
- 记忆管理浮层：筛选器、全局/项目范围切换、刷新、关闭均正常；全局与项目范围均为空
  （与 formation 全挂一致，UI 如实反映）；「预览删除」空态显示「将永久删除全局范围内 0 条记忆」。

## 五、正面发现（值得保持的设计）

1. **审批 fail-closed 超时**：300s 无决策自动拒绝 + 聊天内明说原因 + `tool/result` 如实记录。
2. **Esc 停止闭环**：POST /cancel → run/failed(cancelled) → UI「已取消」，取消不经过完成闸门。
3. **删除/破坏性操作的沟通**：删除项目的软删除说明、影响范围三连、按钮文案精确描述后果。
4. **ArtifactViewer 三态**：加载/分因错误（gone/no-storage/通用，含后端 detail 原文与机读码）/
   内容就绪；内容按需取、不写回会话状态（不变量 #22 的正确落地）。
5. **输出面板诚实**：明示「只读：本项目命令为一次性执行，无交互终端」，不假装可交互。
6. **记忆面板提示**：「编辑会生成新版本；删除不可恢复」先说清楚再动手。
7. **归档/恢复全链路**：UI 与后端状态一致，`include_archived` 语义正确。

## 六、延迟与性能读数

| 项 | 读数 | 说明 |
| --- | --- | --- |
| /api/sessions?limit=200&include_archived=true | ~150 ms | 95 会话全量（扣除 curl+python 进程开销后估计） |
| /api/sessions/{id}/events（2477 事件） | ~60-100 ms | 大会话事件全量 |
| 静态目录（models/permission-modes/agent-profiles/reasoning-efforts/projects） | ~30-50 ms | 温态 |
| /api/capabilities 冷启动 | **4.35 s** | 首次能力扫描；若在首屏 await 会拖慢可用时间 |
| UI 会话切换（690/2477 事件） | header 284 ms | 会话数据+头部就位 |
| 审批超时 | 恰好 300 s | 11:07:25.969 → 11:12:25.986（机器落盘对拍） |
| WS 断连检测 | 数分钟级 | 页签后台后 stalled，重试 3 次失败（B34） |

## 七、遗留未测与风险

- MCP/Skills/Knowledge 逐能力深测、eval/Langfuse 面、SubAgent 委派 UI、窄屏布局（见矩阵 ⏸ 行）。
- 「拒绝」按钮未在活跃卡上实测（审批在点击前已被超时 deny）；拒绝与批准共用同一提交通道，
  风险低但语义未直接验证。
- B33 使测试会话 `94bc3fcf` 永久卡死——**保留作为证据**（含完整 seq 链），未做修复
  （修复需改事件存储或代码，均超出本次授权）。

## 八、方法学注记（诚实记录）

1. **浏览器驱动**：本环境的 chrome-devtools MCP 工具不在工具集内；改用 browser-use
   In-App Browser（同样经 CDP 驱动真实 Chromium：真实 click/fill/DOM 快照）。差异点：
   IAB 的 `locator.press()` 键盘事件**不落地**（用 `window.__keylog` 记录器实证为空）——
   所有键盘类测试（Ctrl+K、Esc、↑↓、Enter）改用合成 `KeyboardEvent`（`bubbles:true`，
   dispatch 到 window/元素），并先用记录器验证合成路径确实到达页面处理器。文本输入用
   Playwright `fill()`（React 受控组件对合成 input 事件不响应，实测踩过）。
2. **GBK 控制台**：Windows Git Bash 控制台会把 UTF-8 中文显示成乱码；中文请求体一律
   落盘 JSON 文件再 `curl --data-binary "@file"`，中文校验用码点比较不看控制台显示。
3. **跨轮次环境差**：本轮 API 全部直打 8001（原因见 §二/E1）；openapi 枚举在 8001 上
   重新做过，47 条 `/api` 路由。
4. **测量口径**：端点计时含 curl+python 子进程启动开销（Windows ~150ms/次），表中已注明
   为估计值；审批 300s 与事件 seq 为机器读数，无人工抄录。
5. **数据边界**：只动自建测试会话（94bc3fcf / a84b54a8 / 88109bad / a8f6c850 / 80fa7176 等）；
   用户真实记忆只读（预览删除点了取消）；未提交「添加供应商」表单（会污染全局
   model-providers.json）；未杀用户占 8000 的另一个应用。

## 九、结论

第四轮暴力测试在三轮基础上把交互式审批、取消/恢复、Inspector 面板（文件改动/输出/artifact）、
记忆面板、命令面板、长任务、稳定性与延迟全部实测闭环。**本轮最重要的发现是 B33（P0）**：
一次「审批等待期间服务重启」即可通过「僵尸审批 + 会话级完成闸门 + 零写入拒绝臂」的组合把
会话永久打砖，用户侧表现为无限「思考中」、每条新消息白烧配额、全部逃生通道失效且原因不可见。
其次是 B34（断连冻结 + 失效审批卡静默无效）。其余为契约矛盾（B28）、可观测缺口（B29）、
一致性/体验问题（B30/B31/B35）与环境脆弱性（E1）。问题明细、依据与成熟产品对照见
`GUI_BUG_LIST_2026-09-28.md`。
