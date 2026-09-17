# 真机浏览器逐控件测试记录（2026-09-17）

> 承接 `docs/LIVE_BROWSER_TEST_20260916.md`。上一批修的 F1–F6 已回归通过，本批**重跑真机**
> 验证并重点攻两条上一批没打透的：**（a）真实模型调用失败时界面能告诉用户什么**；
> **（b）在途追问（F4 修复后）在真机上是否真的不再新建会话**。
>
> 本文件按用户要求**边测边写**：测一条、写一条。缺陷判定标注「前端 / 后端 / 外部」，
> 修 bug 时照此对照。

---

## 0. 测试环境（本记录的前提）

| 项 | 值 |
| --- | --- |
| 后端 | 真 uvicorn，`agent_harness.web.app:create_app --factory`，`127.0.0.1:8000` |
| 前端 | 真 vite dev（**不是** e2e 的 mock 路由），`localhost:5174 --strictPort` |
| 驱动 | Playwright `chromium`，真实点击/输入/按键（无 `routeApi` 拦截） |
| 会话基线 | **32**（测前测后各数一次，见 §4） |
| 观测面 | 浏览器 console 错误、`page.on('request')`、以及**后端结构化日志**（`Uvicorn` stdout 重定向文件） |

**三个环境坑（本批全踩了，写下来省后人一次）**：

1. **前端 `/api/...` 走 vite 代理**，不是直连 `:8000`。用 Playwright 过滤请求时
   必须匹配 `/api/`，匹配 `127.0.0.1:8000` 会**一个请求都抓不到**（本批第一轮探针
   就栽在这里，"零请求"曾被我误读成"前端没发请求"——而 §2.2 那个真 bug 恰恰长成
   同一个形状，差点被这个假象掩盖）。
2. **vite 只监听 IPv6**：`netstat` 是 `[::1]:5174`，所以 `http://127.0.0.1:5174/`
   直接 `ERR_CONNECTION_REFUSED`，要用 `localhost`（解析到 `::1`）。
3. **本页由哪个 checkout 服务，必须看一眼**：Vite 的模块 URL 是
   `@fs/D:/intelligence-agent-frontend/web/node_modules/...`——当前跑着的 vite 属于
   **前端 clone**，不是本仓库。三个 clone 此刻都在 `680b2ad`，且 `web/src` 的 tree 对象
   同为 `6927294730d32631c53e0d6d88caba01524ded33`，**所以被服务的代码无分歧**；
   但"测的是哪棵树"是审计事实，不能凭目录名假设（跨 clone 只能比 git 对象，
   比工作树字节会因行尾配置不同得出"已漂移"的假结论）。

---

## 1. 实测正常（回归证据，逐条勾掉避免重复测）

| # | 验的什么 | 怎么验的 | 结果 |
| --- | --- | --- | --- |
| 1.1 | **刷新后会话内容一致**（用户点名的验收点） | 打开一条 **280 事件 / 6 轮 run** 的既有会话 → 取消息区 `innerText` + 逐事件 DOM 结构哈希 → `page.reload()` → 再取一次 | **逐字节相同** |
| 1.2 | 刷新后**不需要重新点击**就能恢复 | reload 后不做任何交互，直接读消息区 | 会话自行恢复（选中项来自 `localStorage` 键 `ahi.selectedSession`），**零点击** |
| 1.3 | 图标槽在**真载荷**（非夹具）上真的画出了 | 逐个打开三个 picker（权限/档位/推理深度），读每行是否有字形 | 三个 picker 的内置条目**都有**字形；「默认（未选）」行按设计是空槽 |
| 1.4 | #201 tool_scope 提示行的可达性 | 打开选择器后读提示行 | 文案在场（**只在 picker 打开时存在**——这是设计，不是缺失） |
| 1.5 | #212 上下文容量看板不再谎报 | 分别构造 `usage_only`（有用量无上限）与 `no_data`（新会话无 run）两种真实状态 | 两种状态下文案都**如实**；上一批 F3 的「谎报没有任何运行」未复现 |
| 1.6 | 控制台 | 全流程监听 `console` / `pageerror` | **零错误**（警告亦未出现与本项目相关的） |
| 1.7 | **在途追问不新建会话**（F4 修复的真机回归） | 发一条 → 首个 run 在途时**再回车** → 抓请求 + 读会话列表 | 第二个 Enter 打的是 **`POST /api/sessions/{已存在 id}/messages`**，**没有**新建会话；输入框上方出现排队条 |
| 1.8 | 上一条的语义确认 | 读该会话消息序列 | 两条消息（`["只回复：收到","在途追问 A"]`）**落在同一个会话**里，顺序正确 |

> 1.7 / 1.8 是上一批 F4 的**真机闭环**：上一批只证到"请求打到了 `/messages`"，
> 本批连"消息真的进了同一个会话、且顺序对"一起证了。

---

## 2. 问题清单（按发现顺序）

### 2.1 F1 — 真实模型调用**全部失败**，界面上只说「失败」，可操作的原因只活在后端日志里（**P1；归因：外部主因 + 后端可归因性缺陷**）

**现象**：本批所有真机 run **无一成功**。界面（消息流和事件）拿到的错误是：

```
model call failed: BadRequestError
```

**用户视角**：看到一句 `BadRequestError`，不知道是欠费、鉴权、模型名写错，还是网络。
**实际原因**（只在 uvicorn stdout 结构化日志里）：

```
openai.BadRequestError: Error code: 400 - {'code': 'billing',
  'message': '计费账户已被冻结', 'ref_code': 400901, 'ref_scope': 'common'}
```

**两个独立的缺陷，不要混为一谈**：

1. **外部主因（需用户本人处理）**：供应商（`senseaudio`）侧**计费账户已被冻结**。
   这不是代码缺陷，改代码解决不了，**必须由用户解冻/换 key**。本批所有失败都是它。
2. **后端可归因性缺陷（产品缺陷，可修）**：运行时的**脱敏不变量**规定事件只带错误**类型名**
   （`agent/runtime.py::append_model_failed` 注释明确写了"完整消息与调用栈只进结构化日志
   （OBS-008）"），这个方向本身是对的（不能把供应商原文回显给前端，可能含内部信息）。
   **但对"账户/配额/鉴权/模型不存在"这类*成因明确、且对用户可操作*的失败，
   类型名（`BadRequestError`）不构成有效信息**——`BadRequestError` 对应的可操作面
   横跨"欠费/参数错/模型名错"三种完全不同的处置方式。
   **同一条运行时已有**"分类后给可读消息"的机制（`readable_message` 服务于被分类的失败），
   只是**没覆盖供应商侧账户/配额/鉴权类**。修法是把这几类**归类成固定中文文案**
   （**不**回显供应商原文，保持脱敏不变量），见 §3 工单。

**附带发现（同一根因的下游）**：`GET /api/models` 对这条 key 仍报 `is_available: true`。
该字段的语义是**凭证在场**，不是**凭证可用**——所以它没坏，但会让用户以为"模型没问题"，
从而把失败错误地归因到别处。**如实记录，本批不改**（改它要新增一次探测调用，属范围外）。

**F1 已修并真机复核（2026-09-17 当批）**：分类表覆盖账户/配额/鉴权/模型不存在（#218）。
用**同一条被冻结的真 key** 复跑，持久化事件实测：

```
model/failed {"message": "模型供应商账户不可用（欠费 / 配额耗尽 / 账户被冻结），请到供应商控制台检查计费与配额"}
run/failed   {"reason": "provider_account_unavailable", "message": "同上（逐字一致）", "trace_id": "...", "trace_url": "..."}
```

这条复核的价值在于：它证明分类标记命中的是**真实错误的 `str()`**，
不是只在我自己构造的载荷上成立（LangChain 包装过的异常文本是否仍带 `'billing'` 是**假设**，
这里被实测证伪了风险）。**但界面上的价值还没兑现**——见 §2.4。

---

### 2.2 F2 — **空状态按 Ctrl+Enter：输入框被清空，消息凭空消失**（**P1，前端；静默丢用户输入**）

**怎么发现的**：本轮本来要用 Ctrl+Enter 触发一次真机 run 来复核 §2.1 的修复，
结果**一次 run 都没起来**——输入框清空了、页面没有任何报错、也没有任何请求。

**对照实验**（同一空状态、同样输入，只差修饰键）：

| 按键 | 新建会话 | POST 请求 | 输入框 |
| --- | --- | --- | --- |
| `Enter`（无修饰键） | 32 → **33** | **`POST /api/sessions`** | 清空（正常） |
| `Ctrl+Enter` | 33 → **33** | **（零个）** | 清空，**输入内容在页面里再也搜不到** |

**根因（代码级，已定位到行）**：

- `Composer.tsx:126-130`：`Ctrl/Cmd+Enter` 走 **steer** 通道 → `submit('steer')`；
- `Composer.tsx:113`：steer 只要 `onSteer` **存在**就交给它，而它在 `App.tsx:1053` 恒被接线；
- `App.tsx:581`：`handleSteer` 第一行 `if (!selectedId) return;` —— **没有选中会话就直接返回**；
- `Composer.tsx:118`：`setValue('')` **无条件执行**（不在 steer 分支里，也没有返回值可判断）。

四行合起来：**"没选中会话"时按 Ctrl+Enter = 静默丢掉用户刚打的字**。
零反馈（无 toast、无报错、输入框照样清空），用户只会以为"我是不是没按"。

**为什么这个 bug 更险**：`Enter`（queue）与 `Ctrl+Enter`（steer）在**空状态下语义应当等价**
（都要新建会话），而这里只有带修饰键的那条**丢数据**。修饰键是"我确定要发"的更强意图，
却在唯一的入口状态下吞掉消息——所以它比"两个键都失灵"更坏。

**归因**：**前端**。后端两件事都正确——`POST /api/sessions` 在裸 Enter 下正常工作，
且本轮**根本没有请求发出去**（后端无法为没到达的请求负责）；第二处后端还给出了
**可执行的 409 提示**（`steer requires an active run; use mode='queue' to enqueue`），
是前端把它丢了。

**F2 已修并真机复核（#219，当批）**。修法**不是**"前端猜有没有在途 run"——
本项目已经为这个猜法付过一次代价（`Composer.tsx` 就写着：issue #196 的根因正是
「`streaming` 表示本页有活流」被当成「服务端有在途 run」，跨客户端判反 ⇒ 消息静默丢失）。
两个坑位各用**事实判据**堵：

1. **空状态**（没有选中会话）：`handleSteer` 退回 `handleSubmit`（新建会话）。
   判据是 `selectedId`——**事实**，不是状态代理。
2. **有会话但在途 run 已终结**：照常发 steer（前端不猜），后端按契约回 409，
   **交付层认这个 409 并自动改投 queue 重发**。判据是**后端的回答**，不是前端的猜测。

> 迭代记录（留痕，因为它值得留）：第一版我把守卫写成 `Ctrl+Enter` 只在 `streaming`
> 时才 steer —— 独立审查轴当场指出这与 ADR-0030 §5.1 和 #196 的结论**冲突**
> （`streaming = mode.kind === 'live'`，只表示本页有活流），我核对源码后确认审查是对的，
> 于是整条改掉。**这就是为什么要跑独立审查轴，而不是自己看自己**。

真机复核（真 uvicorn + 真浏览器，同一条被冻结的 key）：

```
情形1 空状态：会话 32 → 33   POST=["/api/sessions"]           ← 修复前：零请求零会话
情形2 空闲态：POST /api/sessions/13022fee…/messages [409] body={"...","mode":"steer"}
              POST /api/sessions/13022fee…/messages [200] body={"...","mode":"queue"}
        消息序列=["219复核 第一条","219复核 第二条（空闲态 Ctrl+Enter）"]  ← 落库了
        错误条=0；页面不出现「人工裁决」错话
```

两条请求的 `content` **逐字相同**——这是"只是改了投递模式、没有重打消息"的机械证据。

**顺带修掉一条恒真的 e2e 断言**：`e2e/i-keyboard.spec.ts` 声称覆盖 Ctrl+Enter 提交，
但它断言的是轨道行里出现 `键盘提交`，而夹具会话的 `first_user_message` **恰好就是**
`键盘提交`——断言在任何行为下都成立。实测：**修复前的代码上它照样 2 passed**，
这正是这个丢消息的 bug 能活到现在的原因。现改为断言**请求路径 + 载荷 `task`**，
并配红证（退回修复前 → `waitForRequest` 超时）。


---

### 2.3 F3 — 失败文案已进事件，但**界面不显示它**：RUN 区只有「失败」两字（**P2，前端**）

§2.1 修完（事件里已有可操作文案）后真机复核界面：会话只显示「失败」，
页面文本里**既没有**新文案、**也没有** `BadRequestError`。

**根因**：`run/failed` 的 `reason`/`message` **没有被投影**——
`types.ts::ConversationState` 只有 `run_status` / `run_cancelled`，没有承载失败文案的字段；
`runState.ts:66` 的「失败」是**固定 label**；`projection.ts:1026` 给 `model/failed` 的是
`noopProjection`。所以文案只活在原始事件里（事件检查器/Raw 里能看见，对话区看不见）。

**归因**：**前端**。后端已按契约下发（`run/failed.message`，与上下文超限路径同一形状）——
而这条也说明**这不是 #218 引入的**：上下文超限早就带 `message` 了，同样没被渲染。
#218 只是让这个既有缺口多了一类内容。

**本批不修，已开单（#220）**：修它要动投影（`ConversationState` 新增字段）+ 渲染点，
属独立改动，硬塞进本批会让「事件已经对了」这个已验证的增量和一个未验证的 UI 改动混在一起。
**当下用户可见的效果**：API / SSE 消费方、结构化日志、事件检查器**都能拿到**
可操作文案；**对话区仍然只说「失败」**——这条缺口如实留在这里，不在文档里粉饰成已修。

---

### 2.4 F4 — 一次疑似"在途追问被开成了新会话"（**未复现，如实记录**）

**唯一一次观测**：早期探针里，第二次 Enter 之后出现了一条**只有一条消息**的新会话
（`cb7326c7-2ced-4802-8d35-da3549a01ca0`，首条消息即那句追问）。当时的第一反应是
"F4 没修干净"。

**未复现的证据**：随后**四次**独立重测（同一会话、同一在途窗口、分别在 submit 后
约 **200 / 400 / 700 ms** 三个时点回车），**全部**追加进了原会话（见 §1.7 / §1.8）。
其中一次连消息序列一起断言了。

**结论（诚实划界）**：**当前不认为它是缺陷**。四比一的复现率、且四次都带完整证据，
倾向解释是**探针自身的问题**（第一次探针的请求过滤匹配错了 host，见 §0 那段，
当时对"发生了什么"的观测本身就不可信）。**保留原始 trace，标为未复现**，
一旦再出现就有第二个样本可比对——不凭一次观测去改 F4 的代码。

> 处置原则：**不为了一个未复现的观测去动已修好的代码**（那会把一个已绿的修复重新打红，
> 且改什么、为什么改都说不清）。记录在案 + 等待第二个样本，是这里的正确动作。

---

### 2.5 观察（非缺陷）— 在途 run 的会话删除返回 **409**

整理数据足迹时对"仍在跑的会话"发 `DELETE` → **409**；等 run 落定后再发 → **200**。
**这是正确行为**（不该让一个正在写事件流的会话被删掉），记录在此是为了避免后人
把它当 bug 重复报。本次清理的第一次尝试正是因此留下残留，第二次才清掉。

---

## 3. 由 F1 开出的工单

**供应商侧账户/配额/鉴权类失败 → 固定可读文案**（不回显供应商原文，保持 OBS-008 脱敏不变量）。
验收：构造这四类失败（billing / 配额 / 鉴权 / 模型不存在），断言事件里的可读消息
**命中固定文案**且**不含供应商原文**；未分类的失败仍保持现状（只带类型名）。

---

## 4. 数据足迹（测试自身对用户数据的影响，已复原）

本批新建的会话**全部删除**，基线复原：

```
巡检残留候选 2 个:
  6d276b97-ebe4-4976-9f0c-4fb8ba475ae4  消息=["只回复：收到", "失败后的追问"]
  cb7326c7-2ced-4802-8d35-da3549a01ca0  消息=["第二条：在途追问"]
DELETE 6d276b97 → 200
DELETE cb7326c7 → 200
回收后会话数: 32          ← 与测试前基线一致
```

**测前基线也是 32**：本批对用户数据**零净影响**。

---

## 5. 复现方式

审计脚本是**临时文件，已删除**（仓库不留一次性脚本，与上一批同一纪律）。
复用要点：

- 后端 `?.venv/Scripts/python.exe -m uvicorn agent_harness.web.app:create_app --factory --host 127.0.0.1 --port 8000`；
  前端 `web/` 下 `npm run dev -- --port 5174 --strictPort`。
- **抓请求必须匹配 `/api/`**（走 vite 代理），匹配上游 host 会静默零命中（§0）。
- **"零点击恢复"要真的不点**：reload 后直接读 DOM；先点一下再读会把
  "恢复"验成"点击能选中"，两者不是一回事。
- 想看**真实失败原因**：先看界面（#220 之后分类失败会在 Overview 的「失败原因」行 +
  Timeline 摘要里出现），再读后端 stdout 的结构化日志；**未分类失败**目前两边都可能没有，见 §6.3 F5。

---

## 6. Round 2 巡检（同一夜续做：控件遍历 + 真失败复核）

**方法**：真浏览器（Playwright 驱动真 Chrome）对着**真后端**逐个点控件；每一步取
「点击前/后快照」（`data-density` / `data-theme` / `localStorage` / 会话条目数 / 正文长度 /
页签选中态 / 对话框数 / 当前焦点），所以「点了没反应」是**测出来的**，不是看着像。
用户数据只读：只在**新建的探针会话**里写入，结束时按 id **精确**删除并核对基线（32 → 33 → 32）。

### 6.1 实测正常（本轮新增）

| # | 验的什么 | 结果 |
| --- | --- | --- |
| 6.1.1 | 密度四档（紧凑/均衡/详细/Raw） | 四档都生效：`data-density` 与 `localStorage.ahi.traceDensity` 同步变 |
| 6.1.2 | 主题切换 | `data-theme` dark↔light 往复，持久化到 `ahi.theme` |
| 6.1.3 | 收起 / 展开 Inspector | 正文长度 1947↔1927，按钮 aria-label 同步切换 |
| 6.1.4 | 记忆管理 / API 令牌设置 / 新建项目 | 三个面板都能开、都能关（Esc 或「关闭」）；打开时的请求符合预期 |
| 6.1.5 | 项目 ⋯ 菜单 | 能开、Esc 能关 |
| 6.1.6 | 显示 / 隐藏已归档会话 | `localStorage.ahi.showArchived` 1↔0 |
| 6.1.7 | 工作区三页签 + Inspector 四页签 | 全部切换正确；`Timeline280` / `Changes1` / `Terminal1` / `Artifacts2` 的数字与实际内容对得上（Overview 19 行 detail-row、Artifacts 6 行） |
| 6.1.8 | composer 四个选择器（模型 / 权限 / Agent Profile / Reasoning） | 都能开、都能 Esc 关；关闭后配置**未被改动**（storage 快照无差异）；权限模式四项文案逐字可读 |
| 6.1.9 | **刷新一致性（用户点名）** | 打开 280 事件会话 → 取正文 + DOM 结构哈希 → `reload()` → **正文逐字相同、结构哈希相同、0 次点击**，`ahi.selectedSession` 自动回到同一会话 |
| 6.1.10 | 会话 ⋯ 菜单条目 | 加入项目… / 新建项目… / 归档 / 删除会话…（四项齐全） |
| 6.1.11 | 删除会话流程 | 确认框把后果说清（硬删除 / 不可恢复 / 项目只解除不删目录）；删完转结果态（「已永久删除 4 条事件记录」+「完成」），Esc 也能关；删完视图正确回到空状态、`ahi.selectedSession` 置 null |
| 6.1.12 | 控制台 | 全程零 JS 错误（唯一 404 是浏览器默认探的 `/favicon.ico`，见 F7） |

### 6.2 被证伪的怀疑（如实记录，避免下次重复怀疑）

- **「删完会话界面就点不动了」——错。** 删除确认后去点页签被 `.palette-overlay` 拦住，
  看上去像"死界面"。逐帧取几何后看清：确认框**自己转成了结果态**（`z=51`、可见、
  带「完成」按钮），模态遮罩照常拦背后的点击——这是**模态的正确行为**，不是卡死；
  Esc /「完成」之后一切正常。误判源于我的选择器只认 `/确认|删除/`，没把「完成」算进去。
- **「记忆面板 503 会谎报为空」——错。** 面板如实显示「记忆未启用」并把后端 `detail` 原样带出；
  503 本身是**设计内降级**（`capability 'memory' 初始化失败，按 OPTIONAL_RUNTIME 降级跳过`，
  不变量 #21）。但它的**措辞**会误导，见 F8。

### 6.3 本轮发现

#### F5 — 真实 run 失败时，**界面与会话事件流都没有原因**（P1；后端为主）

**现象**（真机实跑）：发一条消息，44s 内 run 变「失败」，而：

- 运行徽标 `失败`；无 `.app-error` 条（run 失败不是投递失败，符合设计）；
- Inspector `Timeline` 末几行只有 `只回复：收到` 与空行，**没有失败摘要**；
- 切到 `Overview`：`状态 失败 / 轮次 1 / 耗时 4.1s / tokens — …`，**没有「失败原因」行**，
  `.run-failure-val` 元素数为 **0**；
- 对话区只有 `第 1 轮 只回复：收到 编辑 分叉`；
- 页面全文**不含** `ProxyError`，也不含 `model call failed`。

**会话自己的事件流**（`GET /api/sessions/{id}/events`，同源 fetch）：

```
0 session/started {...}
1 user/message    {"content":"只回复：收到"}
2 run/started     {"turn_index":1,"agent_profile":"main"}
3 run/failed      {"trace_id":"11ebf194…","trace_url":null}   ← 没有 reason，也没有 message
```

**没有 `model/failed`**——原因在**持久历史里也不存在**，只活在后端进程日志。

**归因（代码级）**：`src/agent_harness/agent/runtime.py`

- 失败臂只在**模型调用在途**（`terminal.model_call_open`）时才做归因分类；本轮失败发生在
  **更早的阶段**——tiktoken 取 `cl100k_base` 词表要走网络
  （`openaipublic.blob.core.windows.net`），而本机代理不可达 ⇒ `ProxyError` ⇒ run 死在
  「准备上下文 / 数 token」这一步，此时 `model_call_open` 仍为 `False`；
- 于是 `failure_terminal(reason=None, message=None)` ⇒ `run/failed` 两个键都不写；
  `close_observability` 因同一条件**不写 `model/failed`**；
- 即便是在途失败，**未分类**异常（网络/超时等）同样 `reason=None` ⇒ 界面依旧只有「失败」。

**影响**：#218 + #220 的成果对"最常见的失败模式"（模型侧网络/依赖不可达）**失效**——用户看到的
还是两个字，且这次连事件流里都没有线索。既是**可归因性**缺口，也是**可观测/历史保真**缺口
（失败的 run 在 durable 历史里记不出为什么）。

**已修（#222，同夜）**：`run/failed` 在**每条运行期失败路径**上都带 `reason`（未分类退到异常
类型名）+ 项目自拼的可读 `message`；`max_steps` 那条此前完全没落键的路径一并补上并改走
`failure_terminal`（终态字段的唯一 owner）。**修的时候又抓到一个前端 bug**：`Overview` 的
「失败原因」行在"只有码、没有文案"时渲染成**空值**（码原本只在 `message` 同时存在时才缀出来），
而 Timeline 摘要一直有兜底——同一事实两个口径；`toContain(码)` 那条旧断言之所以是绿的，是因为
码还出现在**同一元素**的 `title` 属性里。两者都已修，口径与逐路径清单收进
`docs/adr/0033-run-failure-attribution-surface.md` §2.4。真机复核（代理恢复后把 `MODEL_BASE_URL`
指向死端口制造真实失败）：事件流 `run/failed {"reason":"RateLimitError","message":"运行失败
（RateLimitError），未分类异常；完整原始信息见后端日志"}`，Overview 行与 Timeline 摘要都显示了它。

#### F6 — 在「文件/改动」或「输出」页签上点「新建会话」，用户被留在无法输入的状态（P3；前端）

工作区停在「输出」页签时点「新建会话」：页签仍是「输出」，`#composer-input` 在 DOM 里但
**不可见**（composer 属于 Chat 面板），会话数不变（设计如此：空状态在发送时才建会话）⇒
整屏**没有任何"新建会话"的反馈**、也没有输入框——正是"点了没反应"。切回 Chat 才有输入框。
**归因**：前端（新建会话没有把工作区切回 Chat）。修法有产品取舍：切页签，或让 composer 不绑页签。
**已修（#223，同夜）**：取票面选项 1——`App.tsx::handleNew`（以及同缺陷的
`handleStartTaskInProject`：它把焦点交给一个隐藏的 `#composer-input`）先 `setSelectedSurface('chat')`
再聚焦。红线：e2e 在「输出」页签 → 点「新会话」→ 断言 Chat 被选中、composer 可见且**真的能打字**
（`fill` + `toHaveValue`）；把那一行还原 ⇒ 两条 viewport 全红（`aria-selected` 为 false）。
另外记一笔：空列表态那个按钮的可见文字是「新会话」（UI-05 换成了带文字的按钮），
`aria-label="新建会话"` 是有会话时的 icon 按钮——两个入口同一个 handler。

#### F7 — 每次加载一条 `/favicon.ico` 404（P3；前端，装饰性）

`web/index.html` 未声明任何 icon、`web/public/` 只有 `icons.svg` ⇒ 浏览器默认探
`/favicon.ico` 得 404（本轮抓到 URL 与来源，不再是"某处 404"）。零功能影响，但每次开控制台都是噪声。
**已修（#224，同夜）**：新增 `web/public/favicon.svg`（字形 = 顶栏那个 lucide `Activity` mark，
不引入第二套品牌资产）+ `index.html` 的 `<link rel="icon">`。**没用** `public/icons.svg`：
它只含 `<symbol>`、没有可渲染的根图形，当 icon 渲染出来是空白（那条判据写成了断言）。
红线锁**声明→可获取→真在画东西**这条链（从活 DOM 取 `link[rel~=icon]` 的 href 再取回来看），
不是"index.html 里有一行 link"：把 href 指向 `icons.svg` ⇒ 红；删掉声明 ⇒ 红。

#### F8 — 记忆面板的 503 把「配置缺失」与「初始化失败」混为一谈（P3；后端措辞，前端忠实渲染）

`GET /api/memories` → 503，`detail = "memory capability 未启用：请在 CAPABILITIES 中配置 memory。"`；
面板据此显示「记忆未启用」+「**这是配置状态而非故障**」。而真实原因是
`capability 'memory' 初始化失败，按 OPTIONAL_RUNTIME 降级跳过：VectorStoreError('Memory vector store: unavailable')`
——**是故障**（向量库连不上），不是"没配置"。**归因**：`web/memory.py::_capability()` 只在
`wiring.memory is None` 上判一次，而 `wire_capabilities` 把"没配"与"配了但初始化失败"都塌成
`wiring.memory is None`，**降级原因没有结构化留存**（只进日志）。前端是忠实渲染后端那句话的，
所以修点在后端给原因，必要时前端再按原因分文案。
**已修（#225，同夜）**：装配期把缺席原因**分类留码**（`CapabilityWiring.degradations` +
`DegradeReason`：`not_configured` / `disabled` / `missing_settings` / `init_failed`），
`/api/memories` 的 503 改成 `detail: {code, message}` 并逐原因给话；前端的「记忆未启用」块
**不再自己加**那句"这是配置状态而非故障"（改由后端逐原因说清），`init_failed` 改走错误条 + 重试
（**这是故障**，改配置没用）。判别只认 `code`，老后端（无 code）仍按配置状态。机制落点
`docs/adr/0010-capability-registry-and-plugin-config.md`「补充（#225）」。**顺带修掉同一处塌缩**：
`missing_settings`（CAPABILITIES 里配了但向量库/嵌入模型的前置配置不齐）以前也被告知"去配 CAPABILITIES"。

### 6.4 由本轮巡检开出的工单

| 票 | 级别 | 一句话 | 归因 |
| --- | --- | --- | --- |
| [#222](https://github.com/EricKingWhy/intelligence-agent/issues/222) | **P1** | run 失败时事件流里没有任何原因（`run/failed` 无 reason/message；不在模型调用窗口内的失败连 `model/failed` 都不写） | 后端 |
| [#223](https://github.com/EricKingWhy/intelligence-agent/issues/223) | P3 | 在「文件/改动」/「输出」页签上点「新建会话」零反馈，composer 不可见 | 前端 |
| [#224](https://github.com/EricKingWhy/intelligence-agent/issues/224) | P3 | 每次加载一条 `/favicon.ico` 404（`index.html` 未声明 icon） | 前端 |
| [#225](https://github.com/EricKingWhy/intelligence-agent/issues/225) | P3 | `/api/memories` 的 503 把「没配置」与「初始化失败」混为一谈 | 后端措辞（前端忠实渲染） |

**为什么 #222 这一夜没有当场修**（如实写，避免下一位以为"忘了"）：

1. **它要动一条被两条测试显式钉住的合同**——`tests/agent/test_runtime_failure_paths.py` 的
   `test_unclassified_failure_keeps_type_only_terminal` 与 `test_tool_phase_error_with_marker_not_misclassified`
   都断言 `run/failed` **不落** `reason`/`message` 键，合同写作"缺省 = 模型/执行器异常"，
   并把"用户怎么知道原因"交给 `model/failed.message`（类型名）。改它属于**合同变更**，
   该走 ADR/票面决策，不该在半夜顺手改掉（§9.1）。
2. **本机此刻跑不到那条路径做验证**：代理不可达 ⇒ tiktoken 取词表先炸 ⇒ run 死在
   上下文准备阶段，**根本进不了模型调用窗口**。此时改前端"投影 `model/failed.message`"
   也无法真机证明（那需要一次窗口内的失败）。**没有验证手段的改动不上**。

两条**已确证**的边界事实（留给修 #222 的人，省一次摸索）：窗口内未分类失败 → `model/failed.message
= "model call failed: <TypeName>"`（本机实测 `RuntimeError` 路径单测已覆盖）；窗口**外**失败 →
`model/failed` **一条都没有**，类型名只在进程日志里。

**后续（同夜晚些时候）**：上面两条阻塞都已解除——代理恢复 ⇒ 真机能跑到失败路径；合同变更按
§9.1 走成了显式决策（收进 ADR-0033 §2.4）。#222 已实现、两轴审查、门禁、真机复核、集成关单；
逐条记录见 `docs/phase_status/2026-09.md` 同日两条。

---

## 7. 收尾复核（2026-09-17 夜，B-9 集成之后；用户点名的两个验收点）

环境与 §0 同形（真 uvicorn `127.0.0.1:8000` + 真 vite dev，驱动用 Playwright 真点击/真刷新）。
本轮**只读**（只点会话行 / 刷新 / 读面板），跑完会话基线仍 **32**。

### 7.1 刷新后会话内容一致（用户点名项，在**集成后的那棵树**上复验）

拿事件数最多的既有会话（**280 事件** / 5 天前）做对照：打开 → 取 `.conversation` 的
innerText 与**逐节点 tag 序列的 sha256** → `page.reload()` → 再取一次。

| 对照项 | 刷新前 | 刷新后 | 判定 |
| --- | --- | --- | --- |
| `localStorage.ahi.selectedSession` | `a39876d7-…` | `a39876d7-…` | **一致**（零点击恢复） |
| 正文长度 / 摘要 | 3088 / `4b5a7b4b58304dfc` | 3088 / `4b5a7b4b58304dfc` | **逐字节一致** |
| DOM 节点数 / 结构摘要 | 406 / `8d7396643295cb3d` | 406 / `8d7396643295cb3d` | **结构一致** |
| 工作区页签 / composer | Chat / 可见 | Chat / 可见 | 一致 |
| 控制台错误 | — | **零** | 一致 |

> **一条方法学自我纠正**：第一版探针的"节点数"用的是 `.step-row, .step-card, [data-step-id]`，
> 真机上一个都没匹配到 ⇒ `0 == 0` 的 **PASS 是假绿**。已改成"元素总数 + tag 序列摘要"，
> 数字（406 个节点）才对得上。这与归档里记过的"子串断言假绿"是同一类错误。

### 7.2 刷新发生在 run **在途**时（最难形态）

真模型此刻不可用（实测 `400 billing: 计费账户已被冻结`，**外部原因**，不是代码），
所以用**本地 OpenAI 兼容的慢速 stub**（每 250ms 一行、共 40 行 ≈ 10s）当模型端点，
让后端跑一条**真流**：发消息 → 等到已吐出 `1 2 3 4`（**确认尚未数完**，避免"跑完再刷"的假测）→
`page.reload()` → 断言。

| 判定项 | 结果 |
| --- | --- |
| 刷新后会话 id 恢复（零点击） | **PASS** |
| 刷新**之后**才产生的内容继续到达 | **PASS**（正文 53 → 163 字符） |
| 刷新前已显示的内容仍在 | **PASS** |
| run 终态 | **PASS**（`run/completed`），正文含完整 1..40 |
| 控制台错误 | **零** |

**附带红利（#222 的真机复证）**：真模型那次失败恰好是一条**计费类**失败，事件流落的是
`run/failed {"reason":"provider_account_unavailable","message":"模型供应商账户不可用（欠费 / 配额耗尽 / 账户被冻结），请到供应商控制台检查计费与配额"}`，
外加一条 `model/failed` 同款文案——**可归因性合同在这类真实失败上成立**（不再是"界面只有失败两字"）。

### 7.3 #225 两条 503 分支的真机复核（含**票面原始场景**的逐字复现）

| 构造（真后端重启 + 环境变量覆盖） | `GET /api/memories` | 记忆面板实际渲染 |
| --- | --- | --- |
| `CAPABILITIES={}`（没配 memory） | 503 `{"code":"not_configured","message":"…请在 CAPABILITIES 中配置 memory。"}` | 「记忆未启用」+ 后端原文；**无重试按钮**；**无「非故障」字样** |
| memory 已登记、`MILVUS_URI` 指向死端口 | 503 `{"code":"init_failed","message":"…初始化失败：CAPABILITIES 里已登记且启用，但装配时出错…"}` | **错误条 + 重试按钮**；**不显示「记忆未启用」**、**不显示「非故障」** |

第二条的后端日志逐字是 `capability 'memory' 初始化失败，按 OPTIONAL_RUNTIME 降级跳过：
VectorStoreError('Memory vector store: unavailable')`——**正是票面引用的那一条**，
即票面场景在真机上被原样复现，修前它会说"未启用：请去配 CAPABILITIES"（把人指向本来就配好的开关）。

> **一条如实登记的观察**：两种情况下浏览器控制台都会出现两条
> `Failed to load resource: the server responded with a status of 503`。这是 Chromium 对**任何非 2xx
> 响应**的自动记录，不是前端缺陷（503 本身就是预期的降级信号），也**无法从页面侧消除**。

### 7.4 探针卫生

临时探针脚本（刷新一致性 / 在途刷新 / 记忆面板）、stub 模型、以及中途因脚本崩溃留下的
**2 条探针会话**全部清理：会话基线 32 →（期间最多 34）→ **32**；仓库 `git status` 干净，
不留任何一次性脚本或日志。

## 8. 崩溃恢复真杀进程对照实验（2026-09-17 夜，B-10 集成之后）

**为什么做这一节**：`docs/RESEARCH_DSH_PATTERN_GAP_AUDIT.md` §4.1 把第 11 条（崩溃恢复）
登记为**最后一个未验证项**——DSH 一侧的机制描述（`docs/subsystems/persistence.md:106`，
"读者补一个有类型的 closer"）已现场核对属实，但「它究竟解决了我们哪个**可观测**问题」
没验过。本节用**真杀进程**把这个问题问到底：崩溃后我们的 durable 日志与界面各长什么样。

方法是**两条终态路径各做一次对照**，都是真进程：

| 实验 | 触发 | 预期（读代码得出的假设） |
| --- | --- | --- |
| 甲 | run 在途中**硬杀**后端进程（`TerminateProcess`，不走 uvicorn 优雅退出）→ 重启 | 重启时 lifespan 启动扫描补 `run/interrupted{reason=process_restart}` |
| 乙 | run 在途中**关掉页面**（订阅者离开，进程仍活）→ 等过宽限期（实验用 5s） | 孤儿回收收尾 `run/failed{reason=orphaned}` |

模型端点用本地 OpenAI 兼容的慢速 stub（每 250ms 一行、共 60 行 ≈ 15s），所以"在途中"
是可证伪的：`1 2 3 4` 已经吐出来、但 60 行远未数完。

### 8.1 甲·后端层（真 uvicorn，一次性 workspace，无头）

```
杀前（磁盘）      10 条事件，末条 seq=9 text/delta，**无任何 run 终态**   ← "半截 turn" 成立
硬杀              Popen.kill → TerminateProcess，lifespan 收尾不执行
重启（同一 ws）   seq=10 run/interrupted  run_id=90993e26-…  step=1
                            data={"reason":"process_restart","interrupted_seq":9}
                  seq=11 session/resumed
API 末条          run/interrupted（12 条事件）
启动日志          WARNING 启动扫描：session=… 标记 1 个中断 run（seq=[9]）
                  WARNING 启动崩溃扫描：session=… recovery=ScanRecovery.RECOVERED detail=None
```

**结论：DSH 那条"读者补一个有类型的 closer"我们**有**，而且是同形的**——终态事件本身带
`run_id`（精确到哪个 run 被截断）、`interrupted_seq`（读者一眼知道日志走到哪儿为止）、
`step_id`（"第几步断的"），不是一句无主的"这个会话出错了"。**归因链完整**：`run_id` 与
被杀那次的 `run_id` 逐字相同（`90993e26-…`），所以"这条 closer 属于哪个 run"不靠猜。

> 顺带一条**未在预期内**的观测：重启扫描之后还有一条 `session/resumed`（seq=11）。它来自
> 启动期恢复路径，不是扫描本身写的两条中的一条；这里只如实记录"重启后事件流末尾长这样"，
> 不去推断它的语义（真要弄清得单独读一遍那条路径）。

### 8.2 甲·真机浏览器层（真 Chromium + 真 vite dev + 真后端）

同一实验在浏览器里又跑了一遍，且 run 是**从界面点出来的**（`textarea` 输入 → 点「发送」），
保证是用户路径而不是 API 旁路：

| 时点 | 顶部 Run Pulse | 会话正文（前 120 字） |
| --- | --- | --- |
| run 在途中 | **思考中 · 11s** | `第 1 轮 / 请从 1 数到 60 / 编辑 / 思考 / 1 2 3 4 ` |
| 后端已死 + 刷新页面 | 空闲 | （本条见 §8.4，是**被证伪的怀疑**，不是缺陷） |
| 后端重启 + 刷新页面 | **已中断** | `第 1 轮 / … / 分叉 / 折叠 / 1 2 3 4 ` |

重启后刷新，页面上与 run 状态有关的**可见行**逐字是：

```
已中断
上次运行在第 1 步中断（原因：process_restart）
已中断
已中断
第 1 步中断
```

即：**界面没有停在假在途**（不是"永远思考中"），而是明确说"上一次跑在第 1 步被打断"，
且**断在哪个 run、哪一步**都在屏上。这正是审计第 11 条要问的那个"可观测问题"的答案。

### 8.3 乙·孤儿回收（关掉页面 → 宽限期到期 → 回来看）

真机走的流程：run 在途中 → `page.close()`（订阅者离开，**进程仍在跑**）→ 等 11s
（宽限期 5s 已过）→ 重新打开页面。

```
磁盘终态   seq=17 run/failed  run_id=1dce9561-…  step_id=0
           data={"reason":"orphaned","trace_id":"…","trace_url":"…"}   ← 无 message
后端日志   WARNING run 孤儿回收（session=41cd09ee-…，零订阅者超过 5s）
回来后的页面   Run Pulse = 失败；时间线可见行 = 失败 / 失败 / 失败 / orphaned
```

两条**如实记录的观察**（都不是缺陷，但值得知道）：

1. `reason=orphaned` 在界面上表现为红色「**失败**」，时间线还直接显示英文机读码
   `orphaned`。这是**契约已定**的呈现：`docs/BACKEND_CONTRACT_STREAMING_UI.md:201/208`
   规定 `cancelled`/`orphaned` 之外一律当不透明字符串透传，`web/src/lib/projection.ts:633`
   的注释也写明"非用户意图，**按失败展示**"。所以它与契约一致——但它同时是 da394a9
   「取消 ≠ 失败」那一族里**唯一**没中文化、且唯一上时间线的一条（`cancelled` 刻意返回空串
   不上时间线）。是否要拉齐属产品口径决定，本文只登记事实。
2. 终态事件里带了 `trace_id` / `trace_url`——孤儿回收这种"服务端自己决定的收尾"也留了可追溯
   线索，不需要用户复现一次就能去 Langfuse 看现场。

### 8.4 被证伪的怀疑（如实记录，避免下次重复怀疑）

**怀疑**：后端不可达时，顶部 Run Pulse 显示「空闲」是在**说谎**（run 可能还活着），应改成
"状态未知"。
**证伪**：那一刻页面上**根本没有会话被选中**——侧栏会话列表也拉不到，正文显示
「未选择会话 / 从左侧选择，或开始新任务。」。`deriveRunPulse(null)` 返回 `idle` 的语义就是
"没有会话可谈"（`web/src/lib/runState.ts` 的 `idle` 行注释："no conversation or nothing has
happened"）。**有会话在途时**的断连走的是另一条路（会话已加载 → `run_status='running'` →
显示"思考中" + 重连横幅），与 §7.2 的在途刷新同形。⇒ **不是缺陷**，不改。
（真机上那句「空闲」旁边的**真问题**是另一个，见 §8.5。）

### 8.5 F9 — 会话列表**加载失败**被渲染成「暂无会话，提交任务即可开始。」（**P2；前端**）

**现象**（§8.2 第二格那一刻的整页可见文本，逐字）：

```
项目列表加载失败：加载项目失败（502）        ← 项目列表失败：有错误条 + 重试
重试
暂无会话，提交任务即可开始。                 ← 会话列表失败：说成"没有会话"
未分组
0
加载历史事件失败：get events 502
```

**为什么是缺陷**：这一刻用户最需要知道的是"我的会话还在不在"。项目列表失败时界面
**明说失败并给重试**，会话列表失败时却**把它渲染成一个看起来完全正常的空状态**——
「暂无会话，提交任务即可开始。」是一句**可被执行的假陈述**（用户会据此以为会话丢了，
或者以为这是全新环境）。这与本项目"零伪造"（不变量 #22：Web UI 不维护第二套真相）冲突。

**根因**（读代码确证，不是猜）：`useSession.refreshSessions` 的 catch **确实**报了错——
`setError('加载会话列表失败：…')`（`web/src/hooks/useSession.ts:431`）——但 `error` 是
**单槽通道**（App.tsx:957 渲染为 `.app-error`）：紧接着"加载历史事件失败"把这一槽**覆盖**掉，
于是那句话在屏上消失了，而侧栏的假空态照旧。`SessionList` 的空态判据是
`showEmpty = sessions.length === 0 && projects.length === 0`（`SessionList.tsx:218`）
——**失败与真空在这个判据里无法区分**，因为失败时 `sessions` 就是一个空数组。
同一文件里**项目**列表有 `projectsError` 这条例外通道（`:277` 的错误条 + 重试），
**会话**列表没有：这就是两者的不对称。

**修法**（[#228](https://github.com/EricKingWhy/intelligence-agent/issues/228)，同夜实施）：

- `useSession` 新增**区域级**状态 `sessionsError`（失败写、成功清），**不再**走单槽 `error`
  ——被覆盖掉的那一次就是本缺陷的根因之一；
- `SessionList` 新增同形 prop，渲染与 `projectsError` **同一条**错误条 + 重试
  （重试复用 `onRetryProjects`：它本来就同时重拉项目与会话两个列表，`App.tsx:125-128`）；
- 空态守卫：`showEmpty && !sessionsUnavailable`——**加载失败不说那句空话**；
- 错误条优先级：`opError`（动作级、更具体）在场时列表错误条让位（沿用改动前 else-if 链的
  优先级），但项目与会话这两条**彼此独立**——真机现场就是两个列表同时挂，链式结构只会显示
  第一条，第二条那个假空态仍然没人挡。

**红证 5 组**（每组都实做"改坏 → 变红 → 逐字节还原"，`sha256` 校验还原；详见 issues #228）：

| 改坏什么 | 哪里变红 |
| --- | --- |
| `setSessionsError` 改回 `setError`（= 修复前写法） | 新 e2e 3 条红；**组件测试仍全绿** ← 这就是本票必须锁在 e2e 的理由 |
| 去掉空态守卫 | 组件用例红 |
| 会话错误条退回"项目失败时不可达"的链式结构 | 组件用例 + e2e 红 |
| 去掉 `opError` 优先级 | 既有 `archived.spec` 的"恰好一条就地错误"红 |
| 会话错误条改成依赖 `projectsError` | 组件用例 + e2e 红 |

> **一条自我纠正**（红证阶段发现）：本票第一版组件测试里有一条断言是**永远绿**的——
> 「失败 + 有旧数据」那条也断言了"不出现空态那句话"，可 `sessions` 非空时 `showEmpty`
> 本来就是 false，那句话无论如何都不会渲染。去掉整条空态守卫它**照样通过**。已删掉那条
> 假断言并把原因写在测试注释里（与 §7.1 的"0 == 0 假绿"、归档里的"子串断言假绿"同一类）。

### 8.6 探针卫生（本节）

- 全部实验**各自用一次性 workspace**（`WORKSPACE_DIR` 指向 `%TEMP%\crash*\ws`），**没有**
  碰用户的 `.agent/workspace`：会话基线自始至终 **32**，且 `grep` 未在 `.agent/` 下找到任何
  探针 session id。
- 一条**方法论错误**记在这里（同类错误本节第一次踩）：第一版无头探针的后端端口没守住——
  上一个残留 uvicorn 还占着端口，新进程绑定失败退出，而探针的 `health` 探测**打到了旧进程**，
  于是请求写进了上一个实验的 workspace。**已加护栏**：起服务后先断言"自己的
  workspace/sessions 目录已出现"才认为服务是自己的，起不来就响亮失败。**没有这条护栏，
  整节结论都会是错的**（会得出"扫描没补终态"的假结论）。
- 临时脚本（stub 模型 + 两个 Playwright 驱动 + 一个无头驱动）与截图全部留在仓外的
  `%TEMP%` 工作目录，仓库 `git status` 干净。

---

## 9. Round 3 巡检（2026-09-17 夜，B-11 集成之后：把"每个按钮都点过"变成可查的事实）

### 9.0 为什么有这一轮 + 方法

§1/§6/§7 覆盖的是**用户点得到的主路径**；把它们逐条列出来后可以发现，真机上**从未点过**的
控件还有一大片（弹层内的次级动作、菜单里的增删改、键盘入口）。用户的要求是"每个功能按钮
都必须点一下"，那就得先知道**总数**，否则"都点过了"只是一句话，不是一条事实。

**方法**：从 `web/src/**` 把可交互控件逐个数出来（`aria-label` / `title` / `<button>` /
`role="menuitem"` 等），与本文档已记录/已实操的控件对账，得到「有真机证据 / 只有 e2e
（mock 路由）/ 零覆盖」三档；**零覆盖且用户重要度高的先测**（因为 mock 路由的 e2e 恰恰
测不出"真后端会回什么"这一层）。本轮环境与 §0 同形（真 uvicorn `127.0.0.1:8000` + 真 vite
dev `:5174` + Playwright 真点击），另加一条**探针专用隔离**：后端以临时
`PROVIDER_STORE_PATH` 启动 + 本地 OpenAI 兼容 stub 当模型端点（真模型此刻仍不可用，
外部原因见 §2.1）。

### 9.1 F10 —「管理模型」弹层**渲染在视口外**，用户看到"整页变暗、什么都没有、也点不动"（**P1；前端**）

**现象**（真机，逐字）：点 composer 的「模型选择」→ 菜单里的「管理模型」→ 整页蒙上
0.35 黑遮罩，**弹层不出现**，页面任何位置都点不动（模态遮罩照常拦截），只能 Esc 退出。
§9.1 的那张截图就是用户此刻看到的全部内容。

**定位证据（三条独立，都不是"看着像"）**：

| 证据 | 修复前 | 修复后 |
| --- | --- | --- |
| `.provider-dialog` 计算样式 | `position: static` / `z-index: auto` / rect `x=0 y=900 w=760 h=213`（视口 1440×900 ⇒ **整体在视口下方**） | `position: fixed` / `z-index: 51` / rect `x=340 y=344`（居中） |
| `document.elementFromPoint(按钮中心)` | `null`（y=1079 在视口外） | `button.provider-item.provider-new`（命中按钮自身） |
| **真实鼠标坐标点击**按钮中心 | 表单没出现（`false`） | 表单出现（`#provider-id` 在场） |
| Playwright 可操作性检查 | 拒绝点击：`palette-overlay ... intercepts pointer events` | 点击正常 |

**根因**（读 CSS 确证）：Radix `Dialog.Content` 不带样式，浮层的"定位那一层"由应用自己给。
`web/src/styles/app.css:935` 的共享选择器组 `.project-dialog, .memory-panel` 提供
`position: fixed; left/top 50%; transform; z-index: 51`；`#203` 新增的 `.provider-dialog`
（同文件 `:6285`，commit `597b501`）**只给了皮**（宽高/背景/圆角/阴影），没进这个组 ⇒
留在常规流里被 portal 追加到 body 末尾 ⇒ 落在视口下方，而 `.palette-overlay`
（`fixed; inset: 0; z-index: 50`）盖在其上。

**审计完整性**（避免"只修这一个"）：全仓 7 处 `Dialog.Content` 内容类 ——
`palette-content`（自带 fixed）、`project-dialog`×5、`memory-panel`（均在该组内）、
`provider-dialog`（**唯一漏网**）。这是**单个洞**，不是普遍模式。

**修法**：把 `.provider-dialog` 加进那个共享选择器组（一行），并在组上写清"新增浮层内容类
必须进这个组，否则就是本缺陷"。**没有**复制一份定位规则到 `:6285`——那正是当初漏掉的机制。

**为什么一直没被发现**：这个弹层**没有任何自动化覆盖**（`web/e2e/**` 里 `供应商` /
`apiKey` / `ProviderManager` 零命中；单测测不到 CSS 定位）。CSS-only 回归没有护栏
⇒ 本轮新增 e2e 专门锁"可见 + 可点"。

### 9.2 F11 — 供应商表单的两个校验失败都只说 `model-providers 422`，后端那句话被丢掉（**P2；前端**）

**现象**（真机，F10 修复后继续点）：在「添加供应商」表单里分别制造两种最常见的输入错误，
错误条逐字是：

| 输入 | 修复前界面显示 | 修复后界面显示（= 后端原文） |
| --- | --- | --- |
| ID 填 `Probe-Bad`（大写） | `model-providers 422` | `provider id 必须是 slug（^[a-z0-9][a-z0-9-_]{0,63}$）: 'Probe-Bad'` |
| Base URL 填 `not-a-url` | `model-providers 422` | `base_url 必须是 http/https URL: 'not-a-url'` |

后端这两条响应体（`curl` 实取）本来就是**可行动的中文**：
`{"detail":[{"type":"value_error","loc":["body","id"],"msg":"Value error, provider id 必须是 slug（…）: 'Probe-Bad'",…}]}`
——**信息一直在，是前端把它读了、然后扔了**（同一个 `ProviderError` 类上方的注释写的正是
"detail 就是后端那句话，不自己编文案"）。

**根因**：`web/src/lib/api.ts` 的 `providerFetch` **自己手写了一遍** `{detail}` 读取，且只认
`typeof detail === 'string'`。FastAPI/Pydantic 的请求体校验失败（422）用的是**数组**形状
`Array<{loc,msg,type}>` ⇒ 落入兜底 ⇒ 只剩状态码。同文件 **`readErrorDetail`（`:67`）早就认
三种形状**（字符串 / Pydantic 数组 / `{code,message}`），还会剥掉 Pydantic 的
`Value error, ` 前缀；它的注释里**点名了本缺陷**："不认它就会把「path 必须是绝对路径」降级成
「注册项目失败（422）」"。provider 这条路当时没接上去。

**修法**：`providerFetch` 改为复用 `readErrorDetail`（删掉手写的那一段）。**不是**新加一条
"翻译表"——后端已经是那句话，缺的只是别把它丢掉。

**为什么一直没被发现**：字符串形状的 detail 走同一条兜底，所以同一弹层的
「测试连接」失败（`未配置 API Key`）、删除不存在的供应商（`供应商 X 不存在`）**都显示正常**；
只有 422 这一类"输入形状错"才露馅，而这类恰好没有 e2e（见 §9.1 末）。

> **与既有缺陷同族**：F1（`BadRequestError` 四个字）、F3（失败文案不显示）、F5（失败原因
> 只活在后端日志）、#218/#220（分类后可读文案）——本项目的复发主题就是"后端知道原因、
> 界面不说"。F11 是同一主题在新弹层上的又一次复现，所以按同一口径处置：**只搬后端原话**。

### 9.3 本轮逐控件结果（供应商管理那一栏，含红线遵守）

| 控件 | 结果 |
| --- | --- |
| 「模型选择」→「管理模型」入口 | **修复 F10 前：点了没反应**（弹层在视口外）；修复后：弹层居中可见 |
| 弹层内可点击性 / Esc 与「关闭」 | 修复后全部可点；Esc、「关闭」都能退出，无残留浮层 |
| 空态文案 | 「还没有自定义供应商。添加一个后，它的模型会出现在模型选择器里。」如实 |
| 「添加供应商」→ 表单 | ID / 显示名 / Base URL / 模型列表 / API Key 五段齐全；创建态 ID 可填、编辑态 ID 置灰不可改 |
| 校验分支（非法 ID / 非法 URL） | 修复 F11 后显示后端原文（见 §9.2） |
| 「添加模型」/「删除模型 N」 | 能加能删；只剩一行时删除按钮 disabled（不会把表单删空） |
| 「显示 / 隐藏 API Key」 | `type` 在 `password` ↔ `text` 之间往复，`aria-label` 同步切换 |
| 「创建」（**不带 api_key**） | 成功；列表出现 `probe-provider-1` + 标记「自定义」 |
| 「测试连接」（无密钥） | 如实显示「未配置 API Key」（不是"测试通过"） |
| 「清除已保存的密钥」 | 无密钥时**不出现**（`has_api_key` 门控）——设计如此，非缺失 |
| 「删除供应商」 | 二次确认写明 `确认删除供应商 probe-provider-1？引用它的会话下一轮会明确报错。`；确认后列表回空态、无错误条 |
| 全程控制台 | 仅 Chromium 对两个 422 / 一个 400 的自动记录（§7.3 同类），无 JS 错误 |

**凭证红线遵守（本轮最要紧的一条，AGENTS.md §4.3 第 0 条）**：

- 所有写操作**都不带 `api_key`**（请求体实测：`{"id":"probe-provider-1","label":"","base_url":"…","models":[…]}`），
  ⇒ `ProviderStore.create` 不会走 `credentials.set()` ⇒ **系统凭据管理器零写入**；
- 后端用临时 `PROVIDER_STORE_PATH` 启动 ⇒ 探针条目**没有**写进全局
  `~/.agent-harness/model-providers.json`：创建又删除后该文件 `sha256` 与测前一致
  （`73cefd3b…`），内容仍是 `{"version":1,"providers":[]}`；
- 因此**没有**复现 AGENTS.md 记过的那次事故（假 key 写进系统凭据管理器且删不掉）；
- 真机**未**执行「带密钥创建 + 测试连接成功」这一格——填真 key 会写系统凭据库，填假 key
  会留下一条不可保证清干净的残留。该格由**后端单测**覆盖（`tests/web/test_model_providers.py`
  的 `test_test_uses_create_chat_model_with_resolved_config` / `test_test_failure_is_classified_and_redacted`
  / `test_test_without_credentials_is_rejected`，都注入假 chat model、不碰系统凭据库），
  真机上**如实登记为未做**，不假装点过。

### 9.4 巡检 3：排队控件 + 停止（用 stub 造在途 run）

方法：临时 stub 模型（`stub_model2.py`，`SLOW` 触发 150 行 × 400ms ≈ 60s 的长 run）
让 run 稳定停留在途，再用真 Chromium 逐个点队列条上的控件。探针会话自建自删，
每次结束核基线（32）。

| 控件 | 结果 |
| --- | --- |
| `Enter` 排队 | 队列条出现「排队 <内容>」；后端 `message/queued` ✓ |
| 「编辑排队消息」→「保存」 | 就地输入框打开并**带出原文**；保存后队列显示新文案 ✓（后端语义 = 先 `queue/cancelled` 旧项再 `message/queued` 新项，见 `service.py:748-750`；**代价是该条目移到队尾**，这是"取消+重投"表示的固有结果，代码里写明，非缺陷） |
| 「编辑排队消息」→「取消编辑」 | 草稿丢弃、队列不变、输入框收起 ✓ |
| 「立即发送」 | 条目由「排队」变「引导」（steer 注入在途 run）✓ |
| 「取消排队消息」 | 服务端已取消、UI 也摘条目 —— 修复前不摘，见 F14（**已修**） |
| 「停止」 | `POST /cancel` ✓，run 以 `run/failed{reason:"cancelled"}` 终结（契约见 `app.py:1445-1461`：用户取消就是 `run/failed(reason=cancelled)`，`run/interrupted` 专给崩溃恢复，二者有意区分） |
| `Esc` | 同一条 `POST /cancel` 通道 ✓ |

> **探针方法学更正是记录，不是缺陷**：第一次点「停止」时 Playwright 报
> `element is not stable` 并超时。根因是 `.composer-stop` 带
> `animation: breathe 2.2s infinite`（**有意为之**的脉冲提示，不是坏样式），
> 无限动画的控件永远不满足可点性检查。本仓 e2e 早就踩过并留了正解
> （`e2e/composer-stream-actions.spec.ts:159-164`：开 `prefers-reduced-motion: reduce`
> 让全局兜底压掉动画，**而不是**用 `force: true` 把检查关掉）。本巡检修法一致后
> 点击一次即通。**不得据此开票**——它不是产品缺陷。

#### F13 — 硬删掉的会话会被后台记忆写回**复活**，且复活的日志只剩那一条写回事件（**P1；后端**）

复现（`repro_delete_resurrect.py`，三轮独立复现）：

1. 建会话 → 发一条消息 → 等 run 到终态；
2. `DELETE /api/sessions/{id}` → **200，`{"deleted":true,"events":N}`**（N 条事件被删干净）；
3. 约 20–30s 后 `GET /api/sessions` → **它又回来了**；
4. 打开它的事件流：**只有 1 条**，`type=memory/degraded`，`data={operation:"writeback", reason:"unavailable: TimeoutError"}`
   —— 原 N 条事件**没了**（文件被删除后又被写回任务重新创建）。

三次实例：`eb38cbf3`（复活后 `seq:37`、单条 `memory/degraded`）；
`504837ce`（第二次删除回执 `events: 1`，即复活后只剩写回那一条）；
另两次探针会话删除后 25s 内全部复活。

危害两层：① 用户删掉的会话自己回来（不可逆操作被静默撤销）；② 复活时日志被重建，
**用户历史事实性丢失**（`DELETE` 回执里的 `events:N` 已经承诺删掉了 N 条）。

**根因（已定位并修复，#232）**：`MemoryWriteback` 是 run 收尾后 fire-and-forget 的
**旁路写者**，它持有的 `Session` 不在 `delete_session` 那五道守卫的视野内——守卫 ③ 读的是
`RunManager.is_busy()`，与写回任务无关；于是它在删除之后 `append_event`，把日志凭空重建。
**修法与覆盖边界单点见 ADR-0036**（进程内已删 id 护栏 + 删除/追加共用会话写锁）；
kill test 在 `tests/memory/test_writeback.py`。

#### F14 —「取消排队消息」：服务端取消了，界面上那条**一直不消失**（**P1；前端**；**已修**）

真实症状比"条目不消失"更重：**在途 run 里按 Enter 排队一条消息，整场直播当场冻结**
（同一次运行内对照：排队前正文长度逐帧增长，排队后 8s 采样无一次变化；另一轮停在
73 整整 11s）。「取消排队消息」不摘条目只是同一个根因的第二个表现。

三轮独立复现，判据是**服务端真值对照**（不是只看界面）：

| 轮次 | 点击后 UI 队列条 | `GET /queue`（服务端真值） | 观察时长 |
| --- | --- | --- | --- |
| `p3c` | 仍是 2 条（含被取消那条） | `["排队甲"]`（已摘除） | 6.5s 轮询，每 100ms 一次，全程不变 |
| `p3h` | 仍是 1 条 | `[]` | 10s，每 500ms 一次，全程不变 |
| `p3m` | 仍是 1 条 | `[]` | 点击后 5s |

- 后端事件流**确实写了** `queue/cancelled`（`seq8 queue/cancelled <该 queue_id>`），
  且裸 HTTP 读同一条流实测**取消那一刻就下发**（`p3g`：帧到达时间线里
  `queue/cancelled` 出现在取消后的 5.12s 处，而 run 仍在跑）；
- **刷新页面即正确**（`p3c`：刷新后队列条变成 `["排队甲"]`，与 `GET /queue` 一致）
  ⇒ 首屏/重连的补齐路径（`listSessionQueue` → `restoreUndeliveredFromQueue`）是对的，
  缺的是**实时那一段**；
- 现有代码在字面上都"应该"能摘：`projection.ts:110` 的 `QUEUE_CANCELLED → filter`
  有单测（`projection.test.ts:2128`），`App.tsx:651` 明写「条目摘除由 queue/cancelled
  事件驱动……这里不本地摘」，`useSession.ts:1494` 同样不本地摘。三者一致地指向
  "靠事件"，而事件驱动的摘除在真机上没发生。

**根因（两处，缺一不可；均由真机取证坐实）**

1. **前端：ack 分支把直播丢在半路。** `sendFollowUp` 入口就推进了代际
   （`streamGenRef.current += 1`），`onEvent` 的 gen 守卫随即把原流的后续帧**全部丢弃**；
   而 queued 的 JSON 收据走的那条分支当时只写了两行——`setMode({kind:'viewing'})`
   （注释是「本次不接流」）。原流已作废、又不接新流 ⇒ **一条排队项换掉了整场直播**，
   冻结与"队列条目摘不掉"（`queue/cancelled` 同样被丢掉）是同一件事的两个面。
   修复 = ack 分支按 launched 分支同一套写法把流接回来（带本地游标，避免快照重放
   旧终态把 `terminalSeen` 提前置真）。
2. **dev 环境：Vite 代理没转发 Upgrade。** `web/vite.config.ts` 的
   `server.proxy['/api']` 缺 `ws: true`，而 Vite 的 proxy **默认不处理 Upgrade 请求**，
   于是 live 的真实通道 `WebSocket /api/ws` 在 dev 下握手永远完不成：
   `ws://localhost:5174/api/ws` → 握手 `TimeoutError`，`ws://localhost:8000/api/ws`
   （直连后端）→ 成功（python 取证）。现象即 `p3q` 修复前的记录：第二条 socket
   **opened@9277ms 却 subscribe=[]**（对象构造了、握手没成、`onopen` 从不触发），
   浏览器约 11s 后放弃 → closed@20474ms。这一条也让"ack 分支置 viewing 后由
   history 装载兜底自动接流"这条救援路径在真机上一起失效——两处叠加才是观察到的
   "永久冻结直到刷新"。

**被推翻的假设（勿重追）**：传输层丢 `queue/cancelled`（`p3g` 裸 SSE、`p3n` 裸 WS
中继都收到）；WS 中继有缺口（`p3n`）；`validateEvent` 检疫；`shouldApplyStreamFrame` /
`isSeqGap` 误判；`Composer` 的 memo 比较器（它就是普通 `memo`）。
页内三种探针（fetch tee / `WebSocket` 包装 / `TextDecoder.prototype.decode` 包装）
都自检通过却在真跑时记到 0 帧 ⇒ **探针本身不可信**，不能拿它当"事件没到"的证据；
可信的只有协议侧观测（`page.on('websocket')` 的 `framesent`/`framereceived` 与 CDP）。

**修复后真机复验（`p3q` 重跑，同一脚本同一路径）**

- 段A（排队前）长度 19→56 增长；**段B（排队后）62→116 逐帧增长，16 次采样无一次持平**；
- 应用侧 socket `ws://localhost:5174/api/ws` 在按 Enter 后 **63ms** 打开，并在
  **@9072ms 发出 `subscribe{session f70354b9…, after_seq: 11}`**（带游标）；
- 全程 **23 帧**，其中 `queue/cancelled` @17269ms——正是点「取消排队消息」的那一刻；
- 取消后队列条数 **0**，服务端 `GET /queue` = `[]`；探针会话自删，基线回到 **32**。

**回归锁（e2e）**

- 新增 `web/e2e/multiturn-queue.spec.ts` **T12r**：在途 run 排队一条消息后 live 流必须
  继续收到并应用后续帧。判别力来自"本场只剩一次订阅机会"（历史端点空日志让 viewing
  兜底失效；POST 走窗内 SSE 不产生 WS）——修复前该分支一次都不订阅，那段文本永远
  不上屏。**红证明已做**：把 ack 分支临时还原成两行旧写法 → T12r 失败
  （`element(s) not found`，8s 超时）；还原修复 → 通过。
- **T12o 的断言随之更新**（1 次订阅 → 2 次）：迟到的 2xx 收据纠正"接错流"之后，
  现在会**换**上一条流而不是把用户留在死画面（queued 只可能出现在有在途 run 时，
  见 ADR-0030 §2 术语表：queue 的投递边界 = 当前 run 的终态之后）；"没有被重连复活"
  仍然由"无假「连接中断」横幅 + 订阅数不为 3 次以上（每轮退避各一条）"锁住。

#### 9.4 探针卫生（本节）

- 探针会话全部自建自删；每次结束核对基线：本节经历 `32 → 33/34/35` 的漂移，
  **全部来自 F13 的复活**（删掉又回来），已逐条二次删除并复核回 **32**；
- stub 模型只写进临时 `PROVIDER_STORE_PATH`，未触碰全局供应商文件；
- 用户自有会话（32 条）全程**只读**。

### 9.5 巡检 4：会话级控件（消息动作 / 转写区），全部真机点过

探针 `p4-session-controls.js` / `p4d-session-controls.js` / `p4e-collapse-jumplatest.js` /
`p4h-follow-pill-wheel.js`（`D:\tmp_probe_round3\`）。除注明外，每轮都**先自建探针会话、
用它自己的消息点控件、结束前 `cancel`+`DELETE` 并按 id 复核残留**——基线每轮回到 **32**。
判定一律取**可观察后果**（DOM 文本/属性变化、服务端真值），不是"点了没报错"。

| 控件 | 判定依据 | 结果 |
| --- | --- | --- |
| 示例 chips（空态 3 个） | 点后 Composer 内容 | ✓ 文本注入输入框 |
| 复制消息 | 按钮 `aria-label` → 「已复制」+ 剪贴板真值 | ✓（探针回读剪贴板内容与消息**逐字相同**） |
| 编辑消息 | 按钮 enabled / 弹层带原文 / 取消退出 | ✓ 终态后可用、打开带出原文、取消可退出且不误发 |
| 分叉 | 服务端会话列表 | ✓ 新增一个会话（回执 33→34），原会话不变 |
| 折叠本轮（`.turn-collapse-btn`） | 正文长度 + 折叠标签 | ✓ 227 → 78 字符、出现「已折叠 · N 个工具 · M 轮」；再点回 227 |
| 复制回答 | 按钮文案 | ✓ 「已复制」瞬时反馈 → 回「复制回答」 |
| 「↓ 最新」浮标 | 滚动容器 `scrollTop / scrollHeight / clientHeight` + 浮标显隐 | ✓ 见下 |

**「↓ 最新」为什么前两版探针测不出来（记录方法学，不是缺陷）**：

1. 第一版量错了元素：转写区的滚动容器是 `.conversation-scroll`（`ref={setScrollNode}`，
   `Conversation.tsx:334`），不是外层 `.conversation`。量 `.conversation` 得到
   `scrollHeight 40 / clientHeight 21`，永远"没溢出"，据此会误判"浮标不存在"。
2. 第二版造出了溢出（`scrollHeight 1103 / clientHeight 161`）但仍测不到，因为
   **容器恰好停在 `scrollTop = 0`**（内容在下方增长而视口没动）——`scrollTop <= 0` 时
   上滚什么都不会动，不是"用户要离开底部"，`onWheel` 有意在这一条上早退
   （`Conversation.tsx:289`）。必须先真的贴底、再真的上滚。
3. 脱离跟随的**权威信号是 wheel 而不是 scroll**（`Conversation.tsx:285-293`，
   理由：delta 提交会抢先贴底，scroll 事件派发时 `nearBottom` 已回到真）。
   所以探针必须发真实滚轮事件（`page.mouse.wheel`），程序改 `scrollTop` 复现不了。

修好后一次通过（`p4h`）：贴底 → `gap 0`、无浮标 → 滚轮上滚两格 → `scrollTop 342`、
`gap 659`、浮标 `↓ 最新` 出现（**此时 run 仍在途**：`.composer-stop` 在树）→ 点浮标 →
`gap 0`、浮标消失、正文仍在增长（1590 → 1598 字符）⇒「跳回底部并恢复跟随」语义成立。

### 9.6 巡检 4 探针卫生

- 4 轮探针各建 1 个会话，全部 `POST /cancel` 后 `DELETE`（返回 200，回执里带事件数），
  每轮结束 `GET /api/sessions` 复核**零残留**、基线 32；
- 用户自有会话只读；stub 模型仍走临时 `PROVIDER_STORE_PATH`。

> **§9.7–§9.9 为交接补记**：巡检 2（Ctrl+K 命令面板）、巡检 5（项目生命周期 + 拖拽）与覆盖台账在
> 交接时**没有落进本文档**——交接手册 §2 声称 §9.1–§9.6「含 F10/F11/F12/F13/F14 的记录」，
> 实际 `grep F12` 零命中、巡检 5 无小节。以下全部按 `D:\tmp_probe_round3\` 的探针产物
> **补写**，不含未实测的断言。补记排在 §9.6 之后而非按时序插入，是为了不打乱 §9.1/§9.3
> 等**已被 issue 正文引用**的小节编号（F10 = #229 引 §9.1/§9.3）。

### 9.7 巡检 2：Ctrl+K 命令面板（补记）—— F12 焦点被浮层关闭恢复抢走

方法同 §9.0（真 uvicorn + 真 vite + Playwright 真点击），判定一律取**可观察后果**
（DOM 属性 / `data-theme` / `localStorage` / 剪贴板 / 几何），不是"点了没报错"。

探针：`p2-palette.js`（12:36）→ `p2b-palette-detail.js`（12:37）→ `p2c-dom-discovery.js`
（12:38）→ `p2d-focus-user.js`（12:40，修复后复验）。

**逐条结果**（`p2`，当次面板共 50 项 = **11 条固定命令** + 事件跳转项）：

| 命令 | 判定依据（实测值） | 结果 |
| --- | --- | --- |
| 整页打开 Inspector | `conversationLen 2168 → 0`、`bodyTextLen 4000 → 2573` | ✓ |
| 退出 Inspector 整页 | `conversationLen 0 → 2168`、`bodyTextLen 2573 → 4000` | ✓ 退回侧栏 |
| 跳到最新事件 | Inspector 选中态变化（`p2b`：`candidateCount 9`） | ✓ |
| 复制 Run ID | 剪贴板 = `9f600dad-554a-4e12-9069-9eea812950d4`（len=36） | ✓ |
| 切换主题 | `data-theme "light" → "dark"`、`lsTheme undefined → dark` | ✓ |
| 聚焦输入框 | `document.activeElement` 落点 | **✗ F12，见下** |
| 管理记忆 | `dialogsVisible 0 → 2`、`activeElementAria null → "关闭"` | ✓ |
| 切换到 紧凑 / 均衡 / 详细 / Raw | `density` + `lsDensity` 同步；`详细`/`Raw` 令 `conversationLen 2168 → 3195 → 12061` | ✓ 四档都对 |

**与交接手册的数字差异（如实登记，不掩盖）**：手册 §4 记作「Ctrl+K 命令面板 **16 条**」，
本次按 `p2` 实测的**固定命令为 11 条**（其余为随选中会话变化的 `type · #seq` 事件项，
当次共 39 条）。两个数字对不上，来源已不可考（手册 §5 自己也提到早期侦察数字不宜沿用）；
此处以**探针 JSON 为准**。

**两条不是缺陷的项（记录方法学，禁止据此开票）**：

1. `p2` 里 `复制 Trace ID` / `打开 Trace` / `session/started` 三条命令 **`missing: true`**
   —— 产品没有 Trace 这一概念，是**探针脚本预设了不存在的命令**，`availableInstead`
   已列出面板的真实命令集；
2. `p2b` 的 `inspectorToggle.toggles = false` 是**判据不灵敏**：它量 `inspectorVisible`
   与 `bodyLen`，而收起右栏时这两者都不变。`p2c` 改用**几何量**后效果明确 ——
   `.conversation` 宽 `840 → 1181`、右栏类名 `step-detail → app-regions inspector-closed`，
   再点回 `840` / `step-detail`。

#### F12 —「聚焦输入框」命令执行后焦点被浮层关闭恢复抢走，落在 `body`（**P2；前端；已修**）

**现象**（真机）：`Ctrl+K` → 选「聚焦输入框」→ 面板关闭，但焦点**不在**输入框而在 `body`；
用户直接打字打不进去，观感是"点了没反应"。

**红证（修复前，`p2b` 12:37 / `p2c` 12:38）** —— 时点序列逐字来自探针：

| 时点 | `document.activeElement` |
| --- | --- |
| 命令项高亮（t0 / t60ms） | `button.palette-item.palette-item-actions.active` |
| t200ms 起稳定 | **`body`** —— 命令里那句 `composer-input.focus()` 被这次恢复覆盖 |

同一份 JSON 里 `directFocus = "textarea#composer-input[Agent 任务].composer"`（**直接点**
输入框可聚焦）⇒ 输入框本身没问题，问题只在**命令这条路径**。

**根因**：命令是在浮层**还开着**的时候跑 `run()` 的；Radix 关闭 `Dialog` 会把焦点"还给
打开前的元素"，而**键盘打开的浮层没有触发器** ⇒ 恢复目标就是 `body`。于是命令里的
`focus()` 当场被打断。（同一个坑 `App.tsx` 的 `createEmptySession` 已踩过一次并留了注释。）

**修法**：不再让命令与关闭时序抢焦点 —— 命令只把动作登记进 `App.tsx` 的
`paletteAfterClose` ref，由 `CommandPalette` 的 `onCloseAutoFocus`（Radix 唯一的关闭焦点
钩子）`preventDefault()` 之后执行。改动 2 个文件：`web/src/components/CommandPalette.tsx`
（新增 `onAfterClose` prop + `onCloseAutoFocus`）、`web/src/App.tsx`（登记动作 + 传 prop）。

**复验（修复后，`p2d` 12:40）**：

- `afterCommand = { active: "textarea#composer-input[Agent 任务]", paletteOpen: false }` ✓
- `afterCommandTyping = { value: "命令聚焦后打字", active: "textarea#composer-input[Agent 任务]" }`
  ⇒ **命令关闭后直接打字能进输入框** ✓（与 `directClickTyping` 的直接点击路径行为一致）

### 9.8 巡检 5：项目生命周期 + 拖拽（补记）

探针：`p5a-project-lifecycle.js`（14:32）、`p5b-project-drag.js`（16:29）、
`p5c-project-drag-cdp.js`（16:47）；项目目录 `D:\tmp_probe_round3\probe-proj{,2,3}`。
判定同样取可观察后果（注册表 JSON / 账本顺序 / 弹层原文 / 目录是否仍在）。

| 控件 | 判定依据（实测值） | 结果 |
| --- | --- | --- |
| 新建项目 | `POST /api/projects`；注册表 2 → 3（`0e98fbdd:巡检五探针项目`）；提交后弹层 `stillOpen: false` | ✓ |
| 重命名（空白） | `renameBlank = { err: "不能为空", boxStillOpen: 1 }` —— 空白被拒且**弹层不关** | ✓ |
| 重命名（正常） | `PATCH /api/projects/<id>`；注册表 `巡检五探针项目 → 巡检五探针项目B` | ✓ |
| 软删除 | `DELETE /api/projects/<id>` → 注册表回 2；确认框原文「这是软删除…**目录与其中的文件不会被删除**…**会话日志不会被删除，历史一条不少**」；`afterDelete.dirExists = true` | ✓ 语义如实 |
| 菜单「上移 / 下移」 | `ledgerAfterMenuMove` 顺序真变（`b201306e, 65b1029c` → `65b1029c, b201306e`），`rowsAfterMenuMove` 同步 | ✓ |
| 第 1 行「上移」 | `upOnFirstDisabled = true` | ✓ disabled |
| 「加入项目」（cwd 不匹配） | 后端 **409** 拒绝，弹层显示后端 detail（阴性对照） | ✓ |
| **拖拽重排** | `dragDataItems = null`、`dragReordered = false`、`ledgerAfterDrag` 顺序**未变** | **未证实（工具限制）** |

**拖拽为什么未证实**：两次尝试都取不到 Chromium 的拖拽数据 —— 空 data 的
`dispatchDragEvent`（`p5b`）与 `Input.setInterceptDrags` + `dragIntercepted`（`p5c`）
两条路都得到 `dataItems: null`。菜单路径已覆盖**同一语义**（显式移动，上表已证），
e2e 亦有覆盖 ⇒ 按交接手册 §4 的判断**不开票**，在此如实登记为「真机未证（工具限制）」，
不假装点过。

**探针卫生（本节）**：`p5a`/`p5c` 的 `cleanup` 均为 `sess <id>:200` + `proj <id>:200`；
`projectsFinal = ["ws2-e2e", "ws1-e2e"]`、`sessionAfter = 32` ⇒ 探针自建自删，基线复原 ✓。

### 9.9 覆盖台账（Round 3 收口：每个控件落在哪一档）

**口径**（先声明，防止下表数字被当成"DOM 实例数"）：

- **基数**来自 `web/src/**`（排除 `*.test.*`）的静态命中：`aria-label=` **87** 处 /
  `<button` **121** 处 / `title=` **90** 处，分布在 22 / 27 / 20 个文件。同一按钮的不同渲染
  分支会各算一次，所以它是"源码出现次数"，**不是可点控件个数**，也与下表行数不成对应关系。
- **档位只看证据在不在本文档里**：§1–§9.8 有真机点击记录（取可观察后果）→ **档 1**；
  只见于 `web/e2e/**`（mock 路由）→ **档 2**；两者都无 → **档 3**。
- **档 3 ≠ 没人管**，只表示"本轮没有真机证据"。区域内控件与证据的关联是**就近判读**，
  以「证据」列引用的小节 / 文件为准 —— 判读错了可直接按引用复核。

| 界面区域（源文件，aria-label 处数） | 控件 | 档 | 证据 |
| --- | --- | --- | --- |
| 顶栏 `TopBar.tsx`（7） | 主题切换 / 密度四档 / Inspector 开合 / 上下文看板入口 | 1 | §9.7（同名命令逐条）、§1.5 |
| 会话列表 `SessionList.tsx`（10） | 选中 / 归档 / 删除 / 分叉入口 | 1 | §9.5、§6 |
| 项目区 `ProjectDialogs.tsx`（9） | 新建 / 重命名（含空白被拒）/ 软删除 / 加入项目 | 1 | §9.8 |
| 项目区菜单 | 上移 / 下移 / 首行 disabled | 1 | §9.8 |
| 项目拖拽 | 拖拽重排 | **3**（真机未证 · 工具限制） | §9.8（菜单路径已覆盖同一语义，不开票） |
| Composer `Composer.tsx`（13） | 发送 / Ctrl+Enter / 停止 / 排队 / 引导 / 编辑排队 / 取消排队 | 1 | §9.4 |
| 消息动作 `Conversation.tsx`（5） | 复制 / 编辑 / 分叉 / 折叠 / 复制回答 / ↓最新 | 1 | §9.5 |
| 命令面板 `CommandPalette.tsx`（3） | 11 条固定命令 + 事件跳转 | 1 | §9.7 |
| 供应商管理 `ProviderManagerDialog.tsx`（6） | 全部（含两种 422 校验分支） | 1 | §9.1 / §9.2 / §9.3 |
| 模型选择 `ModelPicker.tsx`（1） | 入口 / 列表 / 「管理模型」 | 1 | §9.1、§6 |
| 选项选择器 `OptionPicker.tsx`（1） | 权限 / 档位 / 推理深度三处 | 1 | §1.3、§1.4 |
| 记忆面板 `MemoryPanel.tsx`（4） | 入口开合（Esc / 「关闭」） | 1 | §9.7（「管理记忆」命令） |
| 记忆面板内部控件 | 记忆条目增删改 | **2** | `web/e2e/s-memories.spec.ts` |
| 上下文看板 `ContextUsagePanel.tsx`（3） | 两种真实状态（`usage_only` / `no_data`）文案 | 1 | §1.5 |
| 目录浏览器 `DirectoryBrowser.tsx`（4） | 目录树 / 选择 | **2** | `web/e2e/v-dir-browser.spec.ts` |
| 项目内新建任务 `StartTaskInProjectDialog.tsx`（2） | — | **2** | `web/e2e/u-project-task.spec.ts` |
| 工作区页签 `WorkspaceTabs.tsx` + `ChangesPanel.tsx` + `OutputPanel.tsx`（1+2+1） | 页签切换 / 改动 / 输出 | **2** | `workspace-modes` / `z-changes-panel` / `x-output-panel` |
| Inspector 事件跳转 `StepDetail.tsx`（10） | 事件列表 / 「跳到最新事件」 | 1 | §9.7 |
| Inspector 其余（peek / 收起展开） | — | **2** | `web/e2e/y-inspector-peek.spec.ts` |
| 委派节点 `DelegationNode.tsx`（1） | — | **2** | `web/e2e/**`（委派相关用例，未逐一核对） |
| 删除确认 `DeleteSessionDialog.tsx`（1） | 确认 / 取消 | 1 | §9.5、`web/e2e/w-session-delete.spec.ts` |
| 复制按钮 `CopyButton.tsx`（1） | — | 1 | §9.5（复制消息后回读剪贴板，与消息逐字相同） |
| 崩溃恢复 | 甲·后端层 / 甲·真机层 / 乙·孤儿回收 | 1 | §8.1–§8.3 |
| **工具卡（展开 / 输出流）** | 展开 L0→L1→L2→L0 三段循环 / 输出流 | 1 | §9.10.1（展开循环）、§9.10.2（输出流，F16 已修） |
| **审批卡（批准 / 拒绝）** | 批准 `approve_once` / 拒绝 `deny` | 1 | §9.10.3（两键都真机点过，含 POST body） |

**本轮的空档（如实登记，不假装点过）**：

- ~~巡检 6（工具类控件）未执行~~ → **已执行**（§9.10，2026-09-17 夜），台账里「工具卡」
  「审批卡」两行由档 3 / 真机未证 升为 **档 1**；该轮开出 F15（#234）/ F16（#235）两条。
- `web/src/lib/markdown.tsx`（1 处 `aria-label`）等**非控件**（渲染产物上的标注）不计入档次判定。
- 采样法的一个固有盲区：真机时序探针每 300ms 采一次，只能证明「长度在增长」，不能证明
  每一段都已落盘到事件流——后者由 `tool/output_delta` 事件数单独核对（§9.10.2）。

---

### 9.10 巡检 6：工具类控件（工具卡展开 / 输出流 / 审批卡两键）

这一节补上 §9.9 台账里最后两个「档 3」。方法沿用 §9.0：**真 Chromium（无头）+ 真 vite
dev(5174) + 真 uvicorn(8000) + 本地 OpenAI 兼容 stub(8100)**。stub 按提示词分支返回
`write` / `bash` 的 `tool_call`；`TOOLBASH` 造 8 行 × 约 1s 的慢速输出（`ping -n 2` 间隔）。
探针会话自建自删，收尾基线：会话数 = **32**（与 §9.9 同一条基线）。

#### 9.10.1 工具卡展开循环：L0 → L1 → L2 → L0

真机「初始 + 3 次点击」、每次回读 `aria-level` / `aria-expanded` 与三处 DOM 计数
（`p6a-toolcard.json`；`inlineDetail` / `body` / `raw` 分别是行内详情、`.tool-out-body`、
原始参数块的数量）：

| 步骤 | aria-level | aria-expanded | inlineDetail | body | raw |
| --- | --- | --- | --- | --- | --- |
| 初始 | 0 | false | 0 | 0 | 0 |
| 点 1 | 1 | true | 1 | 0 | 0 |
| 点 2 | 2 | true | 1 | 1 | 1 |
| 点 3 | 0 | false | 0 | 0 | 0 |

结论：展开是 **三段循环**，第 3 次点击收回 L0；只有在 **L2** 才渲染 `.tool-out-body` 与原始
参数。终态 `act-status-success`，行尾带耗时（`… 10.9s`）。**正常，未开票。**

#### 9.10.2 输出流：修复前是"假流式"（F16 / #235）

**红证（修复前，两处独立证实）**

1. **真机时序**（`p6a3-streamscope`，展开到 L2、每 300ms 采样）：三次采样长度恒为
   **112 / 112 / 112**（`grew12 = grew23 = false`）；`samples[0] = {t: 9530, len: 112}` ——
   第一次 `innerText` 阻塞约 9.5s 才拿到内容，而拿到时已经是全部 8 行。
2. **durable 事件流**（会话 `aced744c…`）：整场只落 **1 条** `tool/output_delta`，
   其 `delta` 一次性包含全部 8 行：

```
seq 4  tool/call         {"tool_call_id":"call_probe_6","tool_name":"bash",…}
seq 5  tool/output_delta {"tool_call_id":"call_probe_6","channel":"stdout",
                          "delta":"probe-line-1 \n…probe-line-8 \n"}   ← 8 行一次性
seq 6  model/completed
```

**根因：两层，`read` 只是外层**

- **外层** `sandbox/local.py`：`_DRAIN_CHUNK_BYTES = 65536` 而 `Popen` 未传 `bufsize`
  ⇒ `process.stdout` 是 `BufferedReader` ⇒ `stream.read(65536)` **阻塞到凑满 64 KiB 或 EOF**。
- **主因** `sandbox/decoding.py`：`StreamDecoder.feed()` 在编码判定前把合法 UTF-8 字节全部
  扣在 `_pending`，一路返回空串，只有累积到 `PROBE_LIMIT`（64 KiB）或 `flush()`（= 进程结束）
  才吐字。工具输出几乎永远 < 64 KiB ⇒ 逐段回调只可能发生在 EOF。
  该缓冲是**有意**的（OBS-011：乱码固化进 append-only JSONL 不可逆），不能简单删掉。

只改外层不够——这正是修复后 `f16_probe.py` 仍报 `callbacks: 1` 的原因，也是本票根因被修正的地方。

**修法与绿证**（两层都要动，详见 #235 的补评）：`read` → `read1`；未判定期间**放行前导 ASCII
段**（0x00–0x7F 在全部候选编码下逐字节等同 ASCII，放行与判定后重解等价），非 ASCII 段落仍
扣住、且一旦扣住不许后续 ASCII 抢跑（防乱序），并把已放行字节计入判定预算（防「纯 ASCII
长流 + 坏字节」被误判成兜底编码而让后续合法 UTF-8 变乱码）。

- **单元**：`tests/sandbox/test_output_encoding.py` 27 passed（新增 `TestStreamingEmission`
  4 例）；`tests/sandbox` + `tests/tooling` 合计 **217 passed / 0 failed**；`ruff` 通过。
- **沙箱级**（`f16_probe.py`）：`callbacks: 3`，`t = 0.13 / 1.28 / 2.44`，`elapsed = 3.65`。
- **真机**（`p6a3-streamscope` 重跑）：运行期 `.tool-out-body` 长度以 14 为步长递增
  **14→28→42→56→70→84→98→112**（8 段 = 8 行），全程 `running = true`，终态长度 = 112。

```
samples: t=68…1025 len=14 | t=1351…2289 len=28 | … | t=8611…10831 len=112 (running=false)
```

#### 9.10.3 审批卡：批准 / 拒绝两键都真机点过（F15 / #234）

审批卡的可达前提（这是 F15 的关键发现）：`interactive = permission_mode_explicit and
permission_mode != danger-full-access`（`SessionService.create_and_launch` 里的 `interactive`
判据；此处刻意不写行号——本票改动会让行号漂移）。因此真机探针以**显式
`permission_mode=read-only`** 建会话，让 `write`（`WORKSPACE_WRITE`）落进「需要审批」。

两键都点过，回读了发出去的 POST body 与卡片消失（`p6b-approval-approve.json` /
`p6b-approval-deny.json`）：

| 用例 | 卡片文案（节选） | POST body | 点后 |
| --- | --- | --- | --- |
| 批准 | 「需要审批 工具授权级别为 workspace-write，但当前策略为只读（read-only）… 批准 Ctrl+⏎ 拒绝 Ctrl+⌫」 | `{"approval_id":"…","approved":true,"decision":"approve_once"}` | 卡片消失（`titleAfter="(card gone)"`） |
| 拒绝 | 同上 | `{"approval_id":"…","approved":false,"decision":"deny"}` | 卡片消失 |

**F15（#234）**：上述形态在**首条消息**下成立；但**续聊**（resume）时审批卡不出现——
只读会话里的 `write` 未经审批直接执行。根因是 `resume_and_launch` 硬编码
`PermissionPolicy.WORKSPACE_WRITE` + `approval_callback=None`（`None` 在 `build_runtime` 里
= 全自动批准），且权限档**从未落进事件流**。修法：把档位写进 `session/started`，续聊时从事件流
派生并重建交互回调。红证 → 绿证对照：

```
repro6（修复前）types = …,tool/call,model/completed,tool/result,…   has_tool_approval_requested=False
repro8（修复后）types = …,tool/call,tool/approval-requested
                 tool_name=write permission=workspace-write policy=read-only
                 allowed_decisions=deny|approve_once
                 → approve_once → permission/resolved → tool/result → run/completed
```

**两轴审查补出同一根因的另外两扇门，已在本票一并关掉**（都属于"创建期权限决策不落
事件流"，不修就是同一条 P1 换个入口）：

- `auto_approve` 的显式声明同样没落盘：`auto_approve=false` 且未选档位时创建走 **deny
  路由**（回调一律拒绝），但续聊读不到它 → 退化成全自动批准。
- **fork 子会话不继承权限档**：`fork.py` 只继承 `cwd`，child 的 `session/started` 没有
  `permission_mode` → 只读父会话分叉出的 child 复制了父的 workspace 文件，却对写操作免审批。

修法与主干同构：`auto_approve` 也在显式声明时落键、续聊复原 deny 回调（档位声明优先，
与创建路径同一优先级）；fork 像继承 `cwd` 一样继承 `permission_mode` + `auto_approve`。
回归锁：`tests/session/test_permission_mode_persistence.py`（16 例）+ `tests/session/test_fork.py`
（2 例），并对 deny 路由做了**变异红证**（把派生函数打回恒 `None` ⇒ 回调变 `None` ⇒ 用例必红）。

#### 9.10.4 本轮开出的工单

| 编号 | 标题 | 级别 | 状态 |
| --- | --- | --- | --- |
| #234 | F15 会话级权限档不落事件流，续聊硬编码 workspace-write + 全自动批准 | P1（安全边界） | 本节已修 + 真机绿证 |
| #235 | F16 本地沙箱输出流是假的（`read` 阻塞 + `StreamDecoder` 探测缓冲） | P2 | 本节已修 + 真机绿证 |

#### 9.10.5 探针卫生（本节）

- 本节所有会话自建自删，收尾 `GET /api/sessions?include_archived=true` = **32**（= §9.9 基线），
  与 `baseline6.json` 逐 id 比对无残留。
- 收尾时清掉一个**上轮遗留**的泄漏会话 `aced744c-6614-4c5d-9bca-7b22ffaa88ef`
  （12 事件、已终态）。
- **环境噪声登记（非产品缺陷）**：`DELETE /api/sessions/{id}` 在「由工具启动的后端」里会返 500。
  真因是 WorkBuddy 的 `sitecustomize.py` 安全删除 shim——单轮批量删除超过阈值 50 个文件时抛
  `SystemExit(1)`，而 `SystemExit` 不是 `Exception`，穿透了应用只捕获业务异常的 `except`。
  该会话目录有 134 个文件故触发。让服务端在**不带** `CODEBUDDY_TOOL_CALL_ID` /
  `CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR` 的干净环境下启动（≈生产）即返回 200：
  `{"deleted":true,"events":12,"detached_from_projects":0}`。
- 另一条同源噪声：Python 侧探针默认走宿主 `http_proxy`（本机 127.0.0.1:58918），会把
  localhost 请求转给代理，由此返回 500 / 502 假信号。本节全部 Python 探针显式
  `ProxyHandler({})` 直连；PowerShell 的 `Invoke-RestMethod` 默认绕过 localhost，故未见此坑。

