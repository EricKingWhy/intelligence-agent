# Agent Harness Inspector — 第五轮问题清单（边测边记）

> 规则：每发现一个 BUG 或不合理之处立即记录。每条包含：现象、复现方式、为什么是 BUG
> （理由依据）、成熟产品的做法。测试全程未修改任何代码，未动用户真实数据。
> 配套测试过程见 `GUI_TEST_REPORT_2026-09-28_R5.md`。
> 注意：第四轮 B33（僵尸审批变砖）已核实与 OPEN 状态的 #337 为同一问题（团队已知，
> 本轮验证其修复进度即为对 #337 的跟进测试）。
>
> **⚠ 2026-09-30 白盒核实增补**：本清单为黑盒观察记录，9 条已全部立案为 issue
> #420-#428 并由子代理做源码级核实。结论：7 条成立、#428 部分成立（错因修正：前端
> 分母没错，是后端 truncated 并集语义）、**#425（R5-P6）对 HEAD 不成立已关单**
> （870c88c5 的 `extra="forbid"` 已修，黑盒观察打在陈旧 dev server 上）。
> 逐条白盒证据（file:line）见各 issue 正文与核实评论；下文正文保留黑盒原貌，
> 与 issue 冲突处**以 issue 为准**。

## 使用中（按发现顺序追加）

### R5-B1（P1，契约/预算）`budget.run` 在 `launch=false` 创建时被静默丢弃——与全文档里最响亮的 422 形成双标
- **现象**：`POST /api/sessions` 带 `launch=false` + `budget.run.max_agent_turns_total=1` 创建会话
  （bc24b111）→ 请求 200，无任何提示；随后 `POST /messages` 启动的 run 连跑 3 个 agent turn 才停，
  预算完全没有生效。创建响应、事件流、UI 三处都没有"预算被丢弃"的痕迹。
- **复现**：`{"launch": false, "task": "...", "budget": {"run": {"max_agent_turns_total": 1}}}` 创建 →
  正常发消息 → run 消耗 3 turns。对照：同样的预算字段如果随 `POST /messages` 传（正确姿势），
  超 1 turn 即暂停并弹 PausedPanel。
- **为什么是 BUG（依据）**：(a) 同一个 API 对"参数没接上"的两种形态态度相反——`expected_version`
  的 CAS 冲突会大声 409、`max_tool_calls` 会 422 拒绝，而 `budget.run` 在 launch=false 路径上被
  **无声吞掉**；安全相关的上限（预算是防失控跑钱的闸）静默失效比报错更危险。(b) 语义上
  "create 时声明预算"是用户合理预期——schema 接受它、响应不警告，用户没有理由知道必须等到
  真正 launch 那一刻再传一次。(c) 根因在实现：`service.py` 的创建路径只在"带 task 直接 launch"
  时把 budget 传给 run（`run/started.data.budget` 的来源），launch=false 时该字段无宿主、直接蒸发。
- **成熟产品做法**：Anthropic/OpenAI 的用量上限 API 对"设置了但当前不生效"的配置一律返回
  显式确认或拒绝；Stripe 对无效参数组合报 `parameter_invalid` 而不是忽略。最小改法二选一：
  launch=false + budget.run 时 422（"budget.run 只能在启动 run 的请求上声明"），或接受但在响应里
  回显 `budget_ignored` 警告字段。
- **关联**：这也是"纯 UI 用户永远够不到 PausedPanel"的根因之一（见 R5-P4）。

### R5-B2（P2，文案/安全 UX）`auto_approve=false` 而未显式传 `permission_mode` 的会话：所有工具静默走拒绝路线，用户看到的是开发者内部文案
- **现象**：创建 d7635eda 时只传 `{"auto_approve": false}`（不传 `permission_mode`）→ 会话能建、
  任务能跑，但工具一调用就失败，`tool/result` 里用户可见的消息是
  **"manual approval not yet wired"**——一句面向开发者的半成品声明。没有审批卡、没有弹窗、
  没有任何"该会话处于全拒绝模式"的事前告知。
- **复现**：创建会话只带 `auto_approve:false` → 发"执行 bash echo" → tool/result 直接报
  上述英文内部文案。
- **为什么是 BUG（依据）**：(a) 代码事实：`session/approval.py:250` 与 `session/assembly.py:314`
  的 deny 回调写死该字符串（未接线的兜底路径），这属于内部状态描述，不该作为面向用户的
  错误消息暴露。(b) 创建时刻没有任何警告告诉用户"这个组合 = 全部工具将被拒绝"——用户付出
  一次完整 run 的 token 成本后才知道一切都执行不了。(c) 与显式传 `permission_mode` 的路径行为
  突变：后者会真正弹审批卡（T12/T13 已验证），前者永远静默拒绝——两个只差一个可选参数的
  创建请求走向完全不同的授权语义。
- **成熟产品做法**：GitHub Actions 的 `permissions: {}` 显式拒绝也会在 workflow 头部展示生效的
  权限集；数据库 GUI 对"只读连接"在连接创建时就打标。最小改法：创建时若 `auto_approve=false`
  且未显式给 `permission_mode`，在创建响应与会话头部标注"审批通道未接线，工具将全部拒绝"；
  同时把该兜底文案改为面向用户的措辞。

### R5-B3（P1，UI 遮挡）活跃审批卡被会话内容覆盖：批准/拒绝按钮用鼠标点不到
- **现象**：审批卡（`role=alertdialog`，含「需要审批」+ 批准/拒绝按钮）出现后，按钮的屏幕位置
  被后到的会话内容（用户消息气泡 `msg-user`、流式 turn 容器 `turn-streaming`）叠盖——
  `document.elementFromPoint(按钮中心)` 两次分别命中消息气泡与 turn 容器而非按钮本身。
  真实鼠标点击落在覆盖物上，按钮点不到。滚动无解：卡片与内容在**同一滚动容器**里，滚到哪
  都维持同样的重叠（布局空间重叠 39px，非滚动位置问题）。卡片与容器全部 `position:static`、
  无 transform、margin 全 0——但 DOM 顺序在 turn 之后的卡片却绘制在 turn 上方且被 turn 的
  内容反盖，疑似卡片被渲染进一个不占流式高度的容器（零高度溢出），机制待前端定位。
- **复现**：会话内发"用 bash 执行 echo xxx"（workspace-write 策略 + danger 工具）→ 审批卡弹出 →
  尝试鼠标点击拒绝/批准 → 命中的是消息气泡。本轮 T12（批准）能点到纯属当时布局侥幸（消息
  更少、无重叠），T13（拒绝）即复现遮挡。
- **为什么是 BUG（依据）**：审批是整个权限体系的人机闸门，按钮点不到 = 高危操作既批不了
  也拒不了，用户唯一出路是等 300s 审批超时（第四轮 B33 的"僵尸审批"正是这个超时机制的
  连锁后果）——等于把 P0 级变砖问题的大门又敞开了一次。遮挡类缺陷的可恨之处在于
  "看起来能点"，没有任何报错提示。
- **成熟产品做法**：模态审批卡应当用真正的 overlay 层（`position:fixed` + 遮罩 + 显式 z-index，
  如 Radix Dialog 默认行为——本项目其他 dialog 都用了 Radix，唯独审批卡是文档流内元素）；
  或至少审批卡容器自带 `z-index` 与 `isolation:isolate`，保证永远在会话内容之上。

### R5-B4（P1，状态机卡死）审批决策提交后整个会话 UI 卡死在流式态：最终回答不渲染、composer 永久禁用，只能刷新页面
- **现象**：审批卡上点「批准」（T12）或「拒绝」（T13）→ POST /approve 成功 → 审批卡翻转
  （已批准/已拒绝）→ **此后 UI 再也不前进**：后端早已 `run/completed`（落盘可查），但
  - 最终模型回答**从未渲染**（最后一条 msg-model 停在"思考 · 持续 <1s…"的思考内容，spinner
    一直转）；
  - composer 永久 disabled，placeholder 停在 **「等待审批决策…」**——而审批明明已决策完；
  - header 停留「思考中 · N tok」。
  唯一恢复手段：刷新页面（刷新后最终回答、终态全部正常显示）。两条决策路径 100% 复现
  （批准一次、拒绝一次），普通不带审批的 run 均正常收口，问题特定于审批流。
- **复现**：workspace-write 会话 → 发 danger 工具任务 → 审批卡弹出 → 点批准或拒绝 →
  等待任意长时间（>1 分钟）→ UI 仍卡在流式态；查后端事件流 `run/completed` 早已存在。
- **为什么是 BUG（依据）**：(a) 后端事件齐全（permission/resolved → tool/result → text/delta →
  model/completed → run/completed），UI 至少从 text/delta 阶段起就停止消费——最终答案的
  delta（seq 55-64）一个都没上屏；(b) composer 的禁用条件与「等待审批决策」placeholder 说明
  前端存在一个"审批等待中"标志位，审批卡组件自己收到了 resolved（卡片翻转了）但该标志位
  没有被同一事件清掉——状态分裂；(c) 用户视角是"点完批准，机器人永远不回话"，与第四轮
  B33（僵尸审批变砖）、B34（WS 冻结）同一受害者链条的又一环：审批流是这个产品里
  状态同步最脆弱的路径。
- **成熟产品做法**：Claude/ChatGPT 的工具审批卡决策后流式恢复是基本盘；单事件源原则——
  卡片翻转与"等待中"标志位应消费同一个 resolved 事件/同一状态切片，而不是两套订阅各自
  为政。修复方向：以 `run/completed` 或 `permission/resolved` 任一到达即清等待标志，并给
  "流式态超过 N 秒无新事件"加自愈探活（重放或重连）。
- **关联**：第四轮 B33/#337（审批超时后僵尸卡）、B34（WS 冻结）。建议合并排查同一条
  审批→恢复链路。

### R5-B5（P2，CLI 语义错位）`fork --from-message N` 实际按 **seq** 解析，帮助文本却承诺"第 N 条用户消息"——锚点静默落在错误位置
- **现象**：`agent-harness fork <id> --from-message 2`（7fef2632 有两条用户消息，分别在 seq 2 与
  seq 33）→ child 的 `session/forked.fork_point_seq=1`，即锚点落在 **seq 2 = 第 1 条** 用户消息上。
  按 help 文本"从父会话的第 N 条用户消息处分叉"，N=2 应锚定 seq 33（fork_point=32）。
  `--from-message 1` 则直接报错：
  `fork 失败：fork 边界 seq=1 不是父会话中的用户消息（可用边界: [2, 33]）`——报错列出的
  "可用边界"是 **seq 列表**而非序数，实现在报错里自证了语义错位。
- **代码定位**：`cli.py:926`（fork_command）把 `from_message` **原样**传给
  `fork_session(boundary_user_message_seq=from_message)`，中间缺了"第 N 条用户消息 → 其 seq"
  的解析步骤。
- **为什么是 BUG（依据）**：(a) flag 名 `--from-message`、参数名 `from_message`、help 文本
  三处都承诺**序数**语义，实现按 **seq** 执行——按文档使用的用户会静默分叉在错误的历史点
  （N 恰好撞上一个用户消息的 seq 时，fork "成功"但内容错了；撞不上时报错文案还进一步
  用 seq 列表误导）。(b) 与 HTTP 端点 `/forks` 的 `from_seq`（seq 语义，如实命名）并存，
  两个入口一个按序数一个按 seq，名字却长得一样。
- **成熟产品做法**：git `rebase -i` / `jj` 的操作都显式区分"第 N 个提交"与"提交 id"；
  pi 的 `/fork` 直接用对话内选择而非数字。最小改法：fork_command 里先解析
  "第 N 条 user/message 的 seq"再传 boundary；或把 flag 改名 `--from-seq` 并改 help。

### R5-P6（P3，契约清晰度）创建会话时 body 里的 `launch` 字段被静默忽略（合法开关只存在于查询参数）
- **现象**：`POST /api/sessions` 请求体带 `"launch": false` + `task` → 200 且**立即启动 run**
  （SSE 全程跑完）；同样的意图用查询参数 `?launch=false` 才生效（此时带 task 反而 422 互斥提示）。
  `CreateSessionRequest` schema 里根本没有 `launch` 字段，pydantic 默认忽略未知字段，请求不报错。
- **为什么算问题**：`launch` 是个听起来极其"官方"的字段名，客户端（或照着别处文档写的调用方）
  传了它却毫无效果、毫无警告，且行为差别是"会不会立刻烧一次 run"——静默忽略一个影响
  副作用的开关，与 R5-B1 同族（参数静默失效）。
- **成熟产品做法**：要么 schema 里 `extra="forbid"`（未知字段 422，Stripe 风格），要么把
  `launch` 提为正式 body 字段。二选一都比静默忽略好。

### R5-P3（P3，文案）等待审批时 header 提示「没有新进展，仍在等待模型」——把"等用户"说成"等模型"
- **现象**：审批卡弹出、run 实际停在等用户决策时，header 的等待提示仍写
  「没有新进展，仍在等待模型」。
- **为什么不合理**：此刻不动的不是模型，是**用户自己**（决策权在用户手里）。这句话会让用户
  以为系统/模型出问题了，去看模型、刷新、重启，而不是去找那张等决策的卡片——方向性误导。
- **成熟产品做法**：Slack 的 approval、GitHub 的 required review 都明确写"等待你的操作 /
  Waiting for your review"。应按 quiescence 报告区分：「等待你的审批决策」/「等待模型响应」。

### R5-P5（P3，文案自相矛盾）ArtifactViewer 行计数「显示 100 / 共 100 行（已截断）」
- **现象**：查看被截断的大 Artifact 时，头部同时显示"显示 100 / 共 100 行"和"（已截断）"——
  若显示的就是全部 100 行，"已截断"从何谈起；若真截断了，分母就不该是 100。
  两个信息源（截断标记与行数统计）没有对齐。
- **成熟产品做法**：VS Code / Less 的惯例是「第 1-100 行，共 N 行」；截断时必须给出真实总数，
  无法给出时至少不展示分母。

### R5-P4（P3，产品缺口）预算功能在纯 UI 里没有任何设置入口——PausedPanel 只对 API 用户存在
- **现象**：预算暂停/恢复面板（PausedPanel）功能本身工作正常（见测试报告 T06），但整个 UI
  （新建会话表单、composer、设置页）找不到任何设置 `budget.run.*` 的入口。纯 UI 用户永远
  无法让一个会话带上预算 → 永远不会见到 PausedPanel。
- **为什么算问题**：票面交付的是"预算超限→暂停→UI 恢复"的完整用户体验，其中"设置预算"
  这一环只在 API 层存在，UI 链条从第一步就断了——功能对 UI 用户等于不存在。
- **成熟产品做法**：Claude/OpenAI 的用量限额都在设置里可见可调；至少应在新建会话表单
  提供"可选：为本次会话设置预算上限"。
- **关联**：R5-B1（API 层预算在 launch=false 时还会被静默丢弃）。
