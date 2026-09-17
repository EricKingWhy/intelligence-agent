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

