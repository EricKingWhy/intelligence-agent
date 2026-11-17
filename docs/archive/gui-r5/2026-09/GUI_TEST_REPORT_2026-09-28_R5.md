# Agent Harness Inspector — 第五轮测试文档（基于已完成 ticket 的功能验证）

> 测试日期：2026-09-28。方法：先深读项目（issue 全量 362 条 / 324 closed、47 条 API 路由、
> 前后端源码结构），从**已完成 ticket 声明的功能**提取可验证测试点，再逐批实测。
> 约束同前几轮：不修改任何代码、不动用户真实数据、`.env` 值零泄漏。
> 问题即时记录于 `GUI_BUG_LIST_2026-09-28_R5.md`；前四轮见
> `GUI_TEST_FINDINGS_2026-09-27.md` / `GUI_TEST_REPORT_2026-09-28.md`（第四轮含 B33=P0）。

## 一、本轮定位

第四轮是"暴力遍历"（每个按钮、每个端点）；第五轮改为**证据导向**：每个测试点对应一条
已关闭 ticket 的完成声明，验证"声明的完成"是否真的完成。测试点不设上限，清单如下
（执行中持续追加）。

## 二、测试点清单（从 closed tickets 提取）

| # | 测试点 | 来源 ticket | 状态 |
| --- | --- | --- | --- |
| T01 | lineage 树 API（fork 祖先链） | #113/#112 Phase14 T7/T6 | ✅ 通过 |
| T02 | /context-usage 返回真实数据（不再是恒 no_data） | #212 | ✅ 通过 |
| T03 | 记忆治理 API：列表/版本/设置/recall 端点 | #300/#299/#302 | ✅ 通过 |
| T04 | /favicon.ico 不再 404 | #224 | ✅ 通过（修复生效） |
| T05 | /api/capabilities 声明 changes/terminal 面 | #193 | ✅ 通过 |
| T06 | 供应商 API：422 校验/不存在删除 404/test 端点 | #203/#215 | ✅ 通过 |
| T07 | 权限档会话内可改（降档直接/升档确认对话框） | #282/#283 F18-A/B | ✅ 通过 |
| T08 | readonly 档写工具需审批（会话级权限落事件流） | #234 F15/F17 | ✅ 通过 |
| T09 | turn 预算暂停→PausedPanel→恢复续跑 | #312 T4 | ✅ 通过 |
| T10 | 预算恢复预校验：必然 409 的草稿被拦 | #313 T5 | ✅ 通过 |
| T11 | 在途 run 入队（Enter 排队）→ 编辑/立即/取消/消费 | #196/#132/#231 | ✅ 通过 |
| T12 | 活跃审批 HTTP 回传：POST approve → 工具执行 | #136 T6 | ✅ 通过 |
| T13 | 活跃审批 POST deny → 工具拒绝 + 模型转述 | #136 T6 | ✅ 通过 |
| T14 | fork 线路：child 工作区 copy-on-fork + tail 摘要 | #109/#110 | ✅ 通过（#110 HTTP 面=文档化偏差） |
| T15 | Inspector 键盘导航/钉住/整页/peek | #182 T2 | ✅ 通过 |
| T16 | 上下文容量看板（ContextUsagePanel）有数据 | #200 | ✅ 通过 |
| T17 | CLI：sessions --tree / fork / replay | #133/#111/#114 | ✅ 通过（R5-B5 语义错位） |
| T18 | 坏模型 → run/failed 带原因 → UI 显示失败原因 | #220/#222 | ✅ 通过（真实历史数据验证） |
| T19 | 会话硬删后不复活（记忆写回不复活已删会话） | #231 F13 | ✅ 通过 |
| T20 | events 全量 seq 连续性 + WS 快照 backlog 截断标记 | #208 | ✅ 通过 |
| T21 | 500-turn 熔断契约暴露（max_steps 兼容层） | #311/#320 | ✅ 通过 |
| T22 | 权限 pill 与 session/started 唯一来源一致 | #234 F17/#282 | ✅ 通过 |

**总判：22/22 测试点的票面声明功能均真实完成**——9 条 closed-ticket 功能链（审批回传、
预算暂停恢复、排队 steer、fork copy-on-fork、tail summary、Inspector 键盘面、容量看板、
失败原因传播、硬删除）在真机端到端成立。过程中另发现 5 个票面外缺陷（2 个 P1、1 个 P1-遮挡、
2 个 P2/P3 语义错位，详见 bug 清单 R5-B1/B3/B4/B5/P6），全部不属于已验证 ticket 的范围，
属于"票面之外的缝隙"。

## 三、执行记录（按批次追加）

### 批次 1 — API 契约面（T01-T06、T20、T21）✅ 全通过
- **T01 lineage**：`GET /api/sessions/{id}/lineage` 对 fork child 返回正确祖先链
  （`origin=fork`、`fork_point_seq` 与 fork 时一致），edges/children 结构完整。
- **T02 context-usage**：`GET /api/sessions/{id}/context-usage` 返回真实估算
  （window 200,000 / used、breakdown 六分类、cache 命中率、`estimated: true`）。
- **T03 记忆治理**：记忆列表/版本/设置/recall 端点全部可达且返回真实数据（#300/#299/#302）。
- **T04 favicon**：`/favicon.ico` 200（#224 修复生效：`web/index.html` 的 svg link + `web/public/favicon.svg` 在位）。
- **T05 capabilities**：`/api/capabilities` 如实声明 changes/terminal 面。
- **T06 供应商 API**：非法供应商配置 422 带可读原因；删除不存在的 provider 404；test 端点可达。
- **T20 seq 连续性**：多会话 events 全量拉取，seq 无跳号、无重复；WS 快照 backlog 截断标记正确（#208）。
- **T21 max_steps 熔断**：`max_steps=501` → 422「请求的 local fuse=501 超过生效 ceiling=500
  （deployment）：下层只能收窄（ADR-0044 D1）」；`max_steps=3` → 200 接受（#311/#320）。
- **附带发现**：`?launch=false`（查询参数）+ `task` → 422 互斥提示，语义清晰；但 body 里
  传 `launch` 字段被静默忽略（见 bug 清单 R5-P6）；`task` 现在可省略（required=[]，
  第四轮 B28 的 422 矛盾已修复——见批次 10 抽检）。

### 批次 2 — 权限档（T07、T08、T22）✅ 全通过
- **T07 会话内改档**：workspace-write → danger 触发确认对话框，取消不改档；确认后
  `session/permission` 类事件落流；降档直接生效。改档后 UI reload 会话，pill 与事件一致。
- **T08 readonly 档**：readonly 会话里写类工具按预期被拦/需审批（F15/F17）。
- **T22 权限 pill 唯一来源**：composer 的权限显示读会话投影（`permission.ts` in-session
  只读 conversation projection），与事件流、改档回执一致。
- **测试方法教训**：曾误判"改档假成功"——实为两个会话同名（FizzBuzz），fetchlog 显示 POST
  打到了另一个会话。此后所有 UI 归因先核对 header 的 session id。

### 批次 3 — 预算暂停/恢复（T09、T10）✅ 通过 + R5-B1
- **T09 正路径**：`POST /messages` 带 `budget.run.max_agent_turns_total=1` 发 2-turn 任务 →
  第 1 turn 结束即 `run/paused`（reason=budget）→ UI PausedPanel 弹出 → 抬高 ceiling 恢复 →
  同一 run 接着跑完（`run/completed` 同 run_id）。
- **T10 恢复预校验**：PausedPanel 抬到必然 409 的 ceiling 被**前端拦截**（不发注定失败的
  请求）；草稿按 run_id 记忆，跨多次暂停仍在。
- **R5-B1**：`launch=false` 创建时 body 里的 `budget.run` 被**静默丢弃**（对照：
  expected_version 409、max_tool_calls 422 都很响），见 bug 清单。

### 批次 4 — 排队/steer（T11）✅ 全通过
- 长 run 进行中 `POST /messages` → 消息入队（UI 队列条出现）；**编辑**改写队列内容、
  **立即发送**注入当前运行（steer 语义）、**取消**移除；run 完成后消费循环
  （run/completed → session/resumed → user/message → run/started → queue/consumed）逐条正常。
- Ctrl+Enter=steer、Enter=排队 的双通道行为与票面一致（#196/#132/#231）。

### 批次 5 — 审批 HTTP 回传（T12、T13）✅ 语义全通过 + R5-B2/B3/B4
- **T12 批准路径**：workspace-write 会话发 danger 工具任务 → `tool/approval-requested`
  落流 → UI 审批卡弹出 → 点「批准」→ `POST /api/sessions/{id}/approve` →
  `permission/resolved decision=approve_once` → 工具真实执行（bash echo，exit_code=0）→
  模型如实转述输出 → `run/completed`。
- **T13 拒绝路径**：同一会话再发一次 → 点「拒绝」→ `decision=deny` →
  `tool/result ok=false error_code=PERMISSION_DENIED`（不执行）→ 模型如实转述被拒 →
  `run/completed` 干净收口。
- **三个新发现**（详见 bug 清单）：R5-B2（`auto_approve=false` 不带显式 `permission_mode`
  走静默拒绝路线且把开发者文案 "manual approval not yet wired" 给用户看）、
  R5-B3（审批卡被会话内容遮挡、按钮鼠标点不到，elementFromPoint 双重实证）、
  R5-B4（审批决策提交后 UI 卡死流式态、最终回答不渲染、composer 停在「等待审批决策…」，
  批准/拒绝两条路 100% 复现，刷新才能恢复）。R5-P3（等待提示把"等用户"说成"等模型"）。

### 批次 6 — fork/lineage（T14）✅ 通过 + P6
- **fork 边界语义**：API `POST /forks {from_seq:33}` → child 复制 seq 0-32 + `session/forked`
  （`boundary_user_message_seq=33`、`fork_point_seq=32`），锚点消息不进 child，与票面一致；
  UI fork（消息悬停 fork 按钮）→ 同语义落流并自动切换到 child。
- **#109 copy-on-fork**：父会话写 `fork-test.txt` → fork → child 工作区**有同名同内容文件**；
  child 里新写 `child-only.txt` → 父工作区**不受影响**。复制与隔离双向成立。
- **#110 tail summary**：HTTP 面 `service.fork` 默认 `with_tail_summary=False`
  （docstring 明说是确定性优先的设计决策），CLI 面默认开（见批次 8 验证）——票面 AC
  "默认开启"在 HTTP 面是**文档化偏差**，不是缺陷；记录在案供对账。
- **lineage 树**：`sessions --tree` 渲染 fork/delegation 两类边；空 seed（fork 自首条消息）
  的 `fork_point_seq=null` 在树上显示 `@?`（lineage.py 显式设计，非 falsy bug）。

### 批次 7 — Inspector 与看板（T15、T16）✅ 全通过
- **T16 ContextUsagePanel**（#200）：TopBar Gauge 入口 → 面板数字与 API 逐项一致：
  `5,873 / 200,000（2.9%）`、消息 795 / 系统提示词 162 / 技能 0 / 系统工具 2,095 /
  MCP 工具 2,744 / 其他 77、平均缓存命中率 96.7%（4 次调用全部带回明细）、估算标记在场。
- **T15 Inspector**（#182/#183/#197）：
  - 键盘：点击时间行选中 → ArrowDown/Up 逐行移动选中（selIdx 2→4→3）→ Space 快按开 peek
    （选中项详情）→ Esc 关闭 peek；
  - 钉住：`aria-pressed` false→true，钉住后切换会话面板保持打开（AC4），可再取消；
  - 拖宽：方向键 ±16px 且方向正确（右停靠面板 ArrowLeft=变宽）；指针拖拽 1:1 跟手
    （左拖 60px → 336→380，右拖 40px → 340）；`role=separator` + aria-valuenow/min/max 如实上报。

### 批次 8 — CLI（T17）✅ 通过 + R5-B5
- **sessions --tree**（#133）：正确渲染 107 会话的 lineage 森林，fork/delegation 边齐全。
- **fork + tail summary**（#111/#110 CLI 面）：`agent-harness fork 7fef2632 --from-message 2`
  → child 的 `session/forked.tail_summary` **在场**且内容准确（概述了被放弃路线：test-1 成功、
  test-2 因 danger/workspace-write 被拒）——#110 的 LLM 摘要挂接真实工作。
- **replay**（#114）：逻辑回放渲染 `[用户]/[工具]/[assistant]`，工具结果显示**冻结终态**
  （不重执行），PERMISSION_DENIED 失败事实如实呈现。
- **R5-B5**：`--from-message N` 按 **seq** 而非"第 N 条用户消息"解析（报错文案自曝：
  `可用边界: [2, 33]` 是 seq 列表），见 bug 清单。
- **环境观察**：CLI 与后端共 root 时被单实例锁拒绝（提示 pid、逃生门 `ALLOW_SHARED_ROOT=1`
  带显著警告）——锁机制本身工作正常，本次测试经逃生门执行只读/低风险命令。

### 批次 9 — 失败原因传播（T18）✅ 通过（以真实历史失败数据验证）
- 环境约束：目录内 3 个模型凭据全部有效，无法安全注入坏供应商（注册探针供应商会污染全局
  `model-providers.json`、违反测试红线）→ 改用**仓库内真实历史失败会话 06e37abf**
  （`run/failed reason=provider_account_unavailable` + 固定中文 message）验证 #222（后端：
  事件带 reason/message，多会话 grep 实证）与 #220（前端投影）：
  - Inspector **Timeline** 的 run/failed 行直接显示完整中文文案「模型供应商账户不可用
    （欠费 / 配额耗尽 / 账户被冻结），请到供应商控制台检查计费与配额」；
  - 切 **Overview**：`.run-failure-val` 元素在场（#222 票面记录的缺陷态是 0），同时显示
    message 与 reason `provider_account_unavailable`；
  - header 徽标「失败」正确。

### 批次 10 — 硬删除不复活 + 修复抽检（T19）✅ 全通过
- **T19 硬删除**（#172/#231 F13）：对我自己的测试会话 8f2b5604 执行 `DELETE /api/sessions/{id}`：
  - 回执如实：`{"deleted":true,"events":3,"detached_from_projects":0}`（3 = 实际事件数）；
  - events 端点 404；会话列表移除；`sessions/*.jsonl` 文件删除；lineage 树零引用；
  - `harness.db` 全部 12 张辅助表（operations/checkpoints/session_meta/workspaces/
    workspace_*/transport_ledger/delegation_trees）**零残留行**；
  - 重复 DELETE → 404（无墓碑，语义诚实）。未观察到任何复活路径。
- **B28 抽检**：无 task 创建会话 → 200（第四轮 B28 的「schema 声明可空、实现强制必填」
  已修复，schema `required=[]` 与行为一致）。

## 四、结论

### 1. 总体判断

**票面可信度高**：从 324 条 closed tickets 里提取的 22 个可验证测试点，22 个全部在真机
端到端成立——包括最容易"纸面完成"的几条：审批 HTTP 回传（批准/拒绝双路径语义完整）、
预算暂停→UI 恢复、copy-on-fork 工作区（复制+隔离双向）、CLI tail summary（LLM 摘要
真实生成且内容准确）、失败原因投影（用真实历史失败数据验证前后端两半）、硬删除
（事件/文件/列表/DB 四面清除+零复活）。#212/#224/#311 等"修复型"票据也复核确认修复在场。

### 2. 本轮发现的缺陷（9 条，详见 `GUI_BUG_LIST_2026-09-28_R5.md`）

| 编号 | 级别 | 一句话 |
| --- | --- | --- |
| R5-B3 | P1 | 审批卡被会话内容遮挡，批准/拒绝按钮鼠标点不到 |
| R5-B4 | P1 | 审批决策后 UI 卡死流式态：最终回答不渲染、composer 永久禁用，只能刷新 |
| R5-B1 | P1 | `budget.run` 在 launch=false 创建时被静默丢弃（预算失效无任何提示） |
| R5-B5 | P2 | CLI `fork --from-message N` 按 seq 解析，帮助承诺的是序数——锚点静默错位 |
| R5-B2 | P2 | `auto_approve=false` 无显式档位 = 静默全拒绝路线，用户看到开发者内部文案 |
| R5-P6 | P3 | body 里的 `launch` 字段被静默忽略（合法开关只在查询参数） |
| R5-P3 | P3 | 等审批时 header 说「没有新进展，仍在等待模型」——把"等用户"说成"等模型" |
| R5-P4 | P3 | 预算设置无 UI 入口，纯 UI 用户永远够不到 PausedPanel |
| R5-P5 | P3 | ArtifactViewer「显示 100 / 共 100 行（已截断）」自相矛盾 |

**模式归纳**：9 条里有 3 条（B1/B5/P6）是同一类病——**参数/语义静默失效**（schema 接受
但不生效、flag 名与实现语义错位、未知字段静默忽略）。这比报错更危险：预算失效烧的是钱，
fork 锚点错位丢的是历史。建议作为一类专项治理（schema `extra="forbid"` + 参数语义审查）。
另有 2 条（B3/B4）集中在**审批人机链路**——与第四轮 B33（#337 僵尸审批）、B34（WS 冻结）
同一条链：审批是这个产品状态同步最脆弱的路径，建议合并排查。

### 3. 测试方法与边界

- **证据导向**：每测试点对应一条 closed ticket 的完成声明，实测前先读票面 AC 与源码，
  "声明什么就验什么"；执行中追加到 22 点。
- **历史数据复用**：T18 无法安全注入坏供应商（注册探针会污染全局 `model-providers.json`、
  违反 AGENTS.md 安全注记），改用仓库内真实失败会话验证——既零污染又更贴近真实。
- **单实例锁**：CLI 与后端共 root 被锁拒绝，经文档化逃生门 `ALLOW_SHARED_ROOT=1` 执行
  只读/低风险命令——锁机制本身工作正常。
- **不修改任何代码；.env 零接触；只用自建测试会话做写操作**（删除的是自己的测试会话）。

### 4. 测试环境与遗留

- 后端 `http://127.0.0.1:8001`（8000 被用户另一应用占用），前端 Vite `http://localhost:5175`
  （仓库外配置 `D:\tmp\vite.test.config.ts`，代理 /api→8001）。
- 本轮新建测试会话：bc24b111、ea52c83b、d7635eda、7fef2632（审批主证据）、803137f0、
  ab13b5a4、8f2b5604（已硬删除）、2c201476（CLI fork）、35252904、2c0ff242 及一个
  B28 抽检空会话；均为测试数据，可随时清理。
- 后端日志中的 `memory/degraded provider_error`（formation 阶段）在多次 run 里复现，
  与本轮测试点无冲突，属既有观察（前四轮 B-memory 系列已记录），不重复立案。
