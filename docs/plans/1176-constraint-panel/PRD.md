# PRD：约束管理面板（#1176）——三层作用域 + subagent 继承

- 关联 Issue：#1176（产品反馈：约束管理面板缺失）
- 状态：已定稿（2026-10-15 王浩宇亲口定稿效果图 v2 + 4 个裁决点）
- 裁决记录：
  1. 分三阶段交付：Phase 1 会话级面板 + 撤销 → Phase 2 项目级作用域 + 登记选择器 → Phase 3 subagent 继承。
  2. 撤销保留二次确认弹窗（跟 issue AC，不跟成熟产品惯例）。
  3. 项目键按 git 仓库根。
  4. 已登记的老事实不做升级路径，只管新增。
- 视觉：Linear 设计语言（`~/workspace/custom-skills/local-design-md/design-md/linear.app/DESIGN.md`），深黑底 #010102、四级 surface、hairline 分隔线、eyebrow 微标签、薰衣草紫 #5e6ad2 只用于主操作。
- 效果图源码：本目录 `mockups/phase{1,2,3}.html`（可交互），截图 `mockups/phase{1,2,3}.png`。

## 1. 背景与目标

用户通过输入框"将这条消息作为可复用规则"登记长期约束后，当前产品没有任何界面可以查看"生效了哪些约束"、也没有撤销入口。目标是补一个约束管理面板：看得见、撤得掉、可审计。

## 2. 术语

| 术语 | 定义 |
|---|---|
| 约束（constraint） | 广义的长期事实：constraint / user_goal / authorization / work_boundary 等类型，`fact_id` 唯一标识 |
| 会话级 | 只在当前会话生效，随会话结束自然失效（现状，零迁移） |
| 项目级 | 写入项目级存储，按 git 仓库根聚合；该仓库开的所有会话都生效（新增） |
| 全局级 | 写入用户级存储，所有项目的新会话都生效（复用现有 USER scope 能力） |
| 撤销 | 用户主动使一条 active 约束失效。实现走现有 `superseded_by_fact_id` 链：追加 revocation 事件，历史不删，视图层抑制 |

优先级（冲突时）：本会话 ＞ 本项目 ＞ 全局。更具体的优先。

## 3. 总体信息架构

约束管理面板常驻 Web Session Inspector 内一页。打开即展示"本会话实际生效的全部约束"：
SESSION 组（本会话事实）+ PROJECT 组（本项目约束，标注 git 根）+ GLOBAL 组（全局约束）。
Phase 1 只有 SESSION 组；Phase 2 起三组齐全。

全局铁律（三个阶段都必须守）：
- 面板数据必须来自后端查询接口的实时投影，前端不维护第二套状态。
- 撤销是副作用操作，走 Runtime 权限/审批路径，不靠 Prompt 约束。
- Memory/事实存储保持 Provider 可替换边界，面板不绑定具体 Provider 实现。

---

## 4. Phase 1：会话级约束管理面板

### 4.1 落点
Web Session Inspector 内新增一页"约束管理"（路由/入口由实现者按现有 Inspector 导航规范定）。

### 4.2 页面元素（对照 mockups/phase1.html）
1. 页眉：eyebrow "CONSTRAINTS" + 标题"约束管理"（28px/600）。
2. 会话行：绿点 + "当前会话" + `sess_id`（mono）+ 说明"数据来自后端实时查询，前端不存第二份状态"。
3. 筛选条：
   - 状态 pill-tabs：全部 / 生效中 / 已撤销，**默认选中"生效中"**（与 #1176 AC 一致）。
   - 类型下拉：全部类型 / constraint / user_goal / authorization / work_boundary / failed_approach / evidence_ref。
4. 列表行（一行一条，hairline 分隔，无卡片盒子）：
   - 类型：小圆点 + 类型名（12px，ink-subtle）。圆点色：constraint #8a7ff0、user_goal #4cc38a、authorization #e5a13d、work_boundary #6b9cf5。
   - 值：14px 正文。超过 3 行折叠，点击展开/收起。
   - 元信息行（12px，ink-tertiary）：`fact_id`（mono）、"MM-DD HH:mm 登记"、"来源事件 #seq N"；已撤销的追加"MM-DD HH:mm 撤销 · 操作人"。
   - 右侧：状态 pill（"● 生效中"绿点 / "● 已撤销"灰点）+ "撤销"文字按钮（默认 ink-tertiary，hover 变红；已撤销的行按钮 disabled 显示"已撤销"）。
5. 撤销二次确认弹窗（Linear modal 样式）：
   - 标题"撤销这条约束？"，说明"撤销后，新的会话将不再注入该条约束。来源记录保留，可审计。"
   - 引用块显示被撤销的内容 + fact_id。
   - 按钮：取消（tertiary）/ 确认撤销（primary，薰衣草紫 #5e6ad2）。
6. 页脚注："撤销走 superseded 历史链 —— 来源记录保留，可审计、可追溯。"

### 4.3 后端接口
- `GET /api/sessions/{session_id}/protected-facts`
  - 直接调用 `derive_protected_facts(events)` 全量投影（含 active 与 revoked/superseded），按 `source_seq` 倒序。
  - Response：`[{fact_id, type, value, status, source_event_id, source_seq, registered_at, revoked_at, revoked_by}]`。`registered_at` 从来源事件时间取；`revoked_at/revoked_by` 从 revocation 事件取。
  - 失败时返回 5xx，前端展示空态 + 错误提示，不允许用缓存数据冒充。
- `POST /api/sessions/{session_id}/protected-facts/{fact_id}/revoke`
  - 走 Runtime 权限/审批路径（与现有敏感操作同口径），审批通过后追加 `authorization_revocation` 事件（携带 `revoke_fact_id`、操作人、时间）。
  - 不物理删除任何事件/记录。
  - Response：`{fact_id, status: "revoked", revoked_at}`。

### 4.4 验收标准
1. 面板列出当前会话全部事实，字段齐 `fact_id`、类型、值、状态、登记时间、来源事件 seq。
2. 点撤销 → 弹二次确认 → 确认后该条状态变为"已撤销"；新开会话（同血缘）不再注入该条（可复现验证）。
3. 撤销记录含操作人、时间、fact_id，可在面板看到。
4. 面板数据与 `GET` 接口逐条对账一致；前端无本地状态。
5. 默认筛选为"生效中"；切换"全部/已撤销"正确过滤；类型下拉正确过滤。
6. 未登录/无权限时接口 4xx，前端不崩。

### 4.5 不做
- 不做项目级/全局（Phase 2）。
- 不做 subagent 继承展示（Phase 3）。
- 不给已撤销的条目提供"恢复"按钮（撤销本身可再登记一条新的，历史链不动）。

---

## 5. Phase 2：作用域三层

### 5.1 数据模型
- `MemoryScope` 新增 `PROJECT`；`MemoryNamespace` 实现 PROJECT 落点。
- 项目键 = git 仓库根绝对路径（worktree/子目录共享同一项目键，抄 Claude Code auto memory 口径）。
- session→project 映射：会话创建/打开时解析其工作目录的 git 根并记录；非 git 目录的会话无项目键，项目级功能对其不可见（回落到会话级）。

### 5.2 登记入口（对照 mockups/phase2.html 上半）
- 输入框"将这条消息作为可复用规则"复选框勾选后，右侧出现 pill 三选一：本会话 / 本项目 / 我的全局，**默认选中"本会话"**。
- 选中项下方一行 hint 文案：
  - 本会话："只在当前会话生效，随会话结束自然失效。适合一次性的要求。"
  - 本项目："写入项目级存储，本仓库开的所有会话都生效，别的项目不受影响。"
  - 我的全局："写入用户级存储，所有项目的新会话都生效。适合长期习惯。"
- 后两项登记时写入对应 scope 存储（project/global），同时在当前会话事件流留痕（可审计、可在面板看到）。

### 5.3 面板（对照 mockups/phase2.html 下半）
- 顶部优先级条："生效优先级：本会话 ＞ 本项目 ＞ 全局 —— 冲突时更具体的优先。撤销走历史保留链。"
- 三组，每组 eyebrow 标题：`SESSION 本会话` / `PROJECT 本项目`（+ mono 标注 `git 根 · owner/repo`）/ `GLOBAL 我的全局`，右侧计数" N 条生效中"。
- 行样式与 Phase 1 一致；项目组中被本会话同类约束覆盖的条目，右侧加描边小标签"被本会话覆盖"。
- 撤销交互与 Phase 1 相同（含二次确认）。

### 5.4 后端接口
- `GET /api/sessions/{session_id}/effective-constraints`：返回本会话实际生效全集 = 会话投影（active）+ 本项目 active + 全局 active，附每条的 scope 与覆盖关系。
- 登记接口扩展 scope 参数：`session | project | global`（默认 session）。

### 5.5 验收标准
1. 登记时三选一默认"本会话"；选"本项目"后，同仓库新开会话的面板 PROJECT 组可见该条并生效。
2. 选"我的全局"后，任意项目新会话 GLOBAL 组可见并生效。
3. 非 git 目录会话不展示 PROJECT 组，不报错。
4. 优先级条文案与实现一致：冲突时更具体的生效（用例覆盖）。
5. Phase 1 的全部验收标准回归通过。

### 5.6 不做
- 已登记的老事实不做"一键升级"（维持会话级自然失效）。

---

## 6. Phase 3：subagent 继承

### 6.1 语义
- 子 agent **默认继承**：项目级 + 全局级的 active 约束，以及当前有效的 authorization（授权边界）。
- 子 agent **不自动继承**：本会话专属事实。父如需子遵守，在委派任务描述中显式转述。
- **干净启动开关**：委派时可勾选，子 agent 不继承任何约束与授权，只按任务描述工作。

### 6.2 实现落点
- `multiagent/provider.py` 构建子 Session 时，将"项目+全局 active 约束 + 有效 authorization"序列化注入子 agent 上下文（复用 `serialize_protected_facts` 口径）。
- 委派工具参数新增 `omit_constraints: bool = False`（干净启动）。
- 授权继承对标 `session/fork.py:317` 的授权边界重映射语义：不出现"父被允许、子被拦"的错位。

### 6.3 展示（对照 mockups/phase3.html）
- 子 agent 详情视图新增"约束继承"区：
  - "INHERITED"：项目+全局 active + 有效授权，每行 scope 标注 + "已注入"。
  - "NOT INHERITED"：会话专属事实灰显，"未注入"，说明"由父在任务描述中按需转述"。
  - 干净启动开关：打开后继承区置灰 + 提示"干净启动已开启：子 agent 不继承任何约束，只按任务描述工作。"

### 6.4 验收标准
1. 委派子 agent 后，其上下文包含项目+全局 active 约束（抽查注入内容）。
2. 会话专属事实默认不在子 agent 上下文；父在任务描述中转述后，子 agent 可见该转述。
3. 有效 authorization 被子 agent 继承；构造"父允许、子越权"的反例验证拦截。
4. 干净启动开关打开后，子 agent 上下文无任何继承约束。
5. Phase 1/2 验收标准回归通过。

### 6.5 不做
- 不改变 fork 语义（fork 本来就继承）。

---

## 7. 成熟产品依据（实现者必读，摘要）

完整一手调研见 `~/workspace/research/codebuddy-1176-scope-research.md`（5 家：Cursor / ChatGPT / Claude Code / Windsurf / OpenHands，2026-10-15 抓取，附官方 URL）。
关键结论：
- 持久层只有"用户全局 + 项目/工作区"两层；"仅本会话"不做持久层（会话默认 ephemeral）。
- 项目/工作区是四家共有的核心中间层；默认窄、显式升级是被验证的交互。
- 管理 UI 都是跨作用域统一列表 + 作用域标签；反例是 ChatGPT 项目记忆无列表。
- 撤销普遍轻确认、重可回溯（本 PRD 按用户裁决保留二次确认，属偏离，需知晓）。
- 子 agent 继承只有 Claude Code 有明确设计：继承持续约束（CLAUDE.md 全层级），不继承会话态，`omitClaudeMd` 逃生门 —— Phase 3 照抄。

## 8. 门禁与交付

- 每个 Phase 独立交付：TDD、全量 pytest（`-n 4`，云端沙箱）、全量 Playwright e2e（web 改动必跑）、`ruff`/`tsc`/`vitest`/`oxlint`、`scripts/gate0.py` 6/6。
- 独立双轴审查（Correctness / Standards），实现者不自审。
- Push + 开 PR（base=main），**不 merge**，等王浩宇亲口确认。
