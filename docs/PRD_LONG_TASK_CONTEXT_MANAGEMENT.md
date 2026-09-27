# 长任务 Agent 上下文管理 PRD（压缩 · 锚定 · 进度可视化）

> 版本：2026-09-27 v1（三轮 grill-me 确认稿）
> 依据调研：`docs/research/2026-09-27-long-task-context-management-research.md`（下称「调研 R」）、`docs/research/2026-09-27-agent-progress-visualization-research.md`（下称「调研 P」）。本文所有「产品依据」均可在两份调研中追到一手来源 URL。
> 执行约束：后续施工由**低成本弱模型**完成，因此本文一切机制优先「确定性、可配置、有确切阈值/字段名」；依赖模型自觉的能力一律用确定性机制兜底。

---

## 1. 目的与适用范围

当 Agent 连续运行数小时时，做到**指令不漂移、目标不遗忘、上下文不爆、进度可见**。本文覆盖三个子系统：

1. **压缩管线**（Context Compaction Pipeline）：阈值触发 → 确定性裁剪 → 混合式摘要 → 校验闸门 → 确定性重注入；
2. **保护事实与目标锚定**（Protected Facts & Re-anchoring）；
3. **进度清单与可视化**（Plan List & Progress Visualization）。

适用层：通用 harness Core（`src/agent_harness/`）+ 长任务工作台产品（#344 W 系列）。**不覆盖**：Memory V2（#296/#303/#304 独立战线）、子代理 delegation（#370/#372 未决，仅写指引）、预算体系（#305 等已占）。

## 2. 与既有冻结决策的关系（去重声明）

| 既有物 | 关系 |
|---|---|
| #344 冻结契约「先可回读确定性裁剪、再带保护事实摘要、失败有界重试、硬护栏暂停」 | **继承不改**。调研 R §0 证实该顺序为全行业收敛做法（DeepSeek Harness pruner、Cline、Anthropic API `clear_tool_uses` 同构） |
| W-01（#345）摘要失败重载丢约束 | 本文 §6.4 校验闸门为其**增量**，票面原地修订 |
| W-02（#346）任务保护事实 | 本文 §5 新增「失败方案」fact 类型为**增量**，票面原地修订 |
| W-03（#347）确定性裁剪 | 本文 §4.2 补充切点规则为**增量**，票面原地修订 |
| W-04（#348）摘要重试与硬护栏 | 本文 §6 的 8 节契约/混合摘要/warning 为**增量**，票面原地修订 |
| W-05/W-06 进度文件 | **不动**。与本文 §8 进度清单是双制品（§8.6 定界） |
| W-13 重启 reconcile、W-21 真实模型 Gate | W-30 是对 W-21 的**增补判据**，不改 W-21 已有判据 |
| 规格 06 §阈值（0.70/0.85，第 100-101 行） | 现行代码 0.80/0.90 与之冲突，**规格权威**，W-25 修复 |

**零冲突承诺**：本文不产生与上述票重复的票面；凡交集一律以「增量修订既有票」方式落地，不开平行票。

## 3. 设计原则（全部有产品依据）

- **P1 状态外置成确定性制品，上下文随时可重建**——调研 R §0 核心结论；Anthropic 长任务工程博客、Codex 长任务博客四份 Markdown 同思路。
- **P2 压缩是最后手段，裁剪是前置手段**——dsh tool-result-pruner（裁完达标即跳过摘要）、Cline 旧文件版本删除、Claude Code 压缩后重读最近文件。
- **P3 能确定性清理就不让 LLM 摘要；能恢复就不丢**——用户 2026-09-27 设计原语，与 P2 同构。
- **P4 用格式与校验替代模型自觉**——Anthropic feature_list.json 只许改 `passes` 布尔（"the model is less likely to inappropriately change or overwrite JSON files"）。
- **P5 失败哲学：宁可超预算也不丢数据**——dsh「压缩失败保留完整历史仅告警；摘要未变小则拒绝」。
- **P6 弱模型兜底**：摘要全文不让弱模型写——程序化预填 + 模型只补 4 个短节（调研 R B2，无产品先例，本仓自建）。

## 4. 压缩管线

### 4.1 触发阈值（规格权威）

- `auto_compact_threshold = 0.70`、`hard_guard_threshold = 0.85`（SPEC_ROOT/06 第 100-101 行冻结值；现行代码 0.80/0.90 为 **Gap**，W-25 修复）。
- `reserve = max(窗口 × 15%, 16384)`（现行代码值，与 Pi `reserveTokens=16384` 精确一致，吸收为正式口径）。
- 行业收敛区间 70–90%（Codex 90%、dsh `min(0.8W, W−O−64K)`、zcode ~73%、Cline `W−40K`）——70/85 在区间内，且更早触发对弱模型更保守。
- token 估算用本仓既有 `estimate_message_tokens`（`context/tokens.py`）+ `builder.usage_snapshot`（#200 已落地），不新造估算器。

### 4.2 确定性裁剪（先于一切 LLM 调用）

判据（W-03 冻结 + 增量）：

1. **supersession 判定**：同一文件同一版本重复读取、同一查询相同结果 hash；无法证明同源/可替代时不剪（W-03 冻结）。
2. **豁免**：被 #346 保护事实引用的结果永不裁（授权记录、精确标识来源）；不设按工具名的静态豁免（静态名单会腐化，动态引用判定才是确定性的）。
3. **配对原子性**：`tool_call_id` 的 call/result 同裁同留；**切点规则：永不在 tool result 处切断**（增量，PORT DESIGN 自 Pi，调研 R R11）。
4. **残留形态**：骨架行 = `结论 + 精确 ID + artifact ref`（W-03 冻结）。**不引 dsh 头尾截断**（8192→头 4096+尾 1024）——那是 dsh 没有统一 ArtifactStore 时的次优解；本仓 ArtifactStore 可回读，骨架行更严。
5. **兜底**：超长单条且无 Artifact 可读回 → 不裁，压力交 §4.5 硬护栏，**不得伪造 ref**。

### 4.3 混合式摘要（程序化预填 + 模型补空）

摘要 8 节契约见 §6。**第 1/2/7/8 节由 harness 程序化生成**（从 SessionEvent/保护事实注册表确定性提取），**模型只写第 3/4/5/6 节**。摘要模型默认主 provider 便宜档；失败代价由 §6.4 闸门兜住 = 放弃本次压缩（安全），而非污染上下文（危险）。

### 4.4 校验闸门（W-01 增量 + 调研 B3 自建）

压缩事件落盘**前**逐条校验，任一不过 → 拒绝本次 compaction、保留原投影：

1. 8 节结构齐全，空节显式写 `(none)`；
2. 程序化节（1/2/7/8）与 harness 重新提取的值**逐字一致**（harness 自己生成的就能自己验）；
3. summary 非空且体积小于被压缩前缀（dsh「摘要未变小则拒绝」）；
4. 保护事实表与 #346 注册值逐字一致；
5. 不产生半个压缩事件、不触发新 Tool 执行（W-01 冻结）。

### 4.5 失败与硬护栏（W-04 冻结 + 增量）

- 摘要模型失败**至多重试一次**，每次失败留诊断与任务可见状态（W-04 冻结，与 dsh `maxOverflowRetries=1`、Pi compact-and-retry、Roo 截 25% 重试同构，调研 R R12）。
- 达硬护栏仍无法形成可核验上下文 → 既有暂停生命周期停住，不发送越窗请求（W-04 冻结）。
- **增量（Anthropic `memory_20250818` 式 warning）**：接近硬护栏时先向模型发系统 warning「即将到达上下文上限，请把关键信息显式落盘（保护事实/进度清单）」，再触发压缩。产品依据：Anthropic API memory tool 接近清除阈值时的自动 warning（调研 R §1.1）。
- **用户消息逐字保留**（调研 B6，Codex 仅二手来源）：用户消息永不被裁剪/摘要改写；因此超限的走硬护栏暂停，不牺牲用户原话。

### 4.6 压缩后确定性重注入

重注入清单（顺序固定）：

1. **保护事实表**（全量，独立预算，不因近期窗口变化被挤掉——#346 冻结语义）；
2. **进度清单**（§7 全表）；
3. **新摘要**（8 节）；
4. **最近修改文件路径清单**（只回路径不读正文；Claude Code 同款：>5000 token 只回 `Referenced file` 路径引用）。

注入节奏：**事件驱动**（清单/保护事实变更即注入）+ **周期兜底每 6 条消息**（Cline Focus Chain 默认值，可配置）。**不做** Claude Code `SessionStart(compact)` hook 自定义注入（仅一家采用，YAGNI）。

## 5. 保护事实（W-02 增量）

fact 类型全集（#346 原有 + **加粗为增量**）：

用户原目标、明确约束/授权及其撤销、验收项与变更、精确标识、已确认的关键决策、已完成/未完成边界、未决 Operation、关键证据 ref、**失败方案**。

「失败方案」结构：`{fact_id, type: "failed_approach", 路径描述, 证伪依据, source_event_id/seq, 状态, session_id}`——证伪依据必须指向证伪事件（测试红/用户否决/运行时错误），不接受「模型觉得不行」。其余契约（字段、来源、撤销语义、独立预算、Fork 继承）按 #346 票面不动。

## 6. 摘要内容契约（逐字规范）

### 6.1 八节结构与生成责任

| 节 | 标题（逐字） | 生成方 | 内容规则 |
|---|---|---|---|
| 1 | `## 原始目标与用户约束` | 程序化 | 从 SessionEvent 用户事件**逐字引用**，禁止改写；多条按时序排列 |
| 2 | `## 保护事实表` | 程序化 | #346 注册表整表复制（含失败方案），结构化值原样，不重述 |
| 3 | `## 已完成工作与关键决策` | 模型 | 每条决策附理由；只写已落盘为事件的事实 |
| 4 | `## 失败方案` | 模型 | 证伪路径 + 证伪依据 + source_event_id；无则写 `(none)` |
| 5 | `## 当前进行中状态` | 模型 | 与进度清单 in_progress 项一致（W-29 联动校验） |
| 6 | `## Next Step` | 模型 | 下一个动作 + 解除条件；无则写 `(none)` |
| 7 | `## 精确标识清单` | 程序化 | 文件路径/命令/ID/错误串逐字提取（dsh "Preserve exact file paths, commands, error strings, identifiers, numeric values"） |
| 8 | `## 文件清单` | 程序化 | 读/改文件列表，**跨压缩累积**（Pi `<read-files>/<modified-files>` 机制） |

### 6.2 滚动合并规则（dsh R3 四条）

不许删节；空节写 `(none)`；逐字保留精确标识；旧摘要保真去陈（新摘要合并旧摘要时，程序化节以最新提取值为准整节替换，模型节保留旧文未被新事实推翻的部分）。

### 6.3 校验与拒绝

按 §4.4 闸门执行；闸门不过 = 该次压缩不发生，原投影保留（W-01 冻结语义）。

## 7. 进度清单（Plan List）

### 7.1 数据模型（JSON schema 校验的 SessionEvent data）

事件类型：`task/plan_updated`（append-only，遵循不变量 #3）。

```json
{
  "items": [
    {
      "id": "稳定字符串，整表覆盖时的对齐键",
      "content": "任务描述（一句话，可执行）",
      "activeForm": "进行中显示形式（Claude Code TaskCreate 同款字段）",
      "status": "pending | in_progress | completed",
      "source": "initializer | user | agent"
    }
  ]
}
```

### 7.2 状态机与不变量（handler 硬校验）

- 三态：`pending → in_progress → completed`；只允许 `pending↔in_progress`、`in_progress→completed`；completed 不可回退（回退=新增项）。
- **单 in_progress**：同一事件 payload 中 `in_progress` 恰为 0 或 1 项；违反 → **拒绝整个更新，不产生事件**，返回完整当前清单 + 错误原因（整表覆盖范式下天然自修复）。依据：全行业无一家硬校验（Codex 源码仅反序列化，渲染端容忍违规），本仓 fail-closed 风格选择硬校验；代价是弱模型违规时多一次带原因的重试。
- **软上限 50 条**：超过 → 拒绝并返回错误「要求合并相邻项」。**无产品先例，工程判断**，W-30 真实模型 Gate 后按实测调整。

### 7.3 工具契约：整表覆盖

工具名 `update_plan`（PORT DESIGN 自 Codex `update_plan` / Gemini `write_todos`）：每次调用提交**全量清单**，harness 校验（§7.2）后落一个 `task/plan_updated` 事件。选整表覆盖而非增量补丁的依据：弱模型不易写坏；天然解决并发合并；Claude Code 增量补丁（TaskCreate/TaskUpdate）是唯一带依赖图的范式，但弱模型容易把状态改乱。

### 7.4 持久化与多端同步

单一事实 = SessionEvent 流（不变量 #22：不维护第二套不可对账真相）；客户端从服务端投影读取。可行性依据：Codex 事件重放（PR #9786）、Cline 文件+Chokidar、zcode 多端共享任务（调研 P）。

### 7.5 渲染契约（四件套，全行业收敛）

N/M 计数、当前项高亮、完成项划线+勾、可折叠列表（zcode 实测 UI、Cline、Claude Code 同构，调研 P）。Web/桌面同一 React 组件（#344 冻结：Electron 承载既有 React Web）；TUI 为独立薄渲染（W-28）。

### 7.6 与 progress.md 的双制品定界

- **进度清单**：JSON schema 校验的 SessionEvent data，面向「agent 状态机 / 多端 UI」，不写入项目文件；
- **`agent-progress/<session-id>/progress.md`**：面向「人 / git」（W-05 冻结：项目根可见、可进 git diff、不自动 add）。
- 两者关系：清单是状态机事实，progress.md 是叙事性记录；内容可重合但**互不为源**——各自从 SessionEvent 投影生成。选择 JSON 承载清单的依据：Anthropic「model is less likely to inappropriately change or overwrite JSON files compared to Markdown files」。

## 8. 复用映射（AGENTS.md §6）

| 项 | 决策 | 出处 |
|---|---|---|
| R1 Tool result 确定性裁剪 | PORT DESIGN（骨架行版，不引头尾截断） | dsh `compaction-tool-result-pruner` |
| R3 8 节硬契约 + 滚动合并 | PORT DESIGN（§6.1 定稿版） | dsh `compaction-basic` |
| R5 压缩后磁盘重注入 | PORT DESIGN（§4.6 定稿版） | Claude Code |
| R7 受限写入结构化清单 | PORT DESIGN（§7 整表覆盖 + 硬校验版） | Anthropic 工程博客 |
| R8 todo 穿透压缩 + 每 6 条重注入 | PORT DESIGN（§4.6 定稿版） | Cline Focus Chain |
| R11 切点规则（永不在 tool result 处切） | PORT DESIGN（并入 W-03 增量） | Pi compaction |
| R12 溢出兜底一次重试 | PORT DESIGN（W-04 已有同语义，对齐确认） | dsh / Pi / Roo |
| ArtifactStore / SessionEvent 投影 | REUSE（本仓既有，不外引） | — |
| B1 保护事实注册表 | BUILD（#346 已占票；无产品做成 harness 一等公民） | 调研 R §4.2 |
| B2 程序化预填 + 模型补空 | BUILD（§4.3；无产品先例，本仓自建，工作量集中 W-04 增量） | 调研 R §4.2 |
| B3 摘要质量校验闸门 | BUILD（§4.4） | 调研 R §4.2 |
| B5 tool result 去重 | BUILD（W-03 supersession 已占） | 调研 R §4.2 |
| B6 用户消息逐字保留 | BUILD（§4.5 增量） | 调研 R §4.2（Codex 二手） |
| R2 dsh 触发公式 | DEFER（已裁决规格 70/85 比例式，仅作注释参考） | dsh |
| R4 字节级重放前缀（KV cache） | DEFER（仅一家采用，W-30 实测后再定） | dsh |
| R6 SessionStart(compact) hook | DEFER（仅一家采用，YAGNI） | Claude Code |
| R9 子代理脏活 | DEFER（#370/#372 未决，仅指引：探索/搜索/大日志分析应委派子代理，主上下文只进摘要——Claude Code 文档原话 "send research to a subagent so the file contents stay in its context window, not yours"） | Claude Code |
| R13 会话制品三件套衔接 | 不开票（W-05/W-06 占 progress.md、W-26 占清单、W-13 占重启 reconcile；本节即衔接说明） | Anthropic 工程博客 |
| Letta 自编辑记忆 / Aider 无结构摘要 / 大窗口硬扛 | **不复制** | 调研 R §4.3 |

实质复制或 Port 上游代码时检查 License 并保留来源（dsh、Pi 均为 MIT；Anthropic/Cline 文档为公开技术博客/文档）。

## 9. 票拆分与依赖链

| 票 | 优先级 | 内容 | 依赖 |
|---|---|---|---|
| W-25 | P1 Bug | 阈值对齐：compactor 默认 0.80/0.90 → 规格 0.70/0.85 | 无 |
| W-26 | P0 Contract | 进度清单服务端契约（§7.1-7.4） | W-02（#346） |
| W-27 | P1 | 清单 Web+桌面渲染（§7.5，同一 React 组件） | W-26 |
| W-28 | P2 | 清单 TUI 渲染（Pi 独立 TUI 包 REUSE 优先） | W-26、W-17 |
| W-29 | P1 | 清单↔压缩锚点集成（§4.6 落地 + 摘要 5/6 节联动校验） | W-26、W-04 增量 |
| W-30 | P1 | W-21 真实模型 Gate 增补判据 | W-29、W-21 |

既有票增量：W-01（§4.4 闸门 + 切点规则）、W-02（§5 失败方案类型）、W-03（切点规则入票面）、W-04（§4.3 混合摘要 + §4.5 warning + §6 契约全文）。

## 10. 验收门禁（全局）

每票沿用 W-01 格式：红证先行 → 可执行命令 + 可判定输出 → 冻结树记录 → review ledger → 明确「不做」。W-30 增补的真实模型判据：

1. 压缩接班后，进度清单与压缩前逐字一致（事件投影重放验证）；
2. 压缩接班后，保护事实（含失败方案）逐项可追到原 Event；
3. 长任务执行中不重走任何已记录失败方案（以失败方案 fact 的 source_event_id 为判据）；
4. 压缩触发时 UI 清单无闪烁/无丢项（真实操作证据，接入 W-20 挑战夹具）。

## 11. 未决问题（诚实清单，施工遇此停下报告）

继承调研 R §5（12 项）与调研 P §7（7 项），与本 PRD 直接相关的：

1. zcode（智谱 ZCode，z.ai）的摘要结构与清单数据模型无公开实现证据——本文相关条目均以「行为观察」为据，未当作实现证据；
2. Claude Code todo 清单存储位置无公开证据——本文选择 SessionEvent 投影是本仓不变量 #22 的独立推论，非追随；
3. 多 Agent 并发写同一清单的合并策略所有产品均无公开方案——本 PRD 以「整表覆盖 + 单写者（服务端串行化）」规避，若未来引入多写者需重新裁决；
4. B6 用户消息逐字保留仅 Codex 二手来源——§4.5 已按最保守语义定稿（永不裁），如实测成本过高走 §9.1.1 变更控制。

## 12. 台账登记行（待 integration 落账）

> 本节内容由 integration line 按 V3.1-lite 格式誊入 `docs/SDD_TICKET_TRACKER.md` 与 `docs/tickets/workbench-2026-09-27/README.md` 索引：W-25~W-30 新票 6 行 + W-01~W-04 修订注记 4 行，日期 2026-09-27，来源：本 PRD 三轮 grill-me 确认稿。
