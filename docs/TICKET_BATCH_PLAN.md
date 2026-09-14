# 票面批次计划 — UI / 多轮会话 / 记忆 / 供应商管理（#194–#204）

- **Status**: 计划冻结，待用户下达开工指令
- **Date**: 2026-09-13
- **单一事实源**：GitHub Issues（本仓 `docs/tickets/` 是脚本生成的**只读镜像**，不要手写进那里）
- **进度落点**：后端合入 `main` 后记 `docs/PHASE_STATUS.md`；前端在途记 frontend worktree 的 `docs/SDD_TICKET_TRACKER.md`
- **本计划对应的设计文档**：
  - `docs/adr/0030-mid-run-input-steering-queue-durability-and-query-supersede.md`（#195/#196）
  - `docs/adr/0031-memory-retrieval-and-write-tools.md`（#202）
  - `docs/adr/0032-custom-model-provider-management.md`（#203）
  - `docs/design/CONTEXT_CAPACITY_DASHBOARD.md`（#200）
  - `docs/design/WEB_UI_BATCH_REDESIGN.md`（#194/#197/#199/#201/#204）

---

## 1 全部在办票一览

| # | 标题 | 端 | 批次 | 设计依据 |
| --- | --- | --- | --- | --- |
| 194 | Composer Esc 停止提示压档位控件 | 前端 | ① | WEB_UI_BATCH §1 |
| 197 | Inspector 水平拖拽方向反了（+ 默认 340） | 前端 | ① | WEB_UI_BATCH §2 |
| 199 | 模型选择器两级飞出重设计 | 前端 | ① | WEB_UI_BATCH §3 |
| 201 | 权限/档位/深度 三下拉统一（删 ContextProviderPicker） | 前端 | ① | WEB_UI_BATCH §4 |
| 198 | agent_profile / 实际工具清单不落任何日志 | 后端（+前端小字） | ④ | 本文 §4 |
| 196 | queue/steer 通道契约在、实现不在 | 后端 | ② | ADR-0030 |
| 195 | 用户消息动作行 + 编辑（含流式输入解锁） | 前端 | ② | ADR-0030 §5 + WEB_UI_BATCH §0 |
| 200 | 上下文容量看板（数据面基本缺失） | 跨端 | ③ | CONTEXT_CAPACITY_DASHBOARD |
| 202 | 记忆无模型可调用的检索工具 | 后端 | ④ | ADR-0031 |
| 203 | 自定义模型供应商管理 | 跨端 | ⑤ | ADR-0032 |
| 204 | 项目弹窗去任务内容 + 权限一致性 | 跨端 | ⑥ | WEB_UI_BATCH §5 |

---

## 2 批次、依赖与门禁

```
① 小 UI 修复（纯前端）
   #194 ──┐
   #197   ├── 无后端依赖，可立刻开工
   #199   │
   #201 ──┘
      │
      ├──→ ③ #200 的"入口替换"依赖 #201（合并后 ContextProviderPicker 才删）
      ├──→ ⑤ #203 的入口（管理模型）嵌在 #199 的菜单里
      └──→ ⑥ #204 的权限选择器复用 #201 的 OptionPicker

② 在途输入（跨端，ADR-0030）
   #196（后端核心：事件 + 投影 + steer 注入 + on_run_terminal + GET/flush + 重启重建）
      └──→ #195（前端：解锁输入 + 队列条 + 动作行 + shadow 渲染）
   ⚠ 顺序硬约束：先冻结 #196 的 HTTP 契约（GET /queue、queue/flush、supersedes_seq/queue_id 字段），
     #195 才能开工。后端可先交付契约 + 假数据端点，前端并行。

③ 上下文看板（跨端）#200
   后端：§3.1 缓存采集 + §3.2 分类快照 + §3.3 端点
      └──→ 前端：面板 + 入口替换（依赖 ①#201）

④ 记忆工具 + 可观测性（后端为主）#202 + #198
   两者都只动后端；#198 的"档位收窄小字"提示若做在 UI，依赖 ①#201

⑤ 供应商管理（跨端）#203
   后端：keyring + CRUD + test 端点 + is_available 真实判定
      └──→ 前端：管理弹层（依赖 ①#199 的两级菜单）

⑥ 项目弹窗（跨端）#204
   后端：POST /api/sessions {launch:false}
      └──→ 前端：去掉任务内容 + 权限一致性（依赖 ①#201 的 OptionPicker）
```

### 每批的关门门禁

| 批次 | 后端门禁 | 前端门禁 |
| --- | --- | --- |
| ① | — | `cd web && npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build` |
| ② | `ruff check` + 全量 `pytest` | 同上 + 新增队列条/编辑 e2e |
| ③ | 同上 | 同上 + 看板 e2e（含空态与未采集文案） |
| ④ | 同上 | 仅有小字提示时跑前端门禁 |
| ⑤ | 同上 | 同上 + 供应商 CRUD e2e |
| ⑥ | 同上 | 同上 + 空会话创建 e2e |

**跨端批次的门禁规则**：两端**不许同时**跑测试套（AGENTS §16 的既有纪律）；后端套先跑完再跑前端套。

---

## 3 关单与集成纪律（每批完成后照做）

1. **关单判定**（AGENTS §14.12）：纯后端且 AC 全覆盖 ⇒ `gh issue close <n> --comment "<commit + 测试结果 + 关键文件>"`；跨端只做完一端 ⇒ **不关单**，用 comment 记录已完成部分与剩余端。
2. **不推远程**：本地 `commit` 由实现 agent 做；`git push` / `merge main` / PR 全部由集成 agent 执行（AGENTS §13.2/§14.4）。
3. **进度记录**：后端 `docs/PHASE_STATUS.md` 追加一行（格式见 AGENTS §16.5）；前端在途记 `docs/SDD_TICKET_TRACKER.md`。
4. **集成提示词**：每批完成后写 `docs/INTEGRATION_PROMPT_<批次>.md`，内容 = 改了什么 / 为什么符合 spec / 测了什么 / 还剩什么 / 风险。

---

## 4 #198 的实现方案（唯一还没写进 ADR 的一票）

### 现象与根因（已查实）
用户问："我明明装了 write/edit/apply_patch，模型为什么说没有？"
**根因**：模型说的是事实——那次会话运行在 `agent_profile="research_review"` 档位下，`assembly.py:260-266` 用 `spec.tool_scope` 收窄了注册表（`_RESEARCH_TOOLS` 只含 read/grep/glob + 知识库/搜索），`prompt/builtin.py:49` 的 profile prompt 还明说「你没有写权限」。

**真正的缺陷**是**不可观测**，不是工具丢了：
| 缺什么 | 证据 |
| --- | --- |
| 会话 JSONL 里没有档位 | 真实会话 `events.jsonl`（2145 事件 / 13 runs / 0 tool calls）全文无 `agent_profile` |
| 诊断日志里没有工具清单 | `.agent/logs/agent.jsonl` 的 `llm_call` 行有 `llm_input`/`llm_output`/`model_id`，无工具名、无 session_id |
| 前端不持久化档位 | `App.tsx:145` 初始 `null` = 全量；刷新即悄悄回到全量 ⇒ 现象不可复现 |

### 方案（最小可回溯）
| # | 改动 | 落点 |
| --- | --- | --- |
| 1 | `run/started.data` 增加 **`agent_profile`**（**总是**写生效档位，未指定时为 `"main"`） | `runtime.py` 的 run 开始处（`RUN_STARTED` append 点） |
| 2 | 结构化日志新增一条 **`run_config`**：`agent_profile` / `model_id` / `tool_names[]`（全量生效工具名）/ `dropped_tools[]`（被 tool_scope 剔除的） | `runtime.py` 或 `assembly` 侧（在 registry 收窄之后取清单） |
| 3 | 被剔除的工具名必须记录（这正是"模型说没有 write"的答案本身） | `assembly.py:260-266` 收窄处 |
| 4 | 前端档位收窄提示（一行小字） | #201 的 `OptionPicker.footer`（WEB_UI_BATCH §4） |
| 5 | Inspector run 头标显示档位 | 读 `run/started.data.agent_profile`；缺字段时显示「档位未知」（兼容旧数据） |

**边界**：工具**名单**不进 SessionEvent（体积），只进结构化日志；事件里只放 `agent_profile` 这一个可枚举字段。

### 测试
| # | 断言 |
| --- | --- |
| T1 | 指定 `agent_profile="research_review"` ⇒ `run/started.data.agent_profile == "research_review"`；未指定 ⇒ `"main"` |
| T2 | 结构化日志 `run_config` 行含全量工具名与 `dropped_tools`；`write`/`edit`/`apply_patch` 出现在 `dropped_tools` 里 |
| T3 | 日志里**不含**任何密钥（沿用既有 redaction 断言） |
| T4 | 前端：Inspector 显示档位；旧数据（无字段）显示「档位未知」不报错 |

---

## 5 已冻结的产品语义索引（实现时查这一节，别重新解释）

| 语义 | 出处 |
| --- | --- |
| queue / steer / supersede 三个词的定义与投递边界 | ADR-0030 §2 |
| "A+B 都要回答" vs "编辑 A 成 B" 的区别 | ADR-0030 §2 末段 |
| 编辑后旧回答段**删除不显示**；暂停打断的回答**保留 + 标记中断** | ADR-0030 §4.5.1 / §4.5.3 |
| "上一轮被中断时先补齐被中断的问题" | ADR-0030 §4.5.4 |
| 记忆：自动注入不动 / 工具名 `retrieve_memory`+`remember_this` / 无分数 / 描述要有区分度 | ADR-0031 §1.3、§3 |
| 窗口真相 = 全局 200k（不接通每模型） | 看板设计 §1 裁定 3 |
| 看板必须有全部元素，含平均缓存命中率 | 看板设计 §1 裁定 2 |
| 供应商：Windows 凭据管理器 / 只做 OpenAI 兼容 / 全局 / 最小 chat completion 测试 | ADR-0032 §1.2 |
| 三 picker 合并、删 `ContextProviderPicker`、不要搜索框 | WEB_UI_BATCH §0/§3/§4 |
| 弹窗权限与 composer pill 保持一致（弹窗只影响第一次） | WEB_UI_BATCH §5.3 |
| 面板默认宽 340 | WEB_UI_BATCH §2 |

---

## 6 风险与顺序理由

| 风险 | 说明 | 处置 |
| --- | --- | --- |
| ② 是唯一动 append-only 语义的批次 | 投影 shadow 写错会让会话历史"看起来丢了" | ADR-0030 T4/T5 先红后绿；后端先合，前端后跟 |
| ① 的 e2e 断言翻转很多 | #197 的期望值全部反了 | WEB_UI_BATCH §2 给了逐行对照表；改一处跑一次 |
| ③ 的"技能/工具"桶天然不可精确分割 | dsh 归档的 "Not separable here" 同病 | 看板设计 §10：如实标注估算，不填假数 |
| ⑤ 引入新依赖（keyring） | 首次引入外部凭据库 | ADR-0032 §5 记录了 REUSE 决策与不可用后端的行为 |
| ⑥ 依赖后端新路径 | 前端删了 textarea 但后端不支持空会话 ⇒ 功能直接坏 | 后端 `launch:false` 与前端同批交付；前端先用 mock 联调 |
| 顺序错配 | 先做 #204 前端会卡在缺后端；先做 #195 会卡在缺 #196 契约 | 按 §2 的箭头顺序推进；跨端批次"后端契约先冻结" |
